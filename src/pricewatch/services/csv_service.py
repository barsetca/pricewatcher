"""CSV-preview, импорт, экспорт текущих Offers и отчёт импорта."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from pricewatch.domain.enums import OfferStatus
from pricewatch.services.product_names import (
    normalize_product_name,
    product_name_comparison_key,
)

if TYPE_CHECKING:
    from pricewatch.services.product_service import ProductService


MAX_CSV_SIZE_BYTES = 5 * 1024 * 1024
MAX_CSV_RECORDS = 100
REQUIRED_HEADERS = ("product_name", "url")
_CSV_LIMIT_LOCK = threading.Lock()
LOG = logging.getLogger(__name__)
CSV_DATABASE_ERROR_REASON = "import interrupted by a database error"
CSV_ROW_ERROR_REASON = "unexpected row processing error"


class CsvErrorCode(str, Enum):
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_ENCODING = "INVALID_ENCODING"
    MISSING_HEADER = "MISSING_HEADER"
    MISSING_REQUIRED_HEADER = "MISSING_REQUIRED_HEADER"
    DUPLICATE_HEADER = "DUPLICATE_HEADER"
    EMPTY_HEADER = "EMPTY_HEADER"
    MALFORMED_CSV = "MALFORMED_CSV"
    TOO_MANY_RECORDS = "TOO_MANY_RECORDS"
    FIELD_COUNT_MISMATCH = "FIELD_COUNT_MISMATCH"
    EMPTY_PRODUCT_NAME = "EMPTY_PRODUCT_NAME"
    EMPTY_URL = "EMPTY_URL"


class CsvRecordStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True)
class CsvValidationError:
    code: CsvErrorCode
    message: str
    line_number: int | None = None


@dataclass(frozen=True)
class CsvValidRecord:
    line_number: int
    product_name: str
    url: str
    product_name_key: str
    source_product_name: str | None = None
    source_url: str | None = None

    @property
    def status(self) -> CsvRecordStatus:
        return CsvRecordStatus.VALID


@dataclass(frozen=True)
class CsvInvalidRecord:
    line_number: int
    product_name: str | None
    url: str | None
    values: tuple[str, ...]
    error: CsvValidationError

    @property
    def status(self) -> CsvRecordStatus:
        return CsvRecordStatus.INVALID

    @property
    def reason(self) -> str:
        return self.error.message


@dataclass(frozen=True)
class CsvParseResult:
    filename: str | None
    total_records: int
    valid_records: tuple[CsvValidRecord, ...] = ()
    invalid_records: tuple[CsvInvalidRecord, ...] = ()
    errors: tuple[CsvValidationError, ...] = ()
    structural_rejected: bool = False

    @property
    def valid_count(self) -> int:
        return len(self.valid_records)

    @property
    def invalid_count(self) -> int:
        return len(self.invalid_records)

    @property
    def can_confirm(self) -> bool:
        return not self.structural_rejected and bool(self.valid_records)


class CsvImportCode(str, Enum):
    IMPORTED = "IMPORTED"
    DUPLICATE = "DUPLICATE"
    BLOCKED = "BLOCKED"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"
    ERROR = "ERROR"
    NOT_PROCESSED = "NOT_PROCESSED"


@dataclass(frozen=True)
class CsvImportRecordResult:
    line_number: int
    product_name: str | None
    url: str | None
    code: CsvImportCode
    reason: str = ""
    product_id: int | None = None
    offer_id: int | None = None
    offer_status: OfferStatus | None = None


@dataclass(frozen=True)
class CsvImportResult:
    filename: str | None
    total_records: int
    records: tuple[CsvImportRecordResult, ...] = ()
    started: bool = False
    completed: bool = False
    interrupted: bool = False
    message: str = ""

    def count(self, code: CsvImportCode) -> int:
        return sum(record.code is code for record in self.records)

    @property
    def imported_count(self) -> int:
        return self.count(CsvImportCode.IMPORTED)

    @property
    def duplicate_count(self) -> int:
        return self.count(CsvImportCode.DUPLICATE)

    @property
    def blocked_count(self) -> int:
        return self.count(CsvImportCode.BLOCKED)

    @property
    def ambiguous_count(self) -> int:
        return self.count(CsvImportCode.AMBIGUOUS)

    @property
    def invalid_count(self) -> int:
        return self.count(CsvImportCode.INVALID)

    @property
    def error_count(self) -> int:
        return self.count(CsvImportCode.ERROR)

    @property
    def not_processed_count(self) -> int:
        return self.count(CsvImportCode.NOT_PROCESSED)


@dataclass(frozen=True)
class CsvExportResult:
    """Результат экспорта текущего состояния Offers одного пользователя."""

    content: bytes = b""
    suggested_filename: str = "products.csv"
    count: int = 0
    has_data: bool = False
    message: str = ""


@dataclass(frozen=True)
class CsvReportResult:
    """Готовый CSV-отчёт по уже выполненному импорту."""

    content: bytes
    suggested_filename: str = "import_report.csv"
    row_count: int = 0


class CsvService:
    """Сформировать preview импорта без обращений к сети, БД или Telegram."""

    def parse(self, content: bytes, filename: str | None = None) -> CsvParseResult:
        if not isinstance(content, bytes):
            raise TypeError("CSV content must be bytes")
        if len(content) > MAX_CSV_SIZE_BYTES:
            return _structural_result(
                filename,
                CsvErrorCode.FILE_TOO_LARGE,
                f"CSV file exceeds {MAX_CSV_SIZE_BYTES} bytes",
            )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _structural_result(
                filename,
                CsvErrorCode.INVALID_ENCODING,
                "CSV file is not valid UTF-8",
            )

        # csv.field_size_limit действует на весь процесс. Lock защищает временное
        # изменение, которое позволяет читать поля до лимита файла, после чего
        # прежнее состояние интерпретатора восстанавливается.
        with _CSV_LIMIT_LOCK:
            previous_limit = csv.field_size_limit()
            try:
                if previous_limit < MAX_CSV_SIZE_BYTES:
                    csv.field_size_limit(MAX_CSV_SIZE_BYTES)
                return _parse_text(text, filename)
            finally:
                if csv.field_size_limit() != previous_limit:
                    csv.field_size_limit(previous_limit)


class CsvImportService:
    """Последовательно импортировать preview через публичные ProductService use cases."""

    def __init__(self, product_service: ProductService) -> None:
        self._products = product_service

    def import_preview(
        self,
        user_id: int,
        preview: CsvParseResult,
        operation_id: str | None = None,
    ) -> CsvImportResult:
        invalid = {
            record.line_number: CsvImportRecordResult(
                record.line_number,
                record.product_name,
                record.url,
                CsvImportCode.INVALID,
                record.reason,
            )
            for record in preview.invalid_records
        }
        if preview.structural_rejected:
            reason = preview.errors[0].message if preview.errors else "CSV is structurally rejected"
            return _import_result(preview, invalid.values(), message=reason)
        if not preview.can_confirm:
            return _import_result(
                preview,
                invalid.values(),
                message="CSV has no valid records to import",
            )
        try:
            user_exists = self._products.get_user(user_id) is not None
        except sqlite3.Error as error:
            reason = CSV_DATABASE_ERROR_REASON
            _log_csv_import_failure(
                "user_lookup", operation_id, error, level=logging.ERROR
            )
            rows = list(invalid.values())
            rows.extend(_not_processed(record, reason) for record in preview.valid_records)
            return _import_result(
                preview,
                rows,
                interrupted=True,
                message=reason,
            )
        if not user_exists:
            rows = list(invalid.values())
            rows.extend(_not_processed(record, "user not found") for record in preview.valid_records)
            return _import_result(preview, rows, message="user not found")

        rows = list(invalid.values())
        interrupted = False
        interruption_reason = ""
        for index, record in enumerate(preview.valid_records):
            try:
                if not self._products.get_user(user_id):
                    interrupted = True
                    interruption_reason = "user no longer exists"
                    rows.extend(
                        _not_processed(item, interruption_reason)
                        for item in preview.valid_records[index:]
                    )
                    break
                rows.append(self._import_record(user_id, record))
            except sqlite3.Error as error:
                interrupted = True
                interruption_reason = CSV_DATABASE_ERROR_REASON
                _log_csv_row_failure(
                    operation_id, record.line_number, error, level=logging.ERROR
                )
                rows.append(_error_record(record, interruption_reason))
                rows.extend(
                    _not_processed(item, "import interrupted after database error")
                    for item in preview.valid_records[index + 1 :]
                )
                break
            except Exception as error:
                _log_csv_row_failure(
                    operation_id, record.line_number, error, level=logging.WARNING
                )
                rows.append(_error_record(record, CSV_ROW_ERROR_REASON))

        return _import_result(
            preview,
            rows,
            started=True,
            completed=not interrupted,
            interrupted=interrupted,
            message=interruption_reason,
        )

    def _import_record(self, user_id: int, record: CsvValidRecord) -> CsvImportRecordResult:
        offer_preview = self._products.preview_offer(user_id, record.url)
        if offer_preview.code != "READY":
            return _preview_import_result(record, offer_preview)

        matches = self._products.find_products_by_name(user_id, record.product_name)
        if len(matches) > 1:
            return _record_result(
                record,
                CsvImportCode.AMBIGUOUS,
                "multiple Products have the same normalized name",
            )
        if matches:
            operation = self._products.add_offer_with_preview(
                user_id, matches[0].id, offer_preview
            )
        else:
            operation = self._products.create_product_with_preview(
                user_id, record.product_name, offer_preview
            )
            if (
                operation.code == "DUPLICATE"
                and operation.duplicate_offer is None
            ):
                # Product мог конкурентно появиться после поиска. Повторно
                # используем уже выполненный preview без второго HTTP-запроса.
                matches = self._products.find_products_by_name(
                    user_id, record.product_name
                )
                if len(matches) > 1:
                    return _record_result(
                        record,
                        CsvImportCode.AMBIGUOUS,
                        "multiple Products have the same normalized name",
                    )
                if len(matches) == 1:
                    operation = self._products.add_offer_with_preview(
                        user_id, matches[0].id, offer_preview
                    )
        return _operation_import_result(record, operation)


class CsvExportService:
    """Сформировать CSV только из уже сохранённых данных без side effects."""

    _PRODUCT_HEADERS = (
        "product_name",
        "store",
        "url",
        "current_price",
        "currency",
        "last_checked",
        "status",
    )
    _REPORT_HEADERS = (
        "row_number",
        "product_name",
        "url",
        "result",
        "offer_status",
        "reason",
    )

    def __init__(self, product_service: ProductService) -> None:
        self._products = product_service

    def export_products(self, user_id: int) -> CsvExportResult:
        """Экспортировать все Offers пользователя, включая неактивные и PENDING."""
        if self._products.get_user(user_id) is None:
            return CsvExportResult(message="Пользователь не найден")

        rows: list[tuple[str, str, str, str, str, str, str, int]] = []
        for product in self._products.list_products(user_id):
            details = self._products.get_product_details(user_id, product.id)
            if details is None:
                continue
            for offer in details.offers:
                price, currency = _export_price(offer.current_price_minor, offer.currency)
                rows.append(
                    (
                        product.name,
                        offer.store_name or "",
                        offer.url,
                        price,
                        currency,
                        offer.last_checked_at or "",
                        offer.status.value,
                        offer.id if offer.id is not None else -1,
                    )
                )

        rows.sort(key=lambda row: (row[0], row[1], row[2], row[7]))
        if not rows:
            return CsvExportResult(message="Нет товаров для экспорта")

        text = io.StringIO(newline="")
        writer = csv.writer(text, lineterminator="\n")
        writer.writerow(self._PRODUCT_HEADERS)
        writer.writerows(row[:-1] for row in rows)
        return CsvExportResult(
            content=text.getvalue().encode("utf-8"),
            count=len(rows),
            has_data=True,
        )

    def export_import_report(self, result: CsvImportResult) -> CsvReportResult:
        """Сформировать отчёт без изменения результата и без повторного импорта."""
        text = io.StringIO(newline="")
        writer = csv.writer(text, lineterminator="\n")
        writer.writerow(self._REPORT_HEADERS)
        for record in result.records:
            writer.writerow(
                (
                    record.line_number,
                    record.product_name or "",
                    record.url or "",
                    record.code.value,
                    record.offer_status.value if record.offer_status else "",
                    record.reason,
                )
            )
        return CsvReportResult(
            content=text.getvalue().encode("utf-8"),
            row_count=len(result.records),
        )


def _parse_text(text: str, filename: str | None) -> CsvParseResult:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",", strict=True)
    header_with_line = _next_nonempty(reader)
    if isinstance(header_with_line, CsvValidationError):
        return _structural_result_from_error(filename, 0, header_with_line)
    if header_with_line is None:
        return _structural_result(
            filename,
            CsvErrorCode.MISSING_HEADER,
            "CSV header is missing",
        )

    header, header_line = header_with_line
    header_error = _validate_header(header, header_line)
    if header_error:
        return _structural_result_from_error(filename, 0, header_error)

    product_index = header.index("product_name")
    url_index = header.index("url")
    parsed_rows: list[tuple[list[str], int]] = []
    while True:
        start_line = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as error:
            malformed = CsvValidationError(
                CsvErrorCode.MALFORMED_CSV,
                f"Malformed CSV: {error}",
                start_line,
            )
            return _structural_result_from_error(filename, len(parsed_rows), malformed)
        if not row:
            continue
        parsed_rows.append((row, start_line))
        if len(parsed_rows) > MAX_CSV_RECORDS:
            too_many = CsvValidationError(
                CsvErrorCode.TOO_MANY_RECORDS,
                f"CSV contains more than {MAX_CSV_RECORDS} non-empty records",
                start_line,
            )
            return _structural_result_from_error(filename, len(parsed_rows), too_many)

    valid_records: list[CsvValidRecord] = []
    invalid_records: list[CsvInvalidRecord] = []
    errors: list[CsvValidationError] = []
    for row, line_number in parsed_rows:
        product_name = row[product_index] if product_index < len(row) else None
        url = row[url_index] if url_index < len(row) else None
        error = _validate_record(row, len(header), product_name, url, line_number)
        if error:
            errors.append(error)
            invalid_records.append(
                CsvInvalidRecord(
                    line_number,
                    product_name,
                    url,
                    tuple(row),
                    error,
                )
            )
            continue
        normalized_name = normalize_product_name(product_name)
        valid_records.append(
            CsvValidRecord(
                line_number,
                normalized_name,
                url.strip(),
                product_name_comparison_key(normalized_name),
                product_name,
                url,
            )
        )

    return CsvParseResult(
        filename=filename,
        total_records=len(parsed_rows),
        valid_records=tuple(valid_records),
        invalid_records=tuple(invalid_records),
        errors=tuple(errors),
    )


def _next_nonempty(
    reader: csv.reader,
) -> tuple[list[str], int] | CsvValidationError | None:
    while True:
        start_line = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            return None
        except csv.Error as error:
            return CsvValidationError(
                CsvErrorCode.MALFORMED_CSV,
                f"Malformed CSV: {error}",
                start_line,
            )
        if row:
            return row, start_line


def _validate_header(header: list[str], line_number: int) -> CsvValidationError | None:
    if any(not value.strip() for value in header):
        return CsvValidationError(
            CsvErrorCode.EMPTY_HEADER,
            "CSV header contains an empty column name",
            line_number,
        )
    seen: set[str] = set()
    for value in header:
        if value in seen:
            return CsvValidationError(
                CsvErrorCode.DUPLICATE_HEADER,
                f"CSV header is duplicated: {value}",
                line_number,
            )
        seen.add(value)
    missing = [value for value in REQUIRED_HEADERS if value not in seen]
    if missing:
        return CsvValidationError(
            CsvErrorCode.MISSING_REQUIRED_HEADER,
            f"CSV header is missing required column(s): {', '.join(missing)}",
            line_number,
        )
    return None


def _validate_record(
    row: list[str],
    header_length: int,
    product_name: str | None,
    url: str | None,
    line_number: int,
) -> CsvValidationError | None:
    if len(row) != header_length:
        return CsvValidationError(
            CsvErrorCode.FIELD_COUNT_MISMATCH,
            f"Record has {len(row)} fields; expected {header_length}",
            line_number,
        )
    if not product_name or not product_name.strip():
        return CsvValidationError(
            CsvErrorCode.EMPTY_PRODUCT_NAME,
            "product_name is empty",
            line_number,
        )
    if not url or not url.strip():
        return CsvValidationError(CsvErrorCode.EMPTY_URL, "url is empty", line_number)
    return None


def _structural_result(
    filename: str | None,
    code: CsvErrorCode,
    message: str,
    line_number: int | None = None,
) -> CsvParseResult:
    return _structural_result_from_error(
        filename,
        0,
        CsvValidationError(code, message, line_number),
    )


def _structural_result_from_error(
    filename: str | None,
    total_records: int,
    error: CsvValidationError,
) -> CsvParseResult:
    return CsvParseResult(
        filename=filename,
        total_records=total_records,
        errors=(error,),
        structural_rejected=True,
    )


def _preview_import_result(record: CsvValidRecord, offer_preview) -> CsvImportRecordResult:
    code = {
        "INVALID": CsvImportCode.INVALID,
        "BLOCKED": CsvImportCode.BLOCKED,
        "DUPLICATE": CsvImportCode.DUPLICATE,
    }.get(offer_preview.code, CsvImportCode.ERROR)
    duplicate = offer_preview.duplicate_offer
    return _record_result(
        record,
        code,
        offer_preview.message,
        product_id=duplicate.product_id if duplicate else None,
        offer_id=duplicate.id if duplicate else None,
        offer_status=duplicate.status if duplicate else None,
    )


def _operation_import_result(record: CsvValidRecord, operation) -> CsvImportRecordResult:
    if operation.code == "CREATED" and operation.offer:
        product_id = operation.product.id if operation.product else operation.offer.product_id
        return _record_result(
            record,
            CsvImportCode.IMPORTED,
            operation.message,
            product_id=product_id,
            offer_id=operation.offer.id,
            offer_status=operation.offer.status,
        )
    if operation.code == "DUPLICATE":
        duplicate = operation.duplicate_offer
        return _record_result(
            record,
            CsvImportCode.DUPLICATE,
            operation.message,
            product_id=duplicate.product_id if duplicate else None,
            offer_id=duplicate.id if duplicate else None,
            offer_status=duplicate.status if duplicate else None,
        )
    code = {
        "INVALID": CsvImportCode.INVALID,
        "BLOCKED": CsvImportCode.BLOCKED,
    }.get(operation.code, CsvImportCode.ERROR)
    product_id = operation.product.id if operation.product else None
    offer_id = operation.offer.id if operation.offer else None
    offer_status = operation.offer.status if operation.offer else None
    reason = operation.message or f"ProductService returned {operation.code}"
    return _record_result(
        record,
        code,
        reason,
        product_id=product_id,
        offer_id=offer_id,
        offer_status=offer_status,
    )


def _record_result(
    record: CsvValidRecord,
    code: CsvImportCode,
    reason: str = "",
    *,
    product_id: int | None = None,
    offer_id: int | None = None,
    offer_status: OfferStatus | None = None,
) -> CsvImportRecordResult:
    return CsvImportRecordResult(
        line_number=record.line_number,
        product_name=record.source_product_name or record.product_name,
        url=record.source_url or record.url,
        code=code,
        reason=reason,
        product_id=product_id,
        offer_id=offer_id,
        offer_status=offer_status,
    )


def _error_record(record: CsvValidRecord, reason: str) -> CsvImportRecordResult:
    return _record_result(record, CsvImportCode.ERROR, reason)


def _not_processed(record: CsvValidRecord, reason: str) -> CsvImportRecordResult:
    return _record_result(record, CsvImportCode.NOT_PROCESSED, reason)


def _log_csv_row_failure(operation_id, row_number, error, *, level):
    LOG.log(
        level,
        "event=csv_row_failed outcome=error operation_id=%s row_number=%s "
        "exception_class=%s",
        operation_id or "unavailable",
        row_number,
        type(error).__name__,
    )


def _log_csv_import_failure(action, operation_id, error, *, level):
    LOG.log(
        level,
        "event=csv_import_failed action=%s outcome=error operation_id=%s "
        "exception_class=%s",
        action,
        operation_id or "unavailable",
        type(error).__name__,
    )


def _import_result(
    preview: CsvParseResult,
    records: Iterable[CsvImportRecordResult],
    *,
    started: bool = False,
    completed: bool = False,
    interrupted: bool = False,
    message: str = "",
) -> CsvImportResult:
    return CsvImportResult(
        filename=preview.filename,
        total_records=preview.total_records,
        records=tuple(sorted(records, key=lambda record: record.line_number)),
        started=started,
        completed=completed,
        interrupted=interrupted,
        message=message,
    )


def _export_price(
    current_price_minor: int | None, currency: str | None
) -> tuple[str, str]:
    """Вернуть денежные поля по действующей модели хранения в центах.

    ProductService сохраняет подтверждённые значения как Decimal * 100 для
    любой поддерживаемой сейчас валюты. В модели пока нет справочника
    currency exponent, поэтому экспорт следует этому уже принятому договору,
    а не вводит собственные правила разрядности валют.
    """
    if current_price_minor is None:
        return "", ""
    price = Decimal(current_price_minor) / Decimal("100")
    return format(price, ".2f"), (currency or "")
