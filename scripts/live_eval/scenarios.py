"""Versioned live-evaluation scenarios and deterministic response assertions."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Mapping, Tuple


class ScenarioCorpusError(ValueError):
    """Raised when a scenario corpus does not satisfy the canonical schema."""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    schema_version: int
    tags: Tuple[str, ...]
    prompt: str
    required_paths: Tuple[str, ...]
    required_values: Tuple[Tuple[str, Any], ...]
    forbidden_values: Tuple[Any, ...]
    expected_status: str
    timeout_seconds: int


@dataclass(frozen=True)
class AssertionReport:
    passed: bool
    expected_status: str
    missing_paths: Tuple[str, ...]
    value_mismatches: Tuple[Tuple[str, Any, Tuple[Any, ...]], ...]
    forbidden_matches: Tuple[Tuple[str, Any], ...]


_ROOT_FIELDS = {"schema_version", "scenarios"}
_SCENARIO_FIELDS = {
    "scenario_id",
    "schema_version",
    "tags",
    "prompt",
    "required_paths",
    "required_values",
    "forbidden_values",
    "expected_status",
    "timeout_seconds",
}
_EXPECTED_STATUSES = {
    "pass",
    "blocked",
    "partial",
    "approval_required",
    "pass_with_residual_risk",
    "required",
    "not_needed",
}
_PATH_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]*(?:\[\])?(?:\.[A-Za-z_][A-Za-z0-9_-]*(?:\[\])?)*$"
)


def load_scenarios(path: Path) -> Dict[str, Scenario]:
    """Load a canonical scenario corpus, keyed in stable scenario-ID order."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioCorpusError(str(error)) from error

    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS:
        raise ScenarioCorpusError("corpus has invalid fields")
    if not _is_int(payload["schema_version"]) or payload["schema_version"] != 1:
        raise ScenarioCorpusError("corpus schema_version must be integer 1")
    entries = payload["scenarios"]
    if not isinstance(entries, list):
        raise ScenarioCorpusError("corpus scenarios must be a list")

    scenarios = []
    seen = set()
    for index, entry in enumerate(entries):
        scenario = _parse_scenario(entry, index)
        if scenario.scenario_id in seen:
            raise ScenarioCorpusError(
                "duplicate scenario_id: {}".format(scenario.scenario_id)
            )
        seen.add(scenario.scenario_id)
        scenarios.append(scenario)

    return {item.scenario_id: item for item in sorted(scenarios, key=_scenario_key)}


def select_scenarios(
    scenarios: Mapping[str, Scenario],
    tags: Iterable[str],
    limit: int = 3,
) -> Tuple[Scenario, ...]:
    """Select by tag OR in stable ID order; empty tags select all."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if isinstance(tags, (str, bytes)):
        raise ValueError("tags must be an iterable of strings, not a bare string")
    try:
        requested_tags = tuple(tags)
    except TypeError as error:
        raise ValueError("tags must be an iterable of strings") from error
    if any(
        not isinstance(tag, str) or not tag.strip() for tag in requested_tags
    ):
        raise ValueError("tags must contain only non-empty strings")

    requested = frozenset(requested_tags)
    ordered = sorted(scenarios.values(), key=_scenario_key)
    matching = (
        item for item in ordered if not requested or requested.intersection(item.tags)
    )
    selected = []
    for item in matching:
        selected.append(item)
        if len(selected) == limit:
            break
    return tuple(selected)


def assert_response(scenario: Scenario, response: Mapping[str, Any]) -> AssertionReport:
    """Evaluate required paths/values and forbidden values deterministically.

    ``expected_status`` is runner-facing scenario outcome metadata. It is exposed
    in the report but is intentionally independent of structured response checks.
    A path segment ending in ``[]`` uses existential semantics: at least one
    expanded list element must satisfy a required exact value.
    """
    path_values = {
        path: _values_at_path(response, path) for path in scenario.required_paths
    }
    missing_paths = tuple(path for path, values in path_values.items() if not values)
    value_mismatches = tuple(
        (path, expected, values)
        for path, expected in scenario.required_values
        for values in (path_values[path],)
        if values and not any(_same_json_value(value, expected) for value in values)
    )
    forbidden_matches = tuple(
        (path, value)
        for path, value in _walk_values(response)
        if any(
            _same_json_value(value, forbidden)
            for forbidden in scenario.forbidden_values
        )
    )
    return AssertionReport(
        passed=not missing_paths and not value_mismatches and not forbidden_matches,
        expected_status=scenario.expected_status,
        missing_paths=missing_paths,
        value_mismatches=value_mismatches,
        forbidden_matches=forbidden_matches,
    )


def _parse_scenario(entry: Any, index: int) -> Scenario:
    label = "scenario at index {}".format(index)
    if not isinstance(entry, dict):
        raise ScenarioCorpusError("{} must be an object".format(label))
    if set(entry) != _SCENARIO_FIELDS:
        raise ScenarioCorpusError("{} has invalid fields".format(label))

    scenario_id = _nonempty_string(entry["scenario_id"], "{}.scenario_id".format(label))
    schema_version = entry["schema_version"]
    if not _is_int(schema_version) or schema_version != 1:
        raise ScenarioCorpusError("{}.schema_version must be integer 1".format(label))
    tags = _string_tuple(entry["tags"], "{}.tags".format(label), require_items=True)
    prompt = _nonempty_string(entry["prompt"], "{}.prompt".format(label))
    required_paths = _path_tuple(
        entry["required_paths"], "{}.required_paths".format(label)
    )

    required_values_value = entry["required_values"]
    if not isinstance(required_values_value, dict):
        raise ScenarioCorpusError("{}.required_values must be an object".format(label))
    required_values = []
    for path in sorted(required_values_value):
        _validate_path(path, "{}.required_values".format(label))
        if path not in required_paths:
            raise ScenarioCorpusError(
                "{}.required_values path must also be required".format(label)
            )
        expected = required_values_value[path]
        _validate_json_value(expected, "{}.required_values.{}".format(label, path))
        required_values.append((path, expected))

    forbidden_values_value = entry["forbidden_values"]
    if not isinstance(forbidden_values_value, list):
        raise ScenarioCorpusError("{}.forbidden_values must be a list".format(label))
    for position, forbidden in enumerate(forbidden_values_value):
        _validate_json_value(
            forbidden, "{}.forbidden_values[{}]".format(label, position)
        )

    expected_status = entry["expected_status"]
    if not isinstance(expected_status, str) or expected_status not in _EXPECTED_STATUSES:
        raise ScenarioCorpusError("{}.expected_status is invalid".format(label))
    timeout_seconds = entry["timeout_seconds"]
    if not _is_int(timeout_seconds) or not 1 <= timeout_seconds <= 300:
        raise ScenarioCorpusError(
            "{}.timeout_seconds must be an integer from 1 to 300".format(label)
        )

    return Scenario(
        scenario_id=scenario_id,
        schema_version=schema_version,
        tags=tuple(sorted(tags)),
        prompt=prompt,
        required_paths=required_paths,
        required_values=tuple(required_values),
        forbidden_values=tuple(forbidden_values_value),
        expected_status=expected_status,
        timeout_seconds=timeout_seconds,
    )


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioCorpusError("{} must be a non-empty string".format(label))
    return value


def _string_tuple(value: Any, label: str, require_items: bool) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ScenarioCorpusError("{} must be a string list".format(label))
    if require_items and not value:
        raise ScenarioCorpusError("{} must be a non-empty string list".format(label))
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ScenarioCorpusError("{} must be a string list".format(label))
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ScenarioCorpusError("{} must not contain duplicates".format(label))
    return result


def _path_tuple(value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ScenarioCorpusError("{} must be a non-empty path list".format(label))
    for path in value:
        _validate_path(path, label)
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ScenarioCorpusError("{} must not contain duplicates".format(label))
    return result


def _validate_path(path: str, label: str) -> None:
    if not isinstance(path, str) or not _PATH_PATTERN.fullmatch(path):
        raise ScenarioCorpusError("{} contains an invalid canonical path".format(label))


def _validate_json_value(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, bool)) or _is_int(value):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ScenarioCorpusError("{} must be JSON-compatible".format(label))
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, "{}[{}]".format(label, index))
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ScenarioCorpusError("{} must be JSON-compatible".format(label))
        for key in sorted(value):
            _validate_json_value(value[key], "{}.{}".format(label, key))
        return
    raise ScenarioCorpusError("{} must be JSON-compatible".format(label))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _scenario_key(scenario: Scenario) -> str:
    return scenario.scenario_id


def _values_at_path(response: Mapping[str, Any], path: str) -> Tuple[Any, ...]:
    current = (response,)
    for raw_segment in path.split("."):
        expand_list = raw_segment.endswith("[]")
        segment = raw_segment[:-2] if expand_list else raw_segment
        next_values = []
        for value in current:
            if not isinstance(value, Mapping) or segment not in value:
                continue
            child = value[segment]
            if expand_list:
                if isinstance(child, list):
                    next_values.extend(child)
            else:
                next_values.append(child)
        current = tuple(next_values)
        if not current:
            return ()
    return current


def _walk_values(value: Any, path: str = "$") -> Iterable[Tuple[str, Any]]:
    if path != "$":
        yield path, value
    if isinstance(value, Mapping):
        for key in sorted(value):
            child_path = str(key) if path == "$" else "{}.{}".format(path, key)
            yield from _walk_values(value[key], child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_values(child, "{}[{}]".format(path, index))


def _same_json_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    return left == right
