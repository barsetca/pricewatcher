"""Repository для пользователей PriceWatch."""

from __future__ import annotations

import sqlite3

from pricewatch.domain.models import User


class UserRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, user: User) -> User:
        cursor = self._connection.execute(
            """
            INSERT INTO users (telegram_user_id, username, alert_threshold, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user.telegram_user_id, user.username, user.alert_threshold, user.created_at),
        )
        return self.get_by_id(cursor.lastrowid)

    def get_by_id(self, user_id: int) -> User | None:
        row = self._connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        row = self._connection.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
        return User.from_row(row) if row else None

    def update_username(self, user_id: int, username: str | None) -> bool:
        return self._connection.execute(
            "UPDATE users SET username = ? WHERE id = ?", (username, user_id)
        ).rowcount == 1

    def update_alert_threshold(self, user_id: int, threshold: float) -> bool:
        return self._connection.execute(
            "UPDATE users SET alert_threshold = ? WHERE id = ?", (threshold, user_id)
        ).rowcount == 1
