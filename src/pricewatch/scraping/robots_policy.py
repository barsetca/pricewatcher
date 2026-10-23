"""Консервативная robots.txt policy для одной проверки URL."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from pricewatch.scraping.url_security import UrlSecurity


USER_AGENT = "PriceWatch/0.1 (+https://example.invalid/pricewatch)"


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool | None
    reason: str
    robots_url: str


class RobotsPolicy:
    def __init__(self, session: requests.Session | None = None, timeout: float = 5.0, url_security: UrlSecurity | None = None, max_redirects: int = 5, max_robots_bytes: int = 512_000) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._security = url_security or UrlSecurity()
        self._max_redirects = max_redirects
        self._max_robots_bytes = max_robots_bytes
        if hasattr(self._session, "headers"):
            self._session.headers["User-Agent"] = USER_AGENT
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False

    def check(self, url: str) -> RobotsDecision:
        if error := self._security.validate(url):
            return RobotsDecision(None, error, "")
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        robots_url = f"{origin}/robots.txt"
        try:
            current_url = robots_url
            visited: set[str] = set()
            for _ in range(self._max_redirects + 1):
                if error := self._security.validate(current_url):
                    return RobotsDecision(None, f"unsafe robots redirect: {error}", current_url)
                response = self._session.get(
                    current_url,
                    timeout=self._timeout,
                    allow_redirects=False,
                    stream=True,
                    headers={"User-Agent": USER_AGENT, "Authorization": None, "Cookie": ""},
                    auth=(),
                )
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location") if hasattr(response, "headers") else None
                        if not location:
                            return RobotsDecision(None, "robots redirect without Location", current_url)
                        target = urljoin(current_url, location)
                        if target == current_url or target in visited:
                            return RobotsDecision(None, "robots redirect loop", target)
                        visited.add(current_url)
                        current_url = target
                        continue
                    return self._evaluate_response(response, url, current_url)
                finally:
                    _close(response)
            else:
                return RobotsDecision(None, "robots redirect limit exceeded", current_url)
        except requests.RequestException as error:
            return RobotsDecision(None, f"robots.txt unavailable: {type(error).__name__}", robots_url)

    def _evaluate_response(self, response, url: str, current_url: str) -> RobotsDecision:
        if response.status_code in (404, 410):
            return RobotsDecision(True, "robots.txt absent", current_url)
        if response.status_code == 304:
            return RobotsDecision(None, "robots.txt HTTP 304 without cached policy", current_url)
        if response.status_code in (401, 403):
            return RobotsDecision(False, f"robots.txt HTTP {response.status_code}", current_url)
        if response.status_code >= 500:
            return RobotsDecision(None, f"robots.txt HTTP {response.status_code}", current_url)
        if response.status_code >= 400:
            return RobotsDecision(None, f"robots.txt HTTP {response.status_code}", current_url)
        content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
        body, error = self._read_body(response)
        if error:
            return RobotsDecision(None, error, current_url)
        body = body.strip()
        if "html" in content_type.lower() or body.lower().startswith(("<!doctype html", "<html")) or "\x00" in body:
            return RobotsDecision(None, "robots.txt unsuitable content", current_url)
        try:
            parser = RobotFileParser()
            parser.parse(body.splitlines())
            allowed = parser.can_fetch(USER_AGENT, url)
        except (TypeError, ValueError) as error:
            return RobotsDecision(None, f"robots.txt parse error: {error}", current_url)
        return RobotsDecision(allowed, "robots.txt allows URL" if allowed else "robots.txt disallows URL", current_url)

    def _read_body(self, response) -> tuple[str, str | None]:
        length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        try:
            if length is not None and int(length) > self._max_robots_bytes:
                return "", "robots.txt exceeds configured limit"
        except (TypeError, ValueError):
            return "", "invalid robots Content-Length"
        if getattr(response, "raw", None) is None and isinstance(getattr(response, "_content", None), bytes):
            body = response.content
            if len(body) > self._max_robots_bytes:
                return "", "robots.txt exceeds configured limit"
            return body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace"), None
        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            return response.text, None
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in iterator(chunk_size=16 * 1024):
                total += len(chunk)
                if total > self._max_robots_bytes:
                    return "", "robots.txt exceeds configured limit"
                chunks.append(chunk)
        except (OSError, requests.RequestException) as error:
            return "", f"robots.txt read error: {type(error).__name__}"
        return b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8", errors="replace"), None


def _close(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
