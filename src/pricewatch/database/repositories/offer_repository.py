"""Repository предложений магазинов."""

from __future__ import annotations

import sqlite3
from typing import Any

from pricewatch.domain.models import Offer


class OfferRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, offer: Offer) -> Offer:
        fields = (
            "product_id", "url", "normalized_url", "domain", "store_name", "source_title",
            "current_price_minor", "currency", "preferred_fetch_method", "status",
            "consecutive_errors", "error_notified", "pending_price_minor",
            "pending_price_detected_at", "pending_check_attempt_id", "last_checked_at",
            "last_success_at", "is_active", "state_version", "created_at",
        )
        values: list[Any] = [getattr(offer, field) for field in fields]
        values[8] = values[8].value if values[8] else None
        values[9] = values[9].value
        cursor = self._connection.execute(
            f"INSERT INTO offers ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            values,
        )
        user_id = self._product_owner_id(offer.product_id)
        return self.get_for_user(cursor.lastrowid, user_id)

    def get_for_user(self, offer_id: int, user_id: int) -> Offer | None:
        row = self._connection.execute(
            """
            SELECT offer.*
            FROM offers AS offer
            JOIN products AS product ON product.id = offer.product_id
            WHERE offer.id = ? AND product.user_id = ?
            """,
            (offer_id, user_id),
        ).fetchone()
        return Offer.from_row(row) if row else None

    def get_internal(self, offer_id: int) -> Offer | None:
        """Внутреннее чтение для MonitoringService."""
        row = self._connection.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        return Offer.from_row(row) if row else None

    def list_for_product(self, product_id: int, user_id: int) -> list[Offer]:
        rows = self._connection.execute(
            """
            SELECT offer.*
            FROM offers AS offer
            JOIN products AS product ON product.id = offer.product_id
            WHERE offer.product_id = ? AND product.user_id = ?
            ORDER BY offer.id
            """,
            (product_id, user_id),
        )
        return [Offer.from_row(row) for row in rows]

    def list_for_user(self, user_id: int) -> list[Offer]:
        rows = self._connection.execute(
            """
            SELECT offer.*
            FROM offers AS offer
            JOIN products AS product ON product.id = offer.product_id
            WHERE product.user_id = ?
            ORDER BY offer.id
            """,
            (user_id,),
        )
        return [Offer.from_row(row) for row in rows]

    def list_for_monitoring(self) -> list[Offer]:
        """Возвращает активные предложения для внутреннего MonitoringService.

        Метод намеренно не принимает user_id и не предназначен для Telegram frontend.
        """
        rows = self._connection.execute(
            """
            SELECT offer.* FROM offers AS offer
            JOIN products AS product ON product.id = offer.product_id
            WHERE offer.is_active = 1 AND offer.status != 'DISABLED' AND product.is_active = 1
            ORDER BY offer.id
            """
        )
        return [Offer.from_row(row) for row in rows]

    def find_duplicate_for_user(self, normalized_url: str, user_id: int) -> Offer | None:
        """Ищет URL пользователя; ProductService применит проверку перед созданием Offer."""
        row = self._connection.execute(
            """
            SELECT offer.*
            FROM offers AS offer
            JOIN products AS product ON product.id = offer.product_id
            WHERE offer.normalized_url = ? AND product.user_id = ?
            ORDER BY offer.id
            LIMIT 1
            """,
            (normalized_url, user_id),
        ).fetchone()
        return Offer.from_row(row) if row else None

    def update_details(self, offer_id: int, user_id: int, **values: object) -> bool:
        allowed_values = {
            key: value
            for key, value in values.items()
            if key in {"url", "normalized_url", "domain", "store_name", "source_title", "currency"}
        }
        if not allowed_values:
            return False
        assignments = ", ".join(f"{field} = ?" for field in allowed_values)
        return self._connection.execute(
            f"""
            UPDATE offers SET {assignments}, state_version = state_version + 1
            WHERE id = ?
              AND product_id IN (SELECT id FROM products WHERE user_id = ?)
            """,
            (*allowed_values.values(), offer_id, user_id),
        ).rowcount == 1

    def update_check_state(self, offer_id: int, **values: object) -> bool:
        """Обновляет состояние из внутреннего MonitoringService без ownership-проверки.

        Метод не предназначен для прямого вызова из Telegram frontend.
        """
        accepted_fields = (
            "status", "current_price_minor", "currency", "preferred_fetch_method",
            "consecutive_errors", "error_notified", "last_checked_at", "last_success_at",
            "pending_price_minor", "pending_price_detected_at", "pending_check_attempt_id",
        )
        update_values = {field: values[field] for field in accepted_fields if field in values}
        if "status" in update_values:
            update_values["status"] = update_values["status"].value
        if update_values.get("preferred_fetch_method"):
            update_values["preferred_fetch_method"] = update_values["preferred_fetch_method"].value
        if "error_notified" in update_values:
            update_values["error_notified"] = int(update_values["error_notified"])
        if not update_values:
            return False
        assignments = ", ".join(f"{field} = ?" for field in update_values)
        return self._connection.execute(
            f"UPDATE offers SET {assignments}, state_version = state_version + 1 WHERE id = ?",
            (*update_values.values(), offer_id),
        ).rowcount == 1

    def update_state_if_version(self, offer_id: int, expected_version: int, **values: object) -> bool:
        accepted = {key: value for key, value in values.items() if key in {
            "status", "current_price_minor", "currency", "consecutive_errors", "error_notified",
            "last_checked_at", "last_success_at", "pending_price_minor",
            "pending_price_detected_at", "pending_check_attempt_id",
        }}
        if "status" in accepted:
            accepted["status"] = accepted["status"].value
        if "error_notified" in accepted:
            accepted["error_notified"] = int(accepted["error_notified"])
        assignments = ", ".join(f"{field} = ?" for field in accepted)
        return self._connection.execute(
            f"UPDATE offers SET {assignments}, state_version = state_version + 1 WHERE id = ? AND state_version = ?",
            (*accepted.values(), offer_id, expected_version),
        ).rowcount == 1

    def set_pending_anomaly(
        self, offer_id: int, user_id: int, price_minor: int, detected_at: str, attempt_id: int | None
    ) -> bool:
        if attempt_id is not None and not self._attempt_belongs_to_offer(attempt_id, offer_id):
            return False
        return self._connection.execute(
            """
            UPDATE offers
            SET pending_price_minor = ?, pending_price_detected_at = ?, pending_check_attempt_id = ?
            WHERE id = ?
              AND product_id IN (SELECT id FROM products WHERE user_id = ?)
            """,
            (price_minor, detected_at, attempt_id, offer_id, user_id),
        ).rowcount == 1

    def set_active(self, offer_id: int, user_id: int, is_active: bool) -> bool:
        return self._connection.execute(
            """
            UPDATE offers SET is_active = ?, state_version = state_version + 1
            WHERE id = ? AND product_id IN (SELECT id FROM products WHERE user_id = ?)
            """,
            (int(is_active), offer_id, user_id),
        ).rowcount == 1

    def delete(self, offer_id: int, user_id: int) -> bool:
        return self._connection.execute(
            """
            DELETE FROM offers
            WHERE id = ? AND product_id IN (SELECT id FROM products WHERE user_id = ?)
            """,
            (offer_id, user_id),
        ).rowcount == 1

    def _product_owner_id(self, product_id: int) -> int:
        row = self._connection.execute("SELECT user_id FROM products WHERE id = ?", (product_id,)).fetchone()
        return row[0]

    def _attempt_belongs_to_offer(self, attempt_id: int, offer_id: int) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM check_attempts WHERE id = ? AND offer_id = ?",
            (attempt_id, offer_id),
        ).fetchone() is not None
