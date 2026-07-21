"""Install and verify an exact skill checkout for isolated live evaluations."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

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
    "plugin_manifest_hash",
    "skill_hashes",
    "skill_names",
    "tree_hash",
}


@dataclass(frozen=True)
class CheckoutManifest:
    tree_hash: str
    plugin_manifest_hash: str
    skill_hashes: Mapping[str, str]
    skill_names: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_hashes", MappingProxyType(dict(self.skill_hashes)))
        object.__setattr__(self, "skill_names", tuple(self.skill_names))


@dataclass(frozen=True)
class PreflightResult:
    classification: str
    result: str
    reason: str
    manifest: Optional[CheckoutManifest] = None


def install_checkout_skills(repo: Path, codex_home: Path) -> CheckoutManifest:
    """Link exactly the live-eval skills into an empty isolated Codex home."""
    root = _plain_directory(repo, "repository")
    home = _plain_directory(codex_home, "CODEX_HOME")
    if any(home.iterdir()):
        raise ValueError("CODEX_HOME must be empty before checkout installation")

    manifest = _checkout_snapshot(root)
    skills_home = home / "skills"
    manifest_path = home / _MANIFEST_NAME
    skills_home.mkdir(mode=0o700)
    try:
        for name in manifest.skill_names:
            source = root / "skills" / name
            (skills_home / name).symlink_to(source, target_is_directory=True)
        _write_exclusive(manifest_path, _manifest_bytes(manifest))
    except Exception:
        if manifest_path.exists() or manifest_path.is_symlink():
            manifest_path.unlink()
        for name in manifest.skill_names:
            target = skills_home / name
            if target.exists() or target.is_symlink():
                target.unlink()
        skills_home.rmdir()
        raise
    return manifest


def verify_loaded_checkout(repo: Path, codex_home: Path) -> PreflightResult:
    """Fail closed unless the installed skills still match the exact checkout."""
    try:
        root = _plain_directory(repo, "repository")
        home = _plain_directory(codex_home, "CODEX_HOME")
        _require_exact_names(home, {"skills", _MANIFEST_NAME}, "CODEX_HOME")
        recorded = _read_manifest(home / _MANIFEST_NAME)
        current = _checkout_snapshot(root)
        if recorded != current:
            raise ValueError("checkout content no longer matches installed manifest")
        _verify_installed_links(root, home, recorded)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        return PreflightResult(
            classification="blocked_isolation",
            result="blocked",
            reason=str(error) or error.__class__.__name__,
        )
    return PreflightResult(
        classification="ready",
        result="pass",
        reason="exact checkout skills verified",
        manifest=recorded,
    )


def _checkout_snapshot(repo: Path) -> CheckoutManifest:
    _require_git_root(repo)
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("repository checkout must be clean")
    tree_hash = _git(repo, "rev-parse", "--verify", "HEAD^{tree}")
    if len(tree_hash) != 40 or any(
        character not in "0123456789abcdef" for character in tree_hash
    ):
        raise ValueError("repository checkout has an invalid Git tree identity")

    plugin_path = repo / ".codex-plugin" / "plugin.json"
    plugin_value = load_canonical_input(_read_regular_file(plugin_path))
    plugin_hash = _sha256(canonical_bytes(plugin_value))
    skill_hashes = {}
    for name in _EXPECTED_SKILLS:
        skill_path = repo / "skills" / name
        _require_tracked_inventory(repo, skill_path)
        skill_hashes[name] = _hash_tree(skill_path, repo)
    return CheckoutManifest(
        tree_hash=tree_hash,
        plugin_manifest_hash=plugin_hash,
        skill_hashes=skill_hashes,
        skill_names=_EXPECTED_SKILLS,
    )


def _verify_installed_links(
    repo: Path, home: Path, manifest: CheckoutManifest
) -> None:
    skills_home = home / "skills"
    metadata = skills_home.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("installed skills path must be a plain directory")
    _require_exact_names(skills_home, set(manifest.skill_names), "installed skills")
    for name in manifest.skill_names:
        link = skills_home / name
        if not stat.S_ISLNK(link.lstat().st_mode):
            raise ValueError(
                "installed skill must remain a symbolic link: {}".format(name)
            )
        target_text = os.readlink(str(link))
        target = Path(target_text)
        if not target.is_absolute():
            target = link.parent / target
        expected = repo / "skills" / name
        if _normalized_absolute(target) != expected:
            raise ValueError(
                "installed skill link was broken or retargeted: {}".format(name)
            )
        if not expected.is_dir():
            raise ValueError("installed skill link is broken: {}".format(name))


def _hash_tree(root: Path, repo: Path) -> str:
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("skill root must be a plain directory: {}".format(root.name))
    digest = hashlib.sha256()
    _hash_directory(digest, root, root, repo)
    return "sha256:" + digest.hexdigest()


def _hash_directory(
    digest: "hashlib._Hash", directory: Path, root: Path, repo: Path
) -> None:
    entries = sorted(
        os.scandir(str(directory)), key=lambda item: item.name.encode("utf-8")
    )
    folded = set()
    for entry in entries:
        folded_name = entry.name.casefold()
        if folded_name in folded:
            raise ValueError("duplicate or case-aliased checkout path")
        folded.add(folded_name)
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            _hash_record(digest, b"directory", relative, b"")
            _hash_directory(digest, path, root, repo)
        elif stat.S_ISREG(metadata.st_mode):
            executable = b"1" if metadata.st_mode & 0o111 else b"0"
            _hash_record(
                digest, b"file" + executable, relative, _read_regular_file(path)
            )
        elif stat.S_ISLNK(metadata.st_mode):
            target_text = os.readlink(str(path))
            target_bytes = target_text.encode("utf-8", errors="strict")
            target = Path(target_text)
            if not target.is_absolute():
                target = path.parent / target
            normalized = _normalized_absolute(target)
            try:
                resolved = normalized.resolve(strict=True)
            except OSError as error:
                raise ValueError("skill symlink target must exist") from error
            if not _is_within(normalized, repo) or not _is_within(resolved, repo):
                raise ValueError(
                    "skill symlink must target an existing path inside repository"
                )
            _hash_record(digest, b"symlink", relative, target_bytes)
        else:
            raise ValueError(
                "unsupported checkout file type: {}".format(relative.decode("utf-8"))
            )


def _hash_record(
    digest: "hashlib._Hash", kind: bytes, path: bytes, content: bytes
) -> None:
    for value in (kind, path, content):
        digest.update(len(value).to_bytes(8, byteorder="big"))
        digest.update(value)


def _read_regular_file(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("expected a regular file: {}".format(path))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
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
    ):
        raise ValueError("file changed while hashing: {}".format(path))
    return b"".join(chunks)


def _manifest_bytes(manifest: CheckoutManifest) -> bytes:
    return canonical_bytes(
        {
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
    names = value["skill_names"]
    hashes = value["skill_hashes"]
    if names != list(_EXPECTED_SKILLS) or not isinstance(hashes, dict):
        raise ValueError("checkout manifest has invalid skill inventory")
    if tuple(sorted(hashes)) != _EXPECTED_SKILLS:
        raise ValueError("checkout manifest has invalid skill hashes")
    values = (value["plugin_manifest_hash"],) + tuple(
        hashes[name] for name in _EXPECTED_SKILLS
    )
    if not all(_is_sha256(item) for item in values):
        raise ValueError("checkout manifest has invalid content hashes")
    tree_hash = value["tree_hash"]
    if not isinstance(tree_hash, str) or len(tree_hash) != 40 or any(
        character not in "0123456789abcdef" for character in tree_hash
    ):
        raise ValueError("checkout manifest has invalid Git tree hash")
    return CheckoutManifest(
        tree_hash=tree_hash,
        plugin_manifest_hash=value["plugin_manifest_hash"],
        skill_hashes=hashes,
        skill_names=tuple(names),
    )


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    top = _git(repo, "rev-parse", "--show-toplevel")
    if _normalized_absolute(Path(top)) != repo:
        raise ValueError("repository path must be the exact Git checkout root")


def _require_tracked_inventory(repo: Path, skill: Path) -> None:
    prefix = skill.relative_to(repo).as_posix()
    tracked_output = _git(repo, "ls-files", "-z", "--", prefix)
    tracked = {item for item in tracked_output.split("\0") if item}
    actual = _filesystem_leaf_paths(skill, repo)
    if tracked != actual:
        raise ValueError("skill content must exactly match tracked Git inventory")


def _filesystem_leaf_paths(directory: Path, repo: Path) -> set:
    paths = set()
    for entry in os.scandir(str(directory)):
        path = Path(entry.path)
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            paths.update(_filesystem_leaf_paths(path, repo))
        elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            paths.add(path.relative_to(repo).as_posix())
        else:
            raise ValueError("unsupported checkout file type")
    return paths


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git",) + arguments,
        cwd=str(repo),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise ValueError("Git checkout identity failed: {}".format(result.stderr.strip()))
    return result.stdout.strip()


def _require_exact_names(directory: Path, expected: set, label: str) -> None:
    names = [entry.name for entry in os.scandir(str(directory))]
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)) or set(names) != expected:
        raise ValueError("{} inventory is not exact".format(label))


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(str(path))))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
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
