#!/usr/bin/env python3
"""Observe host-specific native readiness without model calls."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.live_eval.native_canary_readiness import (  # noqa: E402
    NativeCanaryReadinessRequest,
    NativeCanaryReadinessResult,
    run_native_canary_readiness,
)


class _CLIError(ValueError):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, unused_message: str) -> None:
        raise _CLIError("invalid_arguments") from None


def _build_parser() -> _SanitizedArgumentParser:
    parser = _SanitizedArgumentParser(
        prog="run_harness_canary_readiness.py",
        description=__doc__,
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--codex-executable", required=True)
    parser.add_argument("--python-executable", required=True)
    parser.add_argument("--temp-parent", required=True)
    parser.add_argument("--private-root", required=True)
    return parser


def _blocked_result() -> NativeCanaryReadinessResult:
    return NativeCanaryReadinessResult(
        status="blocked",
        model_calls=0,
        policy_digest="sha256:" + "0" * 64,
        codex_executable_identity_digest=None,
        python_executable_identity_digest=None,
        permission_profile_evidence_digest=None,
        supervisor_environment_evidence_digest=None,
        ledger_probe_evidence_digest=None,
        complete_evidence_digest=None,
        cleanup_state="not_started",
        reason_code="request_invalid",
    )


def _write_result(result: NativeCanaryReadinessResult) -> None:
    print(
        json.dumps(
            asdict(result),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def main(arguments: Sequence[str] = None) -> int:
    parser = _build_parser()
    parsed_arguments = tuple(sys.argv[1:] if arguments is None else arguments)
    if parsed_arguments in (("-h",), ("--help",)):
        parser.print_help()
        return 0
    try:
        namespace = parser.parse_args(parsed_arguments)
        request = NativeCanaryReadinessRequest(
            codex_executable=Path(namespace.codex_executable),
            python_executable=Path(namespace.python_executable),
            temp_parent=Path(namespace.temp_parent),
            private_root=Path(namespace.private_root),
        )
        result = run_native_canary_readiness(request)
    except _CLIError:
        result = _blocked_result()
    except Exception:
        result = _blocked_result()
    _write_result(result)
    return 0 if result.status == "native_primitive_observations_only" else 2


def _entrypoint(arguments: Sequence[str] = None) -> int:
    try:
        return main(arguments)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
