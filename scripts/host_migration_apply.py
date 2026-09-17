#!/usr/bin/env python3
"""Preflight and atomically apply checksum-bound host migration targets."""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional

if __name__ == "__main__" and not sys.flags.isolated:
    print(
        json.dumps(
            {
                "status": "error",
                "issues": [
                    {
                        "code": "migration.input_error",
                        "message": "host migration CLI requires isolated Python (-I)",
                    }
                ],
            },
            sort_keys=True,
        )
    )
    raise SystemExit(1)


def _activate_vendored_dependencies() -> None:
    root = Path(__file__).resolve(strict=True).parents[1] / "vendor" / "py39"
    manifest_path = root.parent / "py39-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != (
        "33c29f435749f05e53bf1fda8a43f038a2e96d4b0c8abea322159708e12731f8"
    ):
        raise ImportError("vendored dependency manifest differs from bootstrap")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not manifest or not all(
        isinstance(relative, str)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        for relative, digest in manifest.items()
    ):
        raise ImportError("vendored dependency manifest is invalid")
    allowed_owners = {0, os.getuid()}
    current = Path(root.anchor)
    directories = [current]
    for component in root.parts[1:]:
        directories.append(directories[-1] / component)
    for directory in directories:
        metadata = directory.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owners
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ImportError("vendored dependency path is not owner-controlled")
    manifest_metadata = manifest_path.lstat()
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_uid not in allowed_owners
        or stat.S_IMODE(manifest_metadata.st_mode) & 0o022
    ):
        raise ImportError("vendored dependency manifest is not owner-controlled")
    actual_files = set()
    for candidate in root.rglob("*"):
        metadata = candidate.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid not in allowed_owners
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise ImportError("vendored dependency directory is not owner-controlled")
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in allowed_owners
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ImportError("vendored dependency file is not owner-controlled")
        relative = str(candidate.relative_to(root))
        actual_files.add(relative)
        candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if relative not in manifest or candidate_digest != manifest[relative]:
            raise ImportError("vendored dependency differs from bootstrap manifest")
    if actual_files != set(manifest):
        raise ImportError("vendored dependency inventory differs from bootstrap manifest")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))


try:
    _activate_vendored_dependencies()
except (OSError, ValueError, ImportError, json.JSONDecodeError) as bootstrap_error:
    if __name__ == "__main__":
        print(
            json.dumps(
                {
                    "status": "error",
                    "issues": [
                        {
                            "code": "migration.input_error",
                            "message": str(bootstrap_error),
                        }
                    ],
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1)
    raise


try:
    import tomllib as _stdlib_tomllib  # type: ignore[import-not-found]
except ImportError:  # Python 3.9/3.10 repository baseline.
    _stdlib_tomllib = None


def _toml_loads(source: str) -> Dict[str, Any]:
    if _stdlib_tomllib is not None:
        return _stdlib_tomllib.loads(source)
    import tomli

    return tomli.loads(source)


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


def _require_sibling_module(module: ModuleType, filename: str) -> None:
    expected = Path(__file__).resolve(strict=True).with_name(filename)
    actual = Path(module.__file__).resolve(strict=True)
    if actual != expected:
        raise ImportError("host migration module is not the sealed sibling: {}".format(filename))


if __package__:
    from . import host_migration_envelope as _envelope_module
    from . import host_migration_snapshot as _snapshot_module
else:  # Direct execution from a copied executor.
    _envelope_module = _load_direct_sibling("host_migration_envelope")
    _snapshot_module = _load_direct_sibling("host_migration_snapshot")

_require_sibling_module(_envelope_module, "host_migration_envelope.py")
_require_sibling_module(_snapshot_module, "host_migration_snapshot.py")
_OperationLock = _snapshot_module._OperationLock
_exchange_paths = _snapshot_module._exchange_paths
_fsync_directory = _snapshot_module._fsync_directory
_host_operation_lock_path = _snapshot_module._host_operation_lock_path
_read_sealed_bytes = _snapshot_module._read_sealed_bytes
_rename_noreplace = _snapshot_module._rename_noreplace
load_receipt = _snapshot_module.load_receipt
load_receipt_sealed = _snapshot_module.load_receipt_sealed
rollback = _snapshot_module.rollback
verify = _snapshot_module.verify


LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MODE = re.compile(r"^[0-7]{4}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")
MANIFEST_KEYS = {
    "label",
    "path",
    "pre_state",
    "current_mode",
    "current_sha256",
    "target_mode",
    "target_sha256",
}
AUDIT_KEYS = {
    "status",
    "scope",
    "stage",
    "validator",
    "policy_sha256",
    "inputs_sha256",
    "target_inventory_sha256",
    "target_root",
    "inputs",
}


class PreMutationJournalError(ValueError):
    """The current invocation did not begin a target namespace mutation."""
STAGE_KEYS = {"name", "labels"}
VALIDATOR_ID = "validate_host_policy.py@3"
MANIFEST_SCHEMA_VERSION = 3
JOURNAL_SCHEMA_VERSION = 3
AUTHORITATIVE_POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "host-policy.json"
REVIEWER_ROUTING_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills/adversarial-review-loop/references/reviewer-routing.json"
)
CANONICAL_AGENT_NAMES = {
    "accessibility-reviewer",
    "api-reviewer",
    "architect",
    "code-reviewer",
    "dba",
    "designer",
    "developer",
    "documenter",
    "explorer",
    "performance-reviewer",
    "pm",
    "publisher",
    "qa-engineer",
    "security-reviewer",
    "test-coverage-reviewer",
    "ux-reviewer",
}


class StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


@dataclass(frozen=True)
class PlannedTarget:
    record: Dict[str, Any]
    artifact: Path
    data: bytes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _canonical_policy_sha256(data: bytes) -> str:
    policy = json.loads(data.decode("utf-8"))
    canonical = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _mode(path: Path) -> str:
    return "{:04o}".format(path.stat().st_mode & 0o7777)


def _require_real_directory(path: Path, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("{} must be a real non-symlink directory".format(label))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("{} ancestry must contain only real directories".format(label))


def _require_regular_file(path: Path, label: str) -> None:
    _require_real_directory(path.parent, "{} parent".format(label))
    if path.is_symlink() or not path.is_file():
        raise ValueError("{} must be a regular non-symlink file".format(label))


def _canonical_absolute_path(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("{} must be a canonical absolute path".format(label))
    _require_real_directory(path.parent, "{} parent".format(label))
    if path.parent.resolve(strict=True) != path.parent:
        raise ValueError("{} parent must resolve without aliases".format(label))
    return path


def _load_json_file(path: Path, label: str) -> Dict[str, Any]:
    _require_regular_file(path, label)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("{} root must be a mapping".format(label))
    return value


def _target_inventory_sha256(manifest: Dict[str, Any]) -> str:
    inventory = {
        "stages": manifest.get("stages"),
        "targets": [
            {
                "label": record.get("label"),
                "path": record.get("path"),
                "target_mode": record.get("target_mode"),
                "target_sha256": record.get("target_sha256"),
            }
            for record in manifest.get("targets", [])
            if isinstance(record, dict)
        ],
    }
    canonical = json.dumps(
        inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_target_audit_binding(
    audit: Dict[str, Any], targets: List[Dict[str, Any]]
) -> None:
    target_root_value = audit.get("target_root")
    target_root = Path(target_root_value) if isinstance(target_root_value, str) else Path()
    if (
        not isinstance(target_root_value, str)
        or not target_root.is_absolute()
        or str(target_root) != target_root_value
        or ".." in target_root.parts
    ):
        raise ValueError("manifest audit target_root must be a canonical absolute path")
    inputs = audit.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("manifest audit inputs must be a non-empty receipt list")
    file_receipts: Dict[str, Dict[str, Any]] = {}
    for item in inputs:
        if not isinstance(item, dict):
            raise ValueError("manifest audit input receipt must be a mapping")
        if set(item) == {"label", "path", "mode", "sha256"}:
            path = item.get("path")
            mode = item.get("mode")
            digest = item.get("sha256")
            if (
                not isinstance(item.get("label"), str)
                or not isinstance(path, str)
                or not Path(path).is_absolute()
                or path in file_receipts
                or not isinstance(mode, str)
                or not MODE.fullmatch(mode)
                or not isinstance(digest, str)
                or not SHA256.fullmatch(digest)
            ):
                raise ValueError("manifest audit file receipt is invalid")
            file_receipts[path] = item
        elif set(item) == {"label", "path", "link_target", "target_sha256"}:
            if not all(isinstance(item.get(key), str) for key in item):
                raise ValueError("manifest audit link receipt is invalid")
        else:
            raise ValueError("manifest audit input receipt has an invalid schema")
    canonical_inputs = json.dumps(
        inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(canonical_inputs).hexdigest() != audit["inputs_sha256"]:
        raise ValueError("manifest audit input digest is invalid")
    for record in targets:
        source = Path(record["path"])
        artifact = target_root / source.relative_to(source.anchor)
        receipt = file_receipts.get(str(artifact))
        if receipt is None:
            raise ValueError(
                "manifest target is missing from audited inputs: {}".format(
                    record["label"]
                )
            )
        if (
            receipt["mode"] != record["target_mode"]
            or receipt["sha256"] != record["target_sha256"]
        ):
            raise ValueError(
                "manifest target differs from audited input: {}".format(
                    record["label"]
                )
            )
        expected_label = _expected_target_receipt_label(record["label"])
        if expected_label.endswith("*"):
            if not receipt["label"].startswith(expected_label[:-1]):
                raise ValueError("manifest target audit label is invalid")
        elif receipt["label"] != expected_label:
            raise ValueError("manifest target audit label is invalid")


def _expected_target_receipt_label(label: str) -> str:
    exact = {
        "codex-config": "Codex config",
        "codex-rules": "Codex rules",
        "codex-profile-scout": "Codex profile scout",
        "codex-profile-builder": "Codex profile builder",
        "codex-profile-operator": "Codex profile operator",
        "serena-config": "Serena config",
    }
    if label in exact:
        return exact[label]
    if label.startswith("serena-project-"):
        return "Serena project *"
    for prefix, receipt_prefix in (
        ("common-", "shared common "),
        ("codex-adapter-", "codex adapter "),
        ("claude-adapter-", "claude adapter "),
    ):
        if label.startswith(prefix):
            return receipt_prefix + label[len(prefix) :]
    raise ValueError("manifest target has no policy audit label: {}".format(label))


def target_home_root(targets: List[Dict[str, Any]]) -> Path:
    records = {record["label"]: Path(record["path"]) for record in targets}
    home_candidates = set()
    for label, path in records.items():
        if label in {
            "codex-config",
            "codex-profile-scout",
            "codex-profile-builder",
            "codex-profile-operator",
        } and path.parent.name == ".codex":
            home_candidates.add(path.parent.parent)
        elif label == "codex-rules" and path.parent.name == "rules" and path.parent.parent.name == ".codex":
            home_candidates.add(path.parent.parent.parent)
        elif label == "serena-config" and path.parent.name == ".serena":
            home_candidates.add(path.parent.parent)
        elif label.startswith(("common-", "codex-adapter-", "claude-adapter-")):
            current = path
            while current != current.parent and current.name != ".agents":
                current = current.parent
            if current.name == ".agents":
                home_candidates.add(current.parent)
    if len(home_candidates) != 1:
        raise ValueError("manifest targets must share one canonical home root")
    return next(iter(home_candidates))


def _validate_target_allowlist(targets: List[Dict[str, Any]]) -> None:
    records = {record["label"]: Path(record["path"]) for record in targets}
    home_root = target_home_root(targets)
    exact = {
        "codex-config": home_root / ".codex" / "config.toml",
        "codex-rules": home_root / ".codex" / "rules" / "default.rules",
        "codex-profile-scout": home_root / ".codex" / "scout.config.toml",
        "codex-profile-builder": home_root / ".codex" / "builder.config.toml",
        "codex-profile-operator": home_root / ".codex" / "operator.config.toml",
        "serena-config": home_root / ".serena" / "serena_config.yml",
    }
    for label, path in records.items():
        if label in exact and path == exact[label]:
            continue
        if label.startswith("serena-project-"):
            try:
                path.relative_to(home_root)
            except ValueError as error:
                raise ValueError("Serena migration target must stay under home root") from error
            if path.name == "project.yml" and path.parent.name == ".serena":
                continue
        for prefix, platform, suffix in (
            ("common-", "common-agents", ".md"),
            ("codex-adapter-", "adapters/codex", ".toml"),
            ("claude-adapter-", "adapters/claude", ".md"),
        ):
            if label.startswith(prefix):
                role = label[len(prefix) :]
                expected = home_root / ".agents" / Path(platform) / "{}{}".format(role, suffix)
                if role in CANONICAL_AGENT_NAMES and path == expected:
                    break
        else:
            raise ValueError("manifest target is outside the host-policy allowlist: {}".format(label))


def _validate_required_manifest_layout(
    audit: Dict[str, Any], labels: set[str], stages: List[Dict[str, Any]]
) -> None:
    policy_bytes = _read_sealed_bytes(
        AUTHORITATIVE_POLICY_PATH, "authoritative host policy"
    )
    if audit["policy_sha256"] != _canonical_policy_sha256(policy_bytes):
        raise ValueError("manifest audit is not bound to the authoritative host policy")
    policy = json.loads(policy_bytes.decode("utf-8"))
    project_names = policy.get("serena", {}).get("projects")
    if not isinstance(project_names, list) or not project_names or not all(
        isinstance(name, str) and LABEL.fullmatch(name) for name in project_names
    ):
        raise ValueError("authoritative Serena project inventory is invalid")
    required_core = {
        "codex-config",
        "codex-rules",
        "codex-profile-scout",
        "codex-profile-builder",
        "codex-profile-operator",
        "serena-config",
    }
    missing_core = required_core - labels
    if missing_core:
        raise ValueError(
            "manifest is missing required host-policy targets: {}".format(
                ", ".join(sorted(missing_core))
            )
        )
    manifest_projects = {
        label for label in labels if label.startswith("serena-project-")
    }
    if len(manifest_projects) != len(project_names):
        raise ValueError("manifest Serena project targets differ from policy")
    if [stage["name"] for stage in stages] != ["baseline", "builder", "operator"]:
        raise ValueError("manifest stages must be baseline, builder, operator")
    stage_map = {stage["name"]: stage["labels"] for stage in stages}
    if stage_map["builder"] != ["codex-profile-builder"] or stage_map[
        "operator"
    ] != ["codex-profile-operator"]:
        raise ValueError("Builder and Operator stages must contain only their profile")
    expected_baseline = labels - {
        "codex-profile-builder",
        "codex-profile-operator",
    }
    if set(stage_map["baseline"]) != expected_baseline:
        raise ValueError("baseline stage must contain every non-elevated target")


def validate_manifest(
    manifest: Any, require_complete_layout: bool = True
) -> Dict[str, Any]:
    if set(manifest) != {"schema_version", "audit", "stages", "targets"} or manifest.get(
        "schema_version"
    ) != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest must use the exact schema_version 3 contract")
    audit = manifest.get("audit")
    if not isinstance(audit, dict) or set(audit) != AUDIT_KEYS:
        raise ValueError("manifest audit attestation has an invalid schema")
    if audit.get("status") != "ok":
        raise ValueError("manifest audit attestation must have status ok")
    if audit.get("scope") != "target" or audit.get("stage") != "full":
        raise ValueError("migration manifest requires a full target audit attestation")
    if audit.get("validator") != VALIDATOR_ID:
        raise ValueError("manifest audit validator identity is invalid")
    for key in ("policy_sha256", "inputs_sha256", "target_inventory_sha256"):
        value = audit.get(key)
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise ValueError("manifest audit {} must be lowercase hex".format(key))
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("manifest targets must be a non-empty list")
    labels = set()
    paths = set()
    for record in targets:
        if not isinstance(record, dict) or set(record) != MANIFEST_KEYS:
            raise ValueError("manifest target has an invalid schema")
        label = record.get("label")
        if not isinstance(label, str) or not LABEL.fullmatch(label) or label in labels:
            raise ValueError("manifest labels must be valid and unique")
        labels.add(label)
        source = record.get("path")
        if not isinstance(source, str) or source in paths:
            raise ValueError("manifest target paths must be strings and unique")
        _canonical_absolute_path(source, "manifest target")
        paths.add(source)
        state = record.get("pre_state")
        if state not in ("present", "absent"):
            raise ValueError("manifest pre_state must be present or absent")
        target_mode = record.get("target_mode")
        target_sha = record.get("target_sha256")
        if not isinstance(target_mode, str) or not MODE.fullmatch(target_mode):
            raise ValueError("manifest target_mode must be four octal digits")
        if not isinstance(target_sha, str) or not SHA256.fullmatch(target_sha):
            raise ValueError("manifest target_sha256 must be lowercase hex")
        if state == "present":
            current_mode = record.get("current_mode")
            current_sha = record.get("current_sha256")
            if not isinstance(current_mode, str) or not MODE.fullmatch(current_mode):
                raise ValueError("present target current_mode must be four octal digits")
            if not isinstance(current_sha, str) or not SHA256.fullmatch(current_sha):
                raise ValueError("present target current_sha256 must be lowercase hex")
        elif record.get("current_mode") is not None or record.get(
            "current_sha256"
        ) is not None:
            raise ValueError("absent targets must have null current mode and checksum")
    stages = manifest.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("manifest stages must be a non-empty ordered list")
    stage_names = set()
    assigned_labels = []
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != STAGE_KEYS:
            raise ValueError("manifest stage has an invalid schema")
        name = stage.get("name")
        stage_labels = stage.get("labels")
        if (
            not isinstance(name, str)
            or not LABEL.fullmatch(name)
            or name in stage_names
        ):
            raise ValueError("manifest stage names must be valid and unique")
        if (
            not isinstance(stage_labels, list)
            or not stage_labels
            or not all(isinstance(value, str) for value in stage_labels)
            or len(stage_labels) != len(set(stage_labels))
        ):
            raise ValueError("manifest stage labels must be a non-empty unique string list")
        stage_names.add(name)
        assigned_labels.extend(stage_labels)
    if len(assigned_labels) != len(set(assigned_labels)) or set(assigned_labels) != labels:
        raise ValueError("manifest stages must assign every target exactly once")
    if require_complete_layout:
        _validate_required_manifest_layout(audit, labels, stages)
    _validate_target_allowlist(targets)
    _validate_target_audit_binding(audit, targets)
    if audit["target_inventory_sha256"] != _target_inventory_sha256(manifest):
        raise ValueError("manifest target inventory differs from its audit attestation")
    return manifest


def load_manifest_sealed(
    path: Path,
    expected_sha256: Optional[str] = None,
    require_complete_layout: bool = True,
) -> tuple[bytes, Dict[str, Any]]:
    _require_regular_file(path, "manifest")
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
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
            current.st_dev,
            current.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise ValueError("manifest changed while it was read")
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not SHA256.fullmatch(
            expected_sha256
        ):
            raise ValueError("expected manifest sha256 is invalid")
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("manifest differs from the approved digest")
    return data, validate_manifest(
        json.loads(data.decode("utf-8")),
        require_complete_layout=require_complete_layout,
    )


def load_manifest(path: Path) -> Dict[str, Any]:
    return load_manifest_sealed(path)[1]


def _runtime_file_inventory() -> Dict[str, Path]:
    snapshot_module = sys.modules[_OperationLock.__module__]
    snapshot_path = Path(snapshot_module.__file__).resolve(strict=True)
    runtime_root = Path(__file__).resolve(strict=True).parents[1]
    inventory = {
        "scripts/__init__.py": Path(__file__).resolve().parent / "__init__.py",
        "scripts/agent_contracts.py": Path(__file__).resolve().parent
        / "agent_contracts.py",
        "scripts/host_migration_apply.py": Path(__file__).resolve(strict=True),
        "scripts/host_migration_envelope.py": Path(
            _envelope_module.__file__
        ).resolve(strict=True),
        "scripts/host_migration_snapshot.py": snapshot_path,
        "scripts/validate_host_policy.py": Path(__file__).resolve().parent
        / "validate_host_policy.py",
        "policies/host-policy.json": AUTHORITATIVE_POLICY_PATH,
        "skills/adversarial-review-loop/references/reviewer-routing.json": REVIEWER_ROUTING_PATH,
    }
    for relative in _envelope_module.VENDORED_RUNTIME_FILES:
        inventory[relative] = runtime_root / relative
    return inventory


def _record_map(records: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {record["label"]: record for record in records}


def bind_snapshot(
    manifest: Dict[str, Any],
    receipt_path: Path,
    receipt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    receipt = load_receipt(receipt_path) if receipt is None else receipt
    manifest_records = _record_map(manifest["targets"])
    receipt_records = _record_map(receipt["files"])
    if set(manifest_records) != set(receipt_records):
        raise ValueError("snapshot labels must exactly match the approved manifest")
    for label, target in manifest_records.items():
        snapshot_record = receipt_records[label]
        if (
            snapshot_record["source"] != target["path"]
            or snapshot_record["state"] != target["pre_state"]
        ):
            raise ValueError("snapshot target identity differs from manifest: {}".format(label))
        if target["pre_state"] == "present" and (
            snapshot_record["mode"] != target["current_mode"]
            or snapshot_record["sha256"] != target["current_sha256"]
        ):
            raise ValueError("snapshot source state differs from manifest: {}".format(label))
    backup_issues = verify(receipt_path, "backup", receipt=receipt)
    if backup_issues:
        raise ValueError("snapshot backup verification failed")
    return receipt


def _verify_live(record: Dict[str, Any]) -> None:
    source = Path(record["path"])
    _require_real_directory(source.parent, "{} parent".format(record["label"]))
    if record["pre_state"] == "absent":
        if source.exists() or source.is_symlink():
            raise ValueError("expected target to remain absent: {}".format(record["label"]))
        return
    _require_regular_file(source, record["label"])
    if _mode(source) != record["current_mode"] or _sha256(source) != record[
        "current_sha256"
    ]:
        raise ValueError("current source differs from manifest: {}".format(record["label"]))


def _artifact_path(target_root: Path, record: Dict[str, Any]) -> Path:
    source = Path(record["path"])
    artifact = target_root / source.relative_to(source.anchor)
    try:
        artifact.relative_to(target_root)
    except ValueError:
        raise ValueError("target artifact escapes target root")
    return artifact


def _validate_codex_target(
    record: Dict[str, Any], data: bytes, policy: Dict[str, Any]
) -> None:
    label = record["label"]
    if label in {
        "codex-config",
        "codex-profile-scout",
        "codex-profile-builder",
        "codex-profile-operator",
    }:
        config = _toml_loads(data.decode("utf-8"))
        db_state = config.get("mcp_servers", {}).get("db-mcp", {}).get("enabled")
        if db_state is not False:
            raise ValueError("migration target must keep db-mcp explicitly disabled")
        return
    if not label.startswith("codex-adapter-"):
        return
    role = label[len("codex-adapter-") :]
    protected = set(policy.get("agents", {}).get("protected_reviewer_roles", []))
    if role not in protected:
        return
    adapter = _toml_loads(data.decode("utf-8"))
    db_state = adapter.get("mcp_servers", {}).get("db-mcp", {}).get("enabled")
    if db_state is not False:
        raise ValueError(
            "protected reviewer target must keep db-mcp explicitly disabled"
        )


def _verify_artifact(
    target_root: Path, record: Dict[str, Any], policy: Dict[str, Any]
) -> tuple[Path, bytes]:
    artifact = _artifact_path(target_root, record)
    _require_regular_file(artifact, "{} artifact".format(record["label"]))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(artifact), flags)
    try:
        before = os.fstat(descriptor)
        chunks = []
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
        current = artifact.lstat()
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
        current.st_dev,
        current.st_ino,
    ) != (after.st_dev, after.st_ino):
        raise ValueError("target artifact changed while it was read")
    data = b"".join(chunks)
    if "{:04o}".format(after.st_mode & 0o7777) != record["target_mode"] or digest.hexdigest() != record["target_sha256"]:
        raise ValueError("target artifact differs from manifest: {}".format(record["label"]))
    _validate_codex_target(record, data, policy)
    return artifact, data


def _stage(manifest: Dict[str, Any], stage_name: str) -> Dict[str, Any]:
    for stage in manifest["stages"]:
        if stage["name"] == stage_name:
            return stage
    raise ValueError("stage is not present in the manifest: {}".format(stage_name))


def validate_journal_progress(
    payload: Dict[str, Any], manifest: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate journal state and derive the exact uncertain rollback scope."""
    stage_names = [item["name"] for item in manifest["stages"]]
    stages = {item["name"]: item["labels"] for item in manifest["stages"]}
    if (
        payload.get("schema_version") != JOURNAL_SCHEMA_VERSION
        or payload.get("stages") != stage_names
    ):
        raise ValueError("journal stage inventory is invalid")
    stage_envelopes = payload.get("stage_envelopes")
    stage_envelope_paths = payload.get("stage_envelope_paths")
    stage_audits = payload.get("stage_audits")
    if (
        not isinstance(stage_envelopes, dict)
        or not isinstance(stage_envelope_paths, dict)
        or not isinstance(stage_audits, dict)
    ):
        raise ValueError("journal stage approval inventory is invalid")
    if not all(
        name in stage_names
        and isinstance(digest, str)
        and SHA256.fullmatch(digest)
        for name, digest in stage_envelopes.items()
    ) or not all(
        name in stage_names
        and isinstance(path, str)
        and Path(path).is_absolute()
        and str(Path(path)) == path
        and ".." not in Path(path).parts
        for name, path in stage_envelope_paths.items()
    ) or set(stage_envelope_paths) != set(stage_envelopes) or not all(
        name in stage_names
        and isinstance(receipt, dict)
        and set(receipt)
        == {
            "audit_file",
            "audit_sha256",
            "runtime_file",
            "runtime_sha256",
            "live_state_sha256",
        }
        and receipt.get("audit_file") == "{}-live-audit.json".format(name)
        and receipt.get("runtime_file") == "{}-runtime-mcp.json".format(name)
        and isinstance(receipt.get("audit_sha256"), str)
        and SHA256.fullmatch(receipt["audit_sha256"])
        and isinstance(receipt.get("runtime_sha256"), str)
        and SHA256.fullmatch(receipt["runtime_sha256"])
        and isinstance(receipt.get("live_state_sha256"), str)
        and SHA256.fullmatch(receipt["live_state_sha256"])
        for name, receipt in stage_audits.items()
    ):
        raise ValueError("journal stage approval inventory is invalid")
    if not set(stage_audits).issubset(stage_envelopes):
        raise ValueError("journal audit receipts lack approval envelopes")
    retained_quarantines = payload.get("retained_quarantines", {})
    target_records = _record_map(manifest["targets"])
    if not isinstance(retained_quarantines, dict):
        raise ValueError("journal retained quarantines are invalid")
    for key, value in retained_quarantines.items():
        if not isinstance(key, str) or ":" not in key or not isinstance(value, str):
            raise ValueError("journal retained quarantines are invalid")
        operation, label = key.split(":", 1)
        if operation not in {"apply", "rollback"} or label not in target_records:
            raise ValueError("journal retained quarantine label is invalid")
        quarantine = Path(value)
        expected_parent = Path(target_records[label]["path"]).parent
        expected_prefix = ".host-policy-{}.".format(operation)
        if (
            not quarantine.is_absolute()
            or quarantine.parent != expected_parent
            or not quarantine.name.startswith(expected_prefix)
        ):
            raise ValueError("journal retained quarantine path is invalid")
    status = payload.get("status")
    if status not in {
        "ready",
        "awaiting_stage_audit",
        "applying",
        "partial",
        "complete",
        "rolling_back",
        "rolled_back",
    }:
        raise ValueError("journal status is invalid")
    rollback_required = payload.get("rollback_required")
    if not isinstance(rollback_required, bool):
        raise ValueError("journal rollback_required must be a boolean")
    if status == "rolling_back":
        source_status = payload.get("rollback_source_status")
        source_rollback_required = payload.get(
            "rollback_source_rollback_required"
        )
        if source_status not in {
            "ready",
            "awaiting_stage_audit",
            "applying",
            "partial",
            "complete",
        }:
            raise ValueError("rollback source status is invalid")
        if not isinstance(source_rollback_required, bool):
            raise ValueError("rollback source state is invalid")
        source_payload = dict(payload)
        source_payload["status"] = source_status
        source_payload["rollback_required"] = source_rollback_required
        source_progress = validate_journal_progress(source_payload, manifest)
        rollback_labels = payload.get("rollback_labels")
        rolled_back_labels = payload.get("rolled_back_labels")
        rollback_active_label = payload.get("rollback_active_label")
        rollback_quarantine = payload.get("rollback_quarantine")
        if rollback_labels != source_progress["rollback_labels"]:
            raise ValueError("journal rollback labels are invalid")
        if (
            not isinstance(rolled_back_labels, list)
            or rolled_back_labels
            != rollback_labels[: len(rolled_back_labels)]
        ):
            raise ValueError("journal rolled-back labels are invalid")
        next_index = len(rolled_back_labels)
        if rollback_active_label is not None and (
            next_index >= len(rollback_labels)
            or rollback_active_label != rollback_labels[next_index]
        ):
            raise ValueError("journal rollback active label is invalid")
        if rollback_quarantine is not None and (
            rollback_active_label is None
            or not isinstance(rollback_quarantine, str)
            or not Path(rollback_quarantine).is_absolute()
        ):
            raise ValueError("journal rollback quarantine is invalid")
        if rollback_required is not True:
            raise ValueError("rolling-back journal must require rollback")
        return {
            "status": status,
            "completed_stages": source_progress["completed_stages"],
            "completed": source_progress["completed"],
            "rollback_labels": rollback_labels,
            "rolled_back_labels": rolled_back_labels,
            "rollback_active_label": rollback_active_label,
            "rollback_quarantine": rollback_quarantine,
        }
    completed_stages = payload.get("completed_stages")
    if (
        not isinstance(completed_stages, list)
        or completed_stages != stage_names[: len(completed_stages)]
    ):
        raise ValueError("journal completed stages are invalid")
    completed = payload.get("completed")
    if (
        not isinstance(completed, list)
        or not all(isinstance(label, str) for label in completed)
        or len(completed) != len(set(completed))
    ):
        raise ValueError("journal completed labels are invalid")
    expected_completed = [
        label for name in completed_stages for label in stages[name]
    ]
    active_stage = payload.get("active_stage")
    active_label = payload.get("active_label")
    active_attempt_id = payload.get("active_attempt_id")
    pending_audit_stage = payload.get("pending_audit_stage")
    pending_audit_envelope_sha256 = payload.get(
        "pending_audit_envelope_sha256"
    )
    if active_stage is not None and not isinstance(active_stage, str):
        raise ValueError("journal active stage is invalid")
    if active_label is not None and not isinstance(active_label, str):
        raise ValueError("journal active label is invalid")
    if active_attempt_id is not None and (
        not isinstance(active_attempt_id, str)
        or not ATTEMPT_ID.fullmatch(active_attempt_id)
    ):
        raise ValueError("journal active attempt id is invalid")
    if pending_audit_stage is not None and pending_audit_stage not in stage_names:
        raise ValueError("journal pending audit stage is invalid")
    if pending_audit_envelope_sha256 is not None and (
        not isinstance(pending_audit_envelope_sha256, str)
        or not SHA256.fullmatch(pending_audit_envelope_sha256)
    ):
        raise ValueError("journal pending audit envelope is invalid")

    if status in {"applying", "partial"}:
        if active_attempt_id is None:
            raise ValueError("active journal must identify its stage attempt")
        if len(completed_stages) >= len(stage_names):
            raise ValueError("journal active stage exceeds the manifest")
        expected_active_stage = stage_names[len(completed_stages)]
        if active_stage != expected_active_stage:
            raise ValueError("journal active stage is invalid")
        stage_labels = stages[expected_active_stage]
        partial_labels = completed[len(expected_completed) :]
        if (
            completed[: len(expected_completed)] != expected_completed
            or partial_labels != stage_labels[: len(partial_labels)]
        ):
            raise ValueError("journal completed labels do not match stage progress")
        next_index = len(partial_labels)
        if active_label is not None and (
            next_index >= len(stage_labels) or active_label != stage_labels[next_index]
        ):
            raise ValueError("journal active label is invalid")
        if status == "partial" and rollback_required is not True:
            raise ValueError("partial journal must require rollback")
        if set(stage_envelopes) != set(completed_stages + [expected_active_stage]):
            raise ValueError("active journal approval envelopes are invalid")
        if set(stage_audits) != set(completed_stages):
            raise ValueError("active journal audit receipts are invalid")
    elif status == "awaiting_stage_audit":
        if active_attempt_id is None:
            raise ValueError("awaiting-audit journal must identify its stage attempt")
        if active_stage is not None or active_label is not None:
            raise ValueError("awaiting-audit journal must not have an active target")
        if completed != expected_completed or not completed_stages:
            raise ValueError("awaiting-audit journal progress is invalid")
        if pending_audit_stage != completed_stages[-1]:
            raise ValueError("awaiting-audit journal stage is invalid")
        if pending_audit_envelope_sha256 != stage_envelopes.get(
            pending_audit_stage
        ):
            raise ValueError("awaiting-audit journal envelope is invalid")
        if pending_audit_stage in stage_audits:
            raise ValueError("pending stage cannot already have an audit receipt")
        if rollback_required is not True:
            raise ValueError("awaiting-audit journal must require rollback")
        if set(stage_envelopes) != set(completed_stages):
            raise ValueError("awaiting-audit approval envelopes are invalid")
        if set(stage_audits) != set(completed_stages[:-1]):
            raise ValueError("awaiting-audit receipts are invalid")
    else:
        if active_stage is not None or active_label is not None:
            raise ValueError("inactive journal must not have an active target")
        if active_attempt_id is not None:
            raise ValueError("inactive journal must not have an active attempt")
        if pending_audit_stage is not None or pending_audit_envelope_sha256 is not None:
            raise ValueError("inactive journal must not have a pending audit")
        if completed != expected_completed:
            raise ValueError("journal completed labels do not match completed stages")
        if status == "complete" and completed_stages != stage_names:
            raise ValueError("complete journal must include every stage")
        if status == "ready" and completed_stages == stage_names:
            raise ValueError("ready journal cannot include every stage")
        if status == "rolled_back" and (completed_stages or completed):
            raise ValueError("rolled-back journal must have empty active progress")
        if rollback_required:
            raise ValueError("inactive journal must not require rollback")
        if status in {"ready", "complete"} and set(stage_audits) != set(
            completed_stages
        ):
            raise ValueError("completed stages must have bound audit receipts")
        if status in {"ready", "complete"} and set(stage_envelopes) != set(
            completed_stages
        ):
            raise ValueError("completed stages must have approval envelopes")

    rollback_labels = list(completed)
    if active_label is not None:
        rollback_labels.append(active_label)
    return {
        "status": status,
        "completed_stages": completed_stages,
        "completed": completed,
        "rollback_labels": rollback_labels,
    }


def preflight(
    manifest_path: Path,
    target_root: Path,
    receipt_path: Path,
    stage_name: str,
    manifest: Optional[Dict[str, Any]] = None,
    receipt: Optional[Dict[str, Any]] = None,
    completed_labels: Optional[Iterable[str]] = None,
) -> List[PlannedTarget]:
    manifest = load_manifest(manifest_path) if manifest is None else manifest
    policy_bytes = _read_sealed_bytes(
        AUTHORITATIVE_POLICY_PATH, "authoritative host policy"
    )
    policy = json.loads(policy_bytes.decode("utf-8"))
    authoritative_policy_digest = _canonical_policy_sha256(policy_bytes)
    if manifest["audit"]["policy_sha256"] != authoritative_policy_digest:
        raise ValueError("manifest audit is not bound to the authoritative host policy")
    bind_snapshot(manifest, receipt_path, receipt)
    _require_real_directory(target_root, "target root")
    if target_root.resolve(strict=True) != target_root:
        raise ValueError("target root must resolve without aliases")
    if str(target_root) != manifest["audit"]["target_root"]:
        raise ValueError("target root differs from the audited target tree")
    stage = _stage(manifest, stage_name)
    labels = stage["labels"]
    targets = _record_map(manifest["targets"])
    completed = set(completed_labels or [])
    if not completed.issubset(targets):
        raise ValueError("completed target inventory is invalid")
    for record in manifest["targets"]:
        if record["label"] in completed:
            _verify_installed_target(record)
        else:
            _verify_live(record)
    planned = []
    for label in labels:
        record = targets[label]
        if label in completed:
            raise ValueError("selected stage target is already completed")
        artifact, data = _verify_artifact(target_root, record, policy)
        planned.append(PlannedTarget(record=record, artifact=artifact, data=data))
    return planned


def _mcp_states(config: Dict[str, Any], label: str) -> Dict[str, bool]:
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError("{} must define the MCP inventory".format(label))
    states = {}
    for name, server in servers.items():
        if not isinstance(name, str) or not isinstance(server, dict):
            raise ValueError("{} MCP inventory is invalid".format(label))
        enabled = server.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("{} MCP enabled state is invalid".format(label))
        states[name] = enabled
    return states


def _expected_runtime_checks(
    target_root: Path, manifest: Dict[str, Any], stage_name: str
) -> Dict[str, Dict[str, bool]]:
    records = _record_map(manifest["targets"])
    record = records.get("codex-config")
    if record is None:
        raise ValueError("manifest is missing the base Codex config")
    artifact = _artifact_path(target_root, record)
    config = _toml_loads(
        _read_sealed_bytes(artifact, "target Codex config").decode("utf-8")
    )
    base_states = _mcp_states(config, "target Codex config")
    if base_states.get("db-mcp") is not False:
        raise ValueError("target base runtime must keep db-mcp disabled")
    required_profiles = {
        "baseline": ("scout",),
        "builder": ("scout", "builder"),
        "operator": ("scout", "builder", "operator"),
    }.get(stage_name)
    if required_profiles is None:
        raise ValueError("runtime stage is invalid")
    checks = {"base": base_states}
    for name in required_profiles:
        label = "codex-profile-{}".format(name)
        profile_record = records.get(label)
        if profile_record is None:
            raise ValueError("installed runtime profile is missing from the manifest")
        profile = _toml_loads(
            _read_sealed_bytes(
                _artifact_path(target_root, profile_record),
                "target Codex profile {}".format(name),
            ).decode("utf-8")
        )
        effective = dict(base_states)
        for server_name, enabled in _mcp_states(
            profile, "target Codex profile {}".format(name)
        ).items():
            effective[server_name] = enabled
        if effective.get("db-mcp") is not False:
            raise ValueError("target profile runtime must keep db-mcp disabled")
        checks[name] = effective
    return checks


def _verify_stage_live_state(
    manifest: Dict[str, Any], completed_labels: Iterable[str]
) -> str:
    completed = set(completed_labels)
    state = []
    for record in manifest["targets"]:
        if record["label"] in completed:
            _verify_installed_target(record)
            state.append(
                {
                    "label": record["label"],
                    "state": "target",
                    "mode": record["target_mode"],
                    "sha256": record["target_sha256"],
                }
            )
        else:
            _verify_live(record)
            state.append(
                {
                    "label": record["label"],
                    "state": record["pre_state"],
                    "mode": record["current_mode"],
                    "sha256": record["current_sha256"],
                }
            )
    canonical = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as error:
        raise ValueError("verification process could not be reaped") from error


def _run_json_command(
    argv: List[str],
    label: str,
    environment_overrides: Optional[Dict[str, str]] = None,
) -> tuple[bytes, Any]:
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if environment_overrides:
        if not set(environment_overrides).issubset(
            {"HOME", "CODEX_HOME", "TMPDIR"}
        ):
            raise ValueError("verification environment override is not allowed")
        environment.update(environment_overrides)
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        raise
    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        raise ValueError("{} output pipes are unavailable".format(label))
    output = bytearray()
    error_output = bytearray()
    stream_limits = {
        process.stdout.fileno(): (output, 8 * 1024 * 1024, "output"),
        process.stderr.fileno(): (error_output, 64 * 1024, "error output"),
    }
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + 120
    try:
        for descriptor in stream_limits:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(min(remaining, 0.25)):
                descriptor = key.fd
                try:
                    block = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(descriptor)
                    continue
                buffer, limit, kind = stream_limits[descriptor]
                if len(buffer) + len(block) > limit:
                    raise ValueError("{} returned too much {}".format(label, kind))
                buffer.extend(block)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        returncode = process.wait(timeout=remaining)
    except TimeoutError:
        _terminate_process_group(process)
        raise ValueError("{} timed out".format(label)) from None
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if returncode != 0:
        error_digest = hashlib.sha256(error_output).hexdigest()
        raise ValueError(
            "{} failed with exit {}; error output bytes={} sha256={}".format(
                label,
                returncode,
                len(error_output),
                error_digest,
            )
        )
    try:
        payload = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("{} did not return valid JSON".format(label)) from error
    return bytes(output), payload


def _validate_live_audit_receipt(
    payload: Any, manifest: Dict[str, Any], stage_name: str
) -> None:
    audit = payload.get("audit") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or payload.get("errors") != []
        or not isinstance(audit, dict)
        or audit.get("validator") != VALIDATOR_ID
        or audit.get("scope") != "live"
        or audit.get("stage") != stage_name
        or audit.get("policy_sha256") != manifest["audit"]["policy_sha256"]
    ):
        raise ValueError("live host-policy audit did not satisfy the approved stage")


def _validate_runtime_receipt(payload: Any, expected: Dict[str, bool]) -> None:
    if not isinstance(payload, list):
        raise ValueError("fresh Codex MCP inventory must be a list")
    actual: Dict[str, bool] = {}
    for item in payload:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("enabled"), bool)
            or item["name"] in actual
        ):
            raise ValueError("fresh Codex MCP inventory is invalid")
        actual[item["name"]] = item["enabled"]
    if actual != expected:
        raise ValueError("fresh Codex MCP inventory differs from the approved target")
    if actual.get("db-mcp") is not False:
        raise ValueError("fresh Codex MCP inventory must keep db-mcp disabled")


def _verify_live_at_parent(descriptor: int, record: Dict[str, Any]) -> None:
    name = Path(record["path"]).name
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if record["pre_state"] == "absent":
            return
        raise ValueError("current source disappeared: {}".format(record["label"]))
    if record["pre_state"] == "absent":
        raise ValueError("expected target to remain absent: {}".format(record["label"]))
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("current source is not a regular file: {}".format(record["label"]))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(name, flags, dir_fd=descriptor)
    try:
        before = os.fstat(source_fd)
        digest = _sha256_fd(source_fd)
        after = os.fstat(source_fd)
        current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    finally:
        os.close(source_fd)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
        current.st_dev,
        current.st_ino,
    ) != (after.st_dev, after.st_ino):
        raise ValueError("current source changed during verification: {}".format(record["label"]))
    if "{:04o}".format(after.st_mode & 0o7777) != record["current_mode"] or digest != record[
        "current_sha256"
    ]:
        raise ValueError("current source differs from manifest: {}".format(record["label"]))


def _read_artifact(planned: PlannedTarget) -> bytes:
    return planned.data


def _verify_installed(descriptor: int, record: Dict[str, Any]) -> None:
    name = Path(record["path"]).name
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(name, flags, dir_fd=descriptor)
    try:
        metadata = os.fstat(source_fd)
        digest = _sha256_fd(source_fd)
    finally:
        os.close(source_fd)
    if not stat.S_ISREG(metadata.st_mode) or "{:04o}".format(
        metadata.st_mode & 0o7777
    ) != record["target_mode"] or digest != record["target_sha256"]:
        raise ValueError("post-apply target verification failed: {}".format(record["label"]))


def _verify_installed_target(record: Dict[str, Any]) -> None:
    source = Path(record["path"])
    _require_real_directory(source.parent, "{} parent".format(record["label"]))
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(str(source.parent), parent_flags)
    try:
        try:
            _verify_installed(parent_fd, record)
        except (FileNotFoundError, OSError) as error:
            raise ValueError(
                "completed migration target differs from manifest: {}".format(
                    record["label"]
                )
            ) from error
    finally:
        os.close(parent_fd)


def _verify_path_state(path: Path, mode: str, digest: str, label: str) -> None:
    _require_regular_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        actual_digest = _sha256_fd(descriptor)
        after = os.fstat(descriptor)
        current = path.lstat()
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
        current.st_dev,
        current.st_ino,
    ) != (after.st_dev, after.st_ino):
        raise ValueError("{} changed during verification".format(label))
    if "{:04o}".format(after.st_mode & 0o7777) != mode or actual_digest != digest:
        raise ValueError("{} differs from the approved state".format(label))


def _install_target(
    planned: PlannedTarget, quarantine_path: Optional[Path] = None
) -> Optional[str]:
    record = planned.record
    source = Path(record["path"])
    data = _read_artifact(planned)
    _require_real_directory(source.parent, "{} parent".format(record["label"]))
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(str(source.parent), parent_flags)
    if quarantine_path is not None:
        if record["pre_state"] != "present" or quarantine_path.parent != source.parent:
            raise ValueError("planned apply quarantine is invalid")
        temporary_name = quarantine_path.name
        if (
            not temporary_name.startswith(".host-policy-apply.")
            or len(temporary_name) != len(".host-policy-apply.") + 24
        ):
            raise ValueError("planned apply quarantine is invalid")
    else:
        temporary_name = ".host-policy-apply.{}".format(secrets.token_hex(12))
    temporary = source.parent / temporary_name
    temporary_created = False
    try:
        _verify_live_at_parent(parent_fd, record)
        temporary_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_fd = os.open(
            temporary_name, temporary_flags, 0o600, dir_fd=parent_fd
        )
        temporary_created = True
        try:
            view = memoryview(data)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("target write made no progress")
                view = view[written:]
            os.fchmod(temporary_fd, int(record["target_mode"], 8))
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        _verify_live_at_parent(parent_fd, record)
        if record["pre_state"] == "absent":
            _rename_noreplace(temporary, source)
            temporary_created = False
            retained_quarantine = None
        else:
            _exchange_paths(temporary, source)
            temporary_created = False
            try:
                _verify_path_state(
                    temporary,
                    record["current_mode"],
                    record["current_sha256"],
                    "exchanged pre-state {}".format(record["label"]),
                )
                _verify_installed(parent_fd, record)
            except BaseException as error:
                os.fsync(parent_fd)
                raise ValueError(
                    "present target exchange requires recovery from retained quarantine {}: {}".format(
                        temporary, error
                    )
                ) from None
            retained_quarantine = str(temporary)
        os.fsync(parent_fd)
        _verify_installed(parent_fd, record)
        return retained_quarantine
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _write_journal(journal_dir: Path, payload: Dict[str, Any]) -> Path:
    path = journal_dir / "journal.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".host-policy-journal.", dir=str(journal_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(str(temporary), str(path))
        directory_fd = os.open(
            str(journal_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _publish_stage_receipt(journal_dir: Path, name: str, data: bytes) -> str:
    path = journal_dir / name
    digest = hashlib.sha256(data).hexdigest()
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ValueError("stage receipt must be a regular non-symlink file")
        if _mode(path) != "0600" or _read_sealed_bytes(path, name) != data:
            raise ValueError("existing stage receipt differs from fresh verification")
        return digest
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".host-policy-stage-receipt.", dir=str(journal_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        _rename_noreplace(temporary, path)
        _fsync_directory(journal_dir)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest


def _verify_stage_receipt_files(
    journal_dir: Path, stage_audits: Dict[str, Dict[str, str]]
) -> None:
    for stage, receipt in stage_audits.items():
        for file_key, digest_key in (
            ("audit_file", "audit_sha256"),
            ("runtime_file", "runtime_sha256"),
        ):
            path = journal_dir / receipt[file_key]
            if _mode(path) != "0600":
                raise ValueError("stage receipt mode is invalid: {}".format(stage))
            data = _read_sealed_bytes(path, "stage receipt {}".format(stage))
            if hashlib.sha256(data).hexdigest() != receipt[digest_key]:
                raise ValueError("stage receipt differs from journal: {}".format(stage))


def apply_manifest(
    manifest_path: Path,
    target_root: Path,
    receipt_path: Path,
    envelope_path: Path,
    expected_envelope_sha256: str,
    expected_manifest_sha256: str,
    journal_dir: Path,
    stage_name: str,
    attempt_id: Optional[str] = None,
) -> Dict[str, Any]:
    manifest_path = Path(os.path.abspath(str(manifest_path)))
    target_root = Path(os.path.abspath(str(target_root)))
    receipt_path = Path(os.path.abspath(str(receipt_path)))
    envelope_path = Path(os.path.abspath(str(envelope_path)))
    journal_dir = Path(os.path.abspath(str(journal_dir)))
    attempt_id = secrets.token_hex(16) if attempt_id is None else attempt_id
    if not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("stage attempt id is invalid")
    _require_real_directory(journal_dir.parent, "journal parent")
    envelope_bytes, envelope = _envelope_module.load_envelope_sealed(
        envelope_path, expected_envelope_sha256
    )
    manifest_bytes, manifest = load_manifest_sealed(
        manifest_path, expected_manifest_sha256
    )
    lock_path = _host_operation_lock_path(target_home_root(manifest["targets"]))
    with _OperationLock(lock_path):
        return _apply_manifest_unlocked(
            manifest_path,
            target_root,
            receipt_path,
            envelope_path,
            expected_envelope_sha256,
            expected_manifest_sha256,
            journal_dir,
            stage_name,
            manifest_bytes,
            manifest,
            envelope_bytes,
            envelope,
            attempt_id,
        )


def _apply_manifest_unlocked(
    manifest_path: Path,
    target_root: Path,
    receipt_path: Path,
    envelope_path: Path,
    expected_envelope_sha256: str,
    expected_manifest_sha256: str,
    journal_dir: Path,
    stage_name: str,
    sealed_manifest_bytes: Optional[bytes] = None,
    sealed_manifest: Optional[Dict[str, Any]] = None,
    sealed_envelope_bytes: Optional[bytes] = None,
    sealed_envelope: Optional[Dict[str, Any]] = None,
    attempt_id: Optional[str] = None,
) -> Dict[str, Any]:
    manifest_path = Path(os.path.abspath(str(manifest_path)))
    target_root = Path(os.path.abspath(str(target_root)))
    receipt_path = Path(os.path.abspath(str(receipt_path)))
    envelope_path = Path(os.path.abspath(str(envelope_path)))
    journal_dir = Path(os.path.abspath(str(journal_dir)))
    attempt_id = secrets.token_hex(16) if attempt_id is None else attempt_id
    if not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("stage attempt id is invalid")
    if sealed_manifest_bytes is None or sealed_manifest is None:
        manifest_bytes, manifest = load_manifest_sealed(
            manifest_path, expected_manifest_sha256
        )
    else:
        manifest_bytes, manifest = sealed_manifest_bytes, sealed_manifest
    if sealed_envelope_bytes is None or sealed_envelope is None:
        envelope_bytes, envelope = _envelope_module.load_envelope_sealed(
            envelope_path, expected_envelope_sha256
        )
    else:
        envelope_bytes, envelope = sealed_envelope_bytes, sealed_envelope
    receipt_bytes, receipt = load_receipt_sealed(receipt_path)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    envelope_digest = hashlib.sha256(envelope_bytes).hexdigest()
    _envelope_module.bind_envelope(
        envelope,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        receipt_path=receipt_path,
        receipt_bytes=receipt_bytes,
        target_root=target_root,
        stage=stage_name,
        runtime_files=_runtime_file_inventory(),
    )
    expected_runtime = _expected_runtime_checks(target_root, manifest, stage_name)
    envelope_runtime = {
        item["name"]: item["expectations"]
        for item in envelope["post_apply"]["runtime_checks"]
    }
    if envelope_runtime != expected_runtime:
        raise ValueError(
            "approval envelope runtime expectations differ from the target config"
        )
    stage_names = [item["name"] for item in manifest["stages"]]
    if journal_dir.is_symlink():
        raise ValueError("journal directory must not be a symlink")
    if not journal_dir.exists() and stage_name != stage_names[0]:
        raise ValueError("the first confirmed stage must be {}".format(stage_names[0]))
    new_journal = not journal_dir.exists()
    if journal_dir.exists():
        _require_real_directory(journal_dir, "journal directory")
        journal_path = journal_dir / "journal.json"
        payload = _load_json_file(journal_path, "journal")
        if (
            payload.get("manifest") != str(manifest_path)
            or payload.get("manifest_sha256") != manifest_digest
            or payload.get("snapshot_receipt") != str(receipt_path)
            or payload.get("snapshot_receipt_sha256") != receipt_digest
            or payload.get("target_root") != str(target_root)
            or payload.get("stages") != stage_names
        ):
            raise ValueError("journal does not match the approved migration inputs")
    else:
        payload = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "status": "ready",
            "manifest": str(manifest_path),
            "manifest_sha256": manifest_digest,
            "snapshot_receipt_sha256": receipt_digest,
            "snapshot_receipt": str(receipt_path),
            "target_root": str(target_root),
            "stages": stage_names,
            "completed_stages": [],
            "completed": [],
            "active_stage": None,
            "active_label": None,
            "active_attempt_id": None,
            "rollback_required": False,
            "retained_quarantines": {},
            "stage_envelopes": {},
            "stage_envelope_paths": {},
            "stage_audits": {},
            "pending_audit_stage": None,
            "pending_audit_envelope_sha256": None,
        }
    progress = validate_journal_progress(payload, manifest)
    if not new_journal:
        _verify_stage_receipt_files(journal_dir, payload["stage_audits"])
    if progress["status"] in {
        "applying",
        "partial",
        "awaiting_stage_audit",
    } or payload["rollback_required"]:
        raise ValueError("uncertain migration must be rolled back before continuing")
    if progress["status"] == "rolled_back":
        raise ValueError("rolled-back journal cannot be reused")
    if progress["status"] == "complete":
        raise ValueError("migration journal is already complete")
    completed_stages = progress["completed_stages"]
    if len(completed_stages) >= len(stage_names) or stage_name != stage_names[len(completed_stages)]:
        raise ValueError("stages must be applied once in manifest order")
    if stage_name in payload["stage_envelopes"]:
        raise ValueError("requested stage already has an approval envelope")
    planned = preflight(
        manifest_path,
        target_root,
        receipt_path,
        stage_name,
        manifest,
        receipt,
        progress["completed"],
    )
    if new_journal:
        _require_real_directory(journal_dir.parent, "journal parent")
        journal_dir.mkdir(mode=0o700)
        _fsync_directory(journal_dir.parent)
    prior_payload = copy.deepcopy(payload)
    payload["stage_envelopes"][stage_name] = envelope_digest
    payload["stage_envelope_paths"][stage_name] = str(envelope_path)
    payload["status"] = "applying"
    payload["active_stage"] = stage_name
    payload["active_attempt_id"] = attempt_id
    journal_path = journal_dir / "journal.json"
    mutation_possible = False
    try:
        for item in planned:
            payload["active_label"] = item.record["label"]
            planned_quarantine = None
            if item.record["pre_state"] == "present":
                planned_quarantine = Path(item.record["path"]).parent / (
                    ".host-policy-apply.{}".format(secrets.token_hex(12))
                )
                quarantine_key = "apply:{}".format(item.record["label"])
                payload["retained_quarantines"][quarantine_key] = str(
                    planned_quarantine
                )
            try:
                _write_journal(journal_dir, payload)
            except BaseException as intent_error:
                if not mutation_possible:
                    try:
                        _write_journal(journal_dir, prior_payload)
                    except Exception:
                        pass
                    if isinstance(intent_error, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise PreMutationJournalError(
                        "stage intent could not be recorded before target mutation"
                    ) from None
                raise
            mutation_possible = True
            retained_quarantine = _install_target(item, planned_quarantine)
            if retained_quarantine is not None:
                quarantine_key = "apply:{}".format(item.record["label"])
                payload["retained_quarantines"][quarantine_key] = retained_quarantine
            payload["completed"].append(item.record["label"])
            payload["active_label"] = None
            _write_journal(journal_dir, payload)
    except BaseException as error:
        if isinstance(error, PreMutationJournalError):
            raise
        payload["status"] = "partial"
        payload["rollback_required"] = True
        _write_journal(journal_dir, payload)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ValueError(
            "apply did not complete; rollback from the journal recorded in {}".format(
                journal_path
            )
        ) from None
    payload["completed_stages"].append(stage_name)
    payload["active_stage"] = None
    payload["status"] = "awaiting_stage_audit"
    payload["pending_audit_stage"] = stage_name
    payload["pending_audit_envelope_sha256"] = envelope_digest
    payload["rollback_required"] = True
    _write_journal(journal_dir, payload)
    return payload


def verify_and_accept_stage(
    manifest_path: Path,
    target_root: Path,
    receipt_path: Path,
    envelope_path: Path,
    expected_envelope_sha256: str,
    expected_manifest_sha256: str,
    journal_dir: Path,
    stage_name: str,
    attempt_id: Optional[str] = None,
) -> Dict[str, Any]:
    manifest_path = Path(os.path.abspath(str(manifest_path)))
    target_root = Path(os.path.abspath(str(target_root)))
    receipt_path = Path(os.path.abspath(str(receipt_path)))
    envelope_path = Path(os.path.abspath(str(envelope_path)))
    journal_dir = Path(os.path.abspath(str(journal_dir)))
    manifest_bytes, manifest = load_manifest_sealed(
        manifest_path, expected_manifest_sha256
    )
    lock_path = _host_operation_lock_path(target_home_root(manifest["targets"]))
    with _OperationLock(lock_path):
        envelope_bytes, envelope = _envelope_module.load_envelope_sealed(
            envelope_path, expected_envelope_sha256
        )
        receipt_bytes, receipt = load_receipt_sealed(receipt_path)
        _envelope_module.bind_envelope(
            envelope,
            manifest_path=manifest_path,
            manifest_bytes=manifest_bytes,
            manifest=manifest,
            receipt_path=receipt_path,
            receipt_bytes=receipt_bytes,
            target_root=target_root,
            stage=stage_name,
            runtime_files=_runtime_file_inventory(),
        )
        expected_runtime = _expected_runtime_checks(
            target_root, manifest, stage_name
        )
        envelope_runtime = {
            item["name"]: item["expectations"]
            for item in envelope["post_apply"]["runtime_checks"]
        }
        if envelope_runtime != expected_runtime:
            raise ValueError(
                "approval envelope runtime expectations differ from the target config"
            )
        _require_real_directory(journal_dir, "journal directory")
        journal_path = journal_dir / "journal.json"
        payload = _load_json_file(journal_path, "journal")
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        envelope_digest = hashlib.sha256(envelope_bytes).hexdigest()
        if (
            payload.get("manifest") != str(manifest_path)
            or payload.get("manifest_sha256") != manifest_digest
            or payload.get("snapshot_receipt") != str(receipt_path)
            or payload.get("snapshot_receipt_sha256") != receipt_digest
            or payload.get("target_root") != str(target_root)
            or payload.get("stage_envelopes", {}).get(stage_name)
            != envelope_digest
            or payload.get("stage_envelope_paths", {}).get(stage_name)
            != str(envelope_path)
            or (
                attempt_id is not None
                and payload.get("active_attempt_id") != attempt_id
            )
        ):
            raise ValueError("journal does not match the approved stage inputs")
        progress = validate_journal_progress(payload, manifest)
        _verify_stage_receipt_files(journal_dir, payload["stage_audits"])
        if (
            progress["status"] != "awaiting_stage_audit"
            or payload.get("pending_audit_stage") != stage_name
            or payload.get("pending_audit_envelope_sha256") != envelope_digest
        ):
            raise ValueError("journal is not awaiting the approved stage audit")
        before_state = _verify_stage_live_state(manifest, progress["completed"])
        home_root = target_home_root(manifest["targets"])
        verification_environment = {
            "HOME": str(home_root),
            "CODEX_HOME": str(home_root / ".codex"),
            "TMPDIR": str(journal_dir),
        }
        audit_bytes, audit_receipt = _run_json_command(
            envelope["post_apply"]["audit_command"],
            "live host-policy audit",
            environment_overrides=verification_environment,
        )
        _validate_live_audit_receipt(audit_receipt, manifest, stage_name)
        runtime_receipts = {}
        for check in envelope["post_apply"]["runtime_checks"]:
            check_name = check["name"]
            _, runtime_receipt = _run_json_command(
                check["command"],
                "fresh Codex MCP inventory {}".format(check_name),
                environment_overrides=verification_environment,
            )
            _validate_runtime_receipt(
                runtime_receipt, expected_runtime[check_name]
            )
            runtime_receipts[check_name] = runtime_receipt
        _envelope_module.verify_command_executables(envelope)
        after_state = _verify_stage_live_state(manifest, progress["completed"])
        if before_state != after_state:
            raise ValueError("live host state changed during stage verification")
        audit_bytes = (
            json.dumps(
                audit_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        runtime_bytes = (
            json.dumps(
                runtime_receipts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        audit_file = "{}-live-audit.json".format(stage_name)
        runtime_file = "{}-runtime-mcp.json".format(stage_name)
        payload["stage_audits"][stage_name] = {
            "audit_file": audit_file,
            "audit_sha256": _publish_stage_receipt(
                journal_dir, audit_file, audit_bytes
            ),
            "runtime_file": runtime_file,
            "runtime_sha256": _publish_stage_receipt(
                journal_dir, runtime_file, runtime_bytes
            ),
            "live_state_sha256": after_state,
        }
        payload["pending_audit_stage"] = None
        payload["pending_audit_envelope_sha256"] = None
        payload["active_attempt_id"] = None
        payload["rollback_required"] = False
        payload["status"] = (
            "complete"
            if payload["completed_stages"]
            == [item["name"] for item in manifest["stages"]]
            else "ready"
        )
        _write_journal(journal_dir, payload)
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = StructuredArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--snapshot-receipt", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--journal-dir", type=Path)
    parser.add_argument("--approval-envelope", type=Path)
    parser.add_argument("--expected-envelope-sha256")
    parser.add_argument("--expected-manifest-sha256")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--confirm-apply", action="store_true")
    return parser


def _journal_requires_attempt_recovery(
    journal_path: Path,
    *,
    attempt_id: str,
    stage_name: str,
    envelope_sha256: str,
    manifest_path: Path,
    receipt_path: Path,
    target_root: Path,
) -> bool:
    if journal_path.is_symlink() or not journal_path.is_file():
        return False
    try:
        journal = _load_json_file(journal_path, "journal")
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    status = journal.get("status")
    stage_matches = (
        journal.get("active_stage") == stage_name
        if status in {"applying", "partial"}
        else journal.get("pending_audit_stage") == stage_name
        if status == "awaiting_stage_audit"
        else False
    )
    mutation_evidence = status == "awaiting_stage_audit"
    if status in {"applying", "partial"}:
        try:
            manifest_bytes, manifest = load_manifest_sealed(manifest_path)
            if hashlib.sha256(manifest_bytes).hexdigest() != journal.get(
                "manifest_sha256"
            ):
                return False
            stage_index = next(
                index
                for index, item in enumerate(manifest["stages"])
                if item["name"] == stage_name
            )
            prior_labels = [
                label
                for item in manifest["stages"][:stage_index]
                for label in item["labels"]
            ]
            completed = journal.get("completed")
            mutation_evidence = bool(
                journal.get("active_label") is not None
                or (
                    isinstance(completed, list)
                    and completed[: len(prior_labels)] == prior_labels
                    and len(completed) > len(prior_labels)
                )
            )
        except (OSError, ValueError, json.JSONDecodeError, StopIteration):
            return False
    return bool(
        journal.get("rollback_required") is True
        and mutation_evidence
        and journal.get("active_attempt_id") == attempt_id
        and stage_matches
        and journal.get("stage_envelopes", {}).get(stage_name)
        == envelope_sha256
        and journal.get("manifest")
        == str(Path(os.path.abspath(str(manifest_path))))
        and journal.get("snapshot_receipt")
        == str(Path(os.path.abspath(str(receipt_path))))
        and journal.get("target_root")
        == str(Path(os.path.abspath(str(target_root))))
    )


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.confirm_apply:
            if args.journal_dir is None:
                raise ValueError("--confirm-apply requires --journal-dir")
            if (
                args.approval_envelope is None
                or args.expected_envelope_sha256 is None
                or args.expected_manifest_sha256 is None
            ):
                raise ValueError(
                    "--confirm-apply requires approval envelope and expected digests"
                )
            attempt_id = secrets.token_hex(16)
            try:
                apply_manifest(
                    args.manifest,
                    args.target_root,
                    args.snapshot_receipt,
                    args.approval_envelope,
                    args.expected_envelope_sha256,
                    args.expected_manifest_sha256,
                    args.journal_dir,
                    args.stage,
                    attempt_id,
                )
                journal = verify_and_accept_stage(
                    args.manifest,
                    args.target_root,
                    args.snapshot_receipt,
                    args.approval_envelope,
                    args.expected_envelope_sha256,
                    args.expected_manifest_sha256,
                    args.journal_dir,
                    args.stage,
                    attempt_id,
                )
                payload = {
                    "status": "ok",
                    "action": "apply-and-verify",
                    "journal": journal,
                }
            except Exception as error:
                recovery: Dict[str, Any] = {"attempted": False}
                journal_path = args.journal_dir / "journal.json"
                if not isinstance(
                    error, PreMutationJournalError
                ) and _journal_requires_attempt_recovery(
                    journal_path,
                    attempt_id=attempt_id,
                    stage_name=args.stage,
                    envelope_sha256=args.expected_envelope_sha256,
                    manifest_path=args.manifest,
                    receipt_path=args.snapshot_receipt,
                    target_root=args.target_root,
                ):
                    recovery["attempted"] = True
                    try:
                        recovery.update(
                            {"status": "ok", "result": rollback(journal_path, True)}
                        )
                    except Exception as rollback_error:
                        recovery.update(
                            {"status": "error", "message": str(rollback_error)}
                        )
                payload = {
                    "status": "error",
                    "action": "apply-and-verify",
                    "issues": [
                        {"code": "migration.input_error", "message": str(error)}
                    ],
                    "recovery": recovery,
                }
        else:
            manifest_bytes, manifest = load_manifest_sealed(
                args.manifest, args.expected_manifest_sha256
            )
            receipt_bytes, receipt = load_receipt_sealed(args.snapshot_receipt)
            planned = preflight(
                args.manifest,
                args.target_root,
                args.snapshot_receipt,
                args.stage,
                manifest,
                receipt,
            )
            payload = {
                "status": "ok",
                "action": "dry-run",
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "snapshot_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "stage": args.stage,
                "labels": [item.record["label"] for item in planned],
            }
    except Exception as error:
        payload = {
            "status": "error",
            "issues": [{"code": "migration.input_error", "message": str(error)}],
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if payload["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
