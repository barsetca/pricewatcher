"""Консервативное извлечение цены без сетевых запросов."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus
from pricewatch.scraping.models import ExtractionResult

_CURRENCIES = {"€": "EUR", "EUR": "EUR", "$": "USD", "USD": "USD", "£": "GBP", "GBP": "GBP"}
_MONEY = re.compile(r"(?:EUR|USD|GBP|€|\$|£)\s*([\d.,\s\u00a0]+)|([\d.,\s\u00a0]+)\s*(EUR|USD|GBP|€|\$|£)", re.I)
_IDENTIFIER_KEYS = ("sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14")
_EXCLUDED_PRICE_CONTEXT = re.compile(
    r"(?:old|original|previous|regular|list|rrp|msrp|pvpr|crossed|strike|"
    r"historic|history|lowest|bestprice|shipping|delivery|env[ií]o|cuota|"
    r"installment|monthly|month|mes|discount|descuento|saving|ahorro|badge|"
    r"30.?day|30.?d[ií]as)",
    re.I,
)
_PRICE_MARKER = re.compile(r"(?:price|precio)", re.I)
_NAVIGATION_TITLES = {
    "home",
    "homepage",
    "inicio",
    "pagina de inicio",
    "startseite",
    "accueil",
}
_NON_PRIMARY_PRICE_CONTAINER = re.compile(
    r"(?:^|[\s_-])(?:old|original|previous|regular|list|rrp|msrp|pvpr|"
    r"historic|lowest|shipping|delivery|installment|monthly|"
    r"recommend(?:ed|ation|ations)?|related|cross.?sell|upsell|"
    r"accessor(?:y|ies)?|complement|frequently.?bought|bundle|combo|"
    r"unit.?price|price.?per.?unit|total.?price|"
    r"recomend(?:ado|ados|acion|aciones)?|accesorio(?:s)?|"
    r"complement(?:o|os)?|comprad(?:o|os).?junt(?:o|os)|precio.?unidad)"
    r"(?:$|[\s_-])",
    re.I,
)


@dataclass(frozen=True)
class _Source:
    method: ExtractionMethod
    candidates: tuple[tuple[Decimal, str | None], ...]
    title: str | None
    found: bool
    identity: str = "implicit"
    reason: str | None = None


class UniversalExtractor:
    """Использует JSON-LD и несколько BeautifulSoup/HTML источников."""

    def extract(self, html: str, url: str | None = None) -> ExtractionResult:
        if not html or not html.strip():
            return ExtractionResult(ExtractionStatus.PARTIAL, diagnostics={"reason": "empty HTML"})
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as error:  # BeautifulSoup редко ошибается, но контракт явный.
            return ExtractionResult(ExtractionStatus.PARSE_ERROR, diagnostics={"reason": str(error)[:200]})

        page_urls = _page_urls(soup, url)
        page_ids = _page_identifiers(soup)
        diagnostics: dict[str, Any] = {"url": url, "page_urls": sorted(page_urls), "sources": []}
        sources = (
            self._jsonld(soup, diagnostics, page_urls),
            self._microdata(soup, diagnostics, page_urls),
            self._metadata(soup),
            self._dom(soup, diagnostics, page_urls, page_ids),
        )
        found = [source for source in sources if source.found]
        diagnostics["sources"] = [source.method.value for source in found]
        diagnostics["source_details"] = [
            {
                "method": source.method.value,
                "identity": source.identity,
                "candidate_count": len(source.candidates),
                **({"reason": source.reason} if source.reason else {}),
            }
            for source in found
        ]
        if not found:
            return ExtractionResult(ExtractionStatus.PARTIAL, title=self._title(soup), diagnostics={**diagnostics, "reason": "no reliable price"})

        # Несовпадающий Product исключается из выбора. Неидентифицированный DOM
        # сам по себе не опровергает такой конфликт: иначе цена соседней карточки
        # снова могла бы стать SUCCESS только потому, что число совпало.
        mismatches = [source for source in found if source.identity == "mismatch"]
        independently_bound = [
            source for source in found
            if source.identity in {"exact_url", "exact_identifier", "main_entity", "exact_name"}
            and source.candidates
        ]
        if mismatches and not independently_bound:
            source = mismatches[0]
            return ExtractionResult(
                ExtractionStatus.AMBIGUOUS,
                title=self._best_title(soup, source.title),
                method=source.method,
                diagnostics={**diagnostics, "reason": source.reason or "product variant identity mismatch"},
            )
        eligible = [source for source in found if source.identity != "mismatch"]
        blocking = [source for source in eligible if source.reason in {"multiple products", "aggregate offer without exact price"}]
        if blocking:
            source = blocking[0]
            return ExtractionResult(
                ExtractionStatus.AMBIGUOUS,
                title=self._best_title(soup, source.title),
                method=source.method,
                diagnostics={**diagnostics, "reason": source.reason},
            )

        singleton: list[tuple[_Source, tuple[Decimal, str | None]]] = []
        internally_ambiguous: list[_Source] = []
        for source in eligible:
            unique = set(source.candidates)
            if len(unique) == 1:
                singleton.append((source, next(iter(unique))))
            elif len(unique) > 1:
                internally_ambiguous.append(source)

        if not singleton:
            mismatch = next((source for source in found if source.identity == "mismatch"), None)
            ambiguous = internally_ambiguous[0] if internally_ambiguous else mismatch
            if ambiguous is not None:
                return ExtractionResult(
                    ExtractionStatus.AMBIGUOUS,
                    title=self._best_title(soup, ambiguous.title),
                    method=ambiguous.method,
                    diagnostics={**diagnostics, "reason": ambiguous.reason or "conflicting price candidates"},
                )
            primary = eligible[0] if eligible else found[0]
            return ExtractionResult(
                ExtractionStatus.PARTIAL,
                title=self._best_title(soup, primary.title),
                method=primary.method,
                diagnostics={**diagnostics, "reason": primary.reason or "no exact price candidate"},
            )

        values = {value for _, value in singleton}
        if len(values) > 1:
            primary = singleton[0][0]
            return ExtractionResult(
                ExtractionStatus.AMBIGUOUS,
                title=self._best_title(soup, primary.title),
                method=primary.method,
                diagnostics={**diagnostics, "reason": "conflicting structured sources"},
            )

        selected_value = next(iter(values))
        # Неоднозначный DOM не перебивает точный Offer URL, если среди его
        # вариантов есть выбранная структурированная цена.
        for source in internally_ambiguous:
            exact_identity = any(item.identity == "exact_url" for item, _ in singleton)
            if selected_value not in set(source.candidates) or not exact_identity:
                return ExtractionResult(
                    ExtractionStatus.AMBIGUOUS,
                    title=self._best_title(soup, source.title),
                    method=source.method,
                    diagnostics={**diagnostics, "reason": source.reason or "conflicting price candidates"},
                )

        primary, (price, currency) = singleton[0]
        best_title = self._best_title(soup, *(source.title for source in found))
        diagnostics["candidates"] = [
            {"method": source.method.value, "price": str(value[0]), "currency": value[1]}
            for source, value in singleton
        ]
        if currency is None:
            return ExtractionResult(
                ExtractionStatus.PARTIAL,
                title=self._best_title(soup, primary.title, best_title),
                price=price,
                method=primary.method,
                diagnostics={**diagnostics, "reason": "currency missing"},
            )
        if price == 0:
            return ExtractionResult(
                ExtractionStatus.PARTIAL,
                title=self._best_title(soup, primary.title, best_title),
                price=price,
                currency=currency,
                method=primary.method,
                diagnostics={**diagnostics, "reason": "zero price requires context"},
            )
        base = {
            ExtractionMethod.JSON_LD: 0.95,
            ExtractionMethod.MICRODATA: 0.85,
            ExtractionMethod.METADATA: 0.70,
            ExtractionMethod.DOM_HEURISTIC: 0.55,
        }[primary.method]
        agreeing_methods = {source.method for source, value in singleton if value == selected_value}
        confidence = min(0.99, base + (0.03 if len(agreeing_methods) > 1 else 0.0))
        return ExtractionResult(
            ExtractionStatus.SUCCESS,
            title=self._best_title(soup, primary.title, best_title),
            price=price,
            currency=currency,
            method=primary.method,
            confidence_score=confidence,
            diagnostics=diagnostics,
        )

    def _jsonld(self, soup: BeautifulSoup, diagnostics: dict[str, Any], page_urls: set[str]) -> _Source:
        payloads: list[Any] = []
        malformed = 0
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payloads.append(json.loads(script.string or script.get_text()))
            except (json.JSONDecodeError, TypeError):
                malformed += 1
        diagnostics["jsonld_malformed"] = malformed
        if not payloads:
            return _Source(ExtractionMethod.JSON_LD, (), None, False)

        nodes = [node for payload in payloads for node in _walk(payload)]
        index = _id_index(nodes)
        products = _unique_nodes(product for payload in payloads for product in _walk_products(payload))
        all_offers = _unique_nodes(node for node in nodes if _types(node) & {"Offer", "AggregateOffer"})
        page_ids = _page_identifiers(soup)
        page_name = _normalized_name(self._title(soup))
        diagnostics["jsonld_product_count"] = len(products)

        exact_offers = [offer for offer in all_offers if _url_state(_entity_urls(offer, diagnostics.get("url")), page_urls) == "exact"]
        if exact_offers:
            candidates, nonexact = _prices_from_offers(exact_offers, index)
            diagnostics["jsonld_selection"] = "exact offer URL"
            diagnostics["jsonld_rejected_product_count"] = len(products)
            return _Source(
                ExtractionMethod.JSON_LD,
                tuple(candidates),
                _title_for_offers(exact_offers, products, index) or self._title(soup),
                True,
                "exact_url",
                "aggregate offer without exact price" if nonexact and not candidates else None,
            )

        main_products = _main_entity_products(nodes, index)
        ranked: list[tuple[str, dict[str, Any]]] = []
        for product in products:
            offers = _product_offers(product, index)
            urls = _entity_urls(product, diagnostics.get("url"))
            offer_urls = {item for offer in offers for item in _entity_urls(offer, diagnostics.get("url"))}
            url_state = _url_state(urls | offer_urls, page_urls)
            ids = _identifiers(product)
            id_state = "match" if page_ids and ids and page_ids & ids else ("mismatch" if page_ids and ids else "unknown")
            name = _normalized_name(_clean(product.get("name")))
            name_match = bool(name and page_name and name == page_name)
            if url_state == "exact":
                rank = "exact_url"
            elif id_state == "match":
                rank = "exact_identifier"
            elif product in main_products and url_state != "mismatch" and id_state != "mismatch":
                rank = "main_entity"
            elif name_match and url_state != "mismatch" and id_state != "mismatch":
                rank = "exact_name"
            elif url_state == "mismatch" or id_state == "mismatch":
                rank = "mismatch"
            else:
                rank = "implicit"
            ranked.append((rank, product))

        selected: list[dict[str, Any]] = []
        identity = "implicit"
        for rank in ("exact_url", "exact_identifier", "main_entity", "exact_name"):
            selected = [product for item_rank, product in ranked if item_rank == rank]
            if selected:
                identity = "exact_url" if rank == "exact_url" else rank
                break
        if not selected:
            implicit = [product for rank, product in ranked if rank == "implicit"]
            if len(products) == 1 and implicit:
                selected = implicit
            elif len(products) > 1:
                return _Source(ExtractionMethod.JSON_LD, (), self._title(soup), True, "implicit", "multiple products")
            elif products:
                diagnostics["jsonld_selection"] = "rejected identity mismatch"
                return _Source(
                    ExtractionMethod.JSON_LD,
                    (),
                    _clean(products[0].get("name")) or self._title(soup),
                    True,
                    "mismatch",
                    "product variant identity mismatch",
                )
            else:
                return _Source(ExtractionMethod.JSON_LD, (), None, False)

        if len(selected) > 1:
            return _Source(ExtractionMethod.JSON_LD, (), self._title(soup), True, identity, "multiple products")

        product = selected[0]
        offers = _product_offers(product, index)
        matching = [offer for offer in offers if _url_state(_entity_urls(offer, diagnostics.get("url")), page_urls) == "exact"]
        neutral = [offer for offer in offers if not _entity_urls(offer, diagnostics.get("url"))]
        usable = matching or neutral
        if offers and not usable:
            diagnostics["jsonld_selection"] = "rejected offer URL mismatch"
            return _Source(
                ExtractionMethod.JSON_LD,
                (),
                _clean(product.get("name")) or self._title(soup),
                True,
                "mismatch",
                "offer variant identity mismatch",
            )
        candidates, nonexact = _prices_from_offers(usable, index)
        diagnostics["jsonld_selection"] = identity
        return _Source(
            ExtractionMethod.JSON_LD,
            tuple(candidates),
            _clean(product.get("name")) or self._title(soup),
            True,
            identity,
            "aggregate offer without exact price" if nonexact and not candidates else None,
        )

    def _microdata(self, soup: BeautifulSoup, diagnostics: dict[str, Any], page_urls: set[str]) -> _Source:
        products = list(soup.find_all(attrs={"itemtype": re.compile(r"(?:^|/)Product$", re.I)}))
        if not products:
            return _Source(ExtractionMethod.MICRODATA, (), None, False)
        exact = [product for product in products if _url_state(_microdata_urls(product, diagnostics.get("url")), page_urls) == "exact"]
        selected = exact or products
        if len(selected) > 1:
            return _Source(ExtractionMethod.MICRODATA, (), None, True, "implicit", "multiple microdata products")
        product = selected[0]
        urls = _microdata_urls(product, diagnostics.get("url"))
        identity = "exact_url" if exact else ("mismatch" if urls else "implicit")
        prices = [parse_price(_tag_value(tag)) for tag in product.find_all(attrs={"itemprop": "price"})]
        currencies = [currency_of(_tag_value(tag)) for tag in product.find_all(attrs={"itemprop": "priceCurrency"})]
        currency = next((item for item in currencies if item), None)
        candidates = tuple((price, currency) for price in prices if price is not None)
        return _Source(ExtractionMethod.MICRODATA, candidates, _prop(product, "name"), True, identity)

    def _metadata(self, soup: BeautifulSoup) -> _Source:
        title = _meta(soup, "og:title", "twitter:title", "product:title")
        raw = _meta(soup, "product:price:amount", "og:price:amount", "price")
        currency = currency_of(_meta(soup, "product:price:currency", "og:price:currency", "price:currency"))
        price = parse_price(raw)
        return _Source(ExtractionMethod.METADATA, ((price, currency),) if price is not None else (), title, bool(title or raw))

    def _dom(self, soup: BeautifulSoup, diagnostics: dict[str, Any], page_urls: set[str], page_ids: set[str]) -> _Source:
        exact_candidates: list[tuple[Decimal, str | None]] = []
        implicit_candidates: list[tuple[Decimal, str | None]] = []
        exact_identities: set[str] = set()
        rejected_identity_count = 0
        for tag in soup.find_all(True):
            if tag.name in {"script", "style", "meta", "link", "del", "s", "strike"}:
                continue
            marker = _tag_marker(tag)
            data_values = _data_price_values(tag)
            if not data_values and not _PRICE_MARKER.search(marker):
                continue
            if _excluded_price_tag(tag, marker, page_urls, diagnostics.get("url")):
                continue
            if any(_PRICE_MARKER.search(_tag_marker(child)) for child in tag.find_all(True)):
                continue
            identity = _dom_identity(tag, page_urls, page_ids, diagnostics.get("url"))
            local_candidates: list[tuple[Decimal, str | None]] = []
            currency_hint = currency_of(tag.get("data-currency") or tag.get("data-price-currency"))
            for raw in data_values:
                if (price := parse_price(raw)) is not None and currency_hint:
                    local_candidates.append((price, currency_hint))
            text = _current_text(tag)
            if text and not _EXCLUDED_PRICE_CONTEXT.search(text) and not re.search(r"\b(?:from|desde|a\s+partir)\b", text, re.I):
                for match in _MONEY.finditer(text):
                    raw = match.group(1) or match.group(2)
                    symbol = match.group(3) or re.search(r"EUR|USD|GBP|€|\$|£", text, re.I).group()
                    if (price := parse_price(raw)) is not None:
                        local_candidates.append((price, currency_of(symbol)))
            if not local_candidates:
                continue
            if identity == "mismatch":
                rejected_identity_count += len(set(local_candidates))
                continue
            candidates = exact_candidates if identity != "implicit" else implicit_candidates
            candidates.extend(local_candidates)
            if identity != "implicit":
                exact_identities.add(identity)
        selected = exact_candidates or implicit_candidates
        diagnostics["dom_rejected_identity_count"] = rejected_identity_count
        if not selected and rejected_identity_count:
            return _Source(
                ExtractionMethod.DOM_HEURISTIC,
                (),
                self._title(soup),
                True,
                "mismatch",
                "conflicting HTML product identity",
            )
        source_identity = "exact_url" if "exact_url" in exact_identities else ("exact_identifier" if exact_identities else "implicit")
        return _Source(
            ExtractionMethod.DOM_HEURISTIC,
            tuple(selected),
            self._title(soup),
            bool(selected),
            source_identity,
            "multiple selected HTML prices" if len(set(selected)) > 1 else None,
        )

    @staticmethod
    def _title(soup: BeautifulSoup) -> str | None:
        return _first_informative_title(
            _meta(soup, "og:title"),
            _meta(soup, "twitter:title"),
            _clean(soup.title.get_text() if soup.title else None),
        )

    @classmethod
    def _best_title(cls, soup: BeautifulSoup, *titles: str | None) -> str | None:
        return _first_informative_title(*titles, cls._title(soup))


def parse_price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if "-" in raw or "−" in raw:
        return None
    currency_matches = list(re.finditer(r"EUR|USD|GBP|€|\$|£", raw, re.I))
    if len(currency_matches) > 1:
        return None
    if currency_matches:
        match = currency_matches[0]
        if raw[:match.start()].strip() and raw[match.end():].strip():
            return None
        without_currency = raw[:match.start()] + raw[match.end():]
    else:
        without_currency = raw
    if re.search(r"[^0-9,\.\s\u00a0\u202f]", without_currency):
        return None
    text = without_currency.replace("\u00a0", " ").replace("\u202f", " ").strip()
    # HTML-магазины нередко разделяют целую и дробную части вложенными span,
    # из-за чего BeautifulSoup даёт ``143 , 99``. Это не меняет правила
    # тысячных разделителей и сохраняет отказ для неоднозначного ``1,234``.
    text = re.sub(r"\s*([.,])\s*", r"\1", text)
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        normalized = text
    elif re.fullmatch(r"\d+[.,]\d{1,2}", text):
        normalized = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?: \d{3})+[.,]\d{1,2}", text):
        normalized = text.replace(" ", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):
        normalized = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", text):
        normalized = text.replace(",", "")
    else:
        return None
    try:
        price = Decimal(normalized)
        return price if price >= 0 else None
    except InvalidOperation:
        return None


def currency_of(value: Any) -> str | None:
    return _CURRENCIES.get(str(value).strip().upper()) if value else None


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _walk_products(value: Any, nested_item: bool = False) -> Iterable[dict[str, Any]]:
    """Не считает Product внутри itemOffered самостоятельной сущностью страницы."""
    if isinstance(value, dict):
        if not nested_item and "Product" in _types(value):
            yield value
        for key, child in value.items():
            yield from _walk_products(child, nested_item or key == "itemOffered")
    elif isinstance(value, list):
        for child in value:
            yield from _walk_products(child, nested_item)


def _types(node: dict[str, Any]) -> set[str]:
    values = node.get("@type", [])
    return {str(value).rsplit("/", 1)[-1] for value in (values if isinstance(values, list) else [values])}


def _id_index(nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        identifier = node.get("@id")
        if isinstance(identifier, str):
            result[identifier] = {**result.get(identifier, {}), **node}
    return result


def _resolve_node(value: Any, index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    identifier = value.get("@id")
    if isinstance(identifier, str) and identifier in index and index[identifier] is not value:
        return {**index[identifier], **value}
    return value


def _product_offers(product: dict[str, Any], index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    raw = product.get("offers", [])
    values = raw if isinstance(raw, list) else [raw]
    return [resolved for value in values if (resolved := _resolve_node(value, index)) is not None]


def _prices_from_offers(offers: Iterable[dict[str, Any]], index: dict[str, dict[str, Any]]) -> tuple[list[tuple[Decimal, str | None]], bool]:
    candidates: list[tuple[Decimal, str | None]] = []
    nonexact = False
    for offer in offers:
        values, item_nonexact = _offer_prices(offer, index, set())
        candidates.extend(values)
        nonexact = nonexact or item_nonexact
    return candidates, nonexact


def _offer_prices(offer: dict[str, Any], index: dict[str, dict[str, Any]], visited: set[str]) -> tuple[list[tuple[Decimal, str | None]], bool]:
    offer = _resolve_node(offer, index) or offer
    identifier = offer.get("@id")
    if isinstance(identifier, str):
        if identifier in visited:
            return [], False
        visited = {*visited, identifier}
    currency = currency_of(offer.get("priceCurrency"))
    nested = offer.get("offers", [])
    nested_values = nested if isinstance(nested, list) else [nested]
    resolved_nested = [resolved for value in nested_values if (resolved := _resolve_node(value, index)) is not None]
    if resolved_nested:
        results = [_offer_prices(child, index, visited) for child in resolved_nested]
        values = [price for child_values, _ in results for price in child_values]
        return values, any(flag for _, flag in results) and not values
    if "AggregateOffer" in _types(offer):
        boundaries = [parse_price(offer.get(key)) for key in ("lowPrice", "highPrice")]
        boundaries = [price for price in boundaries if price is not None]
        return [], len(boundaries) == 2 and boundaries[0] != boundaries[1]
    price = parse_price(offer.get("price"))
    return ([(price, currency)] if price is not None else []), False


def _main_entity_products(nodes: Iterable[dict[str, Any]], index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for node in nodes:
        raw = node.get("mainEntity")
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            resolved = _resolve_node(value, index)
            if resolved is not None and "Product" in _types(resolved):
                products.append(resolved)
    return _unique_nodes(products)


def _unique_nodes(nodes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for node in nodes:
        key = ("id", node.get("@id")) if node.get("@id") else ("object", id(node))
        if key not in seen:
            seen.add(key)
            result.append(node)
    return result


def _entity_urls(node: dict[str, Any], base_url: str | None) -> set[str]:
    result: set[str] = set()
    for key in ("url", "mainEntityOfPage"):
        raw = node.get(key)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if isinstance(value, dict):
                value = value.get("url") or value.get("@id")
            if isinstance(value, str) and (normalized := _normalize_identity_url(value, base_url)):
                result.add(normalized)
    return result


def _page_urls(soup: BeautifulSoup, url: str | None) -> set[str]:
    result: set[str] = set()
    primary = _normalize_identity_url(url, url) if url else None
    if primary:
        result.add(primary)
    auxiliary: list[str] = []
    canonical = soup.find("link", rel=lambda value: value and "canonical" in (value if isinstance(value, list) else [value]))
    if canonical and canonical.get("href"):
        auxiliary.append(str(canonical["href"]))
    if og_url := _meta(soup, "og:url"):
        auxiliary.append(og_url)
    for value in auxiliary:
        normalized = _normalize_identity_url(value, url)
        if normalized and (not primary or primary == normalized):
            result.add(normalized)
    return result


def _normalize_identity_url(value: str | None, base_url: str | None = None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(urljoin(base_url or "", value))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname.lower().rstrip(".")
        port = parts.port
        authority = host if port is None or (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443) else f"{host}:{port}"
        path = parts.path or "/"
        if path != "/":
            path = path.rstrip("/")
        return f"//{authority}{path}" + (f"?{parts.query}" if parts.query else "")
    except ValueError:
        return None


def _url_state(urls: set[str], page_urls: set[str]) -> str:
    if not urls:
        return "unknown"
    return "exact" if urls & page_urls else "mismatch"


def _identifiers(node: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in _IDENTIFIER_KEYS:
        raw = node.get(key)
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if isinstance(item, dict):
                item = item.get("value") or item.get("@value")
            if item is not None and (cleaned := _normalize_identifier(item)):
                values.add(cleaned)
    return values


def _page_identifiers(soup: BeautifulSoup) -> set[str]:
    result: set[str] = set()
    for key in _IDENTIFIER_KEYS:
        for tag in soup.find_all(attrs={"itemprop": key}):
            if cleaned := _normalize_identifier(_tag_value(tag)):
                result.add(cleaned)
        for tag in soup.find_all("meta", attrs={"property": re.compile(rf"(?:^|:){re.escape(key)}$", re.I)}):
            if cleaned := _normalize_identifier(tag.get("content")):
                result.add(cleaned)
    return result


def _normalize_identifier(value: Any) -> str | None:
    cleaned = re.sub(r"[^0-9a-z]", "", str(value).casefold())
    return cleaned or None


def _normalized_name(value: str | None) -> str | None:
    if not value:
        return None
    value = value.split("|", 1)[0]
    value = "".join(character for character in unicodedata.normalize("NFKD", value) if not unicodedata.combining(character))
    cleaned = " ".join(re.findall(r"[0-9a-z]+", value.casefold()))
    return cleaned or None


def _title_for_offers(offers: list[dict[str, Any]], products: list[dict[str, Any]], index: dict[str, dict[str, Any]]) -> str | None:
    offer_ids = {offer.get("@id") for offer in offers if offer.get("@id")}
    for product in products:
        linked = _product_offers(product, index)
        if any(item is offer or item.get("@id") in offer_ids for item in linked for offer in offers):
            return _clean(product.get("name"))
    return None


def _microdata_urls(root: Tag, base_url: str | None) -> set[str]:
    result: set[str] = set()
    for tag in root.find_all(attrs={"itemprop": "url"}):
        raw = tag.get("href") or tag.get("content") or tag.get("value")
        if isinstance(raw, str) and (normalized := _normalize_identity_url(raw, base_url)):
            result.add(normalized)
    return result


def _tag_marker(tag: Tag) -> str:
    return " ".join((*tag.get("class", []), str(tag.get("id", "")), str(tag.get("data-testid", "")))).lower()


def _data_price_values(tag: Tag) -> list[str]:
    return [str(tag[key]) for key in ("data-price", "data-current-price", "data-product-price", "data-sale-price") if tag.get(key) is not None]


def _dom_identity(tag: Tag, page_urls: set[str], page_ids: set[str], base_url: str | None) -> str:
    """Возвращает явную принадлежность HTML-блока товару либо её отсутствие."""
    evidence: set[str] = set()
    for node in (tag, *tag.parents):
        if not isinstance(node, Tag):
            continue
        urls = {
            normalized
            for key in ("data-product-url", "data-url", "data-canonical-url")
            if (normalized := _normalize_identity_url(node.get(key), base_url))
        }
        if urls:
            evidence.add("exact_url" if urls & page_urls else "mismatch")
        identifiers = {
            normalized
            for key in ("data-product-sku", "data-sku", "data-mpn", "data-gtin", "data-gtin8", "data-gtin12", "data-gtin13", "data-gtin14")
            if (normalized := _normalize_identifier(node.get(key)))
        }
        if identifiers and page_ids:
            evidence.add("exact_identifier" if identifiers & page_ids else "mismatch")
    if "mismatch" in evidence:
        return "mismatch"
    if "exact_url" in evidence:
        return "exact_url"
    if "exact_identifier" in evidence:
        return "exact_identifier"
    return "implicit"


def _excluded_price_tag(tag: Tag, marker: str, page_urls: set[str], base_url: str | None) -> bool:
    if _EXCLUDED_PRICE_CONTEXT.search(marker) or tag.find_parent(["del", "s", "strike"]):
        return True
    for ancestor in tag.parents:
        if not isinstance(ancestor, Tag) or ancestor.name in {"body", "html", "[document]"}:
            continue
        if _NON_PRIMARY_PRICE_CONTAINER.search(_tag_marker(ancestor)):
            return True
    anchor = tag if tag.name == "a" else tag.find_parent("a")
    if anchor and anchor.get("href"):
        normalized = _normalize_identity_url(str(anchor["href"]), base_url)
        if normalized and page_urls and normalized not in page_urls:
            return True
    return False


def _current_text(tag: Tag) -> str:
    values: list[str] = []
    for text in tag.find_all(string=True):
        parent = text.parent
        if parent and (parent.name in {"del", "s", "strike"} or parent.find_parent(["del", "s", "strike"])):
            continue
        values.append(str(text))
    return " ".join(" ".join(values).split())


def _clean(value: Any) -> str | None:
    value = " ".join(str(value).split()) if value is not None else ""
    return value or None


def _tag_value(tag: Tag | None) -> str | None:
    return _clean(tag.get("content") or tag.get("value") or tag.get_text(" ") if tag else None)


def _prop(root: Tag, name: str) -> str | None:
    return _tag_value(root.find(attrs={"itemprop": name}))


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean(tag["content"])
    return None


def _first_informative_title(*values: str | None) -> str | None:
    for value in values:
        title = _clean(value)
        if not title:
            continue
        normalized = " ".join(re.findall(r"[0-9a-z]+", _strip_accents(title).casefold()))
        if normalized not in _NAVIGATION_TITLES:
            return title
    return None


def _strip_accents(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
