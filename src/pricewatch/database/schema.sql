CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT NULL,
    alert_threshold REAL NOT NULL DEFAULT 10.0 CHECK (alert_threshold >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    alert_threshold REAL NULL CHECK (alert_threshold IS NULL OR alert_threshold >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (user_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    domain TEXT NULL,
    store_name TEXT NULL,
    source_title TEXT NULL,
    current_price_minor INTEGER NULL CHECK (current_price_minor >= 0),
    currency TEXT NULL,
    preferred_fetch_method TEXT NULL CHECK (preferred_fetch_method IN ('REQUESTS', 'PLAYWRIGHT')),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('ACTIVE', 'PENDING', 'ERROR', 'SUSPICIOUS', 'DISABLED')),
    consecutive_errors INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_errors >= 0),
    error_notified INTEGER NOT NULL DEFAULT 0 CHECK (error_notified IN (0, 1)),
    pending_price_minor INTEGER NULL CHECK (pending_price_minor >= 0),
    pending_price_detected_at TEXT NULL,
    pending_check_attempt_id INTEGER NULL,
    last_checked_at TEXT NULL,
    last_success_at TEXT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    UNIQUE (product_id, normalized_url)
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL,
    price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
    currency TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    FOREIGN KEY (offer_id) REFERENCES offers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS check_attempts (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    fetch_method TEXT NULL CHECK (fetch_method IN ('REQUESTS', 'PLAYWRIGHT')),
    fetch_status TEXT NOT NULL CHECK (fetch_status IN ('SUCCESS', 'BLOCKED', 'ROBOTS_DENIED', 'HTTP_ERROR', 'TIMEOUT', 'NETWORK_ERROR')),
    http_status INTEGER NULL,
    extraction_status TEXT NOT NULL CHECK (extraction_status IN ('SUCCESS', 'PARTIAL', 'AMBIGUOUS', 'PARSE_ERROR', 'NOT_RUN')),
    extraction_method TEXT NULL CHECK (extraction_method IN ('JSON_LD', 'MICRODATA', 'METADATA', 'DOM_HEURISTIC', 'NONE')),
    confidence_score REAL NULL CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
    detected_price_minor INTEGER NULL CHECK (detected_price_minor >= 0),
    currency TEXT NULL,
    elapsed_time_ms INTEGER NULL CHECK (elapsed_time_ms IS NULL OR elapsed_time_ms >= 0),
    error_message TEXT NULL,
    is_suspicious INTEGER NOT NULL DEFAULT 0 CHECK (is_suspicious IN (0, 1)),
    anomaly_decision TEXT NULL CHECK (anomaly_decision IN ('CONFIRMED', 'REJECTED')),
    anomaly_decided_at TEXT NULL,
    FOREIGN KEY (offer_id) REFERENCES offers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('PRICE_DROP', 'ANOMALY_DETECTED', 'REPEATED_ERROR')),
    offer_id INTEGER NULL,
    chat_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT NULL,
    lease_token TEXT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT NULL,
    last_error TEXT NULL,
    telegram_message_id INTEGER NULL,
    FOREIGN KEY (offer_id) REFERENCES offers(id) ON DELETE SET NULL,
    UNIQUE (event_key, chat_id)
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY,
    job_name TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'INTERRUPTED')),
    error_message TEXT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    UNIQUE (job_name, scheduled_for)
);

CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id);
CREATE INDEX IF NOT EXISTS idx_offers_product_id ON offers(product_id);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_price_history_offer_checked_at ON price_history(offer_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_check_attempts_offer_checked_at ON check_attempts(offer_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_ready ON notification_outbox(status, next_attempt_at, created_at, id);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_expired_lease ON notification_outbox(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_created_at ON notification_outbox(created_at, id);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job_slot ON scheduler_runs(job_name, scheduled_for DESC);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_status ON scheduler_runs(status);
