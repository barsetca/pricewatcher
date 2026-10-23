# PriceWatch - модель данных SQLite

## Общие правила
- Использовать стандартный `sqlite3`.
- При каждом соединении включать `PRAGMA foreign_keys = ON`.
- Время хранить в UTC в ISO 8601.
- Денежные значения в SQLite хранить как `INTEGER` в минимальных единицах валюты (`*_price_minor`). Например 829.00 EUR -> 82900 центов, 154.99 EUR -> 15499 центов.
- В Python для денежных вычислений использовать `Decimal`, не `float`.
- История `price_history` является источником предыдущей подтвержденной цены для обязательного сравнения по исходному заданию.

## Связи

```text
users
  1
  |
  N
products
  1
  |
  N
offers
  1                 1
  |                 |
  N                 N
price_history   check_attempts

offers
  |
  N
notification_outbox

scheduler_runs
(independent UTC job slots)
```

Владелец Offer определяется через `offers.product_id -> products.user_id`. Дублировать `user_id` в `offers` не требуется.

## users
Поля:
- `id INTEGER PRIMARY KEY`
- `telegram_user_id INTEGER NOT NULL UNIQUE`
- `username TEXT NULL`
- `alert_threshold REAL NOT NULL DEFAULT 10.0`
- `created_at TEXT NOT NULL`

`users.id` - внутренний ID PriceWatch. `telegram_user_id` - внешний идентификатор Telegram.

## products
Поля:
- `id INTEGER PRIMARY KEY`
- `user_id INTEGER NOT NULL`
- `name TEXT NOT NULL`
- `alert_threshold REAL NULL`
- `is_active INTEGER NOT NULL DEFAULT 1`
- `created_at TEXT NOT NULL`

FK:
- `user_id -> users(id) ON DELETE CASCADE`

Рекомендуемая уникальность:
- `UNIQUE(user_id, name COLLATE NOCASE)`.

`alert_threshold = NULL` означает использовать пользовательский default. Для строгого выполнения задания default = 10%, а уведомление срабатывает только при снижении >10%, не при ровно 10%.

## offers
Поля:
- `id INTEGER PRIMARY KEY`
- `product_id INTEGER NOT NULL`
- `url TEXT NOT NULL`
- `normalized_url TEXT NOT NULL`
- `domain TEXT NULL`
- `store_name TEXT NULL`
- `source_title TEXT NULL`
- `current_price_minor INTEGER NULL`
- `currency TEXT NULL`
- `preferred_fetch_method TEXT NULL`
- `status TEXT NOT NULL DEFAULT 'PENDING'`
- `consecutive_errors INTEGER NOT NULL DEFAULT 0`
- `error_notified INTEGER NOT NULL DEFAULT 0`
- `pending_price_minor INTEGER NULL`
- `pending_price_detected_at TEXT NULL`
- `pending_check_attempt_id INTEGER NULL`
- `last_checked_at TEXT NULL`
- `last_success_at TEXT NULL`
- `is_active INTEGER NOT NULL DEFAULT 1`
- `created_at TEXT NOT NULL`

FK:
- `product_id -> products(id) ON DELETE CASCADE`
- `pending_check_attempt_id -> check_attempts(id)` нужно реализовывать осторожно из-за циклической ссылки; допустимо добавить после создания обеих таблиц либо не задавать физический FK и обеспечивать связь service/repository-логикой. Конкретное решение зафиксировать на M1 после проверки SQLite schema design.

Уникальность:
- `UNIQUE(product_id, normalized_url)`.

Дополнительно `ProductService` должен предотвращать добавление одного нормализованного URL в разные Products одного пользователя.

### Почему нет `previous_price_minor`
Предыдущая подтвержденная цена уже хранится в `price_history`. Перед принятием новой цены `MonitoringService` получает последнюю предыдущую запись истории и сравнивает ее с новой ценой. Дублировать `previous_price` отдельным полем не требуется.

### Почему нет `reference_price_minor` в обязательном MVP
Исходное задание требует сравнения именно с предыдущей ценой. Ранее обсуждавшаяся накопительная reference-price логика является возможным будущим улучшением, но не должна заменять обязательное правило MVP.

## price_history
Поля:
- `id INTEGER PRIMARY KEY`
- `offer_id INTEGER NOT NULL`
- `price_minor INTEGER NOT NULL`
- `currency TEXT NOT NULL`
- `checked_at TEXT NOT NULL`

FK:
- `offer_id -> offers(id) ON DELETE CASCADE`.

Записывать каждое принятое успешное наблюдение, даже если цена не изменилась. Это дает реальную ежедневную историю и однозначную предыдущую цену.

Не записывать blocked/timeout/parse failure и неподтвержденную suspicious price.

Если suspicious price позже подтверждена пользователем, записать ее с временем фактического обнаружения (`pending_price_detected_at`).

## check_attempts
Поля:
- `id INTEGER PRIMARY KEY`
- `offer_id INTEGER NOT NULL`
- `checked_at TEXT NOT NULL`
- `fetch_method TEXT NULL`
- `fetch_status TEXT NOT NULL`
- `http_status INTEGER NULL`
- `extraction_status TEXT NOT NULL`
- `extraction_method TEXT NULL`
- `confidence_score REAL NULL`
- `detected_price_minor INTEGER NULL`
- `currency TEXT NULL`
- `elapsed_time_ms INTEGER NULL`
- `error_message TEXT NULL`
- `is_suspicious INTEGER NOT NULL DEFAULT 0`
- `anomaly_decision TEXT NULL`
- `anomaly_decided_at TEXT NULL`

FK:
- `offer_id -> offers(id) ON DELETE CASCADE`.

Таблица является диагностическим журналом всех попыток проверки. Suspicious detected price сохраняется здесь до подтверждения пользователем.

## notification_outbox
Инфраструктурная очередь M9.1 для надёжной последующей доставки Telegram-уведомлений. Она не выполняет отправку и не меняет правила принятия monitoring events.

Поля:
- `id INTEGER PRIMARY KEY`;
- `event_key TEXT NOT NULL` и `chat_id INTEGER NOT NULL`, с `UNIQUE(event_key, chat_id)` для идемпотентного создания;
- `event_type TEXT NOT NULL`: `PRICE_DROP`, `ANOMALY_DETECTED` или `REPEATED_ERROR`;
- `offer_id INTEGER NULL`, FK на `offers(id) ON DELETE SET NULL`;
- `payload_json TEXT NOT NULL` — самодостаточный снимок, пригодный после удаления Offer;
- `status TEXT NOT NULL`: `PENDING`, `SENDING`, `SENT`, `FAILED`;
- `attempt_count INTEGER NOT NULL DEFAULT 0`, неотрицательный;
- `next_attempt_at`, `lease_until`, `created_at`, `sent_at` — UTC ISO 8601 `TEXT`;
- `last_error TEXT NULL`, `telegram_message_id INTEGER NULL`;
- `lease_token TEXT NULL` — внутренний одноразовый токен актуальной аренды.

`claim` атомарно переводит готовую запись `PENDING` в `SENDING`, увеличивает счётчик и выдаёт новый `lease_token`. `mark_sent`, `reschedule` и `mark_failed` требуют этот токен, поэтому запоздалый worker не может подтвердить уже восстановленную и заново взятую запись. Истёкшая аренда возвращается в `PENDING` без сброса `attempt_count`.

С M9.2 `MonitoringService` создаёт Outbox в той же `BEGIN IMMEDIATE` transaction,
что и `CheckAttempt`, `PriceHistory` и изменение Offer. Ключи привязаны к
неизменяемой записи-источнику:

- `PRICE_DROP:price_history:<id>`;
- `ANOMALY_DETECTED:check_attempt:<id>`;
- `REPEATED_ERROR:check_attempt:<id>`.

Payload format version 1 содержит тип и UTC-время события, Product/Offer ID,
имя Product, обозначение источника, URL, Telegram chat ID, nullable price/
currency/error fields и `pending_check_attempt_id`. `drop_percent` хранится
JSON-строкой без преобразования `Decimal` во `float`. Snapshot не зависит от
дальнейшего существования Offer.

С M9.4 доставленное anomaly-сообщение является также источником авторизации
действия: repository ищет ровно одну `SENT` запись по сохранённым `chat_id` и
`telegram_message_id`. Запись без message ID, запись не в `SENT` или
неоднозначное совпадение callback не авторизует. Дополнительная проверка payload
и текущего Offer выполняется в `MonitoringService`; схема не изменялась.

Денежный formatter следует уже существующему договору persistence/services:
minor unit равен 1/100 валютной единицы для всех принимаемых проектом
трёхбуквенных кодов. Отдельного справочника ISO 4217 exponent пока нет; M9.4 не
вводит новые валютные правила. EUR/USD/GBP показываются символами, остальные
коды — самим ISO-кодом.

В M9.3 worker обрабатывает не более 20 ready-записей за цикл. Claim получает
lease на 300 секунд и фиксируется до Telegram request; acknowledgement выполняется
в новой transaction. После временной ошибки `next_attempt_at` рассчитывается от
UTC-времени её завершения: attempt 1 — +60 секунд, attempt 2 — +300, attempt 3 —
+900, attempt 4+ — +3600. Более длинный `RetryAfter` увеличивает интервал.
Постоянная ошибка переводит запись в `FAILED`; временная и неизвестная — обратно
в `PENDING`. `SENT`/`FAILED` больше не попадают в ready query.

Истёкшая аренда восстанавливается немедленно без сброса `attempt_count`.
`lease_token` защищает новую попытку от устаревшего ack. Семантика доставки —
at-least-once, поскольку Telegram API и SQLite не образуют общую transaction.

## scheduler_runs

Постоянный журнал M9.5 для ежедневного задания `daily_monitoring`:

- `id INTEGER PRIMARY KEY`;
- `job_name TEXT NOT NULL`;
- `scheduled_for TEXT NOT NULL` — плановый UTC-слот 03:00;
- `started_at TEXT NOT NULL`, `finished_at TEXT NULL` — фактические UTC-времена;
- `status TEXT NOT NULL`: `RUNNING`, `SUCCESS`, `FAILED`, `INTERRUPTED`;
- `error_message TEXT NULL`;
- `attempt_count INTEGER NOT NULL DEFAULT 1`, строго положительный;
- `UNIQUE(job_name, scheduled_for)`.

Резервирование выполняется внутри `BEGIN IMMEDIATE`. Новая строка создаётся как
`RUNNING`; успешность фиксируется только после полного `check_all()`. `SUCCESS` и
`FAILED` терминальны и автоматически не повторяются. При startup оставшийся
`RUNNING` становится `INTERRUPTED`; только первая прерванная попытка может быть
зарезервирована повторно с `attempt_count = 2`. Повторно прерванный слот остаётся
`INTERRUPTED`, исключая бесконечный restart loop.

При нескольких пропущенных днях записывается только последний наступивший слот,
а не ретроспективная строка на каждый день. `scheduled_for` и `started_at`
позволяют увидеть задержку. Журнал обеспечивает atomic slot claim, но не
exactly-once HTTP execution: авария после запросов и до terminal update оставляет
неопределённость, которую ограничивает единственная recovery-попытка.

## Индексы
Минимально рекомендуются:
- `products(user_id)`;
- `offers(product_id)`;
- `offers(status)`;
- `price_history(offer_id, checked_at)`;
- `check_attempts(offer_id, checked_at)`.
- `notification_outbox(status, next_attempt_at, created_at, id)` для готовых событий;
- `notification_outbox(status, lease_until)` для истёкших аренд;
- `notification_outbox(created_at, id)` для хронологического порядка.
- `scheduler_runs(job_name, scheduled_for DESC)` для последнего слота;
- `scheduler_runs(status)` для диагностики незавершённых запусков.

Добавлять новые индексы только под реальные запросы.

## Удаление
Физическое удаление Product каскадно удаляет Offers, PriceHistory и CheckAttempts.

При удалении Offer ссылки Outbox не удаляются: `offer_id` становится `NULL`, а snapshot в `payload_json` сохраняется.

В MVP кнопка «Удалить товар» может выполнять физическое удаление после подтверждения.

`is_active` сохраняется для фильтрации и возможной будущей Pause/Resume функции, но отдельная кнопка Pause в MVP не обязательна.
