from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.domain.enums import (
    ExtractionMethod,
    ExtractionStatus,
    FetchMethod,
    FetchStatus,
    OfferStatus,
)
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.csv_service import (
    CsvExportService,
    CsvImportCode,
    CsvImportRecordResult,
    CsvImportResult,
    CsvImportService,
    CsvService,
)
from pricewatch.services.product_service import ProductService


class Checker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def check(self, url: str) -> PriceCheckResult:
        self.calls.append(url)
        fetch = FetchResult(
            url,
            FetchMethod.REQUESTS,
            FetchStatus.SUCCESS,
            final_url=url,
            http_status=200,
            html="<html>",
        )
        extraction = ExtractionResult(
            ExtractionStatus.SUCCESS,
            title="Store title",
            price=Decimal("19.99"),
            currency="EUR",
            method=ExtractionMethod.JSON_LD,
        )
        return PriceCheckResult(fetch, extraction, (fetch,))


def safe_security() -> UrlSecurity:
    return UrlSecurity(
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
    )


@dataclass
class ExportEnvironment:
    connection: sqlite3.Connection
    products: ProductService
    checker: Checker
    exporter: CsvExportService
    user_id: int


@pytest.fixture
def env(tmp_path) -> ExportEnvironment:
    path = tmp_path / "csv-export.db"
    initialize_database(path)
    connection = connect(path)
    checker = Checker()
    products = ProductService(connection, checker, safe_security())
    user = products.get_or_create_user(3001, "csv-export-user")
    value = ExportEnvironment(connection, products, checker, CsvExportService(products), user.id)
    yield value
    connection.close()


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8"), newline="")))


def _add_offer(env: ExportEnvironment, product_name: str, url: str):
    product = env.products.create_product(env.user_id, product_name).product
    return env.products.add_offer(env.user_id, product.id, url).offer


def test_export_contains_all_offers_in_deterministic_order_and_standard_csv(env):
    beta = _add_offer(env, "Бета", "https://shop.test/beta")
    alpha_z = _add_offer(env, "Alpha", "https://shop.test/z")
    alpha_a = env.products.add_offer(
        env.user_id,
        alpha_z.product_id,
        "https://shop.test/a",
    ).offer
    env.connection.execute(
        "UPDATE offers SET store_name = ?, last_checked_at = ?, is_active = 0 WHERE id = ?",
        ('Store, "A"\nТест', "2026-10-19T12:00:00+00:00", alpha_a.id),
    )
    env.connection.execute(
        "UPDATE offers SET store_name = ?, current_price_minor = NULL, currency = 'EUR', "
        "last_checked_at = NULL, status = ? WHERE id = ?",
        ("Z-shop", OfferStatus.PENDING.value, alpha_z.id),
    )
    env.connection.execute(
        "UPDATE offers SET store_name = ?, current_price_minor = ?, currency = ?, "
        "last_checked_at = NULL, status = ? WHERE id = ?",
        ("B-shop", 123456, "EUR", OfferStatus.ERROR.value, beta.id),
    )
    env.connection.commit()
    changes_before = env.connection.total_changes
    calls_before = list(env.checker.calls)

    result = env.exporter.export_products(env.user_id)

    assert result.has_data and result.count == 3
    assert result.suggested_filename == "products.csv"
    assert not result.content.startswith(b"\xef\xbb\xbf")
    assert env.connection.total_changes == changes_before
    assert env.checker.calls == calls_before
    assert _rows(result.content) == [
        ["product_name", "store", "url", "current_price", "currency", "last_checked", "status"],
        ["Alpha", 'Store, "A"\nТест', "https://shop.test/a", "19.99", "EUR", "2026-10-19T12:00:00+00:00", "ACTIVE"],
        ["Alpha", "Z-shop", "https://shop.test/z", "", "", "", "PENDING"],
        ["Бета", "B-shop", "https://shop.test/beta", "1234.56", "EUR", "", "ERROR"],
    ]


def test_export_empty_user_data_and_unknown_user_do_not_create_file(env):
    product = env.products.create_product(env.user_id, "Без предложений").product
    assert product.id is not None

    empty = env.exporter.export_products(env.user_id)
    missing = env.exporter.export_products(env.user_id + 1)

    assert (empty.content, empty.has_data, empty.count, empty.message) == (
        b"", False, 0, "Нет товаров для экспорта"
    )
    assert (missing.content, missing.has_data, missing.count, missing.message) == (
        b"", False, 0, "Пользователь не найден"
    )


def test_export_propagates_sqlite_read_error(env):
    """Сбой SQLite при чтении не должен превращаться в частичный CSV."""
    env.connection.close()

    with pytest.raises(sqlite3.ProgrammingError):
        env.exporter.export_products(env.user_id)


def test_product_export_is_accepted_by_parser_and_reimport_is_duplicate(env):
    _add_offer(env, "Phone", "https://shop.test/phone")
    exported = env.exporter.export_products(env.user_id)

    preview = CsvService().parse(exported.content, "products.csv")
    calls_before = list(env.checker.calls)
    reimported = CsvImportService(env.products).import_preview(env.user_id, preview)

    assert preview.structural_rejected is False
    assert preview.valid_count == 1
    assert reimported.records[0].code is CsvImportCode.DUPLICATE
    assert env.checker.calls == calls_before


def test_export_import_report_preserves_source_order_and_all_fields():
    result = CsvImportResult(
        filename="source.csv",
        total_records=7,
        records=(
            CsvImportRecordResult(9, "Third", "https://shop.test/3", CsvImportCode.NOT_PROCESSED, "stopped"),
            CsvImportRecordResult(2, "Первый", "https://shop.test/1", CsvImportCode.IMPORTED, "", offer_status=OfferStatus.ACTIVE),
            CsvImportRecordResult(4, None, None, CsvImportCode.INVALID, "name, with\nnewline"),
            CsvImportRecordResult(5, "Duplicate", "https://shop.test/5", CsvImportCode.DUPLICATE, offer_status=OfferStatus.PENDING),
            CsvImportRecordResult(6, "Blocked", "https://shop.test/6", CsvImportCode.BLOCKED, "unsafe URL"),
            CsvImportRecordResult(7, "Ambiguous", "https://shop.test/7", CsvImportCode.AMBIGUOUS, "two products"),
            CsvImportRecordResult(8, "Error", "https://shop.test/8", CsvImportCode.ERROR, "database error"),
        ),
        started=True,
    )
    original = result
    report = CsvExportService(object()).export_import_report(result)  # type: ignore[arg-type]

    assert result == original
    assert report.suggested_filename == "import_report.csv"
    assert report.row_count == 7
    assert not report.content.startswith(b"\xef\xbb\xbf")
    assert _rows(report.content) == [
        ["row_number", "product_name", "url", "result", "offer_status", "reason"],
        ["9", "Third", "https://shop.test/3", "NOT_PROCESSED", "", "stopped"],
        ["2", "Первый", "https://shop.test/1", "IMPORTED", "ACTIVE", ""],
        ["4", "", "", "INVALID", "", "name, with\nnewline"],
        ["5", "Duplicate", "https://shop.test/5", "DUPLICATE", "PENDING", ""],
        ["6", "Blocked", "https://shop.test/6", "BLOCKED", "", "unsafe URL"],
        ["7", "Ambiguous", "https://shop.test/7", "AMBIGUOUS", "", "two products"],
        ["8", "Error", "https://shop.test/8", "ERROR", "", "database error"],
    ]


def test_empty_import_report_contains_header():
    report = CsvExportService(object()).export_import_report(CsvImportResult(None, 0))  # type: ignore[arg-type]

    assert report.row_count == 0
    assert _rows(report.content) == [
        ["row_number", "product_name", "url", "result", "offer_status", "reason"]
    ]
