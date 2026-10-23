"""Подключение и инициализация SQLite для PriceWatch."""

from pricewatch.database.connection import connect, initialize_database

__all__ = ["connect", "initialize_database"]
