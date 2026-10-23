"""Опциональный browser fetcher без импорта Playwright при загрузке модуля."""

from __future__ import annotations

import time
from typing import Any

from pricewatch.domain.enums import FetchMethod, FetchStatus
from pricewatch.scraping.models import FetchResult


class PlaywrightFetcher:
    def __init__(self, timeout_ms: int = 25_000) -> None:
        self._timeout_ms = timeout_ms

    def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError:
            return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.NETWORK_ERROR, elapsed_time_ms=_elapsed(started), error_message="Playwright is not installed")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                        status = response.status if response else None
                        final_url = page.url
                        if status in (403, 429):
                            return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.BLOCKED, final_url, status, elapsed_time_ms=_elapsed(started), error_message=f"HTTP {status}")
                        if status is not None and status >= 400:
                            return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.HTTP_ERROR, final_url, status, elapsed_time_ms=_elapsed(started), error_message=f"HTTP {status}")
                        html = page.content()
                        if not html.strip():
                            return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.HTTP_ERROR, final_url, status, elapsed_time_ms=_elapsed(started), error_message="empty response body")
                        return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.SUCCESS, final_url, status, html, _elapsed(started))
                    finally:
                        page.close()
                finally:
                    browser.close()
        except TimeoutError:
            return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.TIMEOUT, elapsed_time_ms=_elapsed(started), error_message="navigation timed out")
        except Error as error:
            return FetchResult(url, FetchMethod.PLAYWRIGHT, FetchStatus.NETWORK_ERROR, elapsed_time_ms=_elapsed(started), error_message=f"Playwright error: {error}")


def _elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
