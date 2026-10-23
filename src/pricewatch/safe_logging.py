"""Настройка logging без раскрытия Telegram token в console/container logs."""
from __future__ import annotations

import copy
import logging
import re
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


REDACTED = "<redacted>"
LOGGING_FORMAT_FAILED = "ERROR logging_format_failed"
PRODUCTION_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
PRODUCTION_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_BOT_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
_TOKEN_ASSIGNMENT = re.compile(
    r"(?i)(telegram_bot_token\s*[=:]\s*)([^\s,;]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])[\"']?authorization[\"']?\s*[:=]\s*"
    r"(?:[\"']?bearer\s+)?(?:[\"'][^\"']*[\"']|[^\s,;}\]]+)"
)
_URL = re.compile(r"(?i)https?://[^\s<>\"']+")
_TRAILING_URL_PUNCTUATION = ".,;!?)]}"
_SENSITIVE_LIBRARY_LOGGERS = ("httpx", "httpcore", "urllib3", "telegram")


def redact_secrets(value: object, secrets: Iterable[str] = ()) -> str:
    """Возвращает безопасный текст без credentials и чувствительных URL частей."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _TOKEN_ASSIGNMENT.sub(rf"\1{REDACTED}", text)
    text = _AUTHORIZATION.sub(f"Authorization: {REDACTED}", text)
    text = _BOT_TOKEN.sub(REDACTED, text)
    return _URL.sub(_sanitize_url_match, text)


def _sanitize_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in _TRAILING_URL_PUNCTUATION:
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        if not hostname:
            return f"<redacted-url>{suffix}"
        safe_host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            safe_host = f"{safe_host}:{parts.port}"
        sanitized = urlunsplit((parts.scheme, safe_host, parts.path, "", ""))
    except (TypeError, ValueError):
        sanitized = "<redacted-url>"
    return sanitized + suffix


class SecretRedactingFormatter(logging.Formatter):
    """Redacts rendered records without changing the record seen by other code."""

    def __init__(self, *args, secrets: Iterable[str] = (), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = tuple(secret for secret in secrets if secret)

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        try:
            # `Formatter.format()` records message/exc_text on LogRecord. Work on
            # a shallow copy so other handlers never observe rendered/redacted state.
            safe_record = copy.copy(record)
            safe_record.msg = redact_secrets(record.getMessage(), self._secrets)
            safe_record.args = ()
            if record.exc_info:
                safe_record.exc_info = None
                safe_record.exc_text = redact_secrets(
                    self.formatException(record.exc_info), self._secrets
                )
            elif record.exc_text:
                safe_record.exc_text = redact_secrets(record.exc_text, self._secrets)
            rendered = super().format(safe_record)
            return redact_secrets(rendered, self._secrets)
        except Exception:
            # Never let logging.Handler.handleError() print the raw record to stderr.
            return LOGGING_FORMAT_FAILED


def configure_logging(level: str, telegram_token: str) -> None:
    """Installs redacting formatters and silences HTTP request-line INFO logs.

    Existing handlers of root and already-created child loggers are formatted
    directly, rather than depending on a root filter and propagation. Repeated
    calls replace formatter state but never add duplicate handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level)
    root.setLevel(level)
    formatter = SecretRedactingFormatter(
        PRODUCTION_LOG_FORMAT,
        datefmt=PRODUCTION_DATE_FORMAT,
        secrets=(telegram_token,),
    )
    for logger in _all_loggers():
        for handler in logger.handlers:
            handler.setFormatter(formatter)
    # Transport DEBUG/INFO may contain URLs, request metadata or Telegram update
    # details. Keep warnings/errors available without weakening a stricter level.
    for name in _SENSITIVE_LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def _all_loggers() -> tuple[logging.Logger, ...]:
    loggers: list[logging.Logger] = [logging.getLogger()]
    for item in logging.Logger.manager.loggerDict.values():
        if isinstance(item, logging.Logger):
            loggers.append(item)
    return tuple(loggers)
