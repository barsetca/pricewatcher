"""Единая чистая нормализация имён Product по правилам D39/D45."""

from __future__ import annotations


def normalize_product_name(value: str) -> str:
    """Удалить крайние пробелы и сжать серии Unicode whitespace."""

    return " ".join(value.split()) if isinstance(value, str) else ""


def product_name_comparison_key(value: str) -> str:
    """Вернуть точный регистронезависимый ключ без fuzzy matching."""

    return normalize_product_name(value).casefold()
