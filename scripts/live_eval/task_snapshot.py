"""Fail-closed trust gate for one operator-attested local Git source."""

from dataclasses import dataclass, field, fields
from contextlib import contextmanager
import hashlib
import inspect
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from types import MappingProxyType
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple
import unicodedata

from scripts.live_eval.experiment_receipts import (
    CanonicalReceipt,
    make_receipt,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


class TaskSnapshotError(ValueError):
    """Raised when a task source cannot satisfy the fixed trust contract."""


_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_POLICY_CEILINGS = {
    "git_timeout_seconds": 15,
    "git_termination_grace_milliseconds": 250,
    "max_git_stdout_bytes": 16 * 1024 * 1024,
    "max_git_stderr_bytes": 64 * 1024,
    "capture_timeout_seconds": 60,
    "max_config_bytes": 256 * 1024,
    "max_packed_refs_bytes": 4 * 1024 * 1024,
    "max_object_entries": 200000,
    "max_object_store_bytes": 2 * 1024 * 1024 * 1024,
    "max_object_depth": 3,
    "max_component_bytes": 255,
    "max_relative_path_bytes": 4096,
    "max_tree_entries": 100000,
    "max_tree_depth": 64,
    "max_files": 10000,
    "max_unique_blobs": 256,
    "max_file_bytes": 4 * 1024 * 1024,
    "max_total_bytes": 64 * 1024 * 1024,
}
_TASK_SOURCE_TRUST_RECEIPT_KEYS = frozenset(
    {
        "task_id",
        "provisioning_class",
        "operator_attested",
        "local_clone_policy",
        "object_format",
        "source_identity_before_digest",
        "object_topology_before_digest",
        "source_identity_after_digest",
        "object_topology_after_digest",
        "git_process_policy_digest",
        "inventory_file_count",
        "inventory_total_bytes",
    }
)
_TASK_SNAPSHOT_RECEIPT_KEYS = frozenset(
    {
        "task_id",
        "object_format",
        "commit_oid",
        "tree_oid",
        "entry_digest",
        "materialized_tree_digest",
        "materializer_policy_version",
        "file_count",
        "total_bytes",
        "source_trust_receipt_digest",
    }
)
_TASK_RECEIPT_PAYLOAD_KEYS = MappingProxyType(
    {
        "task_source_trust": _TASK_SOURCE_TRUST_RECEIPT_KEYS,
        "task_snapshot": _TASK_SNAPSHOT_RECEIPT_KEYS,
    }
)


@dataclass(frozen=True)
class TaskSourceSpec:
    input_digest: str
    task_id: str
    repository_root: Path = field(repr=False)
    commit_oid: str
    provisioning_class: str
    operator_attested: bool
    local_clone_policy: str

    def __post_init__(self) -> None:
        try:
            raw_root = os.fspath(self.repository_root)
        except Exception:
            _fail("task_source_spec_invalid")
        if type(raw_root) is not str:
            _fail("task_source_spec_invalid")
        object.__setattr__(self, "repository_root", Path(raw_root))
        _validate_source(self)


@dataclass(frozen=True)
class TaskSnapshotPolicy:
    policy_version: str = "task-object-materializer-v1"
    git_timeout_seconds: int = 15
    git_termination_grace_milliseconds: int = 250
    max_git_stdout_bytes: int = 16 * 1024 * 1024
    max_git_stderr_bytes: int = 64 * 1024
    capture_timeout_seconds: int = 60
    max_config_bytes: int = 256 * 1024
    max_packed_refs_bytes: int = 4 * 1024 * 1024
    max_object_entries: int = 200000
    max_object_store_bytes: int = 2 * 1024 * 1024 * 1024
    max_object_depth: int = 3
    max_component_bytes: int = 255
    max_relative_path_bytes: int = 4096
    max_tree_entries: int = 100000
    max_tree_depth: int = 64
    max_files: int = 10000
    max_unique_blobs: int = 256
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        _validate_policy(self)


@dataclass(frozen=True)
class TaskTreeEntry:
    path: str
    git_mode: str
    blob_oid: str
    size: int
    content_digest: str

    def __post_init__(self) -> None:
        _validate_task_tree_entry(self)


@dataclass(frozen=True)
class CapturedTaskObjects:
    source_trust_receipt: CanonicalReceipt
    source: TaskSourceSpec = field(repr=False)
    policy: TaskSnapshotPolicy = field(repr=False)
    object_format: str
    commit_oid: str
    tree_oid: str
    entry_digest: str
    entries: Tuple[TaskTreeEntry, ...] = field(repr=False)
    blobs: Mapping[str, bytes] = field(repr=False)
    file_count: int
    total_bytes: int
    unique_blob_count: int
    unique_blob_bytes: int

    def __post_init__(self) -> None:
        _detach_and_validate_captured_objects(self)


@dataclass(frozen=True)
class MaterializedTaskSnapshot:
    snapshot_receipt: CanonicalReceipt
    target_root: Path = field(repr=False)
    target_identity_digest: str
    materialized_tree_digest: str
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        _detach_and_validate_materialized_snapshot(self)


@dataclass(frozen=True)
class ObjectTopologySeal:
    object_topology_digest: str
    entry_count: int
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        _validate_topology_seal(self)


@dataclass(frozen=True)
class PreparedTaskSource:
    source: TaskSourceSpec = field(repr=False)
    policy: TaskSnapshotPolicy = field(repr=False)
    git_dir: Path = field(repr=False)
    object_format: str
    source_identity_digest: str
    local_config_digest: str
    object_topology: ObjectTopologySeal
    git_process_policy_digest: str

    def __post_init__(self) -> None:
        try:
            raw_git_dir = os.fspath(self.git_dir)
        except Exception:
            _fail("task_source_changed")
        if type(raw_git_dir) is not str:
            _fail("task_source_changed")
        object.__setattr__(self, "git_dir", Path(raw_git_dir))
        _validate_prepared_source(self)


@dataclass(frozen=True)
class _FilesystemSeal:
    filesystem_digest: str
    raw_config_digest: str
    git_dir: Path = field(repr=False)
    config_path: Path = field(repr=False)
    repository_identity_key: Tuple[int, int, str]
    git_dir_identity_key: Tuple[int, int, str]


@dataclass(frozen=True)
class _TaskTreeRecord:
    path: str
    git_mode: str
    blob_oid: str
    size: int


@dataclass(frozen=True)
class _ParsedTaskTree:
    object_format: str
    records: Tuple[_TaskTreeRecord, ...]
    directories: Tuple[str, ...]
    blob_sizes: Tuple[Tuple[str, int], ...]
    file_count: int
    directory_count: int
    tree_entry_count: int
    total_bytes: int
    unique_blob_count: int
    unique_blob_bytes: int


@dataclass(frozen=True)
class _CapturedOperationalSeal:
    source_trust_receipt: CanonicalReceipt
    source: TaskSourceSpec = field(repr=False)
    policy: TaskSnapshotPolicy = field(repr=False)
    object_format: str
    commit_oid: str
    tree_oid: str
    entry_digest: str
    entries: Tuple[TaskTreeEntry, ...] = field(repr=False)
    blobs: Mapping[str, bytes] = field(repr=False)
    file_count: int
    total_bytes: int
    unique_blob_count: int
    unique_blob_bytes: int
    protected_identity_keys: Tuple[Tuple[int, int, str], ...]


@dataclass(init=False)
class _TargetParentGate:
    _descriptor_owner: object = field(repr=False)
    target_parent: Path = field(repr=False)
    metadata: os.stat_result = field(repr=False)
    device: int
    current_name: str
    lean_name: str
    _owns_descriptor: bool = field(default=True, repr=False)

    def __init__(
        self,
        descriptor: int = -1,
        target_parent: Optional[Path] = None,
        metadata: Optional[os.stat_result] = None,
        device: int = -1,
        current_name: str = "",
        lean_name: str = "",
        _owns_descriptor: bool = True,
        _descriptor_owner: Optional[object] = None,
    ) -> None:
        self._descriptor_owner = (
            _SourceResourceOwner(descriptor=descriptor)
            if _descriptor_owner is None
            else _descriptor_owner
        )
        self.target_parent = target_parent
        self.metadata = metadata
        self.device = device
        self.current_name = current_name
        self.lean_name = lean_name
        self._owns_descriptor = _owns_descriptor

    @property
    def descriptor(self) -> int:
        return self._descriptor_owner.descriptor

    def close(self) -> bool:
        if not self._owns_descriptor:
            return not self._descriptor_owner._uncertain
        cleanup_ok = _guard_cleanup_boolean(
            self._descriptor_owner.close_descriptor
        )
        if not self._descriptor_owner.has_resources():
            self._owns_descriptor = False
        return cleanup_ok and not self._descriptor_owner._uncertain

    def __enter__(self):
        if self.descriptor < 0:
            _fail("task_snapshot_cleanup_required")
        return self

    def __exit__(self, unused_type, unused_value, unused_traceback):
        preserve_abort = _active_terminal_baseexception()
        if not _guard_cleanup_boolean(self.close):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )
        return False

    def __del__(self):
        if not getattr(self, "_owns_descriptor", False):
            return
        try:
            self._descriptor_owner.close_descriptor()
        except BaseException:
            pass


@dataclass(init=False)
class _MaterializedRootGate:
    _descriptor_owner: object = field(repr=False)
    metadata: os.stat_result = field(repr=False)
    _owns_descriptor: bool = field(default=True, repr=False)

    def __init__(
        self,
        descriptor: int = -1,
        metadata: Optional[os.stat_result] = None,
        _owns_descriptor: bool = True,
        _descriptor_owner: Optional[object] = None,
    ) -> None:
        self._descriptor_owner = (
            _SourceResourceOwner(descriptor=descriptor)
            if _descriptor_owner is None
            else _descriptor_owner
        )
        self.metadata = metadata
        self._owns_descriptor = _owns_descriptor

    @property
    def descriptor(self) -> int:
        return self._descriptor_owner.descriptor

    def close(self) -> bool:
        if not self._owns_descriptor:
            return not self._descriptor_owner._uncertain
        cleanup_ok = _guard_cleanup_boolean(
            self._descriptor_owner.close_descriptor
        )
        if not self._descriptor_owner.has_resources():
            self._owns_descriptor = False
        return cleanup_ok and not self._descriptor_owner._uncertain

    def __del__(self):
        if not getattr(self, "_owns_descriptor", False):
            return
        try:
            self._descriptor_owner.close_descriptor()
        except BaseException:
            pass


@dataclass
class _OwnedTargetEntry:
    parent_components: Tuple[str, ...]
    basename: str
    kind: str
    created: Optional[bool] = None
    token: Optional[Tuple[int, int, int, int, str]] = None


@dataclass
class _TargetOwnershipLedger:
    entries: list = field(default_factory=list)
    by_path: dict = field(default_factory=dict)
    close_uncertain: bool = False


def _fail(code: str) -> None:
    try:
        raise TaskSnapshotError(code) from None
    except TaskSnapshotError as error:
        error.__context__ = None
        error.__cause__ = None
        raise


def _active_terminal_baseexception() -> bool:
    active_error = sys.exc_info()[1]
    return (
        active_error is not None
        and not isinstance(active_error, Exception)
    )


def _guard_cleanup_boolean(
    operation: Callable[..., object],
    *args: object,
) -> bool:
    try:
        return operation(*args) is True
    except Exception:
        return False


def _guard_cleanup_action(
    operation: Callable[..., object],
    *args: object,
) -> bool:
    try:
        operation(*args)
    except Exception:
        return False
    return True


def _fail_cleanup_required_unless_nonexception_active(
    preserve_abort: bool,
) -> None:
    if not preserve_abort:
        _fail("task_snapshot_cleanup_required")


def _exact_fields(value: object, expected_type: type) -> bool:
    if type(value) is not expected_type:
        return False
    actual = vars(value)
    expected = fields(expected_type)
    return len(actual) == len(expected) and all(
        item.name in actual for item in expected
    )


def _validate_source(source: TaskSourceSpec) -> None:
    if not _exact_fields(source, TaskSourceSpec):
        _fail("task_source_spec_invalid")
    try:
        raw_root = os.fspath(source.repository_root)
    except Exception:
        _fail("task_source_spec_invalid")
    if (
        type(source.input_digest) is not str
        or _DIGEST_PATTERN.fullmatch(source.input_digest) is None
        or type(source.task_id) is not str
        or _TASK_ID_PATTERN.fullmatch(source.task_id) is None
        or type(raw_root) is not str
        or type(source.repository_root) is not type(Path())
        or type(source.commit_oid) is not str
        or _OID_PATTERN.fullmatch(source.commit_oid) is None
        or type(source.provisioning_class) is not str
        or source.provisioning_class
        != "operator_owned_trusted_git_local_clone"
        or type(source.operator_attested) is not bool
        or not source.operator_attested
        or type(source.local_clone_policy) is not str
        or source.local_clone_policy != "remote_or_no_local_or_no_hardlinks"
    ):
        _fail("task_source_spec_invalid")


def _validate_policy(policy: TaskSnapshotPolicy) -> None:
    if not _exact_fields(policy, TaskSnapshotPolicy):
        _fail("task_snapshot_policy_invalid")
    if (
        type(policy.policy_version) is not str
        or policy.policy_version != "task-object-materializer-v1"
    ):
        _fail("task_snapshot_policy_invalid")
    for name, ceiling in _POLICY_CEILINGS.items():
        value = getattr(policy, name)
        if type(value) is not int or value <= 0 or value > ceiling:
            _fail("task_snapshot_policy_invalid")
    if (
        policy.max_git_stderr_bytes > policy.max_git_stdout_bytes
        or policy.max_config_bytes > policy.max_git_stdout_bytes
        or policy.max_component_bytes > policy.max_relative_path_bytes
        or policy.max_file_bytes > policy.max_total_bytes
    ):
        _fail("task_snapshot_policy_invalid")


_PUBLIC_TASK_TREE_PATH_POLICY = TaskSnapshotPolicy()


def _validate_task_tree_entry(entry: TaskTreeEntry) -> None:
    if (
        not _exact_fields(entry, TaskTreeEntry)
        or type(entry.path) is not str
        or not entry.path
        or type(entry.git_mode) is not str
        or entry.git_mode not in ("100644", "100755")
        or type(entry.blob_oid) is not str
        or _OID_PATTERN.fullmatch(entry.blob_oid) is None
        or type(entry.size) is not int
        or entry.size < 0
        or type(entry.content_digest) is not str
        or _DIGEST_PATTERN.fullmatch(entry.content_digest) is None
    ):
        _fail("task_tree_invalid")
    components = _task_path_components(
        entry.path,
        _PUBLIC_TASK_TREE_PATH_POLICY,
    )
    _validate_task_path_exclusions(components)


def _task_path_components(
    path: str,
    policy: TaskSnapshotPolicy,
) -> Tuple[str, ...]:
    if type(path) is not str:
        _fail("task_tree_invalid")
    if len(path) > policy.max_relative_path_bytes:
        _fail("task_tree_limit")
    encoded = _encode_task_path(path)
    if len(encoded) > policy.max_relative_path_bytes:
        _fail("task_tree_limit")
    encoded_components = encoded.split(b"/")
    if (
        not path
        or path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or unicodedata.normalize("NFC", path) != path
        or any(
            unicodedata.category(character) in ("Cc", "Cf", "Cs")
            for character in path
        )
    ):
        _fail("task_tree_invalid")
    components = tuple(path.split("/"))
    if any(
        not component
        or component in (".", "..")
        or component.endswith(".")
        or component.endswith(" ")
        for component in components
    ):
        _fail("task_tree_invalid")
    if (
        len(encoded_components) > policy.max_tree_depth
        or any(
            len(component) > policy.max_component_bytes
            for component in encoded_components
        )
    ):
        _fail("task_tree_limit")
    return components


def _encode_task_path(path: str) -> bytes:
    try:
        return path.encode("utf-8")
    except UnicodeError:
        _fail("task_tree_invalid")


def _task_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _validate_raw_task_path_limits(
    raw_path: bytes,
    policy: TaskSnapshotPolicy,
) -> None:
    if len(raw_path) > policy.max_relative_path_bytes:
        _fail("task_tree_limit")
    raw_components = raw_path.split(b"/")
    if (
        len(raw_components) > policy.max_tree_depth
        or any(
            len(component) > policy.max_component_bytes
            for component in raw_components
        )
    ):
        _fail("task_tree_limit")


def _parse_task_blob_size(
    raw_size: bytes,
    policy: TaskSnapshotPolicy,
) -> int:
    try:
        decoded_size = raw_size.decode("ascii")
    except UnicodeDecodeError:
        _fail("task_tree_invalid")
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", decoded_size) is None:
        _fail("task_tree_invalid")
    maximum_size = str(policy.max_file_bytes)
    if (
        len(decoded_size) > len(maximum_size)
        or (
            len(decoded_size) == len(maximum_size)
            and decoded_size > maximum_size
        )
    ):
        _fail("task_blob_limit")
    return int(decoded_size)


def _validate_task_path_exclusions(components: Tuple[str, ...]) -> None:
    keys = tuple(_task_name_key(component) for component in components)
    final_key = keys[-1]
    if ".git" in keys:
        _fail("task_tree_invalid")
    if final_key in {
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
    }:
        _fail("task_tree_invalid")
    if any(
        key in {".codex", ".agents", ".claude", ".mcp"}
        for key in keys[:-1]
    ):
        _fail("task_tree_invalid")
    if (
        len(keys) > 1
        and keys[0] in {".git-hooks", "hooks", "plugins"}
    ):
        _fail("task_tree_invalid")


def _validate_parsed_task_tree(
    parsed: _ParsedTaskTree,
    policy: Optional[TaskSnapshotPolicy] = None,
) -> None:
    if (
        type(parsed) is not _ParsedTaskTree
        or set(vars(parsed))
        != {item.name for item in fields(_ParsedTaskTree)}
        or parsed.object_format not in ("sha1", "sha256")
        or type(parsed.records) is not tuple
        or type(parsed.directories) is not tuple
        or type(parsed.blob_sizes) is not tuple
    ):
        _fail("task_tree_invalid")
    oid_length = 40 if parsed.object_format == "sha1" else 64
    expected_directories = {"."}
    expected_blob_sizes = {}
    total_bytes = 0
    previous_path = None
    for record in parsed.records:
        if (
            type(record) is not _TaskTreeRecord
            or set(vars(record))
            != {item.name for item in fields(_TaskTreeRecord)}
            or type(record.path) is not str
            or not record.path
            or type(record.git_mode) is not str
            or record.git_mode not in ("100644", "100755")
            or type(record.blob_oid) is not str
            or len(record.blob_oid) != oid_length
            or _OID_PATTERN.fullmatch(record.blob_oid) is None
            or type(record.size) is not int
            or record.size < 0
        ):
            _fail("task_tree_invalid")
        if (
            previous_path is not None
            and previous_path.encode("utf-8")
            >= record.path.encode("utf-8")
        ):
            _fail("task_tree_invalid")
        previous_path = record.path
        components = record.path.split("/")
        for index in range(1, len(components)):
            expected_directories.add("/".join(components[:index]))
        existing_size = expected_blob_sizes.get(record.blob_oid)
        if existing_size is not None and existing_size != record.size:
            _fail("task_tree_invalid")
        expected_blob_sizes[record.blob_oid] = record.size
        total_bytes += record.size
    expected_blob_items = tuple(
        sorted(
            expected_blob_sizes.items(),
            key=lambda item: item[0].encode("ascii"),
        )
    )
    expected_directory_items = tuple(
        sorted(expected_directories, key=lambda item: item.encode("utf-8"))
    )
    if (
        parsed.directories != expected_directory_items
        or parsed.blob_sizes != expected_blob_items
        or type(parsed.file_count) is not int
        or parsed.file_count != len(parsed.records)
        or type(parsed.directory_count) is not int
        or parsed.directory_count != len(expected_directories)
        or type(parsed.tree_entry_count) is not int
        or parsed.tree_entry_count
        != parsed.file_count + parsed.directory_count
        or type(parsed.total_bytes) is not int
        or parsed.total_bytes != total_bytes
        or type(parsed.unique_blob_count) is not int
        or parsed.unique_blob_count != len(expected_blob_items)
        or type(parsed.unique_blob_bytes) is not int
        or parsed.unique_blob_bytes
        != sum(size for _, size in expected_blob_items)
    ):
        _fail("task_tree_invalid")
    if policy is not None:
        _validate_policy(policy)
        if (
            parsed.file_count > policy.max_files
            or parsed.tree_entry_count > policy.max_tree_entries
        ):
            _fail("task_tree_limit")
        if (
            any(size > policy.max_file_bytes for _, size in parsed.blob_sizes)
            or parsed.total_bytes > policy.max_total_bytes
            or parsed.unique_blob_count > policy.max_unique_blobs
            or parsed.unique_blob_bytes > policy.max_total_bytes
        ):
            _fail("task_blob_limit")


def _build_parsed_task_tree(
    records: Tuple[_TaskTreeRecord, ...],
    object_format: str,
    policy: TaskSnapshotPolicy,
) -> _ParsedTaskTree:
    _validate_policy(policy)
    if (
        type(records) is not tuple
        or type(object_format) is not str
        or object_format not in ("sha1", "sha256")
    ):
        _fail("task_tree_invalid")

    oid_length = 40 if object_format == "sha1" else 64
    directories = {"."}
    files = set()
    sibling_aliases = {}
    blob_sizes = {}
    total_bytes = 0
    unique_blob_bytes = 0
    for record in records:
        if (
            type(record) is not _TaskTreeRecord
            or not _exact_fields(record, _TaskTreeRecord)
            or type(record.path) is not str
            or type(record.git_mode) is not str
            or record.git_mode not in ("100644", "100755")
            or type(record.blob_oid) is not str
            or len(record.blob_oid) != oid_length
            or _OID_PATTERN.fullmatch(record.blob_oid) is None
            or type(record.size) is not int
            or record.size < 0
        ):
            _fail("task_tree_invalid")
        components = _task_path_components(record.path, policy)
        _validate_task_path_exclusions(components)
        if record.size > policy.max_file_bytes:
            _fail("task_blob_limit")

        parent_components = []
        for index, component in enumerate(components):
            parent = "/".join(parent_components) or "."
            alias_maps = sibling_aliases.setdefault(
                parent, ({}, {}, {})
            )
            keys = (
                component,
                unicodedata.normalize("NFC", component),
                _task_name_key(component),
            )
            for alias_map, key in zip(alias_maps, keys):
                previous = alias_map.get(key)
                if previous is not None and previous != component:
                    _fail("task_tree_invalid")
                alias_map[key] = component
            parent_components.append(component)
            current = "/".join(parent_components)
            is_final = index == len(components) - 1
            if is_final:
                if current in files or current in directories:
                    _fail("task_tree_invalid")
                files.add(current)
                if len(files) > policy.max_files:
                    _fail("task_tree_limit")
            else:
                if current in files:
                    _fail("task_tree_invalid")
                directories.add(current)
            if len(files) + len(directories) > policy.max_tree_entries:
                _fail("task_tree_limit")

        previous_size = blob_sizes.get(record.blob_oid)
        if previous_size is not None and previous_size != record.size:
            _fail("task_tree_invalid")
        if previous_size is None:
            blob_sizes[record.blob_oid] = record.size
            unique_blob_bytes += record.size
            if len(blob_sizes) > policy.max_unique_blobs:
                _fail("task_blob_limit")
            if unique_blob_bytes > policy.max_total_bytes:
                _fail("task_blob_limit")
        total_bytes += record.size
        if total_bytes > policy.max_total_bytes:
            _fail("task_blob_limit")

    sorted_records = tuple(
        sorted(records, key=lambda item: item.path.encode("utf-8"))
    )
    sorted_directories = tuple(
        sorted(directories, key=lambda item: item.encode("utf-8"))
    )
    sorted_blob_sizes = tuple(
        sorted(
            blob_sizes.items(),
            key=lambda item: item[0].encode("ascii"),
        )
    )
    parsed = _ParsedTaskTree(
        object_format=object_format,
        records=sorted_records,
        directories=sorted_directories,
        blob_sizes=sorted_blob_sizes,
        file_count=len(sorted_records),
        directory_count=len(sorted_directories),
        tree_entry_count=len(sorted_records) + len(sorted_directories),
        total_bytes=total_bytes,
        unique_blob_count=len(sorted_blob_sizes),
        unique_blob_bytes=unique_blob_bytes,
    )
    _validate_parsed_task_tree(parsed, policy)
    return parsed


def _parse_task_tree(
    output: bytes,
    object_format: str,
    policy: TaskSnapshotPolicy,
) -> _ParsedTaskTree:
    _validate_policy(policy)
    if (
        type(output) is not bytes
        or type(object_format) is not str
        or object_format not in ("sha1", "sha256")
    ):
        _fail("task_tree_invalid")
    if output == b"":
        return _build_parsed_task_tree((), object_format, policy)
    if not output.endswith(b"\0"):
        _fail("task_tree_invalid")
    raw_records = output[:-1].split(b"\0", policy.max_files)
    if (
        len(raw_records) == policy.max_files + 1
        and b"\0" in raw_records[-1]
    ):
        raw_records[-1] = raw_records[-1].split(b"\0", 1)[0]
    if not raw_records or any(record == b"" for record in raw_records):
        _fail("task_tree_invalid")

    oid_length = 40 if object_format == "sha1" else 64
    records = []
    for raw_record in raw_records:
        fields_bytes = raw_record.split(b"\t", 4)
        if len(fields_bytes) != 5 or any(
            field == b"" for field in fields_bytes[:4]
        ):
            _fail("task_tree_invalid")
        raw_path = fields_bytes[4]
        _validate_raw_task_path_limits(raw_path, policy)
        try:
            git_mode, object_type, blob_oid = (
                field.decode("ascii") for field in fields_bytes[:3]
            )
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, UnicodeEncodeError):
            _fail("task_tree_invalid")
        if (
            git_mode not in ("100644", "100755")
            or object_type != "blob"
            or len(blob_oid) != oid_length
            or _OID_PATTERN.fullmatch(blob_oid) is None
        ):
            _fail("task_tree_invalid")
        records.append(
            _TaskTreeRecord(
                path=path,
                git_mode=git_mode,
                blob_oid=blob_oid,
                size=_parse_task_blob_size(fields_bytes[3], policy),
            )
        )
    return _build_parsed_task_tree(
        tuple(records),
        object_format,
        policy,
    )


def _load_task_blobs(
    git_dir: Path,
    config_path: Path,
    parsed: _ParsedTaskTree,
    policy: TaskSnapshotPolicy,
    *,
    capture_deadline: Optional[float] = None,
) -> Tuple[Tuple[TaskTreeEntry, ...], Mapping[str, bytes]]:
    _validate_parsed_task_tree(parsed, policy)
    contents = {}
    content_digests = {}
    algorithm = hashlib.sha1 if parsed.object_format == "sha1" else hashlib.sha256
    for blob_oid, declared_size in parsed.blob_sizes:
        if capture_deadline is not None:
            _check_capture_deadline(capture_deadline)
        content = _run_git(
            git_dir,
            config_path,
            policy,
            "cat-blob",
            blob_oid,
            capture_deadline=capture_deadline,
            stdout_limit=declared_size,
        )
        if type(content) is not bytes or len(content) != declared_size:
            _fail("task_blob_invalid")
        framed = (
            b"blob "
            + str(len(content)).encode("ascii")
            + b"\0"
            + content
        )
        if algorithm(framed).hexdigest() != blob_oid:
            _fail("task_blob_invalid")
        contents[blob_oid] = bytes(content)
        content_digests[blob_oid] = (
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
        if capture_deadline is not None:
            _check_capture_deadline(capture_deadline)
    entries = tuple(
        TaskTreeEntry(
            path=record.path,
            git_mode=record.git_mode,
            blob_oid=record.blob_oid,
            size=record.size,
            content_digest=content_digests[record.blob_oid],
        )
        for record in parsed.records
    )
    return entries, MappingProxyType(dict(contents))


def _task_entry_document(
    commit_oid: str,
    tree_oid: str,
    parsed: _ParsedTaskTree,
    entries: Tuple[TaskTreeEntry, ...],
) -> Dict[str, object]:
    _validate_parsed_task_tree(parsed)
    oid_length = 40 if parsed.object_format == "sha1" else 64
    if (
        type(commit_oid) is not str
        or len(commit_oid) != oid_length
        or _OID_PATTERN.fullmatch(commit_oid) is None
        or type(tree_oid) is not str
        or len(tree_oid) != oid_length
        or _OID_PATTERN.fullmatch(tree_oid) is None
        or type(entries) is not tuple
        or len(entries) != len(parsed.records)
    ):
        _fail("task_tree_invalid")
    digest_by_oid = {}
    entry_documents = []
    total_bytes = 0
    for record, entry in zip(parsed.records, entries):
        _validate_task_tree_entry(entry)
        if (
            entry.path != record.path
            or entry.git_mode != record.git_mode
            or entry.blob_oid != record.blob_oid
            or entry.size != record.size
        ):
            _fail("task_tree_invalid")
        previous_digest = digest_by_oid.get(entry.blob_oid)
        if (
            previous_digest is not None
            and previous_digest != entry.content_digest
        ):
            _fail("task_blob_invalid")
        digest_by_oid[entry.blob_oid] = entry.content_digest
        total_bytes += entry.size
        entry_documents.append(
            {
                "blob_oid": entry.blob_oid,
                "content_digest": entry.content_digest,
                "git_mode": entry.git_mode,
                "path": entry.path,
                "size": entry.size,
            }
        )
    if (
        total_bytes != parsed.total_bytes
        or len(digest_by_oid) != parsed.unique_blob_count
    ):
        _fail("task_blob_invalid")
    return {
        "commit_oid": commit_oid,
        "directory_count": parsed.directory_count,
        "document_type": "task-tree-entries-v1",
        "entries": entry_documents,
        "file_count": parsed.file_count,
        "logical_total_bytes": parsed.total_bytes,
        "object_format": parsed.object_format,
        "schema_version": 1,
        "tree_entry_count": parsed.tree_entry_count,
        "tree_oid": tree_oid,
        "unique_blob_bytes": parsed.unique_blob_bytes,
        "unique_blob_count": parsed.unique_blob_count,
    }


def _task_entry_digest(
    commit_oid: str,
    tree_oid: str,
    parsed: _ParsedTaskTree,
    entries: Tuple[TaskTreeEntry, ...],
) -> str:
    return _digest(
        _task_entry_document(commit_oid, tree_oid, parsed, entries)
    )


def _valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and _DIGEST_PATTERN.fullmatch(value) is not None
    )


def _receipt_payload_snapshot(
    value: object,
    expected_keys: frozenset,
) -> Dict[str, object]:
    if type(value) is not MappingProxyType:
        _fail("task_snapshot_receipt_invalid")
    try:
        iterator = iter(value.items())
    except Exception:
        _fail("task_snapshot_receipt_invalid")
    result = {}
    max_key_length = max(len(key) for key in expected_keys)
    for unused_index in range(len(expected_keys) + 1):
        try:
            item = next(iterator)
        except StopIteration:
            break
        except Exception:
            _fail("task_snapshot_receipt_invalid")
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or len(item[0]) > max_key_length
            or item[0] not in expected_keys
            or item[0] in result
            or (
                item[1] is not None
                and type(item[1]) not in (bool, int, str)
            )
        ):
            _fail("task_snapshot_receipt_invalid")
        result[item[0]] = item[1]
        if len(result) > len(expected_keys):
            _fail("task_snapshot_receipt_invalid")
    if frozenset(result) != expected_keys:
        _fail("task_snapshot_receipt_invalid")
    return result


def _validate_receipt_payload_bounds(
    payload: Mapping[str, object],
    expected_type: str,
    policy: TaskSnapshotPolicy,
) -> None:
    if expected_type == "task_source_trust":
        digest_names = (
            "source_identity_before_digest",
            "object_topology_before_digest",
            "source_identity_after_digest",
            "object_topology_after_digest",
            "git_process_policy_digest",
        )
        if (
            type(payload["task_id"]) is not str
            or len(payload["task_id"]) > 64
            or _TASK_ID_PATTERN.fullmatch(payload["task_id"]) is None
            or payload["provisioning_class"]
            != "operator_owned_trusted_git_local_clone"
            or type(payload["provisioning_class"]) is not str
            or payload["operator_attested"] is not True
            or type(payload["operator_attested"]) is not bool
            or payload["local_clone_policy"]
            != "remote_or_no_local_or_no_hardlinks"
            or type(payload["local_clone_policy"]) is not str
            or type(payload["object_format"]) is not str
            or payload["object_format"] not in ("sha1", "sha256")
            or any(
                not _valid_digest(payload[name])
                for name in digest_names
            )
            or payload["source_identity_before_digest"]
            != payload["source_identity_after_digest"]
            or payload["object_topology_before_digest"]
            != payload["object_topology_after_digest"]
            or type(payload["inventory_file_count"]) is not int
            or payload["inventory_file_count"] < 0
            or payload["inventory_file_count"]
            > policy.max_files
            or type(payload["inventory_total_bytes"]) is not int
            or payload["inventory_total_bytes"] < 0
            or payload["inventory_total_bytes"]
            > policy.max_object_store_bytes
        ):
            _fail("task_snapshot_receipt_invalid")
        return
    if expected_type == "task_snapshot":
        object_format = payload["object_format"]
        oid_length = 40 if object_format == "sha1" else 64
        if (
            type(payload["task_id"]) is not str
            or len(payload["task_id"]) > 64
            or _TASK_ID_PATTERN.fullmatch(payload["task_id"]) is None
            or type(object_format) is not str
            or object_format not in ("sha1", "sha256")
            or any(
                type(payload[name]) is not str
                or len(payload[name]) != oid_length
                or _OID_PATTERN.fullmatch(payload[name]) is None
                for name in ("commit_oid", "tree_oid")
            )
            or any(
                not _valid_digest(payload[name])
                for name in (
                    "entry_digest",
                    "materialized_tree_digest",
                    "source_trust_receipt_digest",
                )
            )
            or type(payload["materializer_policy_version"]) is not str
            or payload["materializer_policy_version"]
            != "task-object-materializer-v1"
            or type(payload["file_count"]) is not int
            or payload["file_count"] < 0
            or payload["file_count"] > policy.max_files
            or type(payload["total_bytes"]) is not int
            or payload["total_bytes"] < 0
            or payload["total_bytes"] > policy.max_total_bytes
        ):
            _fail("task_snapshot_receipt_invalid")
        return
    _fail("task_snapshot_receipt_invalid")


def _reconstruct_receipt(
    receipt: object,
    expected_type: str,
    policy: TaskSnapshotPolicy,
) -> CanonicalReceipt:
    try:
        expected_keys = _TASK_RECEIPT_PAYLOAD_KEYS.get(expected_type)
        if (
            not _exact_fields(receipt, CanonicalReceipt)
            or type(expected_type) is not str
            or type(expected_keys) is not frozenset
            or receipt.receipt_type != expected_type
            or type(receipt.receipt_type) is not str
            or not _valid_digest(receipt.input_digest)
            or (
                receipt.plan_digest is not None
                and not _valid_digest(receipt.plan_digest)
            )
            or (
                receipt.previous_record_hash is not None
                and not _valid_digest(receipt.previous_record_hash)
            )
            or type(receipt.canonical_bytes) is not bytes
            or not _valid_digest(receipt.receipt_digest)
            or type(receipt.payload) is not MappingProxyType
        ):
            _fail("task_snapshot_receipt_invalid")
        payload = _receipt_payload_snapshot(receipt.payload, expected_keys)
        _validate_receipt_payload_bounds(payload, expected_type, policy)
        reconstructed = make_receipt(
            receipt.receipt_type,
            receipt.input_digest,
            receipt.plan_digest,
            receipt.previous_record_hash,
            payload,
        )
        reconstructed_payload = _receipt_payload_snapshot(
            reconstructed.payload,
            expected_keys,
        )
        _validate_receipt_payload_bounds(
            reconstructed_payload,
            expected_type,
            policy,
        )
        if (
            not _exact_fields(reconstructed, CanonicalReceipt)
            or reconstructed.receipt_type != receipt.receipt_type
            or reconstructed.input_digest != receipt.input_digest
            or reconstructed.plan_digest != receipt.plan_digest
            or reconstructed.previous_record_hash
            != receipt.previous_record_hash
            or reconstructed.canonical_bytes != receipt.canonical_bytes
            or reconstructed.receipt_digest != receipt.receipt_digest
            or reconstructed_payload != payload
        ):
            _fail("task_snapshot_receipt_invalid")
        return reconstructed
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _captured_tree_from_entries(
    captured: CapturedTaskObjects,
) -> _ParsedTaskTree:
    if (
        type(captured.entries) is not tuple
        or len(captured.entries) > captured.policy.max_files
        or any(
            not _exact_fields(entry, TaskTreeEntry)
            for entry in captured.entries
        )
    ):
        _fail("task_snapshot_receipt_invalid")
    expected_records = tuple(
        _TaskTreeRecord(
            path=entry.path,
            git_mode=entry.git_mode,
            blob_oid=entry.blob_oid,
            size=entry.size,
        )
        for entry in captured.entries
    )
    parsed = _build_parsed_task_tree(
        expected_records,
        captured.object_format,
        captured.policy,
    )
    if parsed.records != expected_records:
        _fail("task_snapshot_receipt_invalid")
    return parsed


def _validate_captured_objects(
    captured: CapturedTaskObjects,
) -> CanonicalReceipt:
    if not _exact_fields(captured, CapturedTaskObjects):
        _fail("task_snapshot_receipt_invalid")
    _validate_source(captured.source)
    _validate_policy(captured.policy)
    if (
        type(captured.entries) is not tuple
        or len(captured.entries) > captured.policy.max_files
    ):
        _fail("task_snapshot_receipt_invalid")
    receipt = _reconstruct_receipt(
        captured.source_trust_receipt,
        "task_source_trust",
        captured.policy,
    )
    oid_length = 40 if captured.object_format == "sha1" else 64
    if (
        type(captured.object_format) is not str
        or captured.object_format not in ("sha1", "sha256")
        or type(captured.commit_oid) is not str
        or len(captured.commit_oid) != oid_length
        or _OID_PATTERN.fullmatch(captured.commit_oid) is None
        or captured.commit_oid != captured.source.commit_oid
        or type(captured.tree_oid) is not str
        or len(captured.tree_oid) != oid_length
        or _OID_PATTERN.fullmatch(captured.tree_oid) is None
        or type(captured.entry_digest) is not str
        or _DIGEST_PATTERN.fullmatch(captured.entry_digest) is None
        or type(captured.entries) is not tuple
        or type(captured.blobs) is not MappingProxyType
        or type(captured.file_count) is not int
        or captured.file_count < 0
        or type(captured.total_bytes) is not int
        or captured.total_bytes < 0
        or type(captured.unique_blob_count) is not int
        or captured.unique_blob_count < 0
        or type(captured.unique_blob_bytes) is not int
        or captured.unique_blob_bytes < 0
    ):
        _fail("task_snapshot_receipt_invalid")
    parsed = _captured_tree_from_entries(captured)
    blob_snapshot = _snapshot_captured_blobs(
        captured.blobs,
        captured.policy,
        captured.object_format,
    )
    blob_items = tuple(blob_snapshot.items())
    if (
        tuple(key for key, unused_value in blob_items)
        != tuple(key for key, unused_size in parsed.blob_sizes)
        or any(type(key) is not str for key, unused_value in blob_items)
        or any(type(value) is not bytes for unused_key, value in blob_items)
    ):
        _fail("task_snapshot_receipt_invalid")
    expected_sizes = dict(parsed.blob_sizes)
    content_digests = {}
    git_algorithm = (
        hashlib.sha1
        if captured.object_format == "sha1"
        else hashlib.sha256
    )
    for blob_oid, content in blob_items:
        if len(content) != expected_sizes[blob_oid]:
            _fail("task_snapshot_receipt_invalid")
        framed = (
            b"blob "
            + str(len(content)).encode("ascii")
            + b"\0"
            + content
        )
        if git_algorithm(framed).hexdigest() != blob_oid:
            _fail("task_snapshot_receipt_invalid")
        content_digests[blob_oid] = (
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
    if any(
        entry.content_digest != content_digests.get(entry.blob_oid)
        for entry in captured.entries
    ):
        _fail("task_snapshot_receipt_invalid")
    if (
        captured.file_count != parsed.file_count
        or captured.total_bytes != parsed.total_bytes
        or captured.unique_blob_count != parsed.unique_blob_count
        or captured.unique_blob_bytes != parsed.unique_blob_bytes
        or captured.entry_digest
        != _task_entry_digest(
            captured.commit_oid,
            captured.tree_oid,
            parsed,
            captured.entries,
        )
    ):
        _fail("task_snapshot_receipt_invalid")
    payload = receipt.payload
    expected_payload = {
        "git_process_policy_digest": _process_policy_digest(captured.policy),
        "local_clone_policy": captured.source.local_clone_policy,
        "object_format": captured.object_format,
        "operator_attested": captured.source.operator_attested,
        "provisioning_class": captured.source.provisioning_class,
        "task_id": captured.source.task_id,
    }
    if (
        receipt.input_digest != captured.source.input_digest
        or receipt.plan_digest is not None
        or receipt.previous_record_hash is not None
        or any(
            payload.get(key) != value
            for key, value in expected_payload.items()
        )
        or payload["inventory_file_count"]
        > captured.policy.max_files
        or payload["inventory_total_bytes"]
        > captured.policy.max_object_store_bytes
    ):
        _fail("task_snapshot_receipt_invalid")
    return receipt


def _snapshot_captured_blobs(
    value: object,
    policy: TaskSnapshotPolicy,
    object_format: str,
) -> Mapping[str, bytes]:
    if not isinstance(value, Mapping):
        _fail("task_snapshot_receipt_invalid")
    oid_length = 40 if object_format == "sha1" else 64
    try:
        iterator = iter(value.items())
    except Exception:
        _fail("task_snapshot_receipt_invalid")
    snapshot = {}
    for unused_index in range(policy.max_unique_blobs + 1):
        try:
            item = next(iterator)
        except StopIteration:
            break
        except Exception:
            _fail("task_snapshot_receipt_invalid")
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or len(item[0]) != oid_length
            or _OID_PATTERN.fullmatch(item[0]) is None
            or type(item[1]) is not bytes
            or item[0] in snapshot
        ):
            _fail("task_snapshot_receipt_invalid")
        snapshot[item[0]] = bytes(item[1])
        if len(snapshot) > policy.max_unique_blobs:
            _fail("task_snapshot_receipt_invalid")
    return MappingProxyType(
        {
            key: snapshot[key]
            for key in sorted(snapshot)
        }
    )


def _detach_and_validate_captured_objects(
    captured: CapturedTaskObjects,
) -> None:
    try:
        if not _exact_fields(captured, CapturedTaskObjects):
            _fail("task_snapshot_receipt_invalid")
        if not _exact_fields(captured.source, TaskSourceSpec):
            _fail("task_snapshot_receipt_invalid")
        source = TaskSourceSpec(
            input_digest=captured.source.input_digest,
            task_id=captured.source.task_id,
            repository_root=Path(os.fspath(captured.source.repository_root)),
            commit_oid=captured.source.commit_oid,
            provisioning_class=captured.source.provisioning_class,
            operator_attested=captured.source.operator_attested,
            local_clone_policy=captured.source.local_clone_policy,
        )
        if not _exact_fields(captured.policy, TaskSnapshotPolicy):
            _fail("task_snapshot_receipt_invalid")
        policy = TaskSnapshotPolicy(
            **{
                item.name: getattr(captured.policy, item.name)
                for item in fields(TaskSnapshotPolicy)
            }
        )
        if type(captured.entries) is not tuple:
            _fail("task_snapshot_receipt_invalid")
        if len(captured.entries) > policy.max_files:
            _fail("task_snapshot_receipt_invalid")
        entries = tuple(
            TaskTreeEntry(
                path=entry.path,
                git_mode=entry.git_mode,
                blob_oid=entry.blob_oid,
                size=entry.size,
                content_digest=entry.content_digest,
            )
            for entry in captured.entries
            if _exact_fields(entry, TaskTreeEntry)
        )
        if len(entries) != len(captured.entries):
            _fail("task_snapshot_receipt_invalid")
        if (
            type(captured.object_format) is not str
            or captured.object_format not in ("sha1", "sha256")
        ):
            _fail("task_snapshot_receipt_invalid")
        blobs = _snapshot_captured_blobs(
            captured.blobs,
            policy,
            captured.object_format,
        )
        object.__setattr__(captured, "source", source)
        object.__setattr__(captured, "policy", policy)
        object.__setattr__(captured, "entries", entries)
        object.__setattr__(captured, "blobs", blobs)
        receipt = _validate_captured_objects(captured)
        object.__setattr__(captured, "source_trust_receipt", receipt)
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _detach_and_validate_materialized_snapshot(
    snapshot: MaterializedTaskSnapshot,
) -> None:
    try:
        if not _exact_fields(snapshot, MaterializedTaskSnapshot):
            _fail("task_snapshot_receipt_invalid")
        raw_root = os.fspath(snapshot.target_root)
        if (
            type(raw_root) is not str
            or not os.path.isabs(raw_root)
            or os.path.normpath(raw_root) != raw_root
            or type(snapshot.target_identity_digest) is not str
            or not _valid_digest(snapshot.target_identity_digest)
            or type(snapshot.materialized_tree_digest) is not str
            or not _valid_digest(snapshot.materialized_tree_digest)
            or type(snapshot.file_count) is not int
            or snapshot.file_count < 0
            or snapshot.file_count
            > _PUBLIC_TASK_TREE_PATH_POLICY.max_files
            or type(snapshot.total_bytes) is not int
            or snapshot.total_bytes < 0
            or snapshot.total_bytes
            > _PUBLIC_TASK_TREE_PATH_POLICY.max_total_bytes
        ):
            _fail("task_snapshot_receipt_invalid")
        receipt = _reconstruct_receipt(
            snapshot.snapshot_receipt,
            "task_snapshot",
            _PUBLIC_TASK_TREE_PATH_POLICY,
        )
        payload = receipt.payload
        if (
            receipt.plan_digest is not None
            or receipt.previous_record_hash is not None
            or payload["materialized_tree_digest"]
            != snapshot.materialized_tree_digest
            or payload["file_count"] != snapshot.file_count
            or payload["total_bytes"] != snapshot.total_bytes
        ):
            _fail("task_snapshot_receipt_invalid")
        object.__setattr__(snapshot, "target_root", Path(raw_root))
        object.__setattr__(snapshot, "snapshot_receipt", receipt)
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _require_supported_platform() -> None:
    try:
        popen_parameters = inspect.signature(subprocess.Popen).parameters
        supported = (
            sys.version_info >= (3, 9)
            and os.name == "posix"
            and sys.platform in ("darwin", "linux")
            and all(
                hasattr(os, name)
                for name in (
                    "getuid",
                    "killpg",
                    "getpgid",
                    "O_DIRECTORY",
                    "O_NOFOLLOW",
                    "O_CLOEXEC",
                )
            )
            and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd
            and os.scandir in os.supports_fd
            and "start_new_session" in popen_parameters
            and all(
                hasattr(os.stat_result, name)
                for name in ()
            )
        )
        probe = os.stat(os.sep)
        supported = supported and all(
            hasattr(probe, name)
            for name in ("st_uid", "st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns")
        )
        supported = supported and all(
            hasattr(signal, name) for name in ("SIGTERM", "SIGKILL")
        )
    except (OSError, TypeError, ValueError):
        supported = False
    if not supported:
        _fail("task_source_platform_unsupported")


def _identity(metadata: os.stat_result, kind: str) -> Dict[str, object]:
    values = {
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
    if any(type(value) is not int or value < 0 for key, value in values.items()
           if key != "kind"):
        _fail("task_source_root_invalid")
    return values


def _physical_identity_key(
    metadata: os.stat_result,
    kind: str,
) -> Tuple[int, int, str]:
    if (
        type(metadata.st_dev) is not int
        or metadata.st_dev < 0
        or type(metadata.st_ino) is not int
        or metadata.st_ino < 0
        or type(kind) is not str
        or kind not in ("directory", "file")
    ):
        _fail("task_source_root_invalid")
    return (metadata.st_dev, metadata.st_ino, kind)


def _stable_ancestor_identity(
    metadata: os.stat_result,
) -> Dict[str, object]:
    values = {
        "dev": metadata.st_dev,
        "gid": metadata.st_gid,
        "ino": metadata.st_ino,
        "kind": "directory",
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }
    if any(
        type(value) is not int or value < 0
        for key, value in values.items()
        if key != "kind"
    ):
        _fail("task_source_root_invalid")
    return values


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    names = (
        "st_mode",
        "st_dev",
        "st_ino",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(first, name) == getattr(second, name) for name in names)


def _validate_physical_root(repository_root: Path) -> Tuple[Dict[str, object], ...]:
    try:
        raw_root = os.fspath(repository_root)
        if (
            type(raw_root) is not str
            or not os.path.isabs(raw_root)
            or os.path.normpath(raw_root) != raw_root
            or os.path.realpath(raw_root) != raw_root
        ):
            _fail("task_source_root_invalid")
        components = Path(raw_root).parts
        paths = [Path(components[0])]
        for component in components[1:]:
            paths.append(paths[-1] / component)
        metadata = [os.lstat(path) for path in paths]
        uid = os.getuid()
        for index, item in enumerate(metadata):
            if not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode):
                _fail("task_source_root_invalid")
            if index == len(metadata) - 1:
                if item.st_uid != uid or item.st_mode & 0o022:
                    _fail("task_source_root_invalid")
                continue
            if item.st_uid not in (0, uid):
                _fail("task_source_root_invalid")
            if item.st_mode & 0o022:
                sticky_exception = (
                    item.st_uid == 0
                    and bool(item.st_mode & stat.S_ISVTX)
                    and index + 1 < len(metadata)
                    and metadata[index + 1].st_uid == uid
                    and not metadata[index + 1].st_mode & 0o022
                )
                if not sticky_exception:
                    _fail("task_source_root_invalid")
        return tuple(
            _stable_ancestor_identity(item) for item in metadata[:-1]
        )
    except TaskSnapshotError:
        raise
    except Exception:
        _fail("task_source_root_invalid")


def _digest(document: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _close_fd_once(descriptor: int) -> bool:
    try:
        os.close(descriptor)
        return True
    except OSError:
        return False


def _close_source_descriptors(descriptors: Sequence[int]) -> bool:
    cleanup_ok = True
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        cleanup_ok = _guard_cleanup_boolean(
            _close_fd_once,
            descriptor,
        ) and cleanup_ok
    return cleanup_ok


def _close_source_iterator(iterator: object) -> bool:
    try:
        iterator.close()
    except Exception:
        return False
    return True


def _close_source_resources(
    iterator: Optional[object],
    descriptors: Sequence[int],
) -> bool:
    cleanup_ok = True
    if iterator is not None:
        cleanup_ok = _guard_cleanup_boolean(
            _close_source_iterator,
            iterator,
        ) and cleanup_ok
    cleanup_ok = _guard_cleanup_boolean(
        _close_source_descriptors,
        descriptors,
    ) and cleanup_ok
    return cleanup_ok


@dataclass
class _SourceResourceCell:
    descriptor: int = field(default=-1, repr=False)
    iterator: Optional[object] = field(default=None, repr=False)
    inflight_descriptor: int = field(default=-1, repr=False)
    inflight_iterator: Optional[object] = field(default=None, repr=False)
    descriptor_close_started: bool = field(default=False, repr=False)
    iterator_close_started: bool = field(default=False, repr=False)
    uncertain: bool = field(default=False, repr=False)


class _SourceResourceOwner:
    def __init__(
        self,
        descriptor: int = -1,
        iterator: Optional[object] = None,
    ) -> None:
        self._cell = _SourceResourceCell(
            descriptor=descriptor,
            iterator=iterator,
        )

    @property
    def descriptor(self) -> int:
        return self._cell.descriptor

    @descriptor.setter
    def descriptor(self, value: int) -> None:
        self._cell.descriptor = value

    @property
    def iterator(self) -> Optional[object]:
        return self._cell.iterator

    @iterator.setter
    def iterator(self, value: Optional[object]) -> None:
        self._cell.iterator = value

    @property
    def _uncertain(self) -> bool:
        return self._cell.uncertain

    def has_resources(self) -> bool:
        return (
            self._cell.iterator is not None
            or self._cell.inflight_iterator is not None
            or self._cell.descriptor >= 0
            or self._cell.inflight_descriptor >= 0
        )

    def mark_uncertain(self) -> None:
        self._cell.uncertain = True

    def share_from(self, source: "_SourceResourceOwner") -> bool:
        if (
            type(source) is not _SourceResourceOwner
            or self.has_resources()
            or self._cell.uncertain
            or source.iterator is not None
            or source.descriptor < 0
            or source._cell.inflight_iterator is not None
            or source._cell.inflight_descriptor >= 0
            or source._cell.uncertain
        ):
            return False
        self._cell = source._cell
        return True

    def close_iterator(self) -> bool:
        cell = self._cell
        if cell.iterator is None and cell.inflight_iterator is None:
            return not cell.uncertain
        previous_uncertainty = cell.uncertain
        cell.uncertain = True
        cleanup_ok = True
        if cell.iterator is not None:
            if cell.inflight_iterator is not None:
                return False
            cell.inflight_iterator, cell.iterator = cell.iterator, None
        if cell.inflight_iterator is not None:
            if cell.iterator_close_started:
                cell.inflight_iterator = None
                cleanup_ok = False
            else:
                try:
                    # Keep the flag, close call, and retirement on one Python
                    # line so a line-event interruption can only occur before
                    # the attempt. Opcode-level injection is outside the
                    # recoverable pure-Python ownership model.
                    cell.iterator_close_started = True; cleanup_ok = _close_source_iterator(cell.inflight_iterator); cell.inflight_iterator = None
                except Exception:
                    if not cell.iterator_close_started:
                        self.close_iterator()
                    else:
                        cell.inflight_iterator = None
                    cleanup_ok = False
        cell.uncertain = previous_uncertainty or not cleanup_ok
        return not cell.uncertain

    def close_descriptor(self) -> bool:
        cell = self._cell
        if cell.descriptor < 0 and cell.inflight_descriptor < 0:
            return not cell.uncertain
        previous_uncertainty = cell.uncertain
        cell.uncertain = True
        cleanup_ok = True
        if cell.descriptor >= 0:
            if cell.inflight_descriptor >= 0:
                return False
            cell.inflight_descriptor, cell.descriptor = cell.descriptor, -1
        if cell.inflight_descriptor >= 0:
            if cell.descriptor_close_started:
                cell.inflight_descriptor = -1
                cleanup_ok = False
            else:
                try:
                    # See close_iterator(): this is line-event atomic, not
                    # bytecode- or asynchronously atomic.
                    cell.descriptor_close_started = True; cleanup_ok = _close_fd_once(cell.inflight_descriptor); cell.inflight_descriptor = -1
                except OSError:
                    cell.inflight_descriptor = -1
                    cleanup_ok = False
                except Exception:
                    if not cell.descriptor_close_started:
                        self.close_descriptor()
                    else:
                        cell.inflight_descriptor = -1
                    cleanup_ok = False
        cell.uncertain = previous_uncertainty or not cleanup_ok
        return not cell.uncertain

    def close(self) -> bool:
        iterator_ok = _guard_cleanup_boolean(self.close_iterator)
        descriptor_ok = _guard_cleanup_boolean(self.close_descriptor)
        return iterator_ok and descriptor_ok and not self._cell.uncertain


def _drain_source_owner_snapshot(
    owners: Sequence[_SourceResourceOwner],
) -> bool:
    cleanup_ok = True
    for owner in owners:
        closed = _guard_cleanup_boolean(owner.close)
        if owner.has_resources():
            closed = _guard_cleanup_boolean(owner.close) and closed
        cleanup_ok = closed and cleanup_ok
    return cleanup_ok


def _close_source_owners(
    owners: Sequence[_SourceResourceOwner],
) -> bool:
    try:
        owner_snapshot = tuple(reversed(tuple(owners)))
        return _drain_source_owner_snapshot(owner_snapshot)
    except Exception:
        try:
            recovery_snapshot = owner_snapshot
        except UnboundLocalError:
            recovery_snapshot = tuple(reversed(tuple(owners)))
        _guard_cleanup_boolean(
            _drain_source_owner_snapshot,
            recovery_snapshot,
        )
        return False


def _require_directory_metadata(
    metadata: os.stat_result,
    device: int,
    code: str,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_dev != device
        or metadata.st_mode & 0o022
    ):
        _fail(code)


def _require_file_metadata(
    metadata: os.stat_result,
    device: int,
    code: str,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_dev != device
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
    ):
        _fail(code)


def _open_root_descriptor(
    repository_root: Path,
    owner: _SourceResourceOwner,
) -> Tuple[os.stat_result, Tuple[Dict[str, object], ...]]:
    lexical_ancestors = _validate_physical_root(repository_root)
    raw_root = os.fspath(repository_root)
    if type(owner) is not _SourceResourceOwner or owner.has_resources():
        _fail("task_source_root_invalid")
    try:
        lexical_root = os.lstat(raw_root)
    except OSError:
        _fail("task_source_root_invalid")
    components = Path(raw_root).parts[1:]
    current_owner = _SourceResourceOwner()
    success = False
    try:
        current_owner.descriptor = os.open(os.sep, _directory_flags())
        parent_metadata = os.fstat(current_owner.descriptor)
        actual_ancestors = []
        for index, component in enumerate(components):
            observed = os.stat(
                component,
                dir_fd=current_owner.descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
                _fail("task_source_root_invalid")
            if parent_metadata.st_uid not in (0, os.getuid()):
                _fail("task_source_root_invalid")
            if parent_metadata.st_mode & 0o022:
                sticky_exception = (
                    parent_metadata.st_uid == 0
                    and bool(parent_metadata.st_mode & stat.S_ISVTX)
                    and observed.st_uid == os.getuid()
                    and not observed.st_mode & 0o022
                )
                if not sticky_exception:
                    _fail("task_source_root_invalid")
            parent_identity = _stable_ancestor_identity(parent_metadata)
            if parent_identity != lexical_ancestors[index]:
                _fail("task_source_changed")
            actual_ancestors.append(parent_identity)
            child_owner = _SourceResourceOwner()
            try:
                child_owner.descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_owner.descriptor,
                )
                opened = os.fstat(child_owner.descriptor)
                if index == len(components) - 1:
                    if not _same_identity(observed, opened):
                        _fail("task_source_changed")
                elif (
                    _stable_ancestor_identity(observed)
                    != _stable_ancestor_identity(opened)
                ):
                    _fail("task_source_changed")
                if (
                    _stable_ancestor_identity(parent_metadata)
                    != _stable_ancestor_identity(
                        os.fstat(current_owner.descriptor)
                    )
                ):
                    _fail("task_source_changed")
                if not _guard_cleanup_boolean(
                    current_owner.close_descriptor,
                ):
                    _fail("task_snapshot_cleanup_required")
                current_owner, child_owner = child_owner, current_owner
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not _guard_cleanup_boolean(
                    child_owner.close,
                ):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
            parent_metadata = opened
        if not components:
            _fail("task_source_root_invalid")
        if not _same_identity(lexical_root, parent_metadata):
            _fail("task_source_changed")
        _require_directory_metadata(
            parent_metadata, parent_metadata.st_dev, "task_source_root_invalid"
        )
        if not owner.share_from(current_owner):
            _fail("task_snapshot_cleanup_required")
        success = True
        return parent_metadata, tuple(actual_ancestors)
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_root_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        cleanup_ok = True if success else _guard_cleanup_boolean(
            current_owner.close
        )
        if not success and owner.has_resources():
            cleanup_ok = _guard_cleanup_boolean(
                owner.close
            ) and cleanup_ok
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _open_child_directory(
    parent_fd: int,
    name: str,
    device: int,
    code: str,
    owner: _SourceResourceOwner,
) -> os.stat_result:
    if type(owner) is not _SourceResourceOwner or owner.has_resources():
        _fail(code)
    success = False
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_directory_metadata(observed, device, code)
        owner.descriptor = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
        opened = os.fstat(owner.descriptor)
        if not _same_identity(observed, opened):
            _fail("task_source_changed")
        success = True
        return opened
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail(code)
    finally:
        preserve_abort = _active_terminal_baseexception()
        cleanup_ok = True
        if not success and owner.has_resources():
            cleanup_ok = _guard_cleanup_boolean(
                owner.close
            )
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _optional_metadata(parent_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")


def _read_control_file(
    parent_fd: int,
    name: str,
    device: int,
    limit: int,
    missing_code: str,
    control_owner: _SourceResourceOwner,
) -> Tuple[bytes, Dict[str, object]]:
    if (
        type(control_owner) is not _SourceResourceOwner
        or control_owner.has_resources()
    ):
        _fail(missing_code)
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_file_metadata(observed, device, missing_code)
        control_owner.descriptor = os.open(
            name,
            _file_flags(),
            dir_fd=parent_fd,
        )
        opened = os.fstat(control_owner.descriptor)
        if not _same_identity(observed, opened):
            _fail("task_source_changed")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(
                control_owner.descriptor,
                min(65536, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            _fail(missing_code)
        after = os.fstat(control_owner.descriptor)
        if not _same_identity(opened, after):
            _fail("task_source_changed")
        return content, _identity(after, "file")
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail(missing_code)
    finally:
        preserve_abort = _active_terminal_baseexception()
        cleanup_ok = _guard_cleanup_boolean(control_owner.close)
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _require_absent(parent_fd: int, name: str) -> None:
    if _optional_metadata(parent_fd, name) is not None:
        _fail("task_source_control_invalid")


def _scan_empty_directory(
    parent_fd: int,
    name: str,
    device: int,
) -> str:
    metadata = _optional_metadata(parent_fd, name)
    if metadata is None:
        return "absent"
    owner = _SourceResourceOwner()
    acquired = False
    try:
        opened = _open_child_directory(
            parent_fd,
            name,
            device,
            "task_source_control_invalid",
            owner,
        )
        acquired = True
        owner.iterator = os.scandir(owner.descriptor)
        if next(owner.iterator, None) is not None:
            _fail("task_source_control_invalid")
        if not _guard_cleanup_boolean(
            owner.close_iterator
        ):
            _fail("task_snapshot_cleanup_required")
        if not _same_identity(opened, os.fstat(owner.descriptor)):
            _fail("task_source_changed")
        return "empty"
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        if not acquired and owner.has_resources():
            owner.mark_uncertain()
        if not _guard_cleanup_boolean(
            owner.close
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _scan_hooks(
    git_fd: int,
    device: int,
    policy: TaskSnapshotPolicy,
) -> str:
    metadata = _optional_metadata(git_fd, "hooks")
    if metadata is None:
        return "absent"
    owner = _SourceResourceOwner()
    acquired = False
    count = 0
    exact_names = set()
    nfc_names = set()
    folded_names = set()
    try:
        opened = _open_child_directory(
            git_fd,
            "hooks",
            device,
            "task_source_control_invalid",
            owner,
        )
        acquired = True
        owner.iterator = os.scandir(owner.descriptor)
        for entry in owner.iterator:
            count += 1
            if count > policy.max_object_entries:
                _fail("task_source_control_invalid")
            name = entry.name
            normalized = unicodedata.normalize("NFC", name)
            folded = normalized.casefold()
            if (
                type(name) is not str
                or not name.endswith(".sample")
                or not unicodedata.is_normalized("NFC", name)
                or name in exact_names
                or normalized in nfc_names
                or folded in folded_names
            ):
                _fail("task_source_control_invalid")
            exact_names.add(name)
            nfc_names.add(normalized)
            folded_names.add(folded)
            item = os.stat(
                name,
                dir_fd=owner.descriptor,
                follow_symlinks=False,
            )
            _require_file_metadata(
                item, device, "task_source_control_invalid"
            )
        if not _guard_cleanup_boolean(
            owner.close_iterator
        ):
            _fail("task_snapshot_cleanup_required")
        if not _same_identity(opened, os.fstat(owner.descriptor)):
            _fail("task_source_changed")
        return "sample_only"
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        if not acquired and owner.has_resources():
            owner.mark_uncertain()
        if not _guard_cleanup_boolean(
            owner.close
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _check_refs(git_fd: int, device: int) -> None:
    metadata = _optional_metadata(git_fd, "refs")
    if metadata is None:
        return
    owner = _SourceResourceOwner()
    acquired = False
    try:
        opened = _open_child_directory(
            git_fd,
            "refs",
            device,
            "task_source_control_invalid",
            owner,
        )
        acquired = True
        _require_absent(owner.descriptor, "replace")
        if not _same_identity(opened, os.fstat(owner.descriptor)):
            _fail("task_source_changed")
    finally:
        preserve_abort = _active_terminal_baseexception()
        if not acquired and owner.has_resources():
            owner.mark_uncertain()
        if not _guard_cleanup_boolean(
            owner.close
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _check_object_controls(
    objects_fd: int,
    device: int,
    policy: TaskSnapshotPolicy,
) -> None:
    info_metadata = _optional_metadata(objects_fd, "info")
    if info_metadata is not None:
        info_owner = _SourceResourceOwner()
        info_acquired = False
        try:
            info_opened = _open_child_directory(
                objects_fd,
                "info",
                device,
                "task_source_control_invalid",
                info_owner,
            )
            info_acquired = True
            _require_absent(info_owner.descriptor, "alternates")
            _require_absent(info_owner.descriptor, "http-alternates")
            if not _same_identity(
                info_opened,
                os.fstat(info_owner.descriptor),
            ):
                _fail("task_source_changed")
        finally:
            preserve_abort = _active_terminal_baseexception()
            if not info_acquired and info_owner.has_resources():
                info_owner.mark_uncertain()
            if not _guard_cleanup_boolean(
                info_owner.close
            ):
                _fail_cleanup_required_unless_nonexception_active(
                    preserve_abort
                )
    pack_metadata = _optional_metadata(objects_fd, "pack")
    if pack_metadata is not None:
        pack_owner = _SourceResourceOwner()
        pack_acquired = False
        entry_count = 0
        try:
            pack_opened = _open_child_directory(
                objects_fd,
                "pack",
                device,
                "task_source_control_invalid",
                pack_owner,
            )
            pack_acquired = True
            pack_owner.iterator = os.scandir(pack_owner.descriptor)
            for entry in pack_owner.iterator:
                entry_count += 1
                if entry_count > policy.max_object_entries:
                    _fail("task_source_control_invalid")
                if entry.name.endswith(".promisor"):
                    _fail("task_source_control_invalid")
            if not _guard_cleanup_boolean(
                pack_owner.close_iterator
            ):
                _fail("task_snapshot_cleanup_required")
            if not _same_identity(
                pack_opened,
                os.fstat(pack_owner.descriptor),
            ):
                _fail("task_source_changed")
        finally:
            preserve_abort = _active_terminal_baseexception()
            if not pack_acquired and pack_owner.has_resources():
                pack_owner.mark_uncertain()
            if not _guard_cleanup_boolean(
                pack_owner.close
            ):
                _fail_cleanup_required_unless_nonexception_active(
                    preserve_abort
                )


_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _valid_refname(name: str) -> bool:
    if (
        not name.startswith("refs/")
        or not 1 <= len(name.encode("ascii", "strict")) <= 1024
        or name == "@"
        or name.endswith(("/", "."))
        or ".." in name
        or "@{" in name
        or "//" in name
        or _REF_FORBIDDEN.search(name) is not None
    ):
        return False
    components = name.split("/")
    return all(
        component not in ("", ".", "..")
        and not component.startswith(".")
        and not component.endswith(".lock")
        for component in components
    )


def _parse_packed_refs(content: bytes, oid_length: int) -> None:
    if not content:
        return
    if b"\0" in content or b"\r" in content or not content.endswith(b"\n"):
        _fail("task_source_control_invalid")
    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeDecodeError:
        _fail("task_source_control_invalid")
    index = 0
    if lines and lines[0].startswith("#"):
        header = lines[0]
        prefix = "# pack-refs with: "
        if not header.startswith(prefix) or not header.endswith(" "):
            _fail("task_source_control_invalid")
        tokens = header[len(prefix):-1].split(" ")
        if (
            not tokens
            or len(tokens) != len(set(tokens))
            or any(
                token not in ("peeled", "fully-peeled", "sorted")
                for token in tokens
            )
        ):
            _fail("task_source_control_invalid")
        index = 1
    seen = set()
    previous_ref = False
    peeled_for_previous = False
    oid_pattern = re.compile(r"^[0-9a-f]{" + str(oid_length) + r"}$")
    for line in lines[index:]:
        if line.startswith("^"):
            if (
                not previous_ref
                or peeled_for_previous
                or oid_pattern.fullmatch(line[1:]) is None
            ):
                _fail("task_source_control_invalid")
            peeled_for_previous = True
            continue
        parts = line.split(" ")
        if len(parts) != 2 or oid_pattern.fullmatch(parts[0]) is None:
            _fail("task_source_control_invalid")
        try:
            valid_name = _valid_refname(parts[1])
        except UnicodeEncodeError:
            valid_name = False
        if (
            not valid_name
            or parts[1] in seen
            or parts[1].startswith("refs/replace/")
        ):
            _fail("task_source_control_invalid")
        seen.add(parts[1])
        previous_ref = True
        peeled_for_previous = False


def _capture_filesystem(
    source: TaskSourceSpec,
    policy: TaskSnapshotPolicy,
    expected_format: str,
) -> _FilesystemSeal:
    root_owner = _SourceResourceOwner()
    git_owner = _SourceResourceOwner()
    objects_owner = _SourceResourceOwner()
    config_owner = _SourceResourceOwner()
    packed_owner = _SourceResourceOwner()
    owners = (
        root_owner,
        git_owner,
        objects_owner,
        config_owner,
        packed_owner,
    )
    root_acquired = False
    git_acquired = False
    objects_acquired = False
    try:
        root_metadata, ancestors = _open_root_descriptor(
            source.repository_root,
            root_owner,
        )
        root_acquired = True
        device = root_metadata.st_dev
        git_metadata = _open_child_directory(
            root_owner.descriptor,
            ".git",
            device,
            "task_source_git_dir_invalid",
            git_owner,
        )
        git_acquired = True
        objects_metadata = _open_child_directory(
            git_owner.descriptor,
            "objects",
            device,
            "task_source_control_invalid",
            objects_owner,
        )
        objects_acquired = True
        config_bytes, config_identity = _read_control_file(
            git_owner.descriptor,
            "config",
            device,
            policy.max_config_bytes,
            "task_source_config_invalid",
            config_owner,
        )
        raw_config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        _require_absent(git_owner.descriptor, "commondir")
        _require_absent(git_owner.descriptor, "config.worktree")
        worktrees_state = _scan_empty_directory(
            git_owner.descriptor,
            "worktrees",
            device,
        )
        modules_state = _scan_empty_directory(
            git_owner.descriptor,
            "modules",
            device,
        )
        hooks_state = _scan_hooks(
            git_owner.descriptor,
            device,
            policy,
        )
        _check_refs(git_owner.descriptor, device)
        _check_object_controls(
            objects_owner.descriptor,
            device,
            policy,
        )

        packed_metadata = _optional_metadata(
            git_owner.descriptor,
            "packed-refs",
        )
        if packed_metadata is None:
            packed_document = {"state": "absent"}
        else:
            packed_bytes, packed_identity = _read_control_file(
                git_owner.descriptor,
                "packed-refs",
                device,
                policy.max_packed_refs_bytes,
                "task_source_control_invalid",
                packed_owner,
            )
            _parse_packed_refs(packed_bytes, 40 if expected_format == "sha1" else 64)
            packed_document = {
                "identity": packed_identity,
                "raw_digest": "sha256:"
                + hashlib.sha256(packed_bytes).hexdigest(),
                "state": "present",
            }

        if not _same_identity(
            objects_metadata,
            os.fstat(objects_owner.descriptor),
        ):
            _fail("task_source_changed")
        if not _same_identity(
            git_metadata,
            os.fstat(git_owner.descriptor),
        ):
            _fail("task_source_changed")
        if not _same_identity(
            root_metadata,
            os.fstat(root_owner.descriptor),
        ):
            _fail("task_source_changed")
        document = {
            "ancestor_identities": list(ancestors),
            "config": {
                "identity": config_identity,
                "raw_digest": raw_config_digest,
            },
            "control_policy_state": {
                "commondir": "absent",
                "config_worktree": "absent",
                "hooks": hooks_state,
                "modules": modules_state,
                "object_alternates": "absent",
                "promisor_markers": "absent",
                "replace_refs": "absent",
                "worktrees": worktrees_state,
            },
            "document_type": "task-source-filesystem-seal-v1",
            "expected_object_format": expected_format,
            "git_dir_identity": _identity(git_metadata, "directory"),
            "objects_identity": _identity(objects_metadata, "directory"),
            "packed_refs": packed_document,
            "repository_identity": _identity(root_metadata, "directory"),
            "schema_version": 1,
        }
        return _FilesystemSeal(
            filesystem_digest=_digest(document),
            raw_config_digest=raw_config_digest,
            git_dir=source.repository_root / ".git",
            config_path=source.repository_root / ".git" / "config",
            repository_identity_key=_physical_identity_key(
                root_metadata,
                "directory",
            ),
            git_dir_identity_key=_physical_identity_key(
                git_metadata,
                "directory",
            ),
        )
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        for acquired, owner in (
            (root_acquired, root_owner),
            (git_acquired, git_owner),
            (objects_acquired, objects_owner),
        ):
            if not acquired and owner.has_resources():
                owner.mark_uncertain()
        if not _guard_cleanup_boolean(
            _close_source_owners,
            owners,
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


_CONFIG_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_REMOTE_KEY = re.compile(
    r"^remote\.(" + _CONFIG_NAME + r")\.(url|fetch)$"
)
_BRANCH_KEY = re.compile(
    r"^branch\.(" + _CONFIG_NAME + r")\.(remote|merge)$"
)


def _parse_config_output(
    output: bytes,
    raw_config_digest: str,
    expected_format: str,
) -> str:
    try:
        if (
            type(output) is not bytes
            or not output
            or not output.endswith(b"\0")
            or type(raw_config_digest) is not str
            or _DIGEST_PATTERN.fullmatch(raw_config_digest) is None
            or expected_format not in ("sha1", "sha256")
        ):
            _fail("task_source_config_invalid")
        raw_records = output[:-1].split(b"\0")
        if not raw_records or any(not record for record in raw_records):
            _fail("task_source_config_invalid")
        records = []
        values = {}
        for raw_record in raw_records:
            if raw_record.count(b"\n") != 1:
                _fail("task_source_config_invalid")
            raw_key, raw_value = raw_record.split(b"\n", 1)
            key = raw_key.decode("ascii")
            value = raw_value.decode("utf-8")
            if (
                not key
                or not value
                or key in values
                or not unicodedata.is_normalized("NFC", value)
                or any(
                    unicodedata.category(character) == "Cc"
                    for character in key + value
                )
            ):
                _fail("task_source_config_invalid")
            values[key] = value
            records.append({"key": key, "value": value})

        boolean_keys = {
            "core.filemode",
            "core.bare",
            "core.logallrefupdates",
            "core.ignorecase",
            "core.precomposeunicode",
        }
        for key, value in values.items():
            if key == "core.repositoryformatversion":
                if value not in ("0", "1"):
                    _fail("task_source_config_invalid")
            elif key in boolean_keys:
                if value not in ("true", "false"):
                    _fail("task_source_config_invalid")
            elif key == "extensions.objectformat":
                if value != "sha256":
                    _fail("task_source_config_invalid")
            else:
                remote_match = _REMOTE_KEY.fullmatch(key)
                branch_match = _BRANCH_KEY.fullmatch(key)
                if remote_match:
                    name, kind = remote_match.groups()
                    if kind == "fetch" and value != (
                        "+refs/heads/*:refs/remotes/{0}/*".format(name)
                    ):
                        _fail("task_source_config_invalid")
                elif branch_match:
                    name, kind = branch_match.groups()
                    if kind == "remote":
                        allowed_remotes = {
                            match.group(1)
                            for candidate in values
                            for match in [_REMOTE_KEY.fullmatch(candidate)]
                            if match is not None
                        }
                        if value != "." and value not in allowed_remotes:
                            _fail("task_source_config_invalid")
                    elif value != "refs/heads/{0}".format(name):
                        _fail("task_source_config_invalid")
                else:
                    _fail("task_source_config_invalid")
        if values.get("core.bare") != "false":
            _fail("task_source_config_invalid")
        if expected_format == "sha1":
            if (
                values.get("core.repositoryformatversion") != "0"
                or "extensions.objectformat" in values
            ):
                _fail("task_source_config_invalid")
        elif (
            values.get("core.repositoryformatversion") != "1"
            or values.get("extensions.objectformat") != "sha256"
        ):
            _fail("task_source_config_invalid")
        records.sort(
            key=lambda item: (
                item["key"].encode("utf-8"),
                item["value"].encode("utf-8"),
            )
        )
        document = {
            "document_type": "task-local-config-seal-v1",
            "raw_config_digest": raw_config_digest,
            "records": records,
            "schema_version": 1,
        }
        return _digest(document)
    except TaskSnapshotError:
        raise
    except (UnicodeDecodeError, UnicodeEncodeError, TypeError, ValueError):
        _fail("task_source_config_invalid")


def _object_directory_allowed(path: str) -> bool:
    components = path.split("/")
    if len(components) == 1:
        return (
            components[0] in ("info", "pack")
            or re.fullmatch(r"[0-9a-f]{2}", components[0]) is not None
        )
    return components == ["info", "commit-graphs"]


def _object_file_family(
    path: str,
    oid_length: int,
) -> Optional[Tuple[str, str]]:
    components = path.split("/")
    if (
        len(components) == 2
        and re.fullmatch(r"[0-9a-f]{2}", components[0]) is not None
        and re.fullmatch(
            r"[0-9a-f]{" + str(oid_length - 2) + r"}", components[1]
        )
        is not None
    ):
        return ("loose", "")
    if components in (["info", "packs"], ["info", "commit-graph"]):
        return ("info", "")
    if len(components) == 3 and components[:2] == ["info", "commit-graphs"]:
        if components[2] == "commit-graph-chain":
            return ("commit-graph", "")
        if re.fullmatch(
            r"graph-[0-9a-f]{" + str(oid_length) + r"}\.graph",
            components[2],
        ):
            return ("commit-graph", "")
        return None
    if len(components) != 2 or components[0] != "pack":
        return None
    name = components[1]
    if name == "multi-pack-index":
        return ("multi-pack-index", "")
    if re.fullmatch(
        r"multi-pack-index-[0-9a-f]{"
        + str(oid_length)
        + r"}\.bitmap",
        name,
    ):
        return ("multi-pack-index", "")
    match = re.fullmatch(
        r"(pack-[0-9a-f]{" + str(oid_length) + r"})"
        r"\.(pack|idx|rev|bitmap|mtimes|keep)",
        name,
    )
    if match is None:
        return None
    return (match.group(1), match.group(2))


def _topology_record(
    path: str,
    metadata: os.stat_result,
    kind: str,
) -> Dict[str, object]:
    record = _identity(metadata, kind)
    record["path"] = path
    return record


def _capture_object_topology(
    source: TaskSourceSpec,
    policy: TaskSnapshotPolicy,
    object_format: str,
) -> ObjectTopologySeal:
    root_owner = _SourceResourceOwner()
    git_owner = _SourceResourceOwner()
    objects_owner = _SourceResourceOwner()
    owners = [root_owner, git_owner, objects_owner]
    root_acquired = False
    git_acquired = False
    objects_acquired = False
    root_publish_started = False
    root_published = False
    frames = []
    try:
        if object_format not in ("sha1", "sha256"):
            _fail("task_source_topology_invalid")
        oid_length = 40 if object_format == "sha1" else 64
        root_metadata, unused_ancestors = _open_root_descriptor(
            source.repository_root,
            root_owner,
        )
        root_acquired = True
        device = root_metadata.st_dev
        unused_git_metadata = _open_child_directory(
            root_owner.descriptor,
            ".git",
            device,
            "task_source_git_dir_invalid",
            git_owner,
        )
        git_acquired = True
        objects_metadata = _open_child_directory(
            git_owner.descriptor,
            "objects",
            device,
            "task_source_topology_invalid",
            objects_owner,
        )
        objects_acquired = True
        records = [_topology_record(".", objects_metadata, "directory")]
        entry_count = 1
        file_count = 0
        total_bytes = 0
        pairings: Dict[str, set] = {}
        if entry_count > policy.max_object_entries:
            _fail("task_source_topology_invalid")

        objects_owner.iterator = os.scandir(objects_owner.descriptor)
        root_publish_started = True
        frames.append(
            {
                "owner": objects_owner,
                "metadata": objects_metadata,
                "path": ".",
                "depth": 0,
                "exact": set(),
                "nfc": set(),
                "casefold": set(),
            }
        )
        root_published = True
        while frames:
            frame = frames[-1]
            frame_owner = frame["owner"]
            try:
                entry = next(frame_owner.iterator)
            except StopIteration:
                cleanup_ok = _guard_cleanup_boolean(
                    frame_owner.close_iterator,
                )
                if cleanup_ok and not _same_identity(
                    frame["metadata"],
                    os.fstat(frame_owner.descriptor),
                ):
                    _fail("task_source_changed")
                cleanup_ok = _guard_cleanup_boolean(
                    frame_owner.close_descriptor,
                ) and cleanup_ok
                frames.pop()
                if not cleanup_ok:
                    _fail("task_snapshot_cleanup_required")
                continue

            name = entry.name
            if (
                type(name) is not str
                or not name
                or not unicodedata.is_normalized("NFC", name)
            ):
                _fail("task_source_topology_invalid")
            encoded_name = name.encode("utf-8")
            if len(encoded_name) > policy.max_component_bytes:
                _fail("task_source_topology_invalid")
            normalized = unicodedata.normalize("NFC", name)
            folded = normalized.casefold()
            if (
                name in frame["exact"]
                or normalized in frame["nfc"]
                or folded in frame["casefold"]
            ):
                _fail("task_source_topology_invalid")
            frame["exact"].add(name)
            frame["nfc"].add(normalized)
            frame["casefold"].add(folded)
            path = name if frame["path"] == "." else frame["path"] + "/" + name
            depth = frame["depth"] + 1
            if (
                depth > policy.max_object_depth
                or len(path.encode("utf-8")) > policy.max_relative_path_bytes
            ):
                _fail("task_source_topology_invalid")
            observed = os.stat(
                name,
                dir_fd=frame_owner.descriptor,
                follow_symlinks=False,
            )
            entry_count += 1
            if entry_count > policy.max_object_entries:
                _fail("task_source_topology_invalid")
            if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(
                observed.st_mode
            ):
                _require_directory_metadata(
                    observed, device, "task_source_topology_invalid"
                )
                if not _object_directory_allowed(path):
                    _fail("task_source_topology_invalid")
                child_owner = _SourceResourceOwner()
                owners.append(child_owner)
                child_acquired = False
                child_publish_started = False
                child_published = False
                try:
                    child_metadata = _open_child_directory(
                        frame_owner.descriptor,
                        name,
                        device,
                        "task_source_topology_invalid",
                        child_owner,
                    )
                    child_acquired = True
                    if not _same_identity(observed, child_metadata):
                        _fail("task_source_changed")
                    child_owner.iterator = os.scandir(
                        child_owner.descriptor
                    )
                    records.append(
                        _topology_record(path, child_metadata, "directory")
                    )
                    child_publish_started = True
                    frames.append(
                        {
                            "owner": child_owner,
                            "metadata": child_metadata,
                            "path": path,
                            "depth": depth,
                            "exact": set(),
                            "nfc": set(),
                            "casefold": set(),
                        }
                    )
                    child_published = True
                finally:
                    preserve_abort = _active_terminal_baseexception()
                    cleanup_ok = True
                    if (
                        not child_acquired
                        and child_owner.has_resources()
                    ):
                        child_owner.mark_uncertain()
                    if child_publish_started and not child_published:
                        child_owner.mark_uncertain()
                        cleanup_ok = False
                    if not child_published:
                        cleanup_ok = _guard_cleanup_boolean(
                            child_owner.close
                        ) and cleanup_ok
                    if not cleanup_ok:
                        _fail_cleanup_required_unless_nonexception_active(
                            preserve_abort
                        )
                continue
            _require_file_metadata(
                observed, device, "task_source_topology_invalid"
            )
            family = _object_file_family(path, oid_length)
            if family is None:
                _fail("task_source_topology_invalid")
            after = os.stat(
                name,
                dir_fd=frame_owner.descriptor,
                follow_symlinks=False,
            )
            if not _same_identity(observed, after):
                _fail("task_source_changed")
            file_count += 1
            total_bytes += after.st_size
            if (
                file_count > policy.max_files
                or total_bytes > policy.max_object_store_bytes
            ):
                _fail("task_source_topology_invalid")
            records.append(_topology_record(path, after, "file"))
            if family[0].startswith("pack-"):
                pairings.setdefault(family[0], set()).add(family[1])

        for extensions in pairings.values():
            if "pack" not in extensions or "idx" not in extensions:
                _fail("task_source_topology_invalid")
        records.sort(key=lambda item: item["path"].encode("utf-8"))
        document = {
            "document_type": "task-object-topology-v1",
            "entry_count": entry_count,
            "file_count": file_count,
            "object_format": object_format,
            "records": records,
            "schema_version": 1,
            "total_bytes": total_bytes,
        }
        return ObjectTopologySeal(
            object_topology_digest=_digest(document),
            entry_count=entry_count,
            file_count=file_count,
            total_bytes=total_bytes,
        )
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError, UnicodeError):
        _fail("task_source_topology_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        for acquired, owner in (
            (root_acquired, root_owner),
            (git_acquired, git_owner),
            (objects_acquired, objects_owner),
        ):
            if not acquired and owner.has_resources():
                owner.mark_uncertain()
        if root_publish_started and not root_published:
            objects_owner.mark_uncertain()
        cleanup_ok = _guard_cleanup_boolean(
            _close_source_owners,
            owners,
        )
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


_OPERATION_TEMPLATES = [
    ["config", "--file=<verified-config>", "--no-includes", "--null", "--list"],
    ["rev-parse", "--show-object-format=storage"],
    [
        "rev-parse",
        "--verify",
        "--end-of-options",
        "<validated-full-commit-oid>^{commit}",
    ],
    [
        "rev-parse",
        "--verify",
        "--end-of-options",
        "<validated-full-commit-oid>^{tree}",
    ],
    [
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "--format=%(objectmode)%x09%(objecttype)%x09"
        "%(objectname)%x09%(objectsize)%x09%(path)",
        "<validated-full-tree-oid>",
    ],
    ["cat-file", "blob", "<validated-full-blob-oid>"],
]


def _process_policy_digest(policy: TaskSnapshotPolicy) -> str:
    _validate_policy(policy)
    document = {
        "argv_prefix": [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=<null-device>",
            "-c",
            "core.excludesFile=<null-device>",
            "-c",
            "core.hooksPath=<null-device>",
            "-c",
            "submodule.recurse=false",
            "--git-dir=<verified-git-dir>",
        ],
        "capture_deadline": {
            "capture_timeout_seconds": policy.capture_timeout_seconds,
            "clock": "time.monotonic",
            "effective_process_deadline":
                "earliest-of-capture-and-operation",
            "equal_expiry_classification": "task_capture_timeout",
            "scope": "fresh-F0-through-source-trust-receipt",
        },
        "close_fds": True,
        "cwd": "/",
        "document_type": "task-git-process-policy-v1",
        "environment": {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "<null-device>",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "<os.defpath>",
        },
        "limits": {
            "git_termination_grace_milliseconds":
                policy.git_termination_grace_milliseconds,
            "git_timeout_seconds": policy.git_timeout_seconds,
            "max_git_stderr_bytes": policy.max_git_stderr_bytes,
            "max_git_stdout_bytes": policy.max_git_stdout_bytes,
        },
        "operation_templates": _OPERATION_TEMPLATES,
        "output_policy": {
            "cap_is_inclusive": True,
            "cap_plus_one_action": "terminate-process-group",
            "cat_file_blob_stdout_cap": "validated-declared-blob-size",
            "generic_stdout_cap": "max_git_stdout_bytes",
            "successful_stderr": "empty",
        },
        "schema_version": 1,
        "shell": False,
        "start_new_session": True,
        "stdin": "DEVNULL",
        "termination": [
            "concurrent-bounded-drain",
            "monotonic-deadline",
            "TERM",
            "bounded-grace",
            "KILL",
            "reap",
            "close-pipes",
            "verify-group-absent",
        ],
    }
    return _digest(document)


def _source_identity_digest(
    filesystem_digest: str,
    local_config_digest: str,
) -> str:
    if (
        type(filesystem_digest) is not str
        or _DIGEST_PATTERN.fullmatch(filesystem_digest) is None
        or type(local_config_digest) is not str
        or _DIGEST_PATTERN.fullmatch(local_config_digest) is None
    ):
        _fail("task_source_changed")
    return _digest(
        {
            "document_type": "task-source-identity-v1",
            "filesystem_identity_digest": filesystem_digest,
            "local_config_digest": local_config_digest,
            "schema_version": 1,
        }
    )


def _validate_topology_seal(seal: ObjectTopologySeal) -> None:
    if (
        not _exact_fields(seal, ObjectTopologySeal)
        or type(seal.object_topology_digest) is not str
        or _DIGEST_PATTERN.fullmatch(seal.object_topology_digest) is None
        or type(seal.entry_count) is not int
        or seal.entry_count < 1
        or type(seal.file_count) is not int
        or seal.file_count < 0
        or seal.file_count > seal.entry_count
        or type(seal.total_bytes) is not int
        or seal.total_bytes < 0
    ):
        _fail("task_source_topology_invalid")


def _validate_prepared_source(prepared: PreparedTaskSource) -> None:
    if not _exact_fields(prepared, PreparedTaskSource):
        _fail("task_source_changed")
    _validate_source(prepared.source)
    _validate_policy(prepared.policy)
    _validate_topology_seal(prepared.object_topology)
    try:
        raw_git_dir = os.fspath(prepared.git_dir)
    except Exception:
        _fail("task_source_changed")
    expected_format = "sha1" if len(prepared.source.commit_oid) == 40 else "sha256"
    if (
        type(raw_git_dir) is not str
        or type(prepared.git_dir) is not type(Path())
        or prepared.git_dir != prepared.source.repository_root / ".git"
        or type(prepared.object_format) is not str
        or prepared.object_format != expected_format
        or type(prepared.source_identity_digest) is not str
        or _DIGEST_PATTERN.fullmatch(prepared.source_identity_digest) is None
        or type(prepared.local_config_digest) is not str
        or _DIGEST_PATTERN.fullmatch(prepared.local_config_digest) is None
        or type(prepared.git_process_policy_digest) is not str
        or _DIGEST_PATTERN.fullmatch(prepared.git_process_policy_digest) is None
        or prepared.object_topology.entry_count
        > prepared.policy.max_object_entries
        or prepared.object_topology.file_count > prepared.policy.max_files
        or prepared.object_topology.total_bytes
        > prepared.policy.max_object_store_bytes
    ):
        _fail("task_source_changed")


@dataclass
class _DrainState:
    data: bytearray = field(default_factory=bytearray)
    overflow: bool = False
    failed: bool = False
    done: threading.Event = field(default_factory=threading.Event)


def _drain_stream(stream: object, limit: int, state: _DrainState) -> None:
    try:
        while True:
            remaining = limit + 1 - len(state.data)
            if remaining <= 0:
                state.overflow = True
                return
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                return
            if type(chunk) is not bytes:
                state.failed = True
                return
            state.data.extend(chunk)
            if len(state.data) > limit:
                state.overflow = True
                return
    except Exception:
        state.failed = True
    finally:
        state.done.set()


def _git_environment() -> Dict[str, str]:
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }


def _git_operation_tail(
    operation: str,
    config_path: Path,
    value: Optional[str],
) -> Tuple[str, ...]:
    try:
        raw_config = os.fspath(config_path)
    except Exception:
        _fail("task_git_operation_invalid")
    if (
        type(operation) is not str
        or type(raw_config) is not str
        or not os.path.isabs(raw_config)
        or os.path.normpath(raw_config) != raw_config
        or os.path.realpath(raw_config) != raw_config
    ):
        _fail("task_git_operation_invalid")
    if operation == "config" and value is None:
        return (
            "config",
            "--file=" + raw_config,
            "--no-includes",
            "--null",
            "--list",
        )
    if operation == "storage-format" and value is None:
        return ("rev-parse", "--show-object-format=storage")
    if type(value) is not str or _OID_PATTERN.fullmatch(value) is None:
        _fail("task_git_operation_invalid")
    if operation == "verify-commit":
        return ("rev-parse", "--verify", "--end-of-options", value + "^{commit}")
    if operation == "verify-tree":
        return ("rev-parse", "--verify", "--end-of-options", value + "^{tree}")
    if operation == "ls-tree":
        return (
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--format=%(objectmode)%x09%(objecttype)%x09"
            "%(objectname)%x09%(objectsize)%x09%(path)",
            value,
        )
    if operation == "cat-blob":
        return ("cat-file", "blob", value)
    _fail("task_git_operation_invalid")


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _close_process_pipes(process: subprocess.Popen) -> bool:
    success = True
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                success = False
    return success


def _cleanup_process(
    process: subprocess.Popen,
    process_group: int,
    grace_seconds: float,
) -> bool:
    success = True
    poll_failed = False
    pre_kill_wait_timed_out = False
    killed = False
    try:
        termination_deadline = time.monotonic() + grace_seconds
    except Exception:
        termination_deadline = 0.0
        success = False
    try:
        process.poll()
    except Exception:
        poll_failed = True
        success = False
    if _group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            success = False
    try:
        process.wait(timeout=max(grace_seconds, 0.001))
    except subprocess.TimeoutExpired:
        pre_kill_wait_timed_out = True
    except Exception:
        success = False
    while (
        _group_exists(process_group)
        and time.monotonic() < termination_deadline
    ):
        time.sleep(
            min(0.005, max(0.0, termination_deadline - time.monotonic()))
        )
    if _group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
            killed = True
        except ProcessLookupError:
            pass
        except OSError:
            success = False
    if killed or pre_kill_wait_timed_out or poll_failed:
        try:
            process.wait(timeout=max(grace_seconds, 0.001))
        except subprocess.TimeoutExpired:
            success = False
        except Exception:
            success = False
    try:
        absence_deadline = time.monotonic() + grace_seconds
        while (
            _group_exists(process_group)
            and time.monotonic() < absence_deadline
        ):
            time.sleep(
                min(
                    0.005,
                    max(0.0, absence_deadline - time.monotonic()),
                )
            )
    except Exception:
        success = False
    return success and not _group_exists(process_group)


def _join_started_threads(
    threads: Sequence[threading.Thread],
    timeout_seconds: float,
) -> bool:
    success = True
    for thread in threads:
        try:
            thread.join(timeout=max(timeout_seconds, 0.001))
        except Exception:
            success = False
    return success


def _started_threads_stopped(
    threads: Sequence[threading.Thread],
) -> bool:
    try:
        return all(not thread.is_alive() for thread in threads)
    except Exception:
        return False


def _cleanup_git_failure(
    process: subprocess.Popen,
    started_threads: Sequence[threading.Thread],
    grace_seconds: float,
) -> bool:
    try:
        cleanup_ok = _cleanup_process(
            process, process.pid, grace_seconds
        )
    except Exception:
        cleanup_ok = False
    joined = _join_started_threads(started_threads, grace_seconds)
    try:
        pipes_closed = _close_process_pipes(process)
    except Exception:
        pipes_closed = False
    joined = (
        _join_started_threads(started_threads, grace_seconds) and joined
    )
    try:
        group_absent = not _group_exists(process.pid)
    except Exception:
        group_absent = False
    return (
        cleanup_ok
        and joined
        and pipes_closed
        and _started_threads_stopped(started_threads)
        and group_absent
    )


def _git_deadline_status(
    operation_deadline: float,
    capture_deadline: Optional[float],
) -> Tuple[Optional[float], str]:
    try:
        now = time.monotonic()
    except Exception:
        return None, "task_git_failed"
    operation_remaining = operation_deadline - now
    if capture_deadline is None:
        return operation_remaining, "task_git_timeout"
    capture_remaining = capture_deadline - now
    remaining = min(operation_remaining, capture_remaining)
    if capture_remaining <= 0:
        return remaining, "task_capture_timeout"
    return remaining, "task_git_timeout"


def _run_git(
    git_dir: Path,
    config_path: Path,
    policy: TaskSnapshotPolicy,
    operation: str,
    value: Optional[str] = None,
    *,
    capture_deadline: Optional[float] = None,
    stdout_limit: Optional[int] = None,
) -> bytes:
    _validate_policy(policy)
    try:
        raw_git_dir = os.fspath(git_dir)
    except Exception:
        _fail("task_git_operation_invalid")
    if (
        type(raw_git_dir) is not str
        or not os.path.isabs(raw_git_dir)
        or os.path.normpath(raw_git_dir) != raw_git_dir
        or os.path.realpath(raw_git_dir) != raw_git_dir
    ):
        _fail("task_git_operation_invalid")
    tail = _git_operation_tail(operation, config_path, value)
    if operation == "cat-blob":
        if (
            type(stdout_limit) is not int
            or stdout_limit < 0
            or stdout_limit > policy.max_file_bytes
        ):
            _fail("task_git_operation_invalid")
        effective_stdout_limit = stdout_limit
        stdout_overflow_failure = "task_blob_invalid"
    else:
        if stdout_limit is not None:
            _fail("task_git_operation_invalid")
        effective_stdout_limit = policy.max_git_stdout_bytes
        stdout_overflow_failure = "task_git_output_limit"
    if capture_deadline is not None and (
        type(capture_deadline) is not float
        or not math.isfinite(capture_deadline)
        or capture_deadline < 0.0
    ):
        _fail("task_git_operation_invalid")
    argv = (
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=" + os.devnull,
        "-c",
        "core.excludesFile=" + os.devnull,
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "submodule.recurse=false",
        "--git-dir=" + raw_git_dir,
    ) + tail
    try:
        operation_deadline = (
            time.monotonic() + policy.git_timeout_seconds
        )
    except Exception:
        _fail("task_git_failed")
    remaining, timeout_failure = _git_deadline_status(
        operation_deadline, capture_deadline
    )
    if remaining is None:
        _fail("task_git_failed")
    if remaining <= 0:
        _fail(timeout_failure)
    try:
        process = subprocess.Popen(
            argv,
            cwd="/",
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        remaining, timeout_failure = _git_deadline_status(
            operation_deadline, capture_deadline
        )
        if remaining is None:
            _fail("task_git_failed")
        if remaining <= 0:
            _fail(timeout_failure)
        _fail("task_git_spawn_failed")
    if (
        type(getattr(process, "pid", None)) is not int
        or process.pid <= 0
        or process.stdout is None
        or process.stderr is None
    ):
        cleanup_ok = True
        if (
            type(getattr(process, "pid", None)) is int
            and process.pid > 0
        ):
            cleanup_ok = _cleanup_git_failure(
                process,
                (),
                policy.git_termination_grace_milliseconds / 1000.0,
            )
        else:
            try:
                cleanup_ok = _close_process_pipes(process)
            except Exception:
                cleanup_ok = False
        if not cleanup_ok:
            _fail("task_git_failed")
        _fail("task_git_spawn_failed")
    grace = policy.git_termination_grace_milliseconds / 1000.0
    remaining, timeout_failure = _git_deadline_status(
        operation_deadline, capture_deadline
    )
    if remaining is None:
        _cleanup_git_failure(process, (), grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(process, (), grace):
            _fail("task_git_failed")
        _fail(timeout_failure)

    stdout_state = _DrainState()
    stderr_state = _DrainState()
    reader_specs = (
        (process.stdout, effective_stdout_limit, stdout_state),
        (process.stderr, policy.max_git_stderr_bytes, stderr_state),
    )
    started_threads = []
    reader_setup_failure = None
    for stream, limit, state in reader_specs:
        remaining, timeout_failure = _git_deadline_status(
            operation_deadline, capture_deadline
        )
        if remaining is None:
            reader_setup_failure = "task_git_failed"
            break
        if remaining <= 0:
            reader_setup_failure = timeout_failure
            break
        try:
            thread = threading.Thread(
                target=_drain_stream,
                args=(stream, limit, state),
                daemon=True,
            )
        except Exception:
            reader_setup_failure = "task_git_failed"
            break
        remaining, timeout_failure = _git_deadline_status(
            operation_deadline, capture_deadline
        )
        if remaining is None:
            reader_setup_failure = "task_git_failed"
            break
        if remaining <= 0:
            reader_setup_failure = timeout_failure
            break
        try:
            thread.start()
        except Exception:
            reader_setup_failure = "task_git_failed"
            break
        started_threads.append(thread)
        remaining, timeout_failure = _git_deadline_status(
            operation_deadline, capture_deadline
        )
        if remaining is None:
            reader_setup_failure = "task_git_failed"
            break
        if remaining <= 0:
            reader_setup_failure = timeout_failure
            break
    if reader_setup_failure is not None:
        if not _cleanup_git_failure(process, started_threads, grace):
            _fail("task_git_failed")
        _fail(reader_setup_failure)

    failure = None
    while True:
        try:
            remaining, timeout_failure = _git_deadline_status(
                operation_deadline, capture_deadline
            )
        except Exception:
            failure = "task_git_failed"
            break
        if remaining is None:
            failure = "task_git_failed"
            break
        if remaining <= 0:
            failure = timeout_failure
            break
        if stdout_state.failed or stderr_state.failed:
            failure = "task_git_failed"
            break
        if stdout_state.overflow:
            failure = stdout_overflow_failure
            break
        if stderr_state.overflow:
            failure = "task_git_output_limit"
            break
        try:
            process_status = process.poll()
        except Exception:
            failure = "task_git_failed"
            break
        if process_status is not None:
            if process_status != 0:
                failure = "task_git_failed"
                break
            if (
                stdout_state.done.is_set()
                and stderr_state.done.is_set()
            ):
                break
        try:
            time.sleep(min(0.005, remaining))
        except Exception:
            failure = "task_git_failed"
            break

    if failure is not None:
        cleanup_ok = _cleanup_git_failure(
            process, started_threads, grace
        )
        if (
            not cleanup_ok
            or stdout_state.failed
            or stderr_state.failed
        ):
            _fail("task_git_failed")
        _fail(failure)

    try:
        remaining, timeout_failure = _git_deadline_status(
            operation_deadline, capture_deadline
        )
    except Exception:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining is None:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(
            process, started_threads, grace
        ):
            _fail("task_git_failed")
        _fail(timeout_failure)
    try:
        return_code = process.wait(timeout=max(remaining, 0.001))
    except Exception:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    remaining, timeout_failure = _git_deadline_status(
        operation_deadline, capture_deadline
    )
    if remaining is None:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(
            process, started_threads, grace
        ):
            _fail("task_git_failed")
        _fail(timeout_failure)
    join_ok = _join_started_threads(
        started_threads, min(grace, remaining)
    )
    remaining, timeout_failure = _git_deadline_status(
        operation_deadline, capture_deadline
    )
    if not join_ok or remaining is None:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(
            process, started_threads, grace
        ):
            _fail("task_git_failed")
        _fail(timeout_failure)
    nominal_ok = (
        _started_threads_stopped(started_threads)
        and not stdout_state.failed
        and not stderr_state.failed
        and not stdout_state.overflow
        and not stderr_state.overflow
        and not _group_exists(process.pid)
    )
    if return_code != 0 or not nominal_ok:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if stderr_state.data:
        if not _cleanup_git_failure(process, started_threads, grace):
            _fail("task_git_failed")
        _fail("task_git_failed")
    if not _close_process_pipes(process):
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    return bytes(stdout_state.data)


def _copy_task_snapshot_policy(
    policy: TaskSnapshotPolicy,
) -> TaskSnapshotPolicy:
    _validate_policy(policy)
    return TaskSnapshotPolicy(
        **{
            item.name: getattr(policy, item.name)
            for item in fields(TaskSnapshotPolicy)
        }
    )


def _copy_task_source(source: TaskSourceSpec) -> TaskSourceSpec:
    _validate_source(source)
    return TaskSourceSpec(
        input_digest=source.input_digest,
        task_id=source.task_id,
        repository_root=Path(os.fspath(source.repository_root)),
        commit_oid=source.commit_oid,
        provisioning_class=source.provisioning_class,
        operator_attested=source.operator_attested,
        local_clone_policy=source.local_clone_policy,
    )


def _detach_prepared_source(
    prepared: PreparedTaskSource,
) -> PreparedTaskSource:
    try:
        _validate_prepared_source(prepared)
        detached = PreparedTaskSource(
            source=_copy_task_source(prepared.source),
            policy=_copy_task_snapshot_policy(prepared.policy),
            git_dir=Path(os.fspath(prepared.git_dir)),
            object_format=prepared.object_format,
            source_identity_digest=prepared.source_identity_digest,
            local_config_digest=prepared.local_config_digest,
            object_topology=ObjectTopologySeal(
                object_topology_digest=(
                    prepared.object_topology.object_topology_digest
                ),
                entry_count=prepared.object_topology.entry_count,
                file_count=prepared.object_topology.file_count,
                total_bytes=prepared.object_topology.total_bytes,
            ),
            git_process_policy_digest=prepared.git_process_policy_digest,
        )
        _validate_prepared_source(detached)
        return detached
    except TaskSnapshotError:
        raise
    except Exception:
        _fail("task_source_changed")


def _require_prepared_unchanged(
    prepared: PreparedTaskSource,
    detached: PreparedTaskSource,
) -> None:
    try:
        current = _detach_prepared_source(prepared)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        _fail("task_source_changed")
    if current != detached:
        _fail("task_source_changed")


def _validate_identity_key(
    value: object,
    code: str,
) -> None:
    if (
        type(value) is not tuple
        or len(value) != 3
        or type(value[0]) is not int
        or value[0] < 0
        or type(value[1]) is not int
        or value[1] < 0
        or type(value[2]) is not str
        or value[2] != "directory"
    ):
        _fail(code)


def _validate_filesystem_seal(seal: _FilesystemSeal) -> None:
    if (
        not _exact_fields(seal, _FilesystemSeal)
        or type(seal.filesystem_digest) is not str
        or _DIGEST_PATTERN.fullmatch(seal.filesystem_digest) is None
        or type(seal.raw_config_digest) is not str
        or _DIGEST_PATTERN.fullmatch(seal.raw_config_digest) is None
        or type(seal.git_dir) is not type(Path())
        or type(seal.config_path) is not type(Path())
    ):
        _fail("task_source_changed")
    _validate_identity_key(
        seal.repository_identity_key,
        "task_source_changed",
    )
    _validate_identity_key(
        seal.git_dir_identity_key,
        "task_source_changed",
    )
    if seal.repository_identity_key == seal.git_dir_identity_key:
        _fail("task_source_changed")


def _capture_clock_value() -> float:
    try:
        value = time.monotonic()
    except Exception:
        _fail("task_capture_timeout")
    if (
        type(value) not in (int, float)
        or type(value) is bool
        or not math.isfinite(value)
    ):
        _fail("task_capture_timeout")
    return float(value)


def _capture_deadline(policy: TaskSnapshotPolicy) -> float:
    started = _capture_clock_value()
    deadline = started + policy.capture_timeout_seconds
    if not math.isfinite(deadline):
        _fail("task_capture_timeout")
    return deadline


def _check_capture_deadline(deadline: float) -> None:
    if (
        type(deadline) is not float
        or not math.isfinite(deadline)
        or _capture_clock_value() >= deadline
    ):
        _fail("task_capture_timeout")


def _capture_phase(deadline: float, function: object, *args, **kwargs):
    _check_capture_deadline(deadline)
    result = function(*args, **kwargs)
    _check_capture_deadline(deadline)
    return result


def _validate_commit_output(
    output: bytes,
    commit_oid: str,
) -> None:
    if (
        type(output) is not bytes
        or output != (commit_oid + "\n").encode("ascii")
    ):
        _fail("task_source_changed")


def _validate_storage_format_output(
    output: bytes,
    expected_format: str,
) -> None:
    if (
        type(output) is not bytes
        or output not in (b"sha1\n", b"sha256\n")
    ):
        _fail("task_git_failed")
    if output != (expected_format + "\n").encode("ascii"):
        _fail("task_source_config_invalid")


def _parse_tree_output(
    output: bytes,
    object_format: str,
) -> str:
    oid_length = 40 if object_format == "sha1" else 64
    if (
        type(output) is not bytes
        or len(output) != oid_length + 1
        or not output.endswith(b"\n")
    ):
        _fail("task_tree_invalid")
    try:
        tree_oid = output[:-1].decode("ascii")
    except UnicodeDecodeError:
        _fail("task_tree_invalid")
    if (
        len(tree_oid) != oid_length
        or _OID_PATTERN.fullmatch(tree_oid) is None
    ):
        _fail("task_tree_invalid")
    return tree_oid


def _require_capture_matches_prepared(
    filesystem: _FilesystemSeal,
    config_digest: str,
    topology: ObjectTopologySeal,
    prepared: PreparedTaskSource,
    process_policy_digest: str,
) -> str:
    _validate_filesystem_seal(filesystem)
    _validate_topology_seal(topology)
    source_identity_digest = _source_identity_digest(
        filesystem.filesystem_digest,
        config_digest,
    )
    if (
        filesystem.git_dir != prepared.git_dir
        or filesystem.config_path != prepared.git_dir / "config"
        or config_digest != prepared.local_config_digest
        or topology != prepared.object_topology
        or source_identity_digest != prepared.source_identity_digest
        or process_policy_digest != prepared.git_process_policy_digest
    ):
        _fail("task_source_changed")
    return source_identity_digest


def _make_source_trust_receipt(
    prepared: PreparedTaskSource,
    source_identity_before_digest: str,
    topology_before: ObjectTopologySeal,
    source_identity_after_digest: str,
    topology_after: ObjectTopologySeal,
    process_policy_digest: str,
) -> CanonicalReceipt:
    try:
        return make_receipt(
            "task_source_trust",
            prepared.source.input_digest,
            None,
            None,
            {
                "task_id": prepared.source.task_id,
                "provisioning_class": prepared.source.provisioning_class,
                "operator_attested": prepared.source.operator_attested,
                "local_clone_policy": prepared.source.local_clone_policy,
                "object_format": prepared.object_format,
                "source_identity_before_digest": (
                    source_identity_before_digest
                ),
                "object_topology_before_digest": (
                    topology_before.object_topology_digest
                ),
                "source_identity_after_digest": (
                    source_identity_after_digest
                ),
                "object_topology_after_digest": (
                    topology_after.object_topology_digest
                ),
                "git_process_policy_digest": process_policy_digest,
                "inventory_file_count": topology_before.file_count,
                "inventory_total_bytes": topology_before.total_bytes,
            },
        )
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _copy_validated_receipt_projection(
    receipt: CanonicalReceipt,
    expected_type: str,
    policy: TaskSnapshotPolicy,
) -> CanonicalReceipt:
    expected_keys = _TASK_RECEIPT_PAYLOAD_KEYS.get(expected_type)
    if type(expected_keys) is not frozenset:
        _fail("task_snapshot_receipt_invalid")
    payload = _receipt_payload_snapshot(receipt.payload, expected_keys)
    _validate_receipt_payload_bounds(payload, expected_type, policy)
    return CanonicalReceipt(
        receipt_type=receipt.receipt_type,
        input_digest=receipt.input_digest,
        plan_digest=receipt.plan_digest,
        previous_record_hash=receipt.previous_record_hash,
        payload=payload,
        canonical_bytes=bytes(receipt.canonical_bytes),
        receipt_digest=receipt.receipt_digest,
    )


def _make_operational_seal(
    captured: CapturedTaskObjects,
    protected_identity_keys: Tuple[Tuple[int, int, str], ...],
) -> _CapturedOperationalSeal:
    if type(captured) is not CapturedTaskObjects:
        _fail("task_snapshot_receipt_invalid")
    if type(protected_identity_keys) is not tuple or len(
        protected_identity_keys
    ) != 2:
        _fail("task_snapshot_receipt_invalid")
    for identity_key in protected_identity_keys:
        _validate_identity_key(
            identity_key,
            "task_snapshot_receipt_invalid",
        )
    return _CapturedOperationalSeal(
        source_trust_receipt=_copy_validated_receipt_projection(
            captured.source_trust_receipt,
            "task_source_trust",
            captured.policy,
        ),
        source=_copy_task_source(captured.source),
        policy=_copy_task_snapshot_policy(captured.policy),
        object_format=captured.object_format,
        commit_oid=captured.commit_oid,
        tree_oid=captured.tree_oid,
        entry_digest=captured.entry_digest,
        entries=tuple(
            TaskTreeEntry(
                path=entry.path,
                git_mode=entry.git_mode,
                blob_oid=entry.blob_oid,
                size=entry.size,
                content_digest=entry.content_digest,
            )
            for entry in captured.entries
        ),
        blobs=MappingProxyType(
            {
                oid: bytes(content)
                for oid, content in captured.blobs.items()
            }
        ),
        file_count=captured.file_count,
        total_bytes=captured.total_bytes,
        unique_blob_count=captured.unique_blob_count,
        unique_blob_bytes=captured.unique_blob_bytes,
        protected_identity_keys=tuple(protected_identity_keys),
    )


def _expected_materialized_tree_document(
    seal: _CapturedOperationalSeal,
) -> Dict[str, object]:
    try:
        if (
            not _exact_fields(seal, _CapturedOperationalSeal)
            or type(seal.entries) is not tuple
            or len(seal.entries) > seal.policy.max_files
        ):
            _fail("task_snapshot_receipt_invalid")
        directories = {"."}
        records = []
        total_bytes = 0
        for entry in seal.entries:
            if not _exact_fields(entry, TaskTreeEntry):
                _fail("task_snapshot_receipt_invalid")
            components = _task_path_components(entry.path, seal.policy)
            prefix = []
            for component in components[:-1]:
                prefix.append(component)
                directories.add("/".join(prefix))
            target_mode = 0o444 if entry.git_mode == "100644" else 0o555
            records.append(
                {
                    "content_digest": entry.content_digest,
                    "kind": "file",
                    "mode": target_mode,
                    "path": entry.path,
                    "size": entry.size,
                }
            )
            total_bytes += entry.size
        if (
            len(seal.entries) != seal.file_count
            or total_bytes != seal.total_bytes
            or len(directories) + len(seal.entries)
            > seal.policy.max_tree_entries
        ):
            _fail("task_snapshot_receipt_invalid")
        records.extend(
            {
                "content_digest": None,
                "kind": "directory",
                "mode": 0o555,
                "path": path,
                "size": 0,
            }
            for path in directories
        )
        records.sort(key=lambda item: item["path"].encode("utf-8"))
        return {
            "directory_count": len(directories),
            "document_type": "task-materialized-tree-v1",
            "entry_count": len(records),
            "file_count": seal.file_count,
            "records": records,
            "schema_version": 1,
            "total_bytes": seal.total_bytes,
        }
    except TaskSnapshotError:
        raise
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _target_root_identity_document(
    metadata: os.stat_result,
    materialized_tree_digest: str,
    identity_inventory: Tuple[
        Tuple[str, os.stat_result, str], ...
    ],
) -> Dict[str, object]:
    try:
        if (
            type(metadata) is not os.stat_result
            or not _valid_digest(materialized_tree_digest)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            _fail("task_target_changed")
        if (
            type(identity_inventory) is not tuple
            or not identity_inventory
            or len(identity_inventory)
            > _PUBLIC_TASK_TREE_PATH_POLICY.max_tree_entries
        ):
            _fail("task_target_changed")
        records = []
        previous_path = None
        for path, item_metadata, kind in identity_inventory:
            if (
                type(path) is not str
                or type(item_metadata) is not os.stat_result
                or type(kind) is not str
                or kind not in ("directory", "file")
                or (
                    path == "."
                    and (
                        kind != "directory"
                        or not _same_identity(item_metadata, metadata)
                    )
                )
                or (
                    path != "."
                    and not path
                )
            ):
                _fail("task_target_changed")
            if path != ".":
                try:
                    _task_path_components(
                        path,
                        _PUBLIC_TASK_TREE_PATH_POLICY,
                    )
                except Exception:
                    _fail("task_target_changed")
            if (
                previous_path is not None
                and previous_path.encode("utf-8")
                >= path.encode("utf-8")
            ):
                _fail("task_target_changed")
            if (
                kind == "directory"
                and not stat.S_ISDIR(item_metadata.st_mode)
            ) or (
                kind == "file"
                and not stat.S_ISREG(item_metadata.st_mode)
            ):
                _fail("task_target_changed")
            previous_path = path
            records.append(
                {
                    "identity": _identity(item_metadata, kind),
                    "path": path,
                }
            )
        return {
            "document_type": "task-materialized-root-identity-v2",
            "entry_count": len(records),
            "materialized_tree_digest": materialized_tree_digest,
            "records": records,
            "schema_version": 2,
        }
    except Exception:
        _fail("task_target_changed")


def _make_task_snapshot_receipt(
    seal: _CapturedOperationalSeal,
    materialized_tree_digest: str,
) -> CanonicalReceipt:
    try:
        if (
            not _exact_fields(seal, _CapturedOperationalSeal)
            or not _valid_digest(materialized_tree_digest)
        ):
            _fail("task_snapshot_receipt_invalid")
        receipt = make_receipt(
            "task_snapshot",
            seal.source.input_digest,
            None,
            None,
            {
                "task_id": seal.source.task_id,
                "object_format": seal.object_format,
                "commit_oid": seal.commit_oid,
                "tree_oid": seal.tree_oid,
                "entry_digest": seal.entry_digest,
                "materialized_tree_digest": materialized_tree_digest,
                "materializer_policy_version": seal.policy.policy_version,
                "file_count": seal.file_count,
                "total_bytes": seal.total_bytes,
                "source_trust_receipt_digest": (
                    seal.source_trust_receipt.receipt_digest
                ),
            },
        )
        return _reconstruct_receipt(
            receipt,
            "task_snapshot",
            seal.policy,
        )
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _validate_target_names(
    current_name: str,
    lean_name: str,
    policy: TaskSnapshotPolicy,
) -> Tuple[str, str]:
    _validate_policy(policy)
    checked = []
    for value in (current_name, lean_name):
        if (
            type(value) is not str
            or not value
            or len(value) > policy.max_component_bytes
        ):
            _fail("task_target_invalid")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            _fail("task_target_invalid")
        if (
            len(encoded) > policy.max_component_bytes
            or value in (".", "..")
            or "/" in value
            or "\\" in value
            or "\0" in value
            or unicodedata.normalize("NFC", value) != value
            or value.endswith(".")
            or value.endswith(" ")
            or any(
                unicodedata.category(character) in ("Cc", "Cf", "Cs")
                for character in value
            )
        ):
            _fail("task_target_invalid")
        checked.append(value)
    if (
        checked[0] == checked[1]
        or unicodedata.normalize("NFC", checked[0])
        == unicodedata.normalize("NFC", checked[1])
        or _task_name_key(checked[0]) == _task_name_key(checked[1])
    ):
        _fail("task_target_invalid")
    return (checked[0], checked[1])


def _target_paths_overlap(first: str, second: str) -> bool:
    try:
        common = os.path.commonpath((first, second))
    except (OSError, TypeError, ValueError):
        _fail("task_target_invalid")
    return common == first or common == second


def _target_directory_identity_key(
    metadata: os.stat_result,
) -> Tuple[int, int, str]:
    if (
        type(metadata.st_dev) is not int
        or metadata.st_dev < 0
        or type(metadata.st_ino) is not int
        or metadata.st_ino < 0
    ):
        _fail("task_target_invalid")
    return (metadata.st_dev, metadata.st_ino, "directory")


def _require_target_ancestor(
    metadata: os.stat_result,
    next_metadata: os.stat_result,
) -> None:
    uid = os.getuid()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in (0, uid)
    ):
        _fail("task_target_invalid")
    if metadata.st_mode & 0o022:
        sticky_exception = (
            metadata.st_uid == 0
            and bool(metadata.st_mode & stat.S_ISVTX)
            and next_metadata.st_uid == uid
            and not next_metadata.st_mode & 0o022
        )
        if not sticky_exception:
            _fail("task_target_invalid")


@contextmanager
def _open_target_parent_gate(
    target_parent: Path,
    current_name: str,
    lean_name: str,
    source_root: Path,
    git_dir: Path,
    protected_identity_keys: Tuple[Tuple[int, int, str], ...],
    policy: TaskSnapshotPolicy,
) -> _TargetParentGate:
    current_owner = _SourceResourceOwner()
    gate = None
    yielded = False
    try:
        checked_names = _validate_target_names(
            current_name,
            lean_name,
            policy,
        )
        try:
            raw_parent = os.fspath(target_parent)
        except Exception:
            _fail("task_target_invalid")
        raw_source = os.fspath(source_root)
        raw_git_dir = os.fspath(git_dir)
        if (
            type(raw_parent) is not str
            or not os.path.isabs(raw_parent)
            or os.path.normpath(raw_parent) != raw_parent
            or os.path.realpath(raw_parent) != raw_parent
            or type(raw_source) is not str
            or type(raw_git_dir) is not str
            or type(protected_identity_keys) is not tuple
            or len(protected_identity_keys) != 2
        ):
            _fail("task_target_invalid")
        for identity_key in protected_identity_keys:
            _validate_identity_key(identity_key, "task_target_invalid")
        planned_paths = (
            raw_parent,
            os.path.join(raw_parent, checked_names[0]),
            os.path.join(raw_parent, checked_names[1]),
        )
        for planned in planned_paths:
            for protected_path in (raw_source, raw_git_dir):
                if _target_paths_overlap(planned, protected_path):
                    _fail("task_target_invalid")

        components = Path(raw_parent).parts[1:]
        if not components:
            _fail("task_target_invalid")
        current_owner.descriptor = os.open(os.sep, _directory_flags())
        current_metadata = os.fstat(current_owner.descriptor)
        if (
            _target_directory_identity_key(current_metadata)
            in protected_identity_keys
        ):
            _fail("task_target_invalid")
        for component in components:
            observed = os.stat(
                component,
                dir_fd=current_owner.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
            ):
                _fail("task_target_invalid")
            _require_target_ancestor(current_metadata, observed)
            child_owner = _SourceResourceOwner()
            try:
                child_owner.descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_owner.descriptor,
                )
                opened = os.fstat(child_owner.descriptor)
                if not _same_identity(observed, opened):
                    _fail("task_target_invalid")
                if (
                    _target_directory_identity_key(opened)
                    in protected_identity_keys
                ):
                    _fail("task_target_invalid")
                if (
                    _stable_ancestor_identity(current_metadata)
                    != _stable_ancestor_identity(
                        os.fstat(current_owner.descriptor)
                    )
                ):
                    _fail("task_target_invalid")
                if not _guard_cleanup_boolean(
                    current_owner.close_descriptor
                ):
                    _fail("task_snapshot_cleanup_required")
                current_owner, child_owner = child_owner, current_owner
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not _guard_cleanup_boolean(child_owner.close):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
            current_metadata = opened
        if (
            current_metadata.st_uid != os.getuid()
            or stat.S_IMODE(current_metadata.st_mode) != 0o700
            or not stat.S_ISDIR(current_metadata.st_mode)
            or stat.S_ISLNK(current_metadata.st_mode)
        ):
            _fail("task_target_invalid")
        for name in checked_names:
            try:
                os.stat(
                    name,
                    dir_fd=current_owner.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except (OSError, TypeError, ValueError, OverflowError):
                _fail("task_target_invalid")
            _fail("task_target_invalid")
        final_metadata = os.fstat(current_owner.descriptor)
        if (
            not _same_identity(current_metadata, final_metadata)
            or final_metadata.st_uid != os.getuid()
            or final_metadata.st_dev != current_metadata.st_dev
            or stat.S_IMODE(final_metadata.st_mode) != 0o700
            or not stat.S_ISDIR(final_metadata.st_mode)
            or stat.S_ISLNK(final_metadata.st_mode)
        ):
            _fail("task_target_invalid")
        current_metadata = final_metadata
        gate = _TargetParentGate(
            _descriptor_owner=current_owner,
            target_parent=Path(raw_parent),
            metadata=current_metadata,
            device=current_metadata.st_dev,
            current_name=checked_names[0],
            lean_name=checked_names[1],
            _owns_descriptor=False,
        )
        yielded = True
        yield gate
    except TaskSnapshotError:
        raise
    except Exception:
        if yielded:
            raise
        _fail("task_target_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        cleanup_ok = (
            True
            if gate is not None and gate._owns_descriptor
            else _guard_cleanup_boolean(current_owner.close)
        )
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _owned_target_path(
    entry: _OwnedTargetEntry,
) -> Tuple[str, ...]:
    return entry.parent_components + (entry.basename,)


def _begin_owned_target_entry(
    ledger: _TargetOwnershipLedger,
    parent_components: Tuple[str, ...],
    basename: str,
    kind: str,
) -> _OwnedTargetEntry:
    entry = _OwnedTargetEntry(
        parent_components=parent_components,
        basename=basename,
        kind=kind,
    )
    path = _owned_target_path(entry)
    if path in ledger.by_path:
        ledger.close_uncertain = True
        _fail("task_snapshot_cleanup_required")
    ledger.entries.append(entry)
    ledger.by_path[path] = entry
    return entry


def _target_ownership_token(
    metadata: os.stat_result,
    kind: str,
) -> Tuple[int, int, int, int, str]:
    values = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
    )
    if (
        type(metadata) is not os.stat_result
        or type(kind) is not str
        or kind not in ("directory", "file")
        or any(type(value) is not int or value < 0 for value in values)
    ):
        _fail("task_snapshot_cleanup_required")
    return values + (kind,)


def _target_metadata_matches_token(
    metadata: os.stat_result,
    token: object,
    kind: str,
) -> bool:
    if (
        type(metadata) is not os.stat_result
        or type(token) is not tuple
        or len(token) != 5
        or type(kind) is not str
        or token[4] != kind
        or any(type(value) is not int or value < 0 for value in token[:4])
    ):
        return False
    expected_kind = (
        stat.S_ISDIR(metadata.st_mode)
        if kind == "directory"
        else stat.S_ISREG(metadata.st_mode)
    )
    return (
        expected_kind
        and not stat.S_ISLNK(metadata.st_mode)
        and (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            kind,
        )
        == token
    )


def _valid_owned_metadata(
    metadata: os.stat_result,
    device: int,
    kind: str,
    mode: Optional[int] = None,
) -> bool:
    if (
        type(metadata) is not os.stat_result
        or type(device) is not int
        or metadata.st_dev != device
        or metadata.st_uid != os.getuid()
        or type(metadata.st_nlink) is not int
        or metadata.st_nlink < 1
    ):
        return False
    if kind == "directory":
        kind_ok = stat.S_ISDIR(metadata.st_mode)
    elif kind == "file":
        kind_ok = (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
        )
    else:
        return False
    return (
        kind_ok
        and not stat.S_ISLNK(metadata.st_mode)
        and (
            mode is None
            or stat.S_IMODE(metadata.st_mode) == mode
        )
    )


def _target_descriptor_owner_snapshot(
    descriptors: Sequence[object],
) -> Tuple[_SourceResourceOwner, ...]:
    owners = []
    for descriptor in reversed(tuple(descriptors)):
        if type(descriptor) is _SourceResourceOwner:
            owners.append(descriptor)
        elif descriptor >= 0:
            owners.append(_SourceResourceOwner(descriptor=descriptor))
    return tuple(owners)


def _drain_target_descriptor_owners(
    owners: Sequence[_SourceResourceOwner],
    ledger: _TargetOwnershipLedger,
) -> bool:
    cleanup_ok = True
    for owner in owners:
        closed = _guard_cleanup_boolean(owner.close_descriptor)
        if owner.has_resources():
            closed = (
                _guard_cleanup_boolean(owner.close_descriptor)
                and closed
            )
        if not closed:
            ledger.close_uncertain = True
            cleanup_ok = False
    return cleanup_ok


def _close_target_descriptors(
    descriptors: Sequence[object],
    ledger: _TargetOwnershipLedger,
) -> bool:
    try:
        owner_snapshot = _target_descriptor_owner_snapshot(descriptors)
        return _drain_target_descriptor_owners(owner_snapshot, ledger)
    except Exception:
        ledger.close_uncertain = True
        try:
            recovery_snapshot = owner_snapshot
        except UnboundLocalError:
            recovery_snapshot = _target_descriptor_owner_snapshot(
                descriptors
            )
        _guard_cleanup_boolean(
            _drain_target_descriptor_owners,
            recovery_snapshot,
            ledger,
        )
        return False


def _require_live_target_parent(
    gate: _TargetParentGate,
    code: str,
) -> os.stat_result:
    metadata = os.fstat(gate.descriptor)
    if (
        type(metadata) is not os.stat_result
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_dev != gate.metadata.st_dev
        or metadata.st_ino != gate.metadata.st_ino
        or metadata.st_uid != gate.metadata.st_uid
        or metadata.st_gid != gate.metadata.st_gid
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        _fail(code)
    return metadata


def _open_owned_directory_chain(
    gate: _TargetParentGate,
    components: Tuple[str, ...],
    ledger: _TargetOwnershipLedger,
    code: str,
    opened_owners: list,
) -> int:
    current_descriptor = gate.descriptor
    walked = []
    try:
        if type(opened_owners) is not list or opened_owners:
            _fail(code)
        _require_live_target_parent(gate, code)
        for component in components:
            walked.append(component)
            path = tuple(walked)
            expected = ledger.by_path.get(path)
            if (
                type(expected) is not _OwnedTargetEntry
                or expected.kind != "directory"
                or expected.token is None
            ):
                _fail(code)
            observed = os.stat(
                component,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            child_owner = _SourceResourceOwner()
            child_published = False
            try:
                child_owner.descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_descriptor,
                )
                opened = os.fstat(child_owner.descriptor)
                if (
                    not _same_identity(observed, opened)
                    or not _valid_owned_metadata(
                        opened,
                        gate.device,
                        "directory",
                    )
                    or not _target_metadata_matches_token(
                        opened,
                        expected.token,
                        "directory",
                    )
                ):
                    _fail(code)
                opened_owners.append(child_owner)
                current_descriptor = child_owner.descriptor
                child_published = True
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not child_published and not (
                    _guard_cleanup_boolean(
                        _close_target_descriptors,
                        (child_owner,),
                        ledger,
                    )
                ):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
        return current_descriptor
    except BaseException as caught:
        cleanup_ok = _guard_cleanup_boolean(
            _close_target_descriptors,
            opened_owners,
            ledger,
        )
        if not isinstance(caught, Exception):
            raise
        if not cleanup_ok:
            _fail("task_snapshot_cleanup_required")
        raise


def _create_owned_directory(
    gate: _TargetParentGate,
    parent_components: Tuple[str, ...],
    basename: str,
    ledger: _TargetOwnershipLedger,
) -> None:
    parent_descriptor = -1
    parent_stack = []
    child_owner = _SourceResourceOwner()
    try:
        parent_descriptor = _open_owned_directory_chain(
            gate,
            parent_components,
            ledger,
            "task_target_invalid",
            parent_stack,
        )
        owned = _begin_owned_target_entry(
            ledger,
            parent_components,
            basename,
            "directory",
        )
        os.mkdir(
            basename,
            0o700,
            dir_fd=parent_descriptor,
        )
        owned.created = True
        observed = os.stat(
            basename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        child_owner.descriptor = os.open(
            basename,
            _directory_flags(),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(child_owner.descriptor)
        if (
            not _same_identity(observed, opened)
            or not _valid_owned_metadata(
                opened,
                gate.device,
                "directory",
            )
        ):
            _fail("task_snapshot_cleanup_required")
        owned.token = _target_ownership_token(opened, "directory")
        os.fchmod(child_owner.descriptor, 0o700)
        final_metadata = os.fstat(child_owner.descriptor)
        if (
            not _target_metadata_matches_token(
                final_metadata,
                owned.token,
                "directory",
            )
            or not _valid_owned_metadata(
                final_metadata,
                gate.device,
                "directory",
                0o700,
            )
        ):
            _fail("task_target_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        descriptors = [child_owner]
        descriptors.extend(parent_stack)
        if not _guard_cleanup_boolean(
            _close_target_descriptors,
            descriptors,
            ledger,
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _write_all_target_bytes(
    descriptor: int,
    content: bytes,
) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except InterruptedError:
            continue
        if (
            type(written) is not int
            or written <= 0
            or written > len(content) - offset
        ):
            _fail("task_target_invalid")
        offset += written


def _read_exact_target_bytes(
    descriptor: int,
    expected_size: int,
    code: str,
) -> bytes:
    chunks = []
    remaining = expected_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
        except InterruptedError:
            continue
        if type(chunk) is not bytes or not chunk:
            _fail(code)
        chunks.append(chunk)
        remaining -= len(chunk)
    while True:
        try:
            extra = os.read(descriptor, 1)
            break
        except InterruptedError:
            continue
    if type(extra) is not bytes or extra:
        _fail(code)
    return b"".join(chunks)


def _create_owned_file(
    gate: _TargetParentGate,
    root_name: str,
    entry: TaskTreeEntry,
    content: bytes,
    policy: TaskSnapshotPolicy,
    ledger: _TargetOwnershipLedger,
) -> None:
    path_components = _task_path_components(entry.path, policy)
    parent_components = (root_name,) + path_components[:-1]
    basename = path_components[-1]
    parent_descriptor = -1
    parent_stack = []
    write_owner = _SourceResourceOwner()
    read_owner = _SourceResourceOwner()
    try:
        parent_descriptor = _open_owned_directory_chain(
            gate,
            parent_components,
            ledger,
            "task_target_invalid",
            parent_stack,
        )
        owned = _begin_owned_target_entry(
            ledger,
            parent_components,
            basename,
            "file",
        )
        write_owner.descriptor = os.open(
            basename,
            (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
            ),
            0o600,
            dir_fd=parent_descriptor,
        )
        owned.created = True
        opened = os.fstat(write_owner.descriptor)
        if not _valid_owned_metadata(
            opened,
            gate.device,
            "file",
        ):
            _fail("task_snapshot_cleanup_required")
        owned.token = _target_ownership_token(opened, "file")
        _write_all_target_bytes(write_owner.descriptor, content)
        target_mode = 0o444 if entry.git_mode == "100644" else 0o555
        os.fchmod(write_owner.descriptor, target_mode)
        written_metadata = os.fstat(write_owner.descriptor)
        if (
            not _target_metadata_matches_token(
                written_metadata,
                owned.token,
                "file",
            )
            or not _valid_owned_metadata(
                written_metadata,
                gate.device,
                "file",
                target_mode,
            )
            or written_metadata.st_size != entry.size
        ):
            _fail("task_target_invalid")
        if not _guard_cleanup_boolean(
            _close_target_descriptors,
            (write_owner,),
            ledger,
        ):
            _fail("task_snapshot_cleanup_required")

        observed = os.stat(
            basename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        read_owner.descriptor = os.open(
            basename,
            _file_flags(),
            dir_fd=parent_descriptor,
        )
        reopened = os.fstat(read_owner.descriptor)
        if (
            not _same_identity(observed, reopened)
            or not _target_metadata_matches_token(
                reopened,
                owned.token,
                "file",
            )
            or not _valid_owned_metadata(
                reopened,
                gate.device,
                "file",
                target_mode,
            )
            or reopened.st_size != entry.size
        ):
            _fail("task_target_invalid")
        reread = _read_exact_target_bytes(
            read_owner.descriptor,
            entry.size,
            "task_target_invalid",
        )
        after_read = os.fstat(read_owner.descriptor)
        if (
            not _same_identity(reopened, after_read)
            or hashlib.sha256(reread).hexdigest()
            != entry.content_digest.removeprefix("sha256:")
        ):
            _fail("task_target_invalid")
    finally:
        preserve_abort = _active_terminal_baseexception()
        descriptors = [read_owner, write_owner]
        descriptors.extend(parent_stack)
        if not _guard_cleanup_boolean(
            _close_target_descriptors,
            descriptors,
            ledger,
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _seal_owned_directories(
    gate: _TargetParentGate,
    ledger: _TargetOwnershipLedger,
) -> None:
    directories = [
        entry for entry in ledger.entries
        if entry.kind == "directory" and entry.token is not None
    ]
    directories.sort(
        key=lambda entry: (
            -len(_owned_target_path(entry)),
            _owned_target_path(entry),
        )
    )
    for entry in directories:
        parent_descriptor = -1
        parent_stack = []
        child_owner = _SourceResourceOwner()
        try:
            parent_descriptor = (
                _open_owned_directory_chain(
                    gate,
                    entry.parent_components,
                    ledger,
                    "task_target_changed",
                    parent_stack,
                )
            )
            observed = os.stat(
                entry.basename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            child_owner.descriptor = os.open(
                entry.basename,
                _directory_flags(),
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(child_owner.descriptor)
            if (
                not _same_identity(observed, opened)
                or not _target_metadata_matches_token(
                    opened,
                    entry.token,
                    "directory",
                )
            ):
                _fail("task_target_changed")
            os.fchmod(child_owner.descriptor, 0o555)
            sealed = os.fstat(child_owner.descriptor)
            if (
                not _target_metadata_matches_token(
                    sealed,
                    entry.token,
                    "directory",
                )
                or not _valid_owned_metadata(
                    sealed,
                    gate.device,
                    "directory",
                    0o555,
                )
            ):
                _fail("task_target_changed")
        finally:
            preserve_abort = _active_terminal_baseexception()
            descriptors = [child_owner]
            descriptors.extend(parent_stack)
            if not _guard_cleanup_boolean(
                _close_target_descriptors,
                descriptors,
                ledger,
            ):
                _fail_cleanup_required_unless_nonexception_active(
                    preserve_abort
                )


def _open_sealed_directory_chain(
    root_descriptor: int,
    components: Tuple[str, ...],
    device: int,
    code: str,
    opened_owners: list,
) -> int:
    current_descriptor = root_descriptor
    try:
        if type(opened_owners) is not list or opened_owners:
            _fail(code)
        for component in components:
            observed = os.stat(
                component,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            child_owner = _SourceResourceOwner()
            child_published = False
            try:
                child_owner.descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_descriptor,
                )
                opened = os.fstat(child_owner.descriptor)
                if (
                    not _same_identity(observed, opened)
                    or not _valid_owned_metadata(
                        opened,
                        device,
                        "directory",
                        0o555,
                    )
                ):
                    _fail(code)
                opened_owners.append(child_owner)
                current_descriptor = child_owner.descriptor
                child_published = True
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not child_published and not _guard_cleanup_boolean(
                    child_owner.close_descriptor,
                ):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
        return current_descriptor
    except BaseException as caught:
        cleanup_ok = _guard_cleanup_boolean(
            _close_source_owners,
            opened_owners,
        )
        if not isinstance(caught, Exception):
            raise
        if not cleanup_ok:
            _fail("task_snapshot_cleanup_required")
        raise


def _bounded_directory_names(
    descriptor: int,
    remaining_entries: int,
    code: str,
    iterator_owner: _SourceResourceOwner,
) -> Tuple[str, ...]:
    if (
        type(iterator_owner) is not _SourceResourceOwner
        or iterator_owner.has_resources()
        or iterator_owner._uncertain
    ):
        _fail(code)
    names = []
    try:
        iterator_owner.iterator = os.scandir(descriptor)
        for unused_index in range(remaining_entries + 1):
            try:
                item = next(iterator_owner.iterator)
            except StopIteration:
                break
            if type(item.name) is not str:
                _fail(code)
            names.append(item.name)
            if len(names) > remaining_entries:
                _fail(code)
        try:
            return tuple(
                sorted(names, key=lambda name: name.encode("utf-8"))
            )
        except Exception:
            _fail(code)
    finally:
        preserve_abort = _active_terminal_baseexception()
        cleanup_ok = _guard_cleanup_boolean(
            iterator_owner.close_iterator
        )
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _scan_sealed_target_root(
    root_descriptor: int,
    root_metadata: os.stat_result,
    policy: TaskSnapshotPolicy,
    expected_tokens: Optional[
        Mapping[str, Tuple[int, int, int, int, str]]
    ] = None,
) -> Tuple[
    Dict[str, object],
    os.stat_result,
    Tuple[Tuple[str, os.stat_result, str], ...],
]:
    device = root_metadata.st_dev
    if not _valid_owned_metadata(
        root_metadata,
        device,
        "directory",
        0o555,
    ):
        _fail("task_target_changed")
    records = [
        {
            "content_digest": None,
            "kind": "directory",
            "mode": 0o555,
            "path": ".",
            "size": 0,
        }
    ]
    identity_metadata = {".": (root_metadata, "directory")}
    pending_directories = [()]
    pending_index = 0
    file_count = 0
    total_bytes = 0
    while pending_index < len(pending_directories):
        components = pending_directories[pending_index]
        pending_index += 1
        descriptor = root_descriptor
        descriptor_stack = []
        iterator_owner = _SourceResourceOwner()
        try:
            descriptor = _open_sealed_directory_chain(
                root_descriptor,
                components,
                device,
                "task_target_changed",
                descriptor_stack,
            )
            before_scan = os.fstat(descriptor)
            directory_path = "." if not components else "/".join(components)
            if not _valid_owned_metadata(
                before_scan,
                device,
                "directory",
                0o555,
            ) or (
                expected_tokens is not None
                and not _target_metadata_matches_token(
                    before_scan,
                    expected_tokens.get(directory_path),
                    "directory",
                )
            ):
                _fail("task_target_changed")
            names = _bounded_directory_names(
                descriptor,
                policy.max_tree_entries - len(records),
                "task_target_changed",
                iterator_owner,
            )
            name_keys = set()
            for name in names:
                try:
                    key = _task_name_key(name)
                except Exception:
                    _fail("task_target_changed")
                if key in name_keys:
                    _fail("task_target_changed")
                name_keys.add(key)
                child_components = components + (name,)
                relative_path = "/".join(child_components)
                try:
                    _task_path_components(relative_path, policy)
                except Exception:
                    _fail("task_target_changed")
                observed = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(observed.st_mode):
                    child_owner = _SourceResourceOwner()
                    try:
                        child_owner.descriptor = os.open(
                            name,
                            _directory_flags(),
                            dir_fd=descriptor,
                        )
                        opened = os.fstat(child_owner.descriptor)
                        if (
                            not _same_identity(observed, opened)
                            or not _valid_owned_metadata(
                                opened,
                                device,
                                "directory",
                                0o555,
                            )
                            or (
                                expected_tokens is not None
                                and not _target_metadata_matches_token(
                                    opened,
                                    expected_tokens.get(relative_path),
                                    "directory",
                                )
                            )
                        ):
                            _fail("task_target_changed")
                    finally:
                        preserve_abort = _active_terminal_baseexception()
                        if not _guard_cleanup_boolean(
                            child_owner.close_descriptor
                        ):
                            (
                                _fail_cleanup_required_unless_nonexception_active(
                                    preserve_abort
                                )
                            )
                    pending_directories.append(child_components)
                    records.append(
                        {
                            "content_digest": None,
                            "kind": "directory",
                            "mode": 0o555,
                            "path": relative_path,
                            "size": 0,
                        }
                    )
                    continue
                if not stat.S_ISREG(observed.st_mode):
                    _fail("task_target_changed")
                file_count += 1
                if (
                    file_count > policy.max_files
                    or type(observed.st_size) is not int
                    or observed.st_size < 0
                    or observed.st_size > policy.max_file_bytes
                    or total_bytes + observed.st_size
                    > policy.max_total_bytes
                ):
                    _fail("task_target_changed")
                mode = stat.S_IMODE(observed.st_mode)
                if mode not in (0o444, 0o555):
                    _fail("task_target_changed")
                file_owner = _SourceResourceOwner()
                try:
                    file_owner.descriptor = os.open(
                        name,
                        _file_flags(),
                        dir_fd=descriptor,
                    )
                    opened = os.fstat(file_owner.descriptor)
                    if (
                        not _same_identity(observed, opened)
                        or not _valid_owned_metadata(
                            opened,
                            device,
                            "file",
                            mode,
                        )
                        or (
                            expected_tokens is not None
                            and not _target_metadata_matches_token(
                                opened,
                                expected_tokens.get(relative_path),
                                "file",
                            )
                        )
                    ):
                        _fail("task_target_changed")
                    content = _read_exact_target_bytes(
                        file_owner.descriptor,
                        observed.st_size,
                        "task_target_changed",
                    )
                    final_file = os.fstat(file_owner.descriptor)
                    if not _same_identity(opened, final_file):
                        _fail("task_target_changed")
                    identity_metadata[relative_path] = (
                        final_file,
                        "file",
                    )
                finally:
                    preserve_abort = _active_terminal_baseexception()
                    if not _guard_cleanup_boolean(
                        file_owner.close_descriptor
                    ):
                        (
                            _fail_cleanup_required_unless_nonexception_active(
                                preserve_abort
                            )
                        )
                total_bytes += len(content)
                records.append(
                    {
                        "content_digest": (
                            "sha256:"
                            + hashlib.sha256(content).hexdigest()
                        ),
                        "kind": "file",
                        "mode": mode,
                        "path": relative_path,
                        "size": len(content),
                    }
                )
            after_scan = os.fstat(descriptor)
            if not _same_identity(before_scan, after_scan):
                _fail("task_target_changed")
            identity_metadata[directory_path] = (
                after_scan,
                "directory",
            )
        finally:
            preserve_abort = _active_terminal_baseexception()
            cleanup_ok = _guard_cleanup_boolean(
                iterator_owner.close_iterator
            )
            cleanup_ok = _guard_cleanup_boolean(
                _close_source_owners,
                descriptor_stack,
            ) and cleanup_ok
            if not cleanup_ok:
                _fail_cleanup_required_unless_nonexception_active(
                    preserve_abort
                )
    final_root = os.fstat(root_descriptor)
    if not _same_identity(root_metadata, final_root):
        _fail("task_target_changed")
    identity_metadata["."] = (final_root, "directory")
    if (
        expected_tokens is not None
        and frozenset(expected_tokens) != frozenset(identity_metadata)
    ):
        _fail("task_target_changed")
    records.sort(key=lambda record: record["path"].encode("utf-8"))
    directory_count = sum(
        record["kind"] == "directory" for record in records
    )
    return (
        {
            "directory_count": directory_count,
            "document_type": "task-materialized-tree-v1",
            "entry_count": len(records),
            "file_count": file_count,
            "records": records,
            "schema_version": 1,
            "total_bytes": total_bytes,
        },
        final_root,
        tuple(
            (
                path,
                identity_metadata[path][0],
                identity_metadata[path][1],
            )
            for path in sorted(
                identity_metadata,
                key=lambda value: value.encode("utf-8"),
            )
        ),
    )


def _scan_owned_target_root(
    gate: _TargetParentGate,
    root_name: str,
    root_entry: _OwnedTargetEntry,
    policy: TaskSnapshotPolicy,
    ledger: _TargetOwnershipLedger,
) -> Tuple[
    Dict[str, object],
    os.stat_result,
    Tuple[Tuple[str, os.stat_result, str], ...],
]:
    root_owner = _SourceResourceOwner()
    try:
        _require_live_target_parent(gate, "task_target_changed")
        observed = os.stat(
            root_name,
            dir_fd=gate.descriptor,
            follow_symlinks=False,
        )
        root_owner.descriptor = os.open(
            root_name,
            _directory_flags(),
            dir_fd=gate.descriptor,
        )
        opened = os.fstat(root_owner.descriptor)
        if (
            not _same_identity(observed, opened)
            or not _target_metadata_matches_token(
                opened,
                root_entry.token,
                "directory",
            )
            or not _valid_owned_metadata(
                opened,
                gate.device,
                "directory",
                0o555,
            )
        ):
            _fail("task_target_changed")
        token_prefix = (root_name,)
        expected_tokens = {".": root_entry.token}
        for path, owned in ledger.by_path.items():
            if (
                path[:1] == token_prefix
                and len(path) > 1
                and owned.token is not None
            ):
                expected_tokens["/".join(path[1:])] = owned.token
        return _scan_sealed_target_root(
            root_owner.descriptor,
            opened,
            policy,
            MappingProxyType(expected_tokens),
        )
    finally:
        preserve_abort = _active_terminal_baseexception()
        if not _guard_cleanup_boolean(
            root_owner.close_descriptor,
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _valid_target_ancestor_metadata(
    parent: os.stat_result,
    child: os.stat_result,
) -> bool:
    uid = os.getuid()
    if (
        type(parent) is not os.stat_result
        or type(child) is not os.stat_result
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid not in (0, uid)
    ):
        return False
    if parent.st_mode & 0o022:
        return (
            parent.st_uid == 0
            and bool(parent.st_mode & stat.S_ISVTX)
            and child.st_uid == uid
            and not child.st_mode & 0o022
        )
    return True


@contextmanager
def _open_materialized_snapshot_root(
    target_root: Path,
) -> _MaterializedRootGate:
    current_owner = _SourceResourceOwner()
    gate = None
    yielded = False
    try:
        raw_root = os.fspath(target_root)
        if (
            type(raw_root) is not str
            or not os.path.isabs(raw_root)
            or os.path.normpath(raw_root) != raw_root
            or os.path.realpath(raw_root) != raw_root
        ):
            _fail("task_target_changed")
        components = Path(raw_root).parts[1:]
        if not components:
            _fail("task_target_changed")
        current_owner.descriptor = os.open(os.sep, _directory_flags())
        current_metadata = os.fstat(current_owner.descriptor)
        for component_index, component in enumerate(components):
            if (
                component_index == len(components) - 1
                and (
                    current_metadata.st_uid != os.getuid()
                    or stat.S_IMODE(current_metadata.st_mode) != 0o700
                    or not stat.S_ISDIR(current_metadata.st_mode)
                    or stat.S_ISLNK(current_metadata.st_mode)
                )
            ):
                _fail("task_target_changed")
            observed = os.stat(
                component,
                dir_fd=current_owner.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or not _valid_target_ancestor_metadata(
                    current_metadata,
                    observed,
                )
            ):
                _fail("task_target_changed")
            child_owner = _SourceResourceOwner()
            try:
                child_owner.descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_owner.descriptor,
                )
                opened = os.fstat(child_owner.descriptor)
                if not _same_identity(observed, opened):
                    _fail("task_target_changed")
                if (
                    _stable_ancestor_identity(current_metadata)
                    != _stable_ancestor_identity(
                        os.fstat(current_owner.descriptor)
                    )
                ):
                    _fail("task_target_changed")
                if not _guard_cleanup_boolean(
                    current_owner.close_descriptor
                ):
                    _fail("task_snapshot_cleanup_required")
                current_owner, child_owner = child_owner, current_owner
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not _guard_cleanup_boolean(child_owner.close):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
            current_metadata = opened
        if (
            current_metadata.st_uid != os.getuid()
            or stat.S_IMODE(current_metadata.st_mode) != 0o555
            or not stat.S_ISDIR(current_metadata.st_mode)
            or stat.S_ISLNK(current_metadata.st_mode)
        ):
            _fail("task_target_changed")
        gate = _MaterializedRootGate(
            _descriptor_owner=current_owner,
            metadata=current_metadata,
            _owns_descriptor=True,
        )
        yielded = True
        yield gate
    except TaskSnapshotError:
        raise
    except Exception:
        if yielded:
            raise
        _fail("task_target_changed")
    finally:
        preserve_abort = _active_terminal_baseexception()
        cleanup_ok = _guard_cleanup_boolean(
            gate.close if gate is not None else current_owner.close
        )
        if not cleanup_ok:
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _copy_materialized_snapshot(
    snapshot: MaterializedTaskSnapshot,
    policy: TaskSnapshotPolicy,
) -> MaterializedTaskSnapshot:
    try:
        if (
            not _exact_fields(snapshot, MaterializedTaskSnapshot)
            or type(snapshot.target_root) is not type(Path())
            or type(snapshot.target_identity_digest) is not str
            or not _valid_digest(snapshot.target_identity_digest)
            or type(snapshot.materialized_tree_digest) is not str
            or not _valid_digest(snapshot.materialized_tree_digest)
            or type(snapshot.file_count) is not int
            or snapshot.file_count < 0
            or snapshot.file_count > policy.max_files
            or type(snapshot.total_bytes) is not int
            or snapshot.total_bytes < 0
            or snapshot.total_bytes > policy.max_total_bytes
        ):
            _fail("task_snapshot_receipt_invalid")
        raw_root = os.fspath(snapshot.target_root)
        if (
            type(raw_root) is not str
            or not os.path.isabs(raw_root)
            or os.path.normpath(raw_root) != raw_root
        ):
            _fail("task_snapshot_receipt_invalid")
        detached = MaterializedTaskSnapshot(
            snapshot_receipt=snapshot.snapshot_receipt,
            target_root=Path(raw_root),
            target_identity_digest=snapshot.target_identity_digest,
            materialized_tree_digest=snapshot.materialized_tree_digest,
            file_count=snapshot.file_count,
            total_bytes=snapshot.total_bytes,
        )
        receipt = _reconstruct_receipt(
            detached.snapshot_receipt,
            "task_snapshot",
            policy,
        )
        if (
            detached.file_count > policy.max_files
            or detached.total_bytes > policy.max_total_bytes
            or receipt.payload["materializer_policy_version"]
            != policy.policy_version
        ):
            _fail("task_snapshot_receipt_invalid")
        object.__setattr__(detached, "snapshot_receipt", receipt)
        return detached
    except Exception:
        _fail("task_snapshot_receipt_invalid")


def _rebind_owned_pair_namespace(
    gate: _TargetParentGate,
    ledger: _TargetOwnershipLedger,
    root_metadata: Mapping[str, os.stat_result],
) -> None:
    current_owner = _SourceResourceOwner()
    try:
        raw_parent = os.fspath(gate.target_parent)
        if (
            type(raw_parent) is not str
            or not os.path.isabs(raw_parent)
            or os.path.normpath(raw_parent) != raw_parent
            or os.path.realpath(raw_parent) != raw_parent
        ):
            _fail("task_target_changed")
        current_owner.descriptor = os.open(os.sep, _directory_flags())
        current_metadata = os.fstat(current_owner.descriptor)
        for component in Path(raw_parent).parts[1:]:
            observed = os.stat(
                component,
                dir_fd=current_owner.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or not _valid_target_ancestor_metadata(
                    current_metadata,
                    observed,
                )
            ):
                _fail("task_target_changed")
            child_owner = _SourceResourceOwner()
            try:
                child_owner.descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_owner.descriptor,
                )
                opened = os.fstat(child_owner.descriptor)
                if not _same_identity(observed, opened):
                    _fail("task_target_changed")
                stable_before = (
                    current_metadata.st_dev,
                    current_metadata.st_ino,
                    current_metadata.st_uid,
                    current_metadata.st_gid,
                    stat.S_IMODE(current_metadata.st_mode),
                )
                rechecked_parent = os.fstat(current_owner.descriptor)
                stable_after = (
                    rechecked_parent.st_dev,
                    rechecked_parent.st_ino,
                    rechecked_parent.st_uid,
                    rechecked_parent.st_gid,
                    stat.S_IMODE(rechecked_parent.st_mode),
                )
                if stable_before != stable_after:
                    _fail("task_target_changed")
                if not _guard_cleanup_boolean(
                    _close_target_descriptors,
                    (current_owner,),
                    ledger,
                ):
                    _fail("task_snapshot_cleanup_required")
                current_owner, child_owner = child_owner, current_owner
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not _guard_cleanup_boolean(
                    _close_target_descriptors,
                    (child_owner,),
                    ledger,
                ):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
            current_metadata = opened
        gate_current = _require_live_target_parent(
            gate,
            "task_target_changed",
        )
        if (
            current_metadata.st_dev != gate_current.st_dev
            or current_metadata.st_ino != gate_current.st_ino
            or current_metadata.st_uid != gate_current.st_uid
            or current_metadata.st_gid != gate_current.st_gid
            or current_metadata.st_uid != os.getuid()
            or stat.S_IMODE(current_metadata.st_mode) != 0o700
        ):
            _fail("task_target_changed")
        for root_name in (gate.current_name, gate.lean_name):
            expected_entry = ledger.by_path.get((root_name,))
            expected_metadata = root_metadata.get(root_name)
            if (
                type(expected_entry) is not _OwnedTargetEntry
                or expected_entry.token is None
                or type(expected_metadata) is not os.stat_result
            ):
                _fail("task_target_changed")
            observed = os.stat(
                root_name,
                dir_fd=current_owner.descriptor,
                follow_symlinks=False,
            )
            root_owner = _SourceResourceOwner()
            try:
                root_owner.descriptor = os.open(
                    root_name,
                    _directory_flags(),
                    dir_fd=current_owner.descriptor,
                )
                opened = os.fstat(root_owner.descriptor)
                if (
                    not _same_identity(observed, opened)
                    or not _same_identity(expected_metadata, opened)
                    or not _target_metadata_matches_token(
                        opened,
                        expected_entry.token,
                        "directory",
                    )
                    or not _valid_owned_metadata(
                        opened,
                        gate.device,
                        "directory",
                        0o555,
                    )
                ):
                    _fail("task_target_changed")
            finally:
                preserve_abort = _active_terminal_baseexception()
                if not _guard_cleanup_boolean(
                    _close_target_descriptors,
                    (root_owner,),
                    ledger,
                ):
                    _fail_cleanup_required_unless_nonexception_active(
                        preserve_abort
                    )
        final_parent = os.fstat(current_owner.descriptor)
        if (
            final_parent.st_dev != current_metadata.st_dev
            or final_parent.st_ino != current_metadata.st_ino
            or final_parent.st_uid != current_metadata.st_uid
            or final_parent.st_gid != current_metadata.st_gid
            or stat.S_IMODE(final_parent.st_mode) != 0o700
        ):
            _fail("task_target_changed")
    finally:
        preserve_abort = _active_terminal_baseexception()
        if not _guard_cleanup_boolean(
            _close_target_descriptors,
            (current_owner,),
            ledger,
        ):
            _fail_cleanup_required_unless_nonexception_active(
                preserve_abort
            )


def _rebind_snapshot_namespace(
    target_root: Path,
    expected_metadata: os.stat_result,
) -> None:
    with _open_materialized_snapshot_root(target_root) as gate:
        if not _same_identity(expected_metadata, gate.metadata):
            _fail("task_target_changed")


def _inspect_owned_pair(
    gate: _TargetParentGate,
    ledger: _TargetOwnershipLedger,
) -> bool:
    if ledger.close_uncertain or not ledger.entries:
        return False
    for pending in (
        entry for entry in ledger.entries if entry.token is None
    ):
        if pending.created is True:
            return False
        descriptor_stack = []
        pending_absent = False
        cleanup_ok = True
        try:
            parent_descriptor = (
                _open_owned_directory_chain(
                    gate,
                    pending.parent_components,
                    ledger,
                    "task_target_changed",
                    descriptor_stack,
                )
            )
            try:
                os.stat(
                    pending.basename,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pending_absent = True
        except Exception:
            return False
        finally:
            cleanup_ok = _guard_cleanup_boolean(
                _close_target_descriptors,
                descriptor_stack,
                ledger,
            )
        if not cleanup_ok or not pending_absent:
            return False
    expected_paths = frozenset(
        path for path, entry in ledger.by_path.items()
        if entry.token is not None
    )
    if not expected_paths:
        return True
    actual_paths = set()
    try:
        _require_live_target_parent(gate, "task_target_changed")
        roots = sorted(
            (
                entry for entry in ledger.entries
                if (
                    entry.parent_components == ()
                    and entry.token is not None
                )
            ),
            key=lambda entry: entry.basename.encode("utf-8"),
        )
        pending = [
            (entry.basename,) for entry in roots
        ]
        pending_index = 0
        while pending_index < len(pending):
            directory_path = pending[pending_index]
            pending_index += 1
            directory_entry = ledger.by_path.get(directory_path)
            if (
                type(directory_entry) is not _OwnedTargetEntry
                or directory_entry.kind != "directory"
            ):
                return False
            descriptor_stack = []
            directory_descriptor = (
                _open_owned_directory_chain(
                    gate,
                    directory_path,
                    ledger,
                    "task_target_changed",
                    descriptor_stack,
                )
            )
            iterator_owner = _SourceResourceOwner()
            directory_cleanup_ok = True
            try:
                actual_paths.add(directory_path)
                iterator_owner.iterator = os.scandir(
                    directory_descriptor
                )
                names = []
                remaining = len(expected_paths) - len(actual_paths)
                for unused_index in range(remaining + 1):
                    try:
                        item = next(iterator_owner.iterator)
                    except StopIteration:
                        break
                    if type(item.name) is not str:
                        return False
                    names.append(item.name)
                    if len(names) > remaining:
                        return False
                if not _guard_cleanup_boolean(
                    iterator_owner.close_iterator
                ):
                    ledger.close_uncertain = True
                    return False
                for name in names:
                    path = directory_path + (name,)
                    expected = ledger.by_path.get(path)
                    if (
                        type(expected) is not _OwnedTargetEntry
                        or path in actual_paths
                    ):
                        return False
                    observed = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    flags = (
                        _directory_flags()
                        if expected.kind == "directory"
                        else _file_flags()
                    )
                    child_owner = _SourceResourceOwner()
                    child_cleanup_ok = True
                    try:
                        child_owner.descriptor = os.open(
                            name,
                            flags,
                            dir_fd=directory_descriptor,
                        )
                        opened = os.fstat(child_owner.descriptor)
                        if (
                            not _same_identity(observed, opened)
                            or not _target_metadata_matches_token(
                                opened,
                                expected.token,
                                expected.kind,
                            )
                            or (
                                expected.kind == "file"
                                and opened.st_nlink != 1
                            )
                        ):
                            return False
                    finally:
                        child_cleanup_ok = _guard_cleanup_boolean(
                            _close_target_descriptors,
                            (child_owner,),
                            ledger,
                        )
                    if not child_cleanup_ok:
                        return False
                    actual_paths.add(path)
                    if expected.kind == "directory":
                        pending.append(path)
            finally:
                if not _guard_cleanup_boolean(
                    iterator_owner.close_iterator
                ):
                    ledger.close_uncertain = True
                    directory_cleanup_ok = False
                directory_cleanup_ok = _guard_cleanup_boolean(
                    _close_target_descriptors,
                    descriptor_stack,
                    ledger,
                ) and directory_cleanup_ok
            if not directory_cleanup_ok or ledger.close_uncertain:
                return False
        _require_live_target_parent(gate, "task_target_changed")
        return actual_paths == expected_paths
    except Exception:
        return False


def _open_owned_leaf(
    gate: _TargetParentGate,
    entry: _OwnedTargetEntry,
    ledger: _TargetOwnershipLedger,
    descriptor_stack: list,
    leaf_owner: _SourceResourceOwner,
) -> Tuple[int, os.stat_result]:
    if (
        type(descriptor_stack) is not list
        or descriptor_stack
        or type(leaf_owner) is not _SourceResourceOwner
        or leaf_owner.has_resources()
    ):
        _fail("task_target_changed")
    parent_descriptor = _open_owned_directory_chain(
        gate,
        entry.parent_components,
        ledger,
        "task_target_changed",
        descriptor_stack,
    )
    try:
        observed = os.stat(
            entry.basename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        flags = (
            _directory_flags()
            if entry.kind == "directory"
            else _file_flags()
        )
        leaf_owner.descriptor = os.open(
            entry.basename,
            flags,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(leaf_owner.descriptor)
        if (
            not _same_identity(observed, opened)
            or not _target_metadata_matches_token(
                opened,
                entry.token,
                entry.kind,
            )
            or (
                entry.kind == "file"
                and opened.st_nlink != 1
            )
        ):
            _fail("task_target_changed")
        return parent_descriptor, opened
    except BaseException as caught:
        descriptors = [leaf_owner]
        descriptors.extend(descriptor_stack)
        cleanup_ok = _guard_cleanup_boolean(
            _close_target_descriptors,
            descriptors,
            ledger,
        )
        if not isinstance(caught, Exception):
            raise
        if not cleanup_ok:
            _fail("task_snapshot_cleanup_required")
        raise


def _rollback_owned_pair(
    gate: _TargetParentGate,
    ledger: _TargetOwnershipLedger,
) -> bool:
    if not _inspect_owned_pair(gate, ledger):
        return False
    directories = [
        entry for entry in ledger.entries
        if entry.kind == "directory" and entry.token is not None
    ]
    directories.sort(
        key=lambda entry: (
            len(_owned_target_path(entry)),
            _owned_target_path(entry),
        )
    )
    files = [
        entry for entry in ledger.entries
        if entry.kind == "file" and entry.token is not None
    ]
    files.sort(
        key=lambda entry: (
            -len(_owned_target_path(entry)),
            _owned_target_path(entry),
        )
    )
    try:
        _require_live_target_parent(
            gate,
            "task_snapshot_cleanup_required",
        )
        for entry in directories:
            descriptor_stack = []
            leaf_owner = _SourceResourceOwner()
            unused_parent, unused_metadata = _open_owned_leaf(
                gate,
                entry,
                ledger,
                descriptor_stack,
                leaf_owner,
            )
            close_ok = True
            try:
                os.fchmod(leaf_owner.descriptor, 0o700)
                changed = os.fstat(leaf_owner.descriptor)
                if (
                    not _target_metadata_matches_token(
                        changed,
                        entry.token,
                        "directory",
                    )
                    or stat.S_IMODE(changed.st_mode) != 0o700
                ):
                    return False
            finally:
                close_ok = _guard_cleanup_boolean(
                    _close_target_descriptors,
                    [leaf_owner] + descriptor_stack,
                    ledger,
                )
            if not close_ok:
                return False
        for entry in files:
            descriptor_stack = []
            leaf_owner = _SourceResourceOwner()
            parent_descriptor, opened = _open_owned_leaf(
                gate,
                entry,
                ledger,
                descriptor_stack,
                leaf_owner,
            )
            close_ok = True
            try:
                final_observed = os.stat(
                    entry.basename,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not _same_identity(opened, final_observed)
                    or final_observed.st_nlink != 1
                ):
                    return False
                os.unlink(
                    entry.basename,
                    dir_fd=parent_descriptor,
                )
            finally:
                close_ok = _guard_cleanup_boolean(
                    _close_target_descriptors,
                    [leaf_owner] + descriptor_stack,
                    ledger,
                )
            if not close_ok:
                return False
        for entry in reversed(directories):
            descriptor_stack = []
            leaf_owner = _SourceResourceOwner()
            parent_descriptor, opened = _open_owned_leaf(
                gate,
                entry,
                ledger,
                descriptor_stack,
                leaf_owner,
            )
            close_ok = True
            try:
                final_observed = os.stat(
                    entry.basename,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if not _same_identity(opened, final_observed):
                    return False
                os.rmdir(
                    entry.basename,
                    dir_fd=parent_descriptor,
                )
            finally:
                close_ok = _guard_cleanup_boolean(
                    _close_target_descriptors,
                    [leaf_owner] + descriptor_stack,
                    ledger,
                )
            if not close_ok:
                return False
        _require_live_target_parent(
            gate,
            "task_snapshot_cleanup_required",
        )
        for root_name in (gate.current_name, gate.lean_name):
            try:
                os.stat(
                    root_name,
                    dir_fd=gate.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            return False
        return not ledger.close_uncertain
    except Exception:
        return False


def _require_identity_inventory_matches_tree(
    tree_document: Mapping[str, object],
    identity_inventory: Tuple[Tuple[str, os.stat_result, str], ...],
) -> None:
    try:
        expected = tuple(
            (record["path"], record["kind"])
            for record in tree_document["records"]
        )
        observed = tuple(
            (path, kind)
            for path, unused_metadata, kind in identity_inventory
        )
        if expected != observed:
            _fail("task_target_changed")
    except Exception:
        _fail("task_target_changed")


def _build_materialized_snapshot_pair(
    seal: _CapturedOperationalSeal,
    gate: _TargetParentGate,
    materialized_tree_digest: str,
    current_identity_digest: str,
    lean_identity_digest: str,
) -> Tuple[MaterializedTaskSnapshot, MaterializedTaskSnapshot]:
    try:
        receipt = _make_task_snapshot_receipt(
            seal,
            materialized_tree_digest,
        )
        current_snapshot = MaterializedTaskSnapshot(
            snapshot_receipt=receipt,
            target_root=gate.target_parent / gate.current_name,
            target_identity_digest=current_identity_digest,
            materialized_tree_digest=materialized_tree_digest,
            file_count=seal.file_count,
            total_bytes=seal.total_bytes,
        )
        lean_snapshot = MaterializedTaskSnapshot(
            snapshot_receipt=receipt,
            target_root=gate.target_parent / gate.lean_name,
            target_identity_digest=lean_identity_digest,
            materialized_tree_digest=materialized_tree_digest,
            file_count=seal.file_count,
            total_bytes=seal.total_bytes,
        )
        shared_receipt = current_snapshot.snapshot_receipt
        if shared_receipt != lean_snapshot.snapshot_receipt:
            _fail("task_snapshot_receipt_invalid")
        object.__setattr__(
            lean_snapshot,
            "snapshot_receipt",
            shared_receipt,
        )
        return (current_snapshot, lean_snapshot)
    except Exception:
        _fail("task_snapshot_receipt_invalid")


class TaskSnapshotMaterializer:
    def __init__(self, policy: TaskSnapshotPolicy) -> None:
        self._policy = _copy_task_snapshot_policy(policy)
        self._policy_seal = _copy_task_snapshot_policy(self._policy)
        self._lock = threading.Lock()
        self._state = "new"
        self._captured = None
        self._operational_seal = None
        self._source_root = None
        self._git_dir = None
        self._protected_identity_keys = ()
        self._retained_target_gate = None

    def _clear_locked(self) -> bool:
        retained_gate = self._retained_target_gate
        self._retained_target_gate = None
        self._captured = None
        self._operational_seal = None
        self._source_root = None
        self._git_dir = None
        self._protected_identity_keys = ()
        self._state = "closed"
        if retained_gate is None:
            return True
        return _guard_cleanup_boolean(retained_gate.close)

    def _retain_target_gate_locked(
        self,
        gate: _TargetParentGate,
    ) -> None:
        if (
            type(gate) is not _TargetParentGate
            or gate.descriptor < 0
            or gate._owns_descriptor
            or self._retained_target_gate is not None
        ):
            _fail("task_snapshot_cleanup_required")
        _require_live_target_parent(
            gate,
            "task_snapshot_cleanup_required",
        )
        self._retained_target_gate = gate
        gate._owns_descriptor = True

    def _release_capture_locked(self, state: str) -> None:
        self._captured = None
        self._operational_seal = None
        self._source_root = None
        self._git_dir = None
        self._protected_identity_keys = ()
        self._state = state

    def _validated_pair_inputs_locked(
        self,
        captured: CapturedTaskObjects,
    ) -> Tuple[
        _CapturedOperationalSeal,
        TaskSnapshotPolicy,
        Path,
        Path,
        Tuple[Tuple[int, int, str], ...],
    ]:
        try:
            if (
                captured is not self._captured
                or type(self._operational_seal)
                is not _CapturedOperationalSeal
                or type(self._source_root) is not type(Path())
                or type(self._git_dir) is not type(Path())
                or type(self._protected_identity_keys) is not tuple
                or len(self._protected_identity_keys) != 2
            ):
                _fail("task_snapshot_receipt_invalid")
            live_policy = _copy_task_snapshot_policy(self._policy)
            if live_policy != self._policy_seal:
                _fail("task_snapshot_receipt_invalid")
            _validate_captured_objects(captured)
            candidate = _make_operational_seal(
                captured,
                self._protected_identity_keys,
            )
            if (
                candidate != self._operational_seal
                or candidate.policy != live_policy
                or candidate.protected_identity_keys
                != self._protected_identity_keys
                or candidate.source.repository_root
                != self._source_root
                or Path(self._git_dir)
                != candidate.source.repository_root / ".git"
            ):
                _fail("task_snapshot_receipt_invalid")
            return (
                self._operational_seal,
                live_policy,
                Path(os.fspath(self._source_root)),
                Path(os.fspath(self._git_dir)),
                tuple(self._protected_identity_keys),
            )
        except Exception:
            _fail("task_snapshot_receipt_invalid")

    def capture(
        self,
        prepared: PreparedTaskSource,
    ) -> CapturedTaskObjects:
        with self._lock:
            if self._state != "new":
                _fail("task_snapshot_receipt_invalid")
            try:
                detached = _detach_prepared_source(prepared)
                transaction_policy = _copy_task_snapshot_policy(
                    self._policy
                )
                if detached.policy != transaction_policy:
                    _fail("task_source_changed")
                process_policy_digest = _process_policy_digest(
                    transaction_policy
                )
                if (
                    process_policy_digest
                    != detached.git_process_policy_digest
                ):
                    _fail("task_source_changed")

                deadline = _capture_deadline(transaction_policy)
                first_filesystem = _capture_phase(
                    deadline,
                    _capture_filesystem,
                    detached.source,
                    transaction_policy,
                    detached.object_format,
                )
                first_topology = _capture_phase(
                    deadline,
                    _capture_object_topology,
                    detached.source,
                    transaction_policy,
                    detached.object_format,
                )
                first_config_output = _capture_phase(
                    deadline,
                    _run_git,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    transaction_policy,
                    "config",
                    capture_deadline=deadline,
                )
                first_config_digest = _capture_phase(
                    deadline,
                    _parse_config_output,
                    first_config_output,
                    first_filesystem.raw_config_digest,
                    detached.object_format,
                )
                storage_output = _capture_phase(
                    deadline,
                    _run_git,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    transaction_policy,
                    "storage-format",
                    capture_deadline=deadline,
                )
                _capture_phase(
                    deadline,
                    _validate_storage_format_output,
                    storage_output,
                    detached.object_format,
                )
                first_source_identity = _capture_phase(
                    deadline,
                    _require_capture_matches_prepared,
                    first_filesystem,
                    first_config_digest,
                    first_topology,
                    detached,
                    process_policy_digest,
                )

                commit_output = _capture_phase(
                    deadline,
                    _run_git,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    transaction_policy,
                    "verify-commit",
                    detached.source.commit_oid,
                    capture_deadline=deadline,
                )
                _capture_phase(
                    deadline,
                    _validate_commit_output,
                    commit_output,
                    detached.source.commit_oid,
                )
                tree_output = _capture_phase(
                    deadline,
                    _run_git,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    transaction_policy,
                    "verify-tree",
                    detached.source.commit_oid,
                    capture_deadline=deadline,
                )
                tree_oid = _capture_phase(
                    deadline,
                    _parse_tree_output,
                    tree_output,
                    detached.object_format,
                )
                tree_bytes = _capture_phase(
                    deadline,
                    _run_git,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    transaction_policy,
                    "ls-tree",
                    tree_oid,
                    capture_deadline=deadline,
                )
                parsed = _capture_phase(
                    deadline,
                    _parse_task_tree,
                    tree_bytes,
                    detached.object_format,
                    transaction_policy,
                )
                entries, blobs = _capture_phase(
                    deadline,
                    _load_task_blobs,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    parsed,
                    transaction_policy,
                    capture_deadline=deadline,
                )
                entry_digest = _capture_phase(
                    deadline,
                    _task_entry_digest,
                    detached.source.commit_oid,
                    tree_oid,
                    parsed,
                    entries,
                )

                second_config_output = _capture_phase(
                    deadline,
                    _run_git,
                    first_filesystem.git_dir,
                    first_filesystem.config_path,
                    transaction_policy,
                    "config",
                    capture_deadline=deadline,
                )
                second_config_digest = _capture_phase(
                    deadline,
                    _parse_config_output,
                    second_config_output,
                    first_filesystem.raw_config_digest,
                    detached.object_format,
                )
                second_filesystem = _capture_phase(
                    deadline,
                    _capture_filesystem,
                    detached.source,
                    transaction_policy,
                    detached.object_format,
                )
                second_topology = _capture_phase(
                    deadline,
                    _capture_object_topology,
                    detached.source,
                    transaction_policy,
                    detached.object_format,
                )
                second_source_identity = _capture_phase(
                    deadline,
                    _require_capture_matches_prepared,
                    second_filesystem,
                    second_config_digest,
                    second_topology,
                    detached,
                    process_policy_digest,
                )
                if (
                    second_filesystem != first_filesystem
                    or second_config_digest != first_config_digest
                    or second_topology != first_topology
                    or second_source_identity != first_source_identity
                ):
                    _fail("task_source_changed")
                try:
                    current_policy = _copy_task_snapshot_policy(
                        self._policy
                    )
                except Exception:
                    _fail("task_source_changed")
                if (
                    current_policy != transaction_policy
                    or transaction_policy != detached.policy
                    or _process_policy_digest(transaction_policy)
                    != process_policy_digest
                    or process_policy_digest
                    != detached.git_process_policy_digest
                ):
                    _fail("task_source_changed")
                _capture_phase(
                    deadline,
                    _require_prepared_unchanged,
                    prepared,
                    detached,
                )
                _check_capture_deadline(deadline)

                source_receipt = _capture_phase(
                    deadline,
                    _make_source_trust_receipt,
                    detached,
                    first_source_identity,
                    first_topology,
                    second_source_identity,
                    second_topology,
                    process_policy_digest,
                )
                captured = _capture_phase(
                    deadline,
                    CapturedTaskObjects,
                    source_trust_receipt=source_receipt,
                    source=detached.source,
                    policy=transaction_policy,
                    object_format=detached.object_format,
                    commit_oid=detached.source.commit_oid,
                    tree_oid=tree_oid,
                    entry_digest=entry_digest,
                    entries=entries,
                    blobs=blobs,
                    file_count=parsed.file_count,
                    total_bytes=parsed.total_bytes,
                    unique_blob_count=parsed.unique_blob_count,
                    unique_blob_bytes=parsed.unique_blob_bytes,
                )
                protected_keys = (
                    second_filesystem.repository_identity_key,
                    second_filesystem.git_dir_identity_key,
                )
                operational_seal = _capture_phase(
                    deadline,
                    _make_operational_seal,
                    captured,
                    protected_keys,
                )
                self._captured = captured
                self._operational_seal = operational_seal
                self._source_root = Path(
                    os.fspath(detached.source.repository_root)
                )
                self._git_dir = Path(os.fspath(detached.git_dir))
                self._protected_identity_keys = tuple(protected_keys)
                _check_capture_deadline(deadline)
                self._state = "captured"
                return captured
            except TaskSnapshotError as error:
                cleanup_ok = _guard_cleanup_boolean(
                    self._clear_locked
                )
                if not cleanup_ok:
                    _fail("task_snapshot_cleanup_required")
                error.__context__ = None
                error.__cause__ = None
                raise
            except Exception:
                cleanup_ok = _guard_cleanup_boolean(
                    self._clear_locked
                )
                if not cleanup_ok:
                    _fail("task_snapshot_cleanup_required")
                _fail("task_snapshot_receipt_invalid")
            except BaseException as caught:
                cleanup_ok = _guard_cleanup_boolean(
                    self._clear_locked
                )
                if not isinstance(caught, Exception):
                    raise
                if not cleanup_ok:
                    _fail("task_snapshot_cleanup_required")
                raise

    def _materialize_pair_under_gate_locked(
        self,
        captured: CapturedTaskObjects,
        seal: _CapturedOperationalSeal,
        transaction_policy: TaskSnapshotPolicy,
        expected_document: Mapping[str, object],
        expected_digest: str,
        gate: _TargetParentGate,
        ledger: _TargetOwnershipLedger,
        failure_state: Dict[str, str],
    ) -> Tuple[MaterializedTaskSnapshot, MaterializedTaskSnapshot]:
        (
            rechecked_seal,
            rechecked_policy,
            unused_source_root,
            unused_git_dir,
            unused_protected_keys,
        ) = self._validated_pair_inputs_locked(captured)
        if (
            rechecked_seal != seal
            or rechecked_policy != transaction_policy
        ):
            _fail("task_snapshot_receipt_invalid")
        _require_live_target_parent(gate, "task_target_invalid")

        for root_name in (gate.current_name, gate.lean_name):
            _create_owned_directory(
                gate,
                (),
                root_name,
                ledger,
            )
        directory_paths = tuple(
            record["path"]
            for record in expected_document["records"]
            if (
                record["kind"] == "directory"
                and record["path"] != "."
            )
        )
        for root_name in (gate.current_name, gate.lean_name):
            for directory_path in directory_paths:
                components = _task_path_components(
                    directory_path,
                    transaction_policy,
                )
                _create_owned_directory(
                    gate,
                    (root_name,) + components[:-1],
                    components[-1],
                    ledger,
                )
        for entry in seal.entries:
            content = seal.blobs[entry.blob_oid]
            for root_name in (gate.current_name, gate.lean_name):
                _create_owned_file(
                    gate,
                    root_name,
                    entry,
                    content,
                    transaction_policy,
                    ledger,
                )

        _seal_owned_directories(gate, ledger)
        failure_state["code"] = "task_target_changed"
        _require_live_target_parent(gate, "task_target_changed")
        current_root_entry = ledger.by_path[(gate.current_name,)]
        lean_root_entry = ledger.by_path[(gate.lean_name,)]
        (
            current_document,
            current_metadata,
            current_identity_inventory,
        ) = _scan_owned_target_root(
            gate,
            gate.current_name,
            current_root_entry,
            transaction_policy,
            ledger,
        )
        (
            lean_document,
            lean_metadata,
            lean_identity_inventory,
        ) = _scan_owned_target_root(
            gate,
            gate.lean_name,
            lean_root_entry,
            transaction_policy,
            ledger,
        )
        if (
            current_document != expected_document
            or lean_document != expected_document
            or current_document != lean_document
        ):
            _fail("task_target_changed")
        current_digest = _digest(current_document)
        lean_digest = _digest(lean_document)
        if (
            current_digest != expected_digest
            or lean_digest != expected_digest
        ):
            _fail("task_target_changed")
        _require_identity_inventory_matches_tree(
            current_document,
            current_identity_inventory,
        )
        _require_identity_inventory_matches_tree(
            lean_document,
            lean_identity_inventory,
        )
        if (
            current_metadata.st_dev,
            current_metadata.st_ino,
        ) == (
            lean_metadata.st_dev,
            lean_metadata.st_ino,
        ):
            _fail("task_target_changed")
        current_identity_digest = _digest(
            _target_root_identity_document(
                current_metadata,
                expected_digest,
                current_identity_inventory,
            )
        )
        lean_identity_digest = _digest(
            _target_root_identity_document(
                lean_metadata,
                expected_digest,
                lean_identity_inventory,
            )
        )
        if current_identity_digest == lean_identity_digest:
            _fail("task_target_changed")

        snapshots = _build_materialized_snapshot_pair(
            seal,
            gate,
            expected_digest,
            current_identity_digest,
            lean_identity_digest,
        )
        _rebind_owned_pair_namespace(
            gate,
            ledger,
            MappingProxyType(
                {
                    gate.current_name: current_metadata,
                    gate.lean_name: lean_metadata,
                }
            ),
        )
        return snapshots

    def materialize_pair(
        self,
        captured: CapturedTaskObjects,
        target_parent: Path,
        current_name: str = "current",
        lean_name: str = "lean",
    ) -> Tuple[MaterializedTaskSnapshot, MaterializedTaskSnapshot]:
        with self._lock:
            if self._state != "captured":
                _fail("task_snapshot_receipt_invalid")
            ledger = _TargetOwnershipLedger()
            failure_state = {"code": "task_target_invalid"}
            rollback_completed = False
            try:
                (
                    seal,
                    transaction_policy,
                    source_root,
                    git_dir,
                    protected_identity_keys,
                ) = self._validated_pair_inputs_locked(captured)
                expected_document = _expected_materialized_tree_document(
                    seal
                )
                expected_digest = _digest(expected_document)
                with _open_target_parent_gate(
                    target_parent,
                    current_name,
                    lean_name,
                    source_root,
                    git_dir,
                    protected_identity_keys,
                    transaction_policy,
                ) as gate:
                    try:
                        snapshots = (
                            self._materialize_pair_under_gate_locked(
                                captured,
                                seal,
                                transaction_policy,
                                expected_document,
                                expected_digest,
                                gate,
                                ledger,
                                failure_state,
                            )
                        )
                        self._retain_target_gate_locked(gate)
                        self._release_capture_locked("paired")
                        return snapshots
                    except BaseException as caught:
                        if self._state == "paired":
                            raise
                        rollback_ok = True
                        if ledger.entries:
                            rollback_ok = _guard_cleanup_boolean(
                                _rollback_owned_pair,
                                gate,
                                ledger,
                            )
                            if rollback_ok:
                                rollback_completed = True
                        if not isinstance(caught, Exception):
                            raise
                        if not rollback_ok or ledger.close_uncertain:
                            _fail("task_snapshot_cleanup_required")
                        raise
            except BaseException as caught:
                if self._state == "paired":
                    raise
                cleanup_ok = _guard_cleanup_boolean(
                    self._clear_locked
                )
                if not isinstance(caught, Exception):
                    raise
                if (
                    ledger.entries
                    and not rollback_completed
                ):
                    cleanup_ok = False
                if ledger.close_uncertain:
                    cleanup_ok = False
                if not cleanup_ok:
                    _fail("task_snapshot_cleanup_required")
                if isinstance(caught, TaskSnapshotError):
                    code = str(caught)
                    if code == "task_snapshot_cleanup_required":
                        _fail(code)
                    if code not in {
                        "task_snapshot_receipt_invalid",
                        "task_target_invalid",
                        "task_target_changed",
                    }:
                        code = failure_state["code"]
                    _fail(code)
                if isinstance(caught, Exception):
                    _fail(failure_state["code"])
                raise

    def verify(self, snapshot: MaterializedTaskSnapshot) -> None:
        with self._lock:
            if self._state != "paired":
                _fail("task_snapshot_receipt_invalid")
            try:
                live_policy = _copy_task_snapshot_policy(self._policy)
                if live_policy != self._policy_seal:
                    _fail("task_snapshot_receipt_invalid")
                detached = _copy_materialized_snapshot(
                    snapshot,
                    live_policy,
                )
            except Exception:
                _fail("task_snapshot_receipt_invalid")

            try:
                with _open_materialized_snapshot_root(
                    detached.target_root
                ) as root_gate:
                    (
                        tree_document,
                        final_root_metadata,
                        identity_inventory,
                    ) = _scan_sealed_target_root(
                        root_gate.descriptor,
                        root_gate.metadata,
                        live_policy,
                    )
                    tree_digest = _digest(tree_document)
                    if (
                        tree_digest
                        != detached.materialized_tree_digest
                        or tree_document["file_count"]
                        != detached.file_count
                        or tree_document["total_bytes"]
                        != detached.total_bytes
                    ):
                        _fail("task_target_changed")
                    _require_identity_inventory_matches_tree(
                        tree_document,
                        identity_inventory,
                    )
                    identity_digest = _digest(
                        _target_root_identity_document(
                            final_root_metadata,
                            tree_digest,
                            identity_inventory,
                        )
                    )
                    if (
                        identity_digest
                        != detached.target_identity_digest
                    ):
                        _fail("task_target_changed")
                    _rebind_snapshot_namespace(
                        detached.target_root,
                        final_root_metadata,
                    )
            except TaskSnapshotError as error:
                if str(error) == "task_snapshot_cleanup_required":
                    raise
                _fail("task_target_changed")
            except Exception:
                _fail("task_target_changed")
            return None

    def close(self) -> None:
        preserve_abort = _active_terminal_baseexception()
        with self._lock:
            if self._state == "closed":
                return
            if not _guard_cleanup_boolean(self._clear_locked):
                _fail_cleanup_required_unless_nonexception_active(
                    preserve_abort
                )


def prepare_task_source(
    source: TaskSourceSpec,
    policy: TaskSnapshotPolicy,
) -> PreparedTaskSource:
    """Validate and seal a supported local Git source without reading objects."""
    _validate_source(source)
    _validate_policy(policy)
    _require_supported_platform()
    expected_format = "sha1" if len(source.commit_oid) == 40 else "sha256"
    first_filesystem = _capture_filesystem(source, policy, expected_format)
    first_topology = _capture_object_topology(source, policy, expected_format)
    first_config_output = _run_git(
        first_filesystem.git_dir,
        first_filesystem.config_path,
        policy,
        "config",
    )
    first_config_digest = _parse_config_output(
        first_config_output,
        first_filesystem.raw_config_digest,
        expected_format,
    )
    storage_output = _run_git(
        first_filesystem.git_dir,
        first_filesystem.config_path,
        policy,
        "storage-format",
    )
    _validate_storage_format_output(storage_output, expected_format)
    second_config_output = _run_git(
        first_filesystem.git_dir,
        first_filesystem.config_path,
        policy,
        "config",
    )
    second_config_digest = _parse_config_output(
        second_config_output,
        first_filesystem.raw_config_digest,
        expected_format,
    )
    second_filesystem = _capture_filesystem(source, policy, expected_format)
    second_topology = _capture_object_topology(source, policy, expected_format)
    if (
        first_filesystem.filesystem_digest
        != second_filesystem.filesystem_digest
        or first_filesystem.raw_config_digest
        != second_filesystem.raw_config_digest
        or first_filesystem.git_dir != second_filesystem.git_dir
        or first_filesystem.config_path != second_filesystem.config_path
        or first_config_digest != second_config_digest
        or first_topology != second_topology
    ):
        _fail("task_source_changed")
    prepared = PreparedTaskSource(
        source=source,
        policy=policy,
        git_dir=first_filesystem.git_dir,
        object_format=expected_format,
        source_identity_digest=_source_identity_digest(
            first_filesystem.filesystem_digest, first_config_digest
        ),
        local_config_digest=first_config_digest,
        object_topology=first_topology,
        git_process_policy_digest=_process_policy_digest(policy),
    )
    _validate_prepared_source(prepared)
    return prepared
