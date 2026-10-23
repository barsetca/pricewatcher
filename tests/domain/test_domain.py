import re
import sqlite3
from pathlib import Path

from pricewatch.domain.enums import (
    AnomalyDecision,
    ExtractionMethod,
    ExtractionStatus,
    FetchMethod,
    FetchStatus,
    OfferStatus,
)
from pricewatch.domain.models import CheckAttempt, Offer


def test_nullable_models_and_row_conversion():
    offer = Offer(1, "u", "n", "t")
    assert offer.current_price_minor is None
    assert offer.status is OfferStatus.PENDING
    assert CheckAttempt(1, "t", FetchStatus.SUCCESS, ExtractionStatus.NOT_RUN).currency is None


def _constraint_values(schema: str, table: str, column: str) -> set[str]:
    table_body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema, re.DOTALL).group(1)
    values = re.search(rf"{column} TEXT .*?CHECK \({column} IN \(([^)]*)\)\)", table_body).group(1)
    return set(re.findall(r"'([^']+)'", values))


def test_enum_values_exactly_match_their_sqlite_check_constraints():
    schema = Path("src/pricewatch/database/schema.sql").read_text(encoding="utf-8")
    enum_constraints = {
        FetchStatus: ("check_attempts", "fetch_status"),
        ExtractionStatus: ("check_attempts", "extraction_status"),
        OfferStatus: ("offers", "status"),
        FetchMethod: ("offers", "preferred_fetch_method"),
        ExtractionMethod: ("check_attempts", "extraction_method"),
        AnomalyDecision: ("check_attempts", "anomaly_decision"),
    }
    for enum, (table, column) in enum_constraints.items():
        assert _constraint_values(schema, table, column) == {member.value for member in enum}


def test_nullable_enum_columns_do_not_turn_null_into_an_enum_value():
    assert Offer(1, "url", "normalized", "timestamp").preferred_fetch_method is None
    assert CheckAttempt(1, "timestamp", FetchStatus.SUCCESS, ExtractionStatus.NOT_RUN).anomaly_decision is None
