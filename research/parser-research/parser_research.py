"""Universal, diagnostics-first product-page extraction experiment.

This intentionally contains no store-specific selectors or API calls.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError, RequestException, Timeout

TIMEOUT = 15
USER_AGENT = "PriceWatchResearch/0.1 (+https://example.invalid/pricewatch-research)"
CURRENCIES = {"EUR": "EUR", "€": "EUR", "USD": "USD", "$": "USD", "GBP": "GBP", "£": "GBP"}
PRICE_WORDS = ("price", "product-price", "current-price", "sale-price", "precio")


@dataclass
class PriceCandidate:
    price: Decimal
    currency: Optional[str]
    source: str
    context: str = ""

    def export(self) -> dict[str, Optional[str]]:
        return {"price": str(self.price), "currency": self.currency,
                "source": self.source, "context": self.context[:240]}


@dataclass
class ParseResult:
    url: str
    final_url: Optional[str] = None
    domain: Optional[str] = None
    http_status: Optional[int] = None
    title: Optional[str] = None
    price: Optional[Decimal] = None
    old_price: Optional[Decimal] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    seller: Optional[str] = None
    sku: Optional[str] = None
    extraction_method: Optional[str] = None
    confidence: str = "LOW"
    confidence_score: float = 0.0
    status: str = "PARSE_ERROR"
    error: Optional[str] = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def export(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("price", "old_price"):
            data[key] = str(data[key]) if data[key] is not None else None
        return data


@dataclass
class FetchResult:
    """Transport-only outcome; it deliberately contains no product knowledge."""
    requested_url: str
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    html: Optional[bytes | str] = None
    fetch_method: str = "REQUESTS"
    fetch_status: str = "HTTP_ERROR"
    error: Optional[str] = None
    elapsed_time_ms: int = 0

    def export(self) -> dict[str, Any]:
        return {"requested_url": self.requested_url, "final_url": self.final_url,
                "http_status": self.http_status, "fetch_method": self.fetch_method,
                "fetch_status": self.fetch_status, "error": self.error,
                "elapsed_time_ms": self.elapsed_time_ms,
                "html_size": len(self.html) if self.html is not None else 0}


def clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None


def first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def currency_of(value: Any) -> Optional[str]:
    value = clean(first(value))
    if not value:
        return None
    upper = value.upper()
    return CURRENCIES.get(upper) or CURRENCIES.get(value)


def decimal_of(value: Any) -> Optional[Decimal]:
    """Convert common European/US displayed price strings without float."""
    if value is None or isinstance(value, bool):
        return None
    text = clean(value)
    if not text:
        return None
    text = re.sub(r"[^0-9,.'\s]", "", text).replace("'", "").replace(" ", "")
    if not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        # The final separator is assumed to be decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = text.replace(",", ".") if len(parts[-1]) in (1, 2) else text.replace(",", "")
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) in (1, 2) else "".join(parts)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def type_names(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type", [])
    raw = raw if isinstance(raw, list) else [raw]
    return {str(item).rsplit("/", 1)[-1] for item in raw}


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def object_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return clean(value.get("name")) or clean(value.get("legalName"))
    return clean(value)


def add_offer(offer: dict[str, Any], candidates: list[PriceCandidate], source: str) -> dict[str, Any]:
    price = decimal_of(first(offer.get("price")))
    if price is None:
        price = decimal_of(first(offer.get("lowPrice")))
    if price is not None:
        candidates.append(PriceCandidate(price, currency_of(offer.get("priceCurrency")), source))
    availability = clean(first(offer.get("availability")))
    if availability and "/" in availability:
        availability = availability.rstrip("/").rsplit("/", 1)[-1]
    return {"price": price, "currency": currency_of(offer.get("priceCurrency")),
            "availability": availability,
            "seller": object_name(offer.get("seller"))}


def extract_jsonld(soup: BeautifulSoup, result: ParseResult) -> Optional[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    types: Counter[str] = Counter()
    malformed = 0
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        for node in walk_json(payload):
            node_types = type_names(node)
            types.update(node_types)
            if node_types & {"Product", "Offer", "AggregateOffer"}:
                objects.append(node)
    products = [x for x in objects if "Product" in type_names(x)]
    offers = [x for x in objects if type_names(x) & {"Offer", "AggregateOffer"}]
    candidates: list[PriceCandidate] = []
    choices: list[dict[str, Any]] = []
    for product in products:
        product_offers = product.get("offers", [])
        product_offers = product_offers if isinstance(product_offers, list) else [product_offers]
        extracted = [add_offer(o, candidates, "JSON_LD") for o in product_offers if isinstance(o, dict)]
        choices.append({"title": clean(product.get("name")), "sku": clean(product.get("sku")),
                        "gtin": clean(product.get("gtin")) or clean(product.get("gtin13")) or clean(product.get("gtin14")),
                        "offers": extracted})
    if not products:
        for offer in offers:
            choices.append({"title": None, "sku": None, "gtin": None,
                            "offers": [add_offer(offer, candidates, "JSON_LD_OFFER")]})
    result.diagnostics.update({"jsonld_types": dict(types), "jsonld_malformed_scripts": malformed,
                               "jsonld_product_count": len(products), "jsonld_offer_count": len(offers)})
    result.diagnostics.setdefault("candidate_prices", []).extend(c.export() for c in candidates)
    for choice in choices:
        if choice["title"] or choice["offers"]:
            offer = next((item for item in choice["offers"] if item["price"] is not None), None)
            if offer:
                return {**choice, **offer, "method": "JSON_LD"}
    return None


def prop(soup: BeautifulSoup, name: str) -> Optional[str]:
    tag = soup.find(attrs={"itemprop": name})
    if not tag:
        return None
    return clean(tag.get("content") or tag.get("value") or tag.get("href") or tag.get_text(" "))


def extract_microdata(soup: BeautifulSoup, result: ParseResult) -> Optional[dict[str, Any]]:
    price = decimal_of(prop(soup, "price") or prop(soup, "lowPrice"))
    title = prop(soup, "name")
    result.diagnostics["microdata_found"] = bool(title or price is not None)
    if not (title or price is not None):
        return None
    candidate = PriceCandidate(price, currency_of(prop(soup, "priceCurrency")), "MICRODATA") if price is not None else None
    if candidate:
        result.diagnostics.setdefault("candidate_prices", []).append(candidate.export())
    return {"title": title, "price": price, "currency": currency_of(prop(soup, "priceCurrency")),
            "availability": prop(soup, "availability"), "seller": prop(soup, "seller"),
            "sku": prop(soup, "sku"), "method": "MICRODATA"}


def meta_content(soup: BeautifulSoup, *names: str) -> Optional[str]:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return clean(tag["content"])
    return None


def extract_metadata(soup: BeautifulSoup, result: ParseResult) -> Optional[dict[str, Any]]:
    title = meta_content(soup, "og:title", "twitter:title", "product:title")
    raw_price = meta_content(soup, "product:price:amount", "og:price:amount", "price")
    price = decimal_of(raw_price)
    currency = currency_of(meta_content(soup, "product:price:currency", "og:price:currency", "price:currency"))
    if not (title or price is not None):
        return None
    if price is not None:
        result.diagnostics.setdefault("candidate_prices", []).append(PriceCandidate(price, currency, "METADATA").export())
    return {"title": title, "price": price, "currency": currency, "availability": None,
            "seller": None, "sku": None, "method": "METADATA"}


def extract_dom(soup: BeautifulSoup, result: ParseResult) -> Optional[dict[str, Any]]:
    candidates: list[PriceCandidate] = []
    for tag in soup.find_all(True):
        marker = " ".join([" ".join(tag.get("class", [])), str(tag.get("id", "")),
                           str(tag.get("data-testid", "")), str(tag.get("itemprop", ""))]).lower()
        if not any(word in marker for word in PRICE_WORDS):
            continue
        text = clean(tag.get("content") or tag.get("value") or tag.get_text(" "))
        if not text or len(text) > 180 or re.search(r"(?:month|mes|cuota|financi)", text, re.I):
            continue
        money = re.search(r"(?:EUR|USD|GBP|€|\$|£)\s*([\d., ]+)|([\d., ]+)\s*(EUR|USD|GBP|€|\$|£)", text, re.I)
        if not money:
            continue
        value = money.group(1) or money.group(2)
        symbol = re.search(r"EUR|USD|GBP|€|\$|£", text, re.I)
        price = decimal_of(value)
        if price is not None:
            candidates.append(PriceCandidate(price, currency_of(symbol.group()) if symbol else None, "DOM_HEURISTIC", text))
    unique = {(c.price, c.currency) for c in candidates}
    result.diagnostics.setdefault("candidate_prices", []).extend(c.export() for c in candidates)
    result.diagnostics["dom_unique_price_count"] = len(unique)
    if not candidates:
        return None
    # A DOM-only number is trusted only when the page exposes one distinct labelled price.
    candidate = candidates[0]
    return {"title": meta_content(soup, "og:title", "twitter:title") or clean(soup.title.string if soup.title else None),
            "price": candidate.price, "currency": candidate.currency, "availability": None,
            "seller": None, "sku": None, "method": "DOM_HEURISTIC", "ambiguous": len(unique) > 1}


def apply_data(result: ParseResult, data: dict[str, Any]) -> None:
    for field_name in ("title", "price", "currency", "availability", "seller", "sku"):
        if data.get(field_name) is not None:
            setattr(result, field_name, data[field_name])
    result.extraction_method = data["method"]
    candidates = result.diagnostics.get("candidate_prices", [])
    unique = {(x["price"], x.get("currency")) for x in candidates}
    conflicting_prices = len(unique) > 1
    result.diagnostics["conflicting_price_values"] = len(unique)
    has_price = result.price is not None
    ambiguous = data.get("ambiguous", False) or (conflicting_prices and data["method"] == "DOM_HEURISTIC")
    base = {"JSON_LD": 0.96, "MICRODATA": 0.91, "METADATA": 0.76, "DOM_HEURISTIC": 0.58}[data["method"]]
    if not result.title:
        base -= 0.12
    if not has_price:
        base -= 0.28
    # Multiple Offer values can be legitimate marketplace/variant data, but they
    # still make it less certain that the selected value is the primary offer.
    if conflicting_prices and data["method"] in {"JSON_LD", "MICRODATA", "METADATA"}:
        base = min(base, 0.78)
    if ambiguous:
        base = min(base, 0.42)
    result.confidence_score = round(max(base, 0.05), 2)
    result.confidence = "HIGH" if result.confidence_score >= .85 else "MEDIUM" if result.confidence_score >= .60 else "LOW"
    result.status = "AMBIGUOUS" if ambiguous else ("SUCCESS" if result.title and has_price else "PARTIAL")


class UniversalExtractor:
    """Extract product signals from supplied HTML, independent of its fetcher."""

    def extract(self, fetch: FetchResult) -> ParseResult:
        result = ParseResult(url=fetch.requested_url, final_url=fetch.final_url,
                             domain=urlparse(fetch.final_url or fetch.requested_url).netloc,
                             http_status=fetch.http_status)
        if fetch.fetch_status != "SUCCESS" or fetch.html is None:
            result.status = "NOT_RUN"
            result.error = fetch.error or f"fetch status: {fetch.fetch_status}"
            return result
        try:
            soup = BeautifulSoup(fetch.html, "html.parser")
            for extractor in (extract_jsonld, extract_microdata, extract_metadata, extract_dom):
                data = extractor(soup, result)
                if data:
                    apply_data(result, data)
                    return result
            result.status, result.error = "PARSE_ERROR", "No product signals or price candidates found"
        except Exception as exc:  # keep one malformed response from ending the experiment
            result.status, result.error = "PARSE_ERROR", f"unexpected parser error: {type(exc).__name__}: {exc}"
        return result


def robots_allowed(session: requests.Session, url: str) -> tuple[bool, Optional[str]]:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = session.get(robots_url, timeout=TIMEOUT)
        if response.status_code >= 400:
            return True, f"robots.txt unavailable ({response.status_code}); proceeding"
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url), None
    except RequestException as exc:
        return True, f"robots.txt check failed ({type(exc).__name__}); proceeding"


def parse_url(session: requests.Session, url: str) -> ParseResult:
    result = ParseResult(url=url, domain=urlparse(url).netloc)
    allowed, robots_note = robots_allowed(session, url)
    result.diagnostics["robots_note"] = robots_note
    if not allowed:
        result.status, result.error = "ROBOTS_DENIED", "robots.txt disallows this User-Agent"
        return result
    started = time.monotonic()
    try:
        response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except Timeout:
        result.status, result.error = "HTTP_ERROR", f"timeout after {TIMEOUT}s"
        return result
    except ConnectionError as exc:
        result.status, result.error = "HTTP_ERROR", f"connection error: {exc}"
        return result
    except RequestException as exc:
        result.status, result.error = "HTTP_ERROR", f"request error: {exc}"
        return result
    result.http_status, result.final_url = response.status_code, response.url
    result.domain = urlparse(response.url).netloc
    if response.status_code in (403, 429):
        result.status, result.error = "BLOCKED", f"HTTP {response.status_code}; access may be blocked"
        return result
    if response.status_code >= 400:
        result.status, result.error = "HTTP_ERROR", f"HTTP {response.status_code}"
        return result
    fetch = FetchResult(url, response.url, response.status_code, response.content, "REQUESTS",
                        "SUCCESS", elapsed_time_ms=round((time.monotonic() - started) * 1000))
    parsed = UniversalExtractor().extract(fetch)
    parsed.diagnostics["robots_note"] = robots_note
    return parsed


def print_result(result: ParseResult) -> None:
    amount = f"{result.price} {result.currency or ''}".strip() if result.price is not None else "—"
    old = f"{result.old_price} {result.currency or ''}".strip() if result.old_price is not None else "—"
    print("-" * 50)
    for key, value in (("DOMAIN", result.domain), ("HTTP", result.http_status), ("TITLE", result.title),
                       ("PRICE", amount), ("OLD PRICE", old), ("AVAILABILITY", result.availability),
                       ("SELLER", result.seller), ("METHOD", result.extraction_method),
                       ("CONFIDENCE", f"{result.confidence} ({result.confidence_score:.2f})"),
                       ("STATUS", result.status), ("ERROR", result.error)):
        if value is not None:
            print(f"{key}: {value}")
    print("-" * 50)


def save_results(results: list[ParseResult], directory: Path) -> None:
    exports = [item.export() for item in results]
    (directory / "results.json").write_text(json.dumps(exports, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = [key for key in exports[0] if key != "diagnostics"] if exports else []
    with (directory / "results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row[key] for key in columns} for row in exports)


def main() -> int:
    directory = Path(__file__).resolve().parent
    urls = [line.strip() for line in (directory / "urls.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9,en;q=0.7"})
    results = []
    for url in urls:
        result = parse_url(session, url)
        results.append(result)
        print_result(result)
    save_results(results, directory)
    counts = Counter(item.status for item in results)
    print(f"Completed {len(results)} URLs: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
