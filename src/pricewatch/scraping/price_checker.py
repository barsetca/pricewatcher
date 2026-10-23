"""Оркестрация одной проверки URL без persistence и бизнес-правил цен."""

from __future__ import annotations

from pricewatch.domain.enums import ExtractionStatus, FetchMethod, FetchStatus
from pricewatch.scraping.fetch_strategy import FetchStrategy
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.robots_policy import RobotsPolicy
from pricewatch.scraping.url_security import UrlSecurity


class PriceChecker:
    def __init__(self, robots_policy: RobotsPolicy, requests_fetcher, extractor, playwright_fetcher=None, strategy: FetchStrategy | None = None, resolver=None, url_security: UrlSecurity | None = None) -> None:
        self._robots = robots_policy
        self._requests = requests_fetcher
        self._playwright = playwright_fetcher  # Сохранён для будущего M4.2, но не вызывается.
        self._extractor = extractor
        self._strategy = strategy or FetchStrategy()
        self._security = url_security or UrlSecurity(resolver) if resolver else UrlSecurity()
        if hasattr(self._requests, "set_robots_policy"):
            self._requests.set_robots_policy(robots_policy)

    def check(self, url: str, preferred_fetch_method: FetchMethod | None = None) -> PriceCheckResult:
        invalid = self._validate_url(url)
        if invalid:
            return self._not_run(url, invalid)
        decision = self._robots.check(url)
        if decision.allowed is not True:
            status = FetchStatus.ROBOTS_DENIED if decision.allowed is False else FetchStatus.NETWORK_ERROR
            return self._not_run(url, decision.reason, status)
        methods = self._strategy.order(preferred_fetch_method, False)
        attempts: list[FetchResult] = []
        for index, method in enumerate(methods):
            fetcher = self._requests if method is FetchMethod.REQUESTS else self._playwright
            fetched = fetcher.fetch(url)
            attempts.append(fetched)
            redirect_error = self._redirect_error(fetched.final_url)
            if redirect_error:
                fetched = FetchResult(fetched.requested_url, fetched.method, FetchStatus.NETWORK_ERROR, fetched.final_url, fetched.http_status, elapsed_time_ms=fetched.elapsed_time_ms, error_message=redirect_error)
                attempts[-1] = fetched
                extraction = self._not_run_extraction(redirect_error)
                return PriceCheckResult(fetched, extraction, tuple(attempts))
            extraction = self._extract(fetched)
            if extraction.status is ExtractionStatus.SUCCESS or index == len(methods) - 1 or not self._strategy.may_fallback(fetched.status, extraction.status if fetched.status is FetchStatus.SUCCESS else None, fetched.http_status):
                return PriceCheckResult(fetched, extraction, tuple(attempts))
        return PriceCheckResult(attempts[-1], self._not_run_extraction("no fetch method"), tuple(attempts))

    def _extract(self, fetched: FetchResult) -> ExtractionResult:
        if fetched.status is not FetchStatus.SUCCESS or not fetched.html:
            return self._not_run_extraction(fetched.error_message or fetched.status.value)
        return self._extractor.extract(fetched.html, fetched.final_url or fetched.requested_url)

    @staticmethod
    def _not_run_extraction(reason: str) -> ExtractionResult:
        return ExtractionResult(ExtractionStatus.NOT_RUN, diagnostics={"reason": reason})

    def _not_run(self, url: str, reason: str, status: FetchStatus = FetchStatus.NETWORK_ERROR) -> PriceCheckResult:
        fetch = FetchResult(url, FetchMethod.REQUESTS, status, elapsed_time_ms=0, error_message=reason)
        return PriceCheckResult(fetch, self._not_run_extraction(reason), (fetch,))

    def _validate_url(self, url: str) -> str | None:
        return self._security.validate(url)

    def _redirect_error(self, url: str | None) -> str | None:
        if not url:
            return None
        return self._validate_url(url)
