from decimal import Decimal

import pytest
import requests

from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus
from pricewatch.scraping.fetch_strategy import FetchStrategy
from pricewatch.scraping.models import ExtractionResult, FetchResult
from pricewatch.scraping.price_checker import PriceChecker
from pricewatch.scraping.robots_policy import RobotsDecision, RobotsPolicy
from pricewatch.scraping.url_security import UrlSecurity


class Response:
    def __init__(self, status, text): self.status_code, self.text = status, text


class Session:
    def __init__(self, response=None, error=None): self.response, self.error = response, error
    def get(self, *args, **kwargs):
        if self.error: raise self.error
        return self.response


def test_robots_policy_allow_deny_absent_and_errors():
    security = UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    allow = RobotsPolicy(Session(Response(200, "User-agent: PriceWatch\nDisallow: /private")), url_security=security).check("https://shop.test/item?q=1")
    deny = RobotsPolicy(Session(Response(200, "User-agent: PriceWatch\nDisallow: /private")), url_security=security).check("https://shop.test/private/a")
    assert allow.allowed is True and deny.allowed is False
    assert RobotsPolicy(Session(Response(404, "")), url_security=security).check("https://shop.test/x").allowed is True
    assert RobotsPolicy(Session(Response(403, "")), url_security=security).check("https://shop.test/x").allowed is False
    assert RobotsPolicy(Session(Response(503, "")), url_security=security).check("https://shop.test/x").allowed is None
    assert RobotsPolicy(Session(Response(304, "")), url_security=security).check("https://shop.test/x").allowed is None
    assert RobotsPolicy(Session(Response(200, "<html>error</html>")), url_security=security).check("https://shop.test/x").allowed is None


def test_robots_policy_rejects_oversized_streamed_body():
    response = Response(200, "")
    response.headers = {"Content-Length": "1000"}
    policy = RobotsPolicy(Session(response), url_security=UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]), max_robots_bytes=100)
    decision = policy.check("https://shop.test/x")
    assert decision.allowed is None and "limit" in decision.reason


def test_robots_policy_streams_and_closes_on_limit_and_read_error():
    security = UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    response = Response(200, "")
    reads, closed = [], []
    def chunks(chunk_size):
        for chunk in (b"User-agent: *\\n", b"Allow: /" * 100):
            reads.append(len(chunk))
            yield chunk
    response.iter_content = chunks
    response.close = lambda: closed.append(True)
    decision = RobotsPolicy(Session(response), url_security=security, max_robots_bytes=100).check("https://shop.test/x")
    assert decision.allowed is None and "limit" in decision.reason
    assert len(reads) == 2 and closed == [True]

    response = Response(200, "")
    def broken_chunks(chunk_size):
        raise requests.ReadTimeout("slow robots")
        yield b""
    response.iter_content = broken_chunks
    closed = []
    response.close = lambda: closed.append(True)
    decision = RobotsPolicy(Session(response), url_security=security).check("https://shop.test/x")
    assert decision.allowed is None and "read error" in decision.reason
    assert closed == [True]


def test_robots_policy_closes_response_when_unexpected_processing_error_escapes():
    security = UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    response = Response(200, "")
    response.iter_content = lambda chunk_size: (_ for _ in ()).throw(RuntimeError("unexpected"))
    closed = []
    response.close = lambda: closed.append(True)
    with pytest.raises(RuntimeError, match="unexpected"):
        RobotsPolicy(Session(response), url_security=security).check("https://shop.test/x")
    assert closed == [True]


def test_fetch_strategy_order_and_fallback_rules():
    strategy = FetchStrategy()
    assert strategy.order(None) == (FetchMethod.REQUESTS, FetchMethod.PLAYWRIGHT)
    assert strategy.order(FetchMethod.PLAYWRIGHT, False) == (FetchMethod.REQUESTS,)
    assert strategy.may_fallback(FetchStatus.SUCCESS, ExtractionStatus.PARTIAL, 200)
    assert not strategy.may_fallback(FetchStatus.SUCCESS, ExtractionStatus.AMBIGUOUS, 200)
    assert not strategy.may_fallback(FetchStatus.BLOCKED, None, 403)
    assert not strategy.may_fallback(FetchStatus.HTTP_ERROR, None, 404)
    assert strategy.may_fallback(FetchStatus.TIMEOUT, None, None)


class Policy:
    def __init__(self, allowed=True): self.allowed = allowed; self.calls = []
    def check(self, url): self.calls.append(url); return RobotsDecision(self.allowed, "policy", "robots")


class Fetcher:
    def __init__(self, result): self.result = result; self.calls = []
    def fetch(self, url): self.calls.append(url); return self.result


class Extractor:
    def __init__(self, result): self.result = result; self.calls = []
    def extract(self, html, url): self.calls.append((html, url)); return self.result


def fetch(method, status=FetchStatus.SUCCESS, html="<html>", final="https://shop.test/item", code=200):
    return FetchResult("https://shop.test/item", method, status, final, code, html if status is FetchStatus.SUCCESS else None, 1, "error" if status is not FetchStatus.SUCCESS else None)


def extraction(status=ExtractionStatus.SUCCESS):
    return ExtractionResult(status, price=Decimal("9") if status is ExtractionStatus.SUCCESS else None, currency="EUR" if status is ExtractionStatus.SUCCESS else None, method=ExtractionMethod.JSON_LD if status is ExtractionStatus.SUCCESS else None)


def checker(policy=True, request=None, browser=None, result=None):
    request = request or Fetcher(fetch(FetchMethod.REQUESTS))
    browser = browser or Fetcher(fetch(FetchMethod.PLAYWRIGHT))
    extractor = Extractor(result or extraction())
    return PriceChecker(Policy(policy), request, extractor, browser, resolver=lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]), request, browser, extractor


def test_price_checker_success_preferred_fallback_and_diagnostics():
    check, request, browser, extractor = checker()
    outcome = check.check("https://shop.test/item")
    assert outcome.fetch.method is FetchMethod.REQUESTS and len(outcome.attempts) == 1 and extractor.calls
    check, request, browser, extractor = checker(request=Fetcher(fetch(FetchMethod.REQUESTS)), browser=Fetcher(fetch(FetchMethod.PLAYWRIGHT)), result=extraction(ExtractionStatus.PARTIAL))
    outcome = check.check("https://shop.test/item")
    assert len(outcome.attempts) == 1 and not browser.calls
    check, request, browser, _ = checker()
    assert check.check("https://shop.test/item", FetchMethod.PLAYWRIGHT).fetch.method is FetchMethod.REQUESTS


def test_checker_stops_for_robots_blocked_and_ssrf_without_fetching():
    check, request, browser, extractor = checker(policy=False)
    result = check.check("https://shop.test/item")
    assert result.fetch.status is FetchStatus.ROBOTS_DENIED and not request.calls and not extractor.calls
    check, request, _, _ = checker()
    assert check.check("http://127.0.0.1/x").extraction.status is ExtractionStatus.NOT_RUN
    assert not request.calls
    assert check.check("https://user:pass@shop.test/x").extraction.status is ExtractionStatus.NOT_RUN


def test_checker_never_extracts_failed_fetch_and_rejects_unsafe_redirect():
    failed = Fetcher(fetch(FetchMethod.REQUESTS, FetchStatus.BLOCKED, code=403))
    check, request, browser, extractor = checker(request=failed)
    assert check.check("https://shop.test/item").extraction.status is ExtractionStatus.NOT_RUN
    assert not extractor.calls and not browser.calls
    redirect = Fetcher(fetch(FetchMethod.REQUESTS, final="http://127.0.0.1/admin"))
    check, _, _, extractor = checker(request=redirect)
    result = check.check("https://shop.test/item")
    assert result.fetch.status is FetchStatus.NETWORK_ERROR and not extractor.calls
