#!/usr/bin/env python3
"""Install direct agent-adapter symlinks without replacing existing paths.

JSON manifests contain exact approved local paths and must be treated as sensitive.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


_PathToken = Tuple[int, int]


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _fingerprint(path: Path) -> Tuple[int, int, int, int, int]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _token(metadata: os.stat_result) -> _PathToken:
    return (metadata.st_dev, metadata.st_ino)


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("required no-follow directory operations are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class LinkEntry:
    name: str
    source: str
    target: str
    action: str
    reason: str

    def to_manifest(self) -> Dict[str, str]:
        return {
            "action": self.action,
            "name": self.name,
            "reason": self.reason,
            "source": self.source,
            "target": self.target,
        }


@dataclass(frozen=True)
class _LinkSpec:
    name: str
    source: Path
    target: Path
    source_fingerprint: Tuple[int, int, int, int, int]
    target_fingerprint: Optional[Tuple[int, int, int, int, int]]


@dataclass(frozen=True)
class _TargetApproval:
    root: Path
    parent: Path
    basename: str
    parent_token: _PathToken
    root_token: Optional[_PathToken]
    present: bool


@dataclass(frozen=True)
class LinkPlan:
    source_root: Path
    target_root: Path
    suffix: str
    entries: Tuple[LinkEntry, ...]
    plan_hash: str
    target_root_present: bool
    target_parent: Path
    target_basename: str
    target_parent_token: _PathToken
    target_root_token: Optional[_PathToken]
    _specs: Tuple[_LinkSpec, ...] = field(repr=False, compare=False)

    def _manifest_without_hash(self) -> Dict[str, object]:
        observations = []
        by_name = {spec.name: spec for spec in self._specs}
        for entry in self.entries:
            spec = by_name.get(entry.name)
            if spec is None:
                continue
            observations.append({
                "name": _nfc(spec.name),
                "source_lstat": list(spec.source_fingerprint),
                "target_lstat": (
                    list(spec.target_fingerprint)
                    if spec.target_fingerprint is not None
                    else None
                ),
            })
        return {
            "contains_local_paths": True,
            "entries": [entry.to_manifest() for entry in self.entries],
            "observations": observations,
            "sensitive": True,
            "source_root": _nfc(str(self.source_root)),
            "suffix": _nfc(self.suffix),
            "target_parent_lstat": list(self.target_parent_token),
            "target_root": _nfc(str(self.target_root)),
            "target_root_lstat": (
                list(self.target_root_token)
                if self.target_root_token is not None
                else None
            ),
            "target_root_present": self.target_root_present,
        }

    def to_manifest(self) -> Dict[str, object]:
        manifest = self._manifest_without_hash()
        manifest["plan_hash"] = self.plan_hash
        return manifest

    def to_json(self) -> str:
        return _canonical_json(self.to_manifest())


@dataclass(frozen=True)
class ResultEntry:
    name: str
    source: str
    target: Optional[str]
    reason: str
    target_path_stable: bool = True

    def to_manifest(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "reason": self.reason,
            "source": self.source,
            "target": self.target,
            "target_path_stable": self.target_path_stable,
        }


@dataclass(frozen=True)
class InstallResult:
    plan_hash: str
    status: str
    target_directory_created: bool
    created: Tuple[ResultEntry, ...] = ()
    kept: Tuple[ResultEntry, ...] = ()
    failed: Tuple[ResultEntry, ...] = ()

    def to_manifest(self) -> Dict[str, object]:
        return {
            "contains_local_paths": True,
            "created": [entry.to_manifest() for entry in self.created],
            "failed": [entry.to_manifest() for entry in self.failed],
            "kept": [entry.to_manifest() for entry in self.kept],
            "plan_hash": self.plan_hash,
            "sensitive": True,
            "status": self.status,
            "target_directory_created": self.target_directory_created,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_manifest())


class InstallError(Exception):
    def __init__(self, message: str, result: Optional[InstallResult] = None):
        super().__init__(message)
        self.result = result or InstallResult("", "blocked", False)


class InstallConflict(InstallError):
    pass


def _validate_suffix(suffix: str) -> str:
    if (
        not isinstance(suffix, str)
        or not suffix
        or suffix in (".", "..")
        or "\0" in suffix
        or os.sep in suffix
        or (os.altsep is not None and os.altsep in suffix)
        or unicodedata.normalize("NFC", suffix) != suffix
    ):
        raise ValueError("suffix must be a non-empty NFC filename suffix")
    return suffix


def _resolve_source_root(source_root: Path) -> Path:
    if not Path(source_root).is_absolute():
        raise ValueError("source root must be an absolute path")
    try:
        resolved = Path(source_root).resolve(strict=True)
    except OSError as error:
        raise ValueError("source root is unavailable: {}".format(error)) from error
    if not resolved.is_dir():
        raise ValueError("source root must be a directory")
    return resolved


def _inspect_directory(path: Path) -> _PathToken:
    descriptor = None
    try:
        before = path.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise OSError("not a directory")
        descriptor = os.open(str(path), _directory_flags())
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or _token(opened) != _token(before):
            raise OSError("directory identity changed")
        return _token(opened)
    except (OSError, RuntimeError):
        raise ValueError("approved directory is unavailable or changed") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _resolve_target_root(target_root: Path) -> _TargetApproval:
    if not Path(target_root).is_absolute():
        raise ValueError("target root must be an absolute path")
    raw = Path(os.path.abspath(str(target_root)))
    if not raw.name or raw.name in (".", ".."):
        raise ValueError("target root must have a single directory basename")
    try:
        parent = raw.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("target root parent is unavailable") from None
    parent_token = _inspect_directory(parent)
    root = parent / raw.name
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return _TargetApproval(root, parent, raw.name, parent_token, None, False)
    except OSError:
        raise ValueError("target root cannot be inspected") from None
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("target root must be a non-symlink directory")
    root_token = _inspect_directory(root)
    if root_token != _token(root_metadata):
        raise ValueError("target root identity changed during planning")
    return _TargetApproval(root, parent, raw.name, parent_token, root_token, True)


def _classify_target(source: Path, target: Path, target_root: Path) -> Tuple[str, str]:
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return "create", "target path is absent"
    except OSError as error:
        return "error", "target path cannot be inspected: {}".format(error)

    if not stat.S_ISLNK(metadata.st_mode):
        return "conflict", "target path already exists and is not a symlink"

    try:
        raw_link = Path(os.readlink(str(target)))
        immediate = raw_link if raw_link.is_absolute() else target.parent / raw_link
        immediate = Path(os.path.abspath(os.path.normpath(str(immediate))))
        immediate = immediate.parent.resolve(strict=True) / immediate.name
        resolved = target.resolve(strict=True)
    except FileNotFoundError:
        return "conflict", "target path is a broken symlink"
    except RuntimeError:
        return "error", "target symlink cannot be resolved safely"
    except OSError as error:
        return "error", "target symlink cannot be resolved: {}".format(error)

    if resolved == source and immediate == source:
        return "keep", "target already links directly to the approved source"
    if resolved == source:
        return "conflict", "target reaches the source through an indirect symlink"
    if not _contains(target_root, resolved):
        return "error", "target symlink resolves outside approved roots"
    return "conflict", "target symlink points to a different path"


def plan_links(source_root: Path, target_root: Path, suffix: str) -> LinkPlan:
    """Resolve approved roots and classify every source entry without mutation."""
    suffix = _validate_suffix(suffix)
    source = _resolve_source_root(Path(source_root))
    approval = _resolve_target_root(Path(target_root))
    target = approval.root
    target_present = approval.present

    candidates = []
    try:
        candidates = [path for path in source.iterdir() if path.name.endswith(suffix)]
    except OSError as error:
        raise ValueError("source root cannot be listed: {}".format(error)) from error
    candidates.sort(key=lambda path: (_nfc(path.name).casefold(), _nfc(path.name)))

    aliases: Dict[str, List[Path]] = {}
    for candidate in candidates:
        aliases.setdefault(_nfc(candidate.name).casefold(), []).append(candidate)

    target_aliases: Dict[str, List[Path]] = {}
    if target_present:
        try:
            for child in target.iterdir():
                target_aliases.setdefault(_nfc(child.name).casefold(), []).append(child)
        except OSError as error:
            raise ValueError("target root cannot be listed: {}".format(error)) from error

    entries: List[LinkEntry] = []
    specs: List[_LinkSpec] = []
    for candidate in candidates:
        display_name = _nfc(candidate.name)
        target_path = target / candidate.name
        action_reason = None
        try:
            source_fingerprint = _fingerprint(candidate)
        except OSError as error:
            entries.append(LinkEntry(
                display_name, _nfc(str(candidate)), _nfc(str(target_path)), "error",
                "source path cannot be inspected: {}".format(error),
            ))
            continue

        try:
            target_fingerprint = _fingerprint(target_path) if target_present else None
        except FileNotFoundError:
            target_fingerprint = None
        except OSError as error:
            target_fingerprint = None
            action_reason = ("error", "target path cannot be inspected: {}".format(error))

        alias_key = _nfc(candidate.name).casefold()
        target_name_aliases = [
            child for child in target_aliases.get(alias_key, [])
            if child.name != candidate.name
        ]
        if candidate.name != display_name:
            action, reason = "error", "source name is not Unicode NFC"
        elif len(aliases[alias_key]) > 1:
            action, reason = "error", "source name has a case or Unicode NFC alias"
        elif target_name_aliases:
            action, reason = "error", "target has a case or Unicode NFC alias"
        elif stat.S_ISLNK(source_fingerprint[2]):
            action, reason = "error", "source symlink candidates are not allowed"
        elif not stat.S_ISREG(source_fingerprint[2]):
            action, reason = "error", "source path is not a regular file"
        elif action_reason is not None:
            action, reason = action_reason
        elif not target_present:
            action, reason = "error", "target root must already exist"
        else:
            action, reason = _classify_target(candidate, target_path, target)

        entries.append(LinkEntry(
            display_name,
            _nfc(str(candidate)),
            _nfc(str(target / display_name)),
            action,
            reason,
        ))
        specs.append(_LinkSpec(
            candidate.name,
            candidate,
            target_path,
            source_fingerprint,
            target_fingerprint,
        ))

    provisional = LinkPlan(
        source_root=source,
        target_root=target,
        suffix=suffix,
        entries=tuple(entries),
        plan_hash="",
        target_root_present=target_present,
        target_parent=approval.parent,
        target_basename=approval.basename,
        target_parent_token=approval.parent_token,
        target_root_token=approval.root_token,
        _specs=tuple(specs),
    )
    digest = hashlib.sha256(
        _canonical_json(provisional._manifest_without_hash()).encode("utf-8")
    ).hexdigest()
    return LinkPlan(
        source_root=source,
        target_root=target,
        suffix=suffix,
        entries=tuple(entries),
        plan_hash=digest,
        target_root_present=target_present,
        target_parent=approval.parent,
        target_basename=approval.basename,
        target_parent_token=approval.parent_token,
        target_root_token=approval.root_token,
        _specs=tuple(specs),
    )


def _preflight(plan: LinkPlan) -> LinkPlan:
    try:
        current = plan_links(plan.source_root, plan.target_root, plan.suffix)
    except ValueError:
        raise InstallError(
            "plan can no longer be verified",
            InstallResult(
                plan.plan_hash,
                "blocked",
                False,
                failed=(_root_result_entry(plan, "plan can no longer be verified"),),
            ),
        ) from None
    if current.plan_hash != plan.plan_hash:
        conflicts = [entry for entry in current.entries if entry.action == "conflict"]
        error_type = InstallConflict if conflicts else InstallError
        failed = tuple(
            _result_entry(entry, "filesystem state changed after planning")
            for entry in current.entries
            if entry.action in ("conflict", "error")
        ) or (_root_result_entry(current, "filesystem state changed after planning"),)
        raise error_type(
            "filesystem state changed after planning; no links were created",
            InstallResult(plan.plan_hash, "blocked", False, failed=failed),
        )
    errors = [entry for entry in current.entries if entry.action == "error"]
    if errors:
        message = (
            "target root must already exist"
            if any(entry.reason == "target root must already exist" for entry in errors)
            else "plan contains error entries; no links were created"
        )
        raise InstallError(
            message,
            InstallResult(
                current.plan_hash,
                "blocked",
                False,
                failed=tuple(_result_entry(entry) for entry in errors),
            ),
        )
    conflicts = [entry for entry in current.entries if entry.action == "conflict"]
    if conflicts:
        raise InstallConflict(
            "plan contains conflicts; no links were created",
            InstallResult(
                current.plan_hash,
                "blocked",
                False,
                failed=tuple(_result_entry(entry) for entry in conflicts),
            ),
        )
    return current


def _result_entry(entry: LinkEntry, reason: Optional[str] = None) -> ResultEntry:
    return ResultEntry(entry.name, entry.source, entry.target, reason or entry.reason)


def _root_result_entry(
    plan: LinkPlan, reason: str, target_path_stable: bool = True
) -> ResultEntry:
    return ResultEntry(
        plan.target_basename,
        _nfc(str(plan.source_root)),
        _nfc(str(plan.target_root)),
        reason,
        target_path_stable,
    )


def _unstable_entries(entries: Sequence[ResultEntry]) -> Tuple[ResultEntry, ...]:
    return tuple(
        ResultEntry(
            entry.name,
            entry.source,
            None,
            "{}; target pathname is unstable".format(entry.reason),
            False,
        )
        for entry in entries
    )


def _failed_result(
    plan: LinkPlan,
    created: Sequence[ResultEntry],
    kept: Tuple[ResultEntry, ...],
    failed: ResultEntry,
    target_directory_created: bool,
) -> InstallResult:
    return InstallResult(
        plan.plan_hash,
        "partial" if created else "blocked",
        target_directory_created,
        tuple(created),
        kept,
        (failed,),
    )


def _open_approved_directory(path: Path, expected: _PathToken) -> int:
    descriptor = None
    try:
        descriptor = os.open(str(path), _directory_flags())
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or _token(metadata) != expected:
            raise OSError("directory identity changed")
        return descriptor
    except (OSError, RuntimeError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise InstallError("approved directory identity changed") from None


def _verify_root_identity(
    plan: LinkPlan,
    parent_fd: int,
    root_fd: int,
    root_token: _PathToken,
) -> None:
    current_parent_fd = None
    try:
        if _token(os.fstat(parent_fd)) != plan.target_parent_token:
            raise OSError("parent descriptor changed")
        if _token(os.fstat(root_fd)) != root_token:
            raise OSError("root descriptor changed")
        current_parent_fd = os.open(str(plan.target_parent), _directory_flags())
        if _token(os.fstat(current_parent_fd)) != plan.target_parent_token:
            raise OSError("parent path changed")
        root_path = os.stat(
            plan.target_basename, dir_fd=parent_fd, follow_symlinks=False
        )
        if not stat.S_ISDIR(root_path.st_mode) or _token(root_path) != root_token:
            raise OSError("root path changed")
    except (OSError, RuntimeError):
        raise InstallError("approved target directory identity changed") from None
    finally:
        if current_parent_fd is not None:
            try:
                os.close(current_parent_fd)
            except OSError:
                pass


def _failure_result_after_root_recheck(
    plan: LinkPlan,
    created: Sequence[ResultEntry],
    kept: Sequence[ResultEntry],
    failed: ResultEntry,
    parent_fd: int,
    root_fd: int,
    root_token: _PathToken,
) -> InstallResult:
    """Return a failure result without claiming paths after identity loss."""
    try:
        _verify_root_identity(plan, parent_fd, root_fd, root_token)
    except InstallError:
        created = _unstable_entries(created)
        kept = _unstable_entries(kept)
        failed = _unstable_entries((failed,))[0]
    return _failed_result(plan, created, tuple(kept), failed, False)


def apply_links(plan: LinkPlan) -> InstallResult:
    """Create direct symlinks; stop on EEXIST and never overwrite."""
    current = _preflight(plan)
    created = []  # type: List[ResultEntry]
    kept = tuple(
        _result_entry(entry) for entry in current.entries if entry.action == "keep"
    )
    target_directory_created = False
    parent_fd = None
    root_fd = None
    specs = {spec.name: spec for spec in current._specs}
    try:
        try:
            parent_fd = _open_approved_directory(
                current.target_parent, current.target_parent_token
            )
        except InstallError as error:
            result = _failed_result(
                current, created, kept,
                _root_result_entry(current, "target parent identity changed"),
                target_directory_created,
            )
            raise InstallError(str(error), result) from None

        if not current.target_root_present or current.target_root_token is None:
            result = _failed_result(
                current, created, kept,
                _root_result_entry(current, "target root must already exist"),
                False,
            )
            raise InstallError("target root must already exist", result) from None
        try:
            root_fd = os.open(
                current.target_basename,
                _directory_flags(),
                dir_fd=parent_fd,
            )
            root_metadata = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or _token(root_metadata) != current.target_root_token
            ):
                raise OSError("target root changed")
            root_token = current.target_root_token
        except OSError:
            result = _failed_result(
                current, created, kept,
                _root_result_entry(current, "target root identity changed"),
                False,
            )
            raise InstallError("target root identity changed", result) from None

        for entry in current.entries:
            if entry.action != "create":
                continue
            spec = specs[entry.name]
            try:
                _verify_root_identity(current, parent_fd, root_fd, root_token)
            except InstallError:
                result = _failed_result(
                    current,
                    _unstable_entries(created),
                    _unstable_entries(kept),
                    ResultEntry(
                        entry.name,
                        entry.source,
                        None,
                        "approved target pathname changed before link creation",
                        False,
                    ),
                    target_directory_created,
                )
                raise InstallError("approved target identity changed", result) from None
            try:
                if _fingerprint(spec.source) != spec.source_fingerprint:
                    raise OSError("source identity changed")
            except OSError:
                result = _failure_result_after_root_recheck(
                    current,
                    created,
                    kept,
                    _result_entry(entry, "approved source identity changed"),
                    parent_fd,
                    root_fd,
                    root_token,
                )
                raise InstallError("approved source identity changed", result) from None
            try:
                os.symlink(str(spec.source), entry.name, dir_fd=root_fd)
            except FileExistsError:
                result = _failure_result_after_root_recheck(
                    current,
                    created,
                    kept,
                    _result_entry(entry, "target appeared during apply and was preserved"),
                    parent_fd,
                    root_fd,
                    root_token,
                )
                raise InstallConflict(
                    "target appeared during apply; existing path was preserved", result
                ) from None
            except OSError:
                result = _failure_result_after_root_recheck(
                    current,
                    created,
                    kept,
                    _result_entry(entry, "direct symlink creation failed"),
                    parent_fd,
                    root_fd,
                    root_token,
                )
                raise InstallError("link creation failed", result) from None
            created.append(_result_entry(entry, "direct symlink created"))
            try:
                _verify_root_identity(current, parent_fd, root_fd, root_token)
            except InstallError:
                result = _failed_result(
                    current, _unstable_entries(created), _unstable_entries(kept),
                    _root_result_entry(
                        current, "target path changed during apply", False
                    ),
                    target_directory_created,
                )
                raise InstallError("target path changed during apply", result) from None
        return InstallResult(
            current.plan_hash,
            "success",
            target_directory_created,
            tuple(created),
            kept,
            (),
        )
    finally:
        for descriptor in (root_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--suffix", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _planning_failure_result(args: argparse.Namespace) -> InstallResult:
    source = _nfc(os.path.abspath(str(args.source_root)))
    target = _nfc(os.path.abspath(str(args.target_root)))
    return InstallResult(
        "",
        "blocked",
        False,
        failed=(ResultEntry(
            Path(target).name or "target-root",
            source,
            target,
            "approved source or target could not be planned safely",
        ),),
    )


def _print_non_json_result(result: InstallResult, message: str = "") -> None:
    if message:
        print("installer error: {}".format(message), file=sys.stderr)
    stream = sys.stderr if result.status != "success" else sys.stdout
    print(
        "{}: created={} kept={} failed={} target_directory_created={}".format(
            result.status,
            len(result.created),
            len(result.kept),
            len(result.failed),
            str(result.target_directory_created).lower(),
        ),
        file=stream,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = plan_links(args.source_root, args.target_root, args.suffix)
    except (ValueError, OSError, RuntimeError):
        result = _planning_failure_result(args)
        if args.json:
            print(result.to_json())
        else:
            _print_non_json_result(result, "planning failed safely")
        return 2

    if args.dry_run:
        if args.json:
            print(plan.to_json())
        else:
            for entry in plan.entries:
                print("{}\t{}\t{}".format(entry.action, entry.name, entry.reason))
        return 2 if any(
            entry.action in ("conflict", "error") for entry in plan.entries
        ) else 0

    try:
        result = apply_links(plan)
    except InstallError as error:
        if args.json:
            print(error.result.to_json())
        else:
            _print_non_json_result(error.result, str(error))
        return 2
    if args.json:
        print(result.to_json())
    else:
        _print_non_json_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
