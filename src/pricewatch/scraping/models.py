"""Технические DTO scraping-слоя без бизнес-логики."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    method: FetchMethod
    status: FetchStatus
    final_url: str | None = None
    http_status: int | None = None
    html: str | None = None
    elapsed_time_ms: int = 0
    error_message: str | None = None


@dataclass(frozen=True)
class ExtractionResult:
    status: ExtractionStatus
    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    method: ExtractionMethod | None = None
    confidence_score: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceCheckResult:
    """Результат одной проверки; attempts сохраняет диагностику fallback без SQLite."""

    fetch: FetchResult
    extraction: ExtractionResult
    attempts: tuple[FetchResult, ...] = ()
