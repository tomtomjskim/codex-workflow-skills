"""Canonical JSON encoding and hash identifiers for coordination artifacts."""

import hashlib
import json
import unicodedata
from typing import Dict, List, Tuple


class CanonicalJSONError(ValueError):
    """Raised when a value cannot be represented as canonical JSON."""


def _reject_duplicate_keys(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJSONError("duplicate key: {}".format(key))
        result[key] = value
    return result


def _reject_float(value: str) -> object:
    raise CanonicalJSONError("floating-point values are not allowed: {}".format(value))


def _reject_constant(value: str) -> object:
    raise CanonicalJSONError("non-finite numbers are not allowed: {}".format(value))


def _validate_string(value: str) -> None:
    if not unicodedata.is_normalized("NFC", value):
        raise CanonicalJSONError("strings must use Unicode NFC")


def _validate_value(value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _validate_string(value)
        return
    if isinstance(value, int):
        return
    if isinstance(value, list):
        for item in value:
            _validate_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError("object keys must be strings")
            _validate_string(key)
            _validate_value(item)
        return
    if isinstance(value, float):
        raise CanonicalJSONError("floating-point values are not allowed")
    raise CanonicalJSONError("unsupported JSON value type: {}".format(type(value).__name__))


def load_canonical_input(data: bytes) -> object:
    """Parse JSON with duplicate-key, float, and non-finite-number rejection."""
    if not isinstance(data, bytes):
        raise CanonicalJSONError("canonical input must be UTF-8 JSON bytes")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CanonicalJSONError("canonical input must be valid UTF-8") from error

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise CanonicalJSONError("invalid JSON: {}".format(error.msg)) from error

    _validate_value(value)
    return value


def canonical_bytes(value: object) -> bytes:
    """Validate NFC strings and encode sorted compact UTF-8 JSON."""
    _validate_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_id(value: object) -> str:
    """Return sha256:<lowercase hex> over canonical_bytes(value)."""
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return "sha256:{}".format(digest)
