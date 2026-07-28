#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


MIN_CHECKOUT_MAJOR = 7
_CHECKOUT_TAG_PATTERN = re.compile(r"^v(?P<major>[1-9][0-9]*)$")
_STRING_TAG = "tag:yaml.org,2002:str"
_INTEGER_TAG = "tag:yaml.org,2002:int"
_MERGE_TAG = "tag:yaml.org,2002:merge"
_DEFAULT_BRANCH = "main"
_REQUIRED_DEPENDABOT_ECOSYSTEMS = frozenset(
    ("github-actions", "pip")
)


def _line_number(node) -> int:
    return node.start_mark.line + 1


def _yaml_error(error: yaml.YAMLError, label: str) -> Tuple[int, str]:
    mark = getattr(error, "problem_mark", None)
    line_number = mark.line + 1 if mark is not None else 1
    problem = getattr(error, "problem", None)
    message = label + " YAML is invalid"
    if problem:
        message += ": " + str(problem)
    return line_number, message


def _compose(
    source: str,
    label: str,
) -> Tuple[Optional[object], List[Tuple[int, str]]]:
    try:
        root = yaml.compose(source, Loader=yaml.SafeLoader)
        yaml.load(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        return None, [_yaml_error(error, label)]
    if root is None:
        return None, [(1, label + " YAML is empty")]
    return root, []


def _validate_node_contract(
    node,
    errors: List[Tuple[int, str]],
    visited: Set[int],
) -> None:
    identity = id(node)
    if identity in visited:
        return
    visited.add(identity)

    if isinstance(node, MappingNode):
        seen = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                errors.append(
                    (
                        _line_number(key_node),
                        "mapping keys must be scalar values",
                    )
                )
            else:
                key_identity = (key_node.tag, key_node.value)
                if key_identity in seen:
                    errors.append(
                        (
                            _line_number(key_node),
                            "duplicate YAML key: " + key_node.value,
                        )
                    )
                else:
                    seen[key_identity] = key_node
                if key_node.tag == _MERGE_TAG:
                    errors.append(
                        (
                            _line_number(key_node),
                            "YAML merge keys are not supported",
                        )
                    )
            _validate_node_contract(value_node, errors, visited)
    elif isinstance(node, SequenceNode):
        for value_node in node.value:
            _validate_node_contract(value_node, errors, visited)


def _mapping_value(node, key: str):
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if (
            isinstance(key_node, ScalarNode)
            and key_node.tag == _STRING_TAG
            and key_node.value == key
        ):
            return value_node
    return None


def _static_string(
    node,
    line_number: int,
    label: str,
    errors: List[Tuple[int, str]],
) -> Optional[str]:
    if not isinstance(node, ScalarNode) or node.tag != _STRING_TAG:
        errors.append((line_number, label + " must be a static string"))
        return None
    return node.value


def _record_uses(
    mapping,
    references: List[Tuple[int, str]],
    errors: List[Tuple[int, str]],
) -> None:
    uses_node = _mapping_value(mapping, "uses")
    if uses_node is None:
        return
    line_number = _line_number(uses_node)
    reference = _static_string(
        uses_node,
        line_number,
        "uses",
        errors,
    )
    if reference is not None:
        references.append((line_number, reference))


def _scan_workflow_uses(
    source: str,
) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]:
    root, errors = _compose(source, "workflow")
    if root is None:
        return [], errors

    _validate_node_contract(root, errors, set())
    if not isinstance(root, MappingNode):
        errors.append((_line_number(root), "workflow root must be a mapping"))
        return [], errors

    jobs = _mapping_value(root, "jobs")
    if jobs is None:
        return [], errors
    if not isinstance(jobs, MappingNode):
        errors.append((_line_number(jobs), "jobs must be a mapping"))
        return [], errors

    references = []
    for _, job in jobs.value:
        if not isinstance(job, MappingNode):
            errors.append((_line_number(job), "job must be a mapping"))
            continue

        _record_uses(job, references, errors)
        steps = _mapping_value(job, "steps")
        if steps is None:
            continue
        if not isinstance(steps, SequenceNode):
            errors.append((_line_number(steps), "steps must be a sequence"))
            continue
        for step in steps.value:
            if not isinstance(step, MappingNode):
                errors.append((_line_number(step), "step must be a mapping"))
                continue
            _record_uses(step, references, errors)
    return references, errors


def executable_uses(source: str) -> List[Tuple[int, str]]:
    references, _ = _scan_workflow_uses(source)
    return references


def _dependabot_contract_is_valid(source: str) -> bool:
    root, errors = _compose(source, "dependabot")
    if root is None:
        return False

    _validate_node_contract(root, errors, set())
    if errors or not isinstance(root, MappingNode):
        return False

    version = _mapping_value(root, "version")
    if (
        not isinstance(version, ScalarNode)
        or version.tag != _INTEGER_TAG
        or version.value != "2"
    ):
        return False

    updates = _mapping_value(root, "updates")
    if not isinstance(updates, SequenceNode):
        return False

    root_occurrences = {
        ecosystem: 0
        for ecosystem in _REQUIRED_DEPENDABOT_ECOSYSTEMS
    }
    valid_ecosystems = {
        ecosystem: 0
        for ecosystem in _REQUIRED_DEPENDABOT_ECOSYSTEMS
    }
    for update in updates.value:
        if not isinstance(update, MappingNode):
            continue
        ecosystem = _mapping_value(update, "package-ecosystem")
        directory = _mapping_value(update, "directory")
        schedule = _mapping_value(update, "schedule")
        target_branch = _mapping_value(update, "target-branch")
        if (
            not isinstance(ecosystem, ScalarNode)
            or ecosystem.tag != _STRING_TAG
            or ecosystem.value not in _REQUIRED_DEPENDABOT_ECOSYSTEMS
        ):
            continue
        if (
            not isinstance(directory, ScalarNode)
            or directory.tag != _STRING_TAG
            or directory.value != "/"
        ):
            continue
        if target_branch is not None and (
            not isinstance(target_branch, ScalarNode)
            or target_branch.tag != _STRING_TAG
            or target_branch.value != _DEFAULT_BRANCH
        ):
            continue
        root_occurrences[ecosystem.value] += 1
        if not isinstance(schedule, MappingNode):
            continue
        interval = _mapping_value(schedule, "interval")
        if (
            isinstance(interval, ScalarNode)
            and interval.tag == _STRING_TAG
            and interval.value == "weekly"
        ):
            valid_ecosystems[ecosystem.value] += 1
    return all(
        root_occurrences[ecosystem] == 1
        and valid_ecosystems[ecosystem] == 1
        for ecosystem in _REQUIRED_DEPENDABOT_ECOSYSTEMS
    )


def validate_repository(root: Path) -> List[str]:
    errors = []
    checkout_count = 0
    workflow_root = root / ".github" / "workflows"
    workflow_paths = sorted(workflow_root.glob("*.yml"))
    workflow_paths += sorted(workflow_root.glob("*.yaml"))

    for path in workflow_paths:
        source = path.read_text(encoding="utf-8")
        references, scan_errors = _scan_workflow_uses(source)
        relative = path.relative_to(root)
        for line_number, message in scan_errors:
            errors.append("{}:{} {}".format(relative, line_number, message))
        for line_number, reference in references:
            repository, separator, tag = reference.partition("@")
            if (
                not separator
                or repository.casefold() != "actions/checkout"
            ):
                continue
            checkout_count += 1
            match = _CHECKOUT_TAG_PATTERN.fullmatch(tag)
            if match is None:
                errors.append(
                    "{}:{} checkout must use a vN major tag: {}".format(
                        relative,
                        line_number,
                        reference,
                    )
                )
                continue
            major = int(match.group("major"))
            if major < MIN_CHECKOUT_MAJOR:
                errors.append(
                    "{}:{} uses checkout v{}; minimum is v{}".format(
                        relative,
                        line_number,
                        major,
                        MIN_CHECKOUT_MAJOR,
                    )
                )

    if checkout_count == 0:
        errors.append("no executable actions/checkout reference found")

    dependabot_path = root / ".github" / "dependabot.yml"
    if not dependabot_path.is_file():
        errors.append("missing .github/dependabot.yml")
    elif not _dependabot_contract_is_valid(
        dependabot_path.read_text(encoding="utf-8")
    ):
        errors.append(
            "dependabot.yml must define a weekly GitHub Actions update "
            "and weekly pip update for directory /"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate GitHub Actions runtime maintenance policy."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    errors = validate_repository(args.repo_root.resolve())
    if errors:
        for error in errors:
            print("error: " + error)
        return 1
    print("ok: CI action maintenance policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
