"""Детерминированный порядок fetchers и правила единственного fallback."""

from __future__ import annotations

from pricewatch.domain.enums import ExtractionStatus, FetchMethod, FetchStatus


class FetchStrategy:
    def order(self, preferred: FetchMethod | None, playwright_available: bool = True) -> tuple[FetchMethod, ...]:
        if preferred is FetchMethod.PLAYWRIGHT and playwright_available:
            return (FetchMethod.PLAYWRIGHT, FetchMethod.REQUESTS)
        if preferred is FetchMethod.PLAYWRIGHT:
            return (FetchMethod.REQUESTS,)
        return (FetchMethod.REQUESTS, FetchMethod.PLAYWRIGHT) if playwright_available else (FetchMethod.REQUESTS,)

    def may_fallback(self, fetch_status: FetchStatus, extraction_status: ExtractionStatus | None, http_status: int | None) -> bool:
        if fetch_status in {FetchStatus.BLOCKED, FetchStatus.ROBOTS_DENIED}:
            return False
        if fetch_status is FetchStatus.HTTP_ERROR:
            return False
        if fetch_status in {FetchStatus.TIMEOUT, FetchStatus.NETWORK_ERROR}:
            return True
        return fetch_status is FetchStatus.SUCCESS and extraction_status in {ExtractionStatus.PARTIAL, ExtractionStatus.PARSE_ERROR}
