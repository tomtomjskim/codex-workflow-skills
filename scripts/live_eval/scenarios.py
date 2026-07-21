"""Versioned live-evaluation scenarios and deterministic response assertions."""

from dataclasses import dataclass
import json
from pathlib import Path
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
    forbidden_values: Tuple[str, ...]
    expected_status: str
    timeout_seconds: int


@dataclass(frozen=True)
class AssertionReport:
    passed: bool
    missing_paths: Tuple[str, ...]
    forbidden_matches: Tuple[Tuple[str, str], ...]


_SCENARIO_FIELDS = {
    "scenario_id",
    "schema_version",
    "tags",
    "prompt",
    "required_paths",
    "forbidden_values",
    "expected_status",
    "timeout_seconds",
}


def load_scenarios(path: Path) -> Dict[str, Scenario]:
    """Load a canonical scenario corpus, keyed in stable scenario-ID order."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioCorpusError(str(error)) from error

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ScenarioCorpusError("corpus schema_version must be 1")
    entries = payload.get("scenarios")
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
    """Select matching scenarios in stable ID order, bounded by ``limit``."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    requested_tags = frozenset(tags)
    ordered = sorted(scenarios.values(), key=_scenario_key)
    matching = (
        item
        for item in ordered
        if not requested_tags or requested_tags.intersection(item.tags)
    )
    return tuple(list(matching)[:limit])


def assert_response(scenario: Scenario, response: Mapping[str, Any]) -> AssertionReport:
    """Evaluate structured path presence and exact forbidden values."""
    missing_paths = tuple(
        path for path in scenario.required_paths if not _values_at_path(response, path)
    )
    forbidden_matches = tuple(
        (path, value)
        for path, value in _walk_values(response)
        if any(_same_value(value, forbidden) for forbidden in scenario.forbidden_values)
    )
    return AssertionReport(
        passed=not missing_paths and not forbidden_matches,
        missing_paths=missing_paths,
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
    if schema_version != 1:
        raise ScenarioCorpusError("{}.schema_version must be 1".format(label))
    tags = _string_tuple(entry["tags"], "{}.tags".format(label), require_items=True)
    prompt = _nonempty_string(entry["prompt"], "{}.prompt".format(label))
    required_paths = _string_tuple(
        entry["required_paths"], "{}.required_paths".format(label), require_items=True
    )
    forbidden_values = _string_tuple(
        entry["forbidden_values"],
        "{}.forbidden_values".format(label),
        require_items=False,
    )
    expected_status = _nonempty_string(
        entry["expected_status"], "{}.expected_status".format(label)
    )
    timeout_seconds = entry["timeout_seconds"]
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
    ):
        raise ScenarioCorpusError("{}.timeout_seconds must be positive".format(label))

    return Scenario(
        scenario_id=scenario_id,
        schema_version=schema_version,
        tags=tuple(sorted(tags)),
        prompt=prompt,
        required_paths=required_paths,
        forbidden_values=tuple(forbidden_values),
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


def _walk_values(value: Any, path: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key in sorted(value):
            child_path = "{}.{}".format(path, key) if path else str(key)
            yield from _walk_values(value[key], child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_values(child, "{}[{}]".format(path, index))
        return
    yield path, value


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right
