"""Низкоуровневое подключение и bootstrap SQLite-схемы."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
LOG = logging.getLogger(__name__)
LEGACY_MIGRATIONS = (
    (
        "offers",
        "state_version",
        "ALTER TABLE offers ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0;",
    ),
)


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Открывает SQLite-соединение и включает контроль внешних ключей."""
    path = Path(database_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: str | Path) -> None:
    """Создаёт отсутствующие таблицы и индексы; повторный вызов безопасен."""
    connection = connect(database_path)
    try:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        migrations = _pending_legacy_migrations(connection)
        script = "\n".join(("BEGIN IMMEDIATE;", schema, *migrations, "COMMIT;"))
        connection.executescript(script)
    except Exception as error:
        if connection.in_transaction:
            connection.rollback()
        LOG.error(
            "event=database_bootstrap_failed outcome=error exception_class=%s",
            type(error).__name__,
        )
        raise
    finally:
        connection.close()


def _pending_legacy_migrations(connection: sqlite3.Connection) -> tuple[str, ...]:
    pending = []
    for table, column, statement in LEGACY_MIGRATIONS:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            pending.append(statement)
    return tuple(pending)
