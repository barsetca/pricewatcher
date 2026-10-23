# PriceWatch

PriceWatch — локально запускаемый Telegram-бот для мониторинга публичных цен. Один `Product` объединяет одно или несколько магазинных `Offer`; подтверждённые цены сохраняются в SQLite, а подозрительные изменения требуют решения пользователя.

Целевой вариант эксплуатации в текущем плане — локальный Docker Compose.
Публичное или удалённое production-развёртывание в текущий план не входит.

Локальный Compose runtime прошёл offline-проверки и ручную приёмку. Это
подтверждает работоспособность локального варианта эксплуатации, но не
объявляет проект готовым к публичному или удалённому production deployment.

Telegram UI, ежедневный monitoring scheduler и доставка уведомлений работают в
одном application runtime. Плановая проверка выполняется в 03:00 UTC, Outbox
обрабатывается сразу при старте, по быстрому сигналу и с 60-секундным
периодическим ожиданием между последовательными циклами. Initial, manual и
scheduled проверки используют `Offer.normalized_url`; исходный
пользовательский `Offer.url` сохраняется для отображения.

## Документация

- [Руководство пользователя](docs/PriceWatch_User_Guide_RU.pdf) — понятная
  инструкция по работе с Telegram-ботом: товары, предложения, проверки,
  уведомления, пороги и CSV.
- [Скриншоты работы бота](docs/Screenshots.pdf) — визуальная демонстрация
  основных пользовательских сценариев и экранов PriceWatch.
- [Техническое задание](docs/PriceWatch_Technical_Assignment_RU.docx) —
  описание проекта, целевой аудитории, требований, ограничений и рисков.
- [Границы проекта](docs/PROJECT_SCOPE.md) — назначение MVP, модель
  Product/Offer, правила CSV и функции, не входящие в проект.
- [Бизнес-правила](docs/BUSINESS_RULES.md) — правила сравнения цен,
  уведомлений, аномалий, повторных ошибок и импорта.
- [Архитектура](docs/ARCHITECTURE.md) — состав приложения и взаимодействие его
  основных компонентов.
- [База данных](docs/DATABASE.md) — структура хранимых данных и назначение
  основных сущностей.
- [Развёртывание](docs/DEPLOYMENT.md) — инструкции локального запуска и
  дополнительные рекомендации по сетевой безопасности.

## Создание Telegram-бота

1. Откройте официальный чат `@BotFather` в Telegram и отправьте `/newbot`.
2. Задайте отображаемое имя и уникальный username, оканчивающийся на `bot`.
3. Скопируйте выданный HTTP API token. Это секрет: не публикуйте его, не добавляйте в Git и отзовите через BotFather при утечке.

Для whitelist нужен числовой Telegram **user ID**, а не `@username` и не ID группового чата. Безопасный вариант — написать боту, которому вы доверяете и который показывает ваш числовой ID, либо временно использовать официальный Bot API `getUpdates` только с собственным новым bot token и прочитать `message.from.id`. Не публикуйте ответ API: в URL запроса находится token.

PriceWatch принимает действия только из private chat и только от ID из whitelist.

## Конфигурация

```bash
cp .env.example .env
# Ограничьте чтение файла текущим пользователем.
chmod 600 .env
```

Заполните:

- `TELEGRAM_BOT_TOKEN` — обязательный token от BotFather;
- `TELEGRAM_ALLOWED_USER_IDS` — обязательные положительные user ID через запятую, например `123456789,987654321`;
- `TELEGRAM_MODE` — необязательный режим, сейчас только `polling`, default `polling`;
- `PRICEWATCH_DATABASE_PATH` — путь SQLite, default `data/pricewatch.db`;
- `PRICEWATCH_LOG_LEVEL` — `DEBUG`, `INFO`, `WARNING` или `ERROR`, default `INFO`.

`.env` включён в `.gitignore` и `.dockerignore`. `.env.example` содержит только
безопасный пример и пустые секреты. Не сохраняйте полный вывод
`docker compose config`: эта команда разворачивает значения `env_file`, включая
token. Для проверки синтаксиса используйте только `docker compose config --quiet`.
SQLite bootstrap и offline-тесты не требуют Telegram credentials.

## Локальный Python для разработки (альтернатива Compose)

Требуется Python 3.10+.

Для обычной локальной эксплуатации используйте раздел Docker Compose ниже.
Этот вариант предназначен для development и tests; не направляйте его на ту же
SQLite, которую в данный момент использует Compose.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните .env
PYTHONPATH=src python3 -m pricewatch.main
PYTHONPATH=src python3 -m pricewatch.telegram
```

Обе команды запускают один и тот же runtime: создают/обновляют схему SQLite, проверяют Telegram-конфигурацию, запускают long polling, Delivery Coordinator и ежедневный scheduler. Используйте только одну из них. Остановка — `Ctrl+C`; общий graceful shutdown budget составляет 30 секунд. Импорт модулей сам polling и сетевые операции не запускает.

`scheduler_runs` хранит UTC-слоты плановой проверки. При старте после пропущенного 03:00 выполняется только один актуальный компенсирующий batch. Завершённые `SUCCESS`/`FAILED` слоты автоматически не повторяются; stale `RUNNING` получает не более одной восстановительной попытки.

Проверки разработчика:

```bash
PYTHONPATH=src python3 -m compileall -q src/pricewatch
PYTHONPATH=src python3 -m pytest -q
```

Тесты подменяют Telegram transport, DNS и магазинные HTTP-ответы: реальный token и сеть им не нужны.

## Локальная эксплуатация через Docker Compose

```bash
docker compose config --quiet
docker compose up --build -d
docker compose logs -f --tail=100 pricewatch
docker compose stop
docker compose start
docker compose down
docker compose up -d
```

Это основной способ локальной эксплуатации. Не публикуйте обычный вывод
`docker compose config`: `env_file` содержит token. Для остановки без удаления
данных используйте `docker compose stop`; `docker compose down` также сохраняет
именованный volume и подходит для штатного пересоздания контейнера, если не
добавлять `-v`. Не используйте `docker compose down -v`, если данные нужно
сохранить.

Docker image собирается на базе `python:3.12-slim-bookworm`. Принятый runtime
содержал Python 3.12.15, Debian 12 (bookworm) и OpenSSL 3.0.22.

Правила просмотра и безопасного сбора журналов описаны в разделе
«Логирование и диагностика» ниже.

SQLite и lifecycle lock находятся в Compose-volume `pricewatch-data-local`
(фактическое имя Docker — `pricewatcher_pricewatch-data-local`) и сохраняются
после stop/start и `down`/`up`. До Telegram polling
runtime захватывает OS advisory lock в `/data/pricewatch.db.lock`; второй
экземпляр с той же SQLite завершится с кодом 3 и не запустит polling. Lock-файл
намеренно остаётся на volume, но открытый file descriptor освобождается при
штатном завершении, SIGTERM/SIGINT или аварийном завершении процесса.

Контейнер не публикует порты, работает non-root, с read-only root filesystem,
сброшенными capabilities и ограничениями CPU/RAM/PID. Не запускайте второй
service и не используйте Compose scale для Telegram polling.

Scraping требует исходящего HTTPS/DNS-доступа. Compose bridge сам по себе не гарантирует egress isolation от private/link-local сетей. Инструкции nftables и проверка находятся в `docs/DEPLOYMENT.md`.

Локальная тестовая эксплуатация через Docker Compose принята. Реальный
публичный или удалённый production deployment не входит в текущий scope и не
проходил infrastructure/security acceptance. До такого deployment необходимо
отдельно определить окружение и проверить egress isolation, firewall/DNS,
управление секретами, backup/restore, monitoring и операционные процедуры.

## Логирование и диагностика

PriceWatch пишет application logs в стандартный поток ошибок процесса, а не в
отдельный файл приложения; Docker собирает его вместе с container stdout/stderr
logs. Каждая запись имеет формат:

```text
2026-10-22T12:34:56Z | INFO | pricewatch.runtime | event=runtime_started outcome=success duration_ms=123
```

Timestamp всегда в UTC (`Z`), далее следуют level, имя logger и сообщение.
Поддерживаются уровни `DEBUG`, `INFO`, `WARNING`, `ERROR`; default — `INFO`.
Уровень задаётся только существующей переменной `PRICEWATCH_LOG_LEVEL` в
`.env`. Конфигурация читается при старте процесса. После изменения `.env`
Compose-контейнер нужно пересоздать, чтобы он получил новое окружение:

```bash
docker compose up -d --force-recreate pricewatch
```

Обычный `docker compose restart` перезапускает процесс, но не перечитывает
изменённый `.env`. `DEBUG` следует включать только на время локальной
диагностики: журнал становится подробнее, но гарантии отсутствия чувствительных
данных всё равно нет.

### Просмотр Docker Compose logs

Фактическое имя Compose service — `pricewatch`.

```bash
# Последние 100 строк и возврат в shell.
docker compose logs --tail=100 pricewatch

# Последние 100 строк и дальнейший вывод в реальном времени.
docker compose logs -f --tail=100 pricewatch

# Ограниченный временной фрагмент.
docker compose logs --since=30m --tail=300 pricewatch

# Только записи WARNING и ERROR из ограниченного фрагмента.
docker compose logs --since=30m --tail=300 pricewatch \
  | grep -E ' \| (WARNING|ERROR) \| '
```

`Ctrl+C` останавливает только режим follow и не останавливает контейнер. Для
обычной диагностики не нужно открывать или изменять SQLite: lifecycle, batch,
проверки Offer, доставка и CSV отражаются operational events в журнале.

Если фрагмент нужно передать для диагностики, сначала сохраните только
ограниченный объём без ANSI-цвета, затем просмотрите и обезличьте файл локально:

```bash
docker compose logs --since=15m --tail=200 --no-color pricewatch > pricewatch-logs.txt
```

Не прикладывайте весь журнал, `.env`, SQLite, полный вывод `docker compose
config`, Telegram updates, CSV-файлы или headers. Удалите из фрагмента token,
полные URL и query string, персональные данные, Telegram/chat/user/message ID,
названия товаров и иные бизнес-данные, которые не нужны для разбора.

### Rotation и retention

В текущем `compose.yaml` используется Docker logging driver `json-file` с
`max-size: 10m` и `max-file: 3`. Это rotation контейнерных stdout/stderr logs,
а не файлы внутри приложения. Старые части журнала удаляются при ротации;
удаление или пересоздание контейнера также не является способом долговременного
хранения его logs. Перед `down`/recreate сохраните небольшой обезличенный
фрагмент, если он нужен для расследования.

При запуске через локальный Python место хранения и retention определяются
terminal, перенаправлением shell или process supervisor: само приложение
rotation не настраивает.

### Operational events

| Подсистема | Основные события | Значение |
| --- | --- | --- |
| Runtime | `runtime_starting`, `runtime_started`, `runtime_stopping`, `runtime_stopped`, `runtime_failed` | Жизненный цикл процесса |
| Scheduler | `batch_started`, `batch_completed`, `batch_failed` | Дневная проверка и её итог |
| Monitoring | `offer_check_started`, `offer_check_completed`, `offer_check_failed` | Проверка отдельного Offer |
| Monitoring events | `event_created` | Business event зафиксирован в SQLite |
| Outbox | `outbox_delivery_completed`, `outbox_delivery_retry`, `outbox_delivery_failed`, `outbox_delivery_ack_uncertain` | Результат доставки уведомления |
| CSV | `csv_import_started`, `csv_import_completed`, `csv_import_failed`, `csv_import_interrupted`, `csv_export_started`, `csv_export_completed`, `csv_export_failed` | Импорт и экспорт |
| Telegram | `telegram_callback`, `telegram_unhandled_error`, `telegram_recovery_failed` | Ошибка callback/handler и попытка безопасного recovery |
| Database | `database_bootstrap_failed` | Ошибка атомарной инициализации схемы |

Дополнительно Outbox пишет `outbox_lease_recovered`, `outbox_lease_lost`,
`outbox_cycle_completed`/`outbox_cycle_failed`, а ошибочная CSV-строка —
`csv_row_failed`. `event_created` подтверждает commit business event в SQLite,
но не его доставку в Telegram. `batch_completed outcome=partial` означает, что
обход закончен с одним или несколькими локальными `INTERNAL_ERROR`; scheduler
slot при этом получает `SUCCESS`, поскольку остальные Offers были обработаны.

Поля operational events ограничены служебными значениями:
`event`, `action`, `outcome`, фиксированный `reason`, внутренние operation/run/
Offer/Product/Outbox IDs, status, counters, duration, HTTP status и
`exception_class`. Эти ID помогают сопоставить события, но сами по себе не
являются доказательством успешной business mutation: ориентируйтесь на
`outcome` и terminal event.

При нормальной работе ожидаемы `INFO` lifecycle/start/completed events.
`WARNING` обычно означает ожидаемую деградацию, retry или изолированную ошибку;
`ERROR` — failure текущей операции либо инфраструктуры. Сам уровень не
определяет, остановился ли runtime: проверяйте `event`, `outcome` и последующие
записи.

## Обработка ошибок и восстановление

### Startup и runtime

- Ошибка конфигурации завершает процесс с кодом `2`, занятый lifecycle lock —
  с кодом `3`, прочая startup/runtime failure — с кодом `1`; штатная остановка
  и `Ctrl+C` дают код `0`.
- SQLite bootstrap выполняется одной transaction. При DDL/legacy migration
  failure активная transaction откатывается, пишется
  `database_bootstrap_failed`, и startup завершается nonzero. Ручное изменение
  частично созданной схемы не требуется и не рекомендуется: устраните причину
  и явно перезапустите приложение.
- При частично успешном startup runtime очищает уже запущенные компоненты.
  Graceful shutdown использует общий budget 30 секунд. Фатальный сбой
  scheduler или Outbox coordinator запрашивает остановку всего runtime.

### Monitoring и scheduler

- Ожидаемые HTTP/network/robots/extraction failures превращаются в
  типизированный результат, сохраняются как CheckAttempt и обрабатываются
  business rules. Успешная надёжная цена создаёт baseline/историю либо обычное
  обновление; подозрительное изменение остаётся `SUSPICIOUS` до решения
  пользователя. Для ранее активного Offer первые две последовательные ошибки
  сохраняют `ACTIVE`, третья переводит его в `ERROR` и один раз создаёт
  `REPEATED_ERROR`; исходные `PENDING`, `SUSPICIOUS` и `DISABLED` сохраняют
  свои отдельные правила состояния.
- Неожиданная ошибка checker изолируется в пределах одного Offer:
  `offer_check_failed outcome=internal_error`; этот Offer не изменяет
  CheckAttempt/Offer/history/Outbox, а остальные Offers продолжают проверяться.
- Ошибка SQLite/persistence не маскируется как ошибка магазина: она прерывает
  batch и создаёт `batch_failed`. Завершённый `FAILED` slot автоматически не
  повторяется. При startup stale `RUNNING` помечается `INTERRUPTED` и получает
  не более одной предусмотренной восстановительной попытки.

### Outbox и доставка Telegram

- `RetryAfter`, timeout и network error автоматически переносятся без общего
  лимита попыток. Задержки после последовательных claims: 60, 300, 900 и далее
  3600 секунд; для `RetryAfter` берётся не меньше значения Telegram.
- `Forbidden`/`BadRequest` и невалидный notification payload терминальны и
  переводят запись в `FAILED`. Неизвестная ошибка повторяется до пятого claim
  включительно и затем становится terminal `FAILED`.
- Истёкший lease возвращается в очередь с сохранением attempt count.
  `outbox_delivery_completed` означает и успешную отправку, и подтверждённый
  commit `SENT`.
- `outbox_delivery_ack_uncertain` означает, что Telegram sender уже сообщил об
  успехе, но commit `SENT` не подтверждён или lease потерян. После recovery
  возможна повторная доставка: текущая гарантия — at-least-once, не
  exactly-once. Не удаляйте и не правьте Outbox вручную для «починки».

### CSV и Telegram handlers

- CSV import обрабатывает строки независимо: validation и неожиданная ошибка
  одной строки не отменяют уже применённые строки и не мешают следующим.
  Пользователю возвращаются безопасные категории причин без raw exception
  text. Ошибка SQLite прерывает оставшуюся часть import без автоматического
  retry.
- `csv_import_interrupted outcome=uncertain` означает отмену ожидания async
  task, а не доказанную остановку daemon worker. Durable journal/resume нет;
  после проверки результата новый import запускается только явным действием
  пользователя. Duplicate detection продолжает действовать.
- Global Telegram error handler пишет класс исключения и тип update, пытается
  показать общий recovery screen и прекращает дальнейшую обработку этого
  update. Он не повторяет business action автоматически; после проверки
  состояния пользователь может повторить действие явно.
- Локальные handler/callback failures сохраняют свои безопасные recovery
  экраны. Owner checks и защита stale callbacks продолжают применяться: ошибка
  не даёт доступа к чужому объекту и не восстанавливает устаревшую mutation.

### Безопасность журналов

Formatter маскирует известный Telegram token, token-подобные значения,
`Authorization` и query/fragment URL как `<redacted>`. Transport loggers
`httpx`, `httpcore`, `urllib3` и `telegram` не опускаются ниже `WARNING` даже
при общем `DEBUG`. Это best-effort защита, а не обещание, что любой будущий или
сторонний текст безопасен. В журнал намеренно не должны попадать raw Telegram
update/callback payload, CSV contents, headers, полный URL, database path, raw
SQL или exception text; перед передачей фрагмента всё равно нужна ручная
проверка.

## Пользовательская инструкция

После `/start` доступны шесть кнопок.

### Добавление товара

1. Нажмите «Добавить товар» и отправьте публичный HTTP(S) URL.
2. Бот проверит синтаксис и SSRF-ограничения и выполнит одну предварительную проверку страницы вне asyncio event loop.
3. Примите предложенный `source_title` либо введите своё название.
4. Проверьте итог и нажмите «Подтвердить».

До подтверждения SQLite не изменяется. Если URL допустим, но надёжной цены нет, Offer сохраняется как `PENDING`. `/cancel` и возврат в главное меню отменяют любой незавершённый диалог.

### Товары и предложения

«Мои товары» открывает постраничный список. Карточка Product показывает количество Offers, пользовательский, собственный и effective threshold. Из неё можно без ввода внутренних ID:

- добавить Offer по URL;
- переименовать Product;
- установить Product threshold или вернуть наследование;
- удалить Product после подтверждения.

Карточка Offer показывает URL/source, статус с объяснением, confirmed и pending price отдельно, время последней проверки и до десяти датированных записей истории. Доступны «Проверить сейчас» и удаление с подтверждением. Подозрительная цена подтверждается или отклоняется кнопками в соответствующем Telegram-уведомлении.

### Пороги и аномалии

«Настройки» меняют пользовательский threshold; допустимы значения от 0 до 100, точка или запятая как десятичный разделитель. Product threshold переопределяет пользовательский, а «Наследовать» возвращает default пользователя. Событие `PRICE_DROP` возникает только при снижении строго больше effective threshold.

`SUSPICIOUS` означает, что обнаруженная цена не стала confirmed. Кнопка «✅ Подтвердить» в уведомлении принимает её и добавляет одну запись истории; «❌ Отклонить» сохраняет прежнюю confirmed price. Старые и повторные кнопки новую аномалию не изменяют.

## Troubleshooting

| Симптом | Что проверить | Действие |
| --- | --- | --- |
| Контейнер не запускается или перезапускается | `docker compose ps -a pricewatch` и `docker compose logs --tail=100 pricewatch`; ищите configuration, lifecycle lock, `database_bootstrap_failed` или `runtime_failed` | Исправьте `.env`, права/volume или доступ к SQLite и пересоздайте service. Не удаляйте `.lock` и не редактируйте схему вручную |
| Invalid token / пустой whitelist | Configuration error и exit code `2` | Скопируйте token BotFather без пробелов; задайте хотя бы один положительный числовой user ID, не username |
| Conflict: another `getUpdates` request | Другой процесс использует тот же token | Остановите второй polling-процесс; не масштабируйте service |
| `batch_failed` | `exception_class`, соседние `offer_check_failed`, состояние SQLite/volume | Устраните инфраструктурную причину. Этот завершённый slot автоматически не повторится; нужные Offer можно проверить явно или дождаться следующего slot |
| Offer не обновился | `offer_check_completed` с типизированной деградацией либо `offer_check_failed outcome=internal_error` | Проверьте доступность сайта/robots/extraction. PriceWatch не обходит CAPTCHA/anti-bot; immediate retry не гарантируется |
| Уведомление не пришло | `outbox_delivery_retry`, `outbox_delivery_failed`, `outbox_cycle_failed` | Для transient failure дождитесь автоматического retry. Для terminal invalid/permanent failure исправьте причину; не правьте Outbox вручную |
| Уведомление пришло повторно | `outbox_delivery_ack_uncertain`, `outbox_lease_recovered` | Это допустимо при at-least-once delivery после неподтверждённого commit; сверяйте событие в карточке Offer |
| CSV import прервался или результат неизвестен | `csv_import_failed` либо `csv_import_interrupted outcome=uncertain`; фактический список Products | Не считайте весь файл полностью откатанным. Проверьте результат и только затем явно запустите новый import; автоматического resume нет |
| Команда или callback Telegram завершились ошибкой | `telegram_callback`, `telegram_unhandled_error`, `telegram_recovery_failed` | Проверьте показанный recovery screen и журнал, затем повторите действие явно; handler сам business action не повторяет |
| SQLite unable to open / locked | Путь, права, Docker volume, первый runtime или сторонняя долгая transaction | Восстановите доступ или остановите конкурирующий процесс; не удаляйте lock-файл вручную |
| В журнале виден действующий token | Даже sanitizer не даёт абсолютной гарантии | Прекратите распространение, отзовите token через BotFather, замените `.env`, пересоздайте service и передавайте только обезличенный фрагмент |
| Docker не видит магазин/Telegram | DNS/HTTPS egress, host policy и counters `deploy/verify-m4-2.sh` | Исправьте сетевую политику; не ослабляйте SSRF-защиту ради диагностики |

Полный список пользовательской и технической документации приведён в разделе
[«Документация»](#документация).
