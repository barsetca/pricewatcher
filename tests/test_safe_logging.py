"""Regression tests for Telegram token redaction in emitted log text."""
from __future__ import annotations

import io
import logging
import re
import uuid

import pytest

import pricewatch.main as main_module
from pricewatch.safe_logging import (
    LOGGING_FORMAT_FAILED,
    REDACTED,
    SecretRedactingFormatter,
    configure_logging,
    redact_secrets,
)


TOKEN = "123456:" + "synthetic_logging_token_" * 2
URL = f"https://api.telegram.org/bot{TOKEN}/getMe"


def _logger(*, name: str | None = None):
    stream = io.StringIO()
    logger = logging.getLogger(name or f"pricewatch.test.logging.{uuid.uuid4().hex}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SecretRedactingFormatter("%(levelname)s:%(message)s", secrets=(TOKEN,)))
    logger.addHandler(handler)
    return logger, stream


def test_httpx_info_url_is_not_emitted_and_has_no_token():
    logger, stream = _logger(name="httpx")
    previous_level = logger.level
    try:
        configure_logging("INFO", TOKEN)
        logger.info("HTTP Request: GET %s", URL)
        assert stream.getvalue() == ""
    finally:
        logger.setLevel(previous_level)
        logger.handlers.clear()


def test_application_info_masks_token_inside_bot_api_url():
    logger, stream = _logger()
    logger.info("request failed for %s", URL)
    output = stream.getvalue()
    assert TOKEN not in output
    assert f"bot{REDACTED}/getMe" in output


def test_exception_and_traceback_mask_token():
    logger, stream = _logger()
    try:
        raise RuntimeError(f"telegram transport failed at {URL}")
    except RuntimeError:
        logger.exception("application error with %s", TOKEN)
    output = stream.getvalue()
    assert "Traceback" in output
    assert "telegram transport failed" in output
    assert TOKEN not in output
    assert REDACTED in output


def test_separate_token_and_authorization_header_are_masked():
    logger, stream = _logger()
    logger.error("token=%s Authorization: Bearer %s", TOKEN, TOKEN)
    output = stream.getvalue()
    assert TOKEN not in output
    assert output.count(REDACTED) >= 2


@pytest.mark.parametrize(
    "assignment",
    [
        f"TELEGRAM_BOT_TOKEN={TOKEN}",
        f"telegram_bot_token: {TOKEN}",
    ],
)
def test_telegram_token_assignment_is_masked(assignment):
    output = redact_secrets(f"configuration rejected: {assignment}")
    assert TOKEN not in output
    assert REDACTED in output


def test_safe_message_is_unchanged():
    logger, stream = _logger()
    logger.warning("scheduler slot completed: 2026-10-21T03:00:00+00:00")
    assert stream.getvalue() == "WARNING:scheduler slot completed: 2026-10-21T03:00:00+00:00\n"


def test_configure_logging_is_idempotent_and_protects_existing_child_handler():
    logger, stream = _logger()
    try:
        configure_logging("INFO", TOKEN)
        configure_logging("INFO", TOKEN)
        logger.error("retrying %s", URL)
        output = stream.getvalue()
        assert TOKEN not in output
        assert output.count("retrying") == 1
        assert len(logger.handlers) == 1
    finally:
        logger.handlers.clear()


def test_production_configuration_emits_utc_level_logger_and_message():
    logger, stream = _logger()
    try:
        configure_logging("INFO", TOKEN)
        logger.info("safe startup message")
        output = stream.getvalue()
        assert re.fullmatch(
            rf"\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z \| INFO \| "
            rf"{re.escape(logger.name)} \| safe startup message\n",
            output,
        )
    finally:
        logger.handlers.clear()


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer synthetic_auth_value",
        "Authorization=synthetic_auth_value",
        "{'Authorization': 'Bearer synthetic_auth_value'}",
        'authorization="synthetic_auth_value"',
        "AUTHORIZATION='Basic synthetic_auth_value'",
    ],
)
def test_authorization_forms_are_masked_without_removing_safe_context(message):
    output = redact_secrets(f"request rejected: {message}; retry disabled")
    assert "synthetic_auth_value" not in output
    assert f"Authorization: {REDACTED}" in output
    assert "request rejected:" in output
    assert "retry disabled" in output


def test_authorization_word_in_safe_text_is_unchanged():
    message = "authorization policy enabled"
    assert redact_secrets(message) == message


def test_url_userinfo_query_and_fragment_are_removed_from_message():
    source = (
        "redirect Location=https://synthetic-user:synthetic-pass@shop.invalid/"
        "item?session=synthetic-query#synthetic-fragment; refused"
    )
    output = redact_secrets(source)
    assert "redirect Location=" in output
    assert "https://shop.invalid/item" in output
    assert "refused" in output
    for sensitive in (
        "synthetic-user", "synthetic-pass", "synthetic-query", "synthetic-fragment"
    ):
        assert sensitive not in output


def test_exception_traceback_masks_token_and_sanitizes_url_parts():
    logger, stream = _logger()
    sensitive_url = "https://person:password@example.invalid/path?key=value#private"
    try:
        raise RuntimeError(f"redirect failed at {sensitive_url}")
    except RuntimeError:
        logger.exception("transport failed")
    output = stream.getvalue()
    assert "Traceback" in output
    assert "redirect failed" in output
    assert "https://example.invalid/path" in output
    for sensitive in ("person", "password", "key=value", "private"):
        assert sensitive not in output


@pytest.mark.parametrize("name", ["httpx", "httpcore", "urllib3", "telegram"])
def test_sensitive_library_info_and_debug_are_suppressed_but_warning_remains(name):
    logger = logging.getLogger(name)
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        configure_logging("DEBUG", TOKEN)
        logger.debug("sensitive debug")
        logger.info("sensitive info")
        logger.warning("safe warning")
        output = stream.getvalue()
        assert "sensitive debug" not in output
        assert "sensitive info" not in output
        assert "safe warning" in output
    finally:
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def test_formatter_does_not_mutate_original_record():
    record = logging.LogRecord(
        "pricewatch.test.original",
        logging.ERROR,
        __file__,
        1,
        "safe %s",
        ("argument",),
        None,
    )
    record.exc_text = "existing safe exception text"
    original = (record.msg, record.args, record.exc_info, record.exc_text)
    formatter = SecretRedactingFormatter("%(levelname)s:%(message)s", secrets=(TOKEN,))

    assert "safe argument" in formatter.format(record)
    assert (record.msg, record.args, record.exc_info, record.exc_text) == original


def test_malformed_record_returns_fixed_fail_closed_fallback_without_mutation():
    class BrokenMessage:
        def __str__(self):
            raise RuntimeError("raw synthetic formatter secret")

    message = BrokenMessage()
    record = logging.LogRecord(
        "pricewatch.test.broken", logging.ERROR, __file__, 1, message, (), None
    )
    formatter = SecretRedactingFormatter("%(levelname)s:%(message)s")

    assert formatter.format(record) == LOGGING_FORMAT_FAILED
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.handle(record)
    assert stream.getvalue() == f"{LOGGING_FORMAT_FAILED}\n"
    assert "raw synthetic formatter secret" not in stream.getvalue()
    assert record.msg is message
    assert record.args == ()


def test_unexpected_failure_before_logging_configuration_exposes_only_class(
    monkeypatch, capsys
):
    secret = "123456:" + "synthetic_startup_secret_" * 2

    def fail_before_configuration():
        raise RuntimeError(
            "startup failed with "
            f"{secret} at https://user:pass@example.invalid/path?secret=value"
        )

    monkeypatch.setattr(main_module, "load_settings", fail_before_configuration)

    assert main_module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err
    for sensitive in (secret, "user", "pass", "secret=value", "startup failed with"):
        assert sensitive not in captured.err
