"""Fail-closed command, authentication, and isolation models for live evals."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from typing import Callable, FrozenSet, Mapping, Optional, Tuple
import uuid


_MINIMUM_CODEX_VERSION = (0, 142, 4)
_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_API_KEY_ENV_NAME = "OPENAI_API_KEY"
_CHILD_ENV_POLICY_ID = "codex-shell-environment-policy-v1"
_CONSUMER_CONTRACT = "same-invocation-instance-v1"
_REQUIRED_FLAGS = frozenset(
    {
        "-a",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        "--sandbox",
        "--strict-config",
        "-c",
    }
)
_AUTH_URL_PATTERN = re.compile(
    r"(?:DATABASE|DB|MONGO(?:DB)?|POSTGRES|REDIS|SQL).*?(?:URI|URL)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvalConfig:
    codex_executable: Path
    model: str
    model_allowlist: Tuple[str, ...]
    temp_root: Path
    api_key: str = field(repr=False)
    api_key_env_name: str = _API_KEY_ENV_NAME

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "codex_executable", Path(self.codex_executable).absolute()
        )
        object.__setattr__(self, "temp_root", Path(self.temp_root).absolute())
        object.__setattr__(self, "model_allowlist", tuple(self.model_allowlist))


@dataclass(frozen=True)
class PathIdentity:
    device: int
    inode: int
    resolved: str


@dataclass(frozen=True)
class Invocation:
    argv: Tuple[str, ...]
    transport_env: Mapping[str, str] = field(repr=False)
    tool_env: Mapping[str, str] = field(repr=False)
    executable: Path
    executable_identity: Tuple[int, int]
    model: str
    model_allowlist: Tuple[str, ...]
    temp_root: Path
    run_dir: Path
    codex_home: Path
    cwd: Path
    tmpdir: Path
    output_schema: Path
    path_identities: Mapping[str, PathIdentity] = field(repr=False)
    argv_digest: str
    child_env_policy_id: str
    child_env_policy_digest: str
    invocation_id: str
    consumer_contract: str = _CONSUMER_CONTRACT

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "model_allowlist", tuple(self.model_allowlist))
        object.__setattr__(
            self, "transport_env", MappingProxyType(dict(self.transport_env))
        )
        object.__setattr__(self, "tool_env", MappingProxyType(dict(self.tool_env)))
        object.__setattr__(
            self, "path_identities", MappingProxyType(dict(self.path_identities))
        )


@dataclass(frozen=True)
class AuthReport:
    classification: str
    result: str
    reason: str


@dataclass(frozen=True)
class CliCapabilities:
    selected_executable: Path
    selected_executable_identity: Tuple[int, int]
    cli_version: Tuple[int, int, int]
    supported_flags: FrozenSet[str]
    argv_digest: str
    child_env_policy_id: str
    child_env_policy_digest: str
    non_profile_child_env: bool
    network_disabled: bool
    mcp_disabled: bool
    plugins_disabled: bool
    hooks_disabled: bool
    unexpected_skills_absent: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "selected_executable", Path(self.selected_executable).absolute()
        )
        object.__setattr__(
            self, "selected_executable_identity", tuple(self.selected_executable_identity)
        )
        object.__setattr__(self, "cli_version", tuple(self.cli_version))
        object.__setattr__(self, "supported_flags", frozenset(self.supported_flags))


@dataclass(frozen=True)
class IsolationReport:
    classification: str
    result: str
    missing_guarantees: Tuple[str, ...]
    cli_version: Optional[Tuple[int, int, int]]
    invocation_id: str
    invocation_instance_id: int
    consumer_contract: str


FeatureProbe = Callable[[Invocation], CliCapabilities]


def is_credential_like_name(name: str) -> bool:
    """Conservatively classify environment names that may carry credentials."""
    if not isinstance(name, str) or not name:
        return True
    upper_name = name.upper()
    tokens = tuple(part for part in re.split(r"[^A-Z0-9]+", upper_name) if part)
    credential_tokens = {
        "AUTH",
        "BEARER",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "PASS",
        "PASSWD",
        "PASSWORD",
        "PAT",
        "SECRET",
        "SESSION",
        "TOKEN",
    }
    combined_markers = (
        "APIKEY",
        "AUTHORIZATION",
        "CREDENTIAL",
        "PASSFILE",
        "PASSWD",
        "PASSWORD",
    )
    return bool(
        credential_tokens.intersection(tokens)
        or any(marker in upper_name for marker in combined_markers)
        or _AUTH_URL_PATTERN.search(upper_name)
    )


def toml_string(value: str) -> str:
    """Serialize an untrusted path as a TOML-compatible basic string."""
    if not isinstance(value, str):
        raise TypeError("TOML string value must be text")
    value.encode("utf-8", errors="strict")
    return json.dumps(value, ensure_ascii=True)


def preflight_auth(api_key: Optional[str], oauth_files_present: bool) -> AuthReport:
    """Accept an explicit process-local API key; never inspect OAuth files."""
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
    """Create fresh run paths and an exact isolated Codex invocation."""
    _validate_config(config)
    root_identity = _validate_trusted_temp_root(config.temp_root)
    _reject_embedded_secrets(
        (str(config.temp_root), str(config.codex_executable)), (config.api_key,)
    )
    executable_identity = _validate_executable(config.codex_executable)

    run_dir = Path(
        tempfile.mkdtemp(prefix="codex-live-eval-", dir=str(config.temp_root))
    ).absolute()
    if run_dir.parent != config.temp_root or any(run_dir.iterdir()):
        raise ValueError("fresh run directory is not empty under temp_root")
    _validate_plain_directory(run_dir, "fresh run")
    run_dir.chmod(0o700)
    codex_home = run_dir / "codex-home"
    cwd = run_dir / "cwd"
    tmpdir = run_dir / "tmp"
    for path in (codex_home, cwd, tmpdir):
        path.mkdir(mode=0o700, exist_ok=False)
        path.chmod(0o700)
    output_schema = run_dir / "response.schema.json"

    tool_env = {
        "PATH": _SAFE_PATH,
        "HOME": str(codex_home),
        "TMPDIR": str(tmpdir),
    }
    transport_env = {
        **tool_env,
        "CODEX_HOME": str(codex_home),
        _API_KEY_ENV_NAME: config.api_key,
    }
    _validate_environment_values(
        transport_env, tool_env, config.api_key, config.api_key_env_name
    )
    policy_config = _child_environment_config(tool_env)
    argv = _canonical_argv(
        executable=config.codex_executable,
        model=config.model,
        output_schema=output_schema,
        policy_config=policy_config,
    )
    argv_digest = _digest(argv)
    policy_digest = _digest(
        {
            "policy_id": _CHILD_ENV_POLICY_ID,
            "config": policy_config,
            "tool_env": tool_env,
        }
    )
    path_identities = {
        "temp_root": root_identity,
        "run_dir": _path_identity(run_dir),
        "codex_home": _path_identity(codex_home),
        "cwd": _path_identity(cwd),
        "tmpdir": _path_identity(tmpdir),
    }
    return Invocation(
        argv=argv,
        transport_env=transport_env,
        tool_env=tool_env,
        executable=config.codex_executable,
        executable_identity=executable_identity,
        model=config.model,
        model_allowlist=config.model_allowlist,
        temp_root=config.temp_root,
        run_dir=run_dir,
        codex_home=codex_home,
        cwd=cwd,
        tmpdir=tmpdir,
        output_schema=output_schema,
        path_identities=path_identities,
        argv_digest=argv_digest,
        child_env_policy_id=_CHILD_ENV_POLICY_ID,
        child_env_policy_digest=policy_digest,
        invocation_id=uuid.uuid4().hex,
    )


def preflight_isolation(
    invocation: Invocation, probe: Optional[FeatureProbe] = None
) -> IsolationReport:
    """Prove path, command, and capability contracts before model execution."""
    report_values = {
        "invocation_id": invocation.invocation_id,
        "invocation_instance_id": id(invocation),
        "consumer_contract": invocation.consumer_contract,
    }
    missing = []
    if not _path_integrity_is_intact(invocation):
        missing.append("path_integrity")
    if not _invocation_policy_is_intact(invocation):
        missing.append("invocation_policy")
    if missing:
        return _blocked_report(missing, None, report_values)
    if probe is None:
        return _blocked_report(("cli_capability_probe",), None, report_values)

    try:
        capabilities = probe(invocation)
        capability_missing = _validate_capabilities(invocation, capabilities)
        cli_version = capabilities.cli_version
    except Exception:
        return _blocked_report(("cli_capability_probe",), None, report_values)
    if capability_missing:
        return _blocked_report(capability_missing, cli_version, report_values)
    return IsolationReport(
        classification="ready",
        result="pass",
        missing_guarantees=(),
        cli_version=cli_version,
        **report_values,
    )


def _validate_config(config: EvalConfig) -> None:
    if not config.model or config.model not in config.model_allowlist:
        raise ValueError("model must be present in the model allowlist")
    if config.api_key_env_name != _API_KEY_ENV_NAME:
        raise ValueError("API key environment must be OPENAI_API_KEY")
    if not isinstance(config.api_key, str) or not config.api_key:
        raise ValueError("an explicit process-local API key is required")


def _validate_trusted_temp_root(path: Path) -> PathIdentity:
    if not path.is_absolute():
        raise ValueError("temp_root must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ValueError("temp_root component is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("temp_root component must not be a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("temp_root component must be a directory")
    metadata = path.lstat()
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("temp_root must be owned by the current process user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("temp_root must be mode 0700 or stricter")
    return _path_identity(path)


def _validate_executable(path: Path) -> Tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("Codex executable is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Codex executable must be a regular non-symlink file")
    if not os.access(str(path), os.X_OK):
        raise ValueError("Codex executable must be executable")
    return (metadata.st_dev, metadata.st_ino)


def _validate_plain_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("{} directory is unavailable".format(label)) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("{} directory must not be a symlink".format(label))


def _child_environment_config(tool_env: Mapping[str, str]) -> Tuple[str, ...]:
    serialized_set = "shell_environment_policy.set={}".format(
        "{" + ",".join(
            "{}={}".format(name, toml_string(tool_env[name]))
            for name in ("PATH", "HOME", "TMPDIR")
        ) + "}"
    )
    return (
        'shell_environment_policy.inherit="none"',
        "shell_environment_policy.experimental_use_profile=false",
        "shell_environment_policy.ignore_default_excludes=false",
        serialized_set,
    )


def _canonical_argv(
    executable: Path,
    model: str,
    output_schema: Path,
    policy_config: Tuple[str, ...],
) -> Tuple[str, ...]:
    argv = [
        str(executable),
        "-a",
        "never",
        "exec",
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "--output-schema",
        str(output_schema),
    ]
    for value in policy_config:
        argv.extend(("-c", value))
    return tuple(argv)


def _validate_environment_values(
    transport_env: Mapping[str, str],
    tool_env: Mapping[str, str],
    api_key: str,
    api_key_env_name: str,
) -> None:
    expected_names = {"PATH", "HOME", "TMPDIR", "CODEX_HOME", _API_KEY_ENV_NAME}
    if set(transport_env) != expected_names:
        raise ValueError("transport environment is not the fixed allowlist")
    if set(tool_env) != {"PATH", "HOME", "TMPDIR"}:
        raise ValueError("tool environment is not the fixed allowlist")
    if api_key_env_name != _API_KEY_ENV_NAME:
        raise ValueError("API key environment must be OPENAI_API_KEY")
    for name, value in transport_env.items():
        if name == _API_KEY_ENV_NAME:
            if value != api_key:
                raise ValueError("transport API key mismatch")
            continue
        if is_credential_like_name(name):
            raise ValueError("credential-like environment name is not allowed")
        _reject_embedded_secrets((value,), (api_key,))
    for name, value in tool_env.items():
        if is_credential_like_name(name):
            raise ValueError("credential-like tool environment is not allowed")
        _reject_embedded_secrets((name, value), (api_key,))


def _reject_embedded_secrets(values: Tuple[str, ...], secrets: Tuple[str, ...]) -> None:
    for secret in secrets:
        if not secret:
            continue
        for value in values:
            if secret in value:
                raise ValueError("known secret value is embedded in isolation data")


def _path_identity(path: Path) -> PathIdentity:
    metadata = path.lstat()
    return PathIdentity(metadata.st_dev, metadata.st_ino, str(path.resolve(strict=True)))


def _path_integrity_is_intact(invocation: Invocation) -> bool:
    try:
        if (
            _validate_trusted_temp_root(invocation.temp_root)
            != invocation.path_identities["temp_root"]
        ):
            return False
        if _validate_executable(invocation.executable) != invocation.executable_identity:
            return False
        paths = {
            "run_dir": invocation.run_dir,
            "codex_home": invocation.codex_home,
            "cwd": invocation.cwd,
            "tmpdir": invocation.tmpdir,
        }
        for name, path in paths.items():
            _validate_plain_directory(path, name)
            if _path_identity(path) != invocation.path_identities[name]:
                return False
            if stat.S_IMODE(path.lstat().st_mode) != 0o700:
                return False
        if invocation.run_dir.parent != invocation.temp_root:
            return False
        if invocation.codex_home.parent != invocation.run_dir:
            return False
        if invocation.cwd.parent != invocation.run_dir:
            return False
        if invocation.tmpdir.parent != invocation.run_dir:
            return False
        if any(invocation.codex_home.iterdir()) or any(invocation.cwd.iterdir()):
            return False
        if any(invocation.tmpdir.iterdir()):
            return False
        if invocation.output_schema != invocation.run_dir / "response.schema.json":
            return False
        if invocation.output_schema.exists() or invocation.output_schema.is_symlink():
            return False
        if {item.name for item in invocation.run_dir.iterdir()} != {
            "codex-home",
            "cwd",
            "tmp",
        }:
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


def _invocation_policy_is_intact(invocation: Invocation) -> bool:
    try:
        expected_tool_env = {
            "PATH": _SAFE_PATH,
            "HOME": str(invocation.codex_home),
            "TMPDIR": str(invocation.tmpdir),
        }
        expected_transport_env = {
            **expected_tool_env,
            "CODEX_HOME": str(invocation.codex_home),
            _API_KEY_ENV_NAME: invocation.transport_env[_API_KEY_ENV_NAME],
        }
        _validate_environment_values(
            expected_transport_env,
            expected_tool_env,
            invocation.transport_env[_API_KEY_ENV_NAME],
            _API_KEY_ENV_NAME,
        )
        if dict(invocation.tool_env) != expected_tool_env:
            return False
        if dict(invocation.transport_env) != expected_transport_env:
            return False
        policy_config = _child_environment_config(expected_tool_env)
        expected_argv = _canonical_argv(
            invocation.executable,
            invocation.model,
            invocation.output_schema,
            policy_config,
        )
        if invocation.argv != expected_argv:
            return False
        if invocation.model not in invocation.model_allowlist:
            return False
        if invocation.argv_digest != _digest(expected_argv):
            return False
        expected_policy_digest = _digest(
            {
                "policy_id": _CHILD_ENV_POLICY_ID,
                "config": policy_config,
                "tool_env": expected_tool_env,
            }
        )
        if invocation.child_env_policy_id != _CHILD_ENV_POLICY_ID:
            return False
        if invocation.child_env_policy_digest != expected_policy_digest:
            return False
        if invocation.consumer_contract != _CONSUMER_CONTRACT:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _validate_capabilities(
    invocation: Invocation, capabilities: CliCapabilities
) -> Tuple[str, ...]:
    if not isinstance(capabilities, CliCapabilities):
        raise TypeError("probe must return CliCapabilities")
    version = capabilities.cli_version
    if (
        not isinstance(version, tuple)
        or len(version) != 3
        or any(isinstance(item, bool) or not isinstance(item, int) for item in version)
    ):
        raise ValueError("CLI version must be an integer three-tuple")
    if not isinstance(capabilities.supported_flags, frozenset) or any(
        not isinstance(item, str) for item in capabilities.supported_flags
    ):
        raise ValueError("supported_flags must be a string frozenset")

    missing = []
    if version < _MINIMUM_CODEX_VERSION:
        missing.append("codex_cli_version")
    if not _REQUIRED_FLAGS.issubset(capabilities.supported_flags):
        missing.extend(sorted(_REQUIRED_FLAGS - capabilities.supported_flags))
    exact_values = (
        ("selected_executable", capabilities.selected_executable, invocation.executable),
        (
            "selected_executable_identity",
            capabilities.selected_executable_identity,
            invocation.executable_identity,
        ),
        ("argv_digest", capabilities.argv_digest, invocation.argv_digest),
        (
            "child_env_policy_id",
            capabilities.child_env_policy_id,
            invocation.child_env_policy_id,
        ),
        (
            "child_env_policy_digest",
            capabilities.child_env_policy_digest,
            invocation.child_env_policy_digest,
        ),
    )
    for name, actual, expected in exact_values:
        if actual != expected:
            missing.append(name)
    for name in (
        "non_profile_child_env",
        "network_disabled",
        "mcp_disabled",
        "plugins_disabled",
        "hooks_disabled",
        "unexpected_skills_absent",
    ):
        if getattr(capabilities, name) is not True:
            missing.append(name)
    return tuple(sorted(set(missing)))


def _blocked_report(
    missing: Tuple[str, ...],
    cli_version: Optional[Tuple[int, int, int]],
    report_values: Mapping[str, object],
) -> IsolationReport:
    return IsolationReport(
        classification="blocked_isolation",
        result="blocked",
        missing_guarantees=tuple(sorted(set(missing))),
        cli_version=cli_version,
        invocation_id=str(report_values["invocation_id"]),
        invocation_instance_id=int(report_values["invocation_instance_id"]),
        consumer_contract=str(report_values["consumer_contract"]),
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
