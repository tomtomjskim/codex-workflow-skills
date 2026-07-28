"""Zero-model-call orchestration for the Phase A harness experiment."""

from collections.abc import Mapping as ABCMapping
from collections.abc import Sequence as ABCSequence
from dataclasses import dataclass, field, fields
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Sequence, Tuple
import unicodedata

from scripts.live_eval.checkout import CheckoutManifest
from scripts.live_eval.experiment_plan import (
    CANARY_ARGV_TEMPLATE_DIGEST,
    CANARY_OVERLAY_RECIPE_DIGEST,
    CANARY_RESPONSE_SCHEMA_DIGEST,
    ENVIRONMENT_POLICY_DIGEST,
    PILOT_ARGV_TEMPLATE_DIGEST,
    PILOT_RESPONSE_SCHEMA_DIGEST,
    ROOT_CAPABILITY_POLICY_DIGEST,
    CanonicalExperimentInput,
    CanaryInvocationTemplate,
    ExperimentPlan,
    ExperimentPlanError,
    PilotInvocationPlan,
    build_experiment_plan,
    build_pilot_schedule,
    derive_static_evidence_digests,
    experiment_input_bytes,
    load_experiment_input,
)
from scripts.live_eval.experiment_receipts import (
    CanonicalReceipt,
    ExperimentReceiptError,
    make_receipt,
    replay_runtime_history,
    validate_static_receipt_graph,
)
from scripts.live_eval.harness import (
    HarnessError,
    HarnessManifest,
    HarnessPreflightResult,
    HarnessSourceManifest,
    load_harness_source,
    materialize_harness_home,
    verify_loaded_harness,
)
from scripts.live_eval.task_snapshot import (
    CapturedTaskObjects,
    MaterializedTaskSnapshot,
    TaskSnapshotError,
    TaskSnapshotMaterializer,
    TaskSnapshotPolicy,
    TaskSourceSpec,
    TaskTreeEntry,
    prepare_task_source,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_OID_PATTERNS = {
    "sha1": re.compile(r"^sha1:[0-9a-f]{40}$"),
    "sha256": re.compile(r"^sha256:[0-9a-f]{64}$"),
}
_RESULT_DIGEST_FIELDS = (
    "bundle_digest",
    "current_profile_digest",
    "lean_profile_digest",
    "task_corpus_receipt_digest",
    "plan_digest",
    "preflight_receipt_digest",
)
_RESULT_MASKS = frozenset(
    {
        "000000",
        "100000",
        "110000",
        "111000",
        "111100",
        "111110",
        "111111",
    }
)
_RESULT_REASONS = frozenset(
    {
        "experiment_preflight_invalid",
        "harness_preflight_blocked",
        "task_snapshot_preflight_blocked",
        "static_receipt_preflight_blocked",
        "experiment_plan_preflight_blocked",
        "task_snapshot_cleanup_required",
        "static_preflight_verified",
    }
)
_MAX_COMPONENT_BYTES = 255
_MAX_RELATIVE_PATH_BYTES = 4096
_MAX_TREE_DEPTH = 72
_MAX_OWNED_LEDGER_ENTRIES = 1_000_000
_MAX_HANDOFF_PREFIXES = 2
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class ExperimentPreflightError(ValueError):
    """A fixed, path-free Phase A preflight failure."""


def _fail(reason: str = "experiment_preflight_invalid") -> None:
    if reason not in _RESULT_REASONS:
        reason = "experiment_preflight_invalid"
    try:
        raise ExperimentPreflightError(reason) from None
    except ExperimentPreflightError as error:
        error.__context__ = None
        error.__cause__ = None
        raise


def _close_descriptor(descriptor: int) -> bool:
    active = sys.exc_info()[1]
    try:
        os.close(descriptor)
    except BaseException:
        if active is not None and not isinstance(active, Exception):
            return False
        raise
    return True


def _detached_exact_absolute_path(value: object) -> Path:
    try:
        raw = os.fspath(value)
    except Exception:
        _fail()
    if type(raw) is not str:
        _fail()
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        _fail()
    if (
        not raw
        or not encoded
        or len(encoded) > _MAX_RELATIVE_PATH_BYTES + 1
        or "\0" in raw
        or not os.path.isabs(raw)
        or os.path.normpath(raw) != raw
        or os.path.realpath(raw) != raw
    ):
        _fail()
    components = tuple(
        component for component in Path(raw).parts if component != "/"
    )
    if (
        len(components) > _MAX_TREE_DEPTH
        or any(
            not component
            or len(component.encode("utf-8")) > _MAX_COMPONENT_BYTES
            or unicodedata.normalize("NFC", component) != component
            or any(
                unicodedata.category(character) in ("Cc", "Cf", "Cs")
                for character in component
            )
            for component in components
        )
    ):
        _fail()
    return Path(raw)


@dataclass(frozen=True)
class ExperimentPreflightRequest:
    experiment_input: CanonicalExperimentInput
    bundle_root: Path = field(repr=False)
    skill_repo: Path = field(repr=False)
    temp_parent: Path = field(repr=False)
    task_sources: Mapping[str, TaskSourceSpec] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not _exact_fields(self.experiment_input, CanonicalExperimentInput)
            or not isinstance(self.task_sources, ABCMapping)
        ):
            _fail()
        try:
            detached_sources = dict(self.task_sources)
        except Exception:
            _fail()
        if (
            any(type(key) is not str for key in detached_sources)
            or any(
                not _exact_fields(value, TaskSourceSpec)
                for value in detached_sources.values()
            )
        ):
            _fail()
        object.__setattr__(
            self,
            "bundle_root",
            _detached_exact_absolute_path(self.bundle_root),
        )
        object.__setattr__(
            self,
            "skill_repo",
            _detached_exact_absolute_path(self.skill_repo),
        )
        object.__setattr__(
            self,
            "temp_parent",
            _detached_exact_absolute_path(self.temp_parent),
        )
        object.__setattr__(
            self,
            "task_sources",
            MappingProxyType(detached_sources),
        )


def _exact_fields(value: object, expected_type: type) -> bool:
    return type(value) is expected_type and set(vars(value)) == {
        item.name for item in fields(expected_type)
    }


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and _DIGEST_PATTERN.fullmatch(value) is not None
    )


@dataclass(frozen=True)
class ExperimentPreflightResult:
    status: str
    live_backend_state: str
    global_agents_marker_state: str
    pilot_state: str
    qualification_evidence_classification: str
    model_calls: int
    materialization_result: str
    plan_digest: Optional[str]
    task_corpus_receipt_digest: Optional[str]
    preflight_receipt_digest: Optional[str]
    bundle_digest: Optional[str]
    current_profile_digest: Optional[str]
    lean_profile_digest: Optional[str]
    cleanup_state: str
    reason_code: str

    def __post_init__(self) -> None:
        _validate_preflight_result(self)


def _validate_preflight_result(result: ExperimentPreflightResult) -> None:
    if not _exact_fields(result, ExperimentPreflightResult):
        _fail()
    if (
        type(result.status) is not str
        or result.live_backend_state != "live_backend_not_implemented"
        or type(result.live_backend_state) is not str
        or result.global_agents_marker_state
        != "global_agents_marker_not_run"
        or type(result.global_agents_marker_state) is not str
        or result.pilot_state != "pilot_not_run"
        or type(result.pilot_state) is not str
        or type(result.qualification_evidence_classification) is not str
        or type(result.model_calls) is not int
        or result.model_calls != 0
        or type(result.materialization_result) is not str
        or type(result.cleanup_state) is not str
        or type(result.reason_code) is not str
        or result.reason_code not in _RESULT_REASONS
    ):
        _fail()
    mask_bits = []
    for name in _RESULT_DIGEST_FIELDS:
        value = getattr(result, name)
        if value is None:
            mask_bits.append("0")
        elif _is_digest(value):
            mask_bits.append("1")
        else:
            _fail()
    mask = "".join(mask_bits)
    if mask not in _RESULT_MASKS:
        _fail()
    if mask != "000000" and result.cleanup_state == "not_started":
        _fail()
    qualified = mask in {"111100", "111110", "111111"}
    expected_qualification = (
        "operator_attested_static" if qualified else "not_validated"
    )
    if (
        result.qualification_evidence_classification
        != expected_qualification
    ):
        _fail()
    if mask == "111111":
        if (
            result.status != "static_only"
            or result.materialization_result != "verified"
            or result.cleanup_state != "removed"
            or result.reason_code != "static_preflight_verified"
        ):
            _fail()
        return
    if (
        result.status != "blocked"
        or result.materialization_result != "blocked"
        or result.preflight_receipt_digest is not None
        or result.cleanup_state
        not in ("not_started", "removed", "cleanup_required")
        or result.reason_code == "static_preflight_verified"
    ):
        _fail()
    if (
        result.cleanup_state == "cleanup_required"
    ) != (
        result.reason_code == "task_snapshot_cleanup_required"
    ):
        _fail()
    if mask == "111110" and result.cleanup_state != "cleanup_required":
        _fail()


def _base_profile_document(
    manifest: HarnessManifest,
) -> Mapping[str, object]:
    if (
        not _exact_fields(manifest, HarnessManifest)
        or not _exact_fields(manifest.checkout, CheckoutManifest)
    ):
        _fail("harness_preflight_blocked")
    checkout = manifest.checkout
    if (
        checkout.object_format not in _OID_PATTERNS
        or type(checkout.object_format) is not str
        or type(checkout.skill_names) is not tuple
        or not checkout.skill_names
        or any(type(name) is not str or not name for name in checkout.skill_names)
        or len(set(checkout.skill_names)) != len(checkout.skill_names)
        or not isinstance(checkout.skill_hashes, ABCMapping)
        or not isinstance(checkout.materialized_hashes, ABCMapping)
        or set(checkout.skill_hashes) != set(checkout.skill_names)
        or set(checkout.materialized_hashes) != set(checkout.skill_names)
        or _OID_PATTERNS[checkout.object_format].fullmatch(
            checkout.tree_hash
        )
        is None
        or _OID_PATTERNS[checkout.object_format].fullmatch(
            checkout.plugin_blob_oid
        )
        is None
    ):
        _fail("harness_preflight_blocked")
    digest_values = (
        manifest.bundle_digest,
        manifest.agents_hash,
        manifest.skill_routing_hash,
        manifest.adapter_source_hash,
        manifest.adapter_materialized_hash,
        manifest.common_role_hash,
        manifest.home_digest,
        checkout.plugin_manifest_hash,
        *tuple(checkout.skill_hashes.values()),
        *tuple(checkout.materialized_hashes.values()),
    )
    if (
        type(manifest.bundle_id) is not str
        or not manifest.bundle_id
        or type(manifest.profile) is not str
        or manifest.profile not in ("current", "lean")
        or type(manifest.adapter_count) is not int
        or isinstance(manifest.adapter_count, bool)
        or manifest.adapter_count != 16
        or type(manifest.role_count) is not int
        or isinstance(manifest.role_count, bool)
        or manifest.role_count != 16
        or any(not _is_digest(value) for value in digest_values)
        or any(type(name) is not str for name in checkout.skill_hashes)
    ):
        _fail("harness_preflight_blocked")
    return {
        "adapter_count": manifest.adapter_count,
        "adapter_materialized_hash": manifest.adapter_materialized_hash,
        "adapter_source_hash": manifest.adapter_source_hash,
        "agents_hash": manifest.agents_hash,
        "bundle_digest": manifest.bundle_digest,
        "bundle_id": manifest.bundle_id,
        "checkout": {
            "materialized_hashes": dict(checkout.materialized_hashes),
            "object_format": checkout.object_format,
            "plugin_blob_oid": checkout.plugin_blob_oid,
            "plugin_manifest_hash": checkout.plugin_manifest_hash,
            "skill_hashes": dict(checkout.skill_hashes),
            "skill_names": list(checkout.skill_names),
            "tree_hash": checkout.tree_hash,
        },
        "common_role_hash": manifest.common_role_hash,
        "document_type": "harness-experiment-base-profile-v1",
        "home_digest": manifest.home_digest,
        "profile": manifest.profile,
        "role_count": manifest.role_count,
        "schema_version": 1,
        "skill_routing_hash": manifest.skill_routing_hash,
    }


def _base_profile_digest(manifest: HarnessManifest) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_bytes(_base_profile_document(manifest))
    ).hexdigest()


def _require_base_profile_pair(
    current: HarnessManifest,
    lean: HarnessManifest,
) -> Tuple[str, str]:
    current_document = _base_profile_document(current)
    lean_document = _base_profile_document(lean)
    shared_fields = (
        "bundle_id",
        "bundle_digest",
        "checkout",
        "skill_routing_hash",
        "adapter_source_hash",
        "adapter_materialized_hash",
        "common_role_hash",
        "adapter_count",
        "role_count",
    )
    if (
        current.profile != "current"
        or lean.profile != "lean"
        or any(
            current_document[name] != lean_document[name]
            for name in shared_fields
        )
        or current.agents_hash == lean.agents_hash
        or current.home_digest == lean.home_digest
    ):
        _fail("harness_preflight_blocked")
    current_digest = _base_profile_digest(current)
    lean_digest = _base_profile_digest(lean)
    if current_digest == lean_digest:
        _fail("harness_preflight_blocked")
    return current_digest, lean_digest


def _path_components(path: object) -> Tuple[str, ...]:
    if type(path) is not str:
        _fail("task_snapshot_preflight_blocked")
    try:
        encoded = path.encode("utf-8")
    except UnicodeError:
        _fail("task_snapshot_preflight_blocked")
    if (
        not path
        or len(encoded) > _MAX_RELATIVE_PATH_BYTES
        or path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or ":" in path
        or "\0" in path
        or unicodedata.normalize("NFC", path) != path
        or any(
            unicodedata.category(character) in ("Cc", "Cf", "Cs")
            for character in path
        )
    ):
        _fail("task_snapshot_preflight_blocked")
    components = tuple(path.split("/"))
    if (
        len(components) > _MAX_TREE_DEPTH
        or any(
            not component
            or component in (".", "..")
            or component.endswith(".")
            or component.endswith(" ")
            or len(component.encode("utf-8")) > _MAX_COMPONENT_BYTES
            for component in components
        )
    ):
        _fail("task_snapshot_preflight_blocked")
    keys = tuple(
        unicodedata.normalize("NFC", component).casefold()
        for component in components
    )
    if (
        ".git" in keys
        or keys[-1]
        in {
            "agents.md",
            "agents.override.md",
            ".mcp.json",
            "mcp.json",
            ".env",
            ".env.local",
            ".npmrc",
            ".pypirc",
            "credentials.json",
            "secrets.json",
        }
        or any(
            key in {".codex", ".agents", ".claude", ".mcp"}
            for key in keys[:-1]
        )
        or (
            len(keys) > 1
            and keys[0] in {".git-hooks", "hooks", "plugins"}
        )
    ):
        _fail("task_snapshot_preflight_blocked")
    return components


def _classify_allowed_write_paths(
    paths: Sequence[str],
    entries: Sequence[TaskTreeEntry],
) -> Tuple[Mapping[str, str], ...]:
    if (
        not isinstance(paths, ABCSequence)
        or isinstance(paths, (str, bytes, bytearray))
        or not isinstance(entries, ABCSequence)
        or isinstance(entries, (str, bytes, bytearray))
    ):
        _fail("task_snapshot_preflight_blocked")
    try:
        checked_paths = tuple(paths)
        sorted_paths = tuple(
            sorted(
                checked_paths,
                key=lambda value: value.encode("utf-8"),
            )
        )
    except (AttributeError, TypeError, UnicodeError):
        _fail("task_snapshot_preflight_blocked")
    if checked_paths != sorted_paths:
        _fail("task_snapshot_preflight_blocked")
    path_aliases = [
        unicodedata.normalize("NFC", path).casefold()
        if type(path) is str
        else None
        for path in checked_paths
    ]
    if len(set(path_aliases)) != len(path_aliases):
        _fail("task_snapshot_preflight_blocked")

    files = set()
    directories = {"."}
    children = {}
    for entry in tuple(entries):
        if not _exact_fields(entry, TaskTreeEntry):
            _fail("task_snapshot_preflight_blocked")
        components = _path_components(entry.path)
        current = []
        for index, component in enumerate(components):
            parent = "/".join(current) or "."
            child_map = children.setdefault(parent, {})
            key = unicodedata.normalize("NFC", component).casefold()
            previous = child_map.get(key)
            if previous is not None and previous != component:
                _fail("task_snapshot_preflight_blocked")
            child_map[key] = component
            current.append(component)
            joined = "/".join(current)
            if index == len(components) - 1:
                if joined in directories or joined in files:
                    _fail("task_snapshot_preflight_blocked")
                files.add(joined)
            else:
                if joined in files:
                    _fail("task_snapshot_preflight_blocked")
                directories.add(joined)

    records = []
    for path in checked_paths:
        components = _path_components(path)
        current = []
        for index, component in enumerate(components):
            parent = "/".join(current) or "."
            key = unicodedata.normalize("NFC", component).casefold()
            canonical_sibling = children.get(parent, {}).get(key)
            is_final = index == len(components) - 1
            if canonical_sibling is not None and canonical_sibling != component:
                _fail("task_snapshot_preflight_blocked")
            current.append(component)
            joined = "/".join(current)
            if is_final:
                if joined in files:
                    disposition = "existing_regular_file"
                elif joined in directories:
                    _fail("task_snapshot_preflight_blocked")
                elif parent not in directories:
                    _fail("task_snapshot_preflight_blocked")
                else:
                    disposition = "missing_leaf"
            elif joined not in directories:
                _fail("task_snapshot_preflight_blocked")
        records.append({"disposition": disposition, "path": path})
    return tuple(records)


def _allowed_write_policy_digest(
    *,
    task_id: str,
    materialized_tree_digest: str,
    snapshot_receipt_digest: str,
    records: Sequence[Mapping[str, str]],
) -> str:
    if (
        type(task_id) is not str
        or not task_id
        or not _is_digest(materialized_tree_digest)
        or not _is_digest(snapshot_receipt_digest)
        or not isinstance(records, ABCSequence)
        or isinstance(records, (str, bytes, bytearray))
    ):
        _fail("task_snapshot_preflight_blocked")
    detached_records = []
    for record in tuple(records):
        if (
            not isinstance(record, ABCMapping)
            or set(record) != {"disposition", "path"}
            or record["disposition"]
            not in ("existing_regular_file", "missing_leaf")
            or type(record["path"]) is not str
        ):
            _fail("task_snapshot_preflight_blocked")
        detached_records.append(
            {
                "disposition": record["disposition"],
                "path": record["path"],
            }
        )
    document = {
        "allowed_write_paths": detached_records,
        "document_type": "task-allowed-write-policy-v1",
        "materialized_tree_digest": materialized_tree_digest,
        "schema_version": 1,
        "snapshot_receipt_digest": snapshot_receipt_digest,
        "task_id": task_id,
    }
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


@dataclass(frozen=True)
class _StableToken:
    device: int
    inode: int
    uid: int
    gid: int
    kind: str


@dataclass(frozen=True)
class _OwnedEntry:
    path: Tuple[str, ...]
    token: _StableToken
    mode: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    children: Tuple[str, ...]


@dataclass(frozen=True)
class _HandoffPin:
    path: Tuple[str, ...]
    entry: _OwnedEntry
    owner: "_ResourceOwner" = field(repr=False, compare=False)


@dataclass(eq=False)
class _HandoffCell:
    serial: int
    prefixes: Tuple[Tuple[str, ...], ...]
    pins: Tuple[_HandoffPin, ...] = field(repr=False)
    phase: str = field(default="acquiring", repr=False)


@dataclass(frozen=True)
class _LedgerState:
    entries: Mapping[Tuple[str, ...], _OwnedEntry]
    pending: Tuple[_HandoffCell, ...]


class _ResourceOwner:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._close_started = False
        self._close_uncertain = False

    @property
    def descriptor(self) -> int:
        if self._descriptor < 0 or self._close_started:
            _fail()
        return self._descriptor

    @property
    def close_uncertain(self) -> bool:
        return self._close_uncertain

    @property
    def close_started(self) -> bool:
        return self._close_started

    def owns_descriptor(self, descriptor: int) -> bool:
        return (
            not self._close_started
            and self._descriptor >= 0
            and self._descriptor == descriptor
        )

    def close(self) -> None:
        if self._descriptor < 0:
            return
        if self._close_started:
            self._close_uncertain = True
            _fail()
        self._close_started = True
        descriptor = self._descriptor
        try:
            os.close(descriptor)
        except BaseException:
            self._close_uncertain = True
            raise
        else:
            self._descriptor = -1


class _PhysicalDirectoryGate:
    def __init__(
        self,
        path: Path,
        owner: _ResourceOwner,
        terminal_entry: _OwnedEntry,
        ancestor_identities: Tuple[
            Tuple[int, int, int, int, str, int], ...
        ],
    ) -> None:
        self.path = path
        self._owner = owner
        self.terminal_entry = terminal_entry
        self.ancestor_identities = ancestor_identities

    @property
    def descriptor(self) -> int:
        return self._owner.descriptor

    @property
    def terminal_identity(self) -> Tuple[int, int]:
        return (
            self.terminal_entry.token.device,
            self.terminal_entry.token.inode,
        )

    def require_live(self) -> None:
        candidate_descriptor = -1
        try:
            descriptor_entry = _entry_from_stat(
                (), os.fstat(self.descriptor), ()
            )
            (
                candidate_descriptor,
                candidate_identities,
                candidate_entry,
            ) = _open_absolute_directory_chain(self.path)
            if (
                descriptor_entry.token != self.terminal_entry.token
                or candidate_entry.token != self.terminal_entry.token
                or descriptor_entry.mode != 0o700
                or candidate_entry.mode != 0o700
                or candidate_identities != self.ancestor_identities
            ):
                _fail()
        except ExperimentPreflightError:
            raise
        except (OSError, TypeError, ValueError):
            _fail()
        finally:
            if candidate_descriptor >= 0:
                try:
                    _close_descriptor(candidate_descriptor)
                except OSError:
                    _fail()

    def close(self) -> None:
        self._owner.close()


class _OwnershipLedger:
    def __init__(
        self,
        gate: _PhysicalDirectoryGate,
        entries: Mapping[Tuple[str, ...], _OwnedEntry],
    ) -> None:
        self.gate = gate
        self._state = _LedgerState(
            entries=MappingProxyType(dict(entries)),
            pending=(),
        )
        self._next_serial = 1
        self._removed = False

    @property
    def entries(self) -> Mapping[Tuple[str, ...], _OwnedEntry]:
        return self._state.entries

    @property
    def has_pending_handoffs(self) -> bool:
        return bool(self._state.pending)


def _kind(metadata: os.stat_result) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    _fail("task_snapshot_preflight_blocked")


def _entry_from_stat(
    path: Tuple[str, ...],
    metadata: os.stat_result,
    children: Tuple[str, ...],
) -> _OwnedEntry:
    return _OwnedEntry(
        path=path,
        token=_StableToken(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            uid=metadata.st_uid,
            gid=metadata.st_gid,
            kind=_kind(metadata),
        ),
        mode=stat.S_IMODE(metadata.st_mode),
        nlink=metadata.st_nlink,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        children=children,
    )


def _safe_component(component: object) -> str:
    if type(component) is not str:
        _fail("task_snapshot_preflight_blocked")
    try:
        encoded = component.encode("utf-8")
    except UnicodeError:
        _fail("task_snapshot_preflight_blocked")
    if (
        not component
        or component in (".", "..")
        or "/" in component
        or "\0" in component
        or len(encoded) > _MAX_COMPONENT_BYTES
        or unicodedata.normalize("NFC", component) != component
        or any(
            unicodedata.category(character) in ("Cc", "Cf", "Cs")
            for character in component
        )
    ):
        _fail("task_snapshot_preflight_blocked")
    return component


def _validate_owned_path(path: Tuple[str, ...]) -> None:
    if type(path) is not tuple or len(path) > _MAX_TREE_DEPTH:
        _fail("task_snapshot_preflight_blocked")
    for component in path:
        _safe_component(component)
    try:
        encoded = "/".join(path).encode("utf-8")
    except UnicodeError:
        _fail("task_snapshot_preflight_blocked")
    if len(encoded) > _MAX_RELATIVE_PATH_BYTES:
        _fail("task_snapshot_preflight_blocked")


def _open_relative_directory(
    root_descriptor: int,
    components: Tuple[str, ...],
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for component in components:
            _safe_component(component)
            next_descriptor = os.open(
                component,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=descriptor,
            )
            previous_descriptor = descriptor
            descriptor = next_descriptor
            _close_descriptor(previous_descriptor)
        return descriptor
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _bounded_directory_names(
    descriptor: int,
    *,
    maximum: int = _MAX_OWNED_LEDGER_ENTRIES,
) -> Tuple[str, ...]:
    names = []
    try:
        with os.scandir(descriptor) as iterator:
            for item in iterator:
                names.append(item.name)
                if len(names) > maximum:
                    _fail("task_snapshot_preflight_blocked")
    except ExperimentPreflightError:
        raise
    except (OSError, TypeError):
        _fail("task_snapshot_preflight_blocked")
    try:
        return tuple(sorted(names, key=lambda name: name.encode("utf-8")))
    except (TypeError, UnicodeError):
        _fail("task_snapshot_preflight_blocked")


def _directory_is_empty(
    descriptor: int,
    *,
    reason: str = "task_snapshot_preflight_blocked",
) -> bool:
    try:
        with os.scandir(descriptor) as iterator:
            return next(iterator, None) is None
    except (OSError, TypeError):
        _fail(reason)


def _scan_directory_tree(
    descriptor: int,
    *,
    prefix: Tuple[str, ...],
    expected_device: int,
    entries: dict,
    identities: set,
) -> None:
    _validate_owned_path(prefix)
    try:
        first = os.fstat(descriptor)
        names = _bounded_directory_names(
            descriptor,
            maximum=_MAX_OWNED_LEDGER_ENTRIES - len(entries),
        )
    except (OSError, TypeError, UnicodeError):
        _fail("task_snapshot_preflight_blocked")
    aliases = set()
    for name in names:
        _safe_component(name)
        alias = unicodedata.normalize("NFC", name).casefold()
        if alias in aliases:
            _fail("task_snapshot_preflight_blocked")
        aliases.add(alias)
    root_entry = _entry_from_stat(prefix, first, names)
    if (
        root_entry.token.kind != "directory"
        or root_entry.token.device != expected_device
        or root_entry.token.uid != os.getuid()
    ):
        _fail("task_snapshot_preflight_blocked")
    identity = (
        root_entry.token.device,
        root_entry.token.inode,
    )
    if identity in identities:
        _fail("task_snapshot_preflight_blocked")
    identities.add(identity)
    entries[prefix] = root_entry
    if len(entries) > _MAX_OWNED_LEDGER_ENTRIES:
        _fail("task_snapshot_preflight_blocked")

    for name in names:
        child_path = prefix + (name,)
        _validate_owned_path(child_path)
        try:
            metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except OSError:
            _fail("task_snapshot_preflight_blocked")
        child_kind = _kind(metadata)
        if (
            metadata.st_dev != expected_device
            or metadata.st_uid != os.getuid()
        ):
            _fail("task_snapshot_preflight_blocked")
        if child_kind == "directory":
            try:
                child_descriptor = os.open(
                    name,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
            except OSError:
                _fail("task_snapshot_preflight_blocked")
            try:
                opened = os.fstat(child_descriptor)
                if (
                    opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    _fail("task_snapshot_preflight_blocked")
                _scan_directory_tree(
                    child_descriptor,
                    prefix=child_path,
                    expected_device=expected_device,
                    entries=entries,
                    identities=identities,
                )
            finally:
                try:
                    _close_descriptor(child_descriptor)
                except OSError:
                    _fail("task_snapshot_preflight_blocked")
        else:
            entry = _entry_from_stat(child_path, metadata, ())
            identity = (entry.token.device, entry.token.inode)
            if entry.nlink != 1 or identity in identities:
                _fail("task_snapshot_preflight_blocked")
            identities.add(identity)
            entries[child_path] = entry
            if len(entries) > _MAX_OWNED_LEDGER_ENTRIES:
                _fail("task_snapshot_preflight_blocked")
    try:
        second = os.fstat(descriptor)
        second_names = _bounded_directory_names(
            descriptor,
            maximum=_MAX_OWNED_LEDGER_ENTRIES,
        )
    except (OSError, TypeError, UnicodeError):
        _fail("task_snapshot_preflight_blocked")
    if _entry_from_stat(prefix, second, second_names) != root_entry:
        _fail("task_snapshot_preflight_blocked")


def _scan_phase_tree(
    gate: _PhysicalDirectoryGate,
) -> Mapping[Tuple[str, ...], _OwnedEntry]:
    gate.require_live()
    try:
        descriptor = os.open(
            "phase-a",
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=gate.descriptor,
        )
    except OSError:
        _fail("task_snapshot_preflight_blocked")
    try:
        root_metadata = os.fstat(descriptor)
        entries = {}
        _scan_directory_tree(
            descriptor,
            prefix=(),
            expected_device=root_metadata.st_dev,
            entries=entries,
            identities=set(),
        )
        return MappingProxyType(entries)
    finally:
        try:
            _close_descriptor(descriptor)
        except OSError:
            _fail("task_snapshot_preflight_blocked")


def _stable_directory_identity(
    metadata: os.stat_result,
) -> Tuple[int, int, int, int, str, int]:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        _fail()
    values = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
    )
    if any(type(value) is not int or value < 0 for value in values):
        _fail()
    return values + ("directory", stat.S_IMODE(metadata.st_mode))


def _open_absolute_directory_chain(
    path: Path,
) -> Tuple[
    int,
    Tuple[Tuple[int, int, int, int, str, int], ...],
    _OwnedEntry,
]:
    exact = _detached_exact_absolute_path(path)
    components = tuple(component for component in exact.parts if component != "/")
    descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    identities = []
    try:
        current_metadata = os.fstat(descriptor)
        root_entry = _entry_from_stat((), current_metadata, ())
        if root_entry.token.kind != "directory":
            _fail()
        identities.append(_stable_directory_identity(current_metadata))
        for component in components:
            _safe_component(component)
            try:
                observed = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                _fail()
            _require_trusted_directory_ancestor(
                current_metadata,
                observed,
            )
            next_descriptor = -1
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=descriptor,
                )
                next_metadata = os.fstat(next_descriptor)
                if (
                    _stable_directory_identity(observed)
                    != _stable_directory_identity(next_metadata)
                    or _stable_directory_identity(current_metadata)
                    != _stable_directory_identity(
                        os.fstat(descriptor)
                    )
                ):
                    _fail()
                identities.append(
                    _stable_directory_identity(next_metadata)
                )
            except BaseException:
                if next_descriptor >= 0:
                    _close_descriptor(next_descriptor)
                raise
            previous_descriptor = descriptor
            descriptor = next_descriptor
            _close_descriptor(previous_descriptor)
            current_metadata = next_metadata
        final_metadata = os.fstat(descriptor)
        if (
            _entry_from_stat((), current_metadata, ())
            != _entry_from_stat((), final_metadata, ())
        ):
            _fail()
        terminal = _entry_from_stat((), final_metadata, ())
        if (
            terminal.token.kind != "directory"
            or terminal.token.uid != os.getuid()
            or terminal.mode & 0o022
            or len(
                {
                    (identity[0], identity[1])
                    for identity in identities
                }
            )
            != len(identities)
        ):
            _fail()
        return descriptor, tuple(identities), terminal
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _require_trusted_directory_ancestor(
    metadata: os.stat_result,
    next_metadata: os.stat_result,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(next_metadata.st_mode)
        or stat.S_ISLNK(next_metadata.st_mode)
    ):
        _fail()
    parent = _entry_from_stat((), metadata, ())
    child = _entry_from_stat((), next_metadata, ())
    uid = os.getuid()
    if (
        parent.token.kind != "directory"
        or child.token.kind != "directory"
        or parent.token.uid not in (0, uid)
    ):
        _fail()
    if parent.mode & 0o022:
        sticky_exception = (
            parent.token.uid == 0
            and bool(metadata.st_mode & stat.S_ISVTX)
            and child.token.uid == uid
            and not child.mode & 0o022
        )
        if not sticky_exception:
            _fail()


def _open_physical_directory_gate(path: Path) -> _PhysicalDirectoryGate:
    descriptor = -1
    try:
        descriptor, identities, terminal = _open_absolute_directory_chain(path)
        owner = _ResourceOwner(descriptor)
        gate = _PhysicalDirectoryGate(
            _detached_exact_absolute_path(path),
            owner,
            terminal,
            identities,
        )
        if terminal.mode != 0o700 or not _directory_is_empty(
            descriptor, reason="experiment_preflight_invalid"
        ):
            _fail()
        gate.require_live()
        descriptor = -1
        return gate
    except ExperimentPreflightError:
        raise
    except (OSError, TypeError, ValueError):
        _fail()
    finally:
        if descriptor >= 0:
            try:
                _close_descriptor(descriptor)
            except OSError:
                _fail()


def _require_physical_disjointness(
    temp_gate: _PhysicalDirectoryGate,
    protected_roots: Sequence[Path],
) -> None:
    if (
        not isinstance(protected_roots, ABCSequence)
        or isinstance(protected_roots, (str, bytes, bytearray))
    ):
        _fail()
    temp_gate.require_live()
    for root in tuple(protected_roots):
        descriptor = -1
        try:
            descriptor, identities, terminal = (
                _open_absolute_directory_chain(root)
            )
            protected_terminal = (
                terminal.token.device,
                terminal.token.inode,
            )
            protected_identities = {
                (identity[0], identity[1])
                for identity in identities
            }
            temp_identities = {
                (identity[0], identity[1])
                for identity in temp_gate.ancestor_identities
            }
            if (
                temp_gate.terminal_identity in protected_identities
                or protected_terminal in temp_identities
            ):
                _fail()
        except ExperimentPreflightError:
            raise
        except (OSError, TypeError, ValueError):
            _fail()
        finally:
            if descriptor >= 0:
                try:
                    _close_descriptor(descriptor)
                except OSError:
                    _fail()
    temp_gate.require_live()


def _create_phase_ledger(
    gate: _PhysicalDirectoryGate,
) -> _OwnershipLedger:
    gate.require_live()
    created = False
    try:
        os.mkdir("phase-a", mode=0o700, dir_fd=gate.descriptor)
        created = True
        descriptor = os.open(
            "phase-a",
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=gate.descriptor,
        )
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            _close_descriptor(descriptor)
        scanned = _scan_phase_tree(gate)
        if set(scanned) != {()} or scanned[()].children:
            _fail("task_snapshot_cleanup_required")
        return _OwnershipLedger(gate, scanned)
    except BaseException as caught:
        if not isinstance(caught, Exception):
            raise
        if created:
            _fail("task_snapshot_cleanup_required")
        if isinstance(caught, ExperimentPreflightError):
            raise
        _fail()


def _begin_handoff(
    ledger: _OwnershipLedger,
    prefixes: Sequence[Tuple[str, ...]],
) -> _HandoffCell:
    if (
        type(ledger) is not _OwnershipLedger
        or ledger._removed
        or ledger.has_pending_handoffs
        or not isinstance(prefixes, ABCSequence)
        or isinstance(prefixes, (str, bytes, bytearray))
    ):
        _fail("task_snapshot_preflight_blocked")
    checked = tuple(prefixes)
    if not checked or len(checked) > _MAX_HANDOFF_PREFIXES:
        _fail("task_snapshot_preflight_blocked")
    for prefix in checked:
        _validate_owned_path(prefix)
    if len(set(checked)) != len(checked):
        _fail("task_snapshot_preflight_blocked")

    boundary_paths = set()
    for prefix in checked:
        if (
            prefix in ledger.entries
            and ledger.entries[prefix].token.kind != "directory"
        ):
            _fail("task_snapshot_preflight_blocked")
        boundary = None
        for length in range(len(prefix), -1, -1):
            candidate = prefix[:length]
            entry = ledger.entries.get(candidate)
            if entry is not None and entry.token.kind == "directory":
                boundary = candidate
                break
        if boundary is None:
            _fail("task_snapshot_preflight_blocked")
        boundary_paths.add(boundary)

    pins = []
    raw_descriptor = -1
    current_owner = None
    next_serial = ledger._next_serial + 1
    try:
        for path in sorted(boundary_paths, key=lambda item: (len(item), item)):
            raw_descriptor = _open_relative_directory(
                ledger.gate.descriptor,
                ("phase-a",) + path,
            )
            current_owner = _ResourceOwner(raw_descriptor)
            raw_descriptor = -1
            expected = ledger.entries[path]
            observed = _entry_from_stat(
                path,
                os.fstat(current_owner.descriptor),
                expected.children,
            )
            if observed != expected:
                _fail("task_snapshot_preflight_blocked")
            pin = _HandoffPin(path, expected, current_owner)
            pins.append(pin)
            current_owner = None
        cell = _HandoffCell(
            ledger._next_serial,
            checked,
            tuple(pins),
        )
        next_state = _LedgerState(
            entries=ledger.entries,
            pending=ledger._state.pending + (cell,),
        )
    except BaseException as caught:
        unused_released, close_failure = _drain_pin_owners(
            pins,
            extra_owner=current_owner,
            raw_descriptor=raw_descriptor,
        )
        if close_failure is not None and isinstance(caught, Exception):
            raise close_failure
        if not isinstance(caught, Exception):
            raise
        if isinstance(caught, ExperimentPreflightError):
            raise
        _fail("task_snapshot_preflight_blocked")

    ledger._next_serial = next_serial
    ledger._state = next_state
    return cell


def _path_under(
    path: Tuple[str, ...],
    prefix: Tuple[str, ...],
) -> bool:
    return len(path) >= len(prefix) and path[: len(prefix)] == prefix


def _drain_pin_owners(
    pins: Sequence[_HandoffPin],
    *,
    extra_owner: Optional[_ResourceOwner] = None,
    raw_descriptor: int = -1,
) -> Tuple[bool, Optional[BaseException]]:
    failure = None
    uncertain = False
    if (
        raw_descriptor >= 0
        and extra_owner is not None
        and extra_owner.owns_descriptor(raw_descriptor)
    ):
        raw_descriptor = -1
    if raw_descriptor >= 0:
        try:
            os.close(raw_descriptor)
        except BaseException as caught:
            uncertain = True
            failure = caught

    if extra_owner is not None:
        if extra_owner.close_started:
            uncertain = uncertain or extra_owner.close_uncertain
        else:
            try:
                extra_owner.close()
            except BaseException as caught:
                uncertain = True
                if failure is None or (
                    isinstance(failure, Exception)
                    and not isinstance(caught, Exception)
                ):
                    failure = caught

    index = len(pins) - 1
    while index >= 0:
        owner = pins[index].owner
        if owner.close_started:
            uncertain = uncertain or owner.close_uncertain
            index -= 1
            continue
        try:
            owner.close()
        except BaseException as caught:
            uncertain = True
            if failure is None or (
                isinstance(failure, Exception)
                and not isinstance(caught, Exception)
            ):
                failure = caught
        index -= 1
    return not uncertain, failure


def _release_handoff_pins(
    handoff: _HandoffCell,
    *,
    terminal_phase: str,
) -> bool:
    if (
        type(handoff) is not _HandoffCell
        or terminal_phase not in ("released", "quarantined")
    ):
        _fail("task_snapshot_preflight_blocked")
    if handoff.phase == "close_uncertain":
        return False
    if handoff.phase in ("released", "quarantined"):
        return True
    if handoff.phase not in ("acquiring", "published"):
        _fail("task_snapshot_preflight_blocked")

    active = sys.exc_info()[1]
    released, failure = _drain_pin_owners(handoff.pins)
    handoff.phase = (
        terminal_phase if released else "close_uncertain"
    )
    if failure is not None and not (
        active is not None and not isinstance(active, Exception)
    ):
        raise failure
    return released


def _abandon_handoff(handoff: _HandoffCell) -> None:
    if type(handoff) is not _HandoffCell:
        _fail("task_snapshot_preflight_blocked")
    if handoff.phase in ("acquiring", "published"):
        _release_handoff_pins(
            handoff,
            terminal_phase="quarantined",
        )


def _validate_handoff_pins(
    handoff: _HandoffCell,
    scanned: Mapping[Tuple[str, ...], _OwnedEntry],
) -> None:
    if handoff.phase != "acquiring" or not handoff.pins:
        _fail("task_snapshot_preflight_blocked")
    for pin in handoff.pins:
        try:
            pinned = _entry_from_stat(
                pin.path,
                os.fstat(pin.owner.descriptor),
                pin.entry.children,
            )
        except ExperimentPreflightError:
            raise
        except (OSError, TypeError, ValueError):
            _fail("task_snapshot_preflight_blocked")
        candidate = scanned.get(pin.path)
        if (
            candidate is None
            or pinned.token != pin.entry.token
            or pinned.mode != pin.entry.mode
            or candidate.token != pin.entry.token
            or candidate.mode != pin.entry.mode
        ):
            _fail("task_snapshot_preflight_blocked")


def _validate_handoff_scan(
    ledger: _OwnershipLedger,
    handoff: _HandoffCell,
    scanned: Mapping[Tuple[str, ...], _OwnedEntry],
) -> None:
    if handoff not in ledger._state.pending:
        _fail("task_snapshot_preflight_blocked")
    _validate_handoff_pins(handoff, scanned)
    previous = ledger.entries
    if any(path not in scanned for path in previous):
        _fail("task_snapshot_preflight_blocked")
    boundary_paths = {pin.path for pin in handoff.pins}
    for path, entry in previous.items():
        candidate = scanned[path]
        if candidate == entry:
            continue
        if (
            path not in boundary_paths
            or candidate.token != entry.token
            or candidate.mode != entry.mode
        ):
            _fail("task_snapshot_preflight_blocked")
    new_paths = set(scanned).difference(previous)
    if any(
        not any(_path_under(path, prefix) for prefix in handoff.prefixes)
        for path in new_paths
    ):
        _fail("task_snapshot_preflight_blocked")


def _complete_handoff(
    ledger: _OwnershipLedger,
    handoff: _HandoffCell,
    verifier: Callable[[], None],
    expected_inventory: Optional[
        Mapping[Tuple[str, ...], str]
    ] = None,
    expected_entries: Optional[
        Mapping[Tuple[str, ...], _OwnedEntry]
    ] = None,
) -> Mapping[Tuple[str, ...], _OwnedEntry]:
    if not callable(verifier) or handoff.phase != "acquiring":
        _fail("task_snapshot_preflight_blocked")
    try:
        first = _scan_phase_tree(ledger.gate)
        _validate_handoff_scan(ledger, handoff, first)
        _validate_expected_handoff_inventory(
            first, handoff, expected_inventory
        )
        _validate_expected_handoff_entries(
            first,
            handoff,
            expected_entries,
        )
        verifier()
        second = _scan_phase_tree(ledger.gate)
        _validate_handoff_scan(ledger, handoff, second)
        _validate_expected_handoff_inventory(
            second, handoff, expected_inventory
        )
        _validate_expected_handoff_entries(
            second,
            handoff,
            expected_entries,
        )
        if first != second:
            _fail("task_snapshot_preflight_blocked")
    except BaseException:
        _release_handoff_pins(
            handoff,
            terminal_phase="quarantined",
        )
        raise

    try:
        handoff.phase = "published"
        ledger._state = _LedgerState(
            entries=MappingProxyType(dict(second)),
            pending=ledger._state.pending,
        )
        if not _release_handoff_pins(
            handoff,
            terminal_phase="released",
        ):
            _fail("task_snapshot_preflight_blocked")
        ledger._state = _LedgerState(
            entries=ledger.entries,
            pending=tuple(
                cell
                for cell in ledger._state.pending
                if cell is not handoff
            ),
        )
    except BaseException:
        _abandon_handoff(handoff)
        raise
    return ledger.entries


def _validate_expected_handoff_entries(
    scanned: Mapping[Tuple[str, ...], _OwnedEntry],
    handoff: _HandoffCell,
    expected_entries: Optional[
        Mapping[Tuple[str, ...], _OwnedEntry]
    ],
) -> None:
    if expected_entries is None:
        return
    if not isinstance(expected_entries, ABCMapping):
        _fail("task_snapshot_preflight_blocked")
    try:
        expected = dict(expected_entries)
    except Exception:
        _fail("task_snapshot_preflight_blocked")
    if any(
        type(path) is not tuple
        or not _exact_fields(entry, _OwnedEntry)
        or entry.path != path
        or not any(
            _path_under(path, prefix)
            for prefix in handoff.prefixes
        )
        for path, entry in expected.items()
    ):
        _fail("task_snapshot_preflight_blocked")
    if any(scanned.get(path) != entry for path, entry in expected.items()):
        _fail("task_snapshot_preflight_blocked")


def _validate_expected_handoff_inventory(
    scanned: Mapping[Tuple[str, ...], _OwnedEntry],
    handoff: _HandoffCell,
    expected_inventory: Optional[
        Mapping[Tuple[str, ...], str]
    ],
) -> None:
    if expected_inventory is None:
        return
    if not isinstance(expected_inventory, ABCMapping):
        _fail("task_snapshot_preflight_blocked")
    try:
        expected = dict(expected_inventory)
    except Exception:
        _fail("task_snapshot_preflight_blocked")
    if any(
        type(path) is not tuple
        or kind not in ("directory", "file")
        for path, kind in expected.items()
    ):
        _fail("task_snapshot_preflight_blocked")
    actual = {
        path: entry.token.kind
        for path, entry in scanned.items()
        if any(
            _path_under(path, prefix)
            for prefix in handoff.prefixes
        )
    }
    if actual != expected:
        _fail("task_snapshot_preflight_blocked")


def _resolve_empty_handoff(
    ledger: _OwnershipLedger,
    handoff: _HandoffCell,
) -> bool:
    if (
        type(ledger) is not _OwnershipLedger
        or type(handoff) is not _HandoffCell
        or handoff.phase != "acquiring"
        or handoff not in ledger._state.pending
    ):
        return False
    try:
        scanned = _scan_phase_tree(ledger.gate)
        _validate_handoff_scan(ledger, handoff, scanned)
        if scanned != ledger.entries:
            _fail("task_snapshot_preflight_blocked")
        if not _release_handoff_pins(
            handoff,
            terminal_phase="released",
        ):
            return False
        ledger._state = _LedgerState(
            entries=ledger.entries,
            pending=tuple(
                cell for cell in ledger._state.pending if cell is not handoff
            ),
        )
        return True
    except BaseException as caught:
        try:
            _abandon_handoff(handoff)
        except BaseException as close_error:
            if not isinstance(close_error, Exception):
                raise
            if not isinstance(caught, Exception):
                raise caught
            return False
        if not isinstance(caught, Exception):
            raise
        return False


def _create_owned_directory(
    ledger: _OwnershipLedger,
    path: Tuple[str, ...],
) -> None:
    _validate_owned_path(path)
    if not path or path in ledger.entries:
        _fail("task_snapshot_preflight_blocked")
    parent = path[:-1]
    parent_entry = ledger.entries.get(parent)
    if parent_entry is None or parent_entry.token.kind != "directory":
        _fail("task_snapshot_preflight_blocked")
    handoff = _begin_handoff(ledger, (path,))
    descriptor = -1
    child_descriptor = -1
    try:
        descriptor = _open_relative_directory(
            ledger.gate.descriptor,
            ("phase-a",) + parent,
        )
        os.mkdir(path[-1], mode=0o700, dir_fd=descriptor)
        observed = os.stat(
            path[-1],
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        child_descriptor = os.open(
            path[-1],
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=descriptor,
        )
        opened = os.fstat(child_descriptor)
        observed_entry = _entry_from_stat(path, observed, ())
        opened_entry = _entry_from_stat(path, opened, ())
        if (
            observed_entry != opened_entry
            or opened_entry.token.kind != "directory"
            or opened_entry.token.device != parent_entry.token.device
            or opened_entry.token.uid != os.getuid()
            or opened_entry.mode != 0o700
        ):
            _fail("task_snapshot_preflight_blocked")
        names = _bounded_directory_names(child_descriptor, maximum=0)
        final = os.fstat(child_descriptor)
        named = os.stat(
            path[-1],
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        expected_entry = _entry_from_stat(path, final, names)
        if (
            names
            or expected_entry != _entry_from_stat(path, named, names)
            or expected_entry != opened_entry
            or expected_entry.mode != 0o700
        ):
            _fail("task_snapshot_preflight_blocked")
        closing_child = child_descriptor
        child_descriptor = -1
        _close_descriptor(closing_child)
        _complete_handoff(
            ledger,
            handoff,
            lambda: None,
            MappingProxyType({path: "directory"}),
            MappingProxyType({path: expected_entry}),
        )
    except BaseException as caught:
        if isinstance(caught, Exception):
            _resolve_empty_handoff(ledger, handoff)
        else:
            _abandon_handoff(handoff)
        raise
    finally:
        try:
            if child_descriptor >= 0:
                closing_child = child_descriptor
                child_descriptor = -1
                _close_descriptor(closing_child)
        finally:
            if descriptor >= 0:
                closing_parent = descriptor
                descriptor = -1
                _close_descriptor(closing_parent)


def _same_stable_identity(
    metadata: os.stat_result,
    entry: _OwnedEntry,
) -> bool:
    try:
        candidate = _entry_from_stat(entry.path, metadata, entry.children)
    except ExperimentPreflightError:
        return False
    return candidate.token == entry.token


def _same_open_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_uid,
        left.st_gid,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_uid,
        right.st_gid,
        stat.S_IFMT(right.st_mode),
    )


def _stat_fingerprint(metadata: os.stat_result) -> Tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _name_is_absent(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


def _directory_removal_confirmed(
    before: os.stat_result,
    after: os.stat_result,
    entry: _OwnedEntry,
) -> bool:
    if (
        not _same_stable_identity(before, entry)
        or not _same_stable_identity(after, entry)
        or not _same_open_identity(before, after)
    ):
        return False
    if after.st_nlink == 0:
        return True
    return _stat_fingerprint(after) == _stat_fingerprint(before)


def _cleanup_owned_phase(ledger: _OwnershipLedger) -> bool:
    if (
        type(ledger) is not _OwnershipLedger
        or ledger._removed
        or ledger.has_pending_handoffs
    ):
        return False
    try:
        inspected = _scan_phase_tree(ledger.gate)
    except Exception:
        return False
    if inspected != ledger.entries:
        return False

    directories = tuple(
        sorted(
            (
                path
                for path, entry in ledger.entries.items()
                if entry.token.kind == "directory"
            ),
            key=lambda path: (len(path), path),
        )
    )
    try:
        for path in directories:
            descriptor = _open_relative_directory(
                ledger.gate.descriptor,
                ("phase-a",) + path,
            )
            try:
                entry = ledger.entries[path]
                if _entry_from_stat(
                    path,
                    os.fstat(descriptor),
                    entry.children,
                ) != entry:
                    return False
                os.fchmod(descriptor, 0o700)
            finally:
                _close_descriptor(descriptor)

        files = tuple(
            sorted(
                (
                    path
                    for path, entry in ledger.entries.items()
                    if entry.token.kind == "file"
                ),
                key=lambda path: (len(path), path),
                reverse=True,
            )
        )
        for path in files:
            parent_descriptor = _open_relative_directory(
                ledger.gate.descriptor,
                ("phase-a",) + path[:-1],
            )
            file_descriptor = -1
            try:
                metadata = os.stat(
                    path[-1],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                entry = ledger.entries[path]
                if (
                    _entry_from_stat(path, metadata, ()) != entry
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    return False
                file_descriptor = os.open(
                    path[-1],
                    _FILE_OPEN_FLAGS,
                    dir_fd=parent_descriptor,
                )
                opened = os.fstat(file_descriptor)
                named_again = os.stat(
                    path[-1],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _entry_from_stat(path, opened, ()) != entry
                    or _entry_from_stat(path, named_again, ()) != entry
                    or not _same_open_identity(opened, named_again)
                ):
                    return False
                os.unlink(path[-1], dir_fd=parent_descriptor)
                removed = os.fstat(file_descriptor)
                if (
                    not _same_stable_identity(removed, entry)
                    or not stat.S_ISREG(removed.st_mode)
                    or removed.st_nlink != 0
                    or not _name_is_absent(
                        parent_descriptor, path[-1]
                    )
                ):
                    return False
            finally:
                if file_descriptor >= 0:
                    _close_descriptor(file_descriptor)
                _close_descriptor(parent_descriptor)

        for path in reversed(directories):
            if not path:
                continue
            parent_descriptor = _open_relative_directory(
                ledger.gate.descriptor,
                ("phase-a",) + path[:-1],
            )
            child_descriptor = -1
            try:
                metadata = os.stat(
                    path[-1],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if not _same_stable_identity(
                    metadata, ledger.entries[path]
                ):
                    return False
                child_descriptor = os.open(
                    path[-1],
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=parent_descriptor,
                )
                opened = os.fstat(child_descriptor)
                if (
                    not _same_stable_identity(
                        opened, ledger.entries[path]
                    )
                    or not _same_open_identity(metadata, opened)
                ):
                    return False
                if not _directory_is_empty(child_descriptor):
                    return False
                opened_before = os.fstat(child_descriptor)
                named_before = os.stat(
                    path[-1],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not _same_stable_identity(
                        opened_before, ledger.entries[path]
                    )
                    or not _same_open_identity(
                        named_before, opened_before
                    )
                ):
                    return False
                os.rmdir(path[-1], dir_fd=parent_descriptor)
                opened_after = os.fstat(child_descriptor)
                if (
                    not _directory_removal_confirmed(
                        opened_before,
                        opened_after,
                        ledger.entries[path],
                    )
                    or not _name_is_absent(
                        parent_descriptor, path[-1]
                    )
                ):
                    return False
            finally:
                if child_descriptor >= 0:
                    _close_descriptor(child_descriptor)
                _close_descriptor(parent_descriptor)

        phase_metadata = os.stat(
            "phase-a",
            dir_fd=ledger.gate.descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_stable_identity(phase_metadata, ledger.entries[()])
        ):
            return False
        phase_descriptor = os.open(
            "phase-a",
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=ledger.gate.descriptor,
        )
        try:
            opened_phase = os.fstat(phase_descriptor)
            if (
                not _same_stable_identity(
                    opened_phase, ledger.entries[()]
                )
                or not _same_open_identity(
                    phase_metadata, opened_phase
                )
            ):
                return False
            if not _directory_is_empty(phase_descriptor):
                return False
            phase_before = os.fstat(phase_descriptor)
            named_phase_before = os.stat(
                "phase-a",
                dir_fd=ledger.gate.descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_stable_identity(
                    phase_before, ledger.entries[()]
                )
                or not _same_open_identity(
                    named_phase_before, phase_before
                )
            ):
                return False
            os.rmdir("phase-a", dir_fd=ledger.gate.descriptor)
            phase_after = os.fstat(phase_descriptor)
            if (
                not _directory_removal_confirmed(
                    phase_before,
                    phase_after,
                    ledger.entries[()],
                )
                or not _name_is_absent(
                    ledger.gate.descriptor, "phase-a"
                )
            ):
                return False
        finally:
            _close_descriptor(phase_descriptor)
        ledger.gate.require_live()
        if not _directory_is_empty(ledger.gate.descriptor):
            return False
        ledger._removed = True
        return True
    except BaseException as caught:
        if not isinstance(caught, Exception):
            raise
        return False


@dataclass(frozen=True)
class _TaskEvidence:
    task_id: str
    source_receipt: CanonicalReceipt
    snapshot_receipt: CanonicalReceipt
    snapshot_receipt_digest: str
    materialized_tree_digest: str
    allowed_write_policy_digest: str


@dataclass(frozen=True)
class _ExpectedTaskFile:
    path: str
    components: Tuple[str, ...]
    mode: int
    size: int
    content_digest: str


@dataclass(frozen=True)
class _TaskCaptureExpectation:
    source_receipt: CanonicalReceipt
    snapshot_receipt: CanonicalReceipt
    entries: Tuple[TaskTreeEntry, ...] = field(repr=False)
    directories: Tuple[Tuple[str, ...], ...] = field(repr=False)
    children: Mapping[
        Tuple[str, ...], Tuple[str, ...]
    ] = field(repr=False)
    files: Mapping[
        Tuple[str, ...], _ExpectedTaskFile
    ] = field(repr=False)
    materialized_tree_digest: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class _ObservedTaskRoot:
    materialized_tree_digest: str
    target_identity_digest: str
    file_count: int
    total_bytes: int


def _reconstruct_canonical_receipt(
    receipt: object,
    *,
    reason: str,
) -> CanonicalReceipt:
    if (
        not _exact_fields(receipt, CanonicalReceipt)
        or not isinstance(receipt.payload, ABCMapping)
    ):
        _fail(reason)
    try:
        rebuilt = make_receipt(
            receipt.receipt_type,
            receipt.input_digest,
            receipt.plan_digest,
            receipt.previous_record_hash,
            dict(receipt.payload),
        )
    except (ExperimentReceiptError, TypeError, ValueError):
        _fail(reason)
    if rebuilt != receipt:
        _fail(reason)
    return rebuilt


def _detach_task_capture_expectation(
    captured: object,
    policy: TaskSnapshotPolicy,
) -> _TaskCaptureExpectation:
    if (
        not _exact_fields(captured, CapturedTaskObjects)
        or not _exact_fields(policy, TaskSnapshotPolicy)
        or captured.policy != policy
        or type(captured.entries) is not tuple
    ):
        _fail("task_snapshot_preflight_blocked")
    try:
        entries = tuple(
            TaskTreeEntry(
                path=entry.path,
                git_mode=entry.git_mode,
                blob_oid=entry.blob_oid,
                size=entry.size,
                content_digest=entry.content_digest,
            )
            for entry in captured.entries
        )
    except (TaskSnapshotError, AttributeError, TypeError, ValueError):
        _fail("task_snapshot_preflight_blocked")
    directories = {()}
    files = {}
    total_bytes = 0
    records = []
    for entry in entries:
        components = _path_components(entry.path)
        for index in range(1, len(components)):
            directory = components[:index]
            if directory in files:
                _fail("task_snapshot_preflight_blocked")
            directories.add(directory)
        expected = _ExpectedTaskFile(
            path=entry.path,
            components=components,
            mode=0o444 if entry.git_mode == "100644" else 0o555,
            size=entry.size,
            content_digest=entry.content_digest,
        )
        if (
            components in files
            or components in directories
            or type(entry.size) is not int
            or entry.size < 0
            or entry.size > policy.max_file_bytes
            or not _is_digest(entry.content_digest)
        ):
            _fail("task_snapshot_preflight_blocked")
        files[components] = expected
        total_bytes += entry.size
        if total_bytes > policy.max_total_bytes:
            _fail("task_snapshot_preflight_blocked")
        records.append(
            {
                "content_digest": expected.content_digest,
                "kind": "file",
                "mode": expected.mode,
                "path": expected.path,
                "size": expected.size,
            }
        )
    if (
        len(entries) != captured.file_count
        or total_bytes != captured.total_bytes
        or len(directories) + len(entries)
        > policy.max_tree_entries
    ):
        _fail("task_snapshot_preflight_blocked")
    children = {path: set() for path in directories}
    for path in directories:
        if path:
            parent = path[:-1]
            if parent not in children:
                _fail("task_snapshot_preflight_blocked")
            children[parent].add(path[-1])
    for path in files:
        parent = path[:-1]
        if parent not in children:
            _fail("task_snapshot_preflight_blocked")
        children[parent].add(path[-1])
    records.extend(
        {
            "content_digest": None,
            "kind": "directory",
            "mode": 0o555,
            "path": "." if not path else "/".join(path),
            "size": 0,
        }
        for path in directories
    )
    records.sort(key=lambda record: record["path"].encode("utf-8"))
    tree_document = {
        "directory_count": len(directories),
        "document_type": "task-materialized-tree-v1",
        "entry_count": len(records),
        "file_count": len(entries),
        "records": records,
        "schema_version": 1,
        "total_bytes": total_bytes,
    }
    tree_digest = (
        "sha256:" + hashlib.sha256(canonical_bytes(tree_document)).hexdigest()
    )
    source_receipt = _reconstruct_canonical_receipt(
        captured.source_trust_receipt,
        reason="task_snapshot_preflight_blocked",
    )
    if source_receipt.input_digest != captured.source.input_digest:
        _fail("task_snapshot_preflight_blocked")
    try:
        snapshot_receipt = make_receipt(
            "task_snapshot",
            captured.source.input_digest,
            None,
            None,
            {
                "task_id": captured.source.task_id,
                "object_format": captured.object_format,
                "commit_oid": captured.commit_oid,
                "tree_oid": captured.tree_oid,
                "entry_digest": captured.entry_digest,
                "materialized_tree_digest": tree_digest,
                "materializer_policy_version": policy.policy_version,
                "file_count": len(entries),
                "total_bytes": total_bytes,
                "source_trust_receipt_digest": (
                    source_receipt.receipt_digest
                ),
            },
        )
    except (ExperimentReceiptError, TypeError, ValueError):
        _fail("task_snapshot_preflight_blocked")
    ordered_directories = tuple(
        sorted(
            directories,
            key=lambda path: (
                "." if not path else "/".join(path)
            ).encode("utf-8"),
        )
    )
    detached_children = MappingProxyType(
        {
            path: tuple(
                sorted(names, key=lambda name: name.encode("utf-8"))
            )
            for path, names in children.items()
        }
    )
    return _TaskCaptureExpectation(
        source_receipt=source_receipt,
        snapshot_receipt=snapshot_receipt,
        entries=entries,
        directories=ordered_directories,
        children=detached_children,
        files=MappingProxyType(dict(files)),
        materialized_tree_digest=tree_digest,
        file_count=len(entries),
        total_bytes=total_bytes,
    )


def _validated_experiment_request(
    request: ExperimentPreflightRequest,
) -> Tuple[
    CanonicalExperimentInput,
    Tuple[Mapping[str, object], ...],
    Tuple[TaskSourceSpec, ...],
]:
    if not _exact_fields(request, ExperimentPreflightRequest):
        _fail()
    try:
        authoritative = experiment_input_bytes(request.experiment_input)
        verified = load_experiment_input(authoritative)
    except (ExperimentPlanError, TypeError):
        _fail()
    if (
        not _exact_fields(verified, CanonicalExperimentInput)
        or request.experiment_input.canonical_bytes
        != verified.canonical_bytes
        or request.experiment_input.input_digest != verified.input_digest
        or request.experiment_input.value != verified.value
    ):
        _fail()
    candidates = tuple(verified.value["candidates"])
    task_ids = tuple(candidate["task_id"] for candidate in candidates)
    if (
        len(candidates) != 4
        or set(request.task_sources) != set(task_ids)
    ):
        _fail()
    sources = []
    for candidate in candidates:
        source = request.task_sources[candidate["task_id"]]
        if (
            not _exact_fields(source, TaskSourceSpec)
            or source.input_digest != verified.input_digest
            or source.task_id != candidate["task_id"]
            or source.commit_oid != candidate["commit_oid"]
            or source.provisioning_class
            != candidate["source_provisioning_class"]
            or source.operator_attested
            is not candidate["operator_attested"]
            or source.local_clone_policy
            != candidate["local_clone_policy"]
        ):
            _fail()
        _detached_exact_absolute_path(source.repository_root)
        sources.append(source)
    return verified, candidates, tuple(sources)


def _require_harness_source_pair(
    current: HarnessSourceManifest,
    lean: HarnessSourceManifest,
) -> None:
    if (
        not _exact_fields(current, HarnessSourceManifest)
        or not _exact_fields(lean, HarnessSourceManifest)
    ):
        _fail("harness_preflight_blocked")
    shared = (
        "schema_version",
        "bundle_id",
        "bundle_digest",
        "adapter_source_hash",
        "adapter_materialized_hash",
        "common_role_hash",
        "adapter_count",
        "role_count",
    )
    if (
        current.profile != "current"
        or lean.profile != "lean"
        or current.agents_hash == lean.agents_hash
        or any(getattr(current, name) != getattr(lean, name) for name in shared)
    ):
        _fail("harness_preflight_blocked")


def _manifest_matches_source(
    manifest: HarnessManifest,
    source: HarnessSourceManifest,
) -> bool:
    return (
        _exact_fields(manifest, HarnessManifest)
        and _exact_fields(source, HarnessSourceManifest)
        and manifest.bundle_id == source.bundle_id
        and manifest.bundle_digest == source.bundle_digest
        and manifest.profile == source.profile
        and manifest.agents_hash == source.agents_hash
        and manifest.adapter_source_hash == source.adapter_source_hash
        and manifest.adapter_materialized_hash
        == source.adapter_materialized_hash
        and manifest.common_role_hash == source.common_role_hash
        and manifest.adapter_count == source.adapter_count
        and manifest.role_count == source.role_count
    )


def _require_harness_verification(
    result: HarnessPreflightResult,
    manifest: HarnessManifest,
) -> None:
    if (
        not _exact_fields(result, HarnessPreflightResult)
        or result.classification != "ready"
        or result.result != "pass"
        or result.reason != "fixed_inventory_verified"
        or result.manifest is not manifest
    ):
        _fail("harness_preflight_blocked")


def _materialize_acquired_harness(
    request: ExperimentPreflightRequest,
    ledger: _OwnershipLedger,
    profile: str,
    source: HarnessSourceManifest,
) -> HarnessManifest:
    path = ("homes", profile)
    _create_owned_directory(ledger, path)
    home = request.temp_parent / "phase-a" / Path(*path)
    handoff = _begin_handoff(ledger, (path,))
    try:
        ledger.gate.require_live()
        manifest = materialize_harness_home(
            request.skill_repo,
            request.bundle_root,
            profile,
            home,
        )
    except BaseException as caught:
        if isinstance(caught, Exception):
            _resolve_empty_handoff(ledger, handoff)
        else:
            _abandon_handoff(handoff)
        raise

    def verify() -> None:
        if not _manifest_matches_source(manifest, source):
            _fail("harness_preflight_blocked")
        ledger.gate.require_live()
        result = verify_loaded_harness(
            request.skill_repo,
            request.bundle_root,
            home,
            manifest,
        )
        _require_harness_verification(result, manifest)

    _complete_handoff(ledger, handoff, verify)
    return manifest


def _same_task_metadata(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    try:
        return (
            _entry_from_stat((), first, ())
            == _entry_from_stat((), second, ())
        )
    except ExperimentPreflightError:
        return False


def _require_task_directory_metadata(
    metadata: os.stat_result,
    *,
    device: int,
) -> None:
    entry = _entry_from_stat((), metadata, ())
    if (
        entry.token.kind != "directory"
        or entry.token.device != device
        or entry.token.uid != os.getuid()
        or entry.mode != 0o555
        or entry.nlink < 1
    ):
        _fail("task_snapshot_preflight_blocked")


def _require_task_file_metadata(
    metadata: os.stat_result,
    *,
    device: int,
    expected: _ExpectedTaskFile,
) -> None:
    entry = _entry_from_stat((), metadata, ())
    if (
        entry.token.kind != "file"
        or entry.token.device != device
        or entry.token.uid != os.getuid()
        or entry.mode != expected.mode
        or entry.nlink != 1
        or entry.size != expected.size
    ):
        _fail("task_snapshot_preflight_blocked")


def _read_expected_task_digest(
    descriptor: int,
    expected_size: int,
) -> str:
    digest = hashlib.sha256()
    remaining = expected_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
        except InterruptedError:
            continue
        if (
            type(chunk) is not bytes
            or not chunk
            or len(chunk) > remaining
        ):
            _fail("task_snapshot_preflight_blocked")
        digest.update(chunk)
        remaining -= len(chunk)
    while True:
        try:
            extra = os.read(descriptor, 1)
            break
        except InterruptedError:
            continue
    if type(extra) is not bytes or extra:
        _fail("task_snapshot_preflight_blocked")
    return "sha256:" + digest.hexdigest()


def _task_identity_digest(
    *,
    materialized_tree_digest: str,
    inventory: Sequence[Tuple[str, os.stat_result, str]],
) -> str:
    records = []
    for path, metadata, kind in sorted(
        tuple(inventory),
        key=lambda item: item[0].encode("utf-8"),
    ):
        if (
            type(path) is not str
            or type(metadata) is not os.stat_result
            or kind not in ("directory", "file")
        ):
            _fail("task_snapshot_preflight_blocked")
        identity = {
            "ctime_ns": metadata.st_ctime_ns,
            "dev": metadata.st_dev,
            "gid": metadata.st_gid,
            "ino": metadata.st_ino,
            "kind": kind,
            "mode": stat.S_IMODE(metadata.st_mode),
            "mtime_ns": metadata.st_mtime_ns,
            "nlink": metadata.st_nlink,
            "size": metadata.st_size,
            "uid": metadata.st_uid,
        }
        if any(
            type(value) is not int or value < 0
            for name, value in identity.items()
            if name != "kind"
        ):
            _fail("task_snapshot_preflight_blocked")
        records.append({"identity": identity, "path": path})
    document = {
        "document_type": "task-materialized-root-identity-v2",
        "entry_count": len(records),
        "materialized_tree_digest": materialized_tree_digest,
        "records": records,
        "schema_version": 2,
    }
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


def _scan_expected_task_root(
    gate: _PhysicalDirectoryGate,
    root_components: Tuple[str, ...],
    expectation: _TaskCaptureExpectation,
) -> _ObservedTaskRoot:
    if (
        type(gate) is not _PhysicalDirectoryGate
        or not _exact_fields(expectation, _TaskCaptureExpectation)
    ):
        _fail("task_snapshot_preflight_blocked")
    _validate_owned_path(root_components)
    expected_directories = frozenset(expectation.directories)
    records = []
    identity_inventory = []
    identities = set()
    file_count = 0
    total_bytes = 0

    def remember_identity(metadata: os.stat_result) -> None:
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in identities:
            _fail("task_snapshot_preflight_blocked")
        identities.add(identity)

    def scan_file(
        parent_descriptor: int,
        name: str,
        expected: _ExpectedTaskFile,
        device: int,
    ) -> os.stat_result:
        nonlocal file_count, total_bytes
        descriptor = -1
        try:
            observed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_task_file_metadata(
                observed,
                device=device,
                expected=expected,
            )
            descriptor = os.open(
                name,
                _FILE_OPEN_FLAGS,
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            if not _same_task_metadata(observed, opened):
                _fail("task_snapshot_preflight_blocked")
            _require_task_file_metadata(
                opened,
                device=device,
                expected=expected,
            )
            content_digest = _read_expected_task_digest(
                descriptor,
                expected.size,
            )
            final = os.fstat(descriptor)
            named_final = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_task_metadata(opened, final)
                or not _same_task_metadata(opened, named_final)
                or content_digest != expected.content_digest
            ):
                _fail("task_snapshot_preflight_blocked")
            remember_identity(final)
            file_count += 1
            total_bytes += expected.size
            records.append(
                {
                    "content_digest": content_digest,
                    "kind": "file",
                    "mode": expected.mode,
                    "path": expected.path,
                    "size": expected.size,
                }
            )
            identity_inventory.append((expected.path, final, "file"))
            return final
        finally:
            if descriptor >= 0:
                try:
                    _close_descriptor(descriptor)
                except OSError:
                    _fail("task_snapshot_preflight_blocked")

    def scan_directory(
        descriptor: int,
        components: Tuple[str, ...],
        device: int,
    ) -> os.stat_result:
        before = os.fstat(descriptor)
        _require_task_directory_metadata(before, device=device)
        expected_names = expectation.children.get(components)
        if type(expected_names) is not tuple:
            _fail("task_snapshot_preflight_blocked")
        names = _bounded_directory_names(
            descriptor,
            maximum=len(expected_names),
        )
        if names != expected_names:
            _fail("task_snapshot_preflight_blocked")
        directory_path = "." if not components else "/".join(components)
        for name in names:
            child_components = components + (name,)
            expected_file = expectation.files.get(child_components)
            if child_components in expected_directories:
                if expected_file is not None:
                    _fail("task_snapshot_preflight_blocked")
                child_descriptor = -1
                try:
                    observed = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    child_descriptor = os.open(
                        name,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=descriptor,
                    )
                    opened = os.fstat(child_descriptor)
                    if not _same_task_metadata(observed, opened):
                        _fail("task_snapshot_preflight_blocked")
                    _require_task_directory_metadata(
                        opened,
                        device=device,
                    )
                    child_final = scan_directory(
                        child_descriptor,
                        child_components,
                        device,
                    )
                    named_final = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if not _same_task_metadata(
                        child_final,
                        named_final,
                    ):
                        _fail("task_snapshot_preflight_blocked")
                finally:
                    if child_descriptor >= 0:
                        try:
                            _close_descriptor(child_descriptor)
                        except OSError:
                            _fail("task_snapshot_preflight_blocked")
            elif expected_file is not None:
                scan_file(descriptor, name, expected_file, device)
            else:
                _fail("task_snapshot_preflight_blocked")
        after_names = _bounded_directory_names(
            descriptor,
            maximum=len(expected_names),
        )
        after = os.fstat(descriptor)
        if (
            after_names != expected_names
            or not _same_task_metadata(before, after)
        ):
            _fail("task_snapshot_preflight_blocked")
        remember_identity(after)
        records.append(
            {
                "content_digest": None,
                "kind": "directory",
                "mode": 0o555,
                "path": directory_path,
                "size": 0,
            }
        )
        identity_inventory.append(
            (directory_path, after, "directory")
        )
        return after

    descriptor = -1
    try:
        gate.require_live()
        descriptor = _open_relative_directory(
            gate.descriptor,
            ("phase-a",) + root_components,
        )
        root_metadata = os.fstat(descriptor)
        device = root_metadata.st_dev
        scan_directory(descriptor, (), device)
        gate.require_live()
    except ExperimentPreflightError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        _fail("task_snapshot_preflight_blocked")
    finally:
        if descriptor >= 0:
            try:
                _close_descriptor(descriptor)
            except OSError:
                _fail("task_snapshot_preflight_blocked")
    records.sort(key=lambda record: record["path"].encode("utf-8"))
    tree_document = {
        "directory_count": len(expected_directories),
        "document_type": "task-materialized-tree-v1",
        "entry_count": len(records),
        "file_count": file_count,
        "records": records,
        "schema_version": 1,
        "total_bytes": total_bytes,
    }
    tree_digest = (
        "sha256:" + hashlib.sha256(canonical_bytes(tree_document)).hexdigest()
    )
    if (
        tree_digest != expectation.materialized_tree_digest
        or file_count != expectation.file_count
        or total_bytes != expectation.total_bytes
        or len(records)
        != len(expected_directories) + expectation.file_count
    ):
        _fail("task_snapshot_preflight_blocked")
    return _ObservedTaskRoot(
        materialized_tree_digest=tree_digest,
        target_identity_digest=_task_identity_digest(
            materialized_tree_digest=tree_digest,
            inventory=tuple(identity_inventory),
        ),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def _require_materialized_pair(
    pair: object,
    *,
    target_parent: Path,
    task_id: str,
    expectation: _TaskCaptureExpectation,
) -> Tuple[MaterializedTaskSnapshot, MaterializedTaskSnapshot]:
    if (
        type(pair) is not tuple
        or len(pair) != 2
        or any(
            not _exact_fields(item, MaterializedTaskSnapshot)
            for item in pair
        )
    ):
        _fail("task_snapshot_preflight_blocked")
    returned_current, returned_lean = pair
    if (
        returned_current is returned_lean
        or returned_current.snapshot_receipt
        is not returned_lean.snapshot_receipt
    ):
        _fail("task_snapshot_preflight_blocked")
    try:
        current = MaterializedTaskSnapshot(
            snapshot_receipt=returned_current.snapshot_receipt,
            target_root=returned_current.target_root,
            target_identity_digest=(
                returned_current.target_identity_digest
            ),
            materialized_tree_digest=(
                returned_current.materialized_tree_digest
            ),
            file_count=returned_current.file_count,
            total_bytes=returned_current.total_bytes,
        )
        lean = MaterializedTaskSnapshot(
            snapshot_receipt=returned_lean.snapshot_receipt,
            target_root=returned_lean.target_root,
            target_identity_digest=returned_lean.target_identity_digest,
            materialized_tree_digest=(
                returned_lean.materialized_tree_digest
            ),
            file_count=returned_lean.file_count,
            total_bytes=returned_lean.total_bytes,
        )
    except TaskSnapshotError:
        _fail("task_snapshot_preflight_blocked")
    if current.snapshot_receipt != lean.snapshot_receipt:
        _fail("task_snapshot_preflight_blocked")
    object.__setattr__(
        lean,
        "snapshot_receipt",
        current.snapshot_receipt,
    )
    if (
        current.target_root != target_parent / "current"
        or lean.target_root != target_parent / "lean"
        or current.target_root == lean.target_root
        or current.target_identity_digest == lean.target_identity_digest
        or current.snapshot_receipt.payload["task_id"] != task_id
        or current.snapshot_receipt != expectation.snapshot_receipt
        or current.materialized_tree_digest
        != expectation.materialized_tree_digest
        or lean.materialized_tree_digest
        != expectation.materialized_tree_digest
        or current.file_count != expectation.file_count
        or lean.file_count != expectation.file_count
        or current.total_bytes != expectation.total_bytes
        or lean.total_bytes != expectation.total_bytes
    ):
        _fail("task_snapshot_preflight_blocked")
    return current, lean


def _expected_task_pair_inventory(
    task_parent_components: Tuple[str, ...],
    entries: Sequence[TaskTreeEntry],
) -> Mapping[Tuple[str, ...], str]:
    expected = {}
    for condition in ("current", "lean"):
        root = task_parent_components + (condition,)
        expected[root] = "directory"
        for entry in tuple(entries):
            if not _exact_fields(entry, TaskTreeEntry):
                _fail("task_snapshot_preflight_blocked")
            components = _path_components(entry.path)
            for index in range(1, len(components)):
                path = root + components[:index]
                previous = expected.setdefault(path, "directory")
                if previous != "directory":
                    _fail("task_snapshot_preflight_blocked")
            file_path = root + components
            if file_path in expected:
                _fail("task_snapshot_preflight_blocked")
            expected[file_path] = "file"
    return MappingProxyType(expected)


def _materialize_acquired_task(
    *,
    request: ExperimentPreflightRequest,
    ledger: _OwnershipLedger,
    candidate: Mapping[str, object],
    source: TaskSourceSpec,
    policy: TaskSnapshotPolicy,
) -> _TaskEvidence:
    task_id = candidate["task_id"]
    task_parent_components = ("tasks", task_id)
    _create_owned_directory(ledger, task_parent_components)
    target_parent = (
        request.temp_parent
        / "phase-a"
        / "tasks"
        / task_id
    )
    materializer = TaskSnapshotMaterializer(policy)
    try:
        prepared = prepare_task_source(source, policy)
        captured = materializer.capture(prepared)
        expectation = _detach_task_capture_expectation(
            captured,
            policy,
        )
        records = _classify_allowed_write_paths(
            tuple(candidate["allowed_write_paths"]),
            expectation.entries,
        )
        expected_inventory = _expected_task_pair_inventory(
            task_parent_components,
            expectation.entries,
        )
        handoff = _begin_handoff(
            ledger,
            (
                task_parent_components + ("current",),
                task_parent_components + ("lean",),
            ),
        )
        try:
            ledger.gate.require_live()
            returned = materializer.materialize_pair(
                captured,
                target_parent,
                "current",
                "lean",
            )
        except BaseException as caught:
            if isinstance(caught, Exception):
                _resolve_empty_handoff(ledger, handoff)
            else:
                _abandon_handoff(handoff)
            raise
        pair_holder = {}

        def verify_return() -> None:
            pair = _require_materialized_pair(
                returned,
                target_parent=target_parent,
                task_id=task_id,
                expectation=expectation,
            )
            for condition, snapshot in zip(
                ("current", "lean"),
                pair,
            ):
                observed = _scan_expected_task_root(
                    ledger.gate,
                    task_parent_components + (condition,),
                    expectation,
                )
                if (
                    snapshot.materialized_tree_digest
                    != observed.materialized_tree_digest
                    or snapshot.target_identity_digest
                    != observed.target_identity_digest
                    or snapshot.file_count != observed.file_count
                    or snapshot.total_bytes != observed.total_bytes
                ):
                    _fail("task_snapshot_preflight_blocked")
            pair_holder["value"] = pair

        _complete_handoff(
            ledger,
            handoff,
            verify_return,
            expected_inventory,
        )
        current, lean = pair_holder["value"]
        snapshot_receipt = current.snapshot_receipt
        if (
            not _exact_fields(snapshot_receipt, CanonicalReceipt)
            or snapshot_receipt.receipt_digest
            != lean.snapshot_receipt.receipt_digest
            or snapshot_receipt.input_digest
            != request.experiment_input.input_digest
            or expectation.source_receipt.input_digest
            != request.experiment_input.input_digest
        ):
            _fail("task_snapshot_preflight_blocked")
        allowed_digest = _allowed_write_policy_digest(
            task_id=task_id,
            materialized_tree_digest=current.materialized_tree_digest,
            snapshot_receipt_digest=snapshot_receipt.receipt_digest,
            records=records,
        )
        evidence = _TaskEvidence(
            task_id=task_id,
            source_receipt=expectation.source_receipt,
            snapshot_receipt=snapshot_receipt,
            snapshot_receipt_digest=snapshot_receipt.receipt_digest,
            materialized_tree_digest=current.materialized_tree_digest,
            allowed_write_policy_digest=allowed_digest,
        )
        captured = None
        returned = None
        return evidence
    finally:
        materializer.close()


def _static_receipts(
    experiment_input: CanonicalExperimentInput,
    candidates: Tuple[Mapping[str, object], ...],
    evidence: Tuple[_TaskEvidence, ...],
) -> Tuple[CanonicalReceipt, CanonicalReceipt]:
    static = derive_static_evidence_digests(experiment_input)
    selection = make_receipt(
        "task_selection",
        experiment_input.input_digest,
        None,
        None,
        {
            "candidate_set_digest": static.candidate_set_digest,
            "selection_rule": experiment_input.value["selection_rule"],
            "selection_seed_digest": static.selection_seed_digest,
            "selected_task_ids": [
                candidate["task_id"] for candidate in candidates
            ],
            "pilot_schedule_digest": static.pilot_schedule_digest,
        },
    )
    corpus = make_receipt(
        "task_corpus",
        experiment_input.input_digest,
        None,
        None,
        {
            "candidate_set_digest": static.candidate_set_digest,
            "selection_receipt_digest": selection.receipt_digest,
            "selected_snapshot_receipt_digests": [
                item.snapshot_receipt_digest for item in evidence
            ],
            "qualification_digest": static.qualification_digest,
            "qualification_evidence_classification": (
                "operator_attested_static"
            ),
            "prompt_digests": [
                candidate["prompt_digest"] for candidate in candidates
            ],
            "validator_digests": [
                candidate["validator_digest"] for candidate in candidates
            ],
            "assertion_digests": [
                candidate["assertion_digest"] for candidate in candidates
            ],
            "reference_result_digest": static.reference_result_digest,
            "mutation_sensitivity_digest": (
                static.mutation_sensitivity_digest
            ),
            "difficulty_assignment_digest": (
                static.difficulty_assignment_digest
            ),
        },
    )
    validate_static_receipt_graph(
        experiment_input,
        tuple(item.source_receipt for item in evidence),
        tuple(item.snapshot_receipt for item in evidence),
        selection,
        corpus,
    )
    return selection, corpus


def _invocation_plans(
    experiment_input: CanonicalExperimentInput,
    *,
    current_profile_digest: str,
    lean_profile_digest: str,
    evidence: Tuple[_TaskEvidence, ...],
) -> Tuple[
    Tuple[CanaryInvocationTemplate, ...],
    Tuple[PilotInvocationPlan, ...],
]:
    model = experiment_input.value["model"]
    invocation = experiment_input.value["invocation_policy"]
    containment = experiment_input.value["containment_policy_version"]
    templates = (
        CanaryInvocationTemplate(
            ordinal=1,
            profile="current",
            model_id=model["model_id"],
            reasoning_effort=model["reasoning_effort"],
            sandbox=invocation["canary_sandbox"],
            approval_policy=invocation["approval_policy"],
            provider_transport_allowed=invocation[
                "provider_transport_allowed"
            ],
            tool_network_disabled=invocation["tool_network_disabled"],
            base_profile_digest=current_profile_digest,
            overlay_recipe_policy_digest=CANARY_OVERLAY_RECIPE_DIGEST,
            root_capability_policy_digest=ROOT_CAPABILITY_POLICY_DIGEST,
            child_process_policy=invocation["child_process_policy"],
            validator_policy=invocation["validator_policy"],
            output_schema_digest=CANARY_RESPONSE_SCHEMA_DIGEST,
            environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
            argv_template_digest=CANARY_ARGV_TEMPLATE_DIGEST,
            containment_policy_version=containment,
        ),
        CanaryInvocationTemplate(
            ordinal=2,
            profile="lean",
            model_id=model["model_id"],
            reasoning_effort=model["reasoning_effort"],
            sandbox=invocation["canary_sandbox"],
            approval_policy=invocation["approval_policy"],
            provider_transport_allowed=invocation[
                "provider_transport_allowed"
            ],
            tool_network_disabled=invocation["tool_network_disabled"],
            base_profile_digest=lean_profile_digest,
            overlay_recipe_policy_digest=CANARY_OVERLAY_RECIPE_DIGEST,
            root_capability_policy_digest=ROOT_CAPABILITY_POLICY_DIGEST,
            child_process_policy=invocation["child_process_policy"],
            validator_policy=invocation["validator_policy"],
            output_schema_digest=CANARY_RESPONSE_SCHEMA_DIGEST,
            environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
            argv_template_digest=CANARY_ARGV_TEMPLATE_DIGEST,
            containment_policy_version=containment,
        ),
    )
    evidence_by_task = {item.task_id: item for item in evidence}
    pilots = []
    for run in build_pilot_schedule(
        experiment_input.value["selection_seed"],
        experiment_input.value["candidates"],
    ):
        item = evidence_by_task[run.task_id]
        pilots.append(
            PilotInvocationPlan(
                ordinal=run.ordinal + 2,
                run=run,
                snapshot_receipt_digest=item.snapshot_receipt_digest,
                allowed_write_policy_digest=(
                    item.allowed_write_policy_digest
                ),
                base_profile_digest=(
                    current_profile_digest
                    if run.condition == "current"
                    else lean_profile_digest
                ),
                root_capability_policy_digest=(
                    ROOT_CAPABILITY_POLICY_DIGEST
                ),
                model_id=model["model_id"],
                reasoning_effort=model["reasoning_effort"],
                sandbox=invocation["pilot_sandbox"],
                approval_policy=invocation["approval_policy"],
                provider_transport_allowed=invocation[
                    "provider_transport_allowed"
                ],
                tool_network_disabled=invocation[
                    "tool_network_disabled"
                ],
                child_process_policy=invocation[
                    "child_process_policy"
                ],
                validator_policy=invocation["validator_policy"],
                output_schema_digest=PILOT_RESPONSE_SCHEMA_DIGEST,
                environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
                argv_template_digest=PILOT_ARGV_TEMPLATE_DIGEST,
                containment_policy_version=containment,
            )
        )
    return templates, tuple(pilots)


def _build_plan(
    experiment_input: CanonicalExperimentInput,
    *,
    bundle_digest: str,
    current_profile_digest: str,
    lean_profile_digest: str,
    evidence: Tuple[_TaskEvidence, ...],
    selection: CanonicalReceipt,
    corpus: CanonicalReceipt,
) -> ExperimentPlan:
    templates, pilots = _invocation_plans(
        experiment_input,
        current_profile_digest=current_profile_digest,
        lean_profile_digest=lean_profile_digest,
        evidence=evidence,
    )
    arguments = {
        "bundle_digest": bundle_digest,
        "current_profile_digest": current_profile_digest,
        "lean_profile_digest": lean_profile_digest,
        "task_source_trust_receipt_digests": tuple(
            sorted(
                (
                    item.source_receipt.receipt_digest
                    for item in evidence
                ),
                key=str.encode,
            )
        ),
        "task_selection_receipt_digest": selection.receipt_digest,
        "task_corpus_receipt_digest": corpus.receipt_digest,
        "canary_templates": templates,
        "pilot_invocation_plans": pilots,
    }
    plan = build_experiment_plan(experiment_input, **arguments)
    rebuilt = build_experiment_plan(experiment_input, **arguments)
    if (
        not _exact_fields(plan, ExperimentPlan)
        or plan != rebuilt
        or plan.canonical_bytes != rebuilt.canonical_bytes
    ):
        _fail("experiment_plan_preflight_blocked")
    return plan


def _set_digest(document_type: str, digests: Sequence[str]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_bytes(
            {
                "document_type": document_type,
                "schema_version": 1,
                "digests": list(digests),
            }
        )
    ).hexdigest()


def _success_preflight_receipt(
    plan: ExperimentPlan,
) -> CanonicalReceipt:
    receipt = make_receipt(
        "preflight",
        plan.input_digest,
        plan.plan_digest,
        None,
        {
            "evidence_state": "static_only",
            "live_backend_state": "live_backend_not_implemented",
            "global_agents_marker_state": (
                "global_agents_marker_not_run"
            ),
            "pilot_state": "pilot_not_run",
            "qualification_evidence_classification": (
                "operator_attested_static"
            ),
            "model_calls": 0,
            "bundle_digest": plan.plan_document["bundle_digest"],
            "current_profile_digest": plan.plan_document[
                "current_profile_digest"
            ],
            "lean_profile_digest": plan.plan_document[
                "lean_profile_digest"
            ],
            "task_corpus_receipt_digest": plan.plan_document[
                "task_corpus_receipt_digest"
            ],
            "experiment_plan_digest": plan.plan_digest,
            "canary_template_set_digest": _set_digest(
                "canary_template_set",
                plan.plan_document["canary_template_digests"],
            ),
            "pilot_invocation_plan_set_digest": _set_digest(
                "pilot_invocation_plan_set",
                plan.plan_document["pilot_invocation_plan_digests"],
            ),
            "materialization_result": "verified",
            "cleanup_state": "removed",
        },
    )
    replay_runtime_history(plan, receipt, ())
    return receipt


def _blocked_result(
    progress: Mapping[str, Optional[str]],
    *,
    cleanup_state: str,
    reason_code: str,
) -> ExperimentPreflightResult:
    if cleanup_state == "cleanup_required":
        reason_code = "task_snapshot_cleanup_required"
    return ExperimentPreflightResult(
        status="blocked",
        live_backend_state="live_backend_not_implemented",
        global_agents_marker_state="global_agents_marker_not_run",
        pilot_state="pilot_not_run",
        qualification_evidence_classification=(
            "operator_attested_static"
            if progress["task_corpus_receipt_digest"] is not None
            else "not_validated"
        ),
        model_calls=0,
        materialization_result="blocked",
        plan_digest=progress["plan_digest"],
        task_corpus_receipt_digest=progress[
            "task_corpus_receipt_digest"
        ],
        preflight_receipt_digest=None,
        bundle_digest=progress["bundle_digest"],
        current_profile_digest=progress["current_profile_digest"],
        lean_profile_digest=progress["lean_profile_digest"],
        cleanup_state=cleanup_state,
        reason_code=reason_code,
    )


def _phase_exists(gate: _PhysicalDirectoryGate) -> bool:
    try:
        os.stat(
            "phase-a",
            dir_fd=gate.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _reason_for_exception(
    caught: Exception,
    fallback: str,
) -> str:
    if isinstance(caught, ExperimentPreflightError):
        candidate = str(caught)
        return (
            candidate
            if candidate in _RESULT_REASONS
            else fallback
        )
    if isinstance(caught, HarnessError):
        return "harness_preflight_blocked"
    if isinstance(caught, TaskSnapshotError):
        return "task_snapshot_preflight_blocked"
    if isinstance(caught, ExperimentReceiptError):
        return "static_receipt_preflight_blocked"
    if isinstance(caught, ExperimentPlanError):
        return fallback
    return fallback


def _final_mutation_recheck(
    *,
    request: ExperimentPreflightRequest,
    ledger: _OwnershipLedger,
    current_source: HarnessSourceManifest,
    lean_source: HarnessSourceManifest,
    current_manifest: HarnessManifest,
    lean_manifest: HarnessManifest,
) -> None:
    reloaded_current = load_harness_source(
        request.bundle_root, "current"
    )
    reloaded_lean = load_harness_source(request.bundle_root, "lean")
    if (
        reloaded_current != current_source
        or reloaded_lean != lean_source
    ):
        _fail("harness_preflight_blocked")
    for profile, manifest in (
        ("current", current_manifest),
        ("lean", lean_manifest),
    ):
        home = request.temp_parent / "phase-a" / "homes" / profile
        ledger.gate.require_live()
        _require_harness_verification(
            verify_loaded_harness(
                request.skill_repo,
                request.bundle_root,
                home,
                manifest,
            ),
            manifest,
        )
    if _scan_phase_tree(ledger.gate) != ledger.entries:
        _fail("task_snapshot_preflight_blocked")


def run_experiment_preflight(
    request: ExperimentPreflightRequest,
) -> ExperimentPreflightResult:
    """Build and verify Phase A evidence without any model or live call."""
    progress = {
        "bundle_digest": None,
        "current_profile_digest": None,
        "lean_profile_digest": None,
        "task_corpus_receipt_digest": None,
        "plan_digest": None,
    }
    gate = None
    ledger = None
    stage_reason = "experiment_preflight_invalid"
    try:
        experiment_input, candidates, sources = (
            _validated_experiment_request(request)
        )
        protected_roots = (
            request.bundle_root,
            request.skill_repo,
            *tuple(source.repository_root for source in sources),
            *tuple(
                source.repository_root / ".git" for source in sources
            ),
        )
        gate = _open_physical_directory_gate(request.temp_parent)
        _require_physical_disjointness(gate, protected_roots)
        ledger = _create_phase_ledger(gate)
        _create_owned_directory(ledger, ("homes",))
        _create_owned_directory(ledger, ("tasks",))

        stage_reason = "harness_preflight_blocked"
        current_source = load_harness_source(
            request.bundle_root, "current"
        )
        lean_source = load_harness_source(request.bundle_root, "lean")
        _require_harness_source_pair(current_source, lean_source)
        progress["bundle_digest"] = current_source.bundle_digest
        current_manifest = _materialize_acquired_harness(
            request, ledger, "current", current_source
        )
        progress["current_profile_digest"] = _base_profile_digest(
            current_manifest
        )
        lean_manifest = _materialize_acquired_harness(
            request, ledger, "lean", lean_source
        )
        (
            current_profile_digest,
            lean_profile_digest,
        ) = _require_base_profile_pair(current_manifest, lean_manifest)
        if current_profile_digest != progress["current_profile_digest"]:
            _fail("harness_preflight_blocked")
        progress["lean_profile_digest"] = lean_profile_digest

        stage_reason = "task_snapshot_preflight_blocked"
        policy = TaskSnapshotPolicy()
        evidence = tuple(
            _materialize_acquired_task(
                request=request,
                ledger=ledger,
                candidate=candidate,
                source=source,
                policy=policy,
            )
            for candidate, source in zip(candidates, sources)
        )

        stage_reason = "static_receipt_preflight_blocked"
        selection, corpus = _static_receipts(
            experiment_input, candidates, evidence
        )
        progress["task_corpus_receipt_digest"] = (
            corpus.receipt_digest
        )

        _final_mutation_recheck(
            request=request,
            ledger=ledger,
            current_source=current_source,
            lean_source=lean_source,
            current_manifest=current_manifest,
            lean_manifest=lean_manifest,
        )

        stage_reason = "experiment_plan_preflight_blocked"
        plan = _build_plan(
            experiment_input,
            bundle_digest=current_manifest.bundle_digest,
            current_profile_digest=current_profile_digest,
            lean_profile_digest=lean_profile_digest,
            evidence=evidence,
            selection=selection,
            corpus=corpus,
        )
        forbidden_paths = tuple(
            os.fspath(path).encode("utf-8")
            for path in protected_roots + (request.temp_parent,)
        )
        if any(path in plan.canonical_bytes for path in forbidden_paths):
            _fail("experiment_plan_preflight_blocked")

        if not _cleanup_owned_phase(ledger):
            cleanup_progress = dict(progress)
            cleanup_progress["plan_digest"] = plan.plan_digest
            result = _blocked_result(
                cleanup_progress,
                cleanup_state="cleanup_required",
                reason_code="task_snapshot_cleanup_required",
            )
            try:
                gate.close()
            except BaseException as caught:
                if not isinstance(caught, Exception):
                    raise
            gate = None
            return result
        ledger = None
        try:
            gate.close()
        except BaseException as caught:
            if not isinstance(caught, Exception):
                raise
            close_progress = dict(progress)
            close_progress["plan_digest"] = plan.plan_digest
            return _blocked_result(
                close_progress,
                cleanup_state="cleanup_required",
                reason_code="task_snapshot_cleanup_required",
            )
        gate = None
        try:
            preflight = _success_preflight_receipt(plan)
            return ExperimentPreflightResult(
                status="static_only",
                live_backend_state="live_backend_not_implemented",
                global_agents_marker_state="global_agents_marker_not_run",
                pilot_state="pilot_not_run",
                qualification_evidence_classification=(
                    "operator_attested_static"
                ),
                model_calls=0,
                materialization_result="verified",
                plan_digest=plan.plan_digest,
                task_corpus_receipt_digest=corpus.receipt_digest,
                preflight_receipt_digest=preflight.receipt_digest,
                bundle_digest=current_manifest.bundle_digest,
                current_profile_digest=current_profile_digest,
                lean_profile_digest=lean_profile_digest,
                cleanup_state="removed",
                reason_code="static_preflight_verified",
            )
        except BaseException as caught:
            if not isinstance(caught, Exception):
                raise
            return _blocked_result(
                progress,
                cleanup_state="removed",
                reason_code="static_receipt_preflight_blocked",
            )
    except BaseException as caught:
        if not isinstance(caught, Exception):
            if gate is not None:
                try:
                    gate.close()
                except BaseException:
                    pass
            raise
        cleanup_state = "not_started"
        if ledger is not None:
            cleanup_state = (
                "removed"
                if _cleanup_owned_phase(ledger)
                else "cleanup_required"
            )
        elif gate is not None and _phase_exists(gate):
            cleanup_state = "cleanup_required"
        if gate is not None:
            try:
                gate.close()
            except BaseException as close_error:
                if not isinstance(close_error, Exception):
                    raise
                cleanup_state = "cleanup_required"
        return _blocked_result(
            progress,
            cleanup_state=cleanup_state,
            reason_code=_reason_for_exception(caught, stage_reason),
        )
