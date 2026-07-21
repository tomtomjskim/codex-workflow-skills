#!/usr/bin/env python3
"""Install direct agent-adapter symlinks without replacing existing paths."""

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
class LinkPlan:
    source_root: Path
    target_root: Path
    suffix: str
    entries: Tuple[LinkEntry, ...]
    plan_hash: str
    target_root_present: bool
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
            "entries": [entry.to_manifest() for entry in self.entries],
            "observations": observations,
            "source_root": _nfc(str(self.source_root)),
            "suffix": _nfc(self.suffix),
            "target_root": _nfc(str(self.target_root)),
            "target_root_present": self.target_root_present,
        }

    def to_manifest(self) -> Dict[str, object]:
        manifest = self._manifest_without_hash()
        manifest["plan_hash"] = self.plan_hash
        return manifest

    def to_json(self) -> str:
        return _canonical_json(self.to_manifest())


@dataclass(frozen=True)
class InstallResult:
    created: Tuple[str, ...] = ()
    kept: Tuple[str, ...] = ()
    failed: Optional[str] = None


class InstallError(Exception):
    def __init__(self, message: str, result: Optional[InstallResult] = None):
        super().__init__(message)
        self.result = result or InstallResult()


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
    try:
        resolved = Path(source_root).resolve(strict=True)
    except OSError as error:
        raise ValueError("source root is unavailable: {}".format(error)) from error
    if not resolved.is_dir():
        raise ValueError("source root must be a directory")
    return resolved


def _resolve_target_root(target_root: Path) -> Tuple[Path, bool]:
    raw = Path(target_root)
    try:
        raw_metadata = raw.lstat()
    except FileNotFoundError:
        raw_metadata = None
    except OSError as error:
        raise ValueError("target root cannot be inspected: {}".format(error)) from error

    if raw_metadata is not None:
        resolved = raw.resolve(strict=True)
        if not stat.S_ISDIR(raw_metadata.st_mode) or not resolved.is_dir():
            raise ValueError("target root must be a directory")
        return resolved, True

    try:
        parent = raw.parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("target root parent is unavailable: {}".format(error)) from error
    if not parent.is_dir():
        raise ValueError("target root parent must be a directory")
    return parent / raw.name, False


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
    target, target_present = _resolve_target_root(Path(target_root))

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

        try:
            resolved_candidate = candidate.resolve(strict=True)
        except OSError as error:
            resolved_candidate = candidate
            action_reason = ("error", "source path cannot be resolved: {}".format(error))

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
        elif not _contains(source, resolved_candidate):
            action, reason = "error", "source path resolves outside approved root"
        elif not resolved_candidate.is_file():
            action, reason = "error", "source path is not a regular file"
        elif action_reason is not None:
            action, reason = action_reason
        else:
            action, reason = _classify_target(resolved_candidate, target_path, target)

        entries.append(LinkEntry(
            display_name,
            _nfc(str(resolved_candidate)),
            _nfc(str(target / display_name)),
            action,
            reason,
        ))
        specs.append(_LinkSpec(
            candidate.name,
            resolved_candidate,
            target_path,
            source_fingerprint,
            target_fingerprint,
        ))

    provisional = LinkPlan(
        source, target, suffix, tuple(entries), "", target_present, tuple(specs)
    )
    digest = hashlib.sha256(
        _canonical_json(provisional._manifest_without_hash()).encode("utf-8")
    ).hexdigest()
    return LinkPlan(
        source, target, suffix, tuple(entries), digest, target_present, tuple(specs)
    )


def _preflight(plan: LinkPlan) -> LinkPlan:
    try:
        current = plan_links(plan.source_root, plan.target_root, plan.suffix)
    except ValueError as error:
        raise InstallError("plan can no longer be verified: {}".format(error)) from error
    if current.plan_hash != plan.plan_hash:
        conflicts = [entry for entry in current.entries if entry.action == "conflict"]
        error_type = InstallConflict if conflicts else InstallError
        raise error_type("filesystem state changed after planning; no links were created")
    errors = [entry for entry in current.entries if entry.action == "error"]
    if errors:
        raise InstallError("plan contains error entries; no links were created")
    conflicts = [entry for entry in current.entries if entry.action == "conflict"]
    if conflicts:
        raise InstallConflict("plan contains conflicts; no links were created")
    return current


def apply_links(plan: LinkPlan) -> InstallResult:
    """Create direct symlinks; stop on EEXIST and never overwrite."""
    current = _preflight(plan)
    created: List[str] = []
    kept = tuple(entry.name for entry in current.entries if entry.action == "keep")

    if not current.target_root_present:
        try:
            os.mkdir(str(current.target_root), 0o700)
        except FileExistsError as error:
            result = InstallResult(tuple(created), kept, current.target_root.name)
            raise InstallConflict(
                "target directory appeared during apply; nothing was removed", result
            ) from error
        except OSError as error:
            result = InstallResult(tuple(created), kept, current.target_root.name)
            raise InstallError("target directory could not be created: {}".format(error), result) from error

    specs = {spec.name: spec for spec in current._specs}
    for entry in current.entries:
        if entry.action != "create":
            continue
        spec = specs[entry.name]
        try:
            os.symlink(str(spec.source), str(spec.target))
        except FileExistsError as error:
            result = InstallResult(tuple(created), kept, entry.name)
            raise InstallConflict(
                "target appeared during apply; existing path was preserved", result
            ) from error
        except OSError as error:
            result = InstallResult(tuple(created), kept, entry.name)
            raise InstallError("link creation failed: {}".format(error), result) from error
        created.append(entry.name)
    return InstallResult(tuple(created), kept)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--suffix", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = plan_links(args.source_root, args.target_root, args.suffix)
        if args.json:
            print(plan.to_json())
        else:
            for entry in plan.entries:
                print("{}\t{}\t{}".format(entry.action, entry.name, entry.reason))
        if args.dry_run:
            return 2 if any(entry.action in ("conflict", "error") for entry in plan.entries) else 0
        apply_links(plan)
        return 0
    except (InstallError, ValueError) as error:
        print("installer error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
