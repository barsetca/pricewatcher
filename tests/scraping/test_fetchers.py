import sys
import types
from unittest.mock import Mock

import requests

from pricewatch.domain.enums import FetchMethod, FetchStatus
from pricewatch.scraping.playwright_fetcher import PlaywrightFetcher
from pricewatch.scraping.requests_fetcher import RequestsFetcher
from pricewatch.scraping.url_security import UrlSecurity


def response(status=200, text="<html>ok</html>", url="https://final.test"):
    item = Mock(status_code=status, text=text, url=url)
    item.headers = {}
    item.iter_content = None
    return item


def test_requests_fetcher_success_redirect_and_headers():
    session = Mock(headers={})
    session.get.return_value = response()
    security = UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    result = RequestsFetcher(session, url_security=security).fetch("https://start.test")
    assert (result.status, result.method, result.final_url, result.html) == (FetchStatus.SUCCESS, FetchMethod.REQUESTS, "https://start.test", "<html>ok</html>")
    session.get.assert_called_once()
    assert session.get.call_args.kwargs["allow_redirects"] is False
    assert session.get.call_args.kwargs["stream"] is True
    assert session.get.call_args.kwargs["headers"]["Authorization"] is None
    assert session.get.call_args.kwargs["headers"]["Cookie"] == ""


def test_requests_fetcher_classifies_http_and_network_failures():
    session = Mock(headers={})
    fetcher = RequestsFetcher(session, url_security=UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]))
    for code, expected in ((403, FetchStatus.BLOCKED), (429, FetchStatus.BLOCKED), (404, FetchStatus.HTTP_ERROR)):
        session.get.return_value = response(code)
        assert fetcher.fetch("https://test").status is expected
    session.get.side_effect = requests.Timeout()
    assert fetcher.fetch("https://test").status is FetchStatus.TIMEOUT
    session.get.side_effect = requests.ConnectionError("offline")
    assert fetcher.fetch("https://test").status is FetchStatus.NETWORK_ERROR
    session.get.side_effect = None
    session.get.return_value = response(text=" ")
    assert fetcher.fetch("https://test").error_message == "empty response body"


def test_requests_decodes_valid_utf8_without_charset_instead_of_requests_latin1_default():
    item = requests.Response()
    item.status_code = 200
    item.headers = {"Content-Type": "text/html"}
    item._content = "<title>Auriculares inalámbricos — 149,99 €</title>".encode("utf-8")
    item.raw = None
    item.encoding = "ISO-8859-1"  # requests default for text/* without charset
    session = Mock(headers={})
    session.get.return_value = item
    security = UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    result = RequestsFetcher(session, url_security=security).fetch("https://shop.test/item")
    assert result.status is FetchStatus.SUCCESS
    assert "inalámbricos" in result.html and "€" in result.html
    assert "Ã" not in result.html


def test_requests_honors_http_and_html_declared_charsets_and_handles_invalid_bytes():
    security = UrlSecurity(lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])

    def fetch(body, content_type="text/html"):
        item = requests.Response()
        item.status_code = 200
        item.headers = {"Content-Type": content_type}
        item._content = body
        item.raw = None
        session = Mock(headers={})
        session.get.return_value = item
        return RequestsFetcher(session, url_security=security).fetch("https://shop.test/item")

    http_declared = fetch("<title>España 19,99 €</title>".encode("iso-8859-15"), "text/html; charset=iso-8859-15")
    assert "España" in http_declared.html and "€" in http_declared.html
    html_declared = fetch('<meta charset="iso-8859-1"><title>España</title>'.encode("iso-8859-1"))
    assert "España" in html_declared.html
    damaged = fetch(b"<title>Price \x81 19.99 EUR</title>")
    assert damaged.status is FetchStatus.SUCCESS and "19.99 EUR" in damaged.html


def test_playwright_is_import_safe_and_reports_missing_dependency(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def absent(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", absent)
    result = PlaywrightFetcher().fetch("https://test")
    assert result.method is FetchMethod.PLAYWRIGHT
    assert result.status is FetchStatus.NETWORK_ERROR
    assert "not installed" in result.error_message


def test_playwright_fetcher_success_and_timeout_with_fake_api(monkeypatch):
    class FakeTimeout(Exception): pass
    class FakeError(Exception): pass
    class Page:
        url = "https://final.test"
        def __init__(self, fail=False): self.fail = fail; self.closed = False
        def goto(self, *args, **kwargs):
            if self.fail: raise self.fail
            return types.SimpleNamespace(status=200)
        def content(self): return "<html>ok</html>"
        def close(self): self.closed = True
    class Browser:
        def __init__(self, fail=False): self.page = Page(fail); self.closed = False
        def new_page(self): return self.page
        def close(self): self.closed = True
    class Manager:
        def __init__(self, fail=False): self.browser = Browser(fail); self.chromium = types.SimpleNamespace(launch=lambda headless: self.browser)
        def __enter__(self): return self
        def __exit__(self, *args): return False
    manager = Manager()
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error, sync_api.TimeoutError, sync_api.sync_playwright = FakeError, FakeTimeout, lambda: manager
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    result = PlaywrightFetcher().fetch("https://start.test")
    assert (result.status, result.final_url, manager.browser.page.closed, manager.browser.closed) == (FetchStatus.SUCCESS, "https://final.test", True, True)
    timeout_manager = Manager(fail=FakeTimeout())
    sync_api.sync_playwright = lambda: timeout_manager
    timeout = PlaywrightFetcher().fetch("https://start.test")
    assert timeout.status is FetchStatus.TIMEOUT
    assert timeout_manager.browser.page.closed and timeout_manager.browser.closed
    error_manager = Manager(fail=FakeError("navigation failed"))
    sync_api.sync_playwright = lambda: error_manager
    assert PlaywrightFetcher().fetch("https://start.test").status is FetchStatus.NETWORK_ERROR


def test_playwright_launch_error_is_diagnostic_and_does_not_require_browser(monkeypatch):
    class FakeTimeout(Exception): pass
    class FakeError(Exception): pass
    class Manager:
        chromium = types.SimpleNamespace(launch=lambda **kwargs: (_ for _ in ()).throw(FakeError("browser binary missing")))
        def __enter__(self): return self
        def __exit__(self, *args): return False
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error, sync_api.TimeoutError, sync_api.sync_playwright = FakeError, FakeTimeout, Manager
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    result = PlaywrightFetcher().fetch("https://test")
    assert result.status is FetchStatus.NETWORK_ERROR
    assert "browser binary missing" in result.error_message
