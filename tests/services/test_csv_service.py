from __future__ import annotations

import pytest

from pricewatch.services.csv_service import (
    MAX_CSV_SIZE_BYTES,
    CsvErrorCode,
    CsvRecordStatus,
    CsvService,
    normalize_product_name,
    product_name_comparison_key,
)


@pytest.fixture
def service() -> CsvService:
    return CsvService()


def test_parses_simple_csv_and_preserves_filename(service):
    result = service.parse(
        b"product_name,url\nPhone,https://example.com/phone\nTV,https://example.com/tv\n",
        "products.csv",
    )

    assert result.filename == "products.csv"
    assert result.total_records == 2
    assert result.valid_count == 2
    assert result.invalid_count == 0
    assert result.can_confirm is True
    assert [record.product_name for record in result.valid_records] == ["Phone", "TV"]
    assert all(record.status is CsvRecordStatus.VALID for record in result.valid_records)


@pytest.mark.parametrize(
    ("content", "expected_name", "expected_url"),
    [
        (
            "\ufeffproduct_name,url\nТелефон,https://example.com/телефон\n".encode(),
            "Телефон",
            "https://example.com/телефон",
        ),
        (
            b"product_name,url,note\nPhone,https://example.com,ignored\n",
            "Phone",
            "https://example.com",
        ),
        (
            b"url,product_name\nhttps://example.com,Phone\n",
            "Phone",
            "https://example.com",
        ),
        (
            b'product_name,url\n"Phone, blue",https://example.com\n',
            "Phone, blue",
            "https://example.com",
        ),
        (
            b'product_name,url\n"Phone ""Pro""",https://example.com\n',
            'Phone "Pro"',
            "https://example.com",
        ),
    ],
)
def test_supported_csv_variants(service, content, expected_name, expected_url):
    result = service.parse(content)

    assert result.structural_rejected is False
    assert result.valid_count == 1
    assert result.valid_records[0].product_name == expected_name
    assert result.valid_records[0].url == expected_url


def test_multiline_value_is_one_record_with_starting_physical_line(service):
    result = service.parse(
        b'product_name,url\n"Phone\nPro",https://example.com/one\nTV,https://example.com/two\n'
    )

    assert result.total_records == 2
    assert [record.line_number for record in result.valid_records] == [2, 4]
    assert result.valid_records[0].product_name == "Phone Pro"


def test_empty_physical_lines_are_ignored_and_do_not_consume_limit(service):
    records = "\n".join(f"P{i},https://example.com/{i}\n" for i in range(100))
    result = service.parse(f"\nproduct_name,url\n\n{records}".encode())

    assert result.total_records == 100
    assert result.valid_count == 100
    assert result.valid_records[0].line_number == 4
    assert result.structural_rejected is False


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", CsvErrorCode.MISSING_HEADER),
        (b"\n\r\n", CsvErrorCode.MISSING_HEADER),
        (b"url\nhttps://example.com\n", CsvErrorCode.MISSING_REQUIRED_HEADER),
        (b"product_name\nPhone\n", CsvErrorCode.MISSING_REQUIRED_HEADER),
        (b"product_name,url,product_name\nP,u,P\n", CsvErrorCode.DUPLICATE_HEADER),
        (b"product_name,url,note,note\nP,u,x,y\n", CsvErrorCode.DUPLICATE_HEADER),
        (b"product_name,,url\nP,x,u\n", CsvErrorCode.EMPTY_HEADER),
        (b"product_name,   ,url\nP,x,u\n", CsvErrorCode.EMPTY_HEADER),
        (b"product_name,url\n\xff\n", CsvErrorCode.INVALID_ENCODING),
        (b'product_name,url\n"Phone,https://example.com\n', CsvErrorCode.MALFORMED_CSV),
    ],
)
def test_structural_errors_reject_whole_file(service, content, code):
    result = service.parse(content)

    assert result.structural_rejected is True
    assert result.can_confirm is False
    assert result.valid_records == ()
    assert result.invalid_records == ()
    assert result.errors[0].code is code


def test_101st_nonempty_record_rejects_whole_file(service):
    rows = "".join(f"P{i},https://example.com/{i}\n" for i in range(101))
    result = service.parse(("product_name,url\n" + rows).encode())

    assert result.structural_rejected is True
    assert result.total_records == 101
    assert result.errors[0].code is CsvErrorCode.TOO_MANY_RECORDS
    assert result.errors[0].line_number == 102


def test_file_over_5_mib_rejects_before_decoding(service):
    result = service.parse(b"x" * (MAX_CSV_SIZE_BYTES + 1))

    assert result.structural_rejected is True
    assert result.errors[0].code is CsvErrorCode.FILE_TOO_LARGE


def test_valid_large_ignored_field_is_not_limited_by_csv_default(service):
    note = b"x" * 200_000
    result = service.parse(
        b"product_name,url,note\nPhone,https://example.com," + note + b"\n"
    )

    assert result.structural_rejected is False
    assert result.valid_count == 1


@pytest.mark.parametrize(
    ("row", "code", "product_name", "url"),
    [
        (",https://example.com", CsvErrorCode.EMPTY_PRODUCT_NAME, "", "https://example.com"),
        ("Phone,  ", CsvErrorCode.EMPTY_URL, "Phone", "  "),
        ("Phone", CsvErrorCode.FIELD_COUNT_MISMATCH, "Phone", None),
        ("Phone,https://example.com,extra", CsvErrorCode.FIELD_COUNT_MISMATCH, "Phone", "https://example.com"),
    ],
)
def test_invalid_record_keeps_available_input(service, row, code, product_name, url):
    result = service.parse(f"product_name,url\n{row}\n".encode())

    assert result.structural_rejected is False
    assert result.total_records == 1
    assert result.valid_count == 0
    assert result.invalid_count == 1
    assert result.can_confirm is False
    invalid = result.invalid_records[0]
    assert invalid.line_number == 2
    assert invalid.product_name == product_name
    assert invalid.url == url
    assert invalid.error.code is code
    assert invalid.status is CsvRecordStatus.INVALID
    assert invalid.reason == invalid.error.message


def test_valid_and_invalid_records_are_both_reported_in_source_order(service):
    result = service.parse(
        b"product_name,url\nFirst,https://example.com/1\n,https://example.com/bad\nThird,https://example.com/3\n"
    )

    assert result.total_records == 3
    assert [record.line_number for record in result.valid_records] == [2, 4]
    assert [record.line_number for record in result.invalid_records] == [3]
    assert [error.line_number for error in result.errors] == [3]
    assert result.can_confirm is True


@pytest.mark.parametrize(
    ("value", "normalized", "key"),
    [
        ("  Samsung Galaxy A56  ", "Samsung Galaxy A56", "samsung galaxy a56"),
        ("Samsung   Galaxy A56", "Samsung Galaxy A56", "samsung galaxy a56"),
        ("Samsung\tGalaxy A56", "Samsung Galaxy A56", "samsung galaxy a56"),
        ("SAMSUNG galaxy A56", "SAMSUNG galaxy A56", "samsung galaxy a56"),
    ],
)
def test_d39_product_name_normalization(value, normalized, key):
    assert normalize_product_name(value) == normalized
    assert product_name_comparison_key(value) == key


def test_product_name_normalization_does_not_use_fuzzy_matching():
    assert product_name_comparison_key("iPhone 15") != product_name_comparison_key("iPhone 16")


def test_parse_normalizes_product_name_and_trims_url(service):
    result = service.parse(b"product_name,url\n  Samsung   Galaxy A56  ,  https://example.com/a56  \n")

    record = result.valid_records[0]
    assert record.product_name == "Samsung Galaxy A56"
    assert record.product_name_key == "samsung galaxy a56"
    assert record.url == "https://example.com/a56"


def test_repeated_parse_is_deterministic(service):
    content = b"product_name,url\nPhone,https://example.com\n"

    assert service.parse(content, "same.csv") == service.parse(content, "same.csv")


def test_parse_does_not_call_network_or_database(service, monkeypatch):
    import socket
    import sqlite3

    def fail(*args, **kwargs):
        raise AssertionError("external operation was attempted")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(sqlite3, "connect", fail)

    result = service.parse(b"product_name,url\nPhone,https://example.com\n")

    assert result.valid_count == 1


def test_non_bytes_input_is_rejected_explicitly(service):
    with pytest.raises(TypeError, match="must be bytes"):
        service.parse("product_name,url\n")  # type: ignore[arg-type]
