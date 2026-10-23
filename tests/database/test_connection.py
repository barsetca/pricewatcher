import logging
import sqlite3

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.connection import LEGACY_MIGRATIONS, SCHEMA_PATH


def database_path(tmp_path):
    return tmp_path / "pricewatch-test.db"


def insert_offer_tree(connection):
    connection.execute(
        "INSERT INTO users (telegram_user_id, created_at) VALUES (?, ?)",
        (1001, "2026-10-08T00:00:00Z"),
    )
    user_id = connection.execute("SELECT id FROM users WHERE telegram_user_id = 1001").fetchone()[0]
    connection.execute(
        "INSERT INTO products (user_id, name, created_at) VALUES (?, ?, ?)",
        (user_id, "Тестовый товар", "2026-10-08T00:00:00Z"),
    )
    product_id = connection.execute("SELECT id FROM products").fetchone()[0]
    connection.execute(
        """INSERT INTO offers (product_id, url, normalized_url, created_at)
           VALUES (?, ?, ?, ?)""",
        (product_id, "https://example.test/item", "https://example.test/item", "2026-10-08T00:00:00Z"),
    )
    offer_id = connection.execute("SELECT id FROM offers").fetchone()[0]
    return product_id, offer_id


def test_initialize_creates_database_file(tmp_path):
    path = database_path(tmp_path)

    initialize_database(path)

    assert path.is_file()


def test_initialize_creates_required_tables(tmp_path):
    path = database_path(tmp_path)
    initialize_database(path)

    connection = connect(path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()

    assert {
        "users", "products", "offers", "price_history", "check_attempts",
        "notification_outbox", "scheduler_runs",
    } <= tables


def test_new_database_partial_ddl_failure_rolls_back_and_retry_succeeds(
    tmp_path, monkeypatch, caplog,
):
    path = tmp_path / "private-bootstrap-path.db"
    broken_schema = tmp_path / "broken-schema.sql"
    broken_schema.write_text(
        "CREATE TABLE partial_table (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE broken_table (token=raw-sql-secret;\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.INFO, logger="pricewatch.database.connection")
    monkeypatch.setattr(
        "pricewatch.database.connection.SCHEMA_PATH", broken_schema
    )

    with pytest.raises(sqlite3.Error):
        initialize_database(path)

    connection = connect(path)
    try:
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    finally:
        connection.close()
    assert objects == []
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "event=database_bootstrap_failed outcome=error "
        "exception_class=OperationalError"
    ]
    assert "private-bootstrap-path" not in messages[0]
    assert "raw-sql-secret" not in messages[0]

    monkeypatch.setattr("pricewatch.database.connection.SCHEMA_PATH", SCHEMA_PATH)
    initialize_database(path)
    connection = connect(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='offers'"
        ).fetchone()
    finally:
        connection.close()


def test_index_creation_failure_rolls_back_all_new_schema(tmp_path, monkeypatch):
    path = tmp_path / "index-failure.db"
    broken_schema = tmp_path / "broken-index.sql"
    broken_schema.write_text(
        SCHEMA_PATH.read_text(encoding="utf-8")
        + "\nCREATE INDEX fail_index ON missing_table(raw_secret);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pricewatch.database.connection.SCHEMA_PATH", broken_schema
    )

    with pytest.raises(sqlite3.Error):
        initialize_database(path)

    connection = connect(path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall() == []
    finally:
        connection.close()


def test_connection_enables_foreign_keys(tmp_path):
    connection = connect(database_path(tmp_path))
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_foreign_key_constraint_is_enforced(tmp_path):
    path = database_path(tmp_path)
    initialize_database(path)
    connection = connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO products (user_id, name, created_at) VALUES (?, ?, ?)",
                (99999, "Без владельца", "2026-10-08T00:00:00Z"),
            )
    finally:
        connection.close()


def test_product_deletion_cascades_to_offer_history_and_attempts(tmp_path):
    path = database_path(tmp_path)
    initialize_database(path)
    connection = connect(path)
    try:
        product_id, offer_id = insert_offer_tree(connection)
        connection.execute(
            "INSERT INTO price_history (offer_id, price_minor, currency, checked_at) VALUES (?, ?, ?, ?)",
            (offer_id, 10000, "EUR", "2026-10-08T00:00:00Z"),
        )
        connection.execute(
            """INSERT INTO check_attempts
               (offer_id, checked_at, fetch_status, extraction_status)
               VALUES (?, ?, ?, ?)""",
            (offer_id, "2026-10-08T00:00:00Z", "SUCCESS", "SUCCESS"),
        )
        connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
        connection.commit()

        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    finally:
        connection.close()


def test_pending_check_attempt_id_has_no_physical_foreign_key(tmp_path):
    path = database_path(tmp_path)
    initialize_database(path)
    connection = connect(path)
    try:
        _, offer_id = insert_offer_tree(connection)
        connection.execute("UPDATE offers SET pending_check_attempt_id = ? WHERE id = ?", (99999, offer_id))
        connection.commit()

        foreign_key_columns = {row[3] for row in connection.execute("PRAGMA foreign_key_list(offers)")}
        assert "pending_check_attempt_id" not in foreign_key_columns
    finally:
        connection.close()


def test_initialize_is_idempotent_and_preserves_data(tmp_path):
    path = database_path(tmp_path)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    outbox_start = schema.index("CREATE TABLE IF NOT EXISTS notification_outbox")
    outbox_end = schema.index("CREATE INDEX IF NOT EXISTS idx_products_user_id", outbox_start)
    legacy_schema = schema[:outbox_start] + schema[outbox_end:]
    legacy_schema = "\n".join(
        line for line in legacy_schema.splitlines()
        if "idx_notification_outbox_" not in line and "idx_scheduler_runs_" not in line
    )
    connection = connect(path)
    try:
        connection.executescript(legacy_schema)
        product_id, offer_id = insert_offer_tree(connection)
        connection.execute(
            "INSERT INTO price_history (offer_id, price_minor, currency, checked_at) VALUES (?, ?, ?, ?)",
            (offer_id, 100, "EUR", "2026-10-08T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO check_attempts (offer_id, checked_at, fetch_status, extraction_status) VALUES (?, ?, ?, ?)",
            (offer_id, "2026-10-08T00:00:00Z", "SUCCESS", "SUCCESS"),
        )
        connection.commit()
    finally:
        connection.close()

    initialize_database(path)  # добавляет Outbox к уже существующей M8-схеме
    connection = connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'notification_outbox'").fetchone()
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'scheduler_runs'").fetchone()
    finally:
        connection.close()


def test_legacy_alter_failure_is_atomic_and_successful_retry_preserves_data(
    tmp_path, monkeypatch,
):
    path = tmp_path / "legacy-alter.db"
    legacy_schema = SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),\n",
        "",
    )
    connection = connect(path)
    try:
        connection.executescript(legacy_schema)
        product_id, offer_id = insert_offer_tree(connection)
        connection.commit()
    finally:
        connection.close()

    failing_migrations = (
        (
            "offers",
            "state_version",
            "ALTER TABLE offers ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0;"
            "ALTER TABLE offers ADD COLUMN state_version INTEGER;",
        ),
    )
    monkeypatch.setattr(
        "pricewatch.database.connection.LEGACY_MIGRATIONS", failing_migrations
    )

    with pytest.raises(sqlite3.Error):
        initialize_database(path)

    connection = connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(offers)")}
        assert "state_version" not in columns
        stored = connection.execute(
            "SELECT product_id, url FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()
        assert tuple(stored) == (product_id, "https://example.test/item")
    finally:
        connection.close()

    monkeypatch.setattr(
        "pricewatch.database.connection.LEGACY_MIGRATIONS", LEGACY_MIGRATIONS
    )
    initialize_database(path)
    initialize_database(path)
    connection = connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(offers)")}
        assert "state_version" in columns
        row = connection.execute(
            "SELECT product_id, url, state_version FROM offers WHERE id = ?",
            (offer_id,),
        ).fetchone()
        assert tuple(row) == (product_id, "https://example.test/item", 0)
    finally:
        connection.close()
