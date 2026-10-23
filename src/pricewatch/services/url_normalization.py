"""Детерминированная нормализация URL без DNS и сетевых запросов."""
from __future__ import annotations
from urllib.parse import unquote_plus, urlsplit, urlunsplit
class UrlNormalizationError(ValueError): pass
def normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip(): raise UrlNormalizationError("URL is empty")
    if len(url.strip()) > 2048: raise UrlNormalizationError("URL is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise UrlNormalizationError("URL contains control character")
    try:
        parts = urlsplit(url.strip()); scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}: raise UrlNormalizationError("only HTTP(S) URLs are supported")
        if not parts.hostname: raise UrlNormalizationError("URL has no hostname")
        if parts.username is not None or parts.password is not None:
            raise UrlNormalizationError("URL credentials are prohibited")
        host = parts.hostname.encode("idna").decode("ascii").lower(); port = parts.port
    except (UnicodeError, ValueError) as error: raise UrlNormalizationError(f"invalid URL: {error}") from error
    authority = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    host = f"[{host}]" if ":" in host else host
    keep_port = port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443))
    query = "&".join(part for part in parts.query.split("&") if not (key := unquote_plus(part.partition("=")[0]).lower()).startswith("utm_") and key not in {"gclid", "fbclid"})
    normalized = urlunsplit((scheme, f"{authority}{host}{f':{port}' if keep_port else ''}", parts.path, query, ""))
    if len(normalized) > 2048: raise UrlNormalizationError("URL is too long")
    return normalized
