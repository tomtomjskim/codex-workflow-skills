#!/usr/bin/env python3
"""Dependency-free policy and tracked-file hygiene validation."""

import argparse
import codecs
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


SEVERITIES = frozenset(("HIGH", "MED", "LOW"))
DISPOSITIONS = frozenset(("apply", "ask", "defer", "reject-with-reason"))
VERIFICATION_RESULTS = frozenset(
    ("pass", "fail", "not_run", "blocked", "static_only", "partial")
)
_WEAK_ASSERTION_MARKERS = (
    "does not throw",
    "no exception",
    "smoke",
    "truthy",
    "page loads",
)
_HYGIENE_PATTERNS = (
    (re.compile(r"gh[opsu]_[A-Za-z0-9_]+"), "token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "token"),
    (re.compile(r"/U" r"sers/"), "private path"),
    (re.compile(r"BEGIN (?:RSA|OPENSSH|PRIVATE)"), "private key"),
    (
        re.compile(
            r"\b(?:TO" r"DO|T" r"BD|FIX" r"ME|PLACE" r"HOLDER)\b|\?\?\?"
        ),
        "placeholder",
    ),
)
_REPOSITORY_CONTRACT_SNIPPETS = {
    "skills/workflow-intake/references/parallel-coordination.md": (
        "current validation receipt",
        '"parallel_validation": "blocked"',
        '"execution": "sequential"',
        "CLI version 1",
        "output directory must not exist",
        "authoritative `--manifest`",
        "atomic no-replace publish is unavailable",
    ),
    "skills/workflow-intake/SKILL.md": (
        "references/parallel-coordination.md",
        "current validation receipt",
    ),
    "skills/workflow-intake/references/session-conduct.md": (
        "parallel-coordination",
        "single-owner sequential",
    ),
    "skills/workflow-intake/references/review-packet.md": (
        "coordination_receipt",
        "parallel_validation",
    ),
    "skills/adversarial-review-loop/references/reviewer-trigger-matrix.md": (
        "security-reviewer",
        "api-reviewer",
        "code-reviewer",
        "qa-engineer",
        "test-coverage-reviewer",
        "ux-reviewer",
        "accessibility-reviewer",
        "performance-reviewer",
        "architect",
        "dba",
    ),
    "scripts/validate_repo.sh": (
        "scripts/workflow",
        "scripts/validate_policy_contracts.py",
        "tests.test_policy_contracts",
        "tests.test_workflow_cli",
    ),
    "README.md": (
        "prepare-coordination",
        "validate-coordination",
        "validate-handoff",
        "sequential fallback",
        "output directory must not exist",
        "authoritative `--manifest`",
        "atomic no-replace publish is unavailable",
    ),
}


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _direct_finding_evidence(value: object) -> bool:
    source = value.get("source") if isinstance(value, dict) else None
    direct_source = isinstance(source, str) and (
        re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*:[1-9][0-9]*", source
        )
        is not None
        or re.fullmatch(r"command:\S(?:.*\S)?", source) is not None
        or re.fullmatch(r"artifact:sha256:[0-9a-f]{64}", source) is not None
    )
    return (
        isinstance(value, dict)
        and _nonempty(value.get("observed_problem"))
        and _nonempty(value.get("failure_mode"))
        and direct_source
    )


def validate_review_sample(sample: dict) -> List[str]:
    """Reject unsupported severity, invalid enums, and weak verification claims."""
    if not isinstance(sample, dict):
        return ["review sample must be an object"]

    errors = []
    severity = sample.get("severity")
    if severity not in SEVERITIES:
        errors.append("invalid severity: {}".format(severity))
    if (
        severity == "HIGH"
        and not _direct_finding_evidence(sample.get("finding_evidence"))
        and sample.get("verification_status") != "needs-investigation"
    ):
        errors.append("HIGH requires direct evidence or needs-investigation")

    disposition = sample.get("disposition")
    if disposition not in DISPOSITIONS:
        errors.append("invalid disposition: {}".format(disposition))

    verification = sample.get("verification_evidence")
    if not isinstance(verification, dict):
        errors.append("verification_evidence must be an object")
        return errors
    result = verification.get("result")
    if result not in VERIFICATION_RESULTS:
        errors.append("invalid verification result: {}".format(result))
    assertion = verification.get("assertion_strength")
    if result == "pass" and (
        not _nonempty(assertion)
        or any(marker in assertion.casefold() for marker in _WEAK_ASSERTION_MARKERS)
    ):
        errors.append("pass requires failure-mode assertion strength")
    return errors


def validate_repository_contracts(repo_root: Path) -> List[str]:
    """Require stable workflow dispatch and canonical reviewer contracts."""
    root = Path(repo_root)
    errors = []
    for relative_path, snippets in _REPOSITORY_CONTRACT_SNIPPETS.items():
        path = root / relative_path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            errors.append("{}: required policy file unavailable: {}".format(relative_path, error))
            continue
        for snippet in snippets:
            if snippet not in text:
                errors.append("{}: missing policy contract: {}".format(relative_path, snippet))
    return errors


def _tracked_paths(repo_root: Path) -> List[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError("git ls-files failed: {}".format(message))
    return [repo_root / Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw]


def _scan_line(line: str, relative_path: Path, line_number: int) -> List[str]:
    return [
        "{}:{}: {}".format(relative_path, line_number, label)
        for pattern, label in _HYGIENE_PATTERNS
        if pattern.search(line)
    ]


def _scan_file_stream(stream, relative_path: Path, chunk_size: int = 65536) -> List[str]:
    """Scan UTF-8 chunks, stopping immediately when the initial chunk is binary."""
    first = stream.read(chunk_size)
    if b"\0" in first:
        return []
    decoder = codecs.getincrementaldecoder("utf-8")()
    errors = []
    pending = ""
    line_number = 0
    chunk = first
    try:
        while chunk:
            pending += decoder.decode(chunk)
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                line_number += 1
                errors.extend(_scan_line(line, relative_path, line_number))
            chunk = stream.read(chunk_size)
        pending += decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return []
    if pending:
        errors.extend(_scan_line(pending, relative_path, line_number + 1))
    return errors


def scan_tracked_text_files(repo_root: Path) -> List[str]:
    """Use git ls-files -z, skip binary files, and scan every tracked text file."""
    root = Path(repo_root)
    errors = []
    for path in _tracked_paths(root):
        relative_path = path.relative_to(root)
        try:
            file_mode = path.lstat().st_mode
        except OSError as error:
            errors.append("{}: cannot inspect tracked file: {}".format(relative_path, error))
            continue
        if stat.S_ISLNK(file_mode):
            try:
                target = os.readlink(str(path))
            except OSError as error:
                errors.append("{}: cannot read symlink: {}".format(relative_path, error))
                continue
            for line_number, line in enumerate(target.splitlines() or [target], 1):
                errors.extend(_scan_line(line, relative_path, line_number))
            continue
        if not stat.S_ISREG(file_mode):
            continue
        try:
            with path.open("rb") as stream:
                errors.extend(_scan_file_stream(stream, relative_path))
        except OSError as error:
            errors.append("{}: cannot read tracked file: {}".format(relative_path, error))
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--review-sample", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    errors = []
    try:
        errors.extend(scan_tracked_text_files(args.repo_root))
        errors.extend(validate_repository_contracts(args.repo_root))
        for sample_path in args.review_sample:
            with sample_path.open(encoding="utf-8") as stream:
                sample = json.load(stream)
            samples = sample.get("findings", []) if isinstance(sample, dict) and "findings" in sample else [sample]
            for index, finding in enumerate(samples):
                errors.extend(
                    "{} finding {}: {}".format(sample_path, index, error)
                    for error in validate_review_sample(finding)
                )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(str(error))

    payload = {"schema_version": 1, "status": "error" if errors else "ok", "errors": errors}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif errors:
        for error in errors:
            print("error: {}".format(error), file=sys.stderr)
    else:
        print("ok: policy contracts passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
