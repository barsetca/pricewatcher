from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.price_history_repository import PriceHistoryRepository
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
    CSV_DATABASE_ERROR_REASON,
    CSV_ROW_ERROR_REASON,
    CsvExportService,
    CsvImportCode,
    CsvImportService,
    CsvService,
)
from pricewatch.services.product_service import ProductService


def check_result(success: bool = True) -> PriceCheckResult:
    fetch = FetchResult(
        "https://shop.test/item",
        FetchMethod.REQUESTS,
        FetchStatus.SUCCESS,
        final_url="https://shop.test/item",
        http_status=200,
        html="<html>",
        elapsed_time_ms=3,
    )
    extraction = ExtractionResult(
        ExtractionStatus.SUCCESS if success else ExtractionStatus.PARTIAL,
        title="Store title",
        price=Decimal("19.99") if success else None,
        currency="EUR" if success else None,
        method=ExtractionMethod.JSON_LD,
    )
    return PriceCheckResult(fetch, extraction, (fetch,))


class Checker:
    def __init__(self, value=None):
        self.value = value or check_result()
        self.calls: list[str] = []

    def check(self, url: str) -> PriceCheckResult:
        self.calls.append(url)
        if isinstance(self.value, Exception):
            error, self.value = self.value, check_result()
            raise error
        return self.value


def safe_security() -> UrlSecurity:
    return UrlSecurity(
        lambda host, port: [
            (None, None, None, None, ("93.184.216.34", 0))
        ]
    )


@dataclass
class ImportEnvironment:
    connection: sqlite3.Connection
    products: ProductService
    checker: Checker
    parser: CsvService
    importer: CsvImportService
    user_id: int

    def parse(self, *rows: str):
        content = "product_name,url\n" + "\n".join(rows) + "\n"
        return self.parser.parse(content.encode(), "import.csv")


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "csv-import.db"
    initialize_database(path)
    connection = connect(path)
    checker = Checker()
    products = ProductService(connection, checker, safe_security())
    user = products.get_or_create_user(1001, "csv-user")
    value = ImportEnvironment(
        connection,
        products,
        checker,
        CsvService(),
        CsvImportService(products),
        user.id,
    )
    yield value
    connection.close()


def test_imports_new_product_offer_attempt_and_active_baseline(env):
    result = env.importer.import_preview(
        env.user_id, env.parse("Phone,https://shop.test/phone")
    )

    assert result.started and result.completed and not result.interrupted
    assert result.filename == "import.csv" and result.total_records == 1
    assert result.imported_count == 1
    row = result.records[0]
    assert row.code is CsvImportCode.IMPORTED
    assert row.product_id is not None and row.offer_id is not None
    assert row.offer_status is OfferStatus.ACTIVE
    assert env.products.get_product_details(env.user_id, row.product_id).product.name == "Phone"
    assert env.connection.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 1
    assert env.connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("stored_name", "csv_name"),
    [
        ("Phone", "Phone"),
        ("Phone", "pHONE"),
        ("Big Phone", "Big   Phone"),
        ("Big Phone", "Big\u00a0Phone"),
    ],
)
def test_import_reuses_one_product_by_d39_key(env, stored_name, csv_name):
    product = env.products.create_product(env.user_id, stored_name).product

    result = env.importer.import_preview(
        env.user_id, env.parse(f"{csv_name},https://shop.test/{product.id}")
    )

    assert result.records[0].code is CsvImportCode.IMPORTED
    assert result.records[0].product_id == product.id
    assert len(env.products.list_products(env.user_id)) == 1


def test_two_legacy_products_with_same_d39_key_are_ambiguous(env):
    env.connection.execute(
        "INSERT INTO products (user_id, name, created_at) VALUES (?, ?, ?)",
        (env.user_id, "Legacy  Phone", "2026-10-19T00:00:00+00:00"),
    )
    env.connection.execute(
        "INSERT INTO products (user_id, name, created_at) VALUES (?, ?, ?)",
        (env.user_id, "legacy phone", "2026-10-19T00:00:01+00:00"),
    )
    env.connection.commit()

    result = env.importer.import_preview(
        env.user_id, env.parse("LEGACY\u00a0PHONE,https://shop.test/legacy")
    )

    assert result.records[0].code is CsvImportCode.AMBIGUOUS
    assert env.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    assert [item.name for item in env.products.list_products(env.user_id)] == [
        "Legacy  Phone",
        "legacy phone",
    ]


def test_manual_create_and_rename_use_d39_without_migrating_old_rows(env):
    first = env.products.create_product(env.user_id, "  Big\t Phone  ")
    second = env.products.create_product(env.user_id, "Tablet")

    assert first.product.name == "Big Phone"
    assert env.products.create_product(env.user_id, "big   phone").code == "DUPLICATE"
    assert env.products.create_product(env.user_id, "STRASSE").code == "CREATED"
    assert env.products.create_product(env.user_id, "Straße").code == "DUPLICATE"
    calls_before = len(env.checker.calls)
    assert env.products.create_product_with_offer(
        env.user_id, "BIG PHONE", "https://shop.test/not-checked"
    ).code == "DUPLICATE"
    assert len(env.checker.calls) == calls_before
    assert env.products.rename_product(
        env.user_id, second.product.id, "BIG\u00a0PHONE"
    ).code == "DUPLICATE"
    assert env.products.get_product_details(env.user_id, second.product.id).product.name == "Tablet"


@pytest.mark.parametrize(
    ("second_url", "expected_calls"),
    [
        ("https://shop.test/same", 1),
        ("HTTPS://SHOP.test:443/same?utm_source=csv#part", 1),
    ],
)
def test_duplicate_urls_in_one_csv_are_owner_scoped_and_not_rechecked(
    env, second_url, expected_calls
):
    first_url = (
        "https://shop.test/same"
        if "utm_source" not in second_url
        else "https://shop.test/same"
    )
    result = env.importer.import_preview(
        env.user_id,
        env.parse(f"First,{first_url}", f"Second,{second_url}"),
    )

    assert [row.code for row in result.records] == [
        CsvImportCode.IMPORTED,
        CsvImportCode.DUPLICATE,
    ]
    assert len(env.checker.calls) == expected_calls
    assert len(env.products.list_products(env.user_id)) == 1


def test_existing_url_in_another_product_is_duplicate_without_http(env):
    first = env.products.create_product(env.user_id, "First").product
    saved = env.products.add_offer(env.user_id, first.id, "https://shop.test/existing")
    calls_before = len(env.checker.calls)

    result = env.importer.import_preview(
        env.user_id, env.parse("Different,https://shop.test/existing")
    )

    row = result.records[0]
    assert row.code is CsvImportCode.DUPLICATE
    assert (row.product_id, row.offer_id) == (first.id, saved.offer.id)
    assert len(env.checker.calls) == calls_before
    assert len(env.products.list_products(env.user_id)) == 1


def test_same_url_and_name_of_another_user_do_not_leak_or_block_import(env):
    other = env.products.get_or_create_user(2002)
    foreign = env.products.create_product(other.id, "Phone").product
    env.products.add_offer(other.id, foreign.id, "https://shop.test/shared")
    calls_before = len(env.checker.calls)

    result = env.importer.import_preview(
        env.user_id, env.parse("Phone,https://shop.test/shared")
    )

    row = result.records[0]
    assert row.code is CsvImportCode.IMPORTED
    assert row.product_id != foreign.id
    assert len(env.checker.calls) == calls_before + 1


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("not-a-url", CsvImportCode.INVALID),
        ("http://127.0.0.1/private", CsvImportCode.BLOCKED),
    ],
)
def test_invalid_and_security_blocked_urls_are_not_saved(env, url, code):
    result = env.importer.import_preview(env.user_id, env.parse(f"Unsafe,{url}"))

    assert result.records[0].code is code
    assert env.products.list_products(env.user_id) == ()
    assert env.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    assert env.checker.calls == []


def test_allowed_url_without_price_is_imported_pending(env):
    env.checker.value = check_result(False)

    result = env.importer.import_preview(
        env.user_id, env.parse("Pending,https://shop.test/pending")
    )

    row = result.records[0]
    assert row.code is CsvImportCode.IMPORTED
    assert row.offer_status is OfferStatus.PENDING
    assert PriceHistoryRepository(env.connection).list_for_offer(
        row.offer_id, env.user_id
    ) == []
    assert env.connection.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 1


def test_same_url_is_retried_after_row_exception_that_saved_nothing(env):
    env.checker.value = RuntimeError("temporary checker failure")
    preview = env.parse(
        "First,https://shop.test/retry",
        "Second,https://shop.test/retry",
    )

    result = env.importer.import_preview(env.user_id, preview)

    assert [row.code for row in result.records] == [
        CsvImportCode.ERROR,
        CsvImportCode.IMPORTED,
    ]
    assert len(env.checker.calls) == 2
    assert env.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1


def test_unexpected_row_error_uses_safe_report_reason_and_continues(
    env, caplog,
):
    secret = "https://private.test/item?token=raw-secret-value"
    env.checker.value = RuntimeError(secret)
    preview = env.parser.parse(
        b"product_name,url\n"
        b"Sensitive,https://shop.test/fail\n"
        b"Second,https://shop.test/second\n"
        b",https://shop.test/invalid\n",
        "private-name.csv",
    )
    caplog.set_level(logging.WARNING, logger="pricewatch.services.csv_service")

    result = env.importer.import_preview(
        env.user_id, preview, operation_id="safe-operation"
    )
    report = CsvExportService(env.products).export_import_report(result)
    report_text = report.content.decode()

    assert [row.code for row in result.records] == [
        CsvImportCode.ERROR,
        CsvImportCode.IMPORTED,
        CsvImportCode.INVALID,
    ]
    assert result.records[0].reason == CSV_ROW_ERROR_REASON
    assert result.records[2].reason == "product_name is empty"
    assert secret not in report_text
    assert CSV_ROW_ERROR_REASON in report_text
    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if "event=csv_row_failed" in record.getMessage()
    )
    assert diagnostic == (
        "event=csv_row_failed outcome=error operation_id=safe-operation "
        "row_number=2 exception_class=RuntimeError"
    )
    assert secret not in diagnostic
    assert "Sensitive" not in diagnostic and "shop.test" not in diagnostic


@pytest.mark.parametrize("repository", ["_offers", "_attempts", "_history"])
def test_row_write_failure_rolls_back_new_product_and_continues(
    env, monkeypatch, repository
):
    target = getattr(env.products, repository)
    original = target.create

    def fail_selected(value):
        if "fail" in getattr(value, "url", "") or (
            repository != "_offers"
            and env.connection.execute(
                "SELECT url FROM offers WHERE id = ?", (value.offer_id,)
            ).fetchone()[0].endswith("/fail")
        ):
            raise RuntimeError(f"{repository} failure")
        return original(value)

    monkeypatch.setattr(target, "create", fail_selected)
    result = env.importer.import_preview(
        env.user_id,
        env.parse(
            "First,https://shop.test/first",
            "Broken,https://shop.test/fail",
            "Third,https://shop.test/third",
        ),
    )

    assert [row.code for row in result.records] == [
        CsvImportCode.IMPORTED,
        CsvImportCode.ERROR,
        CsvImportCode.IMPORTED,
    ]
    assert [item.name for item in env.products.list_products(env.user_id)] == [
        "First",
        "Third",
    ]
    assert env.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 2


def test_sqlite_system_error_interrupts_and_marks_remaining_rows(
    env, monkeypatch, caplog,
):
    original = env.products._attempts.create

    def fail_second(attempt):
        url = env.connection.execute(
            "SELECT url FROM offers WHERE id = ?", (attempt.offer_id,)
        ).fetchone()[0]
        if url.endswith("/second"):
            raise sqlite3.OperationalError("database unavailable")
        return original(attempt)

    monkeypatch.setattr(env.products._attempts, "create", fail_second)
    caplog.set_level(logging.ERROR, logger="pricewatch.services.csv_service")
    result = env.importer.import_preview(
        env.user_id,
        env.parse(
            "First,https://shop.test/first",
            "Second,https://shop.test/second",
            "Third,https://shop.test/third",
        ),
        operation_id="sqlite-operation",
    )

    assert result.started and not result.completed and result.interrupted
    assert [row.code for row in result.records] == [
        CsvImportCode.IMPORTED,
        CsvImportCode.ERROR,
        CsvImportCode.NOT_PROCESSED,
    ]
    assert result.records[1].reason == CSV_DATABASE_ERROR_REASON
    assert "database unavailable" not in result.records[1].reason
    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if "event=csv_row_failed" in record.getMessage()
    )
    assert diagnostic == (
        "event=csv_row_failed outcome=error operation_id=sqlite-operation "
        "row_number=3 exception_class=OperationalError"
    )
    assert "database unavailable" not in diagnostic
    assert [item.name for item in env.products.list_products(env.user_id)] == ["First"]


def test_concurrent_duplicate_before_finalize_is_reported_without_empty_product(
    env, monkeypatch
):
    product = env.products.create_product(env.user_id, "Phone").product
    original_add = env.products.add_offer_with_preview

    def insert_first(user_id, product_id, preview):
        monkeypatch.setattr(env.products, "add_offer_with_preview", original_add)
        assert original_add(user_id, product_id, preview).code == "CREATED"
        return original_add(user_id, product_id, preview)

    monkeypatch.setattr(env.products, "add_offer_with_preview", insert_first)
    result = env.importer.import_preview(
        env.user_id, env.parse("Phone,https://shop.test/race")
    )

    assert result.records[0].code is CsvImportCode.DUPLICATE
    assert len(env.products.get_product_details(env.user_id, product.id).offers) == 1
    assert len(env.products.list_products(env.user_id)) == 1


def test_concurrent_product_appearance_reuses_preview_without_second_http(
    env, monkeypatch
):
    original_create = env.products.create_product_with_preview

    def insert_product_first(user_id, name, preview):
        monkeypatch.setattr(
            env.products, "create_product_with_preview", original_create
        )
        assert env.products.create_product(user_id, name).code == "CREATED"
        return original_create(user_id, name, preview)

    monkeypatch.setattr(
        env.products, "create_product_with_preview", insert_product_first
    )
    result = env.importer.import_preview(
        env.user_id, env.parse("Phone,https://shop.test/product-race")
    )

    assert result.records[0].code is CsvImportCode.IMPORTED
    assert len(env.checker.calls) == 1
    assert len(env.products.list_products(env.user_id)) == 1
    assert env.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1


def test_product_deleted_between_match_and_finalize_is_not_recreated(env, monkeypatch):
    product = env.products.create_product(env.user_id, "Phone").product
    original_add = env.products.add_offer_with_preview

    def delete_first(user_id, product_id, preview):
        env.products.delete_product(user_id, product_id)
        return original_add(user_id, product_id, preview)

    monkeypatch.setattr(env.products, "add_offer_with_preview", delete_first)
    result = env.importer.import_preview(
        env.user_id, env.parse("Phone,https://shop.test/deleted")
    )

    assert result.records[0].code is CsvImportCode.ERROR
    assert env.products.list_products(env.user_id) == ()
    assert env.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_invalid_rows_are_preserved_and_results_follow_physical_order(env):
    preview = env.parser.parse(
        b"product_name,url\nFirst,https://shop.test/first\n,https://shop.test/bad\nThird,not-a-url\n"
    )

    result = env.importer.import_preview(env.user_id, preview)

    assert [row.line_number for row in result.records] == [2, 3, 4]
    assert [row.code for row in result.records] == [
        CsvImportCode.IMPORTED,
        CsvImportCode.INVALID,
        CsvImportCode.INVALID,
    ]
    assert result.imported_count == 1 and result.invalid_count == 2


def test_import_result_preserves_source_values_while_storage_uses_d39(env):
    preview = env.parser.parse(
        b"product_name,url\n  Phone   Pro  ,  https://shop.test/source  \n"
    )

    result = env.importer.import_preview(env.user_id, preview)

    row = result.records[0]
    assert row.product_name == "  Phone   Pro  "
    assert row.url == "  https://shop.test/source  "
    assert env.products.list_products(env.user_id)[0].name == "Phone Pro"


def test_rejected_or_invalid_only_csv_makes_no_service_calls_or_changes(env):
    malformed = env.parser.parse(b'product_name,url\n"broken\n')
    invalid_only = env.parser.parse(b"product_name,url\n,https://shop.test/item\n")

    rejected = env.importer.import_preview(env.user_id, malformed)
    no_valid = env.importer.import_preview(env.user_id, invalid_only)

    assert not rejected.started and not rejected.completed
    assert not no_valid.started and not no_valid.completed
    assert no_valid.records[0].code is CsvImportCode.INVALID
    assert env.checker.calls == []
    assert env.products.list_products(env.user_id) == ()


def test_missing_user_is_not_created_and_valid_rows_are_not_processed(env):
    result = env.importer.import_preview(
        999999, env.parse("Phone,https://shop.test/item")
    )

    assert not result.started and not result.completed and not result.interrupted
    assert result.records[0].code is CsvImportCode.NOT_PROCESSED
    assert env.checker.calls == []
    assert env.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
