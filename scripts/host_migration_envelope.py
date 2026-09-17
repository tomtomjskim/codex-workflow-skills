#!/usr/bin/env python3
"""Build and verify one stage-scoped host-migration approval envelope."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


if __name__ == "__main__" and not sys.flags.isolated:
    print(
        json.dumps(
            {
                "status": "error",
                "issues": [
                    {
                        "code": "envelope.input_error",
                        "message": "approval envelope CLI requires isolated Python (-I)",
                    }
                ],
            },
            sort_keys=True,
        )
    )
    raise SystemExit(1)


SHA256 = re.compile(r"^[0-9a-f]{64}$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SCHEMA_VERSION = 1
VENDORED_RUNTIME_FILES = (
    "vendor/licenses/PyYAML/LICENSE",
    "vendor/licenses/tomli/LICENSE",
    "vendor/py39-manifest.json",
    "vendor/py39/tomli/__init__.py",
    "vendor/py39/tomli/_parser.py",
    "vendor/py39/tomli/_re.py",
    "vendor/py39/tomli/_types.py",
    "vendor/py39/tomli/py.typed",
    "vendor/py39/yaml/__init__.py",
    "vendor/py39/yaml/composer.py",
    "vendor/py39/yaml/constructor.py",
    "vendor/py39/yaml/cyaml.py",
    "vendor/py39/yaml/dumper.py",
    "vendor/py39/yaml/emitter.py",
    "vendor/py39/yaml/error.py",
    "vendor/py39/yaml/events.py",
    "vendor/py39/yaml/loader.py",
    "vendor/py39/yaml/nodes.py",
    "vendor/py39/yaml/parser.py",
    "vendor/py39/yaml/reader.py",
    "vendor/py39/yaml/representer.py",
    "vendor/py39/yaml/resolver.py",
    "vendor/py39/yaml/scanner.py",
    "vendor/py39/yaml/serializer.py",
    "vendor/py39/yaml/tokens.py",
)
REQUIRED_RUNTIME_FILES = (
    "scripts/__init__.py",
    "scripts/agent_contracts.py",
    "scripts/host_migration_apply.py",
    "scripts/host_migration_envelope.py",
    "scripts/host_migration_snapshot.py",
    "scripts/validate_host_policy.py",
    "policies/host-policy.json",
    "skills/adversarial-review-loop/references/reviewer-routing.json",
) + VENDORED_RUNTIME_FILES
FILE_REF_KEYS = {"path", "sha256"}
POST_APPLY_KEYS = {
    "audit_command",
    "runtime_checks",
    "rollback_command",
}


class StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("argument error: {}".format(message))


def _load_direct_sibling(module_name: str) -> ModuleType:
    path = Path(__file__).resolve(strict=True).with_name("{}.py".format(module_name))
    sealed_name = "_host_policy_{}_{}".format(
        module_name, hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    )
    existing = sys.modules.get(sealed_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(sealed_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("could not load sealed sibling {}".format(module_name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[sealed_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(sealed_name, None)
        raise
    return module


def _apply_module() -> ModuleType:
    if __package__:
        from . import host_migration_apply as module
    else:
        module = _load_direct_sibling("host_migration_apply")
    expected = Path(__file__).resolve(strict=True).with_name("host_migration_apply.py")
    if Path(module.__file__).resolve(strict=True) != expected:
        raise ImportError("host migration apply module is not the sealed sibling")
    return module


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _real_directory(path: Path, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("{} must be a real non-symlink directory".format(label))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("{} ancestry must contain only real directories".format(label))


def _private_directory(path: Path, label: str) -> None:
    _real_directory(path, label)
    metadata = path.lstat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("{} must be owned by the current user with mode 0700".format(label))


def _regular_file(path: Path, label: str) -> None:
    _real_directory(path.parent, "{} parent".format(label))
    if path.is_symlink() or not path.is_file():
        raise ValueError("{} must be a regular non-symlink file".format(label))


def _trusted_executable_path(path: Path, label: str) -> None:
    allowed_owners = {0, os.getuid()}
    current = Path(path.anchor)
    directories = [current]
    for component in path.parent.parts[1:]:
        directories.append(directories[-1] / component)
    for current in directories:
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owners
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError(
                "{} ancestry must be owner-controlled and non-writable by group/other".format(
                    label
                )
            )
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in allowed_owners
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError(
            "{} must be owner-controlled and non-writable by group/other".format(label)
        )


def read_sealed_bytes(path: Path, label: str) -> bytes:
    _regular_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        chunks = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if stable != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
            current.st_dev,
            current.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise ValueError("{} changed while it was read".format(label))
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sealed_file_sha256(path: Path, label: str, require_executable: bool = False) -> str:
    _regular_file(path, label)
    if require_executable:
        _trusted_executable_path(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        if require_executable and not before.st_mode & 0o111:
            raise ValueError("{} must be executable".format(label))
        if before.st_size > 512 * 1024 * 1024:
            raise ValueError("{} exceeds the executable size limit".format(label))
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if stable != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
            current.st_dev,
            current.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise ValueError("{} changed while it was read".format(label))
        if require_executable:
            _trusted_executable_path(path, label)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("{} path must be a string".format(label))
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("{} path must be canonical and absolute".format(label))
    return path


def _file_ref(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != FILE_REF_KEYS:
        raise ValueError("{} file reference is invalid".format(label))
    path = _absolute_path(value.get("path"), label)
    digest = value.get("sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ValueError("{} digest is invalid".format(label))
    return {"path": str(path), "sha256": digest}


def _string_command(value: Any, label: str) -> List[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item and "\x00" not in item for item in value)
    ):
        raise ValueError("{} must be a non-empty argv list".format(label))
    return list(value)


def _option_values(command: List[str], option: str) -> List[str]:
    values = []
    for index, item in enumerate(command):
        if item != option:
            continue
        if index + 1 >= len(command) or command[index + 1].startswith("--"):
            raise ValueError("audit command option {} has no value".format(option))
        values.append(command[index + 1])
    return values


def _single_option(command: List[str], option: str, expected: str) -> None:
    if _option_values(command, option) != [expected]:
        raise ValueError("audit command {} differs from the envelope".format(option))


def validate_envelope(value: Any) -> Dict[str, Any]:
    expected_keys = {
        "schema_version",
        "authorized_stage",
        "authorized_labels",
        "target_root",
        "files",
        "executor_root",
        "executors",
        "evidence",
        "post_apply",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("approval envelope has an invalid schema")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("approval envelope must use schema_version 1")
    stage = value.get("authorized_stage")
    labels = value.get("authorized_labels")
    if stage not in {"baseline", "builder", "operator"}:
        raise ValueError("approval envelope stage is invalid")
    if (
        not isinstance(labels, list)
        or not labels
        or not all(isinstance(item, str) and LABEL.fullmatch(item) for item in labels)
        or len(labels) != len(set(labels))
    ):
        raise ValueError("approval envelope labels are invalid")
    _absolute_path(value.get("target_root"), "target root")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {
        "manifest",
        "target_audit",
        "snapshot_receipt",
        "policy",
        "reviewer_routing",
        "python_executable",
        "codex_executable",
    }:
        raise ValueError("approval envelope file inventory is invalid")
    for name, reference in files.items():
        _file_ref(reference, "envelope {}".format(name))
    executor_root = _absolute_path(value.get("executor_root"), "executor root")
    executors = value.get("executors")
    if not isinstance(executors, list):
        raise ValueError("approval envelope executors must be a list")
    relative_paths = []
    for item in executors:
        if not isinstance(item, dict) or set(item) != {"relative_path", "path", "sha256"}:
            raise ValueError("approval envelope executor is invalid")
        relative = item.get("relative_path")
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            raise ValueError("approval envelope executor relative path is invalid")
        path = _absolute_path(item.get("path"), "envelope executor")
        if path != executor_root / relative:
            raise ValueError("approval envelope executor path differs from its root")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("approval envelope executor digest is invalid")
        relative_paths.append(relative)
    if sorted(relative_paths) != sorted(REQUIRED_RUNTIME_FILES):
        raise ValueError("approval envelope executor inventory is incomplete")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict) or not all(
        isinstance(label, str) and LABEL.fullmatch(label) for label in evidence
    ):
        raise ValueError("approval envelope evidence inventory is invalid")
    for label, reference in evidence.items():
        _file_ref(reference, "envelope evidence {}".format(label))
    post_apply = value.get("post_apply")
    if not isinstance(post_apply, dict) or set(post_apply) != POST_APPLY_KEYS:
        raise ValueError("approval envelope post-apply contract is invalid")
    audit_command = _string_command(post_apply.get("audit_command"), "audit command")
    rollback_command = _string_command(
        post_apply.get("rollback_command"), "rollback command"
    )
    executor_map = {item["relative_path"]: item for item in executors}
    validator_path = executor_map["scripts/validate_host_policy.py"]["path"]
    snapshot_path = executor_map["scripts/host_migration_snapshot.py"]["path"]
    if (
        len(audit_command) < 3
        or not Path(audit_command[0]).is_absolute()
        or audit_command[1:3] != ["-I", validator_path]
    ):
        raise ValueError("audit command must use the approved validator")
    _single_option(audit_command, "--audit-scope", "live")
    _single_option(audit_command, "--stage", stage)
    if audit_command.count("--json") != 1:
        raise ValueError("audit command must request JSON exactly once")
    if "--target-manifest" in audit_command or "--target-root" in audit_command:
        raise ValueError("live audit command cannot accept target-tree inputs")
    for option, expected in (
        ("--policy", files["policy"]["path"]),
        ("--reviewer-routing", files["reviewer_routing"]["path"]),
    ):
        _single_option(audit_command, option, expected)
    runtime_checks = post_apply.get("runtime_checks")
    if not isinstance(runtime_checks, list) or not runtime_checks:
        raise ValueError("runtime checks must be a non-empty list")
    runtime_names = []
    runtime_executable = None
    for check in runtime_checks:
        if not isinstance(check, dict) or set(check) != {
            "name",
            "command",
            "expectations",
        }:
            raise ValueError("runtime check schema is invalid")
        name = check.get("name")
        if not isinstance(name, str) or not LABEL.fullmatch(name):
            raise ValueError("runtime check name is invalid")
        command = _string_command(check.get("command"), "runtime check command")
        expected_command = (
            [command[0], "mcp", "list", "--json"]
            if name == "base"
            else [command[0], "--profile", name, "mcp", "list", "--json"]
        )
        if (
            command != expected_command
            or not Path(command[0]).is_absolute()
            or (runtime_executable is not None and command[0] != runtime_executable)
        ):
            raise ValueError("runtime check command is invalid")
        runtime_executable = command[0]
        expectations = check.get("expectations")
        if not isinstance(expectations, dict) or not expectations or not all(
            isinstance(server, str)
            and LABEL.fullmatch(server)
            and isinstance(enabled, bool)
            for server, enabled in expectations.items()
        ):
            raise ValueError("runtime check expectations are invalid")
        if expectations.get("db-mcp") is not False:
            raise ValueError("runtime checks must keep db-mcp disabled")
        runtime_names.append(name)
    if len(runtime_names) != len(set(runtime_names)):
        raise ValueError("runtime check names must be unique")
    python_executable = files["python_executable"]["path"]
    codex_executable = files["codex_executable"]["path"]
    if audit_command[0] != python_executable or rollback_command[0] != python_executable:
        raise ValueError("audit and rollback must use the approved Python executable")
    if runtime_executable != codex_executable:
        raise ValueError("runtime checks must use the approved Codex executable")
    if not Path(rollback_command[0]).is_absolute() or rollback_command != [
        rollback_command[0],
        "-I",
        snapshot_path,
        "rollback",
        "--journal",
        "{journal}",
        "--confirm-rollback",
    ]:
        raise ValueError("rollback command must use the approved snapshot executor")
    return value


def load_envelope_sealed(path: Path, expected_sha256: str) -> Tuple[bytes, Dict[str, Any]]:
    if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256):
        raise ValueError("expected envelope sha256 is invalid")
    data = read_sealed_bytes(path, "approval envelope")
    if sha256_bytes(data) != expected_sha256:
        raise ValueError("approval envelope differs from the approved digest")
    value = validate_envelope(json.loads(data.decode("utf-8")))
    if data != canonical_bytes(value):
        raise ValueError("approval envelope must use canonical JSON serialization")
    return data, value


def _verify_ref(reference: Mapping[str, str], label: str) -> bytes:
    path = Path(reference["path"])
    data = read_sealed_bytes(path, label)
    if sha256_bytes(data) != reference["sha256"]:
        raise ValueError("{} differs from the approval envelope".format(label))
    return data


def verify_envelope_files(envelope: Mapping[str, Any]) -> Dict[str, bytes]:
    verified: Dict[str, bytes] = {}
    for name, reference in envelope["files"].items():
        if name.endswith("_executable"):
            digest = _sealed_file_sha256(
                Path(reference["path"]),
                "envelope {}".format(name),
                require_executable=True,
            )
            if digest != reference["sha256"]:
                raise ValueError(
                    "envelope {} differs from the approval envelope".format(name)
                )
            verified[name] = b""
        else:
            verified[name] = _verify_ref(reference, "envelope {}".format(name))
    for item in envelope["executors"]:
        relative = item["relative_path"]
        verified[relative] = _verify_ref(item, "envelope executor {}".format(relative))
    for label, reference in envelope["evidence"].items():
        verified["evidence:{}".format(label)] = _verify_ref(
            reference, "envelope evidence {}".format(label)
        )
    return verified


def verify_command_executables(envelope: Mapping[str, Any]) -> None:
    for name in ("python_executable", "codex_executable"):
        reference = envelope["files"][name]
        digest = _sealed_file_sha256(
            Path(reference["path"]),
            "approved {}".format(name),
            require_executable=True,
        )
        if digest != reference["sha256"]:
            raise ValueError("approved command executable changed during verification")


def _bind_live_audit_command(
    command: List[str],
    manifest: Mapping[str, Any],
    stage: str,
    policy: Mapping[str, Any],
    versions: Mapping[str, str],
) -> None:
    records = {
        item["label"]: item
        for item in manifest.get("targets", [])
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    }
    required_profiles = {
        "codex-profile-scout",
        "codex-profile-builder",
        "codex-profile-operator",
    }
    codex_record = records.get("codex-config")
    rules_record = records.get("codex-rules")
    serena_record = records.get("serena-config")
    if (
        codex_record is None
        or rules_record is None
        or serena_record is None
        or not required_profiles.issubset(records)
    ):
        raise ValueError("manifest must include the complete core host inventory")
    home_root = Path(codex_record["path"]).parent.parent
    exact_paths = {
        "--codex-config": codex_record["path"],
        "--codex-rules": rules_record["path"],
        "--serena-config": serena_record["path"],
    }
    for option, path in exact_paths.items():
        _single_option(command, option, path)

    expected_profile_names = {
        "baseline": ["scout"],
        "builder": ["scout", "builder"],
        "operator": ["scout", "builder", "operator"],
    }[stage]
    profile_values = _option_values(command, "--codex-profile")
    expected_profile_values = [
        "{}={}".format(name, home_root / ".codex/{}.config.toml".format(name))
        for name in expected_profile_names
    ]
    if profile_values != expected_profile_values:
        raise ValueError("live audit Codex profiles differ from the approved stage")

    manifest_project_paths = {
        record["path"]
        for label, record in records.items()
        if label.startswith("serena-project-")
    }
    project_values = _option_values(command, "--serena-project")
    expected_project_names = policy.get("serena", {}).get("projects", [])
    if len(
        {label for label in records if label.startswith("serena-project-")}
    ) != len(expected_project_names):
        raise ValueError("manifest Serena project inventory differs from policy")
    if (
        not isinstance(expected_project_names, list)
        or not expected_project_names
        or len(project_values) != len(expected_project_names)
    ):
        raise ValueError("live audit Serena project inventory is incomplete")
    seen_project_paths = set()
    for value in project_values:
        name, separator, path = value.partition("=")
        candidate = Path(path)
        if (
            not separator
            or name not in expected_project_names
            or not candidate.is_absolute()
            or str(candidate) != path
            or path in seen_project_paths
        ):
            raise ValueError("live audit Serena project input is invalid")
        seen_project_paths.add(path)
    if {value.partition("=")[0] for value in project_values} != set(
        expected_project_names
    ):
        raise ValueError("live audit Serena project identifiers differ from policy")
    if seen_project_paths != manifest_project_paths:
        raise ValueError("live audit Serena project paths differ from the manifest")

    stages = manifest.get("stages")
    if not isinstance(stages, list) or [
        item.get("name") for item in stages if isinstance(item, dict)
    ] != ["baseline", "builder", "operator"]:
        raise ValueError("manifest stage inventory is invalid")
    stage_map = {item["name"]: item.get("labels") for item in stages}
    if (
        stage_map.get("builder") != ["codex-profile-builder"]
        or stage_map.get("operator") != ["codex-profile-operator"]
        or not isinstance(stage_map.get("baseline"), list)
        or set(stage_map["baseline"])
        != set(records) - {"codex-profile-builder", "codex-profile-operator"}
    ):
        raise ValueError("manifest stage partition is invalid")

    for option, expected in (
        ("--home-root", str(home_root)),
        ("--shared-agents-root", str(home_root / ".agents")),
        ("--common-agent-reference-root", str(home_root / ".agents/common-agents")),
        ("--codex-agents-root", str(home_root / ".codex/agents")),
        ("--claude-agents-root", str(home_root / ".claude/agents")),
        ("--codex-version", versions["codex_version"]),
        ("--serena-version", versions["serena_version"]),
    ):
        _single_option(command, option, expected)

    _single_option(command, "--stage", stage)
    value_options = {
        "--policy",
        "--codex-config",
        "--codex-version",
        "--codex-profile",
        "--codex-rules",
        "--serena-config",
        "--serena-project",
        "--serena-version",
        "--shared-agents-root",
        "--common-agent-reference-root",
        "--codex-agents-root",
        "--claude-agents-root",
        "--reviewer-routing",
        "--home-root",
        "--audit-scope",
        "--stage",
    }
    index = 3
    json_count = 0
    while index < len(command):
        option = command[index]
        if option == "--json":
            json_count += 1
            index += 1
            continue
        if option not in value_options or index + 1 >= len(command):
            raise ValueError("live audit command contains an unbound argument")
        value = command[index + 1]
        if not value or value.startswith("--"):
            raise ValueError("live audit command contains an invalid option value")
        index += 2
    if json_count != 1:
        raise ValueError("live audit command must request JSON exactly once")


def _target_audit_attestation(payload: Any) -> Tuple[Dict[str, Any], Dict[str, str]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "status",
        "errors",
        "warnings",
        "audit",
    }:
        raise ValueError("external target audit receipt schema is invalid")
    audit = payload.get("audit")
    expected_audit_keys = {
        "validator",
        "policy_sha256",
        "inputs_sha256",
        "codex_version",
        "serena_version",
        "stage",
        "scope",
        "target_inventory_sha256",
        "target_root",
        "inputs",
    }
    if (
        payload.get("schema_version") != 1
        or payload.get("status") != "ok"
        or payload.get("errors") != []
        or not isinstance(payload.get("warnings"), list)
        or not isinstance(audit, dict)
        or set(audit) != expected_audit_keys
        or not isinstance(audit.get("codex_version"), str)
        or not audit["codex_version"]
        or not isinstance(audit.get("serena_version"), str)
        or not audit["serena_version"]
    ):
        raise ValueError("external target audit receipt is invalid")
    attestation = {
        "status": payload["status"],
        **{
            key: audit[key]
            for key in expected_audit_keys
            if key not in {"codex_version", "serena_version"}
        },
    }
    versions = {
        "codex_version": audit["codex_version"],
        "serena_version": audit["serena_version"],
    }
    return attestation, versions


def bind_envelope(
    envelope: Mapping[str, Any],
    *,
    manifest_path: Path,
    manifest_bytes: bytes,
    manifest: Mapping[str, Any],
    receipt_path: Path,
    receipt_bytes: bytes,
    target_root: Path,
    stage: str,
    runtime_files: Mapping[str, Path],
) -> Dict[str, bytes]:
    verified = verify_envelope_files(envelope)
    if envelope["files"]["manifest"]["path"] != str(manifest_path):
        raise ValueError("approval envelope manifest path differs")
    if envelope["files"]["manifest"]["sha256"] != sha256_bytes(manifest_bytes):
        raise ValueError("manifest differs from the approval envelope")
    if envelope["files"]["snapshot_receipt"]["path"] != str(receipt_path):
        raise ValueError("approval envelope snapshot path differs")
    if envelope["files"]["snapshot_receipt"]["sha256"] != sha256_bytes(receipt_bytes):
        raise ValueError("snapshot receipt differs from the approval envelope")
    if envelope["target_root"] != str(target_root):
        raise ValueError("approval envelope target root differs")
    if envelope["authorized_stage"] != stage:
        raise ValueError("approval envelope does not authorize the requested stage")
    stages = manifest.get("stages")
    stage_labels = next(
        (item.get("labels") for item in stages if item.get("name") == stage), None
    )
    if envelope["authorized_labels"] != stage_labels:
        raise ValueError("approval envelope labels differ from the manifest stage")
    audit_receipt = json.loads(verified["target_audit"].decode("utf-8"))
    audit_attestation, versions = _target_audit_attestation(audit_receipt)
    if audit_attestation != manifest.get("audit"):
        raise ValueError("external target audit differs from the manifest attestation")
    policy = json.loads(verified["policy"].decode("utf-8"))
    _bind_live_audit_command(
        envelope["post_apply"]["audit_command"],
        manifest,
        stage,
        policy,
        versions,
    )
    expected_runtime_names = {
        "baseline": ["base", "scout"],
        "builder": ["base", "scout", "builder"],
        "operator": ["base", "scout", "builder", "operator"],
    }[stage]
    if [
        item["name"] for item in envelope["post_apply"]["runtime_checks"]
    ] != expected_runtime_names:
        raise ValueError("runtime checks differ from the installed stage profiles")
    canonical_policy = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if sha256_bytes(canonical_policy) != manifest["audit"]["policy_sha256"]:
        raise ValueError("approved policy differs from the manifest attestation")
    routing_digest = sha256_bytes(verified["reviewer_routing"])
    routing_receipts = [
        item
        for item in manifest["audit"]["inputs"]
        if item.get("label") == "reviewer routing"
    ]
    if len(routing_receipts) != 1 or routing_receipts[0].get("sha256") != routing_digest:
        raise ValueError("approved reviewer routing differs from the target audit")
    executor_map = {item["relative_path"]: item for item in envelope["executors"]}
    if set(runtime_files) != set(REQUIRED_RUNTIME_FILES):
        raise ValueError("runtime file identity inventory is incomplete")
    for relative, actual_path in runtime_files.items():
        expected_path = Path(executor_map[relative]["path"])
        if actual_path.resolve(strict=True) != expected_path.resolve(strict=True):
            raise ValueError("runtime file is not the approved executor: {}".format(relative))
    return verified


def verify_envelope_binding(envelope: Mapping[str, Any]) -> Dict[str, bytes]:
    # Standalone verification must authenticate every copied executor before
    # importing one of those executors for semantic manifest validation.
    verify_envelope_files(envelope)
    manifest_path = Path(envelope["files"]["manifest"]["path"])
    receipt_path = Path(envelope["files"]["snapshot_receipt"]["path"])
    manifest_bytes = _verify_ref(envelope["files"]["manifest"], "envelope manifest")
    receipt_bytes = _verify_ref(
        envelope["files"]["snapshot_receipt"], "envelope snapshot receipt"
    )
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    _apply_module().validate_manifest(manifest)
    runtime_files = {
        item["relative_path"]: Path(item["path"])
        for item in envelope["executors"]
    }
    return bind_envelope(
        envelope,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        receipt_path=receipt_path,
        receipt_bytes=receipt_bytes,
        target_root=Path(envelope["target_root"]),
        stage=envelope["authorized_stage"],
        runtime_files=runtime_files,
    )


def _file_reference(path: Path) -> Dict[str, str]:
    data = read_sealed_bytes(path, str(path))
    return {"path": str(path), "sha256": sha256_bytes(data)}


def _executable_reference(path: Path, label: str) -> Dict[str, str]:
    path = Path(os.path.abspath(str(path)))
    return {
        "path": str(path),
        "sha256": _sealed_file_sha256(path, label, require_executable=True),
    }


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(path), flags, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("approval envelope write made no progress")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_envelope(
    *,
    output_path: Path,
    executor_root: Path,
    source_root: Path,
    manifest_path: Path,
    target_audit_path: Path,
    receipt_path: Path,
    target_root: Path,
    stage: str,
    evidence: Mapping[str, Path],
    audit_command: Iterable[str],
    runtime_checks: Iterable[Mapping[str, Any]],
    rollback_command: Iterable[str],
) -> Dict[str, Any]:
    output_path = Path(os.path.abspath(str(output_path)))
    executor_root = Path(os.path.abspath(str(executor_root)))
    source_root = Path(os.path.abspath(str(source_root)))
    if output_path.exists() or output_path.is_symlink():
        raise ValueError("approval envelope output must not already exist")
    if executor_root.exists() or executor_root.is_symlink():
        raise ValueError("executor root must not already exist")
    _private_directory(output_path.parent, "approval envelope parent")
    if executor_root.parent != output_path.parent:
        raise ValueError("executor root must be a sibling of the approval envelope")
    manifest_path = Path(os.path.abspath(str(manifest_path)))
    target_audit_path = Path(os.path.abspath(str(target_audit_path)))
    receipt_path = Path(os.path.abspath(str(receipt_path)))
    target_root = Path(os.path.abspath(str(target_root)))
    manifest_bytes = read_sealed_bytes(manifest_path, "manifest")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("stages"), list):
        raise ValueError("manifest stage inventory is invalid")
    labels = next(
        (
            item.get("labels")
            for item in manifest["stages"]
            if isinstance(item, dict) and item.get("name") == stage
        ),
        None,
    )
    if not isinstance(labels, list) or not labels:
        raise ValueError("authorized stage is missing from the manifest")
    audit_command = list(audit_command)
    runtime_checks = list(runtime_checks)
    rollback_command = list(rollback_command)
    if not audit_command or not rollback_command or not runtime_checks:
        raise ValueError("approval envelope commands must not be empty")
    python_executable = Path(audit_command[0])
    runtime_command = runtime_checks[0].get("command")
    if not isinstance(runtime_command, list) or not runtime_command:
        raise ValueError("approval envelope runtime command is invalid")
    codex_executable = Path(runtime_command[0])
    executor_root.mkdir(mode=0o700)
    executors = []
    try:
        for relative in REQUIRED_RUNTIME_FILES:
            source = source_root / relative
            data = read_sealed_bytes(source, "runtime source {}".format(relative))
            destination = executor_root / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = 0o500 if destination.suffix == ".py" else 0o400
            _write_exclusive(destination, data, mode)
            executors.append(
                {
                    "relative_path": relative,
                    "path": str(destination),
                    "sha256": sha256_bytes(data),
                }
            )
        policy_path = executor_root / "policies/host-policy.json"
        routing_path = (
            executor_root
            / "skills/adversarial-review-loop/references/reviewer-routing.json"
        )
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "authorized_stage": stage,
            "authorized_labels": labels,
            "target_root": str(target_root),
            "files": {
                "manifest": _file_reference(manifest_path),
                "target_audit": _file_reference(target_audit_path),
                "snapshot_receipt": _file_reference(receipt_path),
                "policy": _file_reference(policy_path),
                "reviewer_routing": _file_reference(routing_path),
                "python_executable": _executable_reference(
                    python_executable, "approved Python executable"
                ),
                "codex_executable": _executable_reference(
                    codex_executable, "approved Codex executable"
                ),
            },
            "executor_root": str(executor_root),
            "executors": executors,
            "evidence": {
                label: _file_reference(Path(os.path.abspath(str(path))))
                for label, path in sorted(evidence.items())
            },
            "post_apply": {
                "audit_command": audit_command,
                "runtime_checks": [
                    {
                        "name": item["name"],
                        "command": list(item["command"]),
                        "expectations": dict(sorted(item["expectations"].items())),
                    }
                    for item in runtime_checks
                ],
                "rollback_command": rollback_command,
            },
        }
        envelope = validate_envelope(envelope)
        data = canonical_bytes(envelope)
        _write_exclusive(output_path, data, 0o600)
        verify_envelope_binding(envelope)
        for directory in sorted(
            {item for item in executor_root.rglob("*") if item.is_dir()},
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(executor_root)
        _fsync_directory(output_path.parent)
        return envelope
    except BaseException:
        if output_path.is_file() and not output_path.is_symlink():
            output_path.unlink()
        if executor_root.is_dir() and not executor_root.is_symlink():
            shutil.rmtree(str(executor_root))
        _fsync_directory(output_path.parent)
        raise


def build_from_spec(spec_path: Path, output_path: Path, executor_root: Path) -> Dict[str, Any]:
    spec = json.loads(read_sealed_bytes(spec_path, "approval envelope spec").decode("utf-8"))
    expected = {
        "source_root",
        "manifest",
        "target_audit",
        "snapshot_receipt",
        "target_root",
        "stage",
        "evidence",
        "audit_command",
        "runtime_checks",
        "rollback_command",
    }
    if not isinstance(spec, dict) or set(spec) != expected:
        raise ValueError("approval envelope build spec is invalid")
    for field in (
        "source_root",
        "manifest",
        "target_audit",
        "snapshot_receipt",
        "target_root",
    ):
        _absolute_path(spec.get(field), "approval envelope spec {}".format(field))
    if not isinstance(spec.get("stage"), str):
        raise ValueError("approval envelope build stage is invalid")
    evidence = spec.get("evidence")
    if not isinstance(evidence, dict) or not all(
        isinstance(label, str) and isinstance(path, str)
        for label, path in evidence.items()
    ):
        raise ValueError("approval envelope build evidence is invalid")
    for label, path in evidence.items():
        _absolute_path(path, "approval envelope evidence {}".format(label))
    return build_envelope(
        output_path=output_path,
        executor_root=executor_root,
        source_root=Path(spec["source_root"]),
        manifest_path=Path(spec["manifest"]),
        target_audit_path=Path(spec["target_audit"]),
        receipt_path=Path(spec["snapshot_receipt"]),
        target_root=Path(spec["target_root"]),
        stage=spec["stage"],
        evidence={label: Path(path) for label, path in evidence.items()},
        audit_command=spec["audit_command"],
        runtime_checks=spec["runtime_checks"],
        rollback_command=spec["rollback_command"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = StructuredArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=StructuredArgumentParser
    )
    build = subparsers.add_parser("build")
    build.add_argument("--spec", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--executor-root", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--envelope", type=Path, required=True)
    verify.add_argument("--expected-envelope-sha256", required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "build":
            envelope = build_from_spec(args.spec, args.output, args.executor_root)
            payload = {
                "status": "ok",
                "authorized_stage": envelope["authorized_stage"],
                "authorized_labels": envelope["authorized_labels"],
                "envelope": str(Path(os.path.abspath(str(args.output)))),
                "envelope_sha256": sha256_bytes(
                    read_sealed_bytes(args.output, "approval envelope")
                ),
            }
        else:
            _, envelope = load_envelope_sealed(
                args.envelope, args.expected_envelope_sha256
            )
            verify_envelope_binding(envelope)
            payload = {
                "status": "ok",
                "authorized_stage": envelope["authorized_stage"],
                "authorized_labels": envelope["authorized_labels"],
                "envelope_sha256": args.expected_envelope_sha256,
            }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        json.JSONDecodeError,
    ) as error:
        payload = {
            "status": "error",
            "issues": [{"code": "envelope.input_error", "message": str(error)}],
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if payload["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
