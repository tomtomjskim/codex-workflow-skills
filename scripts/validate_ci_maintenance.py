#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
from typing import List, Optional, Tuple


MIN_CHECKOUT_MAJOR = 7
_USES_PATTERN = re.compile(
    r"^\s*(?:-\s*)?uses:\s*"
    r"(?P<reference>(?:'[^']+'|\"[^\"]+\"|[^#\s]+))"
)
_CHECKOUT_PATTERN = re.compile(
    r"^actions/checkout@v(?P<major>[1-9][0-9]*)$"
)


def executable_uses(source: str) -> List[Tuple[int, str]]:
    references = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = _USES_PATTERN.match(line)
        if match is None:
            continue
        reference = match.group("reference").strip("\"'")
        references.append((line_number, reference))
    return references


def _parse_key_value(line: str) -> Optional[Tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("- "):
        stripped = stripped[2:].lstrip()
    key, separator, value = stripped.partition(":")
    if not separator:
        return None
    value = value.split(" #", 1)[0].strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in "\"'"
    ):
        value = value[1:-1]
    return key.strip(), value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _has_weekly_github_actions_update(source: str) -> bool:
    lines = source.splitlines()
    has_version_two = any(
        _indent(line) == 0
        and _parse_key_value(line) == ("version", "2")
        for line in lines
    )
    if not has_version_two:
        return False

    updates_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _indent(line) == 0
            and _parse_key_value(line) == ("updates", "")
        ),
        None,
    )
    if updates_index is None:
        return False

    for index in range(updates_index + 1, len(lines)):
        line = lines[index]
        parsed = _parse_key_value(line)
        if parsed is None:
            continue
        if _indent(line) == 0:
            break
        if not line.strip().startswith("- "):
            continue
        if parsed != ("package-ecosystem", "github-actions"):
            continue

        item_indent = _indent(line)
        item_end = len(lines)
        for candidate in range(index + 1, len(lines)):
            candidate_line = lines[candidate]
            if _parse_key_value(candidate_line) is None:
                continue
            if _indent(candidate_line) <= item_indent:
                item_end = candidate
                break

        has_root_directory = False
        weekly_schedule = False
        for candidate in range(index + 1, item_end):
            candidate_line = lines[candidate]
            candidate_parsed = _parse_key_value(candidate_line)
            if candidate_parsed == ("directory", "/"):
                has_root_directory = True
            if candidate_parsed != ("schedule", ""):
                continue
            schedule_indent = _indent(candidate_line)
            for nested in range(candidate + 1, item_end):
                nested_line = lines[nested]
                nested_parsed = _parse_key_value(nested_line)
                if nested_parsed is None:
                    continue
                if _indent(nested_line) <= schedule_indent:
                    break
                if nested_parsed == ("interval", "weekly"):
                    weekly_schedule = True
                    break
        if has_root_directory and weekly_schedule:
            return True
    return False


def validate_repository(root: Path) -> List[str]:
    errors = []
    checkout_count = 0
    workflow_root = root / ".github" / "workflows"
    workflow_paths = sorted(workflow_root.glob("*.yml"))
    workflow_paths += sorted(workflow_root.glob("*.yaml"))

    for path in workflow_paths:
        source = path.read_text(encoding="utf-8")
        for line_number, reference in executable_uses(source):
            if not reference.startswith("actions/checkout@"):
                continue
            checkout_count += 1
            match = _CHECKOUT_PATTERN.fullmatch(reference)
            relative = path.relative_to(root)
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
    elif not _has_weekly_github_actions_update(
        dependabot_path.read_text(encoding="utf-8")
    ):
        errors.append(
            "dependabot.yml must define a weekly GitHub Actions update "
            "for directory /"
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
