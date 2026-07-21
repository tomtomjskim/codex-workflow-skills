"""Fail-closed validation for coordination artifacts and handoffs."""

import copy
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Dict, List, Optional, Set

from .canonical_json import CanonicalJSONError, sha256_id
from .derive import derive_coordination
from .receipts import ValidationReceipt


class ValidationError(ValueError):
    """Raised when coordination evidence is unsafe or inconsistent."""


_SUBMITTED_SET_FIELDS = (
    "affected_consumers",
    "required_handoffs",
    "required_acknowledgements",
    "required_reviewers",
)
_GLOB_METACHARACTERS = frozenset("*?[]")


def _canonical_hash(value: object) -> str:
    try:
        return sha256_id(value)
    except CanonicalJSONError as error:
        raise ValidationError("invalid canonical JSON: {}".format(error)) from error


def _workstreams(manifest: object) -> List[dict]:
    if not isinstance(manifest, dict):
        raise ValidationError("manifest must be an object")
    workstreams = manifest.get("workstreams")
    if not isinstance(workstreams, list):
        raise ValidationError("manifest workstreams must be a list")
    for workstream in workstreams:
        if not isinstance(workstream, dict):
            raise ValidationError("manifest workstream must be an object")
        if not isinstance(workstream.get("id"), str) or not workstream["id"]:
            raise ValidationError("workstream id must be a non-empty string")
    return workstreams


def _normalize_path(repo_root: Path, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("owned path must be a non-empty string")
    if any(character in value for character in _GLOB_METACHARACTERS):
        raise ValidationError("glob metacharacter in path: {}".format(value))
    if "\\" in value:
        raise ValidationError("path must use POSIX separators: {}".format(value))
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValidationError("absolute path is not allowed: {}".format(value))
    if ".." in posix_path.parts:
        raise ValidationError("parent traversal is not allowed: {}".format(value))

    normalized = str(posix_path)
    if normalized in ("", "."):
        raise ValidationError("repository root cannot be an owned path")

    root = repo_root.resolve(strict=True)
    resolved = (root / normalized).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValidationError("path escapes repository root: {}".format(value)) from error
    return normalized


def _validate_paths(repo_root: Path, workstreams: List[dict]) -> Dict[str, List[str]]:
    normalized_by_workstream = {}
    all_owned_paths = []
    owners = set()
    ids = set()

    for workstream in workstreams:
        workstream_id = workstream["id"]
        if workstream_id in ids:
            raise ValidationError("duplicate workstream id: {}".format(workstream_id))
        ids.add(workstream_id)

        owner = workstream.get("owner")
        if not isinstance(owner, str) or not owner:
            raise ValidationError("workstream owner must be a non-empty string")
        if owner in owners:
            raise ValidationError("duplicate owner: {}".format(owner))
        owners.add(owner)

        for field in ("scope", "exclusive_write_paths"):
            values = workstream.get(field)
            if not isinstance(values, list):
                raise ValidationError("{} must be a list".format(field))
            for value in values:
                _normalize_path(repo_root, value)

        normalized = sorted(
            {_normalize_path(repo_root, path) for path in workstream["exclusive_write_paths"]}
        )
        if len(normalized) != len(workstream["exclusive_write_paths"]):
            raise ValidationError("path overlap: duplicate owned path")
        normalized_by_workstream[workstream_id] = normalized
        all_owned_paths.extend((PurePosixPath(path), workstream_id) for path in normalized)

    for index, (path, workstream_id) in enumerate(all_owned_paths):
        for other_path, other_workstream_id in all_owned_paths[index + 1 :]:
            if path == other_path or path in other_path.parents or other_path in path.parents:
                raise ValidationError(
                    "path overlap between {} and {}: {} / {}".format(
                        workstream_id, other_workstream_id, path, other_path
                    )
                )
    return dict(sorted(normalized_by_workstream.items()))


def _validate_dag(workstreams: List[dict]) -> None:
    ids = {workstream["id"] for workstream in workstreams}
    dependencies = {}
    for workstream in workstreams:
        depends_on = workstream.get("depends_on")
        if not isinstance(depends_on, list) or any(
            not isinstance(dependency, str) or not dependency
            for dependency in depends_on
        ):
            raise ValidationError("workstream dependencies must be strings")
        for dependency in depends_on:
            if dependency not in ids:
                raise ValidationError("unknown dependency: {}".format(dependency))
        dependencies[workstream["id"]] = set(depends_on)

    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(workstream_id: str) -> None:
        if workstream_id in visiting:
            raise ValidationError("dependency cycle includes {}".format(workstream_id))
        if workstream_id in visited:
            return
        visiting.add(workstream_id)
        for dependency in sorted(dependencies[workstream_id]):
            visit(dependency)
        visiting.remove(workstream_id)
        visited.add(workstream_id)

    for workstream_id in sorted(ids):
        visit(workstream_id)


def _required_sets(derived: object) -> dict:
    return {
        "affected_consumers": list(derived.affected_consumers),
        "required_handoffs": [list(handoff) for handoff in derived.required_handoffs],
        "required_acknowledgements": list(derived.required_acknowledgements),
        "required_reviewers": list(derived.required_reviewers),
    }


def _validate_submitted_derivation(manifest: dict, derived: object) -> None:
    expected = _required_sets(derived)
    for field in _SUBMITTED_SET_FIELDS:
        if field in manifest and manifest[field] != expected[field]:
            raise ValidationError("submitted required sets differ from derivation: {}".format(field))
    if "route" in manifest and manifest["route"] != derived.route:
        raise ValidationError("submitted route differs from derivation")


def _validate_contract(
    contract: object, manifest_hash: str, inventory_hash: str
) -> str:
    if not isinstance(contract, dict):
        raise ValidationError("contract must be an object")
    core = contract.get("contract_core")
    ledger = contract.get("execution_ledger")
    if not isinstance(core, dict) or not isinstance(ledger, dict):
        raise ValidationError("contract core and execution ledger are required")

    core_hash = _canonical_hash(core)
    if core.get("manifest_hash") != manifest_hash:
        raise ValidationError("contract core manifest hash mismatch")
    if core.get("inventory_hash") != inventory_hash:
        raise ValidationError("contract core inventory hash mismatch")
    if ledger.get("contract_core_hash") != core_hash:
        raise ValidationError("contract core hash mismatch")

    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("execution ledger entries must be a list")
    previous_hash = _canonical_hash(None)
    entry_hashes = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValidationError("ledger entry must be an object")
        if entry.get("contract_core_hash") != core_hash:
            raise ValidationError("contract core hash mismatch in ledger entry")
        if entry.get("previous_entry_hash") != previous_hash:
            raise ValidationError("ledger chain mismatch at entry {}".format(index))
        entry_hash = entry.get("entry_hash")
        body = {key: copy.deepcopy(value) for key, value in entry.items() if key != "entry_hash"}
        if entry_hash != _canonical_hash(body):
            raise ValidationError("ledger entry hash mismatch at entry {}".format(index))
        entry_hashes.append(entry_hash)
        previous_hash = entry_hash

    if ledger.get("ledger_hash") != _canonical_hash(entry_hashes):
        raise ValidationError("ledger hash mismatch")
    return core_hash


def _checkout_tree_hash(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValidationError("checkout tree hash unavailable") from error
    tree_hash = result.stdout.strip()
    if not tree_hash:
        raise ValidationError("checkout tree hash unavailable")
    return tree_hash


def _recorded_at(clock: Callable[[], object]) -> str:
    value = clock()
    if isinstance(value, str):
        if not value.endswith("Z"):
            raise ValidationError("recorded_at must be UTC RFC3339")
        return value
    if not isinstance(value, datetime):
        raise ValidationError("clock must return datetime or UTC RFC3339 string")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_coordination(
    repo_root: Path,
    manifest: dict,
    inventory: dict,
    contract: Optional[dict],
    *,
    trigger_matrix: Optional[dict] = None,
    checkout_tree_hash: Optional[str] = None,
    run_id_factory: Optional[Callable[[], object]] = None,
    clock: Optional[Callable[[], object]] = None
) -> ValidationReceipt:
    """Validate DAG, canonical paths, completeness, hash domains, and derived sets."""
    root = Path(repo_root)
    if not root.is_dir():
        raise ValidationError("repository root must be an existing directory")
    workstreams = _workstreams(manifest)
    normalized_paths = _validate_paths(root, workstreams)
    _validate_dag(workstreams)

    manifest_hash = _canonical_hash(manifest)
    inventory_hash = _canonical_hash(inventory)
    contract_core_hash = None
    if contract is not None:
        contract_core_hash = _validate_contract(
            contract, manifest_hash, inventory_hash
        )

    derived = derive_coordination(manifest, inventory, trigger_matrix)
    if derived.route == "blocked":
        if trigger_matrix is None:
            raise ValidationError("trigger matrix is required")
        raise ValidationError(
            "coordination derivation blocked: {}".format(derived.completeness)
        )
    _validate_submitted_derivation(manifest, derived)

    if contract is None and derived.route == "contracted":
        raise ValidationError("current contract required for contracted route")
    if (
        contract is not None
        and derived.route == "contracted"
        and contract["execution_ledger"].get("status") != "frozen"
    ):
        raise ValidationError("contracted route execution ledger must be frozen")

    tree_hash = checkout_tree_hash
    if tree_hash is None:
        tree_hash = _checkout_tree_hash(root)
    if not isinstance(tree_hash, str) or not tree_hash:
        raise ValidationError("checkout tree hash must be a non-empty string")

    run_factory = run_id_factory or (lambda: str(uuid.uuid4()))
    run_id = run_factory()
    if not isinstance(run_id, str) or not run_id:
        raise ValidationError("run id must be a non-empty string")
    time_source = clock or (lambda: datetime.now(timezone.utc))

    return ValidationReceipt(
        schema_version=1,
        manifest_hash=manifest_hash,
        inventory_hash=inventory_hash,
        contract_core_hash=contract_core_hash,
        checkout_tree_hash=tree_hash,
        derived_route=derived.route,
        required_sets=_required_sets(derived),
        normalized_paths=normalized_paths,
        run_id=run_id,
        recorded_at=_recorded_at(time_source),
        derived_profiles={
            "shared_interface": derived.profiles.shared_interface,
        },
    )


def validate_handoff(
    repo_root: Path,
    receipt: ValidationReceipt,
    workstream_id: str,
    changed_paths: List[str],
) -> None:
    """Reject tracked or untracked changes outside derived ownership."""
    root = Path(repo_root)
    if not root.is_dir():
        raise ValidationError("repository root must be an existing directory")
    if not isinstance(receipt, ValidationReceipt):
        raise ValidationError("validation receipt is required")
    owned_values = receipt.normalized_paths.get(workstream_id)
    if not isinstance(owned_values, list):
        raise ValidationError("unknown workstream: {}".format(workstream_id))
    if not isinstance(changed_paths, list):
        raise ValidationError("changed paths must be a list")

    owned_paths = [PurePosixPath(_normalize_path(root, path)) for path in owned_values]
    outside = []
    for value in changed_paths:
        normalized = PurePosixPath(_normalize_path(root, value))
        if not any(
            normalized == owned or owned in normalized.parents for owned in owned_paths
        ):
            outside.append(str(normalized))
    if outside:
        raise ValidationError(
            "changed paths outside owned paths for {}: {}".format(
                workstream_id, ", ".join(sorted(outside))
            )
        )
