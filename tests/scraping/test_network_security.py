from collections import deque

import pytest
import requests

from pricewatch.domain.enums import FetchStatus
from pricewatch.scraping.requests_fetcher import RequestsFetcher
from pricewatch.scraping.price_checker import PriceChecker
from pricewatch.scraping.robots_policy import RobotsDecision, RobotsPolicy
from pricewatch.scraping.url_security import UrlSecurity


class Response:
    def __init__(self, status, text="<html>ok</html>", location=None):
        self.status_code, self.text = status, text
        self.headers = {} if location is None else {"Location": location}


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = deque(responses), [], {}
        self.trust_env = True
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.popleft()


class Policy:
    def __init__(self, denied=()): self.denied, self.calls = set(denied), []
    def check(self, url):
        self.calls.append(url)
        return RobotsDecision(url not in self.denied, "denied" if url in self.denied else "allow", "robots")


def security(addresses):
    return UrlSecurity(lambda host, port: [(None, None, None, None, (address, 0)) for address in addresses.get(host, [])])


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://[::1]/x", "http://192.168.1.1/x", "http://169.254.1.1/x", "ftp://shop.test/x", "https://user:pass@shop.test/x"])
def test_url_security_rejects_unsafe_inputs(url):
    assert UrlSecurity(lambda host, port: []).validate(url) is not None


def test_url_security_rejects_mixed_dns_and_resolution_failure():
    assert security({"mixed.test": ["93.184.216.34", "10.0.0.1"]}).validate("https://mixed.test/x") == "unsafe IP address"
    assert UrlSecurity(lambda host, port: (_ for _ in ()).throw(OSError())).validate("https://bad.test/x") == "hostname cannot be resolved safely"


def test_url_security_requires_at_least_one_public_dns_address():
    assert UrlSecurity(lambda host, port: []).validate("https://empty.test/x") == "hostname has no resolved addresses"
    assert security({"one.test": ["93.184.216.34"]}).validate("https://one.test/x") is None
    assert security({"many.test": ["93.184.216.34", "8.8.8.8"]}).validate("https://many.test/x") is None


@pytest.mark.parametrize("url", ["https://[invalid/a", "https://shop.test:bad/x", "https:///x"])
def test_url_security_returns_diagnostics_for_malformed_urls(url):
    assert UrlSecurity(lambda host, port: []).validate(url) is not None


def test_security_errors_prevent_requests_robots_and_price_checker_calls():
    bad_url = "https://[invalid/a"
    session = Session([Response(200)])
    assert RequestsFetcher(session).fetch(bad_url).status is FetchStatus.NETWORK_ERROR
    assert not session.calls
    robots_session = Session([Response(200, "User-agent: *\nAllow: /")])
    assert RobotsPolicy(robots_session).check(bad_url).allowed is None
    assert not robots_session.calls
    checker = PriceChecker(Policy(), RequestsFetcher(Session([Response(200)])), object(), resolver=lambda host, port: [])
    assert checker.check("https://empty.test/x").fetch.status is FetchStatus.NETWORK_ERROR


def test_requests_manual_redirect_checks_target_before_next_request():
    session = Session([Response(302, location="/next"), Response(200)])
    policy = Policy()
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"]}), robots_policy=policy)
    result = fetcher.fetch("https://shop.test/start")
    assert result.status is FetchStatus.SUCCESS and result.final_url == "https://shop.test/next"
    assert [call[0] for call in session.calls] == ["https://shop.test/start", "https://shop.test/next"]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert session.trust_env is False


def test_requests_does_not_request_unsafe_or_robots_denied_redirect_target():
    session = Session([Response(302, location="http://127.0.0.1/admin")])
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"]}), robots_policy=Policy())
    assert fetcher.fetch("https://shop.test/start").status is FetchStatus.NETWORK_ERROR
    assert len(session.calls) == 1


def test_cross_origin_redirect_strips_sensitive_request_headers():
    session = Session([Response(302, location="https://other.test/next"), Response(200)])
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"], "other.test": ["93.184.216.35"]}))
    assert fetcher.fetch("https://shop.test/start").status is FetchStatus.SUCCESS
    assert session.calls[1][1]["headers"]["Authorization"] is None
    assert session.calls[1][1]["headers"]["Cookie"] == ""
    target = "https://other.test/blocked"
    session = Session([Response(302, location=target)])
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"], "other.test": ["93.184.216.35"]}), robots_policy=Policy([target]))
    assert fetcher.fetch("https://shop.test/start").status is FetchStatus.ROBOTS_DENIED
    assert len(session.calls) == 1


@pytest.mark.parametrize("locations", [
    ["https://b.test/one"],
    ["https://b.test/one", "https://b.test/two"],
    ["https://b.test/one", "https://a.test/two"],
    ["/one", "/two"],
])
def test_redirect_chain_never_reuses_session_secrets_or_cookie_jar(locations):
    responses = [Response(302, location=location) for location in locations] + [Response(200)]
    session = Session(responses)
    session.headers.update({"Authorization": "Bearer SECRET", "Cookie": "session=SECRET"})
    # Реальный RequestsCookieJar поддерживает set; fake сохраняет факт наличия jar.
    session.cookies = {"session": "SECRET"}
    addresses = {"a.test": ["93.184.216.34"], "b.test": ["93.184.216.35"]}
    fetcher = RequestsFetcher(session, url_security=security(addresses))
    assert fetcher.fetch("https://a.test/start").status is FetchStatus.SUCCESS
    for _, kwargs in session.calls:
        headers = kwargs["headers"]
        assert headers["Authorization"] is None
        assert headers["Cookie"] == ""
        assert "SECRET" not in str(headers)


class CaptureAdapter(requests.adapters.BaseAdapter):
    def __init__(self, content=b"<html>ok</html>"): self.requests = []; self.content = content
    def close(self): pass
    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.status_code, response.url, response._content = 200, request.url, self.content
        response.request = request
        return response


def test_public_fetch_and_robots_prepared_requests_exclude_session_auth_and_cookies():
    session = requests.Session()
    adapter = CaptureAdapter()
    session.mount("https://", adapter)
    session.auth = ("alice", "SECRET")
    session.headers.update({"Authorization": "Bearer SECRET", "Cookie": "session=SECRET"})
    session.cookies.set("session", "SECRET", domain="shop.test")
    safe = security({"shop.test": ["93.184.216.34"]})
    assert RequestsFetcher(session, url_security=safe).fetch("https://shop.test/item").status is FetchStatus.SUCCESS
    prepared = adapter.requests[-1]
    assert "authorization" not in {name.lower() for name in prepared.headers}
    assert prepared.headers.get("Cookie", "") == ""
    robots_session = requests.Session()
    robots_adapter = CaptureAdapter(b"User-agent: *\nAllow: /")
    robots_session.mount("https://", robots_adapter)
    robots_session.auth = ("alice", "SECRET")
    robots_session.cookies.set("session", "SECRET", domain="shop.test")
    assert RobotsPolicy(robots_session, url_security=safe).check("https://shop.test/item").allowed is True
    prepared = robots_adapter.requests[-1]
    assert "authorization" not in {name.lower() for name in prepared.headers}
    assert prepared.headers.get("Cookie", "") == ""


def test_requests_rejects_redirect_loop_and_limit():
    session = Session([Response(302, location="/start")])
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"]}))
    assert "loop" in fetcher.fetch("https://shop.test/start").error_message
    session = Session([Response(302, location=f"/r{index}") for index in range(6)])
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"]}), max_redirects=2)
    assert "limit" in fetcher.fetch("https://shop.test/start").error_message


def test_requests_limits_streamed_html_before_unbounded_memory_use():
    response = Response(200)
    response.headers["Content-Length"] = "999"
    session = Session([response])
    fetcher = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"]}), max_html_bytes=100)
    result = fetcher.fetch("https://shop.test/item")
    assert result.status is FetchStatus.HTTP_ERROR
    assert "limit" in result.error_message


def test_streamed_body_stops_at_limit_and_closes_response():
    response = Response(200)
    read = []
    response.close = lambda: read.append("closed")
    def chunks(chunk_size):
        for chunk in (b"a" * 60, b"b" * 60, b"c" * 60):
            read.append(len(chunk))
            yield chunk
    response.iter_content = chunks
    session = Session([response])
    result = RequestsFetcher(session, url_security=security({"shop.test": ["93.184.216.34"]}), max_html_bytes=100).fetch("https://shop.test/item")
    assert result.status is FetchStatus.HTTP_ERROR and "limit" in result.error_message
    assert read == [60, 60, "closed"]
    assert session.calls[0][1]["stream"] is True


def test_streamed_body_accepts_missing_or_incorrect_content_length_and_closes():
    for header in (None, "12"):
        response = Response(200)
        response.headers = {} if header is None else {"Content-Length": header}
        response.iter_content = lambda chunk_size: iter((b"<html>", b"ok</html>"))
        closed = []
        response.close = lambda: closed.append(True)
        result = RequestsFetcher(Session([response]), url_security=security({"shop.test": ["93.184.216.34"]}), max_html_bytes=100).fetch("https://shop.test/item")
        assert result.status is FetchStatus.SUCCESS
        assert closed == [True]


def test_stream_read_error_is_diagnostic_and_closes_response():
    response = Response(200)
    def broken_chunks(chunk_size):
        yield b"<html>"
        raise requests.ReadTimeout("slow body")
    response.iter_content = broken_chunks
    closed = []
    response.close = lambda: closed.append(True)
    result = RequestsFetcher(Session([response]), url_security=security({"shop.test": ["93.184.216.34"]})).fetch("https://shop.test/item")
    assert result.status is FetchStatus.HTTP_ERROR
    assert "read error" in result.error_message
    assert closed == [True]


def test_requests_closes_response_when_unexpected_processing_error_escapes():
    response = Response(200)
    response.iter_content = lambda chunk_size: (_ for _ in ()).throw(RuntimeError("unexpected"))
    closed = []
    response.close = lambda: closed.append(True)
    with pytest.raises(RuntimeError, match="unexpected"):
        RequestsFetcher(Session([response]), url_security=security({"shop.test": ["93.184.216.34"]})).fetch("https://shop.test/item")
    assert closed == [True]
