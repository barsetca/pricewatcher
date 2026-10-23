"""Repository SQLite Outbox для надёжной доставки уведомлений."""

from __future__ import annotations

import sqlite3
import uuid

from pricewatch.domain.enums import NotificationOutboxStatus
from pricewatch.domain.models import NotificationOutbox


class NotificationOutboxRepository:
    """Хранит события уведомлений, не фиксируя транзакции самостоятельно.

    ``lease_token`` создаётся при каждом claim. Завершить попытку может только
    владелец актуальной аренды: подтверждение старой попытки не затронет запись,
    уже восстановленную и заново зарезервированную другим worker-ом.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def enqueue(self, event: NotificationOutbox) -> NotificationOutbox:
        """Добавляет новое PENDING-событие либо возвращает уже существующее."""
        if event.status is not NotificationOutboxStatus.PENDING or event.attempt_count != 0:
            raise ValueError("new outbox event must be PENDING with attempt_count=0")
        cursor = self._connection.execute(
            """
            INSERT INTO notification_outbox (
                event_key, event_type, offer_id, chat_id, payload_json, status,
                attempt_count, next_attempt_at, lease_until, lease_token, created_at,
                sent_at, last_error, telegram_message_id
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, NULL, NULL, ?, NULL, NULL, NULL)
            ON CONFLICT(event_key, chat_id) DO NOTHING
            """,
            (
                event.event_key, event.event_type.value, event.offer_id, event.chat_id,
                event.payload_json, event.next_attempt_at, event.created_at,
            ),
        )
        if cursor.rowcount == 1:
            return self.get_by_id(cursor.lastrowid)  # type: ignore[arg-type]
        row = self._connection.execute(
            "SELECT * FROM notification_outbox WHERE event_key = ? AND chat_id = ?",
            (event.event_key, event.chat_id),
        ).fetchone()
        return NotificationOutbox.from_row(row)

    def get_by_id(self, outbox_id: int) -> NotificationOutbox | None:
        row = self._connection.execute(
            "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        return NotificationOutbox.from_row(row) if row else None

    def get_sent_by_telegram_message(
        self, chat_id: int, telegram_message_id: int
    ) -> NotificationOutbox | None:
        """Возвращает источник уже доставленного Telegram-сообщения."""
        rows = self._connection.execute(
            """
            SELECT * FROM notification_outbox
            WHERE chat_id = ? AND telegram_message_id = ? AND status = 'SENT'
            ORDER BY id
            LIMIT 2
            """,
            (chat_id, telegram_message_id),
        ).fetchall()
        return NotificationOutbox.from_row(rows[0]) if len(rows) == 1 else None

    def list_ready(self, now: str, limit: int | None = None) -> list[NotificationOutbox]:
        query = """
            SELECT * FROM notification_outbox
            WHERE status = 'PENDING' AND next_attempt_at <= ?
            ORDER BY created_at, id
        """
        parameters: list[object] = [now]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must not be negative")
            query += " LIMIT ?"
            parameters.append(limit)
        return [NotificationOutbox.from_row(row) for row in self._connection.execute(query, parameters)]

    def claim(self, outbox_id: int, now: str, lease_until: str) -> NotificationOutbox | None:
        """Атомарно резервирует готовую запись и выдаёт новый токен аренды."""
        token = uuid.uuid4().hex
        claimed = self._connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'SENDING', lease_until = ?, lease_token = ?, attempt_count = attempt_count + 1
            WHERE id = ? AND status = 'PENDING' AND next_attempt_at <= ?
            """,
            (lease_until, token, outbox_id, now),
        ).rowcount == 1
        return self.get_by_id(outbox_id) if claimed else None

    def mark_sent(self, outbox_id: int, lease_token: str, sent_at: str, telegram_message_id: int | None = None) -> bool:
        return self._connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'SENT', sent_at = ?, telegram_message_id = ?, lease_until = NULL, lease_token = NULL
            WHERE id = ? AND status = 'SENDING' AND lease_token = ?
            """,
            (sent_at, telegram_message_id, outbox_id, lease_token),
        ).rowcount == 1

    def reschedule(self, outbox_id: int, lease_token: str, next_attempt_at: str, last_error: str) -> bool:
        return self._connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'PENDING', next_attempt_at = ?, last_error = ?, lease_until = NULL, lease_token = NULL
            WHERE id = ? AND status = 'SENDING' AND lease_token = ?
            """,
            (next_attempt_at, last_error, outbox_id, lease_token),
        ).rowcount == 1

    def mark_failed(self, outbox_id: int, lease_token: str, last_error: str) -> bool:
        return self._connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'FAILED', last_error = ?, lease_until = NULL, lease_token = NULL
            WHERE id = ? AND status = 'SENDING' AND lease_token = ?
            """,
            (last_error, outbox_id, lease_token),
        ).rowcount == 1

    def recover_expired(self, now: str) -> int:
        """Возвращает зависшие SENDING-события в очередь без потери счётчика."""
        return self._connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'PENDING', next_attempt_at = ?, lease_until = NULL, lease_token = NULL
            WHERE status = 'SENDING' AND lease_until IS NOT NULL AND lease_until <= ?
            """,
            (now, now),
        ).rowcount
