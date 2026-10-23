"""Compare requests and ordinary headless Chromium using one UniversalExtractor."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from parser_research import FetchResult, TIMEOUT, USER_AGENT, UniversalExtractor, robots_allowed

PAGE_TIMEOUT_MS = 25_000
BLOCK_MARKERS = ("captcha", "access denied", "access forbidden", "security challenge",
                 "verify you are human", "unusual traffic", "bot detection")


class RequestsFetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9,en;q=0.7"})

    def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        try:
            response = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
            elapsed = round((time.monotonic() - started) * 1000)
            if response.status_code in (403, 429):
                return FetchResult(url, response.url, response.status_code, fetch_method="REQUESTS",
                                   fetch_status="BLOCKED", error=f"HTTP {response.status_code}", elapsed_time_ms=elapsed)
            if response.status_code >= 400:
                return FetchResult(url, response.url, response.status_code, fetch_method="REQUESTS",
                                   fetch_status="HTTP_ERROR", error=f"HTTP {response.status_code}", elapsed_time_ms=elapsed)
            return FetchResult(url, response.url, response.status_code, response.content, "REQUESTS", "SUCCESS",
                               elapsed_time_ms=elapsed)
        except requests.Timeout:
            return FetchResult(url, fetch_method="REQUESTS", fetch_status="HTTP_ERROR",
                               error=f"timeout after {TIMEOUT}s", elapsed_time_ms=round((time.monotonic()-started)*1000))
        except requests.RequestException as exc:
            return FetchResult(url, fetch_method="REQUESTS", fetch_status="HTTP_ERROR",
                               error=f"{type(exc).__name__}: {exc}", elapsed_time_ms=round((time.monotonic()-started)*1000))


class PlaywrightFetcher:
    def __init__(self, browser) -> None:
        self.browser = browser

    def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        page = self.browser.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            # A brief render window is sufficient to observe ordinary client-side content.
            page.wait_for_timeout(1500)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeout:
                pass  # background analytics must not turn a useful page into a failure
            html = page.content()
            status = response.status if response else None
            elapsed = round((time.monotonic() - started) * 1000)
            visible_text = page.locator("body").inner_text(timeout=3000).lower()[:12000]
            if (status in (403, 429)) or any(marker in visible_text for marker in BLOCK_MARKERS):
                return FetchResult(url, page.url, status, fetch_method="PLAYWRIGHT", fetch_status="BLOCKED",
                                   error="blocking page detected" if status not in (403, 429) else f"HTTP {status}", elapsed_time_ms=elapsed)
            if status is not None and status >= 400:
                return FetchResult(url, page.url, status, fetch_method="PLAYWRIGHT", fetch_status="HTTP_ERROR",
                                   error=f"HTTP {status}", elapsed_time_ms=elapsed)
            return FetchResult(url, page.url, status, html, "PLAYWRIGHT", "SUCCESS", elapsed_time_ms=elapsed)
        except PlaywrightTimeout:
            return FetchResult(url, page.url, fetch_method="PLAYWRIGHT", fetch_status="HTTP_ERROR",
                               error=f"navigation timeout after {PAGE_TIMEOUT_MS}ms", elapsed_time_ms=round((time.monotonic()-started)*1000))
        except PlaywrightError as exc:
            return FetchResult(url, page.url, fetch_method="PLAYWRIGHT", fetch_status="HTTP_ERROR",
                               error=f"{type(exc).__name__}: {exc}", elapsed_time_ms=round((time.monotonic()-started)*1000))
        finally:
            page.close()


def combine(fetch: FetchResult, parsed) -> dict:
    row = {**fetch.export(), "domain": urlparse(fetch.final_url or fetch.requested_url).netloc,
           "parse_status": parsed.status, "title": parsed.title,
           "price": str(parsed.price) if parsed.price is not None else None,
           "old_price": str(parsed.old_price) if parsed.old_price is not None else None,
           "currency": parsed.currency, "availability": parsed.availability, "seller": parsed.seller,
           "sku": parsed.sku, "extraction_method": parsed.extraction_method,
           "confidence": parsed.confidence, "confidence_score": parsed.confidence_score,
           "parse_error": parsed.error, "diagnostics": parsed.diagnostics}
    return row


def robot_denied(url: str, note: str | None) -> FetchResult:
    return FetchResult(url, fetch_method="ROBOTS", fetch_status="ROBOTS_DENIED", error=note or "robots.txt disallows URL")


def save(rows: list[dict], directory: Path) -> None:
    (directory / "experiment2_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = [key for key in rows[0] if key != "diagnostics"] if rows else []
    with (directory / "experiment2_results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in columns} for row in rows)


def main() -> int:
    directory = Path(__file__).resolve().parent
    urls = [x.strip() for x in (directory / "experiment2_urls.txt").read_text(encoding="utf-8").splitlines() if x.strip()]
    request_fetcher = RequestsFetcher()
    extractor = UniversalExtractor()
    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser_fetcher = PlaywrightFetcher(browser)
        for url in urls:
            allowed, note = robots_allowed(request_fetcher.session, url)
            fetchers = (request_fetcher, browser_fetcher)
            for fetcher in fetchers:
                fetched = fetcher.fetch(url) if allowed else robot_denied(url, note)
                parsed = extractor.extract(fetched)
                parsed.diagnostics["robots_note"] = note
                rows.append(combine(fetched, parsed))
                print(f"{urlparse(url).netloc:24} {fetched.fetch_method:10} fetch={fetched.fetch_status:12} "
                      f"parse={parsed.status:10} price={parsed.price or '—'} method={parsed.extraction_method or '—'}")
        browser.close()
    save(rows, directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
