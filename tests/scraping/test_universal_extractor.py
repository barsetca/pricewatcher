from decimal import Decimal
from pathlib import Path

import pytest

from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus
from pricewatch.scraping.universal_extractor import UniversalExtractor, parse_price


FIXTURES = Path(__file__).parents[1] / "fixtures" / "scraping"


def extract(name):
    return UniversalExtractor().extract((FIXTURES / name).read_text(encoding="utf-8"))


def test_structured_extraction_priority_and_decimal_parsing():
    jsonld = extract("jsonld_offer.html")
    assert (jsonld.status, jsonld.price, jsonld.currency, jsonld.method) == (ExtractionStatus.SUCCESS, Decimal("1234.56"), "EUR", ExtractionMethod.JSON_LD)
    microdata = extract("microdata.html")
    assert (microdata.status, microdata.method) == (ExtractionStatus.SUCCESS, ExtractionMethod.MICRODATA)
    metadata = extract("metadata.html")
    assert (metadata.status, metadata.price, metadata.method) == (ExtractionStatus.SUCCESS, Decimal("1234.56"), ExtractionMethod.METADATA)


def test_microdata_navigation_name_falls_back_to_product_page_title():
    html = (FIXTURES / "microdata_title_conflict.html").read_text(encoding="utf-8")
    result = UniversalExtractor().extract(
        html,
        "https://shop.test/ibanez_ac340ce_opn_artwood.htm",
    )

    assert (result.status, result.price, result.currency, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("374.00"),
        "EUR",
        ExtractionMethod.MICRODATA,
    )
    assert result.title == "Ibanez AC340CE OPN Artwood"


def test_navigation_metadata_title_falls_back_to_informative_document_title():
    html = """
      <title>Ibanez AC340CE OPN Artwood - Test Shop</title>
      <meta property="og:title" content="Home">
      <div itemscope itemtype="https://schema.org/Product">
        <span itemprop="name">Home</span>
        <meta itemprop="price" content="374">
        <meta itemprop="priceCurrency" content="EUR">
      </div>
    """

    result = UniversalExtractor().extract(html)

    assert result.status is ExtractionStatus.SUCCESS
    assert result.title == "Ibanez AC340CE OPN Artwood - Test Shop"


def test_valid_microdata_name_is_kept_when_page_title_is_absent():
    html = """
      <div itemscope itemtype="https://schema.org/Product">
        <span itemprop="name">Studio Monitor X2</span>
        <meta itemprop="price" content="199">
        <meta itemprop="priceCurrency" content="EUR">
      </div>
    """

    result = UniversalExtractor().extract(html)

    assert result.status is ExtractionStatus.SUCCESS
    assert result.title == "Studio Monitor X2"


def test_only_navigation_titles_are_not_returned_as_product_name():
    html = """
      <title>Home</title>
      <meta property="og:title" content="Home">
      <div itemscope itemtype="https://schema.org/Product">
        <span itemprop="name">Home</span>
        <meta itemprop="price" content="199">
        <meta itemprop="priceCurrency" content="EUR">
      </div>
    """

    result = UniversalExtractor().extract(html)

    assert (result.status, result.price, result.currency) == (
        ExtractionStatus.SUCCESS,
        Decimal("199"),
        "EUR",
    )
    assert result.title is None


def test_dom_ignores_old_price_but_marks_conflicts_ambiguous():
    current = extract("dom_current_old.html")
    assert (current.status, current.price) == (ExtractionStatus.SUCCESS, Decimal("149.99"))
    assert extract("dom_conflict.html").status is ExtractionStatus.AMBIGUOUS


@pytest.mark.parametrize("text,expected", [("1234.56", "1234.56"), ("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("1234,56", "1234.56"), ("1 234,56 €", "1234.56")])
def test_price_formats_use_decimal(text, expected):
    assert parse_price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["-19", "-19,99", "−19,99", "- 19,99 €", "", "price unavailable", "19,foo"])
def test_negative_and_invalid_prices_are_rejected(text):
    assert parse_price(text) is None


@pytest.mark.parametrize("text,expected", [("19,99", "19.99"), ("1 234,56", "1234.56"), ("1.234,56", "1234.56"), ("1,234.56", "1234.56"), ("1234.56", "1234.56"), ("1234,56", "1234.56"), ("1234", "1234")])
def test_unambiguous_price_formats_are_accepted(text, expected):
    assert parse_price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["1.234", "1,234", "12.345", "12,345"])
def test_ambiguous_three_digit_separator_is_rejected(text):
    assert parse_price(text) is None


@pytest.mark.parametrize("text", ["19-99", "19/99", "12..34", "19€-", "1 2 3"])
def test_damaged_money_format_is_not_repaired_by_deleting_characters(text):
    assert parse_price(text) is None


@pytest.mark.parametrize("text,expected", [("19.99", "19.99"), ("19,99", "19.99"), ("19 , 99", "19.99"), ("1234", "1234"), ("1\u00a0234,56", "1234.56"), ("1\u202f234,56", "1234.56")])
def test_m3_2_supported_money_formats(text, expected):
    assert parse_price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["19EUR20", "€19€", "EUR 19 USD", "19€20", "USD 19 EUR"])
def test_currency_must_be_single_and_at_the_edge_of_amount(text):
    assert parse_price(text) is None


@pytest.mark.parametrize("text,expected", [("€19,99", "19.99"), ("19,99 €", "19.99"), ("EUR 19.99", "19.99"), ("19.99 EUR", "19.99"), ("$1,234.56", "1234.56"), ("1.234,56 EUR", "1234.56"), ("GBP 99.00", "99")])
def test_valid_edge_currency_notation(text, expected):
    assert parse_price(text) == Decimal(expected)


def test_jsonld_graph_arrays_ranges_multiple_products_missing_currency_and_malformed_json():
    extractor = UniversalExtractor()
    graph = '<script type="application/ld+json">{"@graph":[{"@type":"Product","name":"A","offers":[{"@type":"Offer","price":"9","priceCurrency":"EUR"}]}]}</script>'
    assert extractor.extract(graph).status is ExtractionStatus.SUCCESS
    range_html = '<script type="application/ld+json">{"@type":"Product","offers":{"@type":"AggregateOffer","lowPrice":"9","highPrice":"12","priceCurrency":"EUR"}}</script>'
    assert extractor.extract(range_html).status is ExtractionStatus.AMBIGUOUS
    many = '<script type="application/ld+json">[{"@type":"Product","offers":{"price":"9","priceCurrency":"EUR"}},{"@type":"Product","offers":{"price":"9","priceCurrency":"EUR"}}]</script>'
    assert extractor.extract(many).status is ExtractionStatus.AMBIGUOUS
    missing_currency = '<meta property="product:price:amount" content="9">'
    assert extractor.extract(missing_currency).status is ExtractionStatus.PARTIAL
    malformed_fallback = '<script type="application/ld+json">broken</script><meta property="product:price:amount" content="9"><meta property="product:price:currency" content="EUR">'
    assert extractor.extract(malformed_fallback).status is ExtractionStatus.SUCCESS


@pytest.mark.parametrize("offer,status", [
    ('{"@type":"AggregateOffer","lowPrice":"9","highPrice":"12","priceCurrency":"EUR"}', ExtractionStatus.AMBIGUOUS),
    ('{"@type":"AggregateOffer","lowPrice":"9","priceCurrency":"EUR"}', ExtractionStatus.PARTIAL),
    ('{"@type":"AggregateOffer","highPrice":"12","priceCurrency":"EUR"}', ExtractionStatus.PARTIAL),
    ('{"@type":"AggregateOffer","lowPrice":"9","highPrice":"9","priceCurrency":"EUR"}', ExtractionStatus.PARTIAL),
    ('{"@type":"Offer","price":"9","priceCurrency":"EUR"}', ExtractionStatus.SUCCESS),
])
def test_aggregate_offer_boundaries_never_become_exact_price(offer, status):
    html = f'<script type="application/ld+json">{{"@type":"Product","offers":{offer}}}</script>'
    result = UniversalExtractor().extract(html)
    assert result.status is status
    if status is not ExtractionStatus.SUCCESS:
        assert result.diagnostics.get("reason")


def test_aggregate_offer_uses_nested_concrete_offers():
    html = '<script type="application/ld+json">{"@type":"Product","offers":{"@type":"AggregateOffer","offers":[{"@type":"Offer","price":"9","priceCurrency":"EUR"}]}}</script>'
    result = UniversalExtractor().extract(html)
    assert (result.status, result.price, result.currency) == (ExtractionStatus.SUCCESS, Decimal("9"), "EUR")


def test_nested_item_offered_is_not_a_second_product_but_independent_products_are():
    nested = '<script type="application/ld+json">{"@type":"Product","name":"Main","offers":{"@type":"Offer","price":"99","priceCurrency":"EUR","itemOffered":{"@type":"Product","name":"Main"}}}</script>'
    result = UniversalExtractor().extract(nested)
    assert (result.status, result.price) == (ExtractionStatus.SUCCESS, Decimal("99"))
    independent = '<script type="application/ld+json">{"@graph":[{"@type":"Product","offers":{"price":"9","priceCurrency":"EUR"}},{"@type":"Product","offers":{"price":"10","priceCurrency":"EUR"}}]}</script>'
    result = UniversalExtractor().extract(independent)
    assert result.status is ExtractionStatus.AMBIGUOUS and result.diagnostics["reason"] == "multiple products"


def test_conflicting_structured_sources_are_ambiguous_but_old_and_shipping_markup_are_not():
    extractor = UniversalExtractor()
    same = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"99","priceCurrency":"EUR"}}</script><meta property="product:price:amount" content="99"><meta property="product:price:currency" content="EUR">'
    assert extractor.extract(same).status is ExtractionStatus.SUCCESS
    conflict = same.replace('content="99"><meta property', 'content="9"><meta property', 1)
    result = extractor.extract(conflict)
    assert result.status is ExtractionStatus.AMBIGUOUS and result.diagnostics["reason"] == "conflicting structured sources"
    currency_conflict = same.replace('content="99"><meta property="product:price:currency" content="EUR"', 'content="99"><meta property="product:price:currency" content="USD"')
    assert extractor.extract(currency_conflict).status is ExtractionStatus.AMBIGUOUS


def test_price_source_fallback_only_when_jsonld_has_no_price_and_no_ambiguity():
    extractor = UniversalExtractor()
    jsonld_without_price = '<script type="application/ld+json">{"@type":"Product","name":"Reliable title"}</script>'
    metadata = '<meta property="product:price:amount" content="99"><meta property="product:price:currency" content="EUR">'
    result = extractor.extract(jsonld_without_price + metadata)
    assert (result.status, result.price, result.currency, result.method, result.title) == (ExtractionStatus.SUCCESS, Decimal("99"), "EUR", ExtractionMethod.METADATA, "Reliable title")
    microdata = '<div itemscope itemtype="https://schema.org/Product"><meta itemprop="price" content="99"><meta itemprop="priceCurrency" content="EUR"></div>'
    assert extractor.extract(jsonld_without_price + microdata).method is ExtractionMethod.MICRODATA
    dom = '<span class="product-price">99 EUR</span>'
    assert extractor.extract(jsonld_without_price + dom).method is ExtractionMethod.DOM_HEURISTIC
    priced_jsonld = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"99","priceCurrency":"EUR"}}</script>'
    assert extractor.extract(priced_jsonld + '<meta property="og:title" content="Only title">').method is ExtractionMethod.JSON_LD


def test_jsonld_ambiguity_cannot_fall_back_to_metadata_and_all_empty_is_partial():
    metadata = '<meta property="product:price:amount" content="99"><meta property="product:price:currency" content="EUR">'
    multiple = '<script type="application/ld+json">[{"@type":"Product"},{"@type":"Product"}]</script>'
    result = UniversalExtractor().extract(multiple + metadata)
    assert result.status is ExtractionStatus.AMBIGUOUS and result.diagnostics["reason"] == "multiple products"
    aggregate = '<script type="application/ld+json">{"@type":"Product","offers":{"@type":"AggregateOffer","lowPrice":"9","highPrice":"12","priceCurrency":"EUR"}}</script>'
    assert UniversalExtractor().extract(aggregate + metadata).status is ExtractionStatus.AMBIGUOUS
    result = UniversalExtractor().extract('<script type="application/ld+json">{"@type":"Product"}</script><meta property="og:title" content="No price">')
    assert result.status is ExtractionStatus.PARTIAL and result.diagnostics["reason"]


def test_all_usable_structured_fallback_sources_are_compared():
    jsonld = '<script type="application/ld+json">{"@type":"Product","name":"JSON title"}</script>'
    microdata = '<div itemscope itemtype="https://schema.org/Product"><meta itemprop="price" content="{price}"><meta itemprop="priceCurrency" content="EUR"></div>'
    metadata = '<meta property="product:price:amount" content="{price}"><meta property="product:price:currency" content="EUR">'
    extractor = UniversalExtractor()
    conflict = extractor.extract(jsonld + microdata.format(price="9") + metadata.format(price="99"))
    assert conflict.status is ExtractionStatus.AMBIGUOUS and conflict.diagnostics["reason"] == "conflicting structured sources"
    same = extractor.extract(jsonld + microdata.format(price="99") + metadata.format(price="99"))
    assert (same.status, same.price, same.currency, same.method, same.title) == (ExtractionStatus.SUCCESS, Decimal("99"), "EUR", ExtractionMethod.MICRODATA, "JSON title")
    no_json = extractor.extract(microdata.format(price="9") + metadata.format(price="99"))
    assert no_json.status is ExtractionStatus.AMBIGUOUS
    same_no_json = extractor.extract(microdata.format(price="99") + metadata.format(price="99"))
    assert same_no_json.method is ExtractionMethod.MICRODATA


def test_no_price_empty_html_and_extractor_never_fetches(monkeypatch):
    assert extract("empty.html").status is ExtractionStatus.PARTIAL
    assert UniversalExtractor().extract("").status is ExtractionStatus.PARTIAL
    monkeypatch.setattr("requests.Session.get", lambda *args, **kwargs: pytest.fail("network request"))
    assert UniversalExtractor().extract("<meta property='product:price:amount' content='9'>").status is ExtractionStatus.PARTIAL


def test_graph_resolves_main_entity_and_offer_reference_for_selected_url():
    html = (FIXTURES / "jsonld_graph_selected_offer.html").read_text(encoding="utf-8")
    result = UniversalExtractor().extract(html, "https://shop.test/products/modelo-azul")
    assert (result.status, result.price, result.currency, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("143.99"),
        "EUR",
        ExtractionMethod.JSON_LD,
    )
    assert result.diagnostics["jsonld_selection"] == "exact offer URL"
    assert result.confidence_score == pytest.approx(0.98)


def test_main_entity_selects_one_product_and_resolves_offer_by_id_without_urls():
    html = """
      <script type="application/ld+json">{"@graph":[
        {"@type":"WebPage","mainEntity":{"@id":"#selected"}},
        {"@id":"#other","@type":"Product","offers":{"price":"10","priceCurrency":"EUR"}},
        {"@id":"#selected","@type":"Product","offers":{"@id":"#selected-offer"}},
        {"@id":"#selected-offer","@type":"Offer","price":"20","priceCurrency":"EUR"}
      ]}</script>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/item")
    assert (result.status, result.price) == (ExtractionStatus.SUCCESS, Decimal("20"))
    assert result.diagnostics["jsonld_selection"] == "main_entity"


def test_offer_url_for_other_variant_is_rejected_without_fallback_price():
    html = """
      <title>Modelo con ANC</title>
      <script type="application/ld+json">
        {"@type":"Product","name":"Modelo sin ANC","offers":{
          "@type":"Offer","url":"https://shop.test/sin-anc",
          "price":"123.99","priceCurrency":"EUR"}}
      </script>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/con-anc")
    assert result.status is ExtractionStatus.AMBIGUOUS
    assert result.price is None
    assert "mismatch" in result.diagnostics["reason"]


def test_selected_offer_beats_neighbor_variant_in_pc_like_fixture():
    html = (FIXTURES / "variant_neighbor_pc_like.html").read_text(encoding="utf-8")
    result = UniversalExtractor().extract(html, "https://shop.test/modelo-4-con-cancelacion")
    assert (result.status, result.price, result.currency, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("143.99"),
        "EUR",
        ExtractionMethod.JSON_LD,
    )
    assert all(candidate["price"] != "123.99" for candidate in result.diagnostics["candidates"])


def test_multiple_products_can_be_selected_by_identifier_or_exact_offer_url():
    by_identifier = """
      <meta property="product:sku" content="SELECTED">
      <script type="application/ld+json">{"@graph":[
        {"@type":"Product","sku":"OTHER","offers":{"price":"10","priceCurrency":"EUR"}},
        {"@type":"Product","sku":"SELECTED","offers":{"price":"20","priceCurrency":"EUR"}}
      ]}</script>
    """
    result = UniversalExtractor().extract(by_identifier, "https://shop.test/item")
    assert (result.status, result.price) == (ExtractionStatus.SUCCESS, Decimal("20"))
    by_url = """
      <script type="application/ld+json">{"@graph":[
        {"@type":"Product","offers":{"@type":"Offer","url":"https://shop.test/red","price":"10","priceCurrency":"EUR"}},
        {"@type":"Product","offers":{"@type":"Offer","url":"https://shop.test/blue","price":"20","priceCurrency":"EUR"}}
      ]}</script>
    """
    result = UniversalExtractor().extract(by_url, "https://shop.test/blue")
    assert (result.status, result.price) == (ExtractionStatus.SUCCESS, Decimal("20"))


def test_variant_query_is_not_lost_during_url_matching():
    html = """
      <link rel="canonical" href="https://shop.test/item">
      <script type="application/ld+json">{"@type":"Product","url":"https://shop.test/item",
        "offers":{"price":"10","priceCurrency":"EUR"}}</script>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/item?color=blue")
    assert result.status is ExtractionStatus.AMBIGUOUS


def test_beautifulsoup_extracts_html_only_price_and_structured_data_attributes(monkeypatch):
    import pricewatch.scraping.universal_extractor as module

    real = module.BeautifulSoup
    calls = []
    monkeypatch.setattr(module, "BeautifulSoup", lambda *args, **kwargs: calls.append(args[0]) or real(*args, **kwargs))
    html = '<main><span data-current-price="149.99" data-currency="EUR"></span></main>'
    result = module.UniversalExtractor().extract(html, "https://shop.test/item")
    assert calls == [html]
    assert (result.status, result.price, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("149.99"),
        ExtractionMethod.DOM_HEURISTIC,
    )


def test_html_price_excludes_pvpr_installment_historic_minimum_and_neighbor_variant():
    html = """
      <span id="current-price">149,99 €</span>
      <span id="original-price">PVPR 199 €</span>
      <span class="monthly-price">12 € / mes</span>
      <span class="lowest-price-30-days">Mínimo 30 días: 159,99 €</span>
      <span id="pdp-price-discount">-55 € (-27%)</span>
      <a href="/other"><span class="variant-price">99,99 €</span></a>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/item")
    assert (result.status, result.price, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("149.99"),
        ExtractionMethod.DOM_HEURISTIC,
    )


def test_conflicting_jsonld_and_single_html_price_is_safe_ambiguity():
    html = """
      <script type="application/ld+json">{"@type":"Product","offers":{"price":"99","priceCurrency":"EUR"}}</script>
      <span class="current-price">89 EUR</span>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/item")
    assert result.status is ExtractionStatus.AMBIGUOUS
    assert result.diagnostics["reason"] == "conflicting structured sources"


def test_matching_jsonld_and_html_raise_confidence_but_range_is_not_exact_price():
    matching = """
      <script type="application/ld+json">{"@type":"Product","offers":{"price":"99","priceCurrency":"EUR"}}</script>
      <span class="current-price">99 EUR</span>
    """
    result = UniversalExtractor().extract(matching, "https://shop.test/item")
    assert result.status is ExtractionStatus.SUCCESS
    assert result.confidence_score == pytest.approx(0.98)
    price_range = '<span class="current-price">Desde 99 EUR - 129 EUR</span>'
    assert UniversalExtractor().extract(price_range).status is not ExtractionStatus.SUCCESS


@pytest.mark.parametrize(
    "fixture_name, expected_reason",
    [
        ("identity_conflict_html_unbound.html", "product variant identity mismatch"),
        ("identity_conflict_same_price.html", "product variant identity mismatch"),
    ],
    ids=["different-url-unbound-html", "same-number-different-sku"],
)
def test_jsonld_identity_conflict_blocks_unbound_html_price(fixture_name, expected_reason):
    html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    result = UniversalExtractor().extract(html, "https://shop.test/product/model-pro")
    assert result.status is not ExtractionStatus.SUCCESS
    assert result.price is None
    assert result.diagnostics["reason"] == expected_reason


def test_html_price_bound_to_selected_product_can_replace_rejected_jsonld_variant():
    html = (FIXTURES / "identity_conflict_html_bound.html").read_text(encoding="utf-8")
    result = UniversalExtractor().extract(html, "https://shop.test/product/model-pro")
    assert (result.status, result.price, result.currency, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("149"),
        "EUR",
        ExtractionMethod.DOM_HEURISTIC,
    )
    assert all(candidate["price"] != "99" for candidate in result.diagnostics["candidates"])


def test_html_price_without_jsonld_identity_stays_backward_compatible():
    html = """
      <script type="application/ld+json">{"@type":"Product","name":"Model Pro"}</script>
      <main><span class="current-price">149 EUR</span></main>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/product/model-pro")
    assert (result.status, result.price, result.currency, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("149"),
        "EUR",
        ExtractionMethod.DOM_HEURISTIC,
    )


def test_html_product_scope_excludes_nested_old_recommendation_unit_and_bundle_prices():
    html = (FIXTURES / "html_product_scope.html").read_text(encoding="utf-8")
    result = UniversalExtractor().extract(html, "https://shop.test/products/gps-watch")
    assert (result.status, result.price, result.currency, result.method) == (
        ExtractionStatus.SUCCESS,
        Decimal("154.99"),
        "EUR",
        ExtractionMethod.DOM_HEURISTIC,
    )
    assert result.diagnostics["jsonld_selection"] == "exact offer URL"


def test_multiple_unbound_variant_prices_remain_ambiguous():
    html = """
      <main class="variant-picker">
        <span class="current-price">99 EUR</span>
        <span class="current-price">129 EUR</span>
      </main>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/products/model")
    assert result.status is ExtractionStatus.AMBIGUOUS
    assert result.price is None


def test_conflicting_nested_dom_identity_cannot_override_mismatching_ancestor():
    html = """
      <script type="application/ld+json">
        {"@type":"Offer","url":"https://shop.test/products/selected"}
      </script>
      <section data-product-url="https://shop.test/products/other">
        <div data-product-url="https://shop.test/products/selected">
          <span class="current-price">99 EUR</span>
        </div>
      </section>
    """
    result = UniversalExtractor().extract(html, "https://shop.test/products/selected")
    assert result.status is ExtractionStatus.AMBIGUOUS
    assert result.price is None
    assert result.diagnostics["reason"] == "conflicting HTML product identity"
