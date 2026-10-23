"""Основной HTTP fetcher на requests."""

from __future__ import annotations

import time
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4.dammit import EncodingDetector

from pricewatch.domain.enums import FetchMethod, FetchStatus
from pricewatch.scraping.models import FetchResult
from pricewatch.scraping.robots_policy import USER_AGENT
from pricewatch.scraping.url_security import UrlSecurity


class RequestsFetcher:
    def __init__(self, session: requests.Session | None = None, timeout: tuple[float, float] = (5.0, 20.0), url_security: UrlSecurity | None = None, robots_policy=None, max_redirects: int = 5, max_html_bytes: int = 2_000_000) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._security = url_security or UrlSecurity()
        self._robots = robots_policy
        self._max_redirects = max_redirects
        self._max_html_bytes = max_html_bytes
        self._session.headers["User-Agent"] = USER_AGENT
        self._session.headers["Accept"] = "text/html,application/xhtml+xml"
        self._session.trust_env = False

    def set_robots_policy(self, robots_policy) -> None:
        self._robots = robots_policy

    def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        current_url = url
        visited: set[str] = set()
        try:
            for _ in range(self._max_redirects + 1):
                error = self._security.validate(current_url)
                if error:
                    return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.NETWORK_ERROR, current_url, elapsed_time_ms=_elapsed_ms(started), error_message=error)
                if self._robots is not None:
                    decision = self._robots.check(current_url)
                    if decision.allowed is not True:
                        status = FetchStatus.ROBOTS_DENIED if decision.allowed is False else FetchStatus.NETWORK_ERROR
                        return FetchResult(url, FetchMethod.REQUESTS, status, current_url, elapsed_time_ms=_elapsed_ms(started), error_message=decision.reason)
                # Scraping публичных страниц не использует пользовательскую сессию.
                # Явный пустой Cookie не даёт requests добавить cookie jar session,
                # а None удаляет session-level Authorization при merge headers.
                headers = {
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Authorization": None,
                    "Cookie": "",
                }
                response = self._session.get(
                    current_url,
                    timeout=self._timeout,
                    allow_redirects=False,
                    stream=True,
                    headers=headers,
                    auth=(),
                )
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location") if hasattr(response, "headers") else None
                        if not location:
                            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.HTTP_ERROR, current_url, response.status_code, elapsed_time_ms=_elapsed_ms(started), error_message="redirect without Location")
                        target = urljoin(current_url, location)
                        if target in visited or target == current_url:
                            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.NETWORK_ERROR, target, response.status_code, elapsed_time_ms=_elapsed_ms(started), error_message="redirect loop")
                        visited.add(current_url)
                        if self._security.validate(target):
                            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.NETWORK_ERROR, target, response.status_code, elapsed_time_ms=_elapsed_ms(started), error_message="unsafe redirect target")
                        if self._robots is not None and self._robots.check(target).allowed is not True:
                            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.ROBOTS_DENIED, target, response.status_code, elapsed_time_ms=_elapsed_ms(started), error_message="robots policy rejects redirect target")
                        current_url = target
                        continue
                    elapsed = _elapsed_ms(started)
                    if response.status_code in (403, 429):
                        return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.BLOCKED, current_url, response.status_code, elapsed_time_ms=elapsed, error_message=f"HTTP {response.status_code}")
                    if response.status_code >= 400:
                        return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.HTTP_ERROR, current_url, response.status_code, elapsed_time_ms=elapsed, error_message=f"HTTP {response.status_code}")
                    html, read_error = self._read_html(response)
                    if read_error:
                        return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.HTTP_ERROR, current_url, response.status_code, elapsed_time_ms=elapsed, error_message=read_error)
                    if not html.strip():
                        return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.HTTP_ERROR, current_url, response.status_code, elapsed_time_ms=elapsed, error_message="empty response body")
                    return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.SUCCESS, current_url, response.status_code, html, elapsed)
                finally:
                    _close(response)
            else:
                return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.NETWORK_ERROR, current_url, elapsed_time_ms=_elapsed_ms(started), error_message="redirect limit exceeded")
        except requests.Timeout:
            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.TIMEOUT, elapsed_time_ms=_elapsed_ms(started), error_message="request timed out")
        except requests.ConnectionError as error:
            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.NETWORK_ERROR, elapsed_time_ms=_elapsed_ms(started), error_message=f"connection error: {error}")
        except requests.RequestException as error:
            return FetchResult(url, FetchMethod.REQUESTS, FetchStatus.NETWORK_ERROR, elapsed_time_ms=_elapsed_ms(started), error_message=f"request error: {error}")

    def _read_html(self, response) -> tuple[str, str | None]:
        content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        try:
            if content_length is not None and int(content_length) > self._max_html_bytes:
                return "", "response body exceeds configured limit"
        except (TypeError, ValueError):
            return "", "invalid Content-Length"
        # Некоторые тестовые adapters requests передают уже заполненный Response
        # без raw. У настоящего streamed-ответа raw всегда существует, поэтому
        # этот путь не отменяет потоковое чтение в production.
        if getattr(response, "raw", None) is None and isinstance(getattr(response, "_content", None), bytes):
            body = response.content
            if len(body) > self._max_html_bytes:
                return "", "response body exceeds configured limit"
            return _decode_html(body, response), None
        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            return response.text, None
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in iterator(chunk_size=64 * 1024):
                total += len(chunk)
                if total > self._max_html_bytes:
                    return "", "response body exceeds configured limit"
                chunks.append(chunk)
        except (OSError, requests.RequestException) as error:
            return "", f"response read error: {type(error).__name__}"
        return _decode_html(b"".join(chunks), response), None


def _decode_html(body: bytes, response) -> str:
    """Декодирует bytes без requests-default ISO-8859-1 для text/html."""
    content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
    match = re.search(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.I)
    declared = match.group(1) if match else EncodingDetector.find_declared_encoding(body, is_html=True)
    if declared:
        try:
            return body.decode(declared, errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        # Западноевропейский fallback сохраняет все bytes и, в отличие от
        # ISO-8859-1, корректно представляет знак евро из Windows-1252.
        return body.decode("windows-1252", errors="replace")


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _close(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
