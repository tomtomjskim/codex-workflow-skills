"""Observe native filesystem, network, environment, and ledger primitives."""

import hashlib
import errno
import os
import platform
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

from scripts.live_eval.private_jsonl_ledger import (
    PrivateJSONLLedger,
    PrivateJSONLLedgerError,
)
from scripts.workflow_coordination.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    load_canonical_input,
    sha256_id,
)


CODEX_VERSION = "0.145.0"
CODEX_SHA256 = "6db9193ce2c9a8cef2b5482612cde24202a4329dfc34f4687a036d5d7da619af"
PYTHON_VERSION = "3.9.6"
PYTHON_SHA256 = "6b24be8ba00c4abc8b3e7e2620bd710e2cf7a4312d00c147f8f05fbb6f3d297a"
PERMISSION_PROFILE_NAME = "phase-b0-native-readonly"

CONFIG_BYTES = b"""default_permissions = "phase-b0-native-readonly"

[permissions.phase-b0-native-readonly.filesystem]
":root" = "deny"
":minimal" = "read"

[permissions.phase-b0-native-readonly.filesystem.":workspace_roots"]
"." = "read"

[permissions.phase-b0-native-readonly.network]
enabled = false
"""

CHILD_SOURCE = """import hashlib
import json
import os
import socket
import sys

def read_file(path):
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        data = os.read(descriptor, 4097)
        if len(data) > 4096:
            return {"digest": None, "errno": None, "ok": False, "overflow": True}
        return {"digest": hashlib.sha256(data).hexdigest(), "errno": None, "ok": True, "overflow": False}
    except OSError as error:
        return {"digest": None, "errno": error.errno, "ok": False, "overflow": False}
    finally:
        if descriptor >= 0:
            os.close(descriptor)

def write_file(path, content):
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("write made no progress")
            offset += written
        os.fsync(descriptor)
        return {"errno": None, "ok": True}
    except OSError as error:
        return {"errno": error.errno, "ok": False}
    finally:
        if descriptor >= 0:
            os.close(descriptor)

def connect_loopback(port, content):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(2)
        client.connect(("127.0.0.1", port))
        client.sendall(content)
        return {"errno": None, "ok": True}
    except OSError as error:
        return {"errno": error.errno, "ok": False}
    finally:
        client.close()

if len(sys.argv) != 7:
    raise SystemExit(64)
allowed_read, forbidden_read, allowed_write, forbidden_write, raw_port, raw_token = sys.argv[1:]
token = bytes.fromhex(raw_token)
secret = os.environ.get("PHASE_B0_SYNTHETIC_SECRET")
result = {
    "allowed_read": read_file(allowed_read),
    "allowed_write": write_file(allowed_write, token),
    "environment": {
        "digest": hashlib.sha256(secret.encode("utf-8")).hexdigest() if secret is not None else None,
        "present": secret is not None,
    },
    "forbidden_read": read_file(forbidden_read),
    "forbidden_write": write_file(forbidden_write, token),
    "network": connect_loopback(int(raw_port), token),
    "schema_version": 1,
}
sys.stdout.buffer.write(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\\n")
"""


@dataclass(frozen=True)
class NativeCanaryReadinessRequest:
    codex_executable: Path
    python_executable: Path
    temp_parent: Path
    private_root: Path


@dataclass(frozen=True)
class BoundedCommand:
    argv: Tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    timeout_seconds: int
    stdout_limit: int
    stderr_limit: int


@dataclass(frozen=True)
class BoundedCommandResult:
    returncode: Optional[int]
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_overflow: bool


_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_TERMINALS = {
    "native_primitive_observations_recorded": (
        "native_primitive_observations_only", ("removed",), ("1111111",)
    ),
    "request_invalid": ("blocked", ("not_started",), ("1000000",)),
    "unsupported_platform": ("blocked", ("not_started",), ("1000000",)),
    "executable_identity_invalid": (
        "blocked", ("not_started", "removed"), ("1000000",)
    ),
    "cli_version_mismatch": ("blocked", ("removed",), ("1110000",)),
    "weak_control_invalid": ("blocked", ("removed",), ("1110000",)),
    "native_permission_unproven": ("blocked", ("removed",), ("1110000",)),
    "ledger_primitives_unproven": ("blocked", ("removed",), ("1111100",)),
    "evidence_retention_failed": ("blocked", ("removed",), ("1111110",)),
    "cleanup_required": (
        "blocked",
        ("cleanup_required",),
        ("1000000", "1110000", "1111100", "1111110", "1111111"),
    ),
}


@dataclass(frozen=True)
class NativeCanaryReadinessResult:
    status: str
    model_calls: int
    policy_digest: Optional[str]
    codex_executable_identity_digest: Optional[str]
    python_executable_identity_digest: Optional[str]
    permission_profile_evidence_digest: Optional[str]
    supervisor_environment_evidence_digest: Optional[str]
    ledger_probe_evidence_digest: Optional[str]
    complete_evidence_digest: Optional[str]
    cleanup_state: str
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.model_calls) is not int or self.model_calls != 0:
            raise ValueError("model_calls must be integer zero")
        digests = (
            self.policy_digest,
            self.codex_executable_identity_digest,
            self.python_executable_identity_digest,
            self.permission_profile_evidence_digest,
            self.supervisor_environment_evidence_digest,
            self.ledger_probe_evidence_digest,
            self.complete_evidence_digest,
        )
        if any(
            value is not None
            and (type(value) is not str or not _DIGEST_RE.fullmatch(value))
            for value in digests
        ):
            raise ValueError("invalid digest")
        terminal = _TERMINALS.get(self.reason_code)
        mask = "".join("1" if value is not None else "0" for value in digests)
        if (
            terminal is None
            or self.status != terminal[0]
            or self.cleanup_state not in terminal[1]
            or mask not in terminal[2]
        ):
            raise ValueError("invalid terminal result combination")


@dataclass(frozen=True, init=False)
class _NativeReadinessPolicy:
    _document_bytes: bytes
    config_bytes: bytes
    child_source: str

    def __init__(
        self,
        document: Mapping[str, object],
        config_bytes: bytes,
        child_source: str,
    ) -> None:
        if type(config_bytes) is not bytes or type(child_source) is not str:
            raise ValueError("policy literals have invalid types")
        try:
            encoded = canonical_bytes(document)
            parsed = load_canonical_input(encoded)
            permission = parsed["permission_profile"]
            child = parsed["child"]
        except (CanonicalJSONError, KeyError, TypeError, RecursionError) as error:
            raise ValueError("invalid native readiness policy") from error
        config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        child_bytes = child_source.encode("utf-8")
        child_digest = "sha256:" + hashlib.sha256(child_bytes).hexdigest()
        if (
            permission.get("config_bytes_length") != len(config_bytes)
            or permission.get("config_sha256") != config_digest
            or child.get("bytes_length") != len(child_bytes)
            or child.get("sha256") != child_digest
        ):
            raise ValueError("policy literals do not match policy document")
        object.__setattr__(self, "_document_bytes", encoded)
        object.__setattr__(self, "config_bytes", config_bytes)
        object.__setattr__(self, "child_source", child_source)

    @property
    def document(self) -> dict:
        value = load_canonical_input(self._document_bytes)
        if type(value) is not dict:
            raise ValueError("policy document must be an object")
        return value


_POLICY_DOCUMENT = {
    "argv_templates": {
        "codex_version": ["{codex_executable}", "--version"],
        "python_version": ["{python_executable}", "--version"],
        "strong_probe": ["{codex_executable}", "sandbox", "-P", PERMISSION_PROFILE_NAME, "-C", "{allowed_root}", "--", "{python_executable}", "-I", "-S", "-B", "-c", "{child_source}", "{allowed_read}", "{forbidden_read}", "{allowed_write}", "{forbidden_write}", "{loopback_port}", "{token_hex}"],
        "weak_probe": ["{python_executable}", "-I", "-S", "-B", "-c", "{child_source}", "{allowed_read}", "{forbidden_read}", "{allowed_write}", "{forbidden_write}", "{loopback_port}", "{token_hex}"],
    },
    "child": {
        "bytes_length": 2489,
        "result_schema_version": 1,
        "sha256": "sha256:aa1e8614fdd267c2cd0bbf237f1ed8f3a039e6a1f9de416cef5c52f3c520e6a9",
        "synthetic_secret_key": "PHASE_B0_SYNTHETIC_SECRET",
    },
    "command_contexts": {
        "codex_version": {"cwd_class": "owned_allowed_root", "environment_template": "strong"},
        "python_version": {"cwd_class": "owned_allowed_root", "environment_template": "strong"},
        "strong_probe": {"cwd_class": "owned_allowed_root", "environment_template": "strong"},
        "weak_probe": {"cwd_class": "owned_allowed_root", "environment_template": "weak"},
    },
    "environment_templates": {
        "strong": {"CODEX_HOME": "{codex_home}", "HOME": "{codex_home}", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin", "TMPDIR": "{tmp}"},
        "weak": {"CODEX_HOME": "{codex_home}", "HOME": "{codex_home}", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin", "PHASE_B0_SYNTHETIC_SECRET": "{synthetic_secret}", "TMPDIR": "{tmp}"},
    },
    "evidence": {"artifact_name": "readiness.json", "max_bytes": 65536, "schema_version": 1},
    "executables": {
        "codex": {"expected_mode": "0755", "expected_owner": "operator_uid", "sha256": "sha256:" + CODEX_SHA256, "version_line": "codex-cli " + CODEX_VERSION},
        "python": {"expected_mode": "0755", "expected_owner": "uid:0", "sha256": "sha256:" + PYTHON_SHA256, "version_line": "Python " + PYTHON_VERSION},
    },
    "ledger": {"ledger_name": "runtime.jsonl", "lock_name": "runtime.lock", "max_bytes": 262144, "max_record_bytes": 16384, "max_records": 64},
    "limits": {"command_timeout_seconds": 10, "executable_hash_chunk_bytes": 1048576, "max_executable_bytes": 536870912, "stderr_bytes": 8192, "stdout_bytes": 8192},
    "permission_profile": {"config_bytes_length": 284, "config_sha256": "sha256:74f108e8c6df73d7c045d5dd5453f795916a3b394b2c968dc7b63994fb41764a", "name": PERMISSION_PROFILE_NAME},
    "platform_family": "darwin",
    "policy_schema_version": 1,
}

_PRODUCTION_POLICY = _NativeReadinessPolicy(
    document=_POLICY_DOCUMENT,
    config_bytes=CONFIG_BYTES,
    child_source=CHILD_SOURCE,
)

assert len(CONFIG_BYTES) == 284
assert "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest() == _POLICY_DOCUMENT["permission_profile"]["config_sha256"]
assert len(CHILD_SOURCE.encode("utf-8")) == 2489
assert "sha256:" + hashlib.sha256(CHILD_SOURCE.encode("utf-8")).hexdigest() == _POLICY_DOCUMENT["child"]["sha256"]
assert sha256_id(_POLICY_DOCUMENT) == "sha256:af3a5979f7799bdf27f8ec100352a458b47e01464753d9f76e8f6826639e777c"


def _blocked(
    policy_digest: str,
    reason_code: str,
    cleanup_state: str = "not_started",
    digests: Tuple[Optional[str], ...] = (None,) * 6,
) -> NativeCanaryReadinessResult:
    return NativeCanaryReadinessResult(
        "blocked",
        0,
        policy_digest,
        digests[0],
        digests[1],
        digests[2],
        digests[3],
        digests[4],
        digests[5],
        cleanup_state,
        reason_code,
    )


def _is_canonical_absolute(path: object) -> bool:
    if not isinstance(path, Path):
        return False
    raw = str(path)
    return (
        os.path.isabs(raw)
        and raw == os.path.normpath(raw)
        and raw == os.path.realpath(raw)
    )


def _overlap(left: Path, right: Path) -> bool:
    left_parts = left.parts
    right_parts = right.parts
    return (
        left_parts == right_parts[:len(left_parts)]
        or right_parts == left_parts[:len(right_parts)]
    )


class _TrustedRoots:
    def __init__(
        self,
        temp_fd: int,
        private_fd: int,
        temp_identity: Tuple[int, int],
        private_identity: Tuple[int, int],
    ) -> None:
        self.temp_fd = temp_fd
        self.private_fd = private_fd
        self.temp_identity = temp_identity
        self.private_identity = private_identity

    def close(self) -> None:
        for name in ("private_fd", "temp_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, -1)


def _validate_root_fd(
    descriptor: int, path: Path, *, empty: bool
) -> Tuple[int, int]:
    retained = os.fstat(descriptor)
    current = os.stat(str(path), follow_symlinks=False)
    identity = (retained.st_dev, retained.st_ino)
    if (
        identity != (current.st_dev, current.st_ino)
        or not stat.S_ISDIR(retained.st_mode)
        or retained.st_uid != os.getuid()
        or stat.S_IMODE(retained.st_mode) != 0o700
        or (empty and os.listdir(descriptor))
    ):
        raise OSError(errno.EPERM, "request root is not trusted")
    return identity


def _validate_request(request: object) -> Optional[_TrustedRoots]:
    if type(request) is not NativeCanaryReadinessRequest:
        return None
    paths = (
        request.codex_executable,
        request.python_executable,
        request.temp_parent,
        request.private_root,
    )
    if not all(_is_canonical_absolute(path) for path in paths):
        return None
    if _overlap(request.temp_parent, request.private_root):
        return None
    temp_fd = private_fd = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        temp_fd = os.open(str(request.temp_parent), flags)
        private_fd = os.open(str(request.private_root), flags)
        temp_identity = _validate_root_fd(
            temp_fd, request.temp_parent, empty=True
        )
        private_identity = _validate_root_fd(
            private_fd, request.private_root, empty=False
        )
        return _TrustedRoots(
            temp_fd, private_fd, temp_identity, private_identity
        )
    except OSError:
        for descriptor in (private_fd, temp_fd):
            if descriptor >= 0:
                os.close(descriptor)
        return None


def _create_owned_directory(
    parent_path: Path, parent_fd: int, prefix: str
) -> Tuple[Path, str, Tuple[int, int]]:
    for _ in range(32):
        name = prefix + secrets.token_hex(12)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        path = parent_path / name
        path_info = os.stat(str(path), follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if (
            identity != (path_info.st_dev, path_info.st_ino)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError(errno.EPERM, "created directory identity invalid")
        return path, name, identity
    raise OSError(errno.EEXIST, "cannot create owned directory")


def _terminate_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    grace_deadline = time.monotonic() + 0.25
    while time.monotonic() < grace_deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass


def _run_bounded_command(command: BoundedCommand) -> BoundedCommandResult:
    """Run one command without a shell, inherited input, or ambient environment."""
    if (
        type(command) is not BoundedCommand
        or type(command.argv) is not tuple
        or not command.argv
        or any(type(value) is not str or "\x00" in value for value in command.argv)
        or not _is_canonical_absolute(command.cwd)
        or type(command.environment) is not dict
        or any(
            type(key) is not str or type(value) is not str or "\x00" in key + value
            for key, value in command.environment.items()
        )
        or any(
            type(value) is not int or value < 1
            for value in (
                command.timeout_seconds,
                command.stdout_limit,
                command.stderr_limit,
            )
        )
    ):
        raise ValueError("invalid bounded command")
    process = subprocess.Popen(
        command.argv,
        cwd=str(command.cwd),
        env=dict(command.environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", command.stdout_limit))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", command.stderr_limit))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + command.timeout_seconds
    timed_out = False
    overflow = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_group(process)
            break
        for key, _ in selector.select(min(remaining, 0.1)):
            name, limit = key.data
            chunk = os.read(key.fileobj.fileno(), min(8192, limit + 1))
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            room = max(0, limit - len(buffers[name]))
            buffers[name].extend(chunk[:room])
            if len(chunk) > room:
                overflow = True
                _terminate_group(process)
                break
        if overflow:
            break
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        _terminate_group(process)
        process.wait(timeout=0.5)
    drain_deadline = time.monotonic() + 0.25
    for stream in (process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
    while time.monotonic() < drain_deadline:
        made_progress = False
        for stream in (process.stdout, process.stderr):
            try:
                chunk = os.read(stream.fileno(), 8192)
                made_progress = made_progress or bool(chunk)
            except BlockingIOError:
                pass
            except OSError:
                pass
        if not made_progress:
            time.sleep(0.01)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    selector.close()
    return BoundedCommandResult(
        None if timed_out else process.returncode,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
        timed_out,
        overflow,
    )


class _IdentityError(RuntimeError):
    pass


class _SealedExecutable:
    def __init__(self, path: Path, specification: Mapping[str, object], limits: Mapping[str, object]) -> None:
        self.path = path
        self._specification = specification
        self._limits = limits
        self._descriptor = -1
        try:
            self._descriptor = os.open(
                str(path),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            info = self._validate_metadata(os.fstat(self._descriptor))
            self._identity = (info.st_dev, info.st_ino)
            self._content_digest = self._hash_descriptor()
            if self._content_digest != specification["sha256"]:
                raise _IdentityError("executable content mismatch")
        except (OSError, KeyError, TypeError, ValueError) as error:
            self.close()
            raise _IdentityError("executable identity invalid") from error

    def _validate_metadata(self, info: os.stat_result) -> os.stat_result:
        expected_owner = self._specification["expected_owner"]
        owner = os.getuid() if expected_owner == "operator_uid" else int(str(expected_owner)[4:])
        mode = int(str(self._specification["expected_mode"]), 8)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size > self._limits["max_executable_bytes"]
        ):
            raise _IdentityError("executable metadata mismatch")
        return info

    def _hash_descriptor(self) -> str:
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(
                self._descriptor,
                min(
                    self._limits["executable_hash_chunk_bytes"],
                    self._limits["max_executable_bytes"] + 1 - total,
                ),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > self._limits["max_executable_bytes"]:
                raise _IdentityError("executable too large")
            digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def validate(self) -> None:
        retained = self._validate_metadata(os.fstat(self._descriptor))
        current = self._validate_metadata(os.stat(str(self.path), follow_symlinks=False))
        if (
            (retained.st_dev, retained.st_ino) != self._identity
            or (current.st_dev, current.st_ino) != self._identity
            or self._hash_descriptor() != self._content_digest
        ):
            raise _IdentityError("executable identity changed")

    def evidence_digest(self, role: str) -> str:
        return sha256_id(
            {
                "content_digest": self._content_digest,
                "expected_mode": self._specification["expected_mode"],
                "expected_owner": self._specification["expected_owner"],
                "role": role,
                "single_link": True,
            }
        )

    def close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1


def _validated_runner_result(value: object, command: BoundedCommand) -> BoundedCommandResult:
    if type(value) is not BoundedCommandResult:
        raise RuntimeError("malformed command result")
    if (
        (value.returncode is not None and type(value.returncode) is not int)
        or type(value.stdout) is not bytes
        or type(value.stderr) is not bytes
        or type(value.timed_out) is not bool
        or type(value.output_overflow) is not bool
        or len(value.stdout) > command.stdout_limit
        or len(value.stderr) > command.stderr_limit
        or (value.timed_out and value.returncode is not None)
    ):
        raise RuntimeError("malformed command result")
    return value


def _invoke(
    command: BoundedCommand,
    runner: Callable[[BoundedCommand], BoundedCommandResult],
    executables: Tuple[_SealedExecutable, _SealedExecutable],
) -> BoundedCommandResult:
    for executable in executables:
        executable.validate()
    result = _validated_runner_result(runner(command), command)
    for executable in executables:
        executable.validate()
    return result


def _environment(codex_home: Path, temporary: Path, secret: Optional[str]) -> Mapping[str, str]:
    value = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(codex_home),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(temporary),
        "LANG": "C",
        "LC_ALL": "C",
    }
    if secret is not None:
        value["PHASE_B0_SYNTHETIC_SECRET"] = secret
    return value


def _command(
    argv: Tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    limits: Mapping[str, object],
) -> BoundedCommand:
    return BoundedCommand(
        argv,
        cwd,
        dict(environment),
        limits["command_timeout_seconds"],
        limits["stdout_bytes"],
        limits["stderr_bytes"],
    )


def _version_matches(result: BoundedCommandResult, expected: str) -> bool:
    return (
        result.returncode == 0
        and not result.timed_out
        and not result.output_overflow
        and result.stderr == b""
        and result.stdout in (expected.encode("utf-8"), expected.encode("utf-8") + b"\n")
    )


_CHILD_KEYS = {
    "allowed_read",
    "allowed_write",
    "environment",
    "forbidden_read",
    "forbidden_write",
    "network",
    "schema_version",
}


def _child_digest(value: object) -> bool:
    return (
        value is None
        or (
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )
    )


def _child_errno(value: object) -> bool:
    return value is None or type(value) is int


def _parse_child(result: BoundedCommandResult) -> Mapping[str, object]:
    if (
        result.returncode != 0
        or result.timed_out
        or result.output_overflow
        or result.stderr
        or not result.stdout.endswith(b"\n")
    ):
        raise RuntimeError("child command failed")
    raw = result.stdout[:-1]
    try:
        value = load_canonical_input(raw)
    except CanonicalJSONError as error:
        raise RuntimeError("child output invalid") from error
    if (
        canonical_bytes(value) != raw
        or type(value) is not dict
        or set(value) != _CHILD_KEYS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise RuntimeError("child output schema invalid")
    shapes = {
        "allowed_read": {"digest", "errno", "ok", "overflow"},
        "forbidden_read": {"digest", "errno", "ok", "overflow"},
        "allowed_write": {"errno", "ok"},
        "forbidden_write": {"errno", "ok"},
        "environment": {"digest", "present"},
        "network": {"errno", "ok"},
    }
    for key, keys in shapes.items():
        item = value.get(key)
        if type(item) is not dict or set(item) != keys:
            raise RuntimeError("child output schema invalid")
    for key in ("allowed_read", "forbidden_read"):
        item = value[key]
        if (
            not _child_digest(item["digest"])
            or not _child_errno(item["errno"])
            or type(item["ok"]) is not bool
            or type(item["overflow"]) is not bool
        ):
            raise RuntimeError("child output scalar invalid")
    for key in ("allowed_write", "forbidden_write", "network"):
        item = value[key]
        if (
            not _child_errno(item["errno"])
            or type(item["ok"]) is not bool
        ):
            raise RuntimeError("child output scalar invalid")
    environment = value["environment"]
    if (
        not _child_digest(environment["digest"])
        or type(environment["present"]) is not bool
    ):
        raise RuntimeError("child output scalar invalid")
    return value


class _LoopbackListener:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self._socket.settimeout(0.25)
        self.port = self._socket.getsockname()[1]

    def observation(self, token: bytes) -> str:
        try:
            connection, address = self._socket.accept()
        except socket.timeout:
            return "none"
        except OSError:
            return "failure"
        try:
            connection.settimeout(0.25)
            data = b""
            while len(data) < len(token):
                chunk = connection.recv(len(token) - len(data))
                if not chunk:
                    break
                data += chunk
            if address[0] == "127.0.0.1" and data == token:
                return "matched"
            return "mismatch"
        except OSError:
            return "failure"
        finally:
            connection.close()

    def close(self) -> None:
        self._socket.close()


def _probe_argv(
    codex: Optional[Path],
    python: Path,
    allowed: Path,
    paths: Tuple[Path, Path, Path, Path],
    listener: _LoopbackListener,
    token: bytes,
    child_source: str,
) -> Tuple[str, ...]:
    child = (
        str(python), "-I", "-S", "-B", "-c", child_source,
        str(paths[0]), str(paths[1]), str(paths[2]), str(paths[3]),
        str(listener.port), token.hex(),
    )
    if codex is None:
        return child
    return (
        str(codex), "sandbox", "-P", PERMISSION_PROFILE_NAME,
        "-C", str(allowed), "--",
    ) + child


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _TrustedProbeFile:
    def __init__(
        self,
        directory: "_TrustedProbeDirectory",
        name: str,
        descriptor: int,
        expected: bytes,
    ) -> None:
        self._directory = directory
        self.name = name
        self._descriptor = descriptor
        self._expected = expected
        info = self._validate_metadata(os.fstat(descriptor))
        self._identity = (info.st_dev, info.st_ino)
        self.validate()

    @staticmethod
    def _validate_metadata(info: os.stat_result) -> os.stat_result:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise OSError(errno.EPERM, "probe file metadata invalid")
        return info

    def validate(self) -> None:
        self._directory.validate()
        retained = self._validate_metadata(os.fstat(self._descriptor))
        current = self._validate_metadata(
            os.stat(
                self.name,
                dir_fd=self._directory.descriptor,
                follow_symlinks=False,
            )
        )
        if (
            (retained.st_dev, retained.st_ino) != self._identity
            or (current.st_dev, current.st_ino) != self._identity
            or os.pread(
                self._descriptor, len(self._expected) + 1, 0
            )
            != self._expected
        ):
            raise OSError(errno.EPERM, "probe file identity changed")

    def unlink(self) -> None:
        self.validate()
        os.unlink(self.name, dir_fd=self._directory.descriptor)
        try:
            os.stat(
                self.name,
                dir_fd=self._directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        raise OSError(errno.EPERM, "probe output still exists")

    def close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1


class _TrustedProbeDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor = os.open(
            str(path),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        info = self._validate_metadata(os.fstat(self.descriptor))
        self._identity = (info.st_dev, info.st_ino)
        self.validate()

    @staticmethod
    def _validate_metadata(info: os.stat_result) -> os.stat_result:
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError(errno.EPERM, "probe directory metadata invalid")
        return info

    def validate(self) -> None:
        retained = self._validate_metadata(os.fstat(self.descriptor))
        current = self._validate_metadata(
            os.stat(str(self.path), follow_symlinks=False)
        )
        if (
            (retained.st_dev, retained.st_ino) != self._identity
            or (current.st_dev, current.st_ino) != self._identity
        ):
            raise OSError(errno.EPERM, "probe directory identity changed")

    def create(self, name: str, data: bytes) -> _TrustedProbeFile:
        self.validate()
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=self.descriptor,
        )
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "write made no progress")
                offset += written
            os.fsync(descriptor)
            return _TrustedProbeFile(self, name, descriptor, data)
        except Exception:
            os.close(descriptor)
            raise

    def open_output(self, name: str, expected: bytes) -> _TrustedProbeFile:
        self.validate()
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.descriptor,
        )
        try:
            return _TrustedProbeFile(self, name, descriptor, expected)
        except Exception:
            os.close(descriptor)
            raise

    def require_absent(self, name: str) -> None:
        self.validate()
        try:
            os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise OSError(errno.EEXIST, "denied output path exists")

    def close(self) -> None:
        if self.descriptor >= 0:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = -1


class _EvidencePublisher:
    def __init__(
        self,
        private_root: Path,
        validated_root_fd: int,
        validated_root_identity: Tuple[int, int],
    ) -> None:
        self._root_path = private_root
        self._root_fd = -1
        self._run_fd = -1
        self._root_identity = (-1, -1)
        self._run_identity = (-1, -1)
        self._name = ""
        try:
            self._root_fd = os.dup(validated_root_fd)
            root_info = os.fstat(self._root_fd)
            self._root_identity = (root_info.st_dev, root_info.st_ino)
            if self._root_identity != validated_root_identity:
                raise OSError(errno.EPERM, "validated evidence root changed")
            root_path_info = os.stat(
                str(private_root), follow_symlinks=False
            )
            if (
                (root_path_info.st_dev, root_path_info.st_ino)
                != self._root_identity
                or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.getuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700
            ):
                raise OSError(errno.EPERM, "validated evidence root changed")
            for _ in range(32):
                candidate = "native-readiness-" + secrets.token_hex(12)
                try:
                    os.mkdir(candidate, 0o700, dir_fd=self._root_fd)
                except FileExistsError:
                    continue
                self._name = candidate
                break
            if not self._name:
                raise OSError(errno.EEXIST, "cannot create evidence directory")
            os.fsync(self._root_fd)
            self._run_fd = os.open(
                self._name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._root_fd,
            )
            run_info = os.fstat(self._run_fd)
            self._run_identity = (run_info.st_dev, run_info.st_ino)
            self._validate()
        except Exception:
            self.close()
            raise
        self.run_directory = private_root / self._name
        self.path = self.run_directory / "readiness.json"

    def _validate(self) -> None:
        root_retained = os.fstat(self._root_fd)
        root_path = os.stat(str(self._root_path), follow_symlinks=False)
        run_retained = os.fstat(self._run_fd)
        run_path = os.stat(self._name, dir_fd=self._root_fd, follow_symlinks=False)
        if (
            (root_retained.st_dev, root_retained.st_ino) != self._root_identity
            or (root_path.st_dev, root_path.st_ino) != self._root_identity
            or (run_retained.st_dev, run_retained.st_ino) != self._run_identity
            or (run_path.st_dev, run_path.st_ino) != self._run_identity
            or not stat.S_ISDIR(root_retained.st_mode)
            or root_retained.st_uid != os.getuid()
            or stat.S_IMODE(root_retained.st_mode) != 0o700
            or not stat.S_ISDIR(run_retained.st_mode)
            or run_retained.st_uid != os.getuid()
            or stat.S_IMODE(run_retained.st_mode) != 0o700
        ):
            raise OSError(errno.EPERM, "evidence directory identity invalid")

    def _stage(self, retained: bytes) -> Tuple[str, Tuple[int, int]]:
        staging = "staging-" + secrets.token_hex(12)
        descriptor = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._run_fd,
        )
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(retained):
                written = os.write(descriptor, retained[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "write made no progress")
                offset += written
            os.fsync(descriptor)
            info = self._validate_evidence_metadata(os.fstat(descriptor))
            identity = (info.st_dev, info.st_ino)
        except Exception:
            try:
                os.unlink(staging, dir_fd=self._run_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(descriptor)
        return staging, identity

    @staticmethod
    def _validate_evidence_metadata(info: os.stat_result) -> os.stat_result:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise OSError(errno.EIO, "retained evidence metadata invalid")
        return info

    def _verify_descriptor(
        self, descriptor: int, expected: bytes
    ) -> bytes:
        before = self._validate_evidence_metadata(os.fstat(descriptor))
        identity = (before.st_dev, before.st_ino)
        path_before = self._validate_evidence_metadata(
            os.stat(
                "readiness.json",
                dir_fd=self._run_fd,
                follow_symlinks=False,
            )
        )
        data = os.read(descriptor, 65537)
        after = self._validate_evidence_metadata(os.fstat(descriptor))
        path_after = self._validate_evidence_metadata(
            os.stat(
                "readiness.json",
                dir_fd=self._run_fd,
                follow_symlinks=False,
            )
        )
        if (
            identity != (path_before.st_dev, path_before.st_ino)
            or identity != (after.st_dev, after.st_ino)
            or identity != (path_after.st_dev, path_after.st_ino)
            or data != expected
        ):
            raise OSError(errno.EIO, "retained evidence identity invalid")
        reparsed = load_canonical_input(data)
        if canonical_bytes(reparsed) != data:
            raise OSError(errno.EIO, "retained evidence is not canonical")
        self._validate()
        return data

    def _remove_identity(self, identity: Tuple[int, int]) -> None:
        for name in os.listdir(self._run_fd):
            try:
                info = os.stat(
                    name, dir_fd=self._run_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if (info.st_dev, info.st_ino) == identity:
                os.unlink(name, dir_fd=self._run_fd)

    def _restore_blocked(
        self,
        fallback_staging: str,
        fallback_bytes: bytes,
        success_identity: Tuple[int, int],
    ) -> None:
        restored = False
        try:
            os.replace(
                fallback_staging,
                "readiness.json",
                src_dir_fd=self._run_fd,
                dst_dir_fd=self._run_fd,
            )
            os.fsync(self._run_fd)
            descriptor = os.open(
                "readiness.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._run_fd,
            )
            try:
                self._verify_descriptor(descriptor, fallback_bytes)
            finally:
                os.close(descriptor)
            restored = True
        finally:
            self._remove_identity(success_identity)
            if not restored:
                try:
                    current = os.stat(
                        "readiness.json",
                        dir_fd=self._run_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) == success_identity:
                        os.unlink("readiness.json", dir_fd=self._run_fd)
                except FileNotFoundError:
                    pass
                os.fsync(self._run_fd)

    def publish(
        self,
        value: Mapping[str, object],
        *,
        fallback: Optional[Mapping[str, object]] = None,
    ) -> bytes:
        self._validate()
        retained = canonical_bytes(value)
        if len(retained) > 65536:
            raise OSError(errno.EFBIG, "evidence exceeds byte limit")
        fallback_bytes = canonical_bytes(fallback) if fallback is not None else None
        if fallback_bytes is not None and len(fallback_bytes) > 65536:
            raise OSError(errno.EFBIG, "fallback evidence exceeds byte limit")
        fallback_staging = None
        staging = None
        success_identity = None
        descriptor = -1
        replaced = False
        try:
            if fallback_bytes is not None:
                fallback_staging, _ = self._stage(fallback_bytes)
            staging, success_identity = self._stage(retained)
            os.replace(
                staging,
                "readiness.json",
                src_dir_fd=self._run_fd,
                dst_dir_fd=self._run_fd,
            )
            replaced = True
            descriptor = os.open(
                "readiness.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._run_fd,
            )
            info = self._validate_evidence_metadata(os.fstat(descriptor))
            if (info.st_dev, info.st_ino) != success_identity:
                raise OSError(errno.EIO, "published evidence identity changed")
            os.fsync(self._run_fd)
            data = self._verify_descriptor(descriptor, retained)
            return data
        except Exception:
            if (
                replaced
                and fallback_staging is not None
                and fallback_bytes is not None
                and success_identity is not None
            ):
                self._restore_blocked(
                    fallback_staging, fallback_bytes, success_identity
                )
                fallback_staging = None
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for leftover in (staging, fallback_staging):
                if leftover is None:
                    continue
                try:
                    os.unlink(leftover, dir_fd=self._run_fd)
                except FileNotFoundError:
                    pass

    def close(self) -> None:
        for descriptor_name in ("_run_fd", "_root_fd"):
            descriptor = getattr(self, descriptor_name, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)


def _remove_directory_contents(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                retained = os.fstat(child_fd)
                if (retained.st_dev, retained.st_ino) != identity:
                    raise OSError(errno.EPERM, "cleanup child identity changed")
                _remove_directory_contents(child_fd)
                current = os.stat(
                    name, dir_fd=descriptor, follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) != identity:
                    raise OSError(errno.EPERM, "cleanup child path changed")
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != identity:
                raise OSError(errno.EPERM, "cleanup file path changed")
            os.unlink(name, dir_fd=descriptor)
        else:
            raise OSError(errno.EPERM, "unsupported cleanup entry")


def _remove_tree(
    parent_fd: int, name: str, identity: Tuple[int, int]
) -> bool:
    descriptor = -1
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (current.st_dev, current.st_ino) != identity
            or not stat.S_ISDIR(current.st_mode)
        ):
            return False
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        retained = os.fstat(descriptor)
        if (retained.st_dev, retained.st_ino) != identity:
            return False
        _remove_directory_contents(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            return False
        os.rmdir(name, dir_fd=parent_fd)
        return True
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_probe_ledger(
    directory: Path, genesis: str, limits: Mapping[str, object]
) -> PrivateJSONLLedger:
    return PrivateJSONLLedger.open(
        directory,
        genesis,
        max_records=limits["max_records"],
        max_bytes=limits["max_bytes"],
        max_record_bytes=limits["max_record_bytes"],
    )


def _run_ledger_probe(
    directory: Path, policy_digest: str, limits: Mapping[str, object]
) -> str:
    directory.mkdir(mode=0o700)
    first = canonical_bytes(
        {"observation": "append", "previous_record_hash": policy_digest, "schema_version": 1}
    )
    with _open_probe_ledger(directory, policy_digest, limits) as ledger:
        state = ledger.append(first)
    second = canonical_bytes(
        {"observation": "reopen", "previous_record_hash": state.last_record_hash, "schema_version": 1}
    )
    with _open_probe_ledger(directory, policy_digest, limits) as ledger:
        final = ledger.append(second)
    _probe_ledger_lock(directory.parent / "lock-contention", policy_digest, limits)
    _probe_ledger_corruption(directory.parent, policy_digest, limits)
    return sha256_id(
        {
            "append_fsync_returned": True,
            "creation_directory_fsync_returned": True,
            "lock_contention_rejected": True,
            "normal_reopen_validated": True,
            "physical_corruption_rejected": True,
            "record_count": final.record_count,
        }
    )


def _probe_ledger_lock(
    directory: Path, genesis: str, limits: Mapping[str, object]
) -> None:
    directory.mkdir(mode=0o700)
    ledger = _open_probe_ledger(directory, genesis, limits)
    try:
        read_fd, write_fd = os.pipe()
        try:
            process_id = os.fork()
        except OSError:
            os.close(write_fd)
            os.close(read_fd)
            raise
        if process_id == 0:
            os.close(read_fd)
            exit_code = 1
            try:
                rejected = False
                try:
                    contender = _open_probe_ledger(
                        directory, genesis, limits
                    )
                except PrivateJSONLLedgerError:
                    rejected = True
                else:
                    contender.close()
                os.write(write_fd, b"1" if rejected else b"0")
                exit_code = 0
            except BaseException:
                exit_code = 1
            finally:
                try:
                    os.close(write_fd)
                finally:
                    os._exit(exit_code)
        os.close(write_fd)
        observed, status_value = _wait_for_lock_probe(
            process_id, read_fd, 0.5
        )
        if observed != b"1" or status_value != 0:
            raise PrivateJSONLLedgerError("ledger lock contention was not rejected")
    finally:
        ledger.close()


def _wait_for_lock_probe(
    process_id: int, read_fd: int, timeout_seconds: float
) -> Tuple[bytes, int]:
    selector = selectors.DefaultSelector()
    observed = bytearray()
    status_value = None
    deadline = time.monotonic() + timeout_seconds
    try:
        os.set_blocking(read_fd, False)
        selector.register(read_fd, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            for _, _ in selector.select(
                min(0.05, max(0.0, deadline - time.monotonic()))
            ):
                try:
                    chunk = os.read(read_fd, 2 - len(observed))
                except BlockingIOError:
                    chunk = None
                if chunk:
                    observed.extend(chunk)
                elif chunk == b"":
                    selector.unregister(read_fd)
            waited, status = os.waitpid(process_id, os.WNOHANG)
            if waited == process_id:
                status_value = status
                if not selector.get_map():
                    break
            if len(observed) > 1:
                break
        if status_value is None:
            try:
                os.kill(process_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status_value = os.waitpid(process_id, 0)
            raise PrivateJSONLLedgerError("ledger lock probe timed out")
        return bytes(observed), status_value
    except (OSError, ChildProcessError) as error:
        try:
            os.kill(process_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(process_id, 0)
        except ChildProcessError:
            pass
        raise PrivateJSONLLedgerError("ledger lock probe failed") from error
    finally:
        selector.close()
        try:
            os.close(read_fd)
        except OSError:
            pass


def _expect_ledger_rejection(action: Callable[[], object]) -> None:
    try:
        value = action()
    except PrivateJSONLLedgerError:
        return
    if isinstance(value, PrivateJSONLLedger):
        value.close()
    raise PrivateJSONLLedgerError("ledger corruption was accepted")


def _probe_ledger_corruption(
    parent: Path, genesis: str, limits: Mapping[str, object]
) -> None:
    replacement = parent / "replacement"
    replacement.mkdir(mode=0o700)
    open_ledger = _open_probe_ledger(replacement, genesis, limits)
    original = replacement / "runtime.jsonl"
    moved = replacement / "moved.jsonl"
    os.rename(str(original), str(moved))
    _write_exclusive(original, b"", 0o600)
    record = canonical_bytes(
        {"observation": "replacement", "previous_record_hash": genesis}
    )
    _expect_ledger_rejection(lambda: open_ledger.append(record))
    open_ledger.close()

    partial = parent / "partial-tail"
    partial.mkdir(mode=0o700)
    with _open_probe_ledger(partial, genesis, limits) as ledger:
        pass
    with open(str(partial / "runtime.jsonl"), "ab") as stream:
        stream.write(b"{")
        stream.flush()
        os.fsync(stream.fileno())
    _expect_ledger_rejection(lambda: _open_probe_ledger(partial, genesis, limits))

    wrong_chain = parent / "wrong-chain"
    wrong_chain.mkdir(mode=0o700)
    with _open_probe_ledger(wrong_chain, genesis, limits) as ledger:
        wrong = canonical_bytes(
            {"observation": "wrong", "previous_record_hash": "sha256:" + "0" * 64}
        )
        _expect_ledger_rejection(lambda: ledger.append(wrong))

    wrong_directory = parent / "wrong-directory-mode"
    wrong_directory.mkdir(mode=0o700)
    os.chmod(str(wrong_directory), 0o755)
    _expect_ledger_rejection(
        lambda: _open_probe_ledger(wrong_directory, genesis, limits)
    )

    for name, target in (
        ("ledger-mode", "runtime.jsonl"),
        ("ledger-hardlink", "runtime.jsonl"),
        ("lock-hardlink", "runtime.lock"),
    ):
        probe = parent / name
        probe.mkdir(mode=0o700)
        with _open_probe_ledger(probe, genesis, limits):
            pass
        if name == "ledger-mode":
            os.chmod(str(probe / target), 0o644)
        else:
            os.link(str(probe / target), str(probe / (target + ".alias")))
        _expect_ledger_rejection(
            lambda probe=probe: _open_probe_ledger(probe, genesis, limits)
        )

    for name, target in (
        ("ledger-symlink", "runtime.jsonl"),
        ("lock-symlink", "runtime.lock"),
    ):
        probe = parent / name
        probe.mkdir(mode=0o700)
        with _open_probe_ledger(probe, genesis, limits):
            pass
        os.unlink(str(probe / target))
        os.symlink("missing", str(probe / target))
        _expect_ledger_rejection(
            lambda probe=probe: _open_probe_ledger(probe, genesis, limits)
        )


def _run_with_policy(
    request: NativeCanaryReadinessRequest,
    policy: _NativeReadinessPolicy,
    observed_platform: str,
    command_runner: Optional[Callable[[BoundedCommand], BoundedCommandResult]] = None,
) -> NativeCanaryReadinessResult:
    policy_digest = sha256_id(policy.document)
    roots = _validate_request(request)
    if roots is None:
        return _blocked(policy_digest, "request_invalid")
    if observed_platform != policy.document["platform_family"]:
        roots.close()
        return _blocked(policy_digest, "unsupported_platform")
    codex = python = None
    temporary = None
    temporary_name = None
    temporary_identity = None
    publisher = None
    probe_files = []
    probe_directories = []
    executable_digests: Tuple[Optional[str], Optional[str]] = (None, None)
    permission_digest = supervisor_digest = ledger_digest = complete_digest = None
    runner = command_runner or _run_bounded_command
    reason = "executable_identity_invalid"
    try:
        limits = policy.document["limits"]
        codex = _SealedExecutable(
            request.codex_executable, policy.document["executables"]["codex"], limits
        )
        python = _SealedExecutable(
            request.python_executable, policy.document["executables"]["python"], limits
        )
        executable_digests = (
            codex.evidence_digest("codex"),
            python.evidence_digest("python"),
        )
        temporary, temporary_name, temporary_identity = _create_owned_directory(
            request.temp_parent,
            roots.temp_fd,
            "native-readiness-",
        )
        allowed = temporary / "allowed"
        forbidden = temporary / "forbidden"
        codex_home = temporary / "codex-home"
        ledger_directory = temporary / "ledger"
        for directory in (allowed, forbidden, codex_home):
            directory.mkdir(mode=0o700)
        strong_environment = _environment(codex_home, temporary, None)
        executables = (codex, python)
        reason = "cli_version_mismatch"
        codex_version = _invoke(
            _command(
                (str(request.codex_executable), "--version"),
                allowed,
                strong_environment,
                limits,
            ),
            runner,
            executables,
        )
        python_version = _invoke(
            _command(
                (str(request.python_executable), "--version"),
                allowed,
                strong_environment,
                limits,
            ),
            runner,
            executables,
        )
        if not _version_matches(
            codex_version, policy.document["executables"]["codex"]["version_line"]
        ) or not _version_matches(
            python_version, policy.document["executables"]["python"]["version_line"]
        ):
            reason = "cli_version_mismatch"
            raise RuntimeError("version mismatch")
        token = secrets.token_bytes(32)
        secret = secrets.token_hex(32)
        allowed_read = allowed / "allowed-read"
        forbidden_read = forbidden / "forbidden-read"
        allowed_write = allowed / "allowed-write"
        forbidden_write = forbidden / "forbidden-write"
        allowed_probe = _TrustedProbeDirectory(allowed)
        forbidden_probe = _TrustedProbeDirectory(forbidden)
        probe_directories.extend((allowed_probe, forbidden_probe))
        allowed_sentinel = allowed_probe.create("allowed-read", token + b"a")
        forbidden_sentinel = forbidden_probe.create(
            "forbidden-read", token + b"f"
        )
        probe_files.extend((allowed_sentinel, forbidden_sentinel))
        paths = (allowed_read, forbidden_read, allowed_write, forbidden_write)
        reason = "weak_control_invalid"
        weak_listener = _LoopbackListener()
        try:
            weak_result = _invoke(
                _command(
                    _probe_argv(
                        None, request.python_executable, allowed, paths,
                        weak_listener, token, policy.child_source,
                    ),
                    allowed,
                    _environment(codex_home, temporary, secret),
                    limits,
                ),
                runner,
                executables,
            )
            weak = _parse_child(weak_result)
            weak_network = weak_listener.observation(token)
        finally:
            weak_listener.close()
        weak_valid = (
            weak_network == "matched"
            and weak["allowed_read"]["ok"] is True
            and weak["allowed_read"]["digest"] == hashlib.sha256(token + b"a").hexdigest()
            and weak["allowed_read"]["errno"] is None
            and weak["allowed_read"]["overflow"] is False
            and weak["forbidden_read"]["ok"] is True
            and weak["forbidden_read"]["digest"] == hashlib.sha256(token + b"f").hexdigest()
            and weak["forbidden_read"]["errno"] is None
            and weak["forbidden_read"]["overflow"] is False
            and weak["allowed_write"]["ok"] is True
            and weak["allowed_write"]["errno"] is None
            and weak["forbidden_write"]["ok"] is True
            and weak["forbidden_write"]["errno"] is None
            and weak["network"] == {"errno": None, "ok": True}
            and weak["environment"]["present"] is True
            and weak["environment"]["digest"] == hashlib.sha256(secret.encode()).hexdigest()
        )
        if not weak_valid:
            reason = "weak_control_invalid"
            raise RuntimeError("weak control invalid")
        allowed_output = allowed_probe.open_output("allowed-write", token)
        forbidden_output = forbidden_probe.open_output(
            "forbidden-write", token
        )
        try:
            allowed_sentinel.validate()
            forbidden_sentinel.validate()
            allowed_output.unlink()
            forbidden_output.unlink()
        finally:
            allowed_output.close()
            forbidden_output.close()
        allowed_sentinel.validate()
        forbidden_sentinel.validate()
        _write_exclusive(codex_home / "config.toml", policy.config_bytes, 0o600)
        reason = "native_permission_unproven"
        strong_listener = _LoopbackListener()
        try:
            strong_result = _invoke(
                _command(
                    _probe_argv(
                        request.codex_executable, request.python_executable,
                        allowed, paths, strong_listener, token, policy.child_source,
                    ),
                    allowed,
                    strong_environment,
                    limits,
                ),
                runner,
                executables,
            )
            strong = _parse_child(strong_result)
            strong_network = strong_listener.observation(token)
        finally:
            strong_listener.close()
        strong_valid = (
            strong["allowed_read"]["ok"] is True
            and strong["allowed_read"]["digest"] == hashlib.sha256(token + b"a").hexdigest()
            and strong["allowed_read"]["errno"] is None
            and strong["allowed_read"]["overflow"] is False
            and strong["forbidden_read"]["ok"] is False
            and strong["forbidden_read"]["digest"] is None
            and strong["forbidden_read"]["errno"] in (errno.EACCES, errno.EPERM)
            and strong["forbidden_read"]["overflow"] is False
            and strong["allowed_write"]["ok"] is False
            and strong["allowed_write"]["errno"] in (errno.EACCES, errno.EPERM)
            and strong["forbidden_write"]["ok"] is False
            and strong["forbidden_write"]["errno"] in (errno.EACCES, errno.EPERM)
            and strong["environment"] == {"digest": None, "present": False}
            and strong["network"]["ok"] is False
            and strong["network"]["errno"] in (errno.EACCES, errno.EPERM)
            and strong_network == "none"
        )
        if not strong_valid:
            reason = "native_permission_unproven"
            raise RuntimeError("native permission unproven")
        allowed_probe.require_absent("allowed-write")
        forbidden_probe.require_absent("forbidden-write")
        allowed_sentinel.validate()
        forbidden_sentinel.validate()
        permission_digest = sha256_id(
            {
                "allowed_read": True,
                "filesystem_denials": 3,
                "loopback_denied_by_permission": True,
                "schema_version": 1,
                "weak_control_complete": True,
            }
        )
        supervisor_digest = sha256_id(
            {
                "ambient_environment_inherited": False,
                "strong_environment_keys": sorted(strong_environment),
                "synthetic_secret_absent": True,
                "weak_environment_extra_keys": ["PHASE_B0_SYNTHETIC_SECRET"],
            }
        )
        reason = "ledger_primitives_unproven"
        try:
            ledger_digest = _run_ledger_probe(
                ledger_directory, policy_digest, policy.document["ledger"]
            )
        except (OSError, PrivateJSONLLedgerError):
            reason = "ledger_primitives_unproven"
            raise RuntimeError("ledger primitives unproven")
        reason = "evidence_retention_failed"
        publisher = _EvidencePublisher(
            request.private_root,
            roots.private_fd,
            roots.private_identity,
        )
        publisher.publish(
            {
                "cleanup_state": "pending",
                "reason_code": "cleanup_required",
                "schema_version": 1,
                "status": "blocked",
            }
        )
        reason = "native_primitive_observations_recorded"
    except _IdentityError:
        reason = "executable_identity_invalid"
        executable_digests = (None, None)
    except Exception:
        pass
    finally:
        if codex is not None:
            codex.close()
        if python is not None:
            python.close()
        for probe_file in probe_files:
            probe_file.close()
        for probe_directory in probe_directories:
            probe_directory.close()
    cleanup_ok = True
    if temporary_name is not None and temporary_identity is not None:
        cleanup_ok = _remove_tree(
            roots.temp_fd, temporary_name, temporary_identity
        )
    if not cleanup_ok:
        mask = (
            executable_digests[0], executable_digests[1],
            permission_digest, supervisor_digest, ledger_digest, complete_digest,
        )
        if publisher is not None:
            try:
                publisher.publish(
                    {
                        "cleanup_state": "cleanup_required",
                        "reason_code": "cleanup_required",
                        "schema_version": 1,
                        "status": "blocked",
                    }
                )
            except (CanonicalJSONError, OSError):
                pass
            finally:
                publisher.close()
        roots.close()
        return _blocked(
            policy_digest, "cleanup_required", "cleanup_required", mask
        )
    available = (
        executable_digests[0], executable_digests[1],
        permission_digest, supervisor_digest, ledger_digest, complete_digest,
    )
    if reason == "native_primitive_observations_recorded":
        try:
            assert publisher is not None
            terminal_document = {
                "cleanup_state": "removed",
                "codex_executable_identity_digest": executable_digests[0],
                "ledger_probe_evidence_digest": ledger_digest,
                "permission_profile_evidence_digest": permission_digest,
                "policy_digest": policy_digest,
                "python_executable_identity_digest": executable_digests[1],
                "reason_code": reason,
                "schema_version": 1,
                "status": "native_primitive_observations_only",
                "supervisor_environment_evidence_digest": supervisor_digest,
            }
            complete_digest = sha256_id(terminal_document)
            retained = publisher.publish(
                terminal_document,
                fallback={
                    "cleanup_state": "removed",
                    "reason_code": "evidence_retention_failed",
                    "schema_version": 1,
                    "status": "blocked",
                },
            )
            if "sha256:" + hashlib.sha256(retained).hexdigest() != complete_digest:
                raise OSError(errno.EIO, "retained evidence digest mismatch")
        except (AssertionError, CanonicalJSONError, OSError):
            reason = "evidence_retention_failed"
            complete_digest = None
            available = available[:5] + (None,)
        finally:
            if publisher is not None:
                publisher.close()
    elif publisher is not None:
        publisher.close()
    if reason == "native_primitive_observations_recorded":
        roots.close()
        return NativeCanaryReadinessResult(
            "native_primitive_observations_only",
            0,
            policy_digest,
            executable_digests[0],
            executable_digests[1],
            permission_digest,
            supervisor_digest,
            ledger_digest,
            complete_digest,
            "removed",
            reason,
        )
    if reason == "executable_identity_invalid" and temporary is None:
        roots.close()
        return _blocked(policy_digest, reason, "not_started", (None,) * 6)
    masks = {
        "executable_identity_invalid": (None,) * 6,
        "cli_version_mismatch": executable_digests + (None,) * 4,
        "weak_control_invalid": executable_digests + (None,) * 4,
        "native_permission_unproven": executable_digests + (None,) * 4,
        "ledger_primitives_unproven": executable_digests + (
            permission_digest, supervisor_digest, None, None,
        ),
        "evidence_retention_failed": executable_digests + (
            permission_digest, supervisor_digest, ledger_digest, None,
        ),
    }
    roots.close()
    return _blocked(policy_digest, reason, "removed", masks[reason])


def run_native_canary_readiness(
    request: NativeCanaryReadinessRequest,
    *,
    command_runner: Optional[Callable[[BoundedCommand], BoundedCommandResult]] = None,
) -> NativeCanaryReadinessResult:
    return _run_with_policy(
        request,
        _PRODUCTION_POLICY,
        platform.system().lower(),
        command_runner,
    )
