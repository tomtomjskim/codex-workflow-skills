#!/usr/bin/env python3
"""Read-only validation for Codex, Serena, rules, and shared-agent host policy."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if __name__ == "__main__" and not sys.flags.isolated:
    print(
        json.dumps(
            {
                "status": "error",
                "errors": [
                    {
                        "code": "validator.input_error",
                        "location": "argv",
                        "message": "host-policy validator requires isolated Python (-I)",
                    }
                ],
                "warnings": [],
            },
            sort_keys=True,
        )
    )
    raise SystemExit(1)


def _activate_vendored_dependencies() -> None:
    root = Path(__file__).resolve(strict=True).parents[1] / "vendor" / "py39"
    manifest_path = root.parent / "py39-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != (
        "33c29f435749f05e53bf1fda8a43f038a2e96d4b0c8abea322159708e12731f8"
    ):
        raise ImportError("vendored dependency manifest differs from bootstrap")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not manifest or not all(
        isinstance(relative, str)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        for relative, digest in manifest.items()
    ):
        raise ImportError("vendored dependency manifest is invalid")
    allowed_owners = {0, os.getuid()}
    current = Path(root.anchor)
    directories = [current]
    for component in root.parts[1:]:
        directories.append(directories[-1] / component)
    for directory in directories:
        metadata = directory.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owners
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ImportError("vendored dependency path is not owner-controlled")
    manifest_metadata = manifest_path.lstat()
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_uid not in allowed_owners
        or stat.S_IMODE(manifest_metadata.st_mode) & 0o022
    ):
        raise ImportError("vendored dependency manifest is not owner-controlled")
    actual_files = set()
    for candidate in root.rglob("*"):
        metadata = candidate.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid not in allowed_owners
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise ImportError("vendored dependency directory is not owner-controlled")
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in allowed_owners
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ImportError("vendored dependency file is not owner-controlled")
        relative = str(candidate.relative_to(root))
        actual_files.add(relative)
        candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if relative not in manifest or candidate_digest != manifest[relative]:
            raise ImportError("vendored dependency differs from bootstrap manifest")
    if actual_files != set(manifest):
        raise ImportError("vendored dependency inventory differs from bootstrap manifest")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))


try:
    _activate_vendored_dependencies()
except (OSError, ValueError, ImportError, json.JSONDecodeError) as bootstrap_error:
    if __name__ == "__main__":
        print(
            json.dumps(
                {
                    "status": "error",
                    "errors": [
                        {
                            "code": "validator.input_error",
                            "location": "vendor",
                            "message": str(bootstrap_error),
                        }
                    ],
                    "warnings": [],
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1)
    raise


import yaml


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


def _sibling_module(module_name: str) -> ModuleType:
    if __package__:
        if module_name == "agent_contracts":
            from . import agent_contracts as module
        elif module_name == "host_migration_apply":
            from . import host_migration_apply as module
        else:
            raise ImportError("unsupported host-policy sibling")
    else:
        module = _load_direct_sibling(module_name)
    expected = Path(__file__).resolve(strict=True).with_name("{}.py".format(module_name))
    if Path(module.__file__).resolve(strict=True) != expected:
        raise ImportError("host-policy module is not the sealed sibling")
    return module


effective_reviewer_contract_violations = _sibling_module(
    "agent_contracts"
).effective_reviewer_contract_violations

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python 3.9/3.10 repository baseline.
    import tomli as tomllib  # type: ignore[no-redef]


Issue = Dict[str, str]
AUTHORITATIVE_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "policies" / "host-policy.json"
)
VALIDATOR_ID = "validate_host_policy.py@3"


def _issue(code: str, location: str, message: str) -> Issue:
    return {"code": code, "location": location, "message": message}


def _canonical_policy_sha256(policy: Dict[str, Any]) -> str:
    canonical = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_toml_fallback(source: str) -> Dict[str, Any]:
    """Compatibility entry point backed by the standard-compatible parser."""
    return tomllib.loads(source)


def load_toml(path: Path) -> Dict[str, Any]:
    return load_toml_bytes(path.read_bytes())


def load_toml_bytes(data: bytes) -> Dict[str, Any]:
    return tomllib.loads(data.decode("utf-8"))


def load_yaml(path: Path) -> Dict[str, Any]:
    return load_yaml_bytes(path.read_bytes(), str(path))


def load_yaml_bytes(data: bytes, location: str) -> Dict[str, Any]:
    value = yaml.safe_load(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("YAML root must be a mapping: {}".format(location))
    return value


def _expand_home(value: str, home_root: Path) -> str:
    return value.replace("${HOME}", str(home_root))


def _expand_expected(value: Any, home_root: Path) -> Any:
    if isinstance(value, str):
        return _expand_home(value, home_root)
    if isinstance(value, list):
        return [_expand_expected(item, home_root) for item in value]
    if isinstance(value, dict):
        return {
            key: _expand_expected(item, home_root) for key, item in value.items()
        }
    return value


def _canonical_path(value: str) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _table(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_set(value: Any) -> Optional[set]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ) or len(value) != len(set(value)):
        return None
    return set(value)


def _require_mapping(container: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise ValueError("{} must be a mapping".format(path))
    return value


def _require_list(
    container: Dict[str, Any], key: str, path: str, *, nonempty: bool = False
) -> List[Any]:
    value = container.get(key)
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty list" if nonempty else "a list"
        raise ValueError("{} must be {}".format(path, qualifier))
    return value


def _require_string_list(
    container: Dict[str, Any], key: str, path: str, *, nonempty: bool = False
) -> List[str]:
    value = _require_list(container, key, path, nonempty=nonempty)
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError("{} must contain only non-empty strings".format(path))
    if len(value) != len(set(value)):
        raise ValueError("{} must not contain duplicates".format(path))
    return value


def _require_bool(container: Dict[str, Any], key: str, path: str) -> bool:
    value = container.get(key)
    if not isinstance(value, bool):
        raise ValueError("{} must be a boolean".format(path))
    return value


def _require_int(container: Dict[str, Any], key: str, path: str) -> int:
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("{} must be an integer".format(path))
    return value


def _require_keys(container: Dict[str, Any], keys: Iterable[str], path: str) -> None:
    missing = sorted(set(keys) - set(container))
    if missing:
        raise ValueError("{} is missing required keys: {}".format(path, missing))


def _reject_unknown(container: Dict[str, Any], keys: Iterable[str], path: str) -> None:
    unknown = sorted(set(container) - set(keys))
    if unknown:
        raise ValueError("{} has unknown keys: {}".format(path, unknown))


def validate_policy_manifest(policy: Dict[str, Any]) -> None:
    """Fail closed when the versioned security policy is incomplete or malformed."""
    if not isinstance(policy, dict) or policy.get("schema_version") != 2:
        raise ValueError("host policy must use schema_version 2")
    _require_keys(policy, ("codex", "rules", "serena", "agents"), "policy")
    _reject_unknown(
        policy, ("schema_version", "codex", "rules", "serena", "agents"), "policy"
    )

    codex = _require_mapping(policy, "codex", "policy.codex")
    codex_keys = (
        "supported_versions",
        "allowed_sandbox_modes",
        "allowed_approval_policies",
        "require_full_access_warning",
        "forbidden_trusted_paths",
        "missing_trusted_path_policy",
        "exact_mcp_inventory",
        "mcp_servers",
        "profiles",
        "expensive_default_warning",
    )
    _require_keys(codex, codex_keys, "policy.codex")
    _reject_unknown(codex, codex_keys, "policy.codex")
    _require_string_list(
        codex, "supported_versions", "policy.codex.supported_versions", nonempty=True
    )
    _require_string_list(
        codex, "allowed_sandbox_modes", "policy.codex.allowed_sandbox_modes", nonempty=True
    )
    approval_policies = _require_string_list(
        codex,
        "allowed_approval_policies",
        "policy.codex.allowed_approval_policies",
        nonempty=True,
    )
    if "never" in approval_policies:
        raise ValueError("policy.codex.allowed_approval_policies must not allow never")
    _require_bool(
        codex, "require_full_access_warning", "policy.codex.require_full_access_warning"
    )
    if codex.get("missing_trusted_path_policy") not in ("error", "warning"):
        raise ValueError(
            "policy.codex.missing_trusted_path_policy must be error or warning"
        )
    _require_string_list(
        codex, "forbidden_trusted_paths", "policy.codex.forbidden_trusted_paths", nonempty=True
    )
    _require_bool(codex, "exact_mcp_inventory", "policy.codex.exact_mcp_inventory")
    servers = _require_mapping(codex, "mcp_servers", "policy.codex.mcp_servers")
    if not servers:
        raise ValueError("policy.codex.mcp_servers must not be empty")
    for name, raw_server in servers.items():
        if not isinstance(name, str) or not name or not isinstance(raw_server, dict):
            raise ValueError("policy.codex.mcp_servers entries must be named mappings")
        location = "policy.codex.mcp_servers.{}".format(name)
        server_keys = (
            "identity",
            "required_values",
            "enabled_tools",
            "forbidden_tools",
            "allowed_approval_modes",
            "allowed_tool_approval_modes",
        )
        _reject_unknown(raw_server, server_keys, location)
        identity = _require_mapping(raw_server, "identity", location + ".identity")
        _reject_unknown(
            identity,
            ("command", "args", "cwd", "env"),
            location + ".identity",
        )
        command = identity.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(location + ".identity.command must be a non-empty string")
        _require_string_list(identity, "args", location + ".identity.args")
        if "cwd" in identity and (
            not isinstance(identity["cwd"], str) or not identity["cwd"]
        ):
            raise ValueError(location + ".identity.cwd must be a non-empty string")
        if "env" in identity:
            identity_env = _require_mapping(identity, "env", location + ".identity.env")
            if not all(
                isinstance(key, str)
                and key
                and isinstance(value, str)
                for key, value in identity_env.items()
            ):
                raise ValueError(location + ".identity.env must map strings to strings")
        required_values = _require_mapping(
            raw_server, "required_values", location + ".required_values"
        )
        if not required_values:
            raise ValueError(location + ".required_values must not be empty")
        if "enabled_tools" in raw_server:
            _require_string_list(
                raw_server, "enabled_tools", location + ".enabled_tools", nonempty=True
            )
        if "forbidden_tools" in raw_server:
            _require_string_list(
                raw_server, "forbidden_tools", location + ".forbidden_tools", nonempty=True
            )
        if "allowed_approval_modes" in raw_server:
            _require_string_list(
                raw_server,
                "allowed_approval_modes",
                location + ".allowed_approval_modes",
                nonempty=True,
            )
        if "allowed_tool_approval_modes" in raw_server:
            _require_string_list(
                raw_server,
                "allowed_tool_approval_modes",
                location + ".allowed_tool_approval_modes",
                nonempty=True,
            )
        if "enabled_tools" in raw_server and (
            "forbidden_tools" not in raw_server
            or "allowed_approval_modes" not in raw_server
            or "allowed_tool_approval_modes" not in raw_server
        ):
            raise ValueError(
                location
                + " tool allowlists require forbidden tools and server/tool approval modes"
            )
    db_policy = _require_mapping(servers, "db-mcp", "policy.codex.mcp_servers")
    db_required_values = _require_mapping(
        db_policy,
        "required_values",
        "policy.codex.mcp_servers.db-mcp",
    )
    if db_required_values.get("enabled") is not False:
        raise ValueError("policy must keep base db-mcp explicitly disabled")
    profiles = _require_mapping(codex, "profiles", "policy.codex.profiles")
    if set(profiles) != {"scout", "builder", "operator"}:
        raise ValueError("policy.codex.profiles must exactly define scout, builder, operator")
    for name, raw_profile in profiles.items():
        if not isinstance(raw_profile, dict):
            raise ValueError("policy.codex.profiles.{} must be a mapping".format(name))
        _reject_unknown(
            raw_profile,
            ("required_values", "mcp_enabled"),
            "policy.codex.profiles.{}".format(name),
        )
        required_values = _require_mapping(
            raw_profile, "required_values", "policy.codex.profiles.{}.required_values".format(name)
        )
        _require_keys(
            required_values,
            ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy"),
            "policy.codex.profiles.{}.required_values".format(name),
        )
        _reject_unknown(
            required_values,
            ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy"),
            "policy.codex.profiles.{}.required_values".format(name),
        )
        if required_values["sandbox_mode"] not in codex["allowed_sandbox_modes"]:
            raise ValueError("profile sandbox must be allowed by the Codex base")
        if required_values["approval_policy"] not in approval_policies:
            raise ValueError("profile approval policy must be allowed by the Codex base")
        mcp_enabled = _require_mapping(
            raw_profile, "mcp_enabled", "policy.codex.profiles.{}.mcp_enabled".format(name)
        )
        if set(mcp_enabled) != set(servers):
            raise ValueError("policy.codex.profiles.{}.mcp_enabled must cover exact MCP inventory".format(name))
        if not all(isinstance(value, bool) for value in mcp_enabled.values()):
            raise ValueError("policy.codex.profiles.{}.mcp_enabled values must be booleans".format(name))
        if mcp_enabled.get("db-mcp") is not False:
            raise ValueError("policy must keep db-mcp disabled in every profile")
    expensive = _require_mapping(
        codex, "expensive_default_warning", "policy.codex.expensive_default_warning"
    )
    _reject_unknown(
        expensive,
        ("models", "reasoning_efforts", "service_tiers"),
        "policy.codex.expensive_default_warning",
    )
    for key in ("models", "reasoning_efforts", "service_tiers"):
        _require_string_list(
            expensive,
            key,
            "policy.codex.expensive_default_warning.{}".format(key),
            nonempty=True,
        )

    rules = _require_mapping(policy, "rules", "policy.rules")
    _reject_unknown(rules, ("allowed_allow_prefixes",), "policy.rules")
    allow_prefixes = _require_list(
        rules, "allowed_allow_prefixes", "policy.rules.allowed_allow_prefixes"
    )
    if allow_prefixes:
        raise ValueError(
            "policy.rules.allowed_allow_prefixes must stay empty; prefix rules accept trailing arguments"
        )
    for prefix in allow_prefixes:
        if not isinstance(prefix, list) or not prefix or not all(
            isinstance(item, str) and item for item in prefix
        ):
            raise ValueError("policy.rules.allowed_allow_prefixes must contain string arrays")

    serena = _require_mapping(policy, "serena", "policy.serena")
    serena_keys = (
        "supported_versions",
        "required_trusted_project_path_patterns",
        "required_excluded_tools",
        "required_default_modes",
        "max_default_answer_chars",
        "minimum_log_level",
        "required_values",
        "projects",
        "project",
    )
    _require_keys(serena, serena_keys, "policy.serena")
    _reject_unknown(serena, serena_keys, "policy.serena")
    _require_string_list(
        serena, "supported_versions", "policy.serena.supported_versions", nonempty=True
    )
    _require_string_list(
        serena,
        "required_trusted_project_path_patterns",
        "policy.serena.required_trusted_project_path_patterns",
    )
    _require_string_list(
        serena, "required_excluded_tools", "policy.serena.required_excluded_tools", nonempty=True
    )
    _require_string_list(
        serena, "required_default_modes", "policy.serena.required_default_modes", nonempty=True
    )
    _require_int(
        serena, "max_default_answer_chars", "policy.serena.max_default_answer_chars"
    )
    _require_int(serena, "minimum_log_level", "policy.serena.minimum_log_level")
    _require_mapping(serena, "required_values", "policy.serena.required_values")
    _require_string_list(
        serena, "projects", "policy.serena.projects", nonempty=True
    )
    project_policy = _require_mapping(serena, "project", "policy.serena.project")
    project_keys = (
        "require_languages",
        "forbidden_keys",
        "required_values",
        "require_empty_activation_command",
    )
    _reject_unknown(project_policy, project_keys, "policy.serena.project")
    _require_bool(project_policy, "require_languages", "policy.serena.project.require_languages")
    _require_string_list(
        project_policy, "forbidden_keys", "policy.serena.project.forbidden_keys", nonempty=True
    )
    _require_mapping(project_policy, "required_values", "policy.serena.project.required_values")
    _require_bool(
        project_policy,
        "require_empty_activation_command",
        "policy.serena.project.require_empty_activation_command",
    )

    agents = _require_mapping(policy, "agents", "policy.agents")
    agent_keys = (
        "canonical_names",
        "reviewer_lens_agents",
        "protected_reviewer_roles",
        "reviewer_adapter_instruction_template",
        "forbid_project_local_shadow",
        "reviewer_runtime",
        "required_active_platforms",
    )
    _require_keys(agents, agent_keys, "policy.agents")
    _reject_unknown(agents, agent_keys, "policy.agents")
    canonical_names = set(
        _require_string_list(
            agents, "canonical_names", "policy.agents.canonical_names", nonempty=True
        )
    )
    lens_agents = _require_mapping(
        agents, "reviewer_lens_agents", "policy.agents.reviewer_lens_agents"
    )
    if not lens_agents or not all(
        isinstance(lens, str) and lens and isinstance(role, str) and role
        for lens, role in lens_agents.items()
    ):
        raise ValueError("policy.agents.reviewer_lens_agents must map lenses to roles")
    protected_roles = set(
        _require_string_list(
            agents,
            "protected_reviewer_roles",
            "policy.agents.protected_reviewer_roles",
            nonempty=True,
        )
    )
    if protected_roles != set(lens_agents.values()):
        raise ValueError("protected reviewer roles must exactly cover reviewer routing values")
    if not protected_roles.issubset(canonical_names):
        raise ValueError("protected reviewer roles must be canonical agents")
    instruction = agents.get("reviewer_adapter_instruction_template")
    if (
        not isinstance(instruction, str)
        or instruction.count("${COMMON_ROLE}") != 1
    ):
        raise ValueError(
            "policy.agents.reviewer_adapter_instruction_template must contain one ${COMMON_ROLE}"
        )
    _require_bool(
        agents, "forbid_project_local_shadow", "policy.agents.forbid_project_local_shadow"
    )
    runtime = _require_mapping(agents, "reviewer_runtime", "policy.agents.reviewer_runtime")
    _reject_unknown(runtime, ("codex", "claude"), "policy.agents.reviewer_runtime")
    codex_runtime = _require_mapping(runtime, "codex", "policy.agents.reviewer_runtime.codex")
    _reject_unknown(
        codex_runtime,
        ("sandbox_mode", "disabled_mcp_servers"),
        "policy.agents.reviewer_runtime.codex",
    )
    if codex_runtime.get("sandbox_mode") != "read-only":
        raise ValueError("policy reviewer sandbox must be read-only")
    disabled_reviewer_mcp = _require_string_list(
        codex_runtime,
        "disabled_mcp_servers",
        "policy.agents.reviewer_runtime.codex.disabled_mcp_servers",
        nonempty=True,
    )
    if "db-mcp" not in disabled_reviewer_mcp:
        raise ValueError("policy must disable db-mcp for every protected Codex reviewer")
    claude_runtime = _require_mapping(runtime, "claude", "policy.agents.reviewer_runtime.claude")
    _reject_unknown(
        claude_runtime,
        ("required_disallowed_tools",),
        "policy.agents.reviewer_runtime.claude",
    )
    _require_string_list(
        claude_runtime,
        "required_disallowed_tools",
        "policy.agents.reviewer_runtime.claude.required_disallowed_tools",
        nonempty=True,
    )
    _require_string_list(
        agents,
        "required_active_platforms",
        "policy.agents.required_active_platforms",
        nonempty=True,
    )


def validate_codex_config(
    config: Dict[str, Any],
    policy: Dict[str, Any],
    home_root: Path,
) -> Tuple[List[Issue], List[Issue]]:
    errors: List[Issue] = []
    warnings: List[Issue] = []
    codex_policy = _table(policy.get("codex"))

    sandbox_mode = config.get("sandbox_mode")
    allowed_modes = set(codex_policy.get("allowed_sandbox_modes", []))
    if sandbox_mode not in allowed_modes:
        errors.append(
            _issue(
                "codex.sandbox_mode",
                "config.toml:sandbox_mode",
                "sandbox_mode must be one of {}".format(sorted(allowed_modes)),
            )
        )

    approval_policy = config.get("approval_policy")
    allowed_approval_policies = set(
        codex_policy.get("allowed_approval_policies", [])
    )
    if approval_policy not in allowed_approval_policies:
        errors.append(
            _issue(
                "codex.approval_policy",
                "config.toml:approval_policy",
                "approval_policy must be one of {}".format(
                    sorted(allowed_approval_policies)
                ),
            )
        )

    notice = _table(config.get("notice"))
    if (
        codex_policy.get("require_full_access_warning")
        and notice.get("hide_full_access_warning") is True
    ):
        errors.append(
            _issue(
                "codex.full_access_warning_hidden",
                "config.toml:notice.hide_full_access_warning",
                "the full-access warning must remain visible",
            )
        )

    forbidden_paths = {
        _canonical_path(_expand_home(value, home_root))
        for value in codex_policy.get("forbidden_trusted_paths", [])
    }
    for project_path, project_config in _table(config.get("projects")).items():
        if _table(project_config).get("trust_level") != "trusted":
            continue
        normalized = _canonical_path(project_path)
        if normalized in forbidden_paths:
            errors.append(
                _issue(
                    "codex.broad_trust",
                    "config.toml:projects",
                    "broad trusted path is forbidden by host policy",
                )
            )
        if not Path(project_path).exists():
            issue = _issue(
                "codex.stale_trust",
                "config.toml:projects",
                "a trusted project path does not currently exist",
            )
            if codex_policy.get("missing_trusted_path_policy") == "error":
                errors.append(issue)
            elif codex_policy.get("missing_trusted_path_policy") == "warning":
                warnings.append(issue)

    configured_servers = _table(config.get("mcp_servers"))
    expected_server_names = set(_table(codex_policy.get("mcp_servers")))
    if codex_policy.get("exact_mcp_inventory") and set(configured_servers) != expected_server_names:
        errors.append(
            _issue(
                "codex.mcp_inventory",
                "config.toml:mcp_servers",
                "configured MCP names must exactly match the reviewed inventory",
            )
        )
    for server_name, server_policy_value in _table(
        codex_policy.get("mcp_servers")
    ).items():
        server_policy = _table(server_policy_value)
        server = _table(configured_servers.get(server_name))
        location = "config.toml:mcp_servers.{}".format(server_name)
        if not server:
            errors.append(
                _issue(
                    "codex.mcp_missing",
                    location,
                    "required MCP server policy is not configured",
                )
            )
            continue
        identity = _table(server_policy.get("identity"))
        expected_command = _expand_expected(identity.get("command"), home_root)
        expected_args = _expand_expected(identity.get("args"), home_root)
        expected_cwd = _expand_expected(identity.get("cwd"), home_root)
        expected_env = _expand_expected(identity.get("env", {}), home_root)
        if (
            server.get("command") != expected_command
            or server.get("args", []) != expected_args
            or server.get("cwd") != expected_cwd
            or _table(server.get("env")) != expected_env
            or any(
                key in server
                for key in (
                    "url",
                    "auth",
                    "bearer_token_env_var",
                    "http_headers",
                    "env_http_headers",
                    "experimental_environment",
                )
            )
        ):
            errors.append(
                _issue(
                    "codex.mcp_identity",
                    location,
                    "MCP command and args must exactly match the reviewed identity",
                )
            )
        for key, expected in _table(server_policy.get("required_values")).items():
            if server.get(key) != expected:
                errors.append(
                    _issue(
                        "codex.mcp_required_value",
                        "{}:{}".format(location, key),
                        "expected {!r}".format(expected),
                    )
                )
        expected_tools = server_policy.get("enabled_tools")
        if expected_tools is not None:
            actual_tools = server.get("enabled_tools")
            actual_tool_set = _string_set(actual_tools)
            if actual_tool_set is None or actual_tool_set != set(expected_tools):
                errors.append(
                    _issue(
                        "codex.mcp_tool_allowlist",
                        "{}:enabled_tools".format(location),
                        "enabled_tools must exactly match the reviewed allowlist",
                    )
                )
        actual_tools = server.get("enabled_tools")
        actual_tool_set = _string_set(actual_tools)
        forbidden_tools = set(server_policy.get("forbidden_tools", []))
        if actual_tool_set is None and forbidden_tools:
            errors.append(
                _issue(
                    "codex.mcp_unbounded_tools",
                    "{}:enabled_tools".format(location),
                    "an explicit allowlist is required when tools are forbidden",
                )
            )
        elif forbidden_tools and forbidden_tools.intersection(actual_tool_set):
            errors.append(
                _issue(
                    "codex.mcp_forbidden_tool",
                    "{}:enabled_tools".format(location),
                    "a forbidden MCP tool is enabled",
                )
            )
        approval_modes = server_policy.get("allowed_approval_modes")
        if approval_modes is not None and server.get(
            "default_tools_approval_mode"
        ) not in approval_modes:
            errors.append(
                _issue(
                    "codex.mcp_approval_mode",
                    "{}:default_tools_approval_mode".format(location),
                    "explicit approval mode must be one of {}".format(
                        sorted(approval_modes)
                    ),
                )
            )
        allowed_tool_modes = server_policy.get("allowed_tool_approval_modes")
        if allowed_tool_modes is not None:
            for tool_name, tool_config in _table(server.get("tools")).items():
                if _table(tool_config).get("approval_mode") not in allowed_tool_modes:
                    errors.append(
                        _issue(
                            "codex.mcp_tool_approval_mode",
                            "{}:tools.{}.approval_mode".format(location, tool_name),
                            "tool approval mode must be one of {}".format(
                                sorted(allowed_tool_modes)
                            ),
                        )
                    )

    expensive = _table(codex_policy.get("expensive_default_warning"))
    checks = (
        config.get("model") in expensive.get("models", []),
        config.get("model_reasoning_effort")
        in expensive.get("reasoning_efforts", []),
        config.get("service_tier") in expensive.get("service_tiers", []),
    )
    if expensive and all(checks):
        warnings.append(
            _issue(
                "codex.expensive_default",
                "config.toml:model",
                "the global default uses the highest-cost reviewed combination",
            )
        )
    return errors, warnings


def validate_codex_profiles(
    profiles: Dict[str, Dict[str, Any]],
    policy: Dict[str, Any],
    expected_names: Optional[Iterable[str]] = None,
) -> List[Issue]:
    errors: List[Issue] = []
    profile_policy = _table(_table(policy.get("codex")).get("profiles"))
    expected_inventory = (
        set(profile_policy) if expected_names is None else set(expected_names)
    )
    if not expected_inventory.issubset(profile_policy):
        raise ValueError("requested Codex profile audit inventory is unknown")
    if set(profiles) != expected_inventory:
        errors.append(
            _issue(
                "codex.profile_inventory",
                "Codex profiles",
                "profile inputs must exactly match the reviewed stage inventory",
            )
        )
    for name in sorted(set(profiles).intersection(expected_inventory)):
        config = profiles[name]
        expected = _table(profile_policy[name])
        location = "profile:{}.config.toml".format(name)
        required_value_keys = set(_table(expected.get("required_values")))
        if set(config) != required_value_keys | {"mcp_servers"}:
            errors.append(
                _issue(
                    "codex.profile_contract",
                    location,
                    "profile must contain only reviewed scalar values and MCP state overrides",
                )
            )
        for key, expected_value in _table(expected.get("required_values")).items():
            if config.get(key) != expected_value:
                errors.append(
                    _issue(
                        "codex.profile_required_value",
                        "{}:{}".format(location, key),
                        "expected {!r}".format(expected_value),
                    )
                )
        configured_servers = _table(config.get("mcp_servers"))
        expected_servers = set(_table(expected.get("mcp_enabled")))
        if set(configured_servers) != expected_servers:
            errors.append(
                _issue(
                    "codex.profile_mcp_inventory",
                    "{}:mcp_servers".format(location),
                    "profile MCP names must exactly match the reviewed inventory",
                )
            )
        for server_name, enabled in _table(expected.get("mcp_enabled")).items():
            server = _table(configured_servers.get(server_name))
            if set(server) != {"enabled"}:
                errors.append(
                    _issue(
                        "codex.profile_mcp_override",
                        "{}:mcp_servers.{}".format(location, server_name),
                        "profiles may override only the MCP enabled state",
                    )
                )
            if server.get("enabled") is not enabled:
                errors.append(
                    _issue(
                        "codex.profile_mcp_state",
                        "{}:mcp_servers.{}.enabled".format(location, server_name),
                        "expected {!r}".format(enabled),
                    )
                )
    return errors


def validate_codex_version(version: str, policy: Dict[str, Any]) -> List[Issue]:
    supported = set(_table(policy.get("codex")).get("supported_versions", []))
    if version not in supported:
        return [
            _issue(
                "codex.unsupported_version",
                "Codex runtime",
                "version must be one of {}".format(sorted(supported)),
            )
        ]
    return []


def parse_prefix_rules_text(source: str) -> List[Tuple[int, List[str], str]]:
    rules = []
    expression = re.compile(
        r'^prefix_rule\(pattern=(\[.*\]),\s*decision="([^"]+)"\)\s*$'
    )
    for number, raw_line in enumerate(source.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = expression.match(line)
        if match is None:
            raise ValueError("unsupported rule syntax at line {}".format(number))
        pattern = json.loads(match.group(1))
        if not isinstance(pattern, list) or not all(
            isinstance(value, str) for value in pattern
        ):
            raise ValueError("invalid prefix pattern at line {}".format(number))
        rules.append((number, pattern, match.group(2)))
    return rules


def parse_prefix_rules(path: Path) -> List[Tuple[int, List[str], str]]:
    return parse_prefix_rules_text(path.read_text(encoding="utf-8"))


def validate_rules(
    rules: Iterable[Tuple[int, List[str], str]], policy: Dict[str, Any]
) -> List[Issue]:
    errors = []
    rules_policy = _table(policy.get("rules"))
    allowed = {
        tuple(prefix) for prefix in rules_policy.get("allowed_allow_prefixes", [])
    }
    for line, pattern, decision in rules:
        if decision != "allow" or not pattern:
            continue
        if tuple(pattern) not in allowed:
            errors.append(
                _issue(
                    "rules.unreviewed_allow_prefix",
                    "default.rules:{}".format(line),
                    "unconditional allow must exactly match a reviewed prefix",
                )
            )
    return errors


def validate_serena_config(
    config: Dict[str, Any], policy: Dict[str, Any]
) -> List[Issue]:
    errors = []
    serena_policy = _table(policy.get("serena"))
    trusted = config.get("trusted_project_path_patterns")
    expected_trusted = serena_policy.get(
        "required_trusted_project_path_patterns"
    )
    if trusted != expected_trusted:
        errors.append(
            _issue(
                "serena.trust_all",
                "serena_config.yml:trusted_project_path_patterns",
                "trusted project patterns must exactly match the reviewed base",
            )
        )
    excluded = _string_set(config.get("excluded_tools"))
    if excluded is None:
        excluded = set()
    missing_exclusions = set(
        serena_policy.get("required_excluded_tools", [])
    ) - excluded
    if missing_exclusions:
        errors.append(
            _issue(
                "serena.missing_tool_exclusion",
                "serena_config.yml:excluded_tools",
                "required tool exclusions are missing: {}".format(
                    sorted(missing_exclusions)
                ),
            )
        )
    default_modes = config.get("default_modes")
    expected_modes = serena_policy.get("required_default_modes")
    if default_modes != expected_modes:
        errors.append(
            _issue(
                "serena.unsafe_default_mode",
                "serena_config.yml:default_modes",
                "default modes must exactly match the reviewed base",
            )
        )
    maximum = serena_policy.get("max_default_answer_chars")
    if maximum is not None and (
        not isinstance(config.get("default_max_tool_answer_chars"), int)
        or config.get("default_max_tool_answer_chars") > maximum
    ):
        errors.append(
            _issue(
                "serena.answer_limit",
                "serena_config.yml:default_max_tool_answer_chars",
                "default answer limit must not exceed {}".format(maximum),
            )
        )
    minimum_log_level = serena_policy.get("minimum_log_level")
    if minimum_log_level is not None and (
        not isinstance(config.get("log_level"), int)
        or config.get("log_level") < minimum_log_level
    ):
        errors.append(
            _issue(
                "serena.log_level",
                "serena_config.yml:log_level",
                "log level must be at least {}".format(minimum_log_level),
            )
        )
    for key, expected in _table(serena_policy.get("required_values")).items():
        if config.get(key) != expected:
            errors.append(
                _issue(
                    "serena.required_value",
                    "serena_config.yml:{}".format(key),
                    "expected {!r}".format(expected),
                )
            )
    return errors


def validate_serena_version(version: str, policy: Dict[str, Any]) -> List[Issue]:
    supported = set(_table(policy.get("serena")).get("supported_versions", []))
    if version not in supported:
        return [
            _issue(
                "serena.unsupported_version",
                "Serena runtime",
                "version must be one of {}".format(sorted(supported)),
            )
        ]
    return []


def validate_serena_project(
    project: Dict[str, Any], policy: Dict[str, Any], label: str
) -> List[Issue]:
    errors = []
    project_policy = _table(_table(policy.get("serena")).get("project"))
    languages = project.get("languages")
    if project_policy.get("require_languages") and (
        not isinstance(languages, list)
        or not languages
        or not all(isinstance(value, str) and value for value in languages)
    ):
        errors.append(
            _issue(
                "serena.project_languages",
                "{}:languages".format(label),
                "a non-empty languages list is required",
            )
        )
    for key in project_policy.get("forbidden_keys", []):
        if key in project:
            errors.append(
                _issue(
                    "serena.project_forbidden_key",
                    "{}:{}".format(label, key),
                    "the project key is incompatible with the pinned schema",
                )
            )
    for key, expected in _table(project_policy.get("required_values")).items():
        if project.get(key) != expected:
            errors.append(
                _issue(
                    "serena.project_required_value",
                    "{}:{}".format(label, key),
                    "expected {!r}".format(expected),
                )
            )
    activation = project.get("activation_command")
    if project_policy.get("require_empty_activation_command") and activation not in (
        None,
        "",
    ):
        errors.append(
            _issue(
                "serena.project_activation_command",
                "{}:activation_command".format(label),
                "activation_command must be empty",
            )
        )
    return errors


def validate_project_inventory(
    project_files: Sequence[Tuple[str, Path]], policy: Dict[str, Any]
) -> Tuple[List[Issue], Dict[Path, str]]:
    errors: List[Issue] = []
    expected_ids = set(_table(policy.get("serena")).get("projects", []))
    actual: Dict[Path, str] = {}
    seen_ids = set()
    for project_id, project_file in project_files:
        root = _canonical_path(str(project_file.parent.parent))
        if root in actual or project_id in seen_ids:
            errors.append(
                _issue(
                    "serena.project_inventory",
                    str(project_file),
                    "duplicate Serena project input",
                )
            )
        actual[root] = project_id
        seen_ids.add(project_id)
    if seen_ids != expected_ids or len(actual) != len(expected_ids):
        errors.append(
            _issue(
                "serena.project_inventory",
                "Serena projects",
                "project inputs must exactly match the reviewed inventory",
            )
        )
    return errors, actual


def validate_project_agent_shadowing(
    project_roots: Iterable[Path], policy: Dict[str, Any]
) -> List[Issue]:
    errors: List[Issue] = []
    agents_policy = _table(policy.get("agents"))
    if not agents_policy.get("forbid_project_local_shadow"):
        return errors
    protected = set(agents_policy.get("protected_reviewer_roles", []))
    for root in project_roots:
        for relative, suffix in ((".codex/agents", ".toml"), (".claude/agents", ".md")):
            directory = root / relative
            if not directory.is_dir():
                continue
            shadows = sorted(
                path.name
                for path in directory.glob("*{}".format(suffix))
                if path.stem in protected
            )
            if shadows:
                errors.append(
                    _issue(
                        "agents.project_local_shadow",
                        str(directory),
                        "protected reviewer roles are shadowed: {}".format(shadows),
                    )
                )
    return errors


def _adapter_name(
    path: Path, platform: str, source: Optional[str] = None
) -> Optional[str]:
    text = path.read_text(encoding="utf-8") if source is None else source
    pattern = (
        r'(?m)^name\s*=\s*"([^"]+)"\s*$'
        if platform == "codex"
        else r"(?m)^name:\s*([^\s]+)\s*$"
    )
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _validate_active_links(
    active_root: Path,
    adapter_root: Path,
    suffix: str,
    expected_names: set,
    platform: str,
    required: bool,
    sealed_links: Optional[Dict[Path, Path]] = None,
) -> Tuple[List[Issue], List[Issue]]:
    errors: List[Issue] = []
    warnings: List[Issue] = []
    if not active_root.is_dir() or active_root.is_symlink():
        target = errors if required else warnings
        target.append(
            _issue(
                "agents.active_directory_missing",
                "{} active agents".format(platform),
                "active agent directory is unavailable",
            )
        )
        return errors, warnings
    paths = {path.stem: path for path in active_root.glob("*{}".format(suffix))}
    if set(paths) != expected_names:
        errors.append(
            _issue(
                "agents.active_inventory",
                "{} active agents".format(platform),
                "active agent names must exactly match the canonical inventory",
            )
        )
    for name in sorted(expected_names.intersection(paths)):
        path = paths[name]
        expected = adapter_root / "{}{}".format(name, suffix)
        if expected.is_symlink() or not expected.is_file():
            errors.append(
                _issue(
                    "agents.canonical_adapter_type",
                    "{}:{}".format(platform, name),
                    "canonical adapter must be a regular non-symlink file",
                )
            )
            continue
        if not path.is_symlink() or not path.exists():
            errors.append(
                _issue(
                    "agents.active_link",
                    "{}:{}".format(platform, name),
                    "active agent must be a non-broken direct symlink",
                )
            )
            continue
        if sealed_links is not None and path in sealed_links:
            lexical_target = sealed_links[path]
        else:
            target = Path(os.readlink(str(path)))
            if not target.is_absolute():
                target = path.parent / target
            lexical_target = Path(os.path.abspath(str(target)))
        lexical_expected = Path(os.path.abspath(str(expected)))
        if lexical_target != lexical_expected:
            errors.append(
                _issue(
                    "agents.active_link_target",
                    "{}:{}".format(platform, name),
                    "active agent symlink points to a non-canonical adapter",
                )
            )
    return errors, warnings


def validate_reviewer_routing(
    routing: Dict[str, Any], policy: Dict[str, Any]
) -> List[Issue]:
    expected = _table(_table(policy.get("agents")).get("reviewer_lens_agents"))
    actual = _table(routing.get("lens_agents"))
    if actual != expected:
        return [
            _issue(
                "agents.reviewer_routing",
                "reviewer-routing.json:lens_agents",
                "reviewer routing must exactly match the protected lens registry",
            )
        ]
    return []


def validate_agent_contracts(
    shared_root: Path,
    codex_active_root: Path,
    claude_active_root: Path,
    policy: Dict[str, Any],
    common_reference_root: Optional[Path] = None,
    sealed_files: Optional[Dict[Path, bytes]] = None,
    sealed_links: Optional[Dict[Path, Path]] = None,
) -> Tuple[List[Issue], List[Issue]]:
    errors: List[Issue] = []
    warnings: List[Issue] = []
    agents_policy = _table(policy.get("agents"))
    expected_names = set(agents_policy.get("canonical_names", []))
    reviewer_roles = set(agents_policy.get("protected_reviewer_roles", []))
    reviewer_runtime = _table(agents_policy.get("reviewer_runtime"))
    common_root = shared_root / "common-agents"
    reference_root = common_reference_root or common_root
    def agent_text(path: Path) -> str:
        if sealed_files is not None and path in sealed_files:
            return sealed_files[path].decode("utf-8")
        return path.read_text(encoding="utf-8")
    platform_specs = {
        "codex": (shared_root / "adapters" / "codex", ".toml", codex_active_root),
        "claude": (shared_root / "adapters" / "claude", ".md", claude_active_root),
    }
    for label, root in (
        ("shared root", shared_root),
        ("common agents", common_root),
        ("common agent reference", reference_root),
        ("Codex adapters", platform_specs["codex"][0]),
        ("Claude adapters", platform_specs["claude"][0]),
    ):
        if not root.is_dir() or root.is_symlink():
            errors.append(
                _issue(
                    "agents.canonical_root_type",
                    label,
                    "canonical root must be a real non-symlink directory",
                )
            )
    common_names = {path.stem for path in common_root.glob("*.md")}
    if common_names != expected_names:
        errors.append(
            _issue(
                "agents.common_inventory",
                "shared common agents",
                "common role names must exactly match the canonical inventory",
            )
        )
    required_platforms = set(agents_policy.get("required_active_platforms", []))
    for platform, (adapter_root, suffix, active_root) in platform_specs.items():
        adapter_paths = {
            path.stem: path for path in adapter_root.glob("*{}".format(suffix))
        }
        if set(adapter_paths) != expected_names:
            errors.append(
                _issue(
                    "agents.adapter_inventory",
                    "{} adapters".format(platform),
                    "adapter names must exactly match the canonical inventory",
                )
            )
        for name, path in sorted(adapter_paths.items()):
            if _adapter_name(path, platform, agent_text(path)) != name:
                errors.append(
                    _issue(
                        "agents.adapter_name",
                        "{}:{}".format(platform, name),
                        "declared adapter name does not match its filename",
                    )
                )
        for role in sorted(reviewer_roles):
            common_path = common_root / "{}.md".format(role)
            common_reference_path = reference_root / "{}.md".format(role)
            adapter_path = adapter_root / "{}{}".format(role, suffix)
            if not common_path.is_file() or not adapter_path.is_file():
                continue
            if common_path.is_symlink() or adapter_path.is_symlink():
                errors.append(
                    _issue(
                        "agents.canonical_agent_type",
                        "{}:{}".format(platform, role),
                        "common role and canonical adapter must be regular files",
                    )
                )
                continue
            violations = effective_reviewer_contract_violations(
                agent_text(common_path),
                agent_text(adapter_path),
            )
            for violation in violations:
                errors.append(
                    _issue(
                        "agents.effective_reviewer_contract",
                        "{}:{}".format(platform, role),
                        violation,
                    )
                )
            runtime_policy = _table(reviewer_runtime.get(platform))
            if platform == "codex":
                adapter_config = tomllib.loads(agent_text(adapter_path))
                expected_instruction = agents_policy.get(
                    "reviewer_adapter_instruction_template", ""
                ).replace("${COMMON_ROLE}", str(common_reference_path))
                if adapter_config.get("developer_instructions", "").strip() != expected_instruction:
                    errors.append(
                        _issue(
                            "agents.reviewer_adapter_template",
                            "{}:{}".format(platform, role),
                            "reviewer adapter instructions must exactly match the canonical template",
                        )
                    )
                expected_sandbox = runtime_policy.get("sandbox_mode")
                if (
                    expected_sandbox is not None
                    and adapter_config.get("sandbox_mode") != expected_sandbox
                ):
                    errors.append(
                        _issue(
                            "agents.reviewer_sandbox",
                            "{}:{}".format(platform, role),
                            "reviewer sandbox_mode must be {!r}".format(
                                expected_sandbox
                            ),
                        )
                    )
                adapter_mcp = _table(adapter_config.get("mcp_servers"))
                for server_name in runtime_policy.get(
                    "disabled_mcp_servers", []
                ):
                    if _table(adapter_mcp.get(server_name)).get("enabled") is not False:
                        errors.append(
                            _issue(
                                "agents.reviewer_mcp_enabled",
                                "{}:{}".format(platform, role),
                                "reviewer must explicitly disable MCP server {!r}".format(
                                    server_name
                                ),
                            )
                        )
            elif platform == "claude":
                adapter_source = agent_text(adapter_path)
                front_matter = {}
                adapter_body = adapter_source
                if adapter_source.startswith("---\n") and "\n---\n" in adapter_source[4:]:
                    raw_front_matter, adapter_body = adapter_source[4:].split(
                        "\n---\n", 1
                    )
                    loaded_front_matter = yaml.safe_load(raw_front_matter)
                    front_matter = _table(loaded_front_matter)
                if adapter_body.strip() != agents_policy.get(
                    "reviewer_adapter_instruction_template", ""
                ).replace("${COMMON_ROLE}", str(common_reference_path)):
                    errors.append(
                        _issue(
                            "agents.reviewer_adapter_template",
                            "{}:{}".format(platform, role),
                            "reviewer adapter instructions must exactly match the canonical template",
                        )
                    )
                disallowed = set(front_matter.get("disallowedTools") or [])
                required_disallowed = set(
                    runtime_policy.get("required_disallowed_tools", [])
                )
                if not required_disallowed.issubset(disallowed):
                    errors.append(
                        _issue(
                            "agents.reviewer_disallowed_tools",
                            "{}:{}".format(platform, role),
                            "reviewer must disallow {}".format(
                                sorted(required_disallowed)
                            ),
                        )
                    )
        link_errors, link_warnings = _validate_active_links(
            active_root,
            adapter_root,
            suffix,
            expected_names,
            platform,
            platform in required_platforms,
            sealed_links,
        )
        errors.extend(link_errors)
        warnings.extend(link_warnings)
    return errors, warnings


class StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("argument error: {}".format(message))


def _named_path(value: str) -> Tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    name, raw_path = value.split("=", 1)
    if not name or not raw_path:
        raise argparse.ArgumentTypeError("expected non-empty NAME=PATH")
    return name, Path(raw_path)


def _assert_regular_input(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("{} must be a regular non-symlink file: {}".format(label, path))


def _file_receipt(path: Path, label: str) -> Dict[str, Any]:
    data = path.read_bytes()
    return _file_receipt_bytes(path, label, data)


def _file_receipt_bytes(
    path: Path, label: str, data: bytes, mode: Optional[int] = None
) -> Dict[str, Any]:
    return {
        "label": label,
        "path": str(path),
        "mode": "{:04o}".format(
            (path.stat().st_mode if mode is None else mode) & 0o7777
        ),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _sealed_input(path: Path, label: str) -> Tuple[bytes, Dict[str, Any]]:
    _assert_regular_input(path, label)
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
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
        current.st_dev,
        current.st_ino,
    ) != (after.st_dev, after.st_ino):
        raise ValueError("{} changed while it was read".format(label))
    data = b"".join(chunks)
    return data, _file_receipt_bytes(path, label, data, before.st_mode)


def _sealed_link_target(path: Path, label: str) -> Path:
    before = path.lstat()
    if not stat.S_ISLNK(before.st_mode):
        raise ValueError("{} must be a symlink".format(label))
    raw_target = Path(os.readlink(str(path)))
    after = path.lstat()
    identity = (before.st_dev, before.st_ino, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_mtime_ns):
        raise ValueError("{} changed while it was read".format(label))
    if not raw_target.is_absolute():
        raw_target = path.parent / raw_target
    return Path(os.path.abspath(str(raw_target)))


def _agent_receipts(
    shared_root: Path,
    codex_active_root: Path,
    claude_active_root: Path,
    policy: Dict[str, Any],
    sealed_files: Optional[Dict[Path, bytes]] = None,
    sealed_links: Optional[Dict[Path, Path]] = None,
    sealed_receipts: Optional[Dict[Path, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    receipts: List[Dict[str, Any]] = []
    names = _table(policy.get("agents")).get("canonical_names", [])
    for name in names:
        common_path = shared_root / "common-agents" / "{}.md".format(name)
        if common_path.is_file() and not common_path.is_symlink():
            if sealed_receipts is not None and common_path in sealed_receipts:
                receipts.append(dict(sealed_receipts[common_path]))
            else:
                receipts.append(_file_receipt(common_path, "shared common {}".format(name)))
    for platform, suffix, adapter_root, active_root in (
        (
            "codex",
            ".toml",
            shared_root / "adapters" / "codex",
            codex_active_root,
        ),
        (
            "claude",
            ".md",
            shared_root / "adapters" / "claude",
            claude_active_root,
        ),
    ):
        for name in names:
            adapter_path = adapter_root / "{}{}".format(name, suffix)
            if adapter_path.is_file() and not adapter_path.is_symlink():
                label = "{} adapter {}".format(platform, name)
                if sealed_receipts is not None and adapter_path in sealed_receipts:
                    receipts.append(dict(sealed_receipts[adapter_path]))
                else:
                    receipts.append(_file_receipt(adapter_path, label))
            active_path = active_root / "{}{}".format(name, suffix)
            if active_path.is_symlink() and active_path.exists():
                if sealed_links is not None and active_path in sealed_links:
                    lexical_target = sealed_links[active_path]
                else:
                    raw_target = Path(os.readlink(str(active_path)))
                    if not raw_target.is_absolute():
                        raw_target = active_path.parent / raw_target
                    lexical_target = Path(os.path.abspath(str(raw_target)))
                receipts.append(
                    {
                        "label": "{} active link {}".format(platform, name),
                        "path": str(active_path),
                        "link_target": str(lexical_target),
                        "target_sha256": hashlib.sha256(
                            (
                                sealed_files[lexical_target]
                                if sealed_files is not None
                                and lexical_target in sealed_files
                                else lexical_target.read_bytes()
                            )
                        ).hexdigest(),
                    }
                )
    return receipts


def build_parser() -> argparse.ArgumentParser:
    parser = StructuredArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--codex-config", type=Path, required=True)
    parser.add_argument("--codex-version", required=True)
    parser.add_argument(
        "--stage",
        choices=("full", "baseline", "builder", "operator"),
        default="full",
        help="audit the exact profile inventory installed through this migration stage",
    )
    parser.add_argument(
        "--audit-scope",
        choices=("live", "target"),
        default="live",
        help="identify whether inputs are the live host or a complete proposed target tree",
    )
    parser.add_argument("--target-manifest", type=Path)
    parser.add_argument("--target-root", type=Path)
    parser.add_argument(
        "--codex-profile", type=_named_path, action="append", required=True
    )
    parser.add_argument("--codex-rules", type=Path, required=True)
    parser.add_argument("--serena-config", type=Path, required=True)
    parser.add_argument(
        "--serena-project", type=_named_path, action="append", required=True
    )
    parser.add_argument("--serena-version", required=True)
    parser.add_argument("--shared-agents-root", type=Path, required=True)
    parser.add_argument("--common-agent-reference-root", type=Path)
    parser.add_argument("--codex-agents-root", type=Path, required=True)
    parser.add_argument("--claude-agents-root", type=Path, required=True)
    parser.add_argument("--reviewer-routing", type=Path, required=True)
    parser.add_argument("--home-root", type=Path, default=Path.home())
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_argv
    errors: List[Issue] = []
    warnings: List[Issue] = []
    receipts: List[Dict[str, Any]] = []
    policy_digest: Optional[str] = None
    codex_version: Optional[str] = None
    serena_version: Optional[str] = None
    validation_stage: Optional[str] = None
    target_inventory_digest: Optional[str] = None
    target_manifest: Optional[Dict[str, Any]] = None
    try:
        args = build_parser().parse_args(raw_argv)
        codex_version = args.codex_version
        serena_version = args.serena_version
        validation_stage = args.stage
        if args.audit_scope == "target" and args.stage != "full":
            raise ValueError("target audit scope requires stage full")
        if args.audit_scope == "target":
            if args.target_manifest is None or args.target_root is None:
                raise ValueError(
                    "target audit scope requires --target-manifest and --target-root"
                )
            if (
                not args.target_root.is_dir()
                or args.target_root.is_symlink()
                or args.target_root.resolve(strict=True) != args.target_root
            ):
                raise ValueError("target root must be a canonical real directory")
            target_manifest_bytes = _sealed_input(
                args.target_manifest, "migration target manifest"
            )[0]
            raw_target_manifest = json.loads(target_manifest_bytes.decode("utf-8"))
            apply_module = _sibling_module("host_migration_apply")
            if not isinstance(raw_target_manifest, dict):
                raise ValueError("migration target manifest root must be a mapping")
            candidate_manifest = json.loads(json.dumps(raw_target_manifest))
            bootstrap_receipts = []
            for record in candidate_manifest.get("targets", []):
                source = Path(record["path"])
                artifact = args.target_root / source.relative_to(source.anchor)
                bootstrap_receipts.append(
                    _sealed_input(
                        artifact,
                        apply_module._expected_target_receipt_label(
                            record.get("label")
                        ).rstrip("*"),
                    )[1]
                )
            bootstrap_inputs = json.dumps(
                bootstrap_receipts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            candidate_manifest["audit"] = {
                "status": "ok",
                "scope": "target",
                "stage": "full",
                "validator": VALIDATOR_ID,
                "policy_sha256": "0" * 64,
                "inputs_sha256": hashlib.sha256(bootstrap_inputs).hexdigest(),
                "target_inventory_sha256": apply_module._target_inventory_sha256(
                    candidate_manifest
                ),
                "target_root": str(args.target_root),
                "inputs": bootstrap_receipts,
            }
            target_manifest = apply_module.validate_manifest(
                candidate_manifest, require_complete_layout=False
            )
            target_inventory_digest = apply_module._target_inventory_sha256(
                target_manifest
            )
        elif args.target_manifest is not None or args.target_root is not None:
            raise ValueError("live audit scope cannot accept target migration inputs")
        if not args.home_root.is_dir() or args.home_root.is_symlink():
            raise ValueError("home root must be a real non-symlink directory")
        input_paths = [
            (args.policy, "host policy"),
            (args.codex_config, "Codex config"),
            (args.codex_rules, "Codex rules"),
            (args.serena_config, "Serena config"),
            (args.reviewer_routing, "reviewer routing"),
        ]
        input_paths.extend(
            (path, "Codex profile {}".format(name))
            for name, path in args.codex_profile
        )
        input_paths.extend(
            (path, "Serena project {}".format(name))
            for name, path in args.serena_project
        )
        sealed_inputs: Dict[Path, bytes] = {}
        for path, label in input_paths:
            data, receipt = _sealed_input(path, label)
            sealed_inputs[path] = data
            receipts.append(receipt)
        policy = json.loads(sealed_inputs[args.policy].decode("utf-8"))
        policy_digest = _canonical_policy_sha256(policy)
        authoritative_policy = _sealed_input(
            AUTHORITATIVE_POLICY_PATH, "authoritative host policy"
        )[0]
        authoritative_policy_value = json.loads(authoritative_policy.decode("utf-8"))
        if policy_digest != _canonical_policy_sha256(authoritative_policy_value):
            raise ValueError("host audit policy differs from the authoritative policy")
        validate_policy_manifest(policy)
        codex_errors, codex_warnings = validate_codex_config(
            load_toml_bytes(sealed_inputs[args.codex_config]),
            policy,
            args.home_root,
        )
        errors.extend(codex_errors)
        warnings.extend(codex_warnings)
        errors.extend(validate_codex_version(args.codex_version, policy))
        profile_paths: Dict[str, Path] = {}
        for name, path in args.codex_profile:
            if name in profile_paths:
                raise ValueError("duplicate Codex profile input: {}".format(name))
            profile_paths[name] = path
        profile_roots = {path.parent for path in profile_paths.values()}
        if len(profile_roots) != 1:
            raise ValueError("Codex profiles must share one canonical directory")
        profile_root = next(iter(profile_roots))
        expected_profile_names = {
            "full": {"scout", "builder", "operator"},
            "baseline": {"scout"},
            "builder": {"scout", "builder"},
            "operator": {"scout", "builder", "operator"},
        }[args.stage]
        for name in ("scout", "builder", "operator"):
            canonical_path = profile_root / "{}.config.toml".format(name)
            if name in profile_paths and profile_paths[name] != canonical_path:
                raise ValueError("Codex profile path is not canonical: {}".format(name))
            if name not in expected_profile_names and (
                canonical_path.exists() or canonical_path.is_symlink()
            ):
                errors.append(
                    _issue(
                        "codex.profile_stage_inventory",
                        str(canonical_path),
                        "profile must remain absent at audit stage {}".format(
                            args.stage
                        ),
                    )
                )
        errors.extend(
            validate_codex_profiles(
                {
                    name: load_toml_bytes(sealed_inputs[path])
                    for name, path in profile_paths.items()
                },
                policy,
                expected_profile_names,
            )
        )
        errors.extend(
            validate_rules(
                parse_prefix_rules_text(
                    sealed_inputs[args.codex_rules].decode("utf-8")
                ),
                policy,
            )
        )
        errors.extend(
            validate_serena_config(
                load_yaml_bytes(
                    sealed_inputs[args.serena_config], str(args.serena_config)
                ),
                policy,
            )
        )
        errors.extend(validate_serena_version(args.serena_version, policy))
        inventory_errors, expected_projects = validate_project_inventory(
            args.serena_project, policy
        )
        errors.extend(inventory_errors)
        for project_id, project_path in args.serena_project:
            errors.extend(
                validate_serena_project(
                    load_yaml_bytes(
                        sealed_inputs[project_path], str(project_path)
                    ),
                    policy,
                    "{}={}".format(project_id, project_path),
                )
            )
        errors.extend(
            validate_project_agent_shadowing(expected_projects, policy)
        )
        routing = json.loads(sealed_inputs[args.reviewer_routing].decode("utf-8"))
        if not isinstance(routing, dict):
            raise ValueError("reviewer routing root must be a mapping")
        errors.extend(validate_reviewer_routing(routing, policy))
        agent_sealed_files: Dict[Path, bytes] = {}
        agent_sealed_links: Dict[Path, Path] = {}
        agent_sealed_receipts: Dict[Path, Dict[str, Any]] = {}
        agent_names = _table(policy.get("agents")).get("canonical_names", [])
        for name in agent_names:
            for agent_path, agent_label in (
                (
                    args.shared_agents_root / "common-agents" / "{}.md".format(name),
                    "shared common {}".format(name),
                ),
                (
                    args.shared_agents_root / "adapters" / "codex" / "{}.toml".format(name),
                    "codex adapter {}".format(name),
                ),
                (
                    args.shared_agents_root / "adapters" / "claude" / "{}.md".format(name),
                    "claude adapter {}".format(name),
                ),
            ):
                if agent_path.is_file() and not agent_path.is_symlink():
                    agent_data, agent_receipt = _sealed_input(
                        agent_path, agent_label
                    )
                    agent_sealed_files[agent_path] = agent_data
                    agent_sealed_receipts[agent_path] = agent_receipt
            for active_root, suffix, platform in (
                (args.codex_agents_root, ".toml", "codex"),
                (args.claude_agents_root, ".md", "claude"),
            ):
                active_path = active_root / "{}{}".format(name, suffix)
                if active_path.is_symlink():
                    agent_sealed_links[active_path] = _sealed_link_target(
                        active_path, "{} active link {}".format(platform, name)
                    )
        agent_errors, agent_warnings = validate_agent_contracts(
            args.shared_agents_root,
            args.codex_agents_root,
            args.claude_agents_root,
            policy,
            args.common_agent_reference_root,
            agent_sealed_files,
            agent_sealed_links,
        )
        errors.extend(agent_errors)
        warnings.extend(agent_warnings)
        receipts.extend(
            _agent_receipts(
                args.shared_agents_root,
                args.codex_agents_root,
                args.claude_agents_root,
                policy,
                agent_sealed_files,
                agent_sealed_links,
                agent_sealed_receipts,
            )
        )
        if target_manifest is not None:
            file_receipts = {
                item["path"]: item
                for item in receipts
                if set(item) == {"label", "path", "mode", "sha256"}
            }
            for record in target_manifest["targets"]:
                source = Path(record["path"])
                artifact = args.target_root / source.relative_to(source.anchor)
                receipt = file_receipts.get(str(artifact))
                if receipt is None:
                    raise ValueError(
                        "migration target was not consumed by the policy audit: {}".format(
                            record["label"]
                        )
                    )
                if (
                    receipt["mode"] != record["target_mode"]
                    or receipt["sha256"] != record["target_sha256"]
                ):
                    raise ValueError(
                        "migration target differs from the audited input: {}".format(
                            record["label"]
                        )
                    )
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as error:
        errors.append(_issue("validator.input_error", "host audit", str(error)))

    canonical_inputs = json.dumps(
        receipts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    inputs_digest = hashlib.sha256(canonical_inputs).hexdigest()
    payload = {
        "schema_version": 1,
        "status": "error" if errors else "ok",
        "errors": errors,
        "warnings": warnings,
        "audit": {
            "validator": VALIDATOR_ID,
            "policy_sha256": policy_digest,
            "inputs_sha256": inputs_digest,
            "codex_version": codex_version,
            "serena_version": serena_version,
            "stage": validation_stage,
            "scope": args.audit_scope if "args" in locals() else None,
            "target_inventory_sha256": target_inventory_digest,
            "target_root": (
                str(args.target_root)
                if "args" in locals() and args.target_root is not None
                else None
            ),
            "inputs": receipts,
        },
    }
    if json_requested:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for issue in errors:
            print(
                "error: {code}: {location}: {message}".format(**issue),
                file=sys.stderr,
            )
        for issue in warnings:
            print("warning: {code}: {location}: {message}".format(**issue))
        if not errors:
            print("ok: host policy passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
