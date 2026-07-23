#!/usr/bin/env python3
"""Run isolated workflow live evaluations or deterministic planning preflights."""

import argparse
from dataclasses import asdict, dataclass, field, replace
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterable, Mapping, Optional, Protocol, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.live_eval.artifacts import RedactingWriter, RedactionError
from scripts.live_eval.budget import Budget, BudgetExceeded
from scripts.live_eval.checkout import (
    CheckoutManifest,
    install_checkout_skills,
    verify_loaded_checkout,
)
from scripts.live_eval.harness import (
    materialize_harness_home,
    verify_loaded_harness,
)
from scripts.live_eval.isolation import (
    CliCapabilities,
    EvalConfig,
    Invocation,
    build_invocation,
    preflight_auth,
    preflight_isolation,
    seal_codex_home,
    verify_codex_home_seal,
)
from scripts.live_eval.scenarios import (
    AssertionReport,
    Scenario,
    assert_response,
    load_scenarios,
    select_scenarios,
)


DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_MODELS = (DEFAULT_MODEL,)
DEFAULT_API_KEY_ENV_NAME = "OPENAI_API_KEY"
TARGETED_MAX_SCENARIOS = 3
RELEASE_MAX_SCENARIOS = 26
CHECKOUT_ENTRIES = (".live-eval-checkout.json", "skills")
REQUIRED_FLAGS = frozenset(
    {
        "-a",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        "--sandbox",
        "--strict-config",
        "-c",
    }
)


@dataclass(frozen=True)
class ProcessOutput:
    events: Tuple[bytes, ...] = field(repr=False)

    def __post_init__(self) -> None:
        values = tuple(self.events)
        if any(not isinstance(item, bytes) for item in values):
            raise TypeError("process events must be bytes")
        object.__setattr__(self, "events", values)


class RetryableTransportFailure(RuntimeError):
    """A sanitized, retryable process or transport failure."""

    def __init__(self) -> None:
        super().__init__("live eval invocation failed")


class ProcessExecutionFailure(RuntimeError):
    """A sanitized, non-retryable completed-process failure."""

    def __init__(self) -> None:
        super().__init__("live eval process execution failed")


class InvocationTimeout(RuntimeError):
    """A sanitized, non-retryable process timeout."""

    def __init__(self) -> None:
        super().__init__("live eval invocation timed out")


class OutputLimit(RuntimeError):
    def __init__(self) -> None:
        super().__init__("live eval output limit exceeded")


class InputLimit(RuntimeError):
    def __init__(self) -> None:
        super().__init__("live eval input limit exceeded")


class OutputProtocolError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("live eval output protocol is invalid")


class IsolationChanged(RuntimeError):
    """The sealed invocation changed after preflight and before consumption."""

    def __init__(self) -> None:
        super().__init__("live eval isolation changed before invocation")


InvocationFailure = RetryableTransportFailure


class CodexProcess(Protocol):
    def probe(self, invocation: Invocation) -> CliCapabilities:
        ...

    def invoke(
        self,
        invocation: Invocation,
        prompt: str,
        timeout_seconds: int,
        *,
        max_stdin_bytes: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessOutput:
        ...


@dataclass(frozen=True)
class EvalRequest:
    scenario_ids: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    release_suite: bool = False
    release_approved: bool = False
    model: str = DEFAULT_MODEL
    dry_run_only: bool = False
    repo_root: Optional[Path] = None
    scenario_path: Optional[Path] = None
    temp_root: Optional[Path] = None
    codex_executable: Optional[Path] = None
    api_key: Optional[str] = field(default=None, repr=False)
    api_key_env_name: str = DEFAULT_API_KEY_ENV_NAME
    model_allowlist: Tuple[str, ...] = DEFAULT_MODELS
    max_stdin_bytes: int = Budget.TARGETED_MAX_RAW_BYTES
    max_stdout_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_ids", tuple(self.scenario_ids))
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "model_allowlist", tuple(self.model_allowlist))
        for name in ("repo_root", "scenario_path", "temp_root", "codex_executable"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).absolute())

    @classmethod
    def dry_run(
        cls,
        *,
        scenario_ids: Iterable[str] = (),
        tags: Iterable[str] = (),
        release_suite: bool = False,
        model: str = DEFAULT_MODEL,
        scenario_path: Optional[Path] = None,
    ) -> "EvalRequest":
        return cls(
            scenario_ids=tuple(scenario_ids),
            tags=tuple(tags),
            release_suite=release_suite,
            model=model,
            dry_run_only=True,
            scenario_path=scenario_path,
        )

    @classmethod
    def targeted(cls, **values: object) -> "EvalRequest":
        return cls(**values)


@dataclass(frozen=True)
class HarnessDryRunRequest:
    planning_request: EvalRequest
    profile: str
    bundle_root: Path = field(repr=False)
    skill_repo: Path = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.planning_request, EvalRequest):
            raise TypeError("planning request must be EvalRequest")
        if self.planning_request.dry_run_only is not True:
            raise ValueError("planning request must be dry-run only")
        if self.profile not in ("current", "lean"):
            raise ValueError("harness profile must be current or lean")
        object.__setattr__(self, "bundle_root", Path(self.bundle_root).absolute())
        object.__setattr__(self, "skill_repo", Path(self.skill_repo).absolute())


@dataclass(frozen=True)
class HarnessManifestSummary:
    bundle_id: str
    bundle_digest: str
    profile: str
    agents_hash: str
    skill_routing_hash: str
    adapter_source_hash: str
    adapter_materialized_hash: str
    common_role_hash: str
    home_digest: str
    adapter_count: int
    role_count: int


@dataclass(frozen=True)
class HarnessDryRunResult:
    status: str
    materialization_result: str
    model_conformance: str
    model_calls: int
    scenario_ids: Tuple[str, ...]
    manifest: Optional[HarnessManifestSummary]
    retention: str
    manual_cleanup_required: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_ids", tuple(self.scenario_ids))


@dataclass(frozen=True)
class RunManifest:
    scenario_ids: Tuple[str, ...]
    model: str
    dry_run: bool
    checkout_tree_hash: Optional[str]


@dataclass(frozen=True)
class ScenarioEvalResult:
    scenario_id: str
    status: str
    verification_result: str
    attempts: int
    model_calls: int
    assertion_report: Optional[AssertionReport] = None
    artifact_path: Optional[Path] = None
    retention: str = "none"
    manual_cleanup_required: bool = False
    retained_paths: Tuple[Path, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    status: str
    verification_result: str
    attempts: int
    model_calls: int
    manifest: RunManifest
    scenarios: Tuple[ScenarioEvalResult, ...] = ()
    retention: str = "none"
    manual_cleanup_required: bool = False
    retained_paths: Tuple[Path, ...] = ()


def _default_scenario_path() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "live-eval-scenarios.json"


def _validate_and_select(request: EvalRequest) -> Tuple[Scenario, ...]:
    if not isinstance(request, EvalRequest):
        raise TypeError("request must be EvalRequest")
    if not isinstance(request.model, str) or not request.model.strip():
        raise ValueError("model must be a non-empty string")
    if request.model not in request.model_allowlist:
        raise ValueError("model must be present in the model allowlist")
    if (
        not isinstance(request.max_stdin_bytes, int)
        or isinstance(request.max_stdin_bytes, bool)
        or not 1 <= request.max_stdin_bytes <= Budget.TARGETED_MAX_RAW_BYTES
    ):
        raise ValueError("stdin byte limit must be within the targeted policy")
    modes = sum(bool(value) for value in (request.scenario_ids, request.tags, request.release_suite))
    if modes > 1:
        raise ValueError("scenario IDs, tags, and release suite are mutually exclusive")
    if not isinstance(request.release_approved, bool):
        raise ValueError("release approval must be a boolean")
    if request.release_approved and not request.release_suite:
        raise ValueError("release suite approval requires release suite selection")
    if request.release_suite and not request.dry_run_only and not request.release_approved:
        raise ValueError("live release suite approval is required")
    corpus = load_scenarios(request.scenario_path or _default_scenario_path())
    if request.scenario_ids:
        if len(set(request.scenario_ids)) != len(request.scenario_ids):
            raise ValueError("scenario IDs must be unique")
        try:
            selected = tuple(corpus[item] for item in request.scenario_ids)
        except KeyError as error:
            raise ValueError("unknown scenario ID") from None
    elif request.release_suite:
        if len(corpus) > RELEASE_MAX_SCENARIOS:
            raise ValueError("release scenario limit exceeded")
        selected = select_scenarios(corpus, (), limit=RELEASE_MAX_SCENARIOS)
    else:
        selected = select_scenarios(
            corpus, request.tags, limit=TARGETED_MAX_SCENARIOS
        )
    if not selected:
        raise ValueError("no scenarios selected")
    if not request.release_suite and len(selected) > TARGETED_MAX_SCENARIOS:
        raise ValueError("targeted scenario limit exceeded")
    return selected


def run_harness_dry_run(request: HarnessDryRunRequest) -> HarnessDryRunResult:
    """Plan, materialize, verify, and clean up without invoking a model."""
    if not isinstance(request, HarnessDryRunRequest):
        raise TypeError("request must be HarnessDryRunRequest")
    selected = _validate_and_select(request.planning_request)
    scenario_ids = tuple(item.scenario_id for item in selected)
    result = _blocked_harness_result(
        scenario_ids, "blocked_isolation", "materialized_harness_mismatch"
    )
    temp_root = None
    codex_home = None
    root_identity = None
    home_identity = None
    try:
        temp_root = Path(
            tempfile.mkdtemp(prefix="codex-harness-preflight-root-")
        ).absolute()
        temp_root.chmod(0o700)
        root_identity = _harness_directory_identity(temp_root)
        codex_home = temp_root / "home"
        resolved_root = temp_root.resolve(strict=True)
        if _harness_directory_identity(resolved_root) != root_identity:
            raise ValueError("owned harness path identity changed")
        temp_root = resolved_root
        codex_home = temp_root / "home"
        codex_home.mkdir(mode=0o700)
        codex_home.chmod(0o700)
        home_identity = _harness_directory_identity(codex_home)
        manifest = materialize_harness_home(
            request.skill_repo,
            request.bundle_root,
            request.profile,
            codex_home,
        )
        verification = verify_loaded_harness(
            request.skill_repo, codex_home, manifest
        )
        if (
            verification.classification == "ready"
            and verification.result == "pass"
            and verification.reason == "fixed_inventory_verified"
            and verification.manifest == manifest
        ):
            result = HarnessDryRunResult(
                status="harness_preflight_only",
                materialization_result="pass",
                model_conformance="not_run",
                model_calls=0,
                scenario_ids=scenario_ids,
                manifest=_harness_manifest_summary(manifest),
                retention="none",
                manual_cleanup_required=False,
                reason="fixed_inventory_verified",
            )
        else:
            result = _blocked_harness_result(
                scenario_ids,
                "blocked_isolation",
                _fixed_harness_reason(verification.reason),
            )
    except ValueError as error:
        result = _blocked_harness_result(
            scenario_ids,
            "blocked_isolation",
            _fixed_harness_reason(getattr(error, "reason", str(error))),
        )
    except (OSError, TypeError):
        result = _blocked_harness_result(
            scenario_ids, "blocked_isolation", "materialized_harness_mismatch"
        )
    finally:
        if temp_root is not None:
            cleanup_verified = False
            if root_identity is not None and codex_home is not None:
                try:
                    cleanup_verified = _cleanup_harness_temp_root(
                        temp_root,
                        root_identity,
                        codex_home,
                        home_identity,
                    )
                except (OSError, TypeError, ValueError):
                    cleanup_verified = False
            if not cleanup_verified:
                result = replace(
                    result,
                    status="blocked_cleanup",
                    materialization_result="blocked",
                    retention="cleanup_required",
                    manual_cleanup_required=True,
                    reason="cleanup_unverified",
                )
    return result


def _harness_manifest_summary(manifest: object) -> HarnessManifestSummary:
    return HarnessManifestSummary(
        bundle_id=manifest.bundle_id,
        bundle_digest=manifest.bundle_digest,
        profile=manifest.profile,
        agents_hash=manifest.agents_hash,
        skill_routing_hash=manifest.skill_routing_hash,
        adapter_source_hash=manifest.adapter_source_hash,
        adapter_materialized_hash=manifest.adapter_materialized_hash,
        common_role_hash=manifest.common_role_hash,
        home_digest=manifest.home_digest,
        adapter_count=manifest.adapter_count,
        role_count=manifest.role_count,
    )


def _blocked_harness_result(
    scenario_ids: Iterable[str], status: str, reason: str
) -> HarnessDryRunResult:
    return HarnessDryRunResult(
        status=status,
        materialization_result="blocked",
        model_conformance="not_run",
        model_calls=0,
        scenario_ids=tuple(scenario_ids),
        manifest=None,
        retention="none",
        manual_cleanup_required=False,
        reason=reason,
    )


def _fixed_harness_reason(value: object) -> str:
    reasons = frozenset(
        {
            "invalid_bundle",
            "source_changed",
            "skill_checkout_mismatch",
            "materialized_harness_mismatch",
            "home_seal_mismatch",
            "cleanup_unverified",
        }
    )
    return value if isinstance(value, str) and value in reasons else "invalid_bundle"


def _harness_directory_identity(path: Path) -> Tuple[int, int, int]:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("owned harness path is not a private directory")
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _cleanup_harness_temp_root(
    temp_root: Path,
    root_identity: Tuple[int, int, int],
    codex_home: Path,
    home_identity: Optional[Tuple[int, int, int]],
) -> bool:
    """Remove only the still-owned home/root identities and prove absence."""
    try:
        if _harness_directory_identity(temp_root) != root_identity:
            return False
        if home_identity is None:
            if not _path_is_absent(codex_home):
                return False
        else:
            if _harness_directory_identity(codex_home) != home_identity:
                return False
            _remove_harness_tree(codex_home)
            if not _path_is_absent(codex_home):
                return False
        if _harness_directory_identity(temp_root) != root_identity:
            return False
        temp_root.rmdir()
        return _path_is_absent(temp_root)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False


def _remove_harness_tree(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        path.chmod(0o700)
        names = tuple(entry.name for entry in os.scandir(str(path)))
        for name in names:
            _remove_harness_tree(path / name)
        path.rmdir()
    else:
        path.unlink()


def _path_is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def run_eval(request: EvalRequest, codex: CodexProcess) -> EvalResult:
    """Preflight, select, invoke, redact, assert, and report safely."""
    selected = _validate_and_select(request)
    manifest = RunManifest(
        scenario_ids=tuple(item.scenario_id for item in selected),
        model=request.model,
        dry_run=request.dry_run_only,
        checkout_tree_hash=None,
    )
    if request.dry_run_only:
        return EvalResult("preflight_only", "not_run", 0, 0, manifest)

    auth = preflight_auth(request.api_key, oauth_files_present=False)
    if auth.classification != "ready":
        return EvalResult("blocked_auth", "blocked", 0, 0, manifest)
    if request.api_key_env_name != DEFAULT_API_KEY_ENV_NAME:
        return EvalResult("blocked_auth", "blocked", 0, 0, manifest)
    if request.model not in request.model_allowlist:
        return EvalResult("blocked_model", "blocked", 0, 0, manifest)
    if any(
        value is None
        for value in (request.repo_root, request.temp_root, request.codex_executable)
    ):
        return EvalResult("blocked_preflight", "blocked", 0, 0, manifest)

    budget = Budget.release_suite() if request.release_suite else Budget.targeted()
    results = []
    tree_hash = None
    for scenario in selected:
        scenario_result, checkout_manifest = _run_scenario(
            request, scenario, codex, budget
        )
        results.append(scenario_result)
        if checkout_manifest is not None:
            if tree_hash is None:
                tree_hash = checkout_manifest.tree_hash
            elif tree_hash != checkout_manifest.tree_hash:
                return _aggregate(
                    request, selected, results, "blocked_checkout", "blocked", tree_hash
                )
        if scenario_result.verification_result != "pass":
            break
    return _aggregate(request, selected, results, None, None, tree_hash)


def _run_scenario(
    request: EvalRequest,
    scenario: Scenario,
    codex: CodexProcess,
    budget: Budget,
) -> Tuple[ScenarioEvalResult, Optional[CheckoutManifest]]:
    invocation = None
    try:
        invocation = build_invocation(
            EvalConfig(
                codex_executable=request.codex_executable,
                model=request.model,
                model_allowlist=request.model_allowlist,
                temp_root=request.temp_root,
                api_key=request.api_key,
                api_key_env_name=request.api_key_env_name,
            )
        )
        checkout_manifest = install_checkout_skills(
            request.repo_root, invocation.codex_home
        )
        checkout = verify_loaded_checkout(request.repo_root, invocation.codex_home)
        if checkout.classification != "ready" or checkout.manifest != checkout_manifest:
            raise ValueError("exact checkout verification failed")
        seal = seal_codex_home(invocation.codex_home, CHECKOUT_ENTRIES)
        isolation = preflight_isolation(
            invocation,
            probe=codex.probe,
            expected_codex_home_seal=seal,
        )
    except Exception:
        return _blocked_scenario_result(
            scenario, "blocked_isolation", 0, 0, invocation, None
        )
    if (
        isolation.classification != "ready"
        or isolation.invocation_instance_id != id(invocation)
    ):
        return _blocked_scenario_result(
            scenario, "blocked_isolation", 0, 0, invocation, checkout_manifest
        )

    attempts = 0
    model_calls = 0
    had_infrastructure_failure = False
    while attempts < 2:
        try:
            with budget.acquire_call():
                if not _consumption_ready(
                    request, invocation, seal, checkout_manifest, codex
                ):
                    raise IsolationChanged()
                attempts += 1
                model_calls += 1
                output = codex.invoke(
                    invocation,
                    scenario.prompt,
                    scenario.timeout_seconds,
                    max_stdin_bytes=request.max_stdin_bytes,
                    max_stdout_bytes=request.max_stdout_bytes,
                    max_stderr_bytes=request.max_stderr_bytes,
                )
            artifact = _retain_events(request, invocation, scenario, attempts, output)
            response = _final_response(artifact)
        except IsolationChanged:
            return _blocked_scenario_result(
                scenario,
                "blocked_isolation",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except InvocationTimeout:
            return _blocked_scenario_result(
                scenario,
                "blocked_timeout",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except OutputLimit:
            return _blocked_scenario_result(
                scenario,
                "blocked_output_limit",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except InputLimit:
            return _blocked_scenario_result(
                scenario,
                "blocked_input_limit",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except OutputProtocolError:
            return _blocked_scenario_result(
                scenario,
                "blocked_output_protocol",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except ProcessExecutionFailure:
            return _blocked_scenario_result(
                scenario,
                "blocked_process",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except RedactionError:
            return _blocked_scenario_result(
                scenario,
                "blocked_redaction",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except BudgetExceeded as error:
            return _blocked_scenario_result(
                scenario,
                error.decision.value,
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except RetryableTransportFailure:
            if attempts < 2:
                had_infrastructure_failure = True
                continue
            return _blocked_scenario_result(
                scenario,
                "blocked_infrastructure",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        except Exception:
            return _blocked_scenario_result(
                scenario,
                "blocked_internal",
                attempts,
                model_calls,
                invocation,
                checkout_manifest,
            )
        report = assert_response(scenario, response)
        if not report.passed:
            return (
                ScenarioEvalResult(
                    scenario.scenario_id,
                    "failed_assertion",
                    "fail",
                    attempts,
                    model_calls,
                    report,
                    artifact,
                    "retained_redacted",
                    True,
                ),
                checkout_manifest,
            )
        if had_infrastructure_failure:
            return (
                ScenarioEvalResult(
                    scenario.scenario_id,
                    "blocked_infrastructure",
                    "blocked",
                    attempts,
                    model_calls,
                    report,
                    artifact,
                    "retained_redacted",
                    True,
                ),
                checkout_manifest,
            )
        return (
            ScenarioEvalResult(
                scenario.scenario_id,
                scenario.expected_status,
                "pass",
                attempts,
                model_calls,
                report,
                artifact,
                "retained_redacted",
                True,
            ),
            checkout_manifest,
        )
    raise AssertionError("unreachable")


def _blocked_scenario_result(
    scenario: Scenario,
    status: str,
    attempts: int,
    model_calls: int,
    invocation: Optional[Invocation],
    checkout_manifest: Optional[CheckoutManifest],
) -> Tuple[ScenarioEvalResult, Optional[CheckoutManifest]]:
    retained_paths = (
        _cleanup_invocation_run(invocation) if invocation is not None else ()
    )
    return (
        ScenarioEvalResult(
            scenario.scenario_id,
            status,
            "blocked",
            attempts,
            model_calls,
            retention="cleanup_required" if retained_paths else "none",
            manual_cleanup_required=bool(retained_paths),
            retained_paths=retained_paths,
        ),
        checkout_manifest,
    )


def _cleanup_invocation_run(invocation: Invocation) -> Tuple[Path, ...]:
    run_dir = invocation.run_dir
    try:
        residual = Path(invocation.path_identities["run_dir"].resolved)
    except (AttributeError, KeyError, TypeError, ValueError):
        residual = Path(run_dir.name)
    try:
        expected = invocation.path_identities["run_dir"]
        metadata = run_dir.lstat()
        if (
            metadata.st_dev != expected.device
            or metadata.st_ino != expected.inode
            or str(run_dir.resolve(strict=True)) != expected.resolved
        ):
            return (residual,)
        for current, names, files in os.walk(str(run_dir), topdown=False):
            directory = Path(current)
            directory.chmod(0o700)
            for name in files:
                candidate = directory / name
                if candidate.is_symlink():
                    candidate.unlink()
                else:
                    candidate.chmod(0o600)
                    candidate.unlink()
            for name in names:
                candidate = directory / name
                if candidate.is_symlink():
                    candidate.unlink()
                else:
                    candidate.chmod(0o700)
                    candidate.rmdir()
        run_dir.rmdir()
        return () if not run_dir.exists() else (residual,)
    except (KeyError, OSError, ValueError):
        return (residual,)


def _consumption_ready(
    request: EvalRequest,
    invocation: Invocation,
    seal: object,
    checkout_manifest: CheckoutManifest,
    codex: CodexProcess,
) -> bool:
    checkout = verify_loaded_checkout(request.repo_root, invocation.codex_home)
    if checkout.classification != "ready" or checkout.manifest != checkout_manifest:
        return False
    if not verify_codex_home_seal(invocation, seal):
        return False
    report = preflight_isolation(
        invocation,
        probe=codex.probe,
        expected_codex_home_seal=seal,
    )
    return (
        report.classification == "ready"
        and report.invocation_instance_id == id(invocation)
    )


def _retain_events(
    request: EvalRequest,
    invocation: Invocation,
    scenario: Scenario,
    attempt: int,
    output: ProcessOutput,
) -> Path:
    if not isinstance(output, ProcessOutput):
        raise OutputProtocolError()
    writer = RedactingWriter(
        invocation.run_dir,
        {request.api_key_env_name: request.api_key or ""},
        artifact_name="{}-attempt-{}.jsonl".format(scenario.scenario_id, attempt),
        max_raw_bytes=Budget.TARGETED_MAX_RAW_BYTES,
    )
    try:
        for chunk in output.events:
            writer.write(chunk)
        return writer.finalize()
    except RedactionError:
        writer.abort()
        raise
    except OSError:
        writer.abort()
        raise OutputProtocolError() from None


def _final_response(artifact: Path) -> Mapping[str, object]:
    try:
        candidates = []
        lifecycle_types = {
            "thread.started",
            "turn.started",
            "turn.completed",
            "item.started",
            "item.updated",
        }
        for line in artifact.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("invalid event")
            event_type = event["type"]
            if event_type == "final_response":
                if set(event) != {"type", "response"} or not isinstance(event["response"], dict):
                    raise ValueError("invalid final response")
                candidates.append(event["response"])
            elif event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ValueError("invalid completed item")
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if not isinstance(text, str):
                        raise ValueError("invalid agent message")
                    candidate = json.loads(text)
                    if not isinstance(candidate, dict):
                        raise ValueError("invalid structured response")
                    candidates.append(candidate)
            elif event_type not in lifecycle_types:
                raise ValueError("unsupported event")
        if len(candidates) != 1:
            raise ValueError("missing structured response")
        return candidates[0]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise OutputProtocolError() from None


def _aggregate(
    request: EvalRequest,
    selected: Sequence[Scenario],
    results: Sequence[ScenarioEvalResult],
    forced_status: Optional[str],
    forced_verification: Optional[str],
    tree_hash: Optional[str],
) -> EvalResult:
    result_values = tuple(results)
    verification = forced_verification
    if verification is None:
        if any(item.verification_result == "blocked" for item in result_values):
            verification = "blocked"
        elif any(item.verification_result == "fail" for item in result_values):
            verification = "fail"
        else:
            verification = "pass"
    status = forced_status
    if status is None:
        if len(result_values) == 1:
            status = result_values[0].status
        elif verification == "pass" and len(result_values) == len(selected):
            status = "pass"
        else:
            status = next(
                (item.status for item in result_values if item.verification_result != "pass"),
                "blocked_incomplete",
            )
    manifest = RunManifest(
        tuple(item.scenario_id for item in selected),
        request.model,
        request.dry_run_only,
        tree_hash,
    )
    retained_paths = tuple(
        Path(value)
        for value in sorted(
            {
                str(path)
                for item in result_values
                for path in item.retained_paths
            }
        )
    )
    if any(item.retention == "cleanup_required" for item in result_values):
        retention = "cleanup_required"
    elif any(item.retention == "retained_redacted" for item in result_values):
        retention = "retained_redacted"
    else:
        retention = "none"
    return EvalResult(
        status,
        verification,
        sum(item.attempts for item in result_values),
        sum(item.model_calls for item in result_values),
        manifest,
        result_values,
        retention,
        any(item.manual_cleanup_required for item in result_values),
        retained_paths,
    )


class SubprocessCodex:
    """Network-capable implementation used only by an explicit live CLI run."""

    def probe(self, invocation: Invocation) -> CliCapabilities:
        probe_env = dict(invocation.tool_env)
        probe_env["CODEX_HOME"] = str(invocation.codex_home)
        try:
            version_output, _ = _bounded_process(
                (str(invocation.executable), "--version"),
                env=probe_env,
                cwd=invocation.cwd,
                input_bytes=b"",
                timeout_seconds=10,
                max_stdin_bytes=1,
                max_stdout_bytes=128 * 1024,
                max_stderr_bytes=32 * 1024,
            )
            help_output, _ = _bounded_process(
                (str(invocation.executable), "--help"),
                env=probe_env,
                cwd=invocation.cwd,
                input_bytes=b"",
                timeout_seconds=10,
                max_stdin_bytes=1,
                max_stdout_bytes=128 * 1024,
                max_stderr_bytes=32 * 1024,
            )
            exec_help, _ = _bounded_process(
                (str(invocation.executable), "exec", "--help"),
                env=probe_env,
                cwd=invocation.cwd,
                input_bytes=b"",
                timeout_seconds=10,
                max_stdin_bytes=1,
                max_stdout_bytes=128 * 1024,
                max_stderr_bytes=32 * 1024,
            )
            version_output = version_output.decode("utf-8", "strict")
            help_output = help_output.decode("utf-8", "strict")
            exec_help = exec_help.decode("utf-8", "strict")
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_output)
            if match is None:
                raise ValueError("unrecognized Codex version")
            combined_help = help_output + "\n" + exec_help
            supported = frozenset(
                flag for flag in REQUIRED_FLAGS if flag in combined_help
            )
        except RetryableTransportFailure:
            raise
        except Exception:
            raise ProcessExecutionFailure() from None
        return CliCapabilities(
            selected_executable=invocation.executable,
            selected_executable_identity=invocation.executable_identity,
            cli_version=tuple(int(item) for item in match.groups()),
            supported_flags=supported,
            argv_digest=invocation.argv_digest,
            child_env_policy_id=invocation.child_env_policy_id,
            child_env_policy_digest=invocation.child_env_policy_digest,
            non_profile_child_env=True,
            network_disabled=False,
            mcp_disabled=False,
            plugins_disabled=False,
            hooks_disabled=False,
            unexpected_skills_absent=False,
        )

    def invoke(
        self,
        invocation: Invocation,
        prompt: str,
        timeout_seconds: int,
        *,
        max_stdin_bytes: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessOutput:
        try:
            stdout, returncode = _bounded_process(
                invocation.argv,
                cwd=str(invocation.cwd),
                env=dict(invocation.transport_env),
                input_bytes=prompt.encode("utf-8"),
                timeout_seconds=timeout_seconds,
                max_stdin_bytes=max_stdin_bytes,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
        except (InvocationTimeout, InputLimit, OutputLimit):
            raise
        except RetryableTransportFailure:
            raise
        except Exception:
            raise ProcessExecutionFailure() from None
        if returncode != 0:
            raise ProcessExecutionFailure()
        return ProcessOutput(tuple(stdout.splitlines(keepends=True)))


def _bounded_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    input_bytes: bytes,
    timeout_seconds: float,
    max_stdin_bytes: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> Tuple[bytes, int]:
    if (
        not isinstance(input_bytes, bytes)
        or not isinstance(max_stdin_bytes, int)
        or isinstance(max_stdin_bytes, bool)
        or max_stdin_bytes < 0
        or len(input_bytes) > max_stdin_bytes
    ):
        raise InputLimit()
    try:
        process = subprocess.Popen(
            tuple(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=dict(env),
            start_new_session=True,
            bufsize=0,
        )
    except OSError:
        raise RetryableTransportFailure() from None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    exceeded = threading.Event()
    stdin_failed = threading.Event()
    stdin_completed = threading.Event()
    stop_requested = threading.Event()
    writer_start = threading.Event()
    deadline = time.monotonic() + float(timeout_seconds)
    stdin_writer_fd = None

    def read_stream(stream: object, target: bytearray, limit: int) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            room = max(0, limit - len(target))
            if room:
                target.extend(chunk[:room])
            if len(chunk) > room:
                exceeded.set()

    def write_stdin() -> None:
        try:
            writer_start.wait()
            if stop_requested.is_set():
                return
            if stdin_writer_fd is None:
                stdin_failed.set()
                return
            offset = 0
            while offset < len(input_bytes):
                written = os.write(stdin_writer_fd, input_bytes[offset:])
                if not isinstance(written, int) or written <= 0:
                    stdin_failed.set()
                    return
                offset += written
            stdin_completed.set()
        except (BrokenPipeError, OSError):
            if not stop_requested.is_set():
                stdin_failed.set()
        except Exception:
            stdin_failed.set()
        finally:
            if stdin_writer_fd is not None:
                try:
                    os.close(stdin_writer_fd)
                except OSError:
                    pass

    readers = (
        threading.Thread(
            target=read_stream, args=(process.stdout, stdout_buffer, max_stdout_bytes), daemon=True
        ),
        threading.Thread(
            target=read_stream, args=(process.stderr, stderr_buffer, max_stderr_bytes), daemon=True
        ),
    )
    stdin_writer = threading.Thread(target=write_stdin, daemon=True)
    try:
        stdin_writer.start()
    except Exception:
        _terminate_process_group(process)
        raise ProcessExecutionFailure() from None
    try:
        if process.stdin is None:
            raise ValueError("stdin pipe is unavailable")
        stdin_writer_fd = os.dup(process.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        stop_requested.set()
        writer_start.set()
        _terminate_process_group(process)
        stdin_writer.join(timeout=1)
        raise ProcessExecutionFailure() from None
    try:
        process.stdin.close()
    except Exception:
        stop_requested.set()
        writer_start.set()
        _terminate_process_group(process)
        stdin_writer.join(timeout=1)
        raise ProcessExecutionFailure() from None
    writer_start.set()
    started_readers = []
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
    except Exception:
        _cleanup_reader_start_failure(
            process, stop_requested, stdin_writer, started_readers
        )
        raise ProcessExecutionFailure() from None
    timed_out = False
    group_terminated = False
    while process.poll() is None:
        if exceeded.is_set() or stdin_failed.is_set() or time.monotonic() >= deadline:
            timed_out = not exceeded.is_set() and not stdin_failed.is_set()
            stop_requested.set()
            _terminate_process_group(process)
            group_terminated = True
            break
        time.sleep(0.01)
    process.wait()
    stdin_writer.join(timeout=1)
    for reader in readers:
        reader.join(timeout=1)
    if (
        exceeded.is_set()
        or stdin_failed.is_set()
        or stdin_writer.is_alive()
        or any(reader.is_alive() for reader in readers)
    ) and not group_terminated:
        stop_requested.set()
        _terminate_process_group(process)
        group_terminated = True
        stdin_writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
    try:
        if exceeded.is_set():
            raise OutputLimit()
        if stdin_failed.is_set() or stdin_writer.is_alive():
            raise ProcessExecutionFailure()
        if any(reader.is_alive() for reader in readers):
            raise ProcessExecutionFailure()
        if timed_out:
            raise InvocationTimeout()
        if input_bytes and not stdin_completed.is_set():
            raise ProcessExecutionFailure()
        retained_stdout = bytes(stdout_buffer)
        return retained_stdout, int(process.returncode)
    finally:
        stdout_buffer[:] = b"\x00" * len(stdout_buffer)
        stderr_buffer[:] = b"\x00" * len(stderr_buffer)
        stdout_buffer.clear()
        stderr_buffer.clear()


def _terminate_process_group(process: object) -> None:
    _close_process_stdin(process)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=0.2)
        return
    except Exception:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass


def _cleanup_reader_start_failure(
    process: object,
    stop_requested: threading.Event,
    stdin_writer: threading.Thread,
    started_readers: Sequence[threading.Thread],
) -> None:
    stop_requested.set()
    try:
        _terminate_process_group(process)
    except Exception:
        pass
    _close_process_output_streams(process)
    for thread in (stdin_writer,) + tuple(started_readers):
        try:
            thread.join(timeout=1)
        except Exception:
            pass
    try:
        process.wait(timeout=0.2)
    except Exception:
        pass


def _close_process_output_streams(process: object) -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass


def _close_process_stdin(process: object) -> None:
    stream = getattr(process, "stdin", None)
    if stream is None:
        return
    try:
        stream.close()
    except Exception:
        pass


def _result_json(result: EvalResult) -> str:
    def convert(value: object) -> object:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value

    return json.dumps(convert(asdict(result)), sort_keys=True, separators=(",", ":"))


def _harness_result_json(result: HarnessDryRunResult) -> str:
    return json.dumps(asdict(result), sort_keys=True, separators=(",", ":"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--tags", action="append", default=[])
    parser.add_argument("--release-suite", action="store_true")
    parser.add_argument("--approve-release-suite", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--harness-profile")
    parser.add_argument("--harness-bundle")
    parser.add_argument("--variant-repo")
    arguments = parser.parse_args(argv)
    harness_values = (
        arguments.harness_profile,
        arguments.harness_bundle,
        arguments.variant_repo,
    )
    if any(value is not None for value in harness_values):
        if (
            not all(value is not None for value in harness_values)
            or not arguments.dry_run
            or (
                arguments.approve_release_suite
                and not arguments.release_suite
            )
        ):
            result = _blocked_harness_result((), "blocked_request", "invalid_request")
        else:
            try:
                planning_request = EvalRequest.dry_run(
                    scenario_ids=arguments.scenario,
                    tags=arguments.tags,
                    release_suite=arguments.release_suite,
                    model=arguments.model,
                )
                result = run_harness_dry_run(
                    HarnessDryRunRequest(
                        planning_request=planning_request,
                        profile=arguments.harness_profile,
                        bundle_root=Path(arguments.harness_bundle),
                        skill_repo=Path(arguments.variant_repo),
                    )
                )
            except (TypeError, ValueError):
                result = _blocked_harness_result(
                    (), "blocked_request", "invalid_request"
                )
        print(_harness_result_json(result))
        return 0 if result.materialization_result == "pass" else 2
    owned_temp_root = None
    if arguments.approve_release_suite and not arguments.release_suite:
        print(
            json.dumps(
                {
                    "status": "blocked_request",
                    "verification_result": "blocked",
                    "attempts": 0,
                    "model_calls": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    if (
        arguments.release_suite
        and not arguments.dry_run
        and not arguments.approve_release_suite
    ):
        print(
            json.dumps(
                {
                    "status": "blocked_release_approval",
                    "verification_result": "blocked",
                    "attempts": 0,
                    "model_calls": 0,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    if arguments.dry_run:
        request = EvalRequest.dry_run(
            scenario_ids=arguments.scenario,
            tags=arguments.tags,
            release_suite=arguments.release_suite,
            model=arguments.model,
        )
    else:
        key = os.environ.get(DEFAULT_API_KEY_ENV_NAME)
        executable = shutil.which("codex")
        if not key or executable is None:
            blocked = {
                "status": "blocked_auth_or_process",
                "verification_result": "blocked",
                "attempts": 0,
                "model_calls": 0,
            }
            print(json.dumps(blocked, sort_keys=True, separators=(",", ":")))
            return 2
        temp_root = Path(tempfile.mkdtemp(prefix="codex-live-eval-root-"))
        temp_root.chmod(0o700)
        owned_temp_root = temp_root
        request = EvalRequest.targeted(
            scenario_ids=tuple(arguments.scenario),
            tags=tuple(arguments.tags),
            release_suite=arguments.release_suite,
            release_approved=arguments.approve_release_suite,
            model=arguments.model,
            repo_root=Path.cwd(),
            temp_root=temp_root,
            codex_executable=Path(executable),
            api_key=key,
        )
    try:
        result = run_eval(request, SubprocessCodex())
    except (TypeError, ValueError):
        retained_paths = (
            _cleanup_cli_temp_root(owned_temp_root)
            if owned_temp_root is not None
            else ()
        )
        print(
            json.dumps(
                {
                    "status": "blocked_request",
                    "verification_result": "blocked",
                    "attempts": 0,
                    "model_calls": 0,
                    "retention": "cleanup_required" if retained_paths else "none",
                    "manual_cleanup_required": bool(retained_paths),
                    "retained_paths": [str(path) for path in retained_paths],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    if owned_temp_root is not None and not result.manual_cleanup_required:
        retained_paths = _cleanup_cli_temp_root(owned_temp_root)
        if retained_paths:
            result = _mark_cleanup_required(result, retained_paths)
    print(_result_json(result))
    return 0 if result.verification_result in ("pass", "not_run") else 2


def _mark_cleanup_required(
    result: EvalResult, retained_paths: Sequence[Path]
) -> EvalResult:
    combined = tuple(
        Path(value)
        for value in sorted(
            {str(path) for path in result.retained_paths + tuple(retained_paths)}
        )
    )
    return replace(
        result,
        retention="cleanup_required",
        manual_cleanup_required=True,
        retained_paths=combined,
    )


def _cleanup_cli_temp_root(temp_root: Path) -> Tuple[Path, ...]:
    residual = Path(temp_root).absolute()
    try:
        temp_root.rmdir()
        return () if not temp_root.exists() else (residual,)
    except OSError:
        return (residual,)


if __name__ == "__main__":
    raise SystemExit(main())
