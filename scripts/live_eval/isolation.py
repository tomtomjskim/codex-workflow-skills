"""Fail-closed command, authentication, and isolation models for live evals."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable, FrozenSet, Mapping, Optional, Tuple


_MINIMUM_CODEX_VERSION = (0, 142, 4)
_REQUIRED_FLAGS = frozenset(
    {
        "-a",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        "--sandbox",
    }
)
_TRANSPORT_ENV_ALLOWLIST = frozenset(
    {"LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TMPDIR"}
)
_TOOL_ENV_ALLOWLIST = _TRANSPORT_ENV_ALLOWLIST
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:AUTH|COOKIE|CREDENTIAL|KEY|PASSWD|PASSWORD|SECRET|SESSION|TOKEN)(?:_|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvalConfig:
    codex_executable: Path
    model: str
    model_allowlist: Tuple[str, ...]
    codex_home: Path
    cwd: Path
    api_key_env_name: str
    api_key: Optional[str]
    output_schema: Path
    process_env: Mapping[str, str]


@dataclass(frozen=True)
class Invocation:
    argv: Tuple[str, ...]
    transport_env: Mapping[str, str]
    tool_env: Mapping[str, str]
    codex_home: Path
    cwd: Path


@dataclass(frozen=True)
class AuthReport:
    classification: str
    result: str
    reason: str


@dataclass(frozen=True)
class CliCapabilities:
    supported_flags: FrozenSet[str]
    tool_env_separation: bool
    network_disabled: bool
    mcp_disabled: bool
    plugins_disabled: bool
    hooks_disabled: bool
    unexpected_skills_absent: bool
    cli_version: Optional[Tuple[int, int, int]] = None


@dataclass(frozen=True)
class IsolationReport:
    classification: str
    result: str
    missing_guarantees: Tuple[str, ...]
    cli_version: Optional[Tuple[int, int, int]]


FeatureProbe = Callable[[Invocation], CliCapabilities]


def preflight_auth(api_key: Optional[str], oauth_files_present: bool) -> AuthReport:
    """Accept an explicit process-local API key; never use OAuth files."""
    del oauth_files_present
    if not isinstance(api_key, str) or not api_key:
        return AuthReport(
            classification="blocked_auth",
            result="blocked",
            reason="an explicit process-local API key is required",
        )
    return AuthReport(
        classification="ready",
        result="pass",
        reason="explicit process-local API key supplied",
    )


def build_invocation(config: EvalConfig) -> Invocation:
    """Build an isolated, non-interactive Codex live-eval invocation."""
    _validate_config(config)
    codex_home = Path(config.codex_home).absolute()
    cwd = Path(config.cwd).absolute()
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    cwd.mkdir(mode=0o700, parents=True, exist_ok=True)

    credential_values = {
        value
        for name, value in config.process_env.items()
        if _CREDENTIAL_NAME_PATTERN.search(name)
        and isinstance(value, str)
        and value
    }
    inherited = {
        name: value
        for name, value in config.process_env.items()
        if name in _TRANSPORT_ENV_ALLOWLIST
        and isinstance(value, str)
        and value not in credential_values
    }
    transport_env = dict(inherited)
    transport_env["CODEX_HOME"] = str(codex_home)
    transport_env[config.api_key_env_name] = config.api_key

    tool_env = {
        name: value
        for name, value in config.process_env.items()
        if name in _TOOL_ENV_ALLOWLIST
        and isinstance(value, str)
        and name != config.api_key_env_name
        and value != config.api_key
        and value not in credential_values
    }
    argv = (
        str(config.codex_executable),
        "-a",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--model",
        config.model,
        "--output-schema",
        str(config.output_schema),
    )
    return Invocation(
        argv=argv,
        transport_env=transport_env,
        tool_env=tool_env,
        codex_home=codex_home,
        cwd=cwd,
    )


def preflight_isolation(
    invocation: Invocation, probe: Optional[FeatureProbe] = None
) -> IsolationReport:
    """Prove every required CLI and subprocess boundary before model execution."""
    if probe is None:
        return IsolationReport(
            classification="blocked_isolation",
            result="blocked",
            missing_guarantees=("cli_capability_probe",),
            cli_version=None,
        )
    try:
        capabilities = probe(invocation)
    except Exception:
        return IsolationReport(
            classification="blocked_isolation",
            result="blocked",
            missing_guarantees=("cli_feature_probe",),
            cli_version=None,
        )

    missing = sorted(_REQUIRED_FLAGS.difference(capabilities.supported_flags))
    if (
        capabilities.cli_version is None
        or capabilities.cli_version < _MINIMUM_CODEX_VERSION
    ):
        missing.append("codex_cli_version")
    if not capabilities.tool_env_separation:
        missing.append("tool_env_separation")
    for name in (
        "network_disabled",
        "mcp_disabled",
        "plugins_disabled",
        "hooks_disabled",
        "unexpected_skills_absent",
    ):
        if not getattr(capabilities, name):
            missing.append(name)
    if not _invocation_separates_key(invocation):
        missing.append("api_key_non_inheritance")
    if not _invocation_policy_is_intact(invocation):
        missing.append("invocation_policy")

    missing_guarantees = tuple(sorted(set(missing)))
    if missing_guarantees:
        return IsolationReport(
            classification="blocked_isolation",
            result="blocked",
            missing_guarantees=missing_guarantees,
            cli_version=capabilities.cli_version,
        )
    return IsolationReport(
        classification="ready",
        result="pass",
        missing_guarantees=(),
        cli_version=capabilities.cli_version,
    )


def _validate_config(config: EvalConfig) -> None:
    if not config.model or config.model not in config.model_allowlist:
        raise ValueError("model must be present in the model allowlist")
    if not _ENV_NAME_PATTERN.fullmatch(config.api_key_env_name):
        raise ValueError("API-key environment name is invalid")
    if not isinstance(config.api_key, str) or not config.api_key:
        raise ValueError("an explicit process-local API key is required")
    if Path(config.codex_home).resolve() == Path(config.cwd).resolve():
        raise ValueError("CODEX_HOME and neutral cwd must be separate")


def _invocation_separates_key(invocation: Invocation) -> bool:
    key_names = {
        name
        for name, value in invocation.transport_env.items()
        if name not in _TRANSPORT_ENV_ALLOWLIST and name != "CODEX_HOME" and value
    }
    key_values = {invocation.transport_env[name] for name in key_names}
    return not key_names.intersection(invocation.tool_env) and not key_values.intersection(
        invocation.tool_env.values()
    )


def _invocation_policy_is_intact(invocation: Invocation) -> bool:
    argv = invocation.argv
    if len(argv) < 4 or argv[1:4] != ("-a", "never", "exec"):
        return False
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules"):
        if flag not in argv:
            return False
    expected_values = {"--sandbox": "read-only", "--output-schema": None}
    for flag, expected in expected_values.items():
        try:
            position = argv.index(flag)
            value = argv[position + 1]
        except (ValueError, IndexError):
            return False
        if expected is not None and value != expected:
            return False
        if not value or value.startswith("-"):
            return False
    if invocation.transport_env.get("CODEX_HOME") != str(invocation.codex_home):
        return False
    return set(invocation.tool_env).issubset(_TOOL_ENV_ALLOWLIST)
