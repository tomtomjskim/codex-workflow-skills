"""Canonical typed telemetry summaries for the Phase A experiment."""

from collections.abc import Mapping as ABCMapping
from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import FrozenSet, Mapping, Optional, Tuple
import unicodedata

from scripts.workflow_coordination.canonical_json import canonical_bytes


class TelemetryError(ValueError):
    """Raised when telemetry input, projection, or pricing is invalid."""


_ERROR = "telemetry_summary_invalid"
MAX_TOTAL_BYTES = 8_388_608
MAX_LINE_BYTES = 1_048_576
MAX_EVENTS = 4_096
MAX_TOKEN_VALUE = 1_000_000_000
MAX_JSON_DEPTH = 32
MIN_JSON_INTEGER = -9_223_372_036_854_775_808
MAX_JSON_INTEGER = 9_223_372_036_854_775_807
PRICE_SNAPSHOT_READ_ORDER = (
    "model_id",
    "currency",
    "input_microunits_per_million",
    "cached_input_microunits_per_million",
    "output_microunits_per_million",
    "effective_at",
    "source_label",
)
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
_LIMIT_FIELDS = frozenset(
    {
        "max_total_bytes",
        "max_line_bytes",
        "max_events",
        "max_token_value",
    }
)
_LIMIT_CEILINGS = {
    "max_total_bytes": MAX_TOTAL_BYTES,
    "max_line_bytes": MAX_LINE_BYTES,
    "max_events": MAX_EVENTS,
    "max_token_value": MAX_TOKEN_VALUE,
}
_WIRE_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }
)
_EVENT_KEYS = {
    "thread.started": frozenset({"type", "thread_id"}),
    "turn.started": frozenset({"type"}),
    "item.started": frozenset({"type", "item"}),
    "item.updated": frozenset({"type", "item"}),
    "item.completed": frozenset({"type", "item"}),
    "turn.completed": frozenset({"type", "usage"}),
    "turn.failed": frozenset({"type", "error"}),
    "error": frozenset({"type", "message"}),
}
_ITEM_KEYS = {
    "agent_message": frozenset({"id", "type", "text"}),
    "reasoning": frozenset({"id", "type", "text"}),
    "command_execution": frozenset(
        {
            "id",
            "type",
            "command",
            "aggregated_output",
            "exit_code",
            "status",
        }
    ),
    "file_change": frozenset({"id", "type", "changes", "status"}),
    "todo_list": frozenset({"id", "type", "items"}),
    "error": frozenset({"id", "type", "message"}),
}
_COMMAND_STATUSES = frozenset(
    {"in_progress", "completed", "failed", "declined"}
)
_FILE_CHANGE_STATUSES = frozenset(
    {"in_progress", "completed", "failed"}
)
_FILE_CHANGE_KINDS = frozenset({"add", "delete", "update"})
_STARTABLE_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "todo_list"}
)
_DIRECT_COMPLETION_ITEM_TYPES = frozenset(
    {
        "agent_message",
        "reasoning",
        "command_execution",
        "file_change",
        "error",
    }
)
_SIGNED_INTEGER_LIMIT_TEXT = {
    False: str(MAX_JSON_INTEGER),
    True: str(-MIN_JSON_INTEGER),
}


class _StreamInvalid(ValueError):
    pass


@dataclass(frozen=True)
class TelemetryLimits:
    max_total_bytes: int
    max_line_bytes: int
    max_events: int
    max_token_value: int

    def __post_init__(self) -> None:
        _validate_telemetry_limits(self)


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


@dataclass(frozen=True)
class _TelemetryState:
    phase: str = "START"
    active_items: Tuple[Tuple[str, str], ...] = ()
    seen_item_ids: FrozenSet[str] = frozenset()
    response_digest: Optional[str] = None
    terminal_usage: Optional[Tuple[int, int, int, int, int]] = None


def _raise_invalid() -> None:
    raise TelemetryError(_ERROR)


def _stream_invalid() -> None:
    raise _StreamInvalid()


def _validate_telemetry_limits(limits: object) -> TelemetryLimits:
    valid = False
    try:
        if type(limits) is TelemetryLimits:
            values = vars(limits)
            valid = set(values) == _LIMIT_FIELDS
            if valid:
                valid = all(
                    type(values[field_name]) is int
                    and 1 <= values[field_name] <= ceiling
                    for field_name, ceiling in _LIMIT_CEILINGS.items()
                )
            if valid:
                valid = (
                    values["max_line_bytes"]
                    <= values["max_total_bytes"]
                )
    except Exception:
        valid = False
    if not valid:
        raise TelemetryError("telemetry_limits_invalid")
    return limits


def _is_nfc_text(value: object, *, nonempty: bool = False) -> bool:
    if type(value) is not str or (nonempty and not value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return unicodedata.is_normalized("NFC", value)


def _integer_literal_fits(text: str) -> bool:
    if not text:
        return False
    negative = text.startswith("-")
    digits = text[1:] if negative else text
    if not digits or any(
        character < "0" or character > "9" for character in digits
    ):
        return False
    normalized = digits.lstrip("0") or "0"
    limit = _SIGNED_INTEGER_LIMIT_TEXT[negative]
    return len(normalized) < len(limit) or (
        len(normalized) == len(limit) and normalized <= limit
    )


def _scan_bounded_json(data: bytes) -> None:
    stack = []
    in_string = False
    escaped = False
    index = 0
    while index < len(data):
        byte = data[index]
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            index += 1
            continue
        if byte == 0x22:
            in_string = True
            index += 1
            continue
        if byte in (0x7B, 0x5B):
            stack.append(byte)
            if len(stack) > MAX_JSON_DEPTH:
                _stream_invalid()
            index += 1
            continue
        if byte in (0x7D, 0x5D):
            expected = 0x7B if byte == 0x7D else 0x5B
            if not stack or stack[-1] != expected:
                _stream_invalid()
            stack.pop()
            index += 1
            continue
        if byte == 0x2D or 0x30 <= byte <= 0x39:
            start = index
            if byte == 0x2D:
                index += 1
                if index >= len(data) or not 0x30 <= data[index] <= 0x39:
                    continue
            while index < len(data) and 0x30 <= data[index] <= 0x39:
                index += 1
            try:
                literal = data[start:index].decode("ascii")
            except UnicodeDecodeError:
                _stream_invalid()
            if not _integer_literal_fits(literal):
                _stream_invalid()
            continue
        index += 1


def _reject_json_value(_text: str) -> object:
    _stream_invalid()


def _parse_bounded_json_integer(text: str) -> int:
    if not _integer_literal_fits(text):
        _stream_invalid()
    return int(text)


def _object_without_duplicates(pairs: object) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _stream_invalid()
        result[key] = value
    return result


def _validate_semantic_json_value(value: object, depth: int = 0) -> None:
    if type(value) is dict:
        next_depth = depth + 1
        if next_depth > MAX_JSON_DEPTH:
            _stream_invalid()
        for key, nested in value.items():
            if not _is_nfc_text(key):
                _stream_invalid()
            _validate_semantic_json_value(nested, next_depth)
        return
    if type(value) is list:
        next_depth = depth + 1
        if next_depth > MAX_JSON_DEPTH:
            _stream_invalid()
        for nested in value:
            _validate_semantic_json_value(nested, next_depth)
        return
    if type(value) is str:
        if not _is_nfc_text(value):
            _stream_invalid()
        return
    if type(value) is int:
        if value < MIN_JSON_INTEGER or value > MAX_JSON_INTEGER:
            _stream_invalid()
        return
    if type(value) in (bool, type(None)):
        return
    _stream_invalid()


def _load_bounded_semantic_json(data: bytes) -> dict:
    invalid = False
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            _stream_invalid()
        _scan_bounded_json(data)
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_int=_parse_bounded_json_integer,
            parse_float=_reject_json_value,
            parse_constant=_reject_json_value,
        )
        if type(value) is not dict:
            _stream_invalid()
        _validate_semantic_json_value(value)
    except Exception:
        invalid = True
    if invalid:
        _stream_invalid()
    return value


def _require_stream_keys(value: object, expected: frozenset) -> dict:
    if type(value) is not dict or set(value) != expected:
        _stream_invalid()
    return value


def _structured_response_digest(text: object) -> str:
    if not _is_nfc_text(text):
        _stream_invalid()
    invalid = False
    try:
        response = _load_bounded_semantic_json(text.encode("utf-8"))
        digest = "sha256:{}".format(
            hashlib.sha256(canonical_bytes(response)).hexdigest()
        )
    except Exception:
        invalid = True
    if invalid:
        _stream_invalid()
    return digest


def _validate_wire_usage(
    usage: object, limits: TelemetryLimits
) -> Tuple[int, int, int, int, int]:
    checked = _require_stream_keys(usage, _WIRE_USAGE_KEYS)
    values = []
    for field_name in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = checked[field_name]
        if (
            type(value) is not int
            or value < 0
            or value > limits.max_token_value
        ):
            _stream_invalid()
        values.append(value)
    input_tokens, cached_input, cache_write, output_tokens, reasoning = values
    if (
        cached_input > input_tokens
        or reasoning > output_tokens
        or input_tokens + output_tokens > limits.max_token_value
        or cache_write != 0
    ):
        _stream_invalid()
    return (
        input_tokens,
        cached_input,
        cache_write,
        output_tokens,
        reasoning,
    )


def _require_item_text(item: dict, field_name: str) -> None:
    if not _is_nfc_text(item[field_name]):
        _stream_invalid()


def _validate_item(item: object) -> Tuple[str, str, dict]:
    if (
        type(item) is not dict
        or type(item.get("id")) is not str
        or type(item.get("type")) is not str
    ):
        _stream_invalid()
    item_type = item["type"]
    expected_keys = _ITEM_KEYS.get(item_type)
    if expected_keys is None:
        _stream_invalid()
    checked = _require_stream_keys(item, expected_keys)
    item_id = checked["id"]
    if not _is_nfc_text(item_id, nonempty=True):
        _stream_invalid()

    if item_type in ("agent_message", "reasoning"):
        _require_item_text(checked, "text")
    elif item_type == "command_execution":
        _require_item_text(checked, "command")
        _require_item_text(checked, "aggregated_output")
        exit_code = checked["exit_code"]
        if exit_code is not None and (
            type(exit_code) is not int
            or exit_code < -2_147_483_648
            or exit_code > 2_147_483_647
        ):
            _stream_invalid()
        status = checked["status"]
        if type(status) is not str or status not in _COMMAND_STATUSES:
            _stream_invalid()
    elif item_type == "file_change":
        status = checked["status"]
        if type(status) is not str or status not in _FILE_CHANGE_STATUSES:
            _stream_invalid()
        changes = checked["changes"]
        if type(changes) is not list:
            _stream_invalid()
        for change in changes:
            nested = _require_stream_keys(
                change, frozenset({"path", "kind"})
            )
            if not _is_nfc_text(nested["path"]):
                _stream_invalid()
            if (
                type(nested["kind"]) is not str
                or nested["kind"] not in _FILE_CHANGE_KINDS
            ):
                _stream_invalid()
    elif item_type == "todo_list":
        items = checked["items"]
        if type(items) is not list:
            _stream_invalid()
        for todo in items:
            nested = _require_stream_keys(
                todo, frozenset({"text", "completed"})
            )
            if (
                not _is_nfc_text(nested["text"])
                or type(nested["completed"]) is not bool
            ):
                _stream_invalid()
    else:
        _require_item_text(checked, "message")
    return item_id, item_type, checked


def _validate_event(
    event: object, limits: TelemetryLimits
) -> Tuple[
    str,
    Optional[Tuple[str, str, dict]],
    Optional[Tuple[int, int, int, int, int]],
]:
    if type(event) is not dict or type(event.get("type")) is not str:
        _stream_invalid()
    event_type = event["type"]
    expected_keys = _EVENT_KEYS.get(event_type)
    if expected_keys is None:
        _stream_invalid()
    checked = _require_stream_keys(event, expected_keys)

    item = None
    usage = None
    if event_type == "thread.started":
        if not _is_nfc_text(checked["thread_id"], nonempty=True):
            _stream_invalid()
    elif event_type.startswith("item."):
        item = _validate_item(checked["item"])
    elif event_type == "turn.completed":
        usage = _validate_wire_usage(checked["usage"], limits)
    elif event_type == "turn.failed":
        error = _require_stream_keys(
            checked["error"], frozenset({"message"})
        )
        if not _is_nfc_text(error["message"]):
            _stream_invalid()
    elif event_type == "error":
        if not _is_nfc_text(checked["message"]):
            _stream_invalid()
    return event_type, item, usage


def _consume_item_event(
    state: _TelemetryState,
    event_type: str,
    item: Tuple[str, str, dict],
) -> _TelemetryState:
    item_id, item_type, checked = item
    active = dict(state.active_items)
    seen = set(state.seen_item_ids)

    if event_type == "item.started":
        if (
            item_type not in _STARTABLE_ITEM_TYPES
            or item_id in seen
        ):
            _stream_invalid()
        if item_type == "command_execution" and (
            checked["status"] != "in_progress"
            or checked["exit_code"] is not None
        ):
            _stream_invalid()
        if (
            item_type == "file_change"
            and checked["status"] != "in_progress"
        ):
            _stream_invalid()
        active[item_id] = item_type
        seen.add(item_id)
    elif event_type == "item.updated":
        if (
            item_type != "todo_list"
            or active.get(item_id) != item_type
        ):
            _stream_invalid()
    else:
        active_type = active.get(item_id)
        if active_type is not None:
            if active_type != item_type:
                _stream_invalid()
            del active[item_id]
        else:
            if (
                item_id in seen
                or item_type not in _DIRECT_COMPLETION_ITEM_TYPES
            ):
                _stream_invalid()
            seen.add(item_id)

    response_digest = state.response_digest
    if event_type == "item.completed" and item_type == "agent_message":
        if response_digest is not None:
            _stream_invalid()
        response_digest = _structured_response_digest(checked["text"])
    return replace(
        state,
        active_items=tuple(active.items()),
        seen_item_ids=frozenset(seen),
        response_digest=response_digest,
    )


def _consume_event(
    state: _TelemetryState,
    event: object,
    limits: TelemetryLimits,
) -> _TelemetryState:
    event_type, item, usage = _validate_event(event, limits)
    if state.phase == "START":
        if event_type != "thread.started":
            _stream_invalid()
        return replace(state, phase="THREAD")
    if state.phase == "THREAD":
        if event_type != "turn.started":
            _stream_invalid()
        return replace(state, phase="TURN_ACTIVE")
    if state.phase != "TURN_ACTIVE":
        _stream_invalid()
    if event_type.startswith("item."):
        if item is None:
            _stream_invalid()
        return _consume_item_event(state, event_type, item)
    if event_type in ("error", "turn.failed"):
        _stream_invalid()
    if event_type != "turn.completed":
        _stream_invalid()
    if (
        state.active_items
        or state.response_digest is None
        or usage is None
    ):
        _stream_invalid()
    return replace(
        state,
        phase="SUCCESS",
        terminal_usage=usage,
    )


def _require_mapping(value: object, exact_keys: frozenset) -> dict:
    if not isinstance(value, ABCMapping):
        _raise_invalid()
    snapshot = dict(value)
    if set(snapshot) != set(exact_keys):
        _raise_invalid()
    return snapshot


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


def _validate_usage(usage: object) -> dict:
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


def _validate_price_snapshot(price_snapshot: object) -> dict:
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


def _snapshot_price_mapping(price_snapshot: object) -> dict:
    invalid = False
    try:
        if not isinstance(price_snapshot, ABCMapping):
            _raise_invalid()
        iterator = iter(price_snapshot)
        keys = []
        seen = set()
        for _index in range(len(_PRICE_KEYS) + 1):
            try:
                key = next(iterator)
            except StopIteration:
                break
            if type(key) is not str or key in seen:
                _raise_invalid()
            seen.add(key)
            keys.append(key)
        if len(keys) != len(_PRICE_KEYS) or seen != _PRICE_KEYS:
            _raise_invalid()
        detached = {}
        for key in PRICE_SNAPSHOT_READ_ORDER:
            detached[key] = price_snapshot[key]
    except Exception:
        invalid = True
    if invalid:
        raise TelemetryError(_ERROR)
    return detached


def _validate_summary_document(document: object) -> dict:
    checked = _require_mapping(document, _SUMMARY_KEYS)
    _require_exact_text(checked["classification"], "completed")
    _require_digest(checked["response_digest"])
    checked["usage"] = _validate_usage(checked["usage"])
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


def parse_terminal_telemetry(
    data: bytes,
    limits: TelemetryLimits,
    price_snapshot: Mapping[str, object],
) -> TelemetrySummary:
    """Project one bounded successful Codex JSONL stream."""
    if type(data) is not bytes:
        raise TelemetryError("telemetry_not_bytes")
    checked_limits = _validate_telemetry_limits(limits)
    if not data or len(data) > checked_limits.max_total_bytes:
        raise TelemetryError("telemetry_size_invalid")
    if not data.endswith(b"\n"):
        raise TelemetryError("telemetry_truncated")
    if b"\r" in data:
        raise TelemetryError("telemetry_line_invalid")
    lines = data[:-1].split(b"\n")
    if not lines or len(lines) > checked_limits.max_events:
        raise TelemetryError("telemetry_event_count_invalid")
    for line in lines:
        if not line or len(line) > checked_limits.max_line_bytes:
            raise TelemetryError("telemetry_line_invalid")

    stream_invalid = False
    try:
        state = _TelemetryState()
        for line in lines:
            event = _load_bounded_semantic_json(line)
            state = _consume_event(state, event, checked_limits)
        if (
            state.phase != "SUCCESS"
            or state.response_digest is None
            or state.terminal_usage is None
        ):
            _stream_invalid()
    except Exception:
        stream_invalid = True
    if stream_invalid:
        raise TelemetryError("telemetry_stream_invalid")

    summary_invalid = False
    try:
        price = _validate_price_snapshot(
            _snapshot_price_mapping(price_snapshot)
        )
        (
            input_tokens,
            cached_input_tokens,
            _cache_write_input_tokens,
            output_tokens,
            reasoning_output_tokens,
        ) = state.terminal_usage
        projected_usage = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_reported_tokens": input_tokens + output_tokens,
        }
        projected_usage["estimated_cost_microunits"] = (
            _estimated_cost_microunits(projected_usage, price)
        )
        document = {
            "classification": "completed",
            "response_digest": state.response_digest,
            "usage": projected_usage,
            "event_count": len(lines),
            "raw_retention": "discard",
        }
        summary = telemetry_summary_from_document(document, price)
    except Exception:
        summary_invalid = True
    if summary_invalid:
        raise TelemetryError(_ERROR)
    return summary


def telemetry_summary_from_document(
    document: Mapping[str, object],
    price_snapshot: Mapping[str, object],
) -> TelemetrySummary:
    """Validate a price-bound exact document and return its typed summary."""
    invalid = False
    try:
        checked = _validate_summary_document(document)
        usage = checked["usage"]
        price = _validate_price_snapshot(price_snapshot)
        if (
            usage["estimated_cost_microunits"]
            != _estimated_cost_microunits(usage, price)
        ):
            _raise_invalid()
        summary = TelemetrySummary(
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
        invalid = True
    if invalid:
        raise TelemetryError(_ERROR)
    return summary


def telemetry_summary_document(
    summary: TelemetrySummary,
) -> Mapping[str, object]:
    """Return the exact canonical-document projection of a typed summary."""
    invalid = False
    try:
        checked = _validate_typed_summary(summary)
        document = {
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
        invalid = True
    if invalid:
        raise TelemetryError(_ERROR)
    return document


def telemetry_summary_digest(summary: TelemetrySummary) -> str:
    """Return the canonical SHA-256 identity of a typed summary."""
    invalid = False
    try:
        document = telemetry_summary_document(summary)
        digest = hashlib.sha256(canonical_bytes(document)).hexdigest()
    except Exception:
        invalid = True
    if invalid:
        raise TelemetryError(_ERROR)
    return "sha256:{}".format(digest)
