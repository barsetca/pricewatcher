"""Repository для пользовательских товаров."""

from __future__ import annotations

import sqlite3

from pricewatch.domain.models import Product


class ProductRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(self, product: Product) -> Product:
        cursor = self._connection.execute(
            """
            INSERT INTO products (user_id, name, alert_threshold, is_active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (product.user_id, product.name, product.alert_threshold, int(product.is_active), product.created_at),
        )
        return self.get_for_user(cursor.lastrowid, product.user_id)

    def get_for_user(self, product_id: int, user_id: int) -> Product | None:
        row = self._connection.execute(
            "SELECT * FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
        ).fetchone()
        return Product.from_row(row) if row else None

    def get_internal(self, product_id: int) -> Product | None:
        """Внутреннее чтение для MonitoringService без frontend ownership."""
        row = self._connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return Product.from_row(row) if row else None

    def list_for_user(self, user_id: int) -> list[Product]:
        rows = self._connection.execute(
            "SELECT * FROM products WHERE user_id = ? ORDER BY id", (user_id,)
        )
        return [Product.from_row(row) for row in rows]

    def rename(self, product_id: int, user_id: int, name: str) -> bool:
        return self._connection.execute(
            "UPDATE products SET name = ? WHERE id = ? AND user_id = ?",
            (name, product_id, user_id),
        ).rowcount == 1

    def update_alert_threshold(self, product_id: int, user_id: int, threshold: float | None) -> bool:
        return self._connection.execute(
            "UPDATE products SET alert_threshold = ? WHERE id = ? AND user_id = ?",
            (threshold, product_id, user_id),
        ).rowcount == 1

    def set_active(self, product_id: int, user_id: int, is_active: bool) -> bool:
        return self._connection.execute(
            "UPDATE products SET is_active = ? WHERE id = ? AND user_id = ?",
            (int(is_active), product_id, user_id),
        ).rowcount == 1

    def delete(self, product_id: int, user_id: int) -> bool:
        return self._connection.execute(
            "DELETE FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
        ).rowcount == 1
