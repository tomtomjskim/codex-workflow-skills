"""Authoritative, fail-closed Git worktree change collection."""

import os
import re
import subprocess
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Tuple


class GitStateError(ValueError):
    """Raised when Git state cannot be collected without ambiguity."""


_TREE_HASH = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_STATUS_CODES = frozenset(b" MADUT?!RC")


def _git_environment() -> Dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _run_git(repo_root: Path, arguments: Tuple[str, ...]) -> bytes:
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.ignoreStat=false",
        *arguments,
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=_git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitStateError("Git command failed: {}".format(message or "unknown error"))
    return completed.stdout


def _head_tree_hash(repo_root: Path) -> str:
    raw = _run_git(repo_root, ("rev-parse", "--verify", "HEAD^{tree}"))
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise GitStateError("Git tree hash is not ASCII") from error
    if _TREE_HASH.fullmatch(value) is None:
        raise GitStateError("Git tree hash is invalid")
    return value


def _safe_reported_path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitStateError("Git path must use UTF-8") from error
    if not value or not unicodedata.is_normalized("NFC", value) or "\\" in value:
        raise GitStateError("unsafe Git path: {!r}".format(value))
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or str(posix_path) in ("", ".")
    ):
        raise GitStateError("unsafe Git path: {!r}".format(value))
    return str(posix_path)


def _parse_porcelain_v1_z(payload: bytes) -> Tuple[str, ...]:
    if not payload:
        return ()
    if not payload.endswith(b"\0"):
        raise GitStateError("Git status output is not NUL terminated")
    paths = set()
    for record in payload[:-1].split(b"\0"):
        if len(record) < 4 or record[2:3] != b" ":
            raise GitStateError("Git status record is malformed")
        status = record[:2]
        if any(code not in _STATUS_CODES for code in status):
            raise GitStateError("Git status code is unsupported")
        if b"R" in status or b"C" in status:
            raise GitStateError("Git rename detection was not disabled")
        paths.add(_safe_reported_path(record[3:]))
    return tuple(sorted(paths))


def collect_changed_paths(repo_root: Path, expected_tree_hash: str) -> Tuple[str, ...]:
    """Collect tracked and untracked paths relative to a stable HEAD tree."""
    if not isinstance(expected_tree_hash, str) or _TREE_HASH.fullmatch(
        expected_tree_hash
    ) is None:
        raise GitStateError("expected checkout tree hash is invalid")
    root = Path(repo_root).resolve(strict=True)
    before = _head_tree_hash(root)
    if before != expected_tree_hash:
        raise GitStateError("checkout tree hash does not match receipt base tree")
    status = _run_git(
        root,
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "--no-renames",
        ),
    )
    after = _head_tree_hash(root)
    if after != before:
        raise GitStateError("checkout tree changed during collection")
    return _parse_porcelain_v1_z(status)


def require_clean_base(repo_root: Path, expected_tree_hash: str) -> None:
    """Require dispatch to start from the exact clean tree recorded by receipt."""
    changed_paths = collect_changed_paths(repo_root, expected_tree_hash)
    if changed_paths:
        raise GitStateError(
            "base worktree must be clean before dispatch: {}".format(
                ", ".join(changed_paths)
            )
        )
