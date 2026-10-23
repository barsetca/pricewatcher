# PriceWatch - архитектура

## Архитектурный стиль
Небольшой модульный монолит.

Telegram логически является frontend/presentation layer. Основная функциональность находится в application/services. Отдельный backend-сервис, внутренний HTTP API и отдельный deploy frontend для MVP не нужны.

## Направление зависимостей

```text
Telegram frontend
       |
       v
Application services ------------------- CsvService
       |
       +---- ProductService
       +---- MonitoringService ---- NotificationOutboxRepository
       +---- PriceChecker
       +---- OutboxWorker -------- NotificationOutboxRepository
       |          |
       |          +---- TelegramSender ---- Telegram Bot API
       +---- NotificationFormatter
       |
       v
Repositories -------------------------- SQLite

PriceChecker
   |
   +---- Fetchers
   |       +---- RequestsFetcher
   |       +---- PlaywrightFetcher (fallback)
   |
   +---- UniversalExtractor

Scheduler (`schedule`) ---> MonitoringService
```

Недопустимые зависимости:
- `UniversalExtractor -> Telegram`;
- `Repository -> Telegram`;
- `database -> BeautifulSoup`;
- CSV parsing -> прямая отправка Telegram-сообщений;
- Telegram handler -> raw SQL;
- Telegram handler -> `requests`/`Playwright`.

## Граница deployment security

Production scraping исполняется в выделенной Compose bridge-сети `pricewatch_egress` (`pw-egress0`), не в host network. `UrlSecurity` и ручная проверка redirect защищают приложение до соединения; изолированная nftables-таблица host-а — независимый egress-барьер для той же сети. Она не является частью application/domain architecture и применяется только администратором VPS после фактической проверки сети.

## Ответственность компонентов

### Telegram frontend
Отвечает за команды, кнопки, состояния диалога, получение URL/CSV, отображение Product/Offer/истории/настроек, обработку действий пользователя по аномальной цене и выдачу экспортированного CSV.

Не содержит SQL, scraping, расчеты изменения цены и бизнес-правила.

В реализованном M7 state хранится в PTB `user_data` только для private chat. Каждая операция создания/удаления имеет короткий `operation_id`; handler переводит её в `processing` до первого `await` длительной операции. Возврат в меню и `/cancel` очищают state, а рестарт процесса безопасно сбрасывает незавершённые диалоги. С M9.5 все синхронные service calls выполняются через общий daemon-thread adapter; каждый вызов production runner открывает отдельное SQLite connection внутри своего потока.

CSV-flow M8.4 использует тот же `user_data`: хранит один `CsvParseResult`, состояние ожидания/preview/import-running и короткий nonce в `callback_data`. Замена файла или переход в другой раздел синхронно инвалидирует старый callback. Перед worker-вызовом состояние атомарно меняется на import-running; повторные callbacks, новый файл и отмена не запускают вторую операцию. Загруженные и сформированные CSV остаются в памяти как bytes/`BytesIO`, а Telegram-слой только вызывает `CsvService`, `CsvImportService` и `CsvExportService` и отправляет их typed результаты.

### ProductService
Операции пользователя с Product/Offer:
- создать/переименовать/удалить Product;
- добавить/удалить Offer;
- получить список/детали;
- инициировать первичную проверку URL через `PriceChecker`;
- проверять принадлежность данных пользователю и дубликаты.

Для интерактивного создания используются два application use case:

- `preview_offer()` нормализует URL, проверяет ownership/duplicate/SSRF и выполняет не более одной начальной проверки без записи в SQLite;
- `create_product_with_preview()` повторяет актуальные ownership/duplicate-проверки под `BEGIN IMMEDIATE` и атомарно записывает Product, Offer, CheckAttempt и, при надёжной цене, PriceHistory.

Scraping никогда не выполняется внутри write transaction. Preview является короткоживущим DTO диалога, действует 15 минут и после рестарта отбрасывается. Обычный `create_product_with_offer()` использует тот же preview/finalize контракт.

Отдельный `OfferService` для MVP не нужен.

### PriceChecker
Оркестрирует одну проверку URL: выбирает разрешенную fetch-стратегию, получает страницу, передает ее extractor и возвращает структурированный результат.

Не решает, надо ли уведомлять о падении цены или считать цену аномальной.

### Fetchers
Fetcher только получает содержимое страницы.

`RequestsFetcher` - основной путь, обязательный для соответствия исходному заданию и использует `requests`.

С M10.2 streamed body по-прежнему ограничен 2 MB, но декодируется из bytes по
явному HTTP charset, затем по HTML charset declaration, затем как строгий UTF-8
и только после этого через Windows-1252 fallback. Значение
`requests.Response.encoding = ISO-8859-1`, автоматически назначаемое для
`text/html` без charset, не считается явным указанием сайта. Это исключает
mojibake испанских названий и символа евро, не меняя fetch/security-контракт.

`PlaywrightFetcher` - дополнительный fallback. Исследование показало, что браузер не универсально лучше `requests`, поэтому он не должен автоматически заменять основной путь.

Успешный метод может сохраняться как `preferred_fetch_method`. Точная fallback-политика фиксируется на этапе `PriceChecker`.

### UniversalExtractor
Получает HTML и извлекает данные товара. Использует `beautifulsoup4` там, где требуется разбор HTML.

Приоритет источников:
1. JSON-LD `Product` / `Offer` / `AggregateOffer`;
2. Schema.org microdata;
3. metadata/OpenGraph/product metadata;
4. универсальные DOM-эвристики.

Нельзя брать первое попавшееся число с символом валюты: на странице могут быть старая цена, доставка, аксессуары, рассрочка, marketplace sellers, варианты товара.

При неоднозначности вернуть `AMBIGUOUS`, а не уверенно записать неверную цену.

С M10.2 JSON-LD сначала собирается в общий граф из отдельных scripts и
`@graph`: индексируются `@id`, разрешаются `mainEntity`, Product → Offer и
вложенные Offer/AggregateOffer. Точный Offer выбирается только при достаточном
соответствии карточке по final URL, Product/Offer URL, SKU/MPN/GTIN,
`mainEntity` или точному нормализованному имени. URL identity сохраняет query
дословно и не считает один домен доказательством совпадения; отсутствие
идентификатора само по себе не является конфликтом. Явно несовпадающий вариант
исключается, а не получает высокий confidence. `lowPrice`/`highPrice`
AggregateOffer никогда не становятся точной текущей ценой.

BeautifulSoup применяется в production ко всему документу и реально извлекает
microdata, price metadata, семантические/data-* HTML price elements и основной
price block. DOM-слой исключает old/PVPR/MSRP, discount, historical/30-day,
delivery/installment и цены ссылок на соседние варианты. Согласованные
независимые источники повышают confidence на 0.03 (не выше 0.99); конфликт
единственных выбранных цен даёт `AMBIGUOUS`. Точный Offer URL может иметь
приоритет над неоднозначным общим DOM-набором только когда этот набор содержит
ту же цену; правило и причина выбора сохраняются в diagnostics.

С M10.2.1 явное JSON-LD несовпадение варианта не может быть перекрыто
непривязанной DOM-ценой. В таком конфликте `SUCCESS` допускается только для
HTML-кандидата, чей собственный или родительский блок явно связан с final URL
(`data-product-url`/`data-url`) либо с подтверждённым SKU/MPN/GTIN страницы.
DOM-кандидаты блока другого варианта отбрасываются; отсутствие identity по-
прежнему совместимо с обычной страницей, где нет явного конфликта.

С M10.3 DOM-фильтр проверяет не только сам price element, но и семантику его
ancestor-контейнеров. Цены из явно обозначенных блоков old/original,
recommendation/related/accessory, bundle/combo/total, unit price,
delivery/installment и historical price не участвуют в выборе основной цены.
Правило является общим и не зависит от домена магазина. Несколько вариантов
без надёжной URL/SKU-привязки по-прежнему дают `AMBIGUOUS`. DOM identity
собирается по всей цепочке price element → ancestors: наличие явного mismatch
блокирует цену даже при вложенном совпадающем атрибуте. Если все найденные
цены отклонены таким конфликтом, результат остаётся `AMBIGUOUS`, а не
превращается в `PARTIAL` или `SUCCESS`.

С M10.7 заголовок результата выбирается отдельно от primary-источника цены.
Точное навигационное значение (`Home`, `Homepage`, `Inicio`, `Página de
inicio`, `Startseite`, `Accueil`) не считается названием товара: extractor
переходит к следующему структурированному, OpenGraph или document title, а при
отсутствии информативной альтернативы возвращает `title=None`. Проверка точная,
не отбрасывает названия, лишь содержащие эти слова, и не меняет выбор цены,
валюты, identity, extraction method/status или confidence.

### MonitoringService
Автоматический workflow проверки:
- получить активные Offers;
- вызвать `PriceChecker`;
- записать `CheckAttempt`;
- применить правила ошибок и аномалий;
- сравнить новую подтвержденную цену с предыдущей подтвержденной ценой;
- сохранить новую цену в `price_history`;
- атомарно сохранить Outbox snapshot для `PRICE_DROP`, новой
  `ANOMALY_DETECTED` или первого `REPEATED_ERROR` в эпизоде.

Получение страницы выполняется до `BEGIN IMMEDIATE`. В M9.2 изменения Offer,
`CheckAttempt`, `PriceHistory` и соответствующий `notification_outbox` входят в
одну SQLite-транзакцию; ошибка enqueue откатывает её целиком. После commit
возвращаются те же `MonitoringResult.events`, что и до интеграции. Получателем
Outbox является `users.telegram_user_id`, а не внутренний `users.id`.

С M10.7 неуспешная проверка Offer с уже существующей подтверждённой ценой
сохраняет `ACTIVE` при `consecutive_errors=1` и `2`; третья последовательная
ошибка переводит его в `ERROR` и создаёт первое `REPEATED_ERROR`. Offer без
подтверждённой цены остаётся `PENDING`, успех сбрасывает эпизод ошибок, а
особые состояния `SUSPICIOUS` и `DISABLED` сохраняют прежнюю семантику.

### price_rules
Небольшие чистые функции:
- процент изменения;
- определение снижения более 10%;
- определение аномального изменения >= 50%.

### CsvService
`CsvService` чисто разбирает/валидирует вход и формирует preview. `CsvImportService` построчно оркестрирует импорт, не содержит SQL и использует те же preview/finalize-операции `ProductService`, что и ручное добавление. `CsvExportService` формирует UTF-8 CSV текущих Offers и отчёт уже выполненного импорта: он читает данные только через публичные методы `ProductService`, не запускает scraping и ничего не записывает. Чистая D39-нормализация имён находится в нейтральном `services/product_names.py` и одинаково используется CSV и `ProductService`.

### OutboxWorker, NotificationFormatter и TelegramSender

В M9.3 `OutboxWorker` восстанавливает истёкшие аренды, выбирает до 20 готовых
записей, атомарно фиксирует `claim()` на 300 секунд и закрывает SQLite connection
до сетевого вызова. После отправки он в отдельной короткой transaction выполняет
`mark_sent()`, `reschedule()` или `mark_failed()` только с актуальным
`lease_token`.

С M9.4 `NotificationFormatter` строит окончательный русский plain-text только
из сохранённого JSON snapshot, переводит UTC в `Europe/Madrid` через `zoneinfo`
и добавляет inline-кнопки только для anomaly. Telegram parse mode не
используется. Formatter не читает Product/Offer из БД и не меняет anomaly.

Anomaly callback сначала сопоставляется с единственной `SENT` Outbox-записью
по `(chat_id, telegram_message_id)`, затем проверяются recipient, event type,
payload v1, `offer_id`, `pending_check_attempt_id`, текущий владелец и pending
state. Только после этого Telegram layer вызывает существующий
`confirm_anomaly()` или `reject_anomaly()`. Успешное решение редактирует исходное
сообщение и удаляет keyboard; stale действие удаляет keyboard, если источник
действителен. Ошибка Telegram edit не откатывает и не повторяет решение SQLite.

Ручная проверка по-прежнему вызывает один `check_for_user()`. Если результат
создал events, async `notification_wakeup` после Telegram summary быстро ставит
coalescing-сигнал единственному Delivery Coordinator. Handler не запускает
worker и не ждёт фактической доставки.

`TelegramSender` — отдельный асинхронный адаптер над `Bot.send_message()`. Он
принимает `chat_id`, текст и optional `reply_markup`, возвращает `message_id`,
использует timeout не более 30 секунд и не обращается к SQLite. Исключения PTB
передаются worker-у для классификации.

Retry рассчитывается от времени завершения ошибки: 1/5/15 минут, затем 60 минут
без общего лимита. Более длинный Telegram `RetryAfter` имеет приоритет.
`Forbidden`/`BadRequest` постоянны; network/timeout и неизвестные исключения
переносятся. Ошибка одной записи не блокирует следующие. SQLite-ошибки не
классифицируются как Telegram-ошибки.

Delivery имеет гарантию at-least-once: сбой после принятия сообщения Telegram,
но до `mark_sent()`, может привести к повторной доставке после recovery.
Exactly-once для внешнего Telegram API не заявляется.

### Delivery Coordinator

M9.5 объединяет startup, периодический 60-секундный tick, manual wakeup и wakeup
после автоматического batch через один `asyncio.Event`. Компонент владеет одним
`OutboxWorker`; `asyncio.Lock` и единственный lifecycle task исключают
параллельные `run_once()`. Сигнал во время цикла остаётся установленным и даёт
один последующий цикл, а несколько сигналов объединяются без создания задач на
каждый wakeup. Полная пачка из 20 записей планирует следующий последовательный
цикл с явной передачей управления event loop.

Синхронные SQLite-фазы worker выполняются вне основного event loop. Каждый
daemon worker thread открывает и закрывает собственное соединение; Telegram Bot
API остаётся в основном asyncio loop. Это сохраняет отзывчивость polling и
позволяет соблюдать ограниченный shutdown без небезопасного закрытия SQLite из
чужого потока.

### Scheduler

`DailyMonitoringScheduler` использует единственный экземпляр библиотеки
`schedule` с timezone `UTC` и job на 03:00. Он вызывает существующий
`MonitoringService.check_all()` целиком в daemon worker thread; service graph и
SQLite connection создаются и закрываются в этом же потоке. Scheduler не
дублирует scraping или price rules.

Последний наступивший UTC-слот атомарно резервируется в `scheduler_runs` через
`BEGIN IMMEDIATE`. При первом старте до 03:00 scheduler ждёт; после 03:00 и после
нескольких пропущенных дней выполняет только один последний слот. `SUCCESS` и
`FAILED` терминальны. Stale `RUNNING` помечается `INTERRUPTED` и допускает ровно
одну recovery-попытку. Пара `scheduled_for`/`started_at` сохраняет фактическую
задержку; delayed startup дополнительно логируется. Из-за аварии между HTTP и
фиксацией результата exactly-once batch execution не заявляется.

### Application lifecycle

`PriceWatchRuntime` явно выполняет PTB 22.8 lifecycle без `run_polling()` внутри
активного loop: `initialize` → `updater.start_polling` → `start`, затем запускает
Coordinator/Scheduler; остановка выполняет обратную управляемую
последовательность. SIGTERM/SIGINT переводят CREATED/STARTING/RUNNING в
STOPPING/STOPPED. Все компоненты делят один shutdown budget 30 секунд. Ошибка
одного monitoring job записывается как `FAILED` и не останавливает Telegram или
Outbox; критическая потеря scheduler/coordinator из-за SQLite/infrastructure
завершает runtime с ненулевым кодом.

До создания runtime application entrypoint берёт межпроцессный lifecycle lock
на отдельном файле рядом с канонизированным `Settings.database_path`. На Linux
это non-blocking `fcntl.flock(LOCK_EX)`: descriptor остаётся открытым до конца
process lifetime, поэтому второй процесс с той же SQLite не доходит до
bootstrap, Telegram polling, scheduler или Outbox. Lock-файл намеренно не
удаляется; штатное освобождение descriptor и закрытие ОС после SIGKILL делают
повторный запуск возможным без manual cleanup. Разные SQLite или разные
filesystem не образуют общий lock и не гарантируют глобальную защиту одного
Telegram token.

### Repositories
Граница persistence для SQLite (`sqlite3`). Raw SQL находится здесь/в database layer, но не в services/Telegram.

Ожидаемые repositories:
- `UserRepository`;
- `ProductRepository`;
- `OfferRepository`;
- `PriceHistoryRepository`;
- `CheckAttemptRepository`.
- `NotificationOutboxRepository`.
- `SchedulerRunRepository`.

## Domain и технические модели
В `domain/` находятся `User`, `Product`, `Offer`, `PriceRecord`, `CheckAttempt`, `NotificationOutbox`, `SchedulerRun` и бизнес/infrastructure enum.

В `scraping/models.py` находятся технические DTO/result: `FetchResult`, `ExtractionResult`, `PriceCheckResult`.

Отдельный дублирующий ORM/database model layer в MVP не нужен. Repository преобразует SQLite rows в domain-модели.

## Enum
### FetchStatus
- `SUCCESS`
- `BLOCKED`
- `ROBOTS_DENIED`
- `HTTP_ERROR`
- `TIMEOUT`
- `NETWORK_ERROR`

### ExtractionStatus
- `SUCCESS`
- `PARTIAL`
- `AMBIGUOUS`
- `PARSE_ERROR`
- `NOT_RUN`

### OfferStatus
- `ACTIVE`
- `PENDING`
- `ERROR`
- `SUSPICIOUS`
- `DISABLED`

Ошибки отдельной проверки не копируются в `OfferStatus`. Например, `ACTIVE` Offer после одного `TIMEOUT` остается `ACTIVE` до срабатывания правила повторных ошибок.

### FetchMethod
- `REQUESTS`
- `PLAYWRIGHT`

### ExtractionMethod
- `JSON_LD`
- `MICRODATA`
- `METADATA`
- `DOM_HEURISTIC`
- `NONE`

### AnomalyDecision
- `CONFIRMED`
- `REJECTED`

## Confidence
Источник истины - один `confidence_score` от 0.0 до 1.0. Метки HIGH/MEDIUM/LOW вычисляются, а не хранятся дублирующе.

Начальные границы:
- >= 0.85 - HIGH;
- >= 0.60 и < 0.85 - MEDIUM;
- < 0.60 - LOW.

## Целевая структура

```text
pricewatch/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── src/
│   └── pricewatch/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── domain/
│       │   ├── __init__.py
│       │   ├── models.py
│       │   └── enums.py
│       ├── frontend/
│       │   └── telegram/
│       │       ├── __init__.py
│       │       ├── bot.py
│       │       ├── handlers.py
│       │       ├── keyboards.py
│       │       └── messages.py
│       ├── services/
│       │   ├── __init__.py
│       │   ├── product_service.py
│       │   ├── monitoring_service.py
│       │   ├── price_checker.py
│       │   ├── price_rules.py
│       │   ├── csv_service.py
│       │   └── notification_service.py
│       ├── scraping/
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── requests_fetcher.py
│       │   ├── playwright_fetcher.py
│       │   └── extractor.py
│       ├── database/
│       │   ├── __init__.py
│       │   ├── connection.py
│       │   ├── schema.sql
│       │   └── repositories/
│       │       ├── __init__.py
│       │       ├── user_repository.py
│       │       ├── product_repository.py
│       │       ├── offer_repository.py
│       │       ├── price_history_repository.py
│       │       └── check_attempt_repository.py
│       └── scheduler.py
├── tests/
│   ├── domain/
│   ├── services/
│   ├── scraping/
│   ├── database/
│   └── fixtures/
├── data/
├── docs/
└── research/
    └── parser-research/
```

Это целевая структура, а не требование заранее создавать пустые файлы будущих этапов.
