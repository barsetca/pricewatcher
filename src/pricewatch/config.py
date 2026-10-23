"""Минимальная конфигурация production-приложения."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_PATH = Path("data/pricewatch.db")


def load_env_file(path: Path = ENV_FILE) -> None:
    """Загружает простой файл ``.env``, не перезаписывая окружение процесса."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    database_path: Path


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    allowed_user_ids: frozenset[int]
    mode: str
    log_level: str


def load_settings() -> Settings:
    """Возвращает конфигурацию, полученную из окружения или файла ``.env``."""
    load_env_file()
    configured_path = Path(os.getenv("PRICEWATCH_DATABASE_PATH", str(DEFAULT_DATABASE_PATH)))
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return Settings(database_path=configured_path)


def load_telegram_settings() -> TelegramSettings:
    """Загружает только polling-конфигурацию; bootstrap её не требует."""
    load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    mode = os.getenv("TELEGRAM_MODE", "polling").strip().lower()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required to run the bot")
    if not re.fullmatch(r"[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("TELEGRAM_BOT_TOKEN has an invalid Bot API token format")
    if not raw_ids:
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must not be empty")
    try:
        ids = frozenset(int(value.strip()) for value in raw_ids.split(",") if value.strip())
    except ValueError as error:
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs") from error
    if not ids or any(value <= 0 for value in ids):
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive numeric IDs")
    if mode != "polling":
        raise ValueError("only TELEGRAM_MODE=polling is supported")
    level = os.getenv("PRICEWATCH_LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("PRICEWATCH_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    return TelegramSettings(token, ids, mode, level)
