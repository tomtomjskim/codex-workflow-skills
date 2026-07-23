"""Canonical typed telemetry summaries for the Phase A experiment."""

from collections.abc import Mapping as ABCMapping
from dataclasses import dataclass
import hashlib
import re
from typing import Mapping
import unicodedata

from scripts.workflow_coordination.canonical_json import canonical_bytes


class TelemetryError(ValueError):
    """Raised when a telemetry summary or price snapshot is invalid."""


_ERROR = "telemetry_summary_invalid"
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_SUMMARY_KEYS = frozenset(
    {
        "classification",
        "response_digest",
        "usage",
        "event_count",
        "raw_retention",
    }
)
_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_reported_tokens",
        "estimated_cost_microunits",
    }
)
_PRICE_KEYS = frozenset(
    {
        "model_id",
        "currency",
        "input_microunits_per_million",
        "cached_input_microunits_per_million",
        "output_microunits_per_million",
        "effective_at",
        "source_label",
    }
)
_USAGE_INTEGER_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_reported_tokens",
    "estimated_cost_microunits",
)
_PRICE_RATE_FIELDS = (
    "input_microunits_per_million",
    "cached_input_microunits_per_million",
    "output_microunits_per_million",
)
_PRICE_TEXT_FIELDS = ("model_id", "effective_at", "source_label")


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_reported_tokens: int
    estimated_cost_microunits: int


@dataclass(frozen=True)
class TelemetrySummary:
    classification: str
    response_digest: str
    usage: UsageSummary
    event_count: int
    raw_retention: str


def _raise_invalid() -> None:
    raise TelemetryError(_ERROR)


def _require_mapping(value: object, exact_keys: frozenset) -> ABCMapping:
    if not isinstance(value, ABCMapping):
        _raise_invalid()
    if set(value.keys()) != set(exact_keys):
        _raise_invalid()
    return value


def _require_non_negative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        _raise_invalid()
    return value


def _require_positive_integer(value: object) -> int:
    checked = _require_non_negative_integer(value)
    if checked < 1:
        _raise_invalid()
    return checked


def _require_exact_text(value: object, expected: str) -> str:
    if type(value) is not str or value != expected:
        _raise_invalid()
    return value


def _require_nonempty_nfc_text(value: object) -> str:
    if type(value) is not str or not value:
        _raise_invalid()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _raise_invalid()
    if not unicodedata.is_normalized("NFC", value):
        _raise_invalid()
    return value


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        _raise_invalid()
    return value


def _validate_usage(usage: object) -> ABCMapping:
    checked = _require_mapping(usage, _USAGE_KEYS)
    for field_name in _USAGE_INTEGER_FIELDS:
        _require_non_negative_integer(checked[field_name])
    if checked["cached_input_tokens"] > checked["input_tokens"]:
        _raise_invalid()
    if checked["reasoning_output_tokens"] > checked["output_tokens"]:
        _raise_invalid()
    if (
        checked["total_reported_tokens"]
        != checked["input_tokens"] + checked["output_tokens"]
    ):
        _raise_invalid()
    return checked


def _validate_price_snapshot(price_snapshot: object) -> ABCMapping:
    price = _require_mapping(price_snapshot, _PRICE_KEYS)
    for field_name in _PRICE_RATE_FIELDS:
        _require_non_negative_integer(price[field_name])
    for field_name in _PRICE_TEXT_FIELDS:
        _require_nonempty_nfc_text(price[field_name])
    currency = price["currency"]
    if (
        type(currency) is not str
        or _CURRENCY_PATTERN.fullmatch(currency) is None
    ):
        _raise_invalid()
    return price


def _validate_summary_document(document: object) -> ABCMapping:
    checked = _require_mapping(document, _SUMMARY_KEYS)
    _require_exact_text(checked["classification"], "completed")
    _require_digest(checked["response_digest"])
    _validate_usage(checked["usage"])
    _require_positive_integer(checked["event_count"])
    _require_exact_text(checked["raw_retention"], "discard")
    return checked


def _validate_typed_summary(summary: object) -> TelemetrySummary:
    if type(summary) is not TelemetrySummary:
        _raise_invalid()
    if type(summary.usage) is not UsageSummary:
        _raise_invalid()
    _validate_summary_document(
        {
            "classification": summary.classification,
            "response_digest": summary.response_digest,
            "usage": {
                "input_tokens": summary.usage.input_tokens,
                "cached_input_tokens": summary.usage.cached_input_tokens,
                "output_tokens": summary.usage.output_tokens,
                "reasoning_output_tokens": summary.usage.reasoning_output_tokens,
                "total_reported_tokens": summary.usage.total_reported_tokens,
                "estimated_cost_microunits": (
                    summary.usage.estimated_cost_microunits
                ),
            },
            "event_count": summary.event_count,
            "raw_retention": summary.raw_retention,
        }
    )
    return summary


def _estimated_cost_microunits(
    usage: Mapping[str, int], price: Mapping[str, object]
) -> int:
    non_cached = usage["input_tokens"] - usage["cached_input_tokens"]
    numerator = (
        non_cached * price["input_microunits_per_million"]
        + usage["cached_input_tokens"]
        * price["cached_input_microunits_per_million"]
        + usage["output_tokens"] * price["output_microunits_per_million"]
    )
    return (numerator + 999999) // 1000000


def telemetry_summary_from_document(
    document: Mapping[str, object],
    price_snapshot: Mapping[str, object],
) -> TelemetrySummary:
    """Validate a price-bound exact document and return its typed summary."""
    try:
        checked = _validate_summary_document(document)
        usage = checked["usage"]
        price = _validate_price_snapshot(price_snapshot)
        if (
            usage["estimated_cost_microunits"]
            != _estimated_cost_microunits(usage, price)
        ):
            _raise_invalid()
        return TelemetrySummary(
            classification=checked["classification"],
            response_digest=checked["response_digest"],
            usage=UsageSummary(
                input_tokens=usage["input_tokens"],
                cached_input_tokens=usage["cached_input_tokens"],
                output_tokens=usage["output_tokens"],
                reasoning_output_tokens=usage["reasoning_output_tokens"],
                total_reported_tokens=usage["total_reported_tokens"],
                estimated_cost_microunits=usage[
                    "estimated_cost_microunits"
                ],
            ),
            event_count=checked["event_count"],
            raw_retention=checked["raw_retention"],
        )
    except Exception:
        raise TelemetryError(_ERROR) from None


def telemetry_summary_document(
    summary: TelemetrySummary,
) -> Mapping[str, object]:
    """Return the exact canonical-document projection of a typed summary."""
    try:
        checked = _validate_typed_summary(summary)
        return {
            "classification": checked.classification,
            "response_digest": checked.response_digest,
            "usage": {
                "input_tokens": checked.usage.input_tokens,
                "cached_input_tokens": checked.usage.cached_input_tokens,
                "output_tokens": checked.usage.output_tokens,
                "reasoning_output_tokens": (
                    checked.usage.reasoning_output_tokens
                ),
                "total_reported_tokens": (
                    checked.usage.total_reported_tokens
                ),
                "estimated_cost_microunits": (
                    checked.usage.estimated_cost_microunits
                ),
            },
            "event_count": checked.event_count,
            "raw_retention": checked.raw_retention,
        }
    except Exception:
        raise TelemetryError(_ERROR) from None


def telemetry_summary_digest(summary: TelemetrySummary) -> str:
    """Return the canonical SHA-256 identity of a typed summary."""
    try:
        document = telemetry_summary_document(summary)
        digest = hashlib.sha256(canonical_bytes(document)).hexdigest()
        return "sha256:{}".format(digest)
    except Exception:
        raise TelemetryError(_ERROR) from None
