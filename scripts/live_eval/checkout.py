"""Materialize and verify an exact Git-object skill checkout for live evals."""

import ctypes
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple
import unicodedata

from scripts.workflow_coordination.canonical_json import (
    canonical_bytes,
    load_canonical_input,
)


_EXPECTED_SKILLS = (
    "adversarial-review-loop",
    "workflow",
    "workflow-intake",
)
_MANIFEST_NAME = ".live-eval-checkout.json"
_MANIFEST_FIELDS = {
    "materialized_hashes",
    "object_format",
    "plugin_blob_oid",
    "plugin_manifest_hash",
    "skill_hashes",
    "skill_names",
    "tree_hash",
}
_REGULAR_MODES = {"100644", "100755"}
_OID_LENGTHS = {"sha1": 40, "sha256": 64}
_GIT_CONFIG_ARGUMENTS = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "submodule.recurse=false",
)
_PathToken = Tuple[int, int, int]


@dataclass(frozen=True)
class CheckoutManifest:
    object_format: str
    tree_hash: str
    plugin_blob_oid: str
    plugin_manifest_hash: str
    skill_hashes: Mapping[str, str]
    materialized_hashes: Mapping[str, str]
    skill_names: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_hashes", MappingProxyType(dict(self.skill_hashes)))
        object.__setattr__(
            self,
            "materialized_hashes",
            MappingProxyType(dict(self.materialized_hashes)),
        )
        object.__setattr__(self, "skill_names", tuple(self.skill_names))


@dataclass(frozen=True)
class PreflightResult:
    classification: str
    result: str
    reason: str
    manifest: Optional[CheckoutManifest] = None


@dataclass(frozen=True)
class _GitEntry:
    path: str
    mode: str
    oid: str


@dataclass(frozen=True)
class _CheckoutSnapshot:
    manifest: CheckoutManifest
    entries: Mapping[str, Tuple[_GitEntry, ...]]
    blobs: Mapping[str, bytes]


def canonical_name_key(name: str) -> str:
    """Return the NFC/casefold collision key for an untrusted path name."""
    if not isinstance(name, str) or not name:
        raise ValueError("checkout names must be non-empty strings")
    return unicodedata.normalize("NFC", name).casefold()


def require_unique_canonical_names(names: Iterable[str]) -> Tuple[str, ...]:
    """Require NFC names with no exact, case, or Unicode-normalized aliases."""
    if isinstance(names, (str, bytes)):
        raise ValueError("checkout names must be an iterable of strings")
    result = tuple(names)
    seen = set()
    for name in result:
        key = canonical_name_key(name)
        if not unicodedata.is_normalized("NFC", name):
            raise ValueError("checkout names must use Unicode NFC")
        if key in seen:
            raise ValueError("duplicate or canonical-aliased checkout name")
        seen.add(key)
    return result


def install_checkout_skills(repo: Path, codex_home: Path) -> CheckoutManifest:
    """Materialize exactly three skills from clean HEAD Git objects."""
    root = _plain_directory(repo, "repository")
    home = _private_empty_home(codex_home)
    snapshot = _checkout_snapshot(root, require_clean=True)
    staged = Path(tempfile.mkdtemp(prefix=".skills-stage-", dir=str(home)))
    staged_token = _required_path_token(staged)
    published = home / "skills"
    manifest_path = home / _MANIFEST_NAME
    staging_published = False
    published_token = None
    manifest_token = None
    try:
        staged.chmod(0o700)
        _materialize_snapshot(staged, snapshot)
        _verify_materialized(staged, snapshot, directory_mode=0o700)
        _fsync_tree(staged)
        _require_install_identity(root, snapshot.manifest)
        _publish_directory_noreplace(staged, published)
        staging_published = True
        published_token = staged_token
        _require_matching_path_token(published, published_token)
        _make_directories_read_only(published)
        _verify_materialized(published, snapshot)
        manifest_token = _write_exclusive(
            manifest_path, _manifest_bytes(snapshot.manifest), 0o400
        )
        _fsync_directory(home)
    except Exception as error:
        warnings = []
        failed_path = getattr(error, "owned_path", None)
        if manifest_token is None and failed_path == manifest_path:
            manifest_token = getattr(error, "owned_path_token", None)
        if not staging_published:
            _cleanup_owned_tree(staged, staged_token, warnings)
        _cleanup_owned_tree(published, published_token, warnings)
        _cleanup_owned_file(manifest_path, manifest_token, warnings)
        _attach_cleanup_warnings(error, warnings)
        raise
    return snapshot.manifest


def verify_loaded_checkout(repo: Path, codex_home: Path) -> PreflightResult:
    """Fail closed unless the read-only materialization matches current HEAD."""
    return _verify_loaded_checkout_inventory(
        repo, codex_home, frozenset({"skills", _MANIFEST_NAME})
    )


def _verify_loaded_checkout_inventory(
    repo: Path, codex_home: Path, expected_home_entries: frozenset
) -> PreflightResult:
    """Verify a checkout within an internally supplied exact home inventory."""
    try:
        root = _plain_directory(repo, "repository")
        home = _private_home(codex_home)
        _require_exact_names(home, set(expected_home_entries), "CODEX_HOME")
        manifest_path = home / _MANIFEST_NAME
        if stat.S_IMODE(manifest_path.lstat().st_mode) != 0o400:
            raise ValueError("checkout manifest must remain read-only")
        recorded = _read_manifest(manifest_path)
        current = _checkout_snapshot(root, require_clean=False)
        if recorded != current.manifest:
            raise ValueError("HEAD Git-object snapshot no longer matches manifest")
        _verify_materialized(home / "skills", current)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return PreflightResult(
            classification="blocked_isolation",
            result="blocked",
            reason=str(error) or error.__class__.__name__,
        )
    return PreflightResult(
        classification="ready",
        result="pass",
        reason="exact HEAD Git-object materialization verified",
        manifest=recorded,
    )


def _checkout_snapshot(repo: Path, require_clean: bool) -> _CheckoutSnapshot:
    _require_git_root(repo)
    if require_clean and _git_text(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=all",
    ):
        raise ValueError("repository checkout must be clean")
    object_format = _git_text(repo, "rev-parse", "--show-object-format")
    if object_format not in _OID_LENGTHS:
        raise ValueError("unsupported Git object format")
    tree_oid = _git_text(repo, "rev-parse", "--verify", "HEAD^{tree}")
    _require_oid(tree_oid, object_format)

    plugin_entries = _ls_tree(repo, tree_oid, ".codex-plugin/plugin.json", recursive=False)
    if len(plugin_entries) != 1 or plugin_entries[0].path != ".codex-plugin/plugin.json":
        raise ValueError("plugin manifest must be one exact HEAD blob")
    plugin_entry = plugin_entries[0]
    plugin_blob = _cat_blob(repo, plugin_entry.oid)
    plugin_value = load_canonical_input(plugin_blob)

    entries: Dict[str, Tuple[_GitEntry, ...]] = {}
    blobs: Dict[str, bytes] = {plugin_entry.oid: plugin_blob}
    skill_hashes = {}
    materialized_hashes = {}
    for name in _EXPECTED_SKILLS:
        prefix = "skills/{}/".format(name)
        skill_entries = _ls_tree(repo, tree_oid, "skills/{}".format(name), recursive=True)
        if not skill_entries or any(not item.path.startswith(prefix) for item in skill_entries):
            raise ValueError("expected skill is missing from HEAD: {}".format(name))
        require_unique_canonical_names(item.path for item in skill_entries)
        if not any(item.path == prefix + "SKILL.md" for item in skill_entries):
            raise ValueError("expected skill has no tracked SKILL.md: {}".format(name))
        entries[name] = skill_entries
        for item in skill_entries:
            if item.oid not in blobs:
                blobs[item.oid] = _cat_blob(repo, item.oid)
        skill_hashes[name] = _git_entry_hash(skill_entries, object_format)
        materialized_hashes[name] = _content_hash(name, skill_entries, blobs)

    if _git_text(repo, "rev-parse", "--verify", "HEAD^{tree}") != tree_oid:
        raise ValueError("HEAD changed while capturing checkout snapshot")
    if require_clean and _git_text(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=all",
    ):
        raise ValueError("repository checkout changed while capturing snapshot")

    manifest = CheckoutManifest(
        object_format=object_format,
        tree_hash=_domain_oid(object_format, tree_oid),
        plugin_blob_oid=_domain_oid(object_format, plugin_entry.oid),
        plugin_manifest_hash=_sha256(canonical_bytes(plugin_value)),
        skill_hashes=skill_hashes,
        materialized_hashes=materialized_hashes,
        skill_names=_EXPECTED_SKILLS,
    )
    return _CheckoutSnapshot(
        manifest=manifest,
        entries=MappingProxyType(entries),
        blobs=MappingProxyType(blobs),
    )


def _ls_tree(
    repo: Path, tree_oid: str, pathspec: str, recursive: bool
) -> Tuple[_GitEntry, ...]:
    arguments = ["ls-tree", "-z"]
    if recursive:
        arguments.append("-r")
    arguments.extend((tree_oid, "--", pathspec))
    output = _git_bytes(repo, *arguments)
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, object_type, oid_bytes = metadata.split(b" ", 2)
            path = path_bytes.decode("utf-8", errors="strict")
            mode = mode_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("malformed or non-UTF-8 Git tree entry") from error
        if object_type != b"blob" or mode not in _REGULAR_MODES:
            raise ValueError("unsupported Git tree entry mode: {}".format(mode))
        entries.append(_GitEntry(path=path, mode=mode, oid=oid))
    entries.sort(key=lambda item: item.path.encode("utf-8"))
    require_unique_canonical_names(item.path for item in entries)
    return tuple(entries)


def _cat_blob(repo: Path, oid: str) -> bytes:
    return _git_bytes(repo, "cat-file", "blob", oid)


def _materialize_snapshot(staged: Path, snapshot: _CheckoutSnapshot) -> None:
    for name in snapshot.manifest.skill_names:
        skill = staged / name
        skill.mkdir(mode=0o700)
        prefix = "skills/{}/".format(name)
        for entry in snapshot.entries[name]:
            relative = entry.path[len(prefix) :]
            parts = relative.split("/")
            for part in parts:
                if not unicodedata.is_normalized("NFC", part):
                    raise ValueError("checkout path components must use Unicode NFC")
            parent = skill
            for part in parts[:-1]:
                parent = parent / part
                if not parent.exists():
                    parent.mkdir(mode=0o700)
            mode = 0o555 if entry.mode == "100755" else 0o444
            _write_exclusive(parent / parts[-1], snapshot.blobs[entry.oid], mode)


def _verify_materialized(
    root: Path, snapshot: _CheckoutSnapshot, directory_mode: int = 0o555
) -> None:
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != directory_mode
    ):
        raise ValueError("materialized skills root must be a read-only directory")
    _require_exact_names(root, set(snapshot.manifest.skill_names), "materialized skills")
    for name in snapshot.manifest.skill_names:
        actual_hash = _materialized_skill_hash(
            root / name, name, snapshot.entries[name], directory_mode
        )
        if actual_hash != snapshot.manifest.materialized_hashes[name]:
            raise ValueError("materialized skill content hash mismatch: {}".format(name))


def _materialized_skill_hash(
    skill: Path, name: str, entries: Sequence[_GitEntry], directory_mode: int
) -> str:
    if (
        not stat.S_ISDIR(skill.lstat().st_mode)
        or stat.S_IMODE(skill.lstat().st_mode) != directory_mode
    ):
        raise ValueError("materialized skill directory mode is invalid: {}".format(name))
    prefix = "skills/{}/".format(name)
    expected_files = {item.path[len(prefix) :]: item for item in entries}
    expected_directories = set()
    for relative in expected_files:
        parts = relative.split("/")
        for index in range(1, len(parts)):
            expected_directories.add("/".join(parts[:index]))

    actual_files = set()
    actual_directories = set()
    _collect_materialized(
        skill, skill, actual_files, actual_directories, directory_mode
    )
    if actual_files != set(expected_files) or actual_directories != expected_directories:
        raise ValueError("materialized skill inventory is not exact: {}".format(name))

    digest = hashlib.sha256()
    for relative in sorted(expected_files, key=lambda value: value.encode("utf-8")):
        entry = expected_files[relative]
        path = skill / relative
        metadata = path.lstat()
        expected_mode = 0o555 if entry.mode == "100755" else 0o444
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != expected_mode:
            raise ValueError("materialized file type or mode changed: {}".format(relative))
        _hash_record(digest, relative, expected_mode, _read_regular_file(path))
    return "sha256:" + digest.hexdigest()


def _collect_materialized(
    directory: Path,
    root: Path,
    files: set,
    directories: set,
    directory_mode: int,
) -> None:
    entries = list(os.scandir(str(directory)))
    require_unique_canonical_names(entry.name for entry in entries)
    for entry in entries:
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix()
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != directory_mode:
                raise ValueError("materialized directory mode changed: {}".format(relative))
            directories.add(relative)
            _collect_materialized(path, root, files, directories, directory_mode)
        elif stat.S_ISREG(metadata.st_mode):
            files.add(relative)
        else:
            raise ValueError("materialized checkout contains a link or special file")


def _git_entry_hash(entries: Sequence[_GitEntry], object_format: str) -> str:
    return _sha256(
        canonical_bytes(
            [
                {
                    "mode": item.mode,
                    "oid": _domain_oid(object_format, item.oid),
                    "path": item.path,
                }
                for item in entries
            ]
        )
    )


def _content_hash(
    name: str, entries: Sequence[_GitEntry], blobs: Mapping[str, bytes]
) -> str:
    prefix = "skills/{}/".format(name)
    digest = hashlib.sha256()
    for entry in entries:
        relative = entry.path[len(prefix) :]
        mode = 0o555 if entry.mode == "100755" else 0o444
        _hash_record(digest, relative, mode, blobs[entry.oid])
    return "sha256:" + digest.hexdigest()


def _hash_record(digest: "hashlib._Hash", path: str, mode: int, content: bytes) -> None:
    for value in (path.encode("utf-8"), oct(mode).encode("ascii"), content):
        digest.update(len(value).to_bytes(8, byteorder="big"))
        digest.update(value)


def _manifest_bytes(manifest: CheckoutManifest) -> bytes:
    return canonical_bytes(
        {
            "materialized_hashes": dict(manifest.materialized_hashes),
            "object_format": manifest.object_format,
            "plugin_blob_oid": manifest.plugin_blob_oid,
            "plugin_manifest_hash": manifest.plugin_manifest_hash,
            "skill_hashes": dict(manifest.skill_hashes),
            "skill_names": list(manifest.skill_names),
            "tree_hash": manifest.tree_hash,
        }
    )


def _read_manifest(path: Path) -> CheckoutManifest:
    value = load_canonical_input(_read_regular_file(path))
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise ValueError("checkout manifest has invalid fields")
    object_format = value["object_format"]
    if object_format not in _OID_LENGTHS:
        raise ValueError("checkout manifest has invalid object format")
    names = value["skill_names"]
    if names != list(_EXPECTED_SKILLS):
        raise ValueError("checkout manifest has invalid skill inventory")
    skill_hashes = _validated_hash_mapping(value["skill_hashes"])
    materialized_hashes = _validated_hash_mapping(value["materialized_hashes"])
    tree_hash = value["tree_hash"]
    plugin_blob_oid = value["plugin_blob_oid"]
    if not _is_domain_oid(tree_hash, object_format) or not _is_domain_oid(
        plugin_blob_oid, object_format
    ):
        raise ValueError("checkout manifest has invalid Git object identity")
    plugin_hash = value["plugin_manifest_hash"]
    if not _is_sha256(plugin_hash):
        raise ValueError("checkout manifest has invalid plugin hash")
    return CheckoutManifest(
        object_format=object_format,
        tree_hash=tree_hash,
        plugin_blob_oid=plugin_blob_oid,
        plugin_manifest_hash=plugin_hash,
        skill_hashes=skill_hashes,
        materialized_hashes=materialized_hashes,
        skill_names=tuple(names),
    )


def _validated_hash_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or tuple(sorted(value)) != _EXPECTED_SKILLS:
        raise ValueError("checkout manifest has invalid skill hash mapping")
    if not all(_is_sha256(item) for item in value.values()):
        raise ValueError("checkout manifest has invalid skill hashes")
    return value


def _write_exclusive(
    path: Path, content: bytes, final_mode: int
) -> _PathToken:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    token = _stat_token(os.fstat(descriptor))
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written < 1:
                raise OSError("short write while materializing checkout")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, final_mode)
    except Exception as error:
        _annotate_owned_path(error, path, token)
        raise
    finally:
        os.close(descriptor)
    try:
        _require_matching_path_token(path, token)
    except Exception as error:
        _annotate_owned_path(error, path, token)
        raise
    return token


def _read_regular_file(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("expected a regular file: {}".format(path))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
        ):
            raise ValueError("file identity changed while hashing: {}".format(path))
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or after.st_nlink != 1
    ):
        raise ValueError("file changed while hashing: {}".format(path))
    return b"".join(chunks)


def _make_directories_read_only(root: Path) -> None:
    directories = [root]
    for current, names, _files in os.walk(str(root)):
        directories.extend(Path(current) / name for name in names)
    for directory in reversed(directories):
        directory.chmod(0o555)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for current, names, _files in os.walk(str(root)):
        directories.extend(Path(current) / name for name in names)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_directory_noreplace(staged: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(str(staged))
    target_bytes = os.fsencode(str(target))
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as error:
            raise ValueError("atomic no-replace publish is unavailable") from error
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
            raise ValueError("atomic no-replace publish is unavailable") from error
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 0x00000004)
    else:
        raise ValueError("atomic no-replace publish is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(error_number, os.strerror(error_number), str(target))
    if error_number in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
        raise ValueError("atomic no-replace publish is unavailable")
    raise OSError(error_number, os.strerror(error_number), str(target))


def _remove_owned_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    for current, names, files in os.walk(str(path), topdown=False):
        directory = Path(current)
        directory.chmod(0o700)
        for name in files:
            file_path = directory / name
            if file_path.is_symlink():
                file_path.unlink()
            else:
                file_path.chmod(0o600)
                file_path.unlink()
        for name in names:
            child = directory / name
            if child.is_symlink():
                child.unlink()
            else:
                child.chmod(0o700)
                child.rmdir()
    path.rmdir()


def _cleanup_owned_tree(
    path: Path,
    token: Optional[_PathToken],
    warnings: list,
) -> None:
    if token is None:
        return
    if not _cleanup_token_matches(path, token, warnings):
        return
    _remove_owned_tree(path)


def _cleanup_owned_file(
    path: Path,
    token: Optional[_PathToken],
    warnings: list,
) -> None:
    if token is None:
        return
    if not _cleanup_token_matches(path, token, warnings):
        return
    path.unlink()


def _cleanup_token_matches(
    path: Path, token: _PathToken, warnings: list
) -> bool:
    current = _path_token(path)
    if current == token:
        return True
    state = "missing" if current is None else "replaced"
    warnings.append("cleanup skipped for {}: pathname {}".format(path, state))
    return False


def _attach_cleanup_warnings(error: Exception, warnings: Sequence[str]) -> None:
    if not warnings:
        return
    existing = tuple(getattr(error, "cleanup_warnings", ()))
    combined = existing + tuple(warnings)
    try:
        setattr(error, "cleanup_warnings", combined)
    except (AttributeError, TypeError):
        pass
    add_note = getattr(error, "add_note", None)
    if add_note is not None:
        for warning in warnings:
            add_note(warning)


def _annotate_owned_path(
    error: Exception, path: Path, token: _PathToken
) -> None:
    try:
        setattr(error, "owned_path", path)
        setattr(error, "owned_path_token", token)
    except (AttributeError, TypeError):
        pass


def _stat_token(metadata: os.stat_result) -> _PathToken:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _path_token(path: Path) -> Optional[_PathToken]:
    try:
        return _stat_token(path.lstat())
    except FileNotFoundError:
        return None


def _required_path_token(path: Path) -> _PathToken:
    token = _path_token(path)
    if token is None:
        raise ValueError("owned path disappeared after creation: {}".format(path))
    return token


def _require_matching_path_token(path: Path, token: _PathToken) -> None:
    if _path_token(path) != token:
        raise ValueError("owned pathname identity changed: {}".format(path))


def _private_empty_home(path: Path) -> Path:
    home = _private_home(path)
    if any(home.iterdir()):
        raise ValueError("CODEX_HOME must be empty before checkout installation")
    return home


def _require_install_identity(repo: Path, manifest: CheckoutManifest) -> None:
    tree_oid = _git_text(repo, "rev-parse", "--verify", "HEAD^{tree}")
    if _domain_oid(manifest.object_format, tree_oid) != manifest.tree_hash:
        raise ValueError("HEAD changed before checkout publication")
    if _git_text(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=all",
    ):
        raise ValueError("repository checkout changed before publication")


def _private_home(path: Path) -> Path:
    home = _plain_directory(path, "CODEX_HOME")
    if stat.S_IMODE(home.stat().st_mode) != 0o700:
        raise ValueError("CODEX_HOME must have mode 0700")
    return home


def _plain_directory(path: Path, label: str) -> Path:
    value = Path(path).absolute()
    _reject_symlink_components(value, label)
    try:
        metadata = value.lstat()
    except OSError as error:
        raise ValueError("{} must be an existing directory".format(label)) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("{} must be a plain directory".format(label))
    return value


def _reject_symlink_components(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError("{} path must not contain symlinks".format(label))
        except FileNotFoundError:
            break


def _require_git_root(repo: Path) -> None:
    top = _git_text(repo, "rev-parse", "--show-toplevel")
    if Path(os.path.normpath(os.path.abspath(top))) != repo:
        raise ValueError("repository path must be the exact Git checkout root")


def _git_text(repo: Path, *arguments: str) -> str:
    return _git_bytes(repo, *arguments).decode("utf-8", errors="strict").strip()


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    result = subprocess.run(
        ("git",) + _GIT_CONFIG_ARGUMENTS + arguments,
        cwd=str(repo),
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError(
            "Git object lookup failed: {}".format(
                result.stderr.decode("utf-8", errors="replace").strip()
            )
        )
    return result.stdout


def _require_exact_names(directory: Path, expected: set, label: str) -> None:
    names = [entry.name for entry in os.scandir(str(directory))]
    require_unique_canonical_names(names)
    if set(names) != expected:
        raise ValueError("{} inventory is not exact".format(label))


def _require_oid(oid: str, object_format: str) -> None:
    expected_length = _OID_LENGTHS[object_format]
    if len(oid) != expected_length or any(
        character not in "0123456789abcdef" for character in oid
    ):
        raise ValueError("invalid {} Git object ID".format(object_format))


def _domain_oid(object_format: str, oid: str) -> str:
    _require_oid(oid, object_format)
    return "{}:{}".format(object_format, oid)


def _is_domain_oid(value: object, object_format: str) -> bool:
    if not isinstance(value, str) or not value.startswith(object_format + ":"):
        return False
    try:
        _require_oid(value[len(object_format) + 1 :], object_format)
    except ValueError:
        return False
    return True


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
