from pricewatch.database import connect, initialize_database
from pricewatch.domain.enums import ExtractionStatus, FetchMethod, FetchStatus
from pricewatch.scraping.models import FetchResult, PriceCheckResult
from pricewatch.scraping.universal_extractor import UniversalExtractor
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.monitoring_service import MonitoringService
from pricewatch.services.product_service import ProductService


SOURCE_URL = "https://shop.test/products/watch?utm_source=campaign"
NORMALIZED_URL = "https://shop.test/products/watch"
HTML = """
<html>
  <head>
    <script type="application/ld+json">
      {
        "@type": "Product",
        "url": "https://shop.test/products/watch",
        "name": "Test watch",
        "offers": {"@type": "Offer", "price": "138.49", "priceCurrency": "EUR"}
      }
    </script>
  </head>
  <body><main><span class="current-price">138.49 EUR</span></main></body>
</html>
"""


class HtmlChecker:
    def __init__(self):
        self.calls = []
        self.extractor = UniversalExtractor()

    def check(self, url):
        self.calls.append(url)
        fetch = FetchResult(
            url,
            FetchMethod.REQUESTS,
            FetchStatus.SUCCESS,
            final_url=url,
            http_status=200,
            html=HTML,
        )
        return PriceCheckResult(fetch, self.extractor.extract(HTML, url), (fetch,))


def test_initial_and_manual_checks_share_canonical_url_with_identity_sensitive_html(
    tmp_path,
):
    extractor = UniversalExtractor()
    assert extractor.extract(HTML, NORMALIZED_URL).status is ExtractionStatus.SUCCESS
    assert extractor.extract(HTML, SOURCE_URL).status is ExtractionStatus.AMBIGUOUS

    path = tmp_path / "canonical-url.sqlite"
    initialize_database(path)
    connection = connect(path)
    checker = HtmlChecker()
    security = UrlSecurity(
        lambda host, port: [
            (None, None, None, None, ("93.184.216.34", 0))
        ]
    )
    products = ProductService(connection, checker, security)
    try:
        user = products.get_or_create_user(9001)
        product = products.create_product(user.id, "Test watch").product

        initial = products.add_offer(user.id, product.id, SOURCE_URL)
        manual = MonitoringService(connection, checker).check_for_user(
            user.id, initial.offer.id,
        )
        stored = products.get_offer(user.id, initial.offer.id)

        assert initial.code == "CREATED"
        assert initial.check.extraction.status is ExtractionStatus.SUCCESS
        assert manual.code == "SUCCESS"
        assert checker.calls == [NORMALIZED_URL, NORMALIZED_URL]
        assert stored.url == SOURCE_URL
        assert stored.normalized_url == NORMALIZED_URL
    finally:
        connection.close()
