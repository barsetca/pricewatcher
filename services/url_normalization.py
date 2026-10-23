"""Детерминированная нормализация URL без DNS и сетевых запросов."""

from __future__ import annotations

from urllib.parse import unquote_plus, urlsplit, urlunsplit


class UrlNormalizationError(ValueError):
    """URL синтаксически не пригоден для сохранения."""


def normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise UrlNormalizationError("URL is empty")
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise UrlNormalizationError("only HTTP(S) URLs are supported")
        if not parts.hostname:
            raise UrlNormalizationError("URL has no hostname")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as error:
        raise UrlNormalizationError(f"invalid URL: {error}") from error

    # urlsplit уже отделяет IPv6 brackets; возвращаем их только в netloc.
    formatted_host = f"[{host}]" if ":" in host else host
    authority = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    keep_port = port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443))
    netloc = f"{authority}{formatted_host}{f':{port}' if keep_port else ''}"
    query = _without_tracking(parts.query)
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def _without_tracking(query: str) -> str:
    kept: list[str] = []
    for part in query.split("&"):
        key = unquote_plus(part.partition("=")[0]).lower()
        if key.startswith("utm_") or key in {"gclid", "fbclid"}:
            continue
        kept.append(part)
    return "&".join(kept)
