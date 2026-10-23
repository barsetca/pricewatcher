"""Repository подтверждённой истории цен."""

from __future__ import annotations

import sqlite3

from pricewatch.domain.models import PriceRecord


class PriceHistoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, record: PriceRecord) -> PriceRecord:
        cursor = self._connection.execute(
            """
            INSERT INTO price_history (offer_id, price_minor, currency, checked_at)
            VALUES (?, ?, ?, ?)
            """,
            (record.offer_id, record.price_minor, record.currency, record.checked_at),
        )
        row = self._connection.execute(
            "SELECT * FROM price_history WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return PriceRecord.from_row(row)

    def latest_for_offer(self, offer_id: int) -> PriceRecord | None:
        """Возвращает цену для внутреннего мониторинга без проверки владельца.

        Метод не предназначен для прямого вызова из Telegram frontend.
        """
        row = self._connection.execute(
            """
            SELECT * FROM price_history
            WHERE offer_id = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (offer_id,),
        ).fetchone()
        return PriceRecord.from_row(row) if row else None

    def list_for_offer(self, offer_id: int, user_id: int, limit: int | None = None) -> list[PriceRecord]:
        query = """
            SELECT history.*
            FROM price_history AS history
            JOIN offers AS offer ON offer.id = history.offer_id
            JOIN products AS product ON product.id = offer.product_id
            WHERE history.offer_id = ? AND product.user_id = ?
            ORDER BY history.checked_at DESC, history.id DESC
        """
        parameters: list[int] = [offer_id, user_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(query, parameters)
        return [PriceRecord.from_row(row) for row in rows]
