"""Fail-closed validation for coordination artifacts and handoffs."""

import copy
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
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
    "required_checkpoints",
    "required_acknowledgements",
    "required_reviewers",
)
_GLOB_METACHARACTERS = frozenset("*?[]")
_CONTRACT_KEYS = {"contract_core", "execution_ledger"}
_CORE_KEYS = {
    "schema_version",
    "manifest_hash",
    "inventory_hash",
    "revision",
    "parent_contract_core_hash",
    "contract_owner",
    "integration_owner",
    "derived_profile",
    "extension_requirements",
}
_PROFILE_KEYS = {
    "shared_interface",
    "path_overlap",
    "integration_dependency",
}
_EXTENSION_KEYS = {"interface_contract", "path_ownership", "integration"}
_LEDGER_KEYS = {
    "contract_core_hash",
    "status",
    "entries",
    "ledger_hash",
    "integration_gate",
    "reviewer_registry",
}
_ENTRY_KEYS = {
    "contract_core_hash",
    "previous_entry_hash",
    "checkout_tree_hash",
    "producer_id",
    "command_or_scenario_id",
    "artifact_digest",
    "run_id",
    "recorded_at",
    "record_type",
    "subject_id",
    "status",
    "entry_hash",
}
_REVIEWER_KEYS = {
    "lens",
    "canonical_agent",
    "required",
    "contract_core_hash",
    "status",
    "dispatch_evidence",
    "completion_evidence",
    "defer_receipt",
}
_SHA256_ID = re.compile(r"sha256:[0-9a-f]{64}")


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
    if not unicodedata.is_normalized("NFC", value):
        raise ValidationError("path must use Unicode NFC: {}".format(value))
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

    try:
        root = repo_root.resolve(strict=True)
        resolved = (root / normalized).resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValidationError("path identity cannot be resolved: {}".format(value)) from error
    try:
        canonical_relative = resolved.relative_to(root)
    except ValueError as error:
        raise ValidationError("path escapes repository root: {}".format(value)) from error
    canonical = str(canonical_relative)
    if canonical in ("", "."):
        raise ValidationError("repository root cannot be an owned path")
    if not unicodedata.is_normalized("NFC", canonical):
        raise ValidationError("resolved path must use Unicode NFC: {}".format(value))

    current = root
    for part in posix_path.parts:
        if not current.exists():
            break
        if not current.is_dir():
            raise ValidationError("path parent is not a directory: {}".format(value))
        try:
            children = tuple(current.iterdir())
        except OSError as error:
            raise ValidationError(
                "path identity cannot be verified: {}".format(value)
            ) from error
        exact = next((child for child in children if child.name == part), None)
        if exact is not None:
            current = exact
            continue
        identity = unicodedata.normalize("NFC", part).casefold()
        if any(
            unicodedata.normalize("NFC", child.name).casefold() == identity
            for child in children
        ):
            raise ValidationError("path alias does not match filesystem case: {}".format(value))
        break
    return canonical


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
            identity = PurePosixPath(*(part.casefold() for part in path.parts))
            other_identity = PurePosixPath(
                *(part.casefold() for part in other_path.parts)
            )
            actual_overlap = (
                path == other_path
                or path in other_path.parents
                or other_path in path.parents
            )
            identity_overlap = (
                identity == other_identity
                or identity in other_identity.parents
                or other_identity in identity.parents
            )
            if identity_overlap and not actual_overlap:
                raise ValidationError(
                    "path alias between {} and {}: {} / {}".format(
                        workstream_id, other_workstream_id, path, other_path
                    )
                )
            if actual_overlap:
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
        "required_checkpoints": [
            list(checkpoint) for checkpoint in derived.required_checkpoints
        ],
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


def _exact_object(value: object, keys: Set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError("{} schema is invalid".format(label))
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("{} must be a non-empty string".format(label))
    return value


def _sha256_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_ID.fullmatch(value) is None:
        raise ValidationError("{} must be a sha256 identifier".format(label))
    return value


def _entry_subject(entry: dict) -> object:
    record_type = entry["record_type"]
    subject = entry["subject_id"]
    if record_type in ("handoff", "checkpoint"):
        if (
            not isinstance(subject, list)
            or len(subject) != 2
            or any(not isinstance(item, str) or not item for item in subject)
        ):
            raise ValidationError("edge ledger subject_id must contain two workstreams")
        return tuple(subject)
    if record_type == "acknowledgement":
        return _nonempty_string(subject, "acknowledgement subject_id")
    raise ValidationError("ledger record_type is invalid")


def _required_ledger_records(derived: object) -> Set[object]:
    records = {
        ("handoff", tuple(edge)) for edge in derived.required_handoffs
    }
    records.update(
        ("checkpoint", tuple(edge)) for edge in derived.required_checkpoints
    )
    records.update(
        ("acknowledgement", workstream_id)
        for workstream_id in derived.required_acknowledgements
    )
    return records


def _validate_contract(
    contract: object,
    manifest_hash: str,
    inventory_hash: str,
    derived: object,
    checkout_tree_hash: str,
) -> str:
    contract = _exact_object(contract, _CONTRACT_KEYS, "contract")
    core = contract.get("contract_core")
    ledger = contract.get("execution_ledger")
    core = _exact_object(core, _CORE_KEYS, "contract core")
    ledger = _exact_object(ledger, _LEDGER_KEYS, "execution ledger")

    if core.get("schema_version") != 1:
        raise ValidationError("contract core schema_version must be 1")
    revision = core.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValidationError("contract core revision must be a positive integer")
    parent_hash = core.get("parent_contract_core_hash")
    if revision == 1 and parent_hash is not None:
        raise ValidationError("initial contract core parent hash must be null")
    if revision > 1:
        _sha256_identifier(parent_hash, "parent contract core hash")
    _nonempty_string(core.get("contract_owner"), "contract owner")
    _nonempty_string(core.get("integration_owner"), "integration owner")

    core_hash = _canonical_hash(core)
    if core.get("manifest_hash") != manifest_hash:
        raise ValidationError("contract core manifest hash mismatch")
    if core.get("inventory_hash") != inventory_hash:
        raise ValidationError("contract core inventory hash mismatch")
    expected_profile = {
        "shared_interface": derived.profiles.shared_interface,
        "path_overlap": derived.profiles.path_overlap,
        "integration_dependency": derived.profiles.integration_dependency,
    }
    profile = _exact_object(
        core.get("derived_profile"), _PROFILE_KEYS, "contract derived profile"
    )
    if any(type(value) is not bool for value in profile.values()):
        raise ValidationError("contract derived profile values must be booleans")
    if profile != expected_profile:
        raise ValidationError("contract derived profile differs from derivation")

    extensions = _exact_object(
        core.get("extension_requirements"),
        _EXTENSION_KEYS,
        "contract extension requirements",
    )
    for name, value in extensions.items():
        if not isinstance(value, dict):
            raise ValidationError("contract extension {} must be an object".format(name))
        _canonical_hash(value)
    active_extensions = {
        "shared_interface": "interface_contract",
        "path_overlap": "path_ownership",
        "integration_dependency": "integration",
    }
    for profile_name, extension_name in active_extensions.items():
        if expected_profile[profile_name] and not extensions[extension_name]:
            raise ValidationError(
                "active profile requires non-empty {} extension".format(extension_name)
            )

    if ledger.get("contract_core_hash") != core_hash:
        raise ValidationError("contract core hash mismatch")

    if ledger.get("status") not in ("draft", "frozen", "changed"):
        raise ValidationError("execution ledger status is invalid")

    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("execution ledger entries must be a list")
    previous_hash = _canonical_hash(None)
    entry_hashes = []
    completed_records = set()
    for index, entry in enumerate(entries):
        entry = _exact_object(entry, _ENTRY_KEYS, "ledger entry")
        if entry.get("contract_core_hash") != core_hash:
            raise ValidationError("contract core hash mismatch in ledger entry")
        if entry.get("previous_entry_hash") != previous_hash:
            raise ValidationError("ledger chain mismatch at entry {}".format(index))
        if entry.get("checkout_tree_hash") != checkout_tree_hash:
            raise ValidationError("ledger entry checkout tree hash mismatch")
        for field in ("producer_id", "command_or_scenario_id", "run_id"):
            _nonempty_string(entry.get(field), "ledger entry {}".format(field))
        _sha256_identifier(entry.get("artifact_digest"), "artifact digest")
        _recorded_at(lambda: entry.get("recorded_at"))
        if entry.get("status") not in ("completed", "failed", "stale"):
            raise ValidationError("ledger entry status is invalid")
        record = (entry.get("record_type"), _entry_subject(entry))
        if record not in _required_ledger_records(derived):
            raise ValidationError("unexpected ledger evidence record")
        if record in completed_records:
            raise ValidationError("duplicate ledger evidence record")
        if entry["status"] == "completed":
            completed_records.add(record)
        entry_hash = entry.get("entry_hash")
        body = {key: copy.deepcopy(value) for key, value in entry.items() if key != "entry_hash"}
        if entry_hash != _canonical_hash(body):
            raise ValidationError("ledger entry hash mismatch at entry {}".format(index))
        entry_hashes.append(entry_hash)
        previous_hash = entry_hash

    if ledger.get("ledger_hash") != _canonical_hash(entry_hashes):
        raise ValidationError("ledger hash mismatch")

    missing_records = _required_ledger_records(derived).difference(completed_records)
    if missing_records:
        raise ValidationError("required ledger evidence is missing or incomplete")

    gate = _exact_object(
        ledger.get("integration_gate"), {"status"}, "integration gate"
    )
    if gate.get("status") != "open":
        raise ValidationError("integration gate cannot be self-declared closed")

    registry = ledger.get("reviewer_registry")
    if not isinstance(registry, list):
        raise ValidationError("reviewer registry must be a list")
    completed_reviewers = set()
    seen_reviewers = set()
    expected_reviewers = set(derived.required_reviewers)
    for reviewer in registry:
        reviewer = _exact_object(reviewer, _REVIEWER_KEYS, "reviewer registry entry")
        canonical_agent = _nonempty_string(
            reviewer.get("canonical_agent"), "reviewer canonical agent"
        )
        _nonempty_string(reviewer.get("lens"), "reviewer lens")
        if canonical_agent not in expected_reviewers:
            raise ValidationError("unexpected reviewer registry entry")
        if canonical_agent in seen_reviewers:
            raise ValidationError("duplicate reviewer registry entry")
        seen_reviewers.add(canonical_agent)
        if reviewer.get("required") is not True:
            raise ValidationError("required reviewer registry entry must be required")
        if reviewer.get("contract_core_hash") != core_hash:
            raise ValidationError("reviewer registry contract core hash mismatch")
        if reviewer.get("defer_receipt") is not None:
            raise ValidationError("reviewer defer receipts require a separate validator")
        if reviewer.get("status") != "completed":
            raise ValidationError("required reviewer is not completed")
        if not isinstance(reviewer.get("dispatch_evidence"), dict) or not reviewer[
            "dispatch_evidence"
        ]:
            raise ValidationError("required reviewer dispatch evidence is missing")
        if not isinstance(reviewer.get("completion_evidence"), dict) or not reviewer[
            "completion_evidence"
        ]:
            raise ValidationError("required reviewer completion evidence is missing")
        _canonical_hash(reviewer["dispatch_evidence"])
        _canonical_hash(reviewer["completion_evidence"])
        completed_reviewers.add(canonical_agent)
    if completed_reviewers != expected_reviewers:
        raise ValidationError("required reviewer registry is incomplete")
    return core_hash


def _validate_checkout_tree_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValidationError("checkout tree hash must be 40 or 64 lowercase hex characters")
    return value


def _recorded_at(clock: Callable[[], object]) -> str:
    value = clock()
    if isinstance(value, str):
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value
        ) is None:
            raise ValidationError("recorded_at must be UTC RFC3339")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise ValidationError("recorded_at must be UTC RFC3339") from error
        if parsed.utcoffset() != timedelta(0):
            raise ValidationError("recorded_at must be UTC RFC3339")
        return value
    if not isinstance(value, datetime):
        raise ValidationError("clock must return timezone-aware UTC RFC3339")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValidationError("recorded_at must be UTC RFC3339")
    recorded_at = value.isoformat().replace("+00:00", "Z")
    return _recorded_at(lambda: recorded_at)


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
    tree_hash = _validate_checkout_tree_hash(checkout_tree_hash)
    derived = derive_coordination(
        manifest,
        inventory,
        trigger_matrix,
        normalized_paths=normalized_paths,
    )
    if derived.route == "blocked":
        if trigger_matrix is None:
            raise ValidationError("trigger matrix is required")
        raise ValidationError(
            "coordination derivation blocked: {}".format(derived.completeness)
        )
    _validate_submitted_derivation(manifest, derived)

    contract_core_hash = None
    if contract is not None:
        contract_core_hash = _validate_contract(
            contract,
            manifest_hash,
            inventory_hash,
            derived,
            tree_hash,
        )

    if contract is None and derived.route == "contracted":
        raise ValidationError("current contract required for contracted route")
    if (
        contract is not None
        and derived.route == "contracted"
        and contract["execution_ledger"].get("status") != "frozen"
    ):
        raise ValidationError("contracted route execution ledger must be frozen")

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
            "path_overlap": derived.profiles.path_overlap,
            "integration_dependency": derived.profiles.integration_dependency,
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
    if not isinstance(owned_values, tuple):
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
