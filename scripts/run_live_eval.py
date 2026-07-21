#!/usr/bin/env python3
"""Run isolated workflow live evaluations or deterministic planning preflights."""

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, Mapping, Optional, Protocol, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.live_eval.artifacts import RedactingWriter, RedactionError
from scripts.live_eval.budget import Budget, BudgetExceeded, BudgetPolicy
from scripts.live_eval.checkout import (
    CheckoutManifest,
    install_checkout_skills,
    verify_loaded_checkout,
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


class InvocationFailure(RuntimeError):
    """A sanitized, retryable process or transport failure."""

    def __init__(self, classification: str = "infrastructure") -> None:
        del classification
        super().__init__("live eval invocation failed")


class InvocationTimeout(RuntimeError):
    """A sanitized, non-retryable process timeout."""

    def __init__(self) -> None:
        super().__init__("live eval invocation timed out")


class CodexProcess(Protocol):
    def probe(self, invocation: Invocation) -> CliCapabilities:
        ...

    def invoke(
        self, invocation: Invocation, prompt: str, timeout_seconds: int
    ) -> ProcessOutput:
        ...


@dataclass(frozen=True)
class EvalRequest:
    scenario_ids: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    release_suite: bool = False
    model: str = DEFAULT_MODEL
    dry_run_only: bool = False
    repo_root: Optional[Path] = None
    scenario_path: Optional[Path] = None
    temp_root: Optional[Path] = None
    codex_executable: Optional[Path] = None
    api_key: Optional[str] = field(default=None, repr=False)
    api_key_env_name: str = DEFAULT_API_KEY_ENV_NAME
    model_allowlist: Tuple[str, ...] = DEFAULT_MODELS

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


@dataclass(frozen=True)
class EvalResult:
    status: str
    verification_result: str
    attempts: int
    model_calls: int
    manifest: RunManifest
    scenarios: Tuple[ScenarioEvalResult, ...] = ()


def _default_scenario_path() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "live-eval-scenarios.json"


def _validate_and_select(request: EvalRequest) -> Tuple[Scenario, ...]:
    if not isinstance(request, EvalRequest):
        raise TypeError("request must be EvalRequest")
    if not isinstance(request.model, str) or not request.model.strip():
        raise ValueError("model must be a non-empty string")
    if request.model not in request.model_allowlist:
        raise ValueError("model must be present in the model allowlist")
    modes = sum(bool(value) for value in (request.scenario_ids, request.tags, request.release_suite))
    if modes > 1:
        raise ValueError("scenario IDs, tags, and release suite are mutually exclusive")
    corpus = load_scenarios(request.scenario_path or _default_scenario_path())
    if request.scenario_ids:
        if len(set(request.scenario_ids)) != len(request.scenario_ids):
            raise ValueError("scenario IDs must be unique")
        try:
            selected = tuple(corpus[item] for item in request.scenario_ids)
        except KeyError as error:
            raise ValueError("unknown scenario ID") from None
    elif request.release_suite:
        selected = select_scenarios(corpus, (), limit=max(1, len(corpus)))
    else:
        selected = select_scenarios(corpus, request.tags, limit=3)
    if not selected:
        raise ValueError("no scenarios selected")
    return selected


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

    policy = BudgetPolicy(
        max_calls=max(1, len(selected) * 2),
        max_seconds=float(max(1, sum(item.timeout_seconds for item in selected) * 2)),
        concurrency=1,
        max_raw_bytes=1024 * 1024,
    )
    budget = Budget(policy)
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
        return (
            ScenarioEvalResult(
                scenario.scenario_id, "blocked_isolation", "blocked", 0, 0
            ),
            None,
        )
    if (
        isolation.classification != "ready"
        or isolation.invocation_instance_id != id(invocation)
        or not verify_codex_home_seal(invocation, seal)
    ):
        return (
            ScenarioEvalResult(
                scenario.scenario_id, "blocked_isolation", "blocked", 0, 0
            ),
            checkout_manifest,
        )

    attempts = 0
    model_calls = 0
    had_infrastructure_failure = False
    while attempts < 2:
        if not verify_codex_home_seal(invocation, seal):
            return (
                ScenarioEvalResult(
                    scenario.scenario_id,
                    "blocked_isolation",
                    "blocked",
                    attempts,
                    model_calls,
                ),
                checkout_manifest,
            )
        attempts += 1
        try:
            with budget.acquire_call():
                model_calls += 1
                output = codex.invoke(
                    invocation, scenario.prompt, scenario.timeout_seconds
                )
            artifact = _retain_events(request, invocation, scenario, attempts, output)
            response = _final_response(artifact)
        except InvocationTimeout:
            return (
                ScenarioEvalResult(
                    scenario.scenario_id,
                    "blocked_timeout",
                    "blocked",
                    attempts,
                    model_calls,
                ),
                checkout_manifest,
            )
        except BudgetExceeded as error:
            return (
                ScenarioEvalResult(
                    scenario.scenario_id,
                    error.decision.value,
                    "blocked",
                    attempts,
                    model_calls,
                ),
                checkout_manifest,
            )
        except Exception:
            if attempts < 2:
                had_infrastructure_failure = True
                continue
            return (
                ScenarioEvalResult(
                    scenario.scenario_id,
                    "blocked_infrastructure",
                    "blocked",
                    attempts,
                    model_calls,
                ),
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
            ),
            checkout_manifest,
        )
    raise AssertionError("unreachable")


def _retain_events(
    request: EvalRequest,
    invocation: Invocation,
    scenario: Scenario,
    attempt: int,
    output: ProcessOutput,
) -> Path:
    if not isinstance(output, ProcessOutput):
        raise InvocationFailure()
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
    except (OSError, RedactionError):
        writer.abort()
        raise InvocationFailure() from None


def _final_response(artifact: Path) -> Mapping[str, object]:
    try:
        final = None
        for line in artifact.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if isinstance(event, dict) and event.get("type") == "final_response":
                final = event.get("response")
            elif isinstance(event, dict) and event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        candidate = json.loads(text)
                        if isinstance(candidate, dict):
                            final = candidate
        if not isinstance(final, dict):
            raise ValueError("missing structured response")
        return final
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise InvocationFailure() from None


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
    return EvalResult(
        status,
        verification,
        sum(item.attempts for item in result_values),
        sum(item.model_calls for item in result_values),
        manifest,
        result_values,
    )


class SubprocessCodex:
    """Network-capable implementation used only by an explicit live CLI run."""

    def probe(self, invocation: Invocation) -> CliCapabilities:
        try:
            version_output = subprocess.run(
                (str(invocation.executable), "--version"),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(invocation.transport_env),
                timeout=10,
                text=True,
            ).stdout
            help_output = subprocess.run(
                (str(invocation.executable), "--help"),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(invocation.transport_env),
                timeout=10,
                text=True,
            ).stdout
            exec_help = subprocess.run(
                (str(invocation.executable), "exec", "--help"),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(invocation.transport_env),
                timeout=10,
                text=True,
            ).stdout
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_output)
            if match is None:
                raise ValueError("unrecognized Codex version")
            combined_help = help_output + "\n" + exec_help
            supported = frozenset(
                flag for flag in REQUIRED_FLAGS if flag in combined_help
            )
        except Exception:
            raise InvocationFailure("probe") from None
        return CliCapabilities(
            selected_executable=invocation.executable,
            selected_executable_identity=invocation.executable_identity,
            cli_version=tuple(int(item) for item in match.groups()),
            supported_flags=supported,
            argv_digest=invocation.argv_digest,
            child_env_policy_id=invocation.child_env_policy_id,
            child_env_policy_digest=invocation.child_env_policy_digest,
            non_profile_child_env=True,
            network_disabled=True,
            mcp_disabled=True,
            plugins_disabled=True,
            hooks_disabled=True,
            unexpected_skills_absent=True,
        )

    def invoke(
        self, invocation: Invocation, prompt: str, timeout_seconds: int
    ) -> ProcessOutput:
        try:
            completed = subprocess.run(
                invocation.argv,
                input=prompt.encode("utf-8"),
                cwd=str(invocation.cwd),
                env=dict(invocation.transport_env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise InvocationTimeout() from None
        except Exception:
            raise InvocationFailure() from None
        if completed.returncode != 0:
            raise InvocationFailure()
        return ProcessOutput(tuple(completed.stdout.splitlines(keepends=True)))


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--tags", action="append", default=[])
    parser.add_argument("--release-suite", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
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
        request = EvalRequest.targeted(
            scenario_ids=tuple(arguments.scenario),
            tags=tuple(arguments.tags),
            release_suite=arguments.release_suite,
            model=arguments.model,
            repo_root=Path.cwd(),
            temp_root=temp_root,
            codex_executable=Path(executable),
            api_key=key,
        )
    try:
        result = run_eval(request, SubprocessCodex())
    except (TypeError, ValueError):
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
    print(_result_json(result))
    return 0 if result.verification_result in ("pass", "not_run") else 2


if __name__ == "__main__":
    raise SystemExit(main())
