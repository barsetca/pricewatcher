"""Repository диагностических попыток проверки."""

from __future__ import annotations

import sqlite3
from typing import Any

from pricewatch.domain.enums import AnomalyDecision
from pricewatch.domain.models import CheckAttempt


class CheckAttemptRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, attempt: CheckAttempt) -> CheckAttempt:
        """Сохраняет внутреннюю запись мониторинга без проверки владельца Offer.

        Метод предназначен для MonitoringService, а не для Telegram frontend.
        """
        fields = (
            "offer_id", "checked_at", "fetch_method", "fetch_status", "http_status",
            "extraction_status", "extraction_method", "confidence_score", "detected_price_minor",
            "currency", "elapsed_time_ms", "error_message", "is_suspicious",
            "anomaly_decision", "anomaly_decided_at",
        )
        values: list[Any] = [getattr(attempt, field) for field in fields]
        for index in (2, 3, 5, 6, 13):
            values[index] = values[index].value if values[index] else None
        values[12] = int(values[12])
        cursor = self._connection.execute(
            f"INSERT INTO check_attempts ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            values,
        )
        return self.get_internal(cursor.lastrowid)

    def get_internal(self, attempt_id: int) -> CheckAttempt | None:
        """Возвращает attempt для внутренней оркестрации без проверки владельца.

        Не предназначен для прямого вызова из Telegram frontend.
        """
        row = self._connection.execute(
            "SELECT * FROM check_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        return CheckAttempt.from_row(row) if row else None

    def get_for_user(self, attempt_id: int, user_id: int) -> CheckAttempt | None:
        row = self._connection.execute(
            """
            SELECT attempt.*
            FROM check_attempts AS attempt
            JOIN offers AS offer ON offer.id = attempt.offer_id
            JOIN products AS product ON product.id = offer.product_id
            WHERE attempt.id = ? AND product.user_id = ?
            """,
            (attempt_id, user_id),
        ).fetchone()
        return CheckAttempt.from_row(row) if row else None

    def list_for_offer(self, offer_id: int, user_id: int, limit: int | None = None) -> list[CheckAttempt]:
        query = """
            SELECT attempt.*
            FROM check_attempts AS attempt
            JOIN offers AS offer ON offer.id = attempt.offer_id
            JOIN products AS product ON product.id = offer.product_id
            WHERE attempt.offer_id = ? AND product.user_id = ?
            ORDER BY attempt.checked_at DESC, attempt.id DESC
        """
        parameters: list[int] = [offer_id, user_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(query, parameters)
        return [CheckAttempt.from_row(row) for row in rows]

    def set_anomaly_decision(
        self, attempt_id: int, user_id: int, decision: AnomalyDecision, decided_at: str
    ) -> bool:
        return self._connection.execute(
            """
            UPDATE check_attempts
            SET anomaly_decision = ?, anomaly_decided_at = ?
            WHERE id = ?
              AND offer_id IN (
                  SELECT offer.id
                  FROM offers AS offer
                  JOIN products AS product ON product.id = offer.product_id
                  WHERE product.user_id = ?
              )
            """,
            (decision.value, decided_at, attempt_id, user_id),
        ).rowcount == 1
