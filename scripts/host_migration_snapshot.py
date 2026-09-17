#!/usr/bin/env python3
"""Prepare, verify, and roll back a manifest-bound host-config snapshot."""

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Tuple


if __name__ == "__main__" and not sys.flags.isolated:
    print(
        json.dumps(
            {
                "status": "error",
                "issues": [
                    {"message": "snapshot CLI requires isolated Python (-I)"}
                ],
            },
            sort_keys=True,
        )
    )
    raise SystemExit(1)


LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def _verify_bound_executor_before_import(journal: Dict[str, Any]) -> None:
    """Verify the journal-bound copied core before importing a sibling module."""
    stages = journal.get("stages")
    envelope_digests = journal.get("stage_envelopes")
    envelope_paths = journal.get("stage_envelope_paths")
    if (
        not isinstance(stages, list)
        or not isinstance(envelope_digests, dict)
        or not isinstance(envelope_paths, dict)
        or set(envelope_paths) != set(envelope_digests)
    ):
        raise ValueError("journal is missing bound approval envelope paths")
    bound_stages = [stage for stage in stages if stage in envelope_digests]
    if not bound_stages:
        raise ValueError("journal has no bound approval envelope")
    stage = bound_stages[-1]
    envelope_path_value = envelope_paths.get(stage)
    envelope_digest = envelope_digests.get(stage)
    if (
        not isinstance(envelope_path_value, str)
        or not Path(envelope_path_value).is_absolute()
        or str(Path(envelope_path_value)) != envelope_path_value
        or not isinstance(envelope_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", envelope_digest)
    ):
        raise ValueError("journal bound approval envelope is invalid")
    envelope_path = Path(envelope_path_value)
    envelope_bytes = _read_sealed_bytes(
        envelope_path, "journal-bound approval envelope"
    )
    if hashlib.sha256(envelope_bytes).hexdigest() != envelope_digest:
        raise ValueError("journal-bound approval envelope checksum differs")
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    executors = envelope.get("executors") if isinstance(envelope, dict) else None
    if not isinstance(executors, list):
        raise ValueError("journal-bound executor inventory is invalid")
    references = {
        item.get("relative_path"): item
        for item in executors
        if isinstance(item, dict)
    }
    sibling_root = Path(__file__).resolve(strict=True).parent
    for filename in (
        "host_migration_apply.py",
        "host_migration_envelope.py",
        "host_migration_snapshot.py",
    ):
        relative = "scripts/{}".format(filename)
        reference = references.get(relative)
        expected_path = sibling_root / filename
        if (
            not isinstance(reference, dict)
            or set(reference) != {"relative_path", "path", "sha256"}
            or reference.get("path") != str(expected_path)
            or not isinstance(reference.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
        ):
            raise ValueError(
                "journal-bound executor identity is invalid: {}".format(relative)
            )
        data = _read_sealed_bytes(expected_path, "journal-bound {}".format(relative))
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError(
                "journal-bound executor checksum differs: {}".format(relative)
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_sealed_bytes(path: Path, label: str) -> bytes:
    _regular_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("{} must be a regular file".format(label))
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
            raise ValueError("{} changed while it was read".format(label))
        return b"".join(chunks)
    finally:
        os.close(descriptor)


class _OperationLock:
    def __init__(self, path: Path):
        self.path = path
        self.descriptor: Optional[int] = None

    def __enter__(self) -> "_OperationLock":
        _real_directory(self.path.parent, "migration lock directory")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        self.descriptor = os.open(str(self.path), flags, 0o600)
        try:
            metadata = os.fstat(self.descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ValueError("migration lock must be an owner-controlled regular file")
            os.fchmod(self.descriptor, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.descriptor)
            self.descriptor = None
            raise ValueError("another host migration operation is active") from None
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def _host_operation_lock_path(home_root: Path) -> Path:
    """Return one owner-protected lock shared by migrations for a host home."""
    _real_directory(home_root, "migration home")
    lock_root = home_root / ".codex"
    _real_directory(lock_root, "migration lock directory")
    metadata = lock_root.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError("migration lock directory must be owner-controlled")
    return lock_root / ".host-policy-migration.lock"


def _real_directory(path: Path, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("{} must be a real non-symlink directory: {}".format(label, path))


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("{} must be a regular non-symlink file: {}".format(label, path))
    _real_directory(path.parent, "{} parent".format(label))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o7777


def _fsync_directory(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename source only when target does not exist."""
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(str(source))
    target_bytes = os.fsencode(str(target))
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as error:
            raise ValueError("atomic no-replace rename is unavailable") from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, target_bytes, 1)
    elif sys.platform == "darwin":
        try:
            rename = library.renamex_np
        except AttributeError as error:
            raise ValueError("atomic no-replace rename is unavailable") from error
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000004)
    else:
        raise ValueError("atomic no-replace rename is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(error_number, os.strerror(error_number), str(target))
    if error_number in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
        raise ValueError("atomic no-replace rename is unavailable")
    raise OSError(error_number, os.strerror(error_number), str(target))


def _exchange_paths(first: Path, second: Path) -> None:
    """Atomically exchange two existing paths without an unsafe fallback."""
    library = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(str(first))
    second_bytes = os.fsencode(str(second))
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as error:
            raise ValueError("atomic exchange rename is unavailable") from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, first_bytes, -100, second_bytes, 2)
    elif sys.platform == "darwin":
        try:
            rename = library.renamex_np
        except AttributeError as error:
            raise ValueError("atomic exchange rename is unavailable") from error
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(first_bytes, second_bytes, 0x00000002)
    else:
        raise ValueError("atomic exchange rename is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
        raise ValueError("atomic exchange rename is unavailable")
    raise OSError(error_number, os.strerror(error_number))


def _verify_manifest_prestate(record: Dict[str, Any]) -> None:
    source = Path(record["path"])
    _real_directory(source.parent, "{} parent".format(record["label"]))
    if record["pre_state"] == "absent":
        if source.exists() or source.is_symlink():
            raise ValueError(
                "manifest source state differs before snapshot: {}".format(
                    record["label"]
                )
            )
        return
    _regular_file(source, record["label"])
    if (
        "{:04o}".format(_mode(source)) != record["current_mode"]
        or _sha256(source) != record["current_sha256"]
    ):
        raise ValueError(
            "manifest source state differs before snapshot: {}".format(
                record["label"]
            )
        )


def _copy_sealed_source(source: Path, backup: Path) -> Tuple[int, str]:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(str(source), source_flags)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("snapshot source changed to a non-regular file: {}".format(source))
        backup_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        backup_fd = os.open(str(backup), backup_flags, 0o600)
        digest = hashlib.sha256()
        try:
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(backup_fd, view)
                    if written <= 0:
                        raise OSError("snapshot backup write made no progress")
                    view = view[written:]
            os.fchmod(backup_fd, before.st_mode & 0o7777)
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)
        after = os.fstat(source_fd)
        path_state = source.lstat()
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in stable_fields) or (
            path_state.st_dev,
            path_state.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise ValueError("snapshot source changed while it was copied: {}".format(source))
        return before.st_mode & 0o7777, digest.hexdigest()
    finally:
        os.close(source_fd)


def snapshot(
    output_dir: Path,
    files: Iterable[Tuple[str, Path]],
    absent_files: Iterable[Tuple[str, Path]],
) -> Dict[str, Any]:
    entries = list(files)
    absent_entries = list(absent_files)
    labels = [label for label, _ in entries + absent_entries]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("snapshot requires one or more unique labels")
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("snapshot output directory must not already exist")
    _real_directory(output_dir.parent, "snapshot parent")
    for label, source in entries:
        _regular_file(source, label)
    for label, source in absent_entries:
        _real_directory(source.parent, "{} parent".format(label))
        if source.exists() or source.is_symlink():
            raise ValueError("{} must be absent before snapshot: {}".format(label, source))
    output_dir.mkdir(mode=0o700, parents=False)
    files_dir = output_dir / "files"
    files_dir.mkdir(mode=0o700)
    records: List[Dict[str, Any]] = []
    for label, source in sorted(entries):
        backup = files_dir / label
        source_mode, source_digest = _copy_sealed_source(source, backup)
        records.append(
            {
                "label": label,
                "state": "present",
                "source": str(source.resolve(strict=True)),
                "backup": "files/{}".format(label),
                "mode": "{:04o}".format(source_mode),
                "sha256": source_digest,
            }
        )
    for label, source in sorted(absent_entries):
        records.append(
            {
                "label": label,
                "state": "absent",
                "source": str(source.parent.resolve(strict=True) / source.name),
            }
        )
    records.sort(key=lambda record: record["label"])
    receipt = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": records,
    }
    receipt_path = output_dir / "receipt.json"
    with receipt_path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        os.fchmod(handle.fileno(), 0o600)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(files_dir)
    _fsync_directory(output_dir)
    return receipt


def prepare(manifest_path: Path, output_dir: Path) -> Dict[str, Any]:
    """Derive the complete snapshot inventory from the approved manifest."""
    apply_module = _apply_module()

    manifest_bytes, manifest = apply_module.load_manifest_sealed(
        manifest_path, require_complete_layout=False
    )
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("snapshot output directory must not already exist")
    _real_directory(output_dir.parent, "snapshot parent")
    files = []
    absent_files = []
    for record in manifest["targets"]:
        _verify_manifest_prestate(record)
        entry = (record["label"], Path(record["path"]))
        if record["pre_state"] == "present":
            files.append(entry)
        else:
            absent_files.append(entry)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=".host-policy-snapshot.", dir=str(output_dir.parent))
    )
    staging_parent.chmod(0o700)
    staging = staging_parent / "snapshot"
    try:
        receipt = snapshot(staging, files, absent_files)
        if _sha256(manifest_path) != manifest_digest:
            raise ValueError("manifest changed while the snapshot was prepared")
        apply_module.bind_snapshot(manifest, staging / "receipt.json")
        for record in manifest["targets"]:
            _verify_manifest_prestate(record)
        try:
            _rename_noreplace(staging, output_dir)
        except FileExistsError as error:
            raise ValueError(
                "snapshot output directory appeared during publication"
            ) from error
        _fsync_directory(output_dir.parent)
        staging_parent.rmdir()
        _fsync_directory(output_dir.parent)
        return receipt
    except BaseException:
        if staging_parent.is_dir() and not staging_parent.is_symlink():
            shutil.rmtree(str(staging_parent))
            _fsync_directory(output_dir.parent)
        raise


def _validate_receipt(value: Any, receipt_path: Path) -> Dict[str, Any]:
    _real_directory(receipt_path.parent / "files", "snapshot files directory")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("receipt must use schema_version 1")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("receipt files must be a non-empty list")
    labels = set()
    sources = set()
    for record in files:
        if not isinstance(record, dict):
            raise ValueError("receipt file entry must be a mapping")
        state = record.get("state")
        expected_keys = (
            {"label", "state", "source", "backup", "mode", "sha256"}
            if state == "present"
            else {"label", "state", "source"}
        )
        if state not in ("present", "absent") or set(record) != expected_keys:
            raise ValueError("receipt file entry has an invalid schema")
        label = record["label"]
        if not isinstance(label, str) or not LABEL.fullmatch(label) or label in labels:
            raise ValueError("receipt labels must be valid and unique")
        labels.add(label)
        source = record["source"]
        if not isinstance(source, str) or not Path(source).is_absolute() or source in sources:
            raise ValueError("receipt source paths must be absolute and unique")
        sources.add(source)
        if state == "present":
            if record["backup"] != "files/{}".format(label):
                raise ValueError("receipt backup path must be canonical")
            if not isinstance(record["mode"], str) or not re.fullmatch(r"[0-7]{4}", record["mode"]):
                raise ValueError("receipt mode must be four octal digits")
            if not isinstance(record["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", record["sha256"]
            ):
                raise ValueError("receipt sha256 must be lowercase hex")
    return value


def load_receipt_sealed(receipt_path: Path) -> Tuple[bytes, Dict[str, Any]]:
    data = _read_sealed_bytes(receipt_path, "receipt")
    return data, _validate_receipt(json.loads(data.decode("utf-8")), receipt_path)


def load_receipt(receipt_path: Path) -> Dict[str, Any]:
    return load_receipt_sealed(receipt_path)[1]


def verify(
    receipt_path: Path,
    scope: str,
    labels: Optional[Iterable[str]] = None,
    receipt: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    receipt = load_receipt(receipt_path) if receipt is None else receipt
    selected = None if labels is None else set(labels)
    receipt_labels = {record["label"] for record in receipt["files"]}
    if selected is not None and not selected.issubset(receipt_labels):
        raise ValueError("verification labels are not present in the receipt")
    issues: List[Dict[str, str]] = []
    for record in receipt["files"]:
        if selected is not None and record["label"] not in selected:
            continue
        if record["state"] == "absent":
            if scope in ("source", "both"):
                path = Path(record["source"])
                if path.exists() or path.is_symlink():
                    issues.append(
                        {
                            "label": record["label"],
                            "scope": "source",
                            "message": "expected path to remain absent",
                        }
                    )
            continue
        candidates = []
        if scope in ("source", "both"):
            candidates.append(("source", Path(record["source"])))
        if scope in ("backup", "both"):
            candidates.append(("backup", receipt_path.parent / record["backup"]))
        for kind, path in candidates:
            try:
                _regular_file(path, "{} {}".format(record["label"], kind))
                if _sha256(path) != record["sha256"]:
                    raise ValueError("checksum differs")
                if _mode(path) != int(record["mode"], 8):
                    raise ValueError("mode differs")
            except (OSError, ValueError) as error:
                issues.append(
                    {
                        "label": record["label"],
                        "scope": kind,
                        "message": str(error),
                    }
                )
    return issues


def _classify_live_state(
    receipt_record: Dict[str, Any], target_record: Dict[str, Any]
) -> str:
    source = Path(receipt_record["source"])
    if source.is_symlink() or (source.exists() and not source.is_file()):
        return "drift"
    if not source.exists():
        return "prestate" if receipt_record["state"] == "absent" else "drift"
    current_mode = "{:04o}".format(_mode(source))
    current_sha = _sha256(source)
    if (
        receipt_record["state"] == "present"
        and current_mode == receipt_record["mode"]
        and current_sha == receipt_record["sha256"]
    ):
        return "prestate"
    if (
        current_mode == target_record["target_mode"]
        and current_sha == target_record["target_sha256"]
    ):
        return "target"
    return "drift"


def _read_sealed_backup(
    receipt_path: Path, record: Dict[str, Any]
) -> bytes:
    backup = receipt_path.parent / record["backup"]
    _regular_file(backup, "{} backup".format(record["label"]))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(backup), flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("snapshot backup is not a regular file")
        digest = hashlib.sha256()
        chunks = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
        current = backup.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
            current.st_dev,
            current.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise ValueError("snapshot backup changed while it was read")
        if (
            "{:04o}".format(after.st_mode & 0o7777) != record["mode"]
            or digest.hexdigest() != record["sha256"]
        ):
            raise ValueError("snapshot backup differs from its receipt")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _path_matches(path: Path, mode: str, digest: str, label: str) -> bool:
    try:
        data = _read_sealed_bytes(path, label)
    except (OSError, ValueError):
        return False
    return "{:04o}".format(_mode(path)) == mode and hashlib.sha256(data).hexdigest() == digest


def _restore_backup_noreplace(source: Path, data: bytes, mode: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".host-policy-restore.", dir=str(source.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(data)
            os.fchmod(destination.fileno(), int(mode, 8))
            destination.flush()
            os.fsync(destination.fileno())
        _rename_noreplace(temporary, source)
        _fsync_directory(source.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_live_rollback_scope(
    manifest: Dict[str, Any],
    receipt: Dict[str, Any],
    rollback_labels: Iterable[str],
) -> Dict[str, str]:
    selected = set(rollback_labels)
    receipt_records = {record["label"]: record for record in receipt["files"]}
    manifest_records = {record["label"]: record for record in manifest["targets"]}
    states = {}
    for label, target_record in manifest_records.items():
        state = _classify_live_state(receipt_records[label], target_record)
        states[label] = state
        if label in selected and state == "drift":
            raise ValueError(
                "restore target differs from both approved states: {}".format(
                    target_record["path"]
                )
            )
        if label not in selected and state == "target":
            raise ValueError(
                "installed target is missing from journal progress: {}".format(label)
            )
    return states


def _restore_receipt(
    receipt_path: Path,
    receipt: Dict[str, Any],
    manifest: Dict[str, Any],
    journal_path: Path,
    journal: Dict[str, Any],
) -> Dict[str, Any]:
    labels = list(journal["rollback_labels"])
    if len(labels) != len(set(labels)):
        raise ValueError("rollback labels must be unique")
    backup_issues = verify(receipt_path, "backup", labels, receipt)
    if backup_issues:
        raise ValueError("backup verification failed: {}".format(backup_issues))
    receipt_records = {record["label"]: record for record in receipt["files"]}
    manifest_records = {record["label"]: record for record in manifest["targets"]}
    try:
        records = [
            (receipt_records[label], manifest_records[label]) for label in labels
        ]
    except KeyError as error:
        raise ValueError("rollback label is not present in the receipt") from error
    _validate_live_rollback_scope(manifest, receipt, labels)
    backups: Dict[str, bytes] = {}
    for receipt_record, target_record in records:
        record = receipt_record
        source = Path(record["source"])
        _real_directory(source.parent, "restore target parent")
        if record["state"] == "present":
            backups[record["label"]] = _read_sealed_backup(receipt_path, record)
    restored = list(journal["rolled_back_labels"])
    removed: List[str] = []
    retained = journal.setdefault("retained_quarantines", {})
    for record, target_record in records[len(journal["rolled_back_labels"]) :]:
        source = Path(record["source"])
        quarantine_value = journal.get("rollback_quarantine")
        if quarantine_value is None:
            quarantine = source.parent / ".host-policy-rollback.{}".format(
                secrets.token_hex(12)
            )
        else:
            quarantine = Path(quarantine_value)
            expected_prefix = ".host-policy-rollback."
            if (
                quarantine.parent != source.parent
                or not quarantine.name.startswith(expected_prefix)
                or len(quarantine.name) != len(expected_prefix) + 24
            ):
                raise ValueError("rollback quarantine path is invalid")
        journal["rollback_active_label"] = record["label"]
        journal["rollback_quarantine"] = str(quarantine)
        _write_journal(journal_path, journal)
        state = _classify_live_state(record, target_record)
        if quarantine.exists() or quarantine.is_symlink():
            if not _path_matches(
                quarantine,
                target_record["target_mode"],
                target_record["target_sha256"],
                "rollback quarantine",
            ):
                raise ValueError(
                    "rollback quarantine differs from the approved target: {}".format(
                        quarantine
                    )
                )
        elif state == "prestate":
            restored.append(record["label"])
            journal["rolled_back_labels"].append(record["label"])
            journal["rollback_active_label"] = None
            journal["rollback_quarantine"] = None
            _write_journal(journal_path, journal)
            continue
        elif state == "target":
            try:
                _rename_noreplace(source, quarantine)
                _fsync_directory(source.parent)
            except FileExistsError as error:
                raise ValueError("rollback quarantine unexpectedly exists") from error
            if not _path_matches(
                quarantine,
                target_record["target_mode"],
                target_record["target_sha256"],
                "quarantined target",
            ):
                if not source.exists() and not source.is_symlink():
                    _rename_noreplace(quarantine, source)
                    _fsync_directory(source.parent)
                    journal["rollback_quarantine"] = None
                    _write_journal(journal_path, journal)
                raise ValueError("restore target changed during rollback isolation")
        else:
            raise ValueError(
                "restore target differs from both approved states: {}".format(source)
            )

        if record["state"] == "absent":
            if source.exists() or source.is_symlink():
                raise ValueError("restore target changed after rollback isolation")
            removed.append(record["label"])
        else:
            current_state = _classify_live_state(record, target_record)
            if current_state == "drift" and not source.exists() and not source.is_symlink():
                _restore_backup_noreplace(
                    source,
                    backups[record["label"]],
                    record["mode"],
                )
            elif current_state != "prestate":
                raise ValueError("restore target changed after rollback isolation")
        retained["rollback:{}".format(record["label"])] = str(quarantine)
        restored.append(record["label"])
        journal["rolled_back_labels"].append(record["label"])
        journal["rollback_active_label"] = None
        journal["rollback_quarantine"] = None
        _write_journal(journal_path, journal)
    issues = verify(receipt_path, "source", labels, receipt)
    if issues:
        raise ValueError("post-restore verification failed: {}".format(issues))
    return {
        "restored": restored,
        "removed_new_files": removed,
        "retained_quarantines": dict(retained),
    }


def _write_journal(journal_path: Path, payload: Dict[str, Any]) -> None:
    _real_directory(journal_path.parent, "journal directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".host-policy-rollback-journal.", dir=str(journal_path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(str(temporary), str(journal_path))
        _fsync_directory(journal_path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def rollback(journal_path: Path, confirmed: bool) -> Dict[str, Any]:
    if not confirmed:
        raise ValueError("rollback requires --confirm-rollback")
    journal_path = Path(os.path.abspath(str(journal_path)))
    journal = json.loads(_read_sealed_bytes(journal_path, "journal").decode("utf-8"))
    if not isinstance(journal, dict) or not isinstance(journal.get("manifest"), str):
        raise ValueError("journal is missing manifest")
    _verify_bound_executor_before_import(journal)
    apply_module = _apply_module()
    _, manifest = apply_module.load_manifest_sealed(
        Path(journal["manifest"]), require_complete_layout=False
    )
    lock_path = _host_operation_lock_path(
        apply_module.target_home_root(manifest["targets"])
    )
    with _OperationLock(lock_path):
        return _rollback_unlocked(journal_path, confirmed, lock_path)


def _rollback_unlocked(
    journal_path: Path, confirmed: bool, operation_lock_path: Path
) -> Dict[str, Any]:
    if not confirmed:
        raise ValueError("rollback requires --confirm-rollback")
    journal = json.loads(_read_sealed_bytes(journal_path, "journal").decode("utf-8"))
    if not isinstance(journal, dict) or journal.get("schema_version") != 3:
        raise ValueError("journal must use schema_version 3")
    for key in (
        "manifest",
        "manifest_sha256",
        "snapshot_receipt",
        "snapshot_receipt_sha256",
    ):
        if not isinstance(journal.get(key), str) or not journal[key]:
            raise ValueError("journal is missing {}".format(key))
    manifest_path = Path(journal["manifest"])
    receipt_path = Path(journal["snapshot_receipt"])
    apply_module = _apply_module()
    manifest_bytes, manifest = apply_module.load_manifest_sealed(
        manifest_path, require_complete_layout=False
    )
    expected_lock_path = _host_operation_lock_path(
        apply_module.target_home_root(manifest["targets"])
    )
    if expected_lock_path != operation_lock_path:
        raise ValueError("journal manifest changed host-operation lock identity")
    receipt_bytes, receipt = load_receipt_sealed(receipt_path)
    if hashlib.sha256(manifest_bytes).hexdigest() != journal["manifest_sha256"]:
        raise ValueError("journal manifest checksum differs")
    if hashlib.sha256(receipt_bytes).hexdigest() != journal["snapshot_receipt_sha256"]:
        raise ValueError("journal snapshot receipt checksum differs")
    apply_module.bind_snapshot(manifest, receipt_path, receipt)
    progress = apply_module.validate_journal_progress(journal, manifest)
    if progress["status"] == "rolled_back":
        raise ValueError("journal has already been rolled back")
    if progress["status"] != "rolling_back":
        journal["rollback_source_status"] = journal["status"]
        journal["rollback_source_rollback_required"] = journal[
            "rollback_required"
        ]
        journal["rollback_labels"] = list(progress["rollback_labels"])
        journal["rolled_back_labels"] = []
        journal["rollback_active_label"] = None
        journal["rollback_quarantine"] = None
        journal["status"] = "rolling_back"
        journal["rollback_required"] = True
        _write_journal(journal_path, journal)
    result = _restore_receipt(
        receipt_path, receipt, manifest, journal_path, journal
    )
    journal["completed_before_rollback"] = list(journal["completed"])
    journal["completed_stages_before_rollback"] = list(journal["completed_stages"])
    journal["rolled_back_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    journal["status"] = "rolled_back"
    journal["completed"] = []
    journal["completed_stages"] = []
    journal["active_stage"] = None
    journal["active_label"] = None
    journal["active_attempt_id"] = None
    journal["pending_audit_stage"] = None
    journal["pending_audit_envelope_sha256"] = None
    journal["rollback_quarantine"] = None
    journal["rollback_required"] = False
    _write_journal(journal_path, journal)
    return {**result, "journal_status": "rolled_back"}


def build_parser() -> argparse.ArgumentParser:
    parser = StructuredArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=StructuredArgumentParser
    )
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--manifest", type=Path, required=True)
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--receipt", type=Path, required=True)
    verify_parser.add_argument(
        "--scope", choices=("source", "backup", "both"), default="both"
    )
    digest_parser = subparsers.add_parser("target-digest")
    digest_parser.add_argument("--manifest", type=Path, required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--journal", type=Path, required=True)
    rollback_parser.add_argument("--confirm-rollback", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "prepare":
            payload = {
                "status": "ok",
                "receipt": prepare(args.manifest, args.output_dir),
                "manifest_sha256": _sha256(args.manifest),
            }
        elif args.command == "verify":
            issues = verify(args.receipt, args.scope)
            payload = {"status": "error" if issues else "ok", "issues": issues}
        elif args.command == "target-digest":
            apply_module = _apply_module()
            raw_manifest = json.loads(
                _read_sealed_bytes(args.manifest, "manifest").decode("utf-8")
            )
            if not isinstance(raw_manifest, dict):
                raise ValueError("manifest root must be a mapping")
            payload = {
                "status": "ok",
                "target_inventory_sha256": apply_module._target_inventory_sha256(
                    raw_manifest
                ),
            }
        else:
            payload = {"status": "ok", **rollback(args.journal, args.confirm_rollback)}
    except (OSError, ValueError, ImportError, json.JSONDecodeError) as error:
        payload = {"status": "error", "issues": [{"message": str(error)}]}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if payload["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
