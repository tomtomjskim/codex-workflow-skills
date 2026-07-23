"""Safely materialize a fixed current-or-lean live-eval harness bundle."""

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

try:
    import tomllib as _tomllib
except ImportError:  # pragma: no cover - exercised by the Python 3.9 test runtime
    _tomllib = None

from scripts.live_eval.checkout import (
    CheckoutManifest,
    _verify_loaded_checkout_inventory,
    _write_exclusive,
    install_checkout_skills,
    require_unique_canonical_names,
)
from scripts.live_eval.isolation import seal_codex_home
from scripts.workflow_coordination.canonical_json import (
    canonical_bytes,
    load_canonical_input,
)


_PROFILES = ("current", "lean")
_EXPECTED_ROLES = (
    "accessibility-reviewer",
    "api-reviewer",
    "architect",
    "code-reviewer",
    "dba",
    "designer",
    "developer",
    "documenter",
    "explorer",
    "performance-reviewer",
    "pm",
    "publisher",
    "qa-engineer",
    "security-reviewer",
    "test-coverage-reviewer",
    "ux-reviewer",
)
_HOME_ENTRIES = frozenset(
    {".agents", ".live-eval-checkout.json", "AGENTS.md", "agents", "skills"}
)
_MAX_FILE_BYTES = 1024 * 1024
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024
_MAX_ROLES = 64
_SOURCE_FILE_MODE = 0o600
_SOURCE_DIRECTORY_MODE = 0o700
_READ_ONLY_FILE_MODE = 0o444
_AGENTS_FILE_MODE = 0o400
_READ_ONLY_DIRECTORY_MODE = 0o555
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_BACKTICK_PATH_PATTERN = re.compile(r"`([^`\r\n]*)`")
_REASONS = frozenset(
    {
        "fixed_inventory_verified",
        "invalid_bundle",
        "source_changed",
        "skill_checkout_mismatch",
        "materialized_harness_mismatch",
        "home_seal_mismatch",
        "cleanup_unverified",
    }
)
_StatToken = Tuple[int, int, int, int, int, int, int]


class HarnessError(ValueError):
    """A sanitized fail-closed harness error."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS or reason == "fixed_inventory_verified":
            reason = "invalid_bundle"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class HarnessSourceManifest:
    schema_version: int
    bundle_id: str
    bundle_digest: str
    profile: str
    agents_hash: str
    adapter_source_hash: str
    adapter_materialized_hash: str
    common_role_hash: str
    adapter_count: int
    role_count: int


@dataclass(frozen=True)
class HarnessManifest:
    bundle_id: str
    bundle_digest: str
    profile: str
    checkout: CheckoutManifest
    agents_hash: str
    skill_routing_hash: str
    adapter_source_hash: str
    adapter_materialized_hash: str
    common_role_hash: str
    home_digest: str
    adapter_count: int
    role_count: int


@dataclass(frozen=True)
class HarnessPreflightResult:
    classification: str
    result: str
    reason: str
    manifest: Optional[HarnessManifest] = None


@dataclass(frozen=True)
class _BundleInventory:
    names: Tuple[str, ...]
    tokens: Mapping[str, _StatToken]

    def __post_init__(self) -> None:
        object.__setattr__(self, "names", tuple(self.names))
        object.__setattr__(self, "tokens", MappingProxyType(dict(self.tokens)))


@dataclass(frozen=True)
class _SourceSnapshot:
    manifest: HarnessSourceManifest
    agents: bytes
    adapters: Mapping[str, bytes]
    materialized_adapters: Mapping[str, bytes]
    common_roles: Mapping[str, bytes]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))
        object.__setattr__(
            self,
            "materialized_adapters",
            MappingProxyType(dict(self.materialized_adapters)),
        )
        object.__setattr__(
            self, "common_roles", MappingProxyType(dict(self.common_roles))
        )


def load_harness_source(bundle_root: Path, profile: str) -> HarnessSourceManifest:
    """Validate and hash one profile from the fixed harness bundle inventory."""
    try:
        return _load_source_snapshot(bundle_root, profile).manifest
    except HarnessError:
        raise
    except (OSError, TypeError, ValueError):
        raise HarnessError("invalid_bundle") from None


def materialize_harness_home(
    skill_repo: Path, bundle_root: Path, profile: str, codex_home: Path
) -> HarnessManifest:
    """Install clean-HEAD skills and one validated harness into an empty home."""
    snapshot = _load_source_snapshot_sanitized(bundle_root, profile)
    home_path = Path(codex_home).absolute()
    try:
        parent_token = _stat_token(home_path.parent.lstat())
        home_identity = _identity_token(home_path.lstat())
    except OSError:
        raise HarnessError("materialized_harness_mismatch") from None
    try:
        checkout = install_checkout_skills(skill_repo, codex_home)
    except (OSError, TypeError, ValueError):
        raise HarnessError("skill_checkout_mismatch") from None

    home = Path(codex_home).absolute()
    try:
        agents_directory = home / "agents"
        common_root = home / ".agents"
        common_directory = common_root / "common-agents"
        agents_directory.mkdir(mode=_SOURCE_DIRECTORY_MODE)
        common_root.mkdir(mode=_SOURCE_DIRECTORY_MODE)
        common_directory.mkdir(mode=_SOURCE_DIRECTORY_MODE)
        _write_exclusive(home / "AGENTS.md", snapshot.agents, _AGENTS_FILE_MODE)
        for name, content in snapshot.materialized_adapters.items():
            _write_exclusive(
                agents_directory / "{}.toml".format(name),
                content,
                _READ_ONLY_FILE_MODE,
            )
        for name, content in snapshot.common_roles.items():
            _write_exclusive(
                common_directory / "{}.md".format(name),
                content,
                _READ_ONLY_FILE_MODE,
            )
        common_directory.chmod(_READ_ONLY_DIRECTORY_MODE)
        common_root.chmod(_READ_ONLY_DIRECTORY_MODE)
        agents_directory.chmod(_READ_ONLY_DIRECTORY_MODE)
        skill_routing_hash = _skill_routing_hash(checkout)
        provisional = HarnessManifest(
            bundle_id=snapshot.manifest.bundle_id,
            bundle_digest=snapshot.manifest.bundle_digest,
            profile=snapshot.manifest.profile,
            checkout=checkout,
            agents_hash=snapshot.manifest.agents_hash,
            skill_routing_hash=skill_routing_hash,
            adapter_source_hash=snapshot.manifest.adapter_source_hash,
            adapter_materialized_hash=snapshot.manifest.adapter_materialized_hash,
            common_role_hash=snapshot.manifest.common_role_hash,
            home_digest="sha256:" + ("0" * 64),
            adapter_count=snapshot.manifest.adapter_count,
            role_count=snapshot.manifest.role_count,
        )
        _verify_materialized_harness_tree(home, provisional)
        preseal_checkout = _verify_loaded_checkout_inventory(
            skill_repo, home, _HOME_ENTRIES
        )
        if (
            preseal_checkout.result != "pass"
            or preseal_checkout.manifest != checkout
        ):
            raise HarnessError("skill_checkout_mismatch")
        seal = seal_codex_home(home, tuple(sorted(_HOME_ENTRIES)))
        manifest = replace(provisional, home_digest=seal.content_digest)
    except HarnessError:
        raise
    except (OSError, TypeError, ValueError):
        raise HarnessError("materialized_harness_mismatch") from None

    result = verify_loaded_harness(skill_repo, home, manifest)
    if result.result != "pass":
        raise HarnessError(result.reason)
    try:
        if (
            _stat_token(home.parent.lstat()) != parent_token
            or _identity_token(home.lstat()) != home_identity
        ):
            raise HarnessError("materialized_harness_mismatch")
    except OSError:
        raise HarnessError("materialized_harness_mismatch") from None
    return manifest


def verify_loaded_harness(
    skill_repo: Path, codex_home: Path, expected: HarnessManifest
) -> HarnessPreflightResult:
    """Fail closed unless the fixed materialized harness still matches its manifest."""
    try:
        if not isinstance(expected, HarnessManifest):
            raise HarnessError("materialized_harness_mismatch")
        home = _private_home(codex_home)
        parent_token = _stat_token(home.parent.lstat())
        home_identity = _identity_token(home.lstat())
        _verify_materialized_harness_tree(home, expected)
        checkout = _verify_loaded_checkout_inventory(
            skill_repo, home, _HOME_ENTRIES
        )
        if checkout.result != "pass" or checkout.manifest != expected.checkout:
            raise HarnessError("skill_checkout_mismatch")
        if _skill_routing_hash(checkout.manifest) != expected.skill_routing_hash:
            raise HarnessError("skill_checkout_mismatch")
        seal = seal_codex_home(home, tuple(sorted(_HOME_ENTRIES)))
        if seal.content_digest != expected.home_digest:
            raise HarnessError("home_seal_mismatch")
        if (
            _stat_token(home.parent.lstat()) != parent_token
            or _identity_token(home.lstat()) != home_identity
        ):
            raise HarnessError("materialized_harness_mismatch")
    except HarnessError as error:
        return HarnessPreflightResult(
            classification="blocked_isolation",
            result="blocked",
            reason=error.reason,
        )
    except (OSError, TypeError, ValueError):
        return HarnessPreflightResult(
            classification="blocked_isolation",
            result="blocked",
            reason="materialized_harness_mismatch",
        )
    return HarnessPreflightResult(
        classification="ready",
        result="pass",
        reason="fixed_inventory_verified",
        manifest=expected,
    )


def _load_source_snapshot_sanitized(
    bundle_root: Path, profile: str
) -> _SourceSnapshot:
    try:
        return _load_source_snapshot(bundle_root, profile)
    except HarnessError:
        raise
    except (OSError, TypeError, ValueError):
        raise HarnessError("invalid_bundle") from None


def _load_source_snapshot(bundle_root: Path, profile: str) -> _SourceSnapshot:
    if profile not in _PROFILES:
        raise HarnessError("invalid_bundle")
    root = _private_bundle_root(bundle_root)
    initial = _capture_bundle_inventory(root)
    try:
        contents = {
            relative: _read_source_file(root / relative, initial.tokens[relative])
            for relative in initial.names
        }
    except HarnessError:
        raise
    except (KeyError, OSError, TypeError, ValueError):
        raise HarnessError("source_changed") from None

    try:
        final = _capture_bundle_inventory(root)
    except (OSError, TypeError, ValueError, HarnessError):
        raise HarnessError("source_changed") from None
    if initial != final:
        raise HarnessError("source_changed")

    try:
        metadata = load_canonical_input(contents["harness.json"])
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"bundle_id", "schema_version"}
            or metadata["schema_version"] != 1
            or isinstance(metadata["schema_version"], bool)
            or not isinstance(metadata["bundle_id"], str)
            or not _BUNDLE_ID_PATTERN.fullmatch(metadata["bundle_id"])
        ):
            raise ValueError("invalid metadata")
        adapter_names = tuple(
            relative[len("shared/agents/") : -len(".toml")]
            for relative in initial.names
            if relative.startswith("shared/agents/")
        )
        adapters = {
            name: contents["shared/agents/{}.toml".format(name)]
            for name in adapter_names
        }
        common_roles = {
            name: contents["shared/common-agents/{}.md".format(name)]
            for name in adapter_names
        }
        materialized_adapters = {
            name: _rewrite_adapter(name, content)
            for name, content in adapters.items()
        }
        agents = contents["profiles/{}/AGENTS.md".format(profile)]
        if _sha256(contents["profiles/current/AGENTS.md"]) == _sha256(
            contents["profiles/lean/AGENTS.md"]
        ):
            raise ValueError("profiles must differ")
        manifest = HarnessSourceManifest(
            schema_version=1,
            bundle_id=metadata["bundle_id"],
            bundle_digest=_bundle_digest(contents),
            profile=profile,
            agents_hash=_sha256(agents),
            adapter_source_hash=_inventory_hash("adapter-source-v1", adapters, ".toml"),
            adapter_materialized_hash=_inventory_hash(
                "adapter-materialized-v1", materialized_adapters, ".toml"
            ),
            common_role_hash=_inventory_hash(
                "common-role-v1", common_roles, ".md"
            ),
            adapter_count=len(adapters),
            role_count=len(common_roles),
        )
    except HarnessError:
        raise
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise HarnessError("invalid_bundle") from None
    return _SourceSnapshot(
        manifest=manifest,
        agents=agents,
        adapters=adapters,
        materialized_adapters=materialized_adapters,
        common_roles=common_roles,
    )


def _capture_bundle_inventory(root: Path) -> _BundleInventory:
    tokens: Dict[str, _StatToken] = {".": _directory_token(root)}
    _require_directory_names(root, {"harness.json", "profiles", "shared"})
    directories = (
        "profiles",
        "profiles/current",
        "profiles/lean",
        "shared",
        "shared/agents",
        "shared/common-agents",
    )
    for relative in directories:
        path = root / relative
        tokens[relative] = _directory_token(path)
    _require_directory_names(root / "profiles", set(_PROFILES))
    for profile in _PROFILES:
        _require_directory_names(root / "profiles" / profile, {"AGENTS.md"})
    _require_directory_names(root / "shared", {"agents", "common-agents"})

    adapter_files = _directory_names(root / "shared" / "agents")
    role_files = _directory_names(root / "shared" / "common-agents")
    if not adapter_files or len(adapter_files) > _MAX_ROLES:
        raise HarnessError("invalid_bundle")
    if any(not name.endswith(".toml") or not name[:-5] for name in adapter_files):
        raise HarnessError("invalid_bundle")
    if any(not name.endswith(".md") or not name[:-3] for name in role_files):
        raise HarnessError("invalid_bundle")
    adapter_names = tuple(name[:-5] for name in adapter_files)
    role_names = tuple(name[:-3] for name in role_files)
    require_unique_canonical_names(adapter_names)
    require_unique_canonical_names(role_names)
    if (
        set(adapter_names) != set(role_names)
        or set(adapter_names) != set(_EXPECTED_ROLES)
    ):
        raise HarnessError("invalid_bundle")

    files = ["harness.json"]
    files.extend("profiles/{}/AGENTS.md".format(item) for item in _PROFILES)
    files.extend("shared/agents/{}".format(item) for item in adapter_files)
    files.extend("shared/common-agents/{}".format(item) for item in role_files)
    total_size = 0
    for relative in files:
        token = _source_file_token(root / relative)
        tokens[relative] = token
        total_size += token[4]
    if total_size > _MAX_BUNDLE_BYTES:
        raise HarnessError("invalid_bundle")
    names = tuple(sorted(files, key=lambda value: value.encode("utf-8")))
    return _BundleInventory(names=names, tokens=tokens)


def _private_bundle_root(path: Path) -> Path:
    root = Path(path).absolute()
    _reject_symlink_components(root)
    try:
        metadata = root.lstat()
    except OSError:
        raise HarnessError("invalid_bundle") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != _SOURCE_DIRECTORY_MODE
        or metadata.st_uid != os.getuid()
    ):
        raise HarnessError("invalid_bundle")
    return root


def _private_home(path: Path) -> Path:
    home = Path(path).absolute()
    _reject_symlink_components(home)
    metadata = home.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise HarnessError("materialized_harness_mismatch")
    return home


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise HarnessError("invalid_bundle")
        except FileNotFoundError:
            break


def _directory_names(path: Path) -> Tuple[str, ...]:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != _SOURCE_DIRECTORY_MODE
        or metadata.st_uid != os.getuid()
    ):
        raise HarnessError("invalid_bundle")
    entries = tuple(os.scandir(str(path)))
    names = require_unique_canonical_names(entry.name for entry in entries)
    return tuple(sorted(names, key=lambda value: value.encode("utf-8")))


def _require_directory_names(path: Path, expected: set) -> None:
    if set(_directory_names(path)) != expected:
        raise HarnessError("invalid_bundle")


def _directory_token(path: Path) -> _StatToken:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != _SOURCE_DIRECTORY_MODE
        or metadata.st_uid != os.getuid()
    ):
        raise HarnessError("invalid_bundle")
    return _stat_token(metadata)


def _source_file_token(path: Path) -> _StatToken:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != _SOURCE_FILE_MODE
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_FILE_BYTES
    ):
        raise HarnessError("invalid_bundle")
    return _stat_token(metadata)


def _stat_token(metadata: os.stat_result) -> _StatToken:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _identity_token(metadata: os.stat_result) -> Tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _read_source_file(path: Path, expected: _StatToken) -> bytes:
    if _source_file_token(path) != expected:
        raise HarnessError("source_changed")
    return _read_bounded_file(
        path,
        expected_mode=_SOURCE_FILE_MODE,
        changed_reason="source_changed",
    )


def _read_bounded_file(path: Path, expected_mode: int, changed_reason: str) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        raise HarnessError(changed_reason)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != expected_mode
        or before.st_nlink != 1
        or before.st_size > _MAX_FILE_BYTES
    ):
        raise HarnessError(changed_reason)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise HarnessError(changed_reason) from None
    try:
        opened = os.fstat(descriptor)
        if _stat_token(opened) != _stat_token(before):
            raise HarnessError(changed_reason)
        chunks = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    try:
        current = path.lstat()
    except OSError:
        raise HarnessError(changed_reason) from None
    if (
        len(content) > _MAX_FILE_BYTES
        or _stat_token(after) != _stat_token(opened)
        or _stat_token(current) != _stat_token(after)
    ):
        raise HarnessError(changed_reason)
    return content


def _rewrite_adapter(name: str, content: bytes) -> bytes:
    try:
        text = content.decode("utf-8", errors="strict")
        value = _parse_toml(text)
        if value.get("name") != name:
            raise ValueError("name mismatch")
        instructions = value.get("developer_instructions")
        if not isinstance(instructions, str):
            raise ValueError("missing instructions")
        absolute_paths = tuple(
            token
            for token in _BACKTICK_PATH_PATTERN.findall(instructions)
            if token.startswith("/")
        )
        for token in absolute_paths:
            _validate_absolute_posix_path(token)
        common_paths = tuple(
            token
            for token in absolute_paths
            if "/.agents/common-agents/" in token
        )
        suffix = "/.agents/common-agents/{}.md".format(name)
        if len(common_paths) != 1 or not common_paths[0].endswith(suffix):
            raise ValueError("include mismatch")
        source = common_paths[0]
        source_directive = "`{}`".format(source)
        target = "~/.agents/common-agents/{}.md".format(name)
        if text.count(source_directive) != 1:
            raise ValueError("include mismatch")
        rewritten = text.replace(source_directive, "`{}`".format(target), 1)
        rewritten_value = _parse_toml(rewritten)
        rewritten_instructions = rewritten_value.get("developer_instructions")
        if (
            rewritten_value.get("name") != name
            or not isinstance(rewritten_instructions, str)
            or rewritten_instructions.count("`{}`".format(target)) != 1
            or any(
                token.startswith("/") and "/.agents/common-agents/" in token
                for token in _BACKTICK_PATH_PATTERN.findall(rewritten_instructions)
            )
        ):
            raise ValueError("rewrite mismatch")
        return rewritten.encode("utf-8")
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise HarnessError("invalid_bundle") from None


def _parse_toml(text: str) -> Mapping[str, object]:
    if _tomllib is not None:
        value = _tomllib.loads(text)
        if not isinstance(value, dict):
            raise ValueError("invalid TOML")
        return value
    # Python 3.9 compatibility for the two required top-level string fields.
    result = {}
    for key in ("name", "developer_instructions"):
        matches = re.findall(
            r"(?m)^{}\s*=\s*(\"(?:\\.|[^\"\\])*\")\s*(?:#.*)?$".format(key),
            text,
        )
        if len(matches) != 1:
            raise ValueError("invalid TOML")
        result[key] = json.loads(matches[0])
    return result


def _validate_absolute_posix_path(value: str) -> None:
    if "\x00" in value or "\n" in value or "\r" in value or not value.startswith("/"):
        raise ValueError("invalid include")
    components = value[1:].split("/")
    if not components or any(item in ("", ".", "..") for item in components):
        raise ValueError("invalid include")


def _inventory_hash(domain: str, values: Mapping[str, bytes], suffix: str) -> str:
    digest = hashlib.sha256()
    _hash_part(digest, domain.encode("ascii"))
    for name in sorted(values, key=lambda value: value.encode("utf-8")):
        _hash_part(digest, (name + suffix).encode("utf-8"))
        _hash_part(digest, oct(_READ_ONLY_FILE_MODE).encode("ascii"))
        _hash_part(digest, values[name])
    return "sha256:" + digest.hexdigest()


def _hash_part(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(len(value).to_bytes(8, byteorder="big"))
    digest.update(value)


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _bundle_digest(contents: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    _hash_part(digest, b"fixed-harness-bundle-v1")
    for relative in sorted(contents, key=lambda value: value.encode("utf-8")):
        _hash_part(digest, relative.encode("utf-8"))
        _hash_part(digest, oct(_SOURCE_FILE_MODE).encode("ascii"))
        _hash_part(digest, contents[relative])
    return "sha256:" + digest.hexdigest()


def _skill_routing_hash(checkout: CheckoutManifest) -> str:
    return _sha256(
        canonical_bytes(
            {
                "domain": "live-eval-skill-routing-v1",
                "materialized_skill_hashes": [
                    [name, checkout.materialized_hashes[name]]
                    for name in checkout.skill_names
                ],
                "plugin_manifest_hash": checkout.plugin_manifest_hash,
                "skill_names": list(checkout.skill_names),
            }
        )
    )


def _verify_materialized_harness_tree(
    home: Path, expected: HarnessManifest
) -> None:
    _require_materialized_names(home, set(_HOME_ENTRIES), 0o700)
    agents = _read_materialized_file(home / "AGENTS.md", _AGENTS_FILE_MODE)
    if _sha256(agents) != expected.agents_hash:
        raise HarnessError("materialized_harness_mismatch")

    adapter_directory = home / "agents"
    common_root = home / ".agents"
    common_directory = common_root / "common-agents"
    _require_materialized_names(common_root, {"common-agents"})
    adapter_files = _materialized_leaf_names(adapter_directory, ".toml")
    role_files = _materialized_leaf_names(common_directory, ".md")
    adapter_names = tuple(name[:-5] for name in adapter_files)
    role_names = tuple(name[:-3] for name in role_files)
    require_unique_canonical_names(adapter_names)
    require_unique_canonical_names(role_names)
    if (
        set(adapter_names) != set(role_names)
        or set(adapter_names) != set(_EXPECTED_ROLES)
        or len(adapter_names) != expected.adapter_count
        or len(role_names) != expected.role_count
    ):
        raise HarnessError("materialized_harness_mismatch")
    adapters = {
        name: _read_materialized_file(
            adapter_directory / "{}.toml".format(name), _READ_ONLY_FILE_MODE
        )
        for name in adapter_names
    }
    roles = {
        name: _read_materialized_file(
            common_directory / "{}.md".format(name), _READ_ONLY_FILE_MODE
        )
        for name in role_names
    }
    for name, content in adapters.items():
        text = content.decode("utf-8", errors="strict")
        value = _parse_toml(text)
        instructions = value.get("developer_instructions")
        target = "`~/.agents/common-agents/{}.md`".format(name)
        if (
            value.get("name") != name
            or not isinstance(instructions, str)
            or instructions.count(target) != 1
        ):
            raise HarnessError("materialized_harness_mismatch")
    if (
        _inventory_hash("adapter-materialized-v1", adapters, ".toml")
        != expected.adapter_materialized_hash
        or _inventory_hash("common-role-v1", roles, ".md")
        != expected.common_role_hash
    ):
        raise HarnessError("materialized_harness_mismatch")
    if not all(
        _SHA256_PATTERN.fullmatch(value)
        for value in (
            expected.agents_hash,
            expected.bundle_digest,
            expected.adapter_source_hash,
            expected.adapter_materialized_hash,
            expected.common_role_hash,
            expected.skill_routing_hash,
            expected.home_digest,
        )
    ):
        raise HarnessError("materialized_harness_mismatch")


def _require_materialized_names(
    directory: Path, expected: set, mode: int = _READ_ONLY_DIRECTORY_MODE
) -> None:
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode:
        raise HarnessError("materialized_harness_mismatch")
    entries = tuple(os.scandir(str(directory)))
    names = require_unique_canonical_names(entry.name for entry in entries)
    if set(names) != expected:
        raise HarnessError("materialized_harness_mismatch")


def _materialized_leaf_names(directory: Path, suffix: str) -> Tuple[str, ...]:
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != _READ_ONLY_DIRECTORY_MODE
    ):
        raise HarnessError("materialized_harness_mismatch")
    entries = tuple(os.scandir(str(directory)))
    names = require_unique_canonical_names(entry.name for entry in entries)
    if not names or any(not name.endswith(suffix) or not name[: -len(suffix)] for name in names):
        raise HarnessError("materialized_harness_mismatch")
    for entry in entries:
        metadata = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessError("materialized_harness_mismatch")
    return tuple(sorted(names, key=lambda value: value.encode("utf-8")))


def _read_materialized_file(path: Path, mode: int) -> bytes:
    return _read_bounded_file(
        path,
        expected_mode=mode,
        changed_reason="materialized_harness_mismatch",
    )
