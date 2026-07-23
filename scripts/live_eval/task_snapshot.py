"""Fail-closed trust gate for one operator-attested local Git source."""

from dataclasses import dataclass, field, fields
import hashlib
import inspect
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Dict, Optional, Sequence, Tuple
import unicodedata

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
    "max_config_bytes": 256 * 1024,
    "max_packed_refs_bytes": 4 * 1024 * 1024,
    "max_object_entries": 200000,
    "max_object_store_bytes": 2 * 1024 * 1024 * 1024,
    "max_object_depth": 3,
    "max_component_bytes": 255,
    "max_relative_path_bytes": 4096,
    "max_files": 10000,
    "max_file_bytes": 4 * 1024 * 1024,
    "max_total_bytes": 64 * 1024 * 1024,
}


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
    max_config_bytes: int = 256 * 1024
    max_packed_refs_bytes: int = 4 * 1024 * 1024
    max_object_entries: int = 200000
    max_object_store_bytes: int = 2 * 1024 * 1024 * 1024
    max_object_depth: int = 3
    max_component_bytes: int = 255
    max_relative_path_bytes: int = 4096
    max_files: int = 10000
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        _validate_policy(self)


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


def _fail(code: str) -> None:
    try:
        raise TaskSnapshotError(code) from None
    except TaskSnapshotError as error:
        error.__context__ = None
        error.__cause__ = None
        raise


def _exact_fields(value: object, expected_type: type) -> bool:
    return (
        type(value) is expected_type
        and set(vars(value)) == {item.name for item in fields(expected_type)}
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
) -> Tuple[int, os.stat_result, Tuple[Dict[str, object], ...]]:
    lexical_ancestors = _validate_physical_root(repository_root)
    raw_root = os.fspath(repository_root)
    try:
        lexical_root = os.lstat(raw_root)
    except OSError:
        _fail("task_source_root_invalid")
    components = Path(raw_root).parts[1:]
    descriptor = -1
    try:
        descriptor = os.open(os.sep, _directory_flags())
        parent_metadata = os.fstat(descriptor)
        actual_ancestors = []
        for index, component in enumerate(components):
            observed = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
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
            child_descriptor = -1
            try:
                child_descriptor = os.open(
                    component, _directory_flags(), dir_fd=descriptor
                )
                opened = os.fstat(child_descriptor)
                if not _same_identity(observed, opened):
                    _fail("task_source_changed")
                if (
                    _stable_ancestor_identity(parent_metadata)
                    != _stable_ancestor_identity(os.fstat(descriptor))
                ):
                    _fail("task_source_changed")
                owned_parent = descriptor
                descriptor = -1
                if not _close_fd_once(owned_parent):
                    _fail("task_source_root_invalid")
                descriptor = child_descriptor
                child_descriptor = -1
            finally:
                if child_descriptor >= 0:
                    owned_child = child_descriptor
                    child_descriptor = -1
                    _close_fd_once(owned_child)
            parent_metadata = opened
        if not components:
            _fail("task_source_root_invalid")
        if not _same_identity(lexical_root, parent_metadata):
            _fail("task_source_changed")
        _require_directory_metadata(
            parent_metadata, parent_metadata.st_dev, "task_source_root_invalid"
        )
        return descriptor, parent_metadata, tuple(actual_ancestors)
    except TaskSnapshotError:
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            _close_fd_once(owned_descriptor)
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            _close_fd_once(owned_descriptor)
        _fail("task_source_root_invalid")


def _open_child_directory(
    parent_fd: int,
    name: str,
    device: int,
    code: str,
) -> Tuple[int, os.stat_result]:
    descriptor = -1
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_directory_metadata(observed, device, code)
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not _same_identity(observed, opened):
            _fail("task_source_changed")
        result = (descriptor, opened)
        descriptor = -1
        return result
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail(code)
    finally:
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            _close_fd_once(owned_descriptor)


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
) -> Tuple[bytes, Dict[str, object]]:
    descriptor = -1
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_file_metadata(observed, device, missing_code)
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not _same_identity(observed, opened):
            _fail("task_source_changed")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            _fail(missing_code)
        after = os.fstat(descriptor)
        if not _same_identity(opened, after):
            _fail("task_source_changed")
        return content, _identity(after, "file")
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail(missing_code)
    finally:
        if descriptor >= 0:
            owned_descriptor = descriptor
            descriptor = -1
            _close_fd_once(owned_descriptor)


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
    descriptor, opened = _open_child_directory(
        parent_fd, name, device, "task_source_control_invalid"
    )
    try:
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                _fail("task_source_control_invalid")
        if not _same_identity(opened, os.fstat(descriptor)):
            _fail("task_source_changed")
        return "empty"
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")
    finally:
        owned_descriptor = descriptor
        descriptor = -1
        _close_fd_once(owned_descriptor)


def _scan_hooks(
    git_fd: int,
    device: int,
    policy: TaskSnapshotPolicy,
) -> str:
    metadata = _optional_metadata(git_fd, "hooks")
    if metadata is None:
        return "absent"
    descriptor, opened = _open_child_directory(
        git_fd, "hooks", device, "task_source_control_invalid"
    )
    count = 0
    exact_names = set()
    nfc_names = set()
    folded_names = set()
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
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
                item = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                _require_file_metadata(
                    item, device, "task_source_control_invalid"
                )
        if not _same_identity(opened, os.fstat(descriptor)):
            _fail("task_source_changed")
        return "sample_only"
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")
    finally:
        owned_descriptor = descriptor
        descriptor = -1
        _close_fd_once(owned_descriptor)


def _check_refs(git_fd: int, device: int) -> None:
    metadata = _optional_metadata(git_fd, "refs")
    if metadata is None:
        return
    descriptor, opened = _open_child_directory(
        git_fd, "refs", device, "task_source_control_invalid"
    )
    try:
        _require_absent(descriptor, "replace")
        if not _same_identity(opened, os.fstat(descriptor)):
            _fail("task_source_changed")
    finally:
        owned_descriptor = descriptor
        descriptor = -1
        _close_fd_once(owned_descriptor)


def _check_object_controls(
    objects_fd: int,
    device: int,
    policy: TaskSnapshotPolicy,
) -> None:
    info_metadata = _optional_metadata(objects_fd, "info")
    if info_metadata is not None:
        info_fd, info_opened = _open_child_directory(
            objects_fd, "info", device, "task_source_control_invalid"
        )
        try:
            _require_absent(info_fd, "alternates")
            _require_absent(info_fd, "http-alternates")
            if not _same_identity(info_opened, os.fstat(info_fd)):
                _fail("task_source_changed")
        finally:
            owned_info_fd = info_fd
            info_fd = -1
            _close_fd_once(owned_info_fd)
    pack_metadata = _optional_metadata(objects_fd, "pack")
    if pack_metadata is not None:
        pack_fd, pack_opened = _open_child_directory(
            objects_fd, "pack", device, "task_source_control_invalid"
        )
        entry_count = 0
        try:
            with os.scandir(pack_fd) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > policy.max_object_entries:
                        _fail("task_source_control_invalid")
                    if entry.name.endswith(".promisor"):
                        _fail("task_source_control_invalid")
            if not _same_identity(pack_opened, os.fstat(pack_fd)):
                _fail("task_source_changed")
        finally:
            owned_pack_fd = pack_fd
            pack_fd = -1
            _close_fd_once(owned_pack_fd)


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
    root_fd = git_fd = objects_fd = -1
    try:
        root_fd, root_metadata, ancestors = _open_root_descriptor(
            source.repository_root
        )
        device = root_metadata.st_dev
        git_fd, git_metadata = _open_child_directory(
            root_fd, ".git", device, "task_source_git_dir_invalid"
        )
        objects_fd, objects_metadata = _open_child_directory(
            git_fd, "objects", device, "task_source_control_invalid"
        )
        config_bytes, config_identity = _read_control_file(
            git_fd,
            "config",
            device,
            policy.max_config_bytes,
            "task_source_config_invalid",
        )
        raw_config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        _require_absent(git_fd, "commondir")
        _require_absent(git_fd, "config.worktree")
        worktrees_state = _scan_empty_directory(git_fd, "worktrees", device)
        modules_state = _scan_empty_directory(git_fd, "modules", device)
        hooks_state = _scan_hooks(git_fd, device, policy)
        _check_refs(git_fd, device)
        _check_object_controls(objects_fd, device, policy)

        packed_metadata = _optional_metadata(git_fd, "packed-refs")
        if packed_metadata is None:
            packed_document = {"state": "absent"}
        else:
            packed_bytes, packed_identity = _read_control_file(
                git_fd,
                "packed-refs",
                device,
                policy.max_packed_refs_bytes,
                "task_source_control_invalid",
            )
            _parse_packed_refs(packed_bytes, 40 if expected_format == "sha1" else 64)
            packed_document = {
                "identity": packed_identity,
                "raw_digest": "sha256:"
                + hashlib.sha256(packed_bytes).hexdigest(),
                "state": "present",
            }

        if not _same_identity(objects_metadata, os.fstat(objects_fd)):
            _fail("task_source_changed")
        if not _same_identity(git_metadata, os.fstat(git_fd)):
            _fail("task_source_changed")
        if not _same_identity(root_metadata, os.fstat(root_fd)):
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
        )
    except TaskSnapshotError:
        raise
    except (OSError, TypeError, ValueError, OverflowError):
        _fail("task_source_control_invalid")
    finally:
        owned_descriptors = (objects_fd, git_fd, root_fd)
        objects_fd = git_fd = root_fd = -1
        for descriptor in owned_descriptors:
            if descriptor >= 0:
                _close_fd_once(descriptor)


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
    root_fd = git_fd = objects_fd = -1
    frames = []
    try:
        if object_format not in ("sha1", "sha256"):
            _fail("task_source_topology_invalid")
        oid_length = 40 if object_format == "sha1" else 64
        root_fd, root_metadata, unused_ancestors = _open_root_descriptor(
            source.repository_root
        )
        device = root_metadata.st_dev
        git_fd, unused_git_metadata = _open_child_directory(
            root_fd, ".git", device, "task_source_git_dir_invalid"
        )
        objects_fd, objects_metadata = _open_child_directory(
            git_fd, "objects", device, "task_source_topology_invalid"
        )
        records = [_topology_record(".", objects_metadata, "directory")]
        entry_count = 1
        file_count = 0
        total_bytes = 0
        pairings: Dict[str, set] = {}
        if entry_count > policy.max_object_entries:
            _fail("task_source_topology_invalid")

        root_iterator = os.scandir(objects_fd)
        frames.append(
            {
                "fd": objects_fd,
                "metadata": objects_metadata,
                "path": ".",
                "depth": 0,
                "iterator": root_iterator,
                "exact": set(),
                "nfc": set(),
                "casefold": set(),
            }
        )
        objects_fd = -1
        while frames:
            frame = frames[-1]
            try:
                entry = next(frame["iterator"])
            except StopIteration:
                frame["iterator"].close()
                if not _same_identity(
                    frame["metadata"], os.fstat(frame["fd"])
                ):
                    _fail("task_source_changed")
                frames.pop()
                owned_frame_fd = frame["fd"]
                frame["fd"] = -1
                if not _close_fd_once(owned_frame_fd):
                    _fail("task_source_topology_invalid")
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
                name, dir_fd=frame["fd"], follow_symlinks=False
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
                child_fd = -1
                child_iterator = None
                try:
                    child_fd = os.open(
                        name, _directory_flags(), dir_fd=frame["fd"]
                    )
                    child_metadata = os.fstat(child_fd)
                    if not _same_identity(observed, child_metadata):
                        _fail("task_source_changed")
                    child_iterator = os.scandir(child_fd)
                    records.append(
                        _topology_record(path, child_metadata, "directory")
                    )
                    frames.append(
                        {
                            "fd": child_fd,
                            "metadata": child_metadata,
                            "path": path,
                            "depth": depth,
                            "iterator": child_iterator,
                            "exact": set(),
                            "nfc": set(),
                            "casefold": set(),
                        }
                    )
                    child_fd = -1
                    child_iterator = None
                finally:
                    if child_iterator is not None:
                        try:
                            child_iterator.close()
                        except OSError:
                            pass
                    if child_fd >= 0:
                        owned_child_fd = child_fd
                        child_fd = -1
                        _close_fd_once(owned_child_fd)
                continue
            _require_file_metadata(
                observed, device, "task_source_topology_invalid"
            )
            family = _object_file_family(path, oid_length)
            if family is None:
                _fail("task_source_topology_invalid")
            after = os.stat(name, dir_fd=frame["fd"], follow_symlinks=False)
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
        while frames:
            frame = frames.pop()
            try:
                frame["iterator"].close()
            except (OSError, AttributeError):
                pass
            owned_frame_fd = frame["fd"]
            frame["fd"] = -1
            if owned_frame_fd >= 0:
                _close_fd_once(owned_frame_fd)
        owned_descriptors = (objects_fd, git_fd, root_fd)
        objects_fd = git_fd = root_fd = -1
        for descriptor in owned_descriptors:
            if descriptor >= 0:
                _close_fd_once(descriptor)


_OPERATION_TEMPLATES = [
    ["config", "--file=<verified-config>", "--no-includes", "--null", "--list"],
    ["rev-parse", "--show-object-format=storage"],
    [
        "rev-parse",
        "--verify",
        "--end-of-options",
        "<validated-full-oid>^{commit}",
    ],
    [
        "rev-parse",
        "--verify",
        "--end-of-options",
        "<validated-full-oid>^{tree}",
    ],
    ["ls-tree", "-r", "-l", "-z", "--full-tree", "<validated-tree-oid>"],
    ["cat-file", "blob", "<validated-full-oid>"],
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
        return ("ls-tree", "-r", "-l", "-z", "--full-tree", value)
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


def _deadline_remaining(deadline: float) -> Optional[float]:
    try:
        return deadline - time.monotonic()
    except Exception:
        return None


def _run_git(
    git_dir: Path,
    config_path: Path,
    policy: TaskSnapshotPolicy,
    operation: str,
    value: Optional[str] = None,
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
        deadline = time.monotonic() + policy.git_timeout_seconds
    except Exception:
        _fail("task_git_failed")
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
    remaining = _deadline_remaining(deadline)
    if remaining is None:
        _cleanup_git_failure(process, (), grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(process, (), grace):
            _fail("task_git_failed")
        _fail("task_git_timeout")

    stdout_state = _DrainState()
    stderr_state = _DrainState()
    reader_specs = (
        (process.stdout, policy.max_git_stdout_bytes, stdout_state),
        (process.stderr, policy.max_git_stderr_bytes, stderr_state),
    )
    started_threads = []
    reader_setup_failure = None
    for stream, limit, state in reader_specs:
        remaining = _deadline_remaining(deadline)
        if remaining is None:
            reader_setup_failure = "task_git_failed"
            break
        if remaining <= 0:
            reader_setup_failure = "task_git_timeout"
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
        remaining = _deadline_remaining(deadline)
        if remaining is None:
            reader_setup_failure = "task_git_failed"
            break
        if remaining <= 0:
            reader_setup_failure = "task_git_timeout"
            break
        try:
            thread.start()
        except Exception:
            reader_setup_failure = "task_git_failed"
            break
        started_threads.append(thread)
        remaining = _deadline_remaining(deadline)
        if remaining is None:
            reader_setup_failure = "task_git_failed"
            break
        if remaining <= 0:
            reader_setup_failure = "task_git_timeout"
            break
    if reader_setup_failure is not None:
        if not _cleanup_git_failure(process, started_threads, grace):
            _fail("task_git_failed")
        _fail(reader_setup_failure)

    failure = None
    while True:
        try:
            remaining = deadline - time.monotonic()
        except Exception:
            failure = "task_git_failed"
            break
        if remaining <= 0:
            failure = "task_git_timeout"
            break
        if stdout_state.failed or stderr_state.failed:
            failure = "task_git_failed"
            break
        if stdout_state.overflow or stderr_state.overflow:
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
        remaining = deadline - time.monotonic()
    except Exception:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(
            process, started_threads, grace
        ):
            _fail("task_git_failed")
        _fail("task_git_timeout")
    try:
        return_code = process.wait(timeout=max(remaining, 0.001))
    except Exception:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    remaining = _deadline_remaining(deadline)
    if remaining is None:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(
            process, started_threads, grace
        ):
            _fail("task_git_failed")
        _fail("task_git_timeout")
    join_ok = _join_started_threads(
        started_threads, min(grace, remaining)
    )
    remaining = _deadline_remaining(deadline)
    if not join_ok or remaining is None:
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    if remaining <= 0:
        if not _cleanup_git_failure(
            process, started_threads, grace
        ):
            _fail("task_git_failed")
        _fail("task_git_timeout")
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
    if not _close_process_pipes(process):
        _cleanup_git_failure(process, started_threads, grace)
        _fail("task_git_failed")
    return bytes(stdout_state.data)


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
    if storage_output not in (b"sha1\n", b"sha256\n"):
        _fail("task_git_failed")
    if storage_output != (expected_format + "\n").encode("ascii"):
        _fail("task_source_config_invalid")
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
