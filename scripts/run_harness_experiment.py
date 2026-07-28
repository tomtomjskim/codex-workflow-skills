#!/usr/bin/env python3
"""Run the zero-model-call harness experiment preflight."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.live_eval.experiment import (  # noqa: E402
    ExperimentPreflightError,
    ExperimentPreflightRequest,
    ExperimentPreflightResult,
    run_experiment_preflight,
)
from scripts.live_eval.experiment_plan import (  # noqa: E402
    CanonicalExperimentInput,
    ExperimentPlanError,
    load_experiment_input,
)
from scripts.live_eval.experiment_receipts import (  # noqa: E402
    ExperimentReceiptError,
)
from scripts.live_eval.harness import HarnessError  # noqa: E402
from scripts.live_eval.task_snapshot import (  # noqa: E402
    TaskSnapshotError,
    TaskSourceSpec,
)


_MAX_INPUT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REQUIRED_TASK_SOURCE_COUNT = 4


class _CLIError(ValueError):
    """A fixed CLI input failure that retains no user-provided value."""


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, unused_message: str) -> None:
        raise _CLIError("invalid_arguments") from None


def _build_parser() -> _SanitizedArgumentParser:
    parser = _SanitizedArgumentParser(
        prog="run_harness_experiment.py",
        description=__doc__,
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        dest="root_help",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SanitizedArgumentParser,
    )
    preflight = commands.add_parser(
        "preflight",
        allow_abbrev=False,
        add_help=False,
        help="verify static Phase A evidence without model calls",
    )
    preflight.add_argument(
        "-h",
        "--help",
        action="store_true",
        dest="preflight_help",
    )
    preflight.add_argument("--input", required=True)
    preflight.add_argument("--bundle-root", required=True)
    preflight.add_argument("--skill-repo", required=True)
    preflight.add_argument("--temp-parent", required=True)
    preflight.add_argument(
        "--task-source",
        action="append",
        required=True,
    )
    return parser


def _print_help(
    parser: _SanitizedArgumentParser,
    command: Optional[str],
) -> None:
    if command is None:
        parser.print_help()
        return
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.choices[command].print_help()
            return
    _fail()


def _fail() -> None:
    raise _CLIError("invalid_input") from None


def _stat_fingerprint(metadata: os.stat_result) -> tuple:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_regular_single_link(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 0
        or metadata.st_size > _MAX_INPUT_BYTES
    ):
        _fail()


def _input_path(value: object) -> Path:
    if type(value) is not str or not value or "\0" in value:
        _fail()
    try:
        value.encode("utf-8")
        return Path(value)
    except (TypeError, UnicodeError, ValueError):
        _fail()


def _close_descriptor(descriptor: int) -> None:
    active = sys.exc_info()[1]
    try:
        os.close(descriptor)
    except BaseException:
        if active is not None and not isinstance(active, Exception):
            return
        raise


def _read_stable_input_file(path: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if (
        type(nofollow) is not int
        or nofollow == 0
        or type(nonblock) is not int
        or nonblock == 0
        or type(cloexec) is not int
        or cloexec == 0
    ):
        _fail()

    initial = os.lstat(path)
    _require_regular_single_link(initial)
    expected = _stat_fingerprint(initial)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | nonblock | cloexec,
        )
        opened = os.fstat(descriptor)
        _require_regular_single_link(opened)
        if _stat_fingerprint(opened) != expected:
            _fail()

        chunks = []
        total = 0
        while total <= _MAX_INPUT_BYTES:
            maximum = min(
                _READ_CHUNK_BYTES,
                _MAX_INPUT_BYTES + 1 - total,
            )
            chunk = os.read(descriptor, maximum)
            if not isinstance(chunk, bytes) or len(chunk) > maximum:
                _fail()
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_INPUT_BYTES:
            _fail()

        final_open = os.fstat(descriptor)
        final_named = os.lstat(path)
        _require_regular_single_link(final_open)
        _require_regular_single_link(final_named)
        if (
            _stat_fingerprint(final_open) != expected
            or _stat_fingerprint(final_named) != expected
            or total != initial.st_size
        ):
            _fail()
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            closing = descriptor
            descriptor = -1
            _close_descriptor(closing)


def _parse_task_source_bindings(
    values: object,
) -> Mapping[str, str]:
    if (
        type(values) is not list
        or len(values) != _REQUIRED_TASK_SOURCE_COUNT
        or any(type(value) is not str for value in values)
    ):
        _fail()

    parsed = []
    seen = set()
    for value in values:
        task_id, separator, raw_path = value.partition("=")
        if (
            separator != "="
            or _TASK_ID_PATTERN.fullmatch(task_id) is None
            or task_id in seen
        ):
            _fail()
        seen.add(task_id)
        parsed.append((task_id, raw_path))

    for unused_task_id, raw_path in parsed:
        if (
            not raw_path
            or "\0" in raw_path
            or not os.path.isabs(raw_path)
            or os.path.normpath(raw_path) != raw_path
        ):
            _fail()
        try:
            raw_path.encode("utf-8")
        except UnicodeError:
            _fail()
    return {task_id: raw_path for task_id, raw_path in parsed}


def _build_request(
    arguments: argparse.Namespace,
    experiment_input: CanonicalExperimentInput,
    bindings: Mapping[str, str],
) -> ExperimentPreflightRequest:
    candidates = tuple(experiment_input.value["candidates"])
    task_ids = tuple(candidate["task_id"] for candidate in candidates)
    if (
        len(candidates) != _REQUIRED_TASK_SOURCE_COUNT
        or len(set(task_ids)) != _REQUIRED_TASK_SOURCE_COUNT
        or set(bindings) != set(task_ids)
    ):
        _fail()
    sources = {
        candidate["task_id"]: TaskSourceSpec(
            input_digest=experiment_input.input_digest,
            task_id=candidate["task_id"],
            repository_root=Path(bindings[candidate["task_id"]]),
            commit_oid=candidate["commit_oid"],
            provisioning_class=candidate[
                "source_provisioning_class"
            ],
            operator_attested=candidate["operator_attested"],
            local_clone_policy=candidate["local_clone_policy"],
        )
        for candidate in candidates
    }
    return ExperimentPreflightRequest(
        experiment_input=experiment_input,
        bundle_root=Path(arguments.bundle_root),
        skill_repo=Path(arguments.skill_repo),
        temp_parent=Path(arguments.temp_parent),
        task_sources=sources,
    )


def _blocked_result(
    *,
    orchestration_started: bool = False,
) -> ExperimentPreflightResult:
    return ExperimentPreflightResult(
        status="blocked",
        live_backend_state="live_backend_not_implemented",
        global_agents_marker_state="global_agents_marker_not_run",
        pilot_state="pilot_not_run",
        qualification_evidence_classification="not_validated",
        model_calls=0,
        materialization_result="blocked",
        plan_digest=None,
        task_corpus_receipt_digest=None,
        preflight_receipt_digest=None,
        bundle_digest=None,
        current_profile_digest=None,
        lean_profile_digest=None,
        cleanup_state=(
            "cleanup_required"
            if orchestration_started
            else "not_started"
        ),
        reason_code=(
            "task_snapshot_cleanup_required"
            if orchestration_started
            else "experiment_preflight_invalid"
        ),
    )


def _result_json(result: ExperimentPreflightResult) -> str:
    return (
        json.dumps(
            asdict(result),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    orchestration_started = False
    try:
        values = list(sys.argv[1:] if argv is None else argv)
        parser = _build_parser()
        if (
            all(type(value) is str for value in values)
            and tuple(values)
            in (
                ("-h",),
                ("--help",),
                ("preflight", "-h"),
                ("preflight", "--help"),
            )
        ):
            _print_help(
                parser,
                "preflight" if values[0] == "preflight" else None,
            )
            return 0
        arguments = parser.parse_args(values)
        if arguments.root_help or arguments.preflight_help:
            _print_help(parser, arguments.command)
            return 0
        bindings = _parse_task_source_bindings(
            arguments.task_source
        )
        input_bytes = _read_stable_input_file(
            _input_path(arguments.input)
        )
        experiment_input = load_experiment_input(input_bytes)
        request = _build_request(
            arguments,
            experiment_input,
            bindings,
        )
        orchestration_started = True
        result = run_experiment_preflight(request)
    except (
        _CLIError,
        ExperimentPlanError,
        ExperimentPreflightError,
        ExperimentReceiptError,
        HarnessError,
        OSError,
        TaskSnapshotError,
        TypeError,
    ):
        result = _blocked_result(
            orchestration_started=orchestration_started
        )
    sys.stdout.write(_result_json(result))
    return 0 if result.status == "static_only" else 2


def _entrypoint(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return main(argv)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
