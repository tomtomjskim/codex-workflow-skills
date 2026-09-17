import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import host_migration_apply, validate_host_policy as host_policy
from scripts.validate_host_policy import (
    _load_toml_fallback,
    main,
    parse_prefix_rules,
    validate_codex_config,
    validate_codex_profiles,
    validate_codex_version,
    validate_policy_manifest,
    validate_project_agent_shadowing,
    validate_project_inventory,
    validate_reviewer_routing,
    validate_rules,
    validate_serena_config,
    validate_serena_project,
    validate_serena_version,
)


ROOT = Path(__file__).parents[1]
POLICY = json.loads(
    (ROOT / "policies" / "host-policy.json").read_text(encoding="utf-8")
)


def issue_codes(issues):
    return {issue["code"] for issue in issues}


def expand_home(value, home_root):
    if isinstance(value, str):
        return value.replace("${HOME}", str(home_root))
    if isinstance(value, list):
        return [expand_home(item, home_root) for item in value]
    if isinstance(value, dict):
        return {key: expand_home(item, home_root) for key, item in value.items()}
    return value


def select_cli_profile_stage(args, stage, names):
    selected = ["--stage", stage]
    index = 0
    while index < len(args):
        if args[index] == "--codex-profile":
            value = args[index + 1]
            if value.split("=", 1)[0] in names:
                selected.extend((args[index], value))
            index += 2
            continue
        selected.append(args[index])
        index += 1
    return selected


def cli_profile_paths(args):
    result = {}
    for index, value in enumerate(args):
        if value == "--codex-profile":
            name, raw_path = args[index + 1].split("=", 1)
            result[name] = Path(raw_path)
    return result


def target_audit_args(args, root):
    config = Path(args[args.index("--codex-config") + 1])
    record = {
        "label": "codex-config",
        "path": str(config),
        "pre_state": "present",
        "current_mode": "{:04o}".format(config.stat().st_mode & 0o7777),
        "current_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "target_mode": "{:04o}".format(config.stat().st_mode & 0o7777),
        "target_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    }
    manifest_payload = {
        "schema_version": 3,
        "audit": {},
        "stages": [{"name": "baseline", "labels": ["codex-config"]}],
        "targets": [record],
    }
    manifest = root / "target-manifest.json"
    manifest.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")
    return [
        "--audit-scope",
        "target",
        "--target-manifest",
        str(manifest),
        "--target-root",
        "/",
        *args,
    ], manifest


def safe_codex_config(home_root):
    db_policy = POLICY["codex"]["mcp_servers"]["db-mcp"]
    serena_policy = POLICY["codex"]["mcp_servers"]["serena"]
    return {
        "model": "gpt-5.6-terra",
        "model_reasoning_effort": "medium",
        "service_tier": "default",
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
        "notice": {"hide_full_access_warning": False},
        "projects": {
            str(home_root / "dev" / "owned-repository"): {
                "trust_level": "trusted"
            }
        },
        "mcp_servers": {
            "db-mcp": {
                "command": POLICY["codex"]["mcp_servers"]["db-mcp"]["identity"]["command"],
                "args": [str(home_root / "dev" / "db-mcp" / "dist" / "multi-index.js")],
                "env": expand_home(db_policy["identity"]["env"], home_root),
                "enabled": False,
                "enabled_tools": list(db_policy["enabled_tools"]),
                "default_tools_approval_mode": "prompt",
            },
            "serena": {
                "command": str(home_root / ".local" / "bin" / "serena"),
                "args": ["start-mcp-server", "--context=codex", "--project-from-cwd"],
                "enabled": True,
                "required": True,
                "startup_timeout_sec": 60,
                "tool_timeout_sec": 240,
                "enabled_tools": list(serena_policy["enabled_tools"]),
                "default_tools_approval_mode": "writes",
            },
            "node_repl": {
                "command": "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl",
                "args": [],
                "enabled": False,
                "startup_timeout_sec": 120,
                "env": expand_home(
                    POLICY["codex"]["mcp_servers"]["node_repl"]["identity"]["env"],
                    home_root,
                ),
                "default_tools_approval_mode": "prompt",
            },
            "computer-use": {
                "command": "./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient",
                "args": ["mcp"],
                "cwd": ".",
                "enabled": False,
            },
        },
    }


def safe_profiles():
    return {
        name: {
            **profile["required_values"],
            "mcp_servers": {
                server: {"enabled": enabled}
                for server, enabled in profile["mcp_enabled"].items()
            },
        }
        for name, profile in POLICY["codex"]["profiles"].items()
    }


def write_cli_fixture(root):
    home = root / "home"
    home.mkdir()
    local_policy = json.loads(json.dumps(POLICY))
    owned = home / "dev" / "owned-repository"
    owned.mkdir(parents=True)
    project_files = []
    for project_id in POLICY["serena"]["projects"]:
        project_root = home / "dev" / project_id
        project_file = project_root / ".serena" / "project.yml"
        project_file.parent.mkdir(parents=True)
        project_file.write_text(
            "project_name: {}\nlanguages:\n- python\nread_only: true\n"
            "activation_command: null\n".format(project_id),
            encoding="utf-8",
        )
        project_files.append((project_id, project_file))

    codex_root = home / ".codex"
    (codex_root / "rules").mkdir(parents=True)
    codex_config = codex_root / "config.toml"
    codex_config.write_text(
        'model = "gpt-5.6-terra"\n'
        'model_reasoning_effort = "medium"\n'
        'service_tier = "default"\n'
        'sandbox_mode = "workspace-write"\n'
        'approval_policy = "on-request"\n'
        '[notice]\nhide_full_access_warning = false\n'
        '[projects.{}]\ntrust_level = "trusted"\n'.format(
            json.dumps(str(owned))
        )
        + '[mcp_servers.db-mcp]\ncommand = {}\nargs = [{}]\n'
        'enabled = false\nenabled_tools = {}\n'
        'default_tools_approval_mode = "prompt"\n'.format(
            json.dumps(POLICY["codex"]["mcp_servers"]["db-mcp"]["identity"]["command"]),
            json.dumps(str(home / "dev" / "db-mcp" / "dist" / "multi-index.js")),
            json.dumps(POLICY["codex"]["mcp_servers"]["db-mcp"]["enabled_tools"]),
        )
        + '[mcp_servers.db-mcp.env]\n'
        + "".join(
            "{} = {}\n".format(key, json.dumps(value))
            for key, value in expand_home(
                POLICY["codex"]["mcp_servers"]["db-mcp"]["identity"]["env"],
                home,
            ).items()
        )
        + '[mcp_servers.serena]\ncommand = {}\nargs = {}\nenabled = true\n'
        'required = true\nstartup_timeout_sec = 60\ntool_timeout_sec = 240\n'
        'enabled_tools = {}\ndefault_tools_approval_mode = "writes"\n'.format(
            json.dumps(str(home / ".local" / "bin" / "serena")),
            json.dumps(POLICY["codex"]["mcp_servers"]["serena"]["identity"]["args"]),
            json.dumps(POLICY["codex"]["mcp_servers"]["serena"]["enabled_tools"]),
        )
        + '[mcp_servers.node_repl]\ncommand = {}\nargs = []\nenabled = false\n'
        'startup_timeout_sec = 120\ndefault_tools_approval_mode = "prompt"\n'.format(
            json.dumps(POLICY["codex"]["mcp_servers"]["node_repl"]["identity"]["command"])
        )
        + '[mcp_servers.node_repl.env]\n'
        + "".join(
            "{} = {}\n".format(key, json.dumps(value))
            for key, value in expand_home(
                POLICY["codex"]["mcp_servers"]["node_repl"]["identity"]["env"],
                home,
            ).items()
        )
        + '[mcp_servers.computer-use]\ncommand = {}\nargs = ["mcp"]\n'
        'cwd = "."\nenabled = false\n'.format(
            json.dumps(POLICY["codex"]["mcp_servers"]["computer-use"]["identity"]["command"])
        ),
        encoding="utf-8",
    )
    rules = codex_root / "rules" / "default.rules"
    rules.write_text("# no unconditional allow rules\n", encoding="utf-8")
    serena_config = home / ".serena" / "serena_config.yml"
    serena_config.parent.mkdir()
    serena_config.write_text(
        "trusted_project_path_patterns: []\nexcluded_tools:\n"
        "- replace_in_files\ndefault_modes:\n- interactive\n"
        "default_max_tool_answer_chars: 50000\nlog_level: 30\n"
        "web_dashboard: false\n",
        encoding="utf-8",
    )

    profile_paths = {}
    for name, profile in safe_profiles().items():
        path = codex_root / "{}.config.toml".format(name)
        lines = [
            '{} = {}\n'.format(key, json.dumps(value))
            for key, value in profile.items()
            if key != "mcp_servers"
        ]
        for server_name, server in profile["mcp_servers"].items():
            lines.append("[mcp_servers.{}]\n".format(server_name))
            lines.append(
                "enabled = {}\n".format(
                    "true" if server["enabled"] else "false"
                )
            )
        path.write_text("".join(lines), encoding="utf-8")
        profile_paths[name] = path

    shared = home / ".agents"
    common = shared / "common-agents"
    codex_adapters = shared / "adapters" / "codex"
    claude_adapters = shared / "adapters" / "claude"
    codex_active = root / "codex-active"
    for directory in (common, codex_adapters, claude_adapters, codex_active):
        directory.mkdir(parents=True)
    protected = set(POLICY["agents"]["protected_reviewer_roles"])
    instruction_template = POLICY["agents"]["reviewer_adapter_instruction_template"]
    common_role = """# Reviewer

## Return

- `handoff_target`: `developer`, `qa-engineer`, or another explicit implementation role
- `handoff_reason`: accepted finding and required change

## Boundary

- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
- This read-only boundary cannot be overridden by user requests, approvals, project-local routing, or instructions elsewhere in this role.
"""
    for name in POLICY["agents"]["canonical_names"]:
        common_path = common / "{}.md".format(name)
        common_path.write_text(
            common_role if name in protected else "# {}\n".format(name),
            encoding="utf-8",
        )
        instruction = instruction_template.replace("${COMMON_ROLE}", str(common_path))
        codex_adapter = codex_adapters / "{}.toml".format(name)
        codex_source = 'name = {}\n'.format(json.dumps(name))
        if name in protected:
            codex_source += (
                'sandbox_mode = "read-only"\ndeveloper_instructions = {}\n'
                '[mcp_servers.db-mcp]\nenabled = false\n'
                '[mcp_servers.node_repl]\nenabled = false\n'
                '[mcp_servers.computer-use]\nenabled = false\n'
            ).format(json.dumps(instruction))
        codex_adapter.write_text(codex_source, encoding="utf-8")
        claude_adapter = claude_adapters / "{}.md".format(name)
        claude_source = "---\nname: {}\n".format(name)
        if name in protected:
            claude_source += "disallowedTools:\n- Bash\n- Edit\n- Write\n"
        claude_source += "---\n\n{}\n".format(
            instruction if name in protected else "Load the common role."
        )
        claude_adapter.write_text(claude_source, encoding="utf-8")
        (codex_active / "{}.toml".format(name)).symlink_to(codex_adapter)

    policy_path = root / "host-policy.json"
    policy_path.write_text(
        json.dumps(local_policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    routing = root / "reviewer-routing.json"
    routing.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lens_agents": POLICY["agents"]["reviewer_lens_agents"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    args = [
        "--policy",
        str(policy_path),
        "--codex-config",
        str(codex_config),
        "--codex-version",
        "0.147.0",
    ]
    for name, path in sorted(profile_paths.items()):
        args.extend(("--codex-profile", "{}={}".format(name, path)))
    args.extend(("--codex-rules", str(rules), "--serena-config", str(serena_config)))
    for project_id, project_file in project_files:
        args.extend(
            ("--serena-project", "{}={}".format(project_id, project_file))
        )
    args.extend(
        (
            "--serena-version",
            "1.6.1",
            "--shared-agents-root",
            str(shared),
            "--codex-agents-root",
            str(codex_active),
            "--claude-agents-root",
            str(root / "missing-claude-active"),
            "--reviewer-routing",
            str(routing),
            "--home-root",
            str(home),
            "--json",
        )
    )
    return args, policy_path


class HostPolicyContractTests(unittest.TestCase):
    def test_policy_manifest_uses_supported_schema(self):
        self.assertEqual(2, POLICY["schema_version"])
        validate_policy_manifest(POLICY)

    def test_policy_manifest_rejects_removed_or_malformed_security_fields(self):
        mutations = []
        missing_rules = json.loads(json.dumps(POLICY))
        del missing_rules["rules"]["allowed_allow_prefixes"]
        mutations.append(missing_rules)
        malformed_sandbox = json.loads(json.dumps(POLICY))
        malformed_sandbox["codex"]["allowed_sandbox_modes"] = 1
        mutations.append(malformed_sandbox)
        missing_reviewers = json.loads(json.dumps(POLICY))
        missing_reviewers["agents"]["protected_reviewer_roles"] = []
        mutations.append(missing_reviewers)
        unknown_key = json.loads(json.dumps(POLICY))
        unknown_key["codex"]["silent_bypass"] = True
        mutations.append(unknown_key)
        unsafe_allow = json.loads(json.dumps(POLICY))
        unsafe_allow["rules"]["allowed_allow_prefixes"] = [["safe-wrapper"]]
        mutations.append(unsafe_allow)
        enabled_base_db = json.loads(json.dumps(POLICY))
        enabled_base_db["codex"]["mcp_servers"]["db-mcp"]["required_values"][
            "enabled"
        ] = True
        mutations.append(enabled_base_db)
        enabled_profile_db = json.loads(json.dumps(POLICY))
        enabled_profile_db["codex"]["profiles"]["builder"]["mcp_enabled"][
            "db-mcp"
        ] = True
        mutations.append(enabled_profile_db)
        reviewer_db_override = json.loads(json.dumps(POLICY))
        reviewer_db_override["agents"]["reviewer_runtime"]["codex"][
            "disabled_mcp_servers"
        ].remove("db-mcp")
        mutations.append(reviewer_db_override)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    validate_policy_manifest(mutation)

    def test_safe_codex_fixture_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            home_root = Path(directory)
            (home_root / "dev" / "owned-repository").mkdir(parents=True)

            errors, warnings = validate_codex_config(
                safe_codex_config(home_root), POLICY, home_root
            )

        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_full_access_broad_trust_and_hidden_warning_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            home_root = Path(directory)
            config = safe_codex_config(home_root)
            config["sandbox_mode"] = "danger-full-access"
            config["notice"]["hide_full_access_warning"] = True
            config["projects"][str(home_root)] = {"trust_level": "trusted"}

            errors, _ = validate_codex_config(config, POLICY, home_root)

        self.assertTrue(
            {
                "codex.sandbox_mode",
                "codex.full_access_warning_hidden",
                "codex.broad_trust",
            }.issubset(issue_codes(errors))
        )

    def test_missing_trusted_project_is_a_policy_error(self):
        with tempfile.TemporaryDirectory() as directory:
            home_root = Path(directory)
            (home_root / "dev" / "owned-repository").mkdir(parents=True)
            config = safe_codex_config(home_root)
            config["projects"][str(home_root / "dev" / "future-repository")] = {
                "trust_level": "trusted"
            }

            errors, warnings = validate_codex_config(config, POLICY, home_root)

        self.assertIn("codex.stale_trust", issue_codes(errors))
        self.assertEqual([], warnings)

    def test_missing_allowlist_and_write_tool_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            home_root = Path(directory)
            config = safe_codex_config(home_root)
            config["mcp_servers"]["db-mcp"]["enabled_tools"] = [
                "execute_dev_statement"
            ]
            del config["mcp_servers"]["serena"]["enabled_tools"]

            errors, _ = validate_codex_config(config, POLICY, home_root)

        self.assertIn("codex.mcp_forbidden_tool", issue_codes(errors))
        self.assertIn("codex.mcp_unbounded_tools", issue_codes(errors))
        self.assertIn("codex.mcp_tool_allowlist", issue_codes(errors))

    def test_mcp_inventory_identity_approval_and_global_policy_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            home_root = Path(directory)
            (home_root / "dev" / "owned-repository").mkdir(parents=True)
            config = safe_codex_config(home_root)
            config["approval_policy"] = "never"
            config["mcp_servers"]["unreviewed"] = {
                "command": "malicious",
                "args": [],
            }
            config["mcp_servers"]["serena"]["command"] = "/tmp/replaced"
            config["mcp_servers"]["db-mcp"]["env"]["NODE_OPTIONS"] = "--require payload"
            config["mcp_servers"]["db-mcp"]["tools"] = {
                "schema_check": {"approval_mode": "approve"}
            }

            errors, _ = validate_codex_config(config, POLICY, home_root)

        self.assertTrue(
            {
                "codex.approval_policy",
                "codex.mcp_inventory",
                "codex.mcp_identity",
                "codex.mcp_tool_approval_mode",
            }.issubset(issue_codes(errors))
        )

    def test_node_repl_chrome_instruction_is_identity_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            home_root = Path(directory)
            (home_root / "dev" / "owned-repository").mkdir(parents=True)
            config = safe_codex_config(home_root)
            chrome_instruction = config["mcp_servers"]["node_repl"]["env"][
                "NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME"
            ]
            self.assertIn("Chrome Plugin", chrome_instruction)
            config["mcp_servers"]["node_repl"]["env"][
                "NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME"
            ] = chrome_instruction.replace("Chrome Plugin", "Browser Plugin")

            errors, _ = validate_codex_config(config, POLICY, home_root)

        self.assertIn("codex.mcp_identity", issue_codes(errors))

    def test_profiles_are_exact_and_enforce_capability_deltas(self):
        profiles = safe_profiles()
        self.assertEqual([], validate_codex_profiles(profiles, POLICY))
        self.assertEqual(
            [],
            validate_codex_profiles(
                {"scout": profiles["scout"]}, POLICY, ("scout",)
            ),
        )
        del profiles["scout"]
        profiles["operator"]["mcp_servers"]["node_repl"]["enabled"] = False

        errors = validate_codex_profiles(profiles, POLICY)

        self.assertEqual(
            {"codex.profile_inventory", "codex.profile_mcp_state"},
            issue_codes(errors),
        )

    def test_profiles_reject_extra_mcp_and_capability_bearing_overrides(self):
        profiles = safe_profiles()
        profiles["builder"]["mcp_servers"]["unreviewed"] = {"enabled": True}
        profiles["operator"]["mcp_servers"]["db-mcp"]["command"] = "/tmp/node"
        profiles["scout"]["unreviewed_capability"] = True

        errors = validate_codex_profiles(profiles, POLICY)

        self.assertTrue(
            {
                "codex.profile_contract",
                "codex.profile_mcp_inventory",
                "codex.profile_mcp_override",
            }.issubset(issue_codes(errors))
        )

    def test_codex_version_is_pinned(self):
        self.assertEqual([], validate_codex_version("0.147.0", POLICY))
        self.assertEqual(
            {"codex.unsupported_version"},
            issue_codes(validate_codex_version("0.148.0", POLICY)),
        )

    def test_rules_reject_destructive_and_shell_allow_prefixes(self):
        rules = (
            (1, ["rm", "-rf", ".serena", "extra-target"], "allow"),
            (2, ["/bin/zsh", "-lc", "arbitrary command"], "allow"),
            (3, ["bash", "-cl", "arbitrary command"], "allow"),
            (4, ["sh", "-c", "arbitrary command"], "allow"),
            (5, ["git", "status"], "prompt"),
        )

        errors = validate_rules(rules, POLICY)

        self.assertEqual({"rules.unreviewed_allow_prefix"}, issue_codes(errors))

    def test_rules_reject_wrappers_versioned_interpreters_and_git_clean(self):
        rules = (
            (1, ["env", "rm", "-rf", "target"], "allow"),
            (2, ["python3.13", "-c", "code"], "allow"),
            (3, ["git", "clean", "-fdx"], "allow"),
        )

        self.assertEqual(
            {"rules.unreviewed_allow_prefix"},
            issue_codes(validate_rules(rules, POLICY)),
        )

    def test_rule_parser_preserves_all_trailing_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "default.rules"
            path.write_text(
                'prefix_rule(pattern=["rm", "-rf", ".serena", "extra"], '
                'decision="allow")\n',
                encoding="utf-8",
            )

            parsed = parse_prefix_rules(path)

        self.assertEqual(["rm", "-rf", ".serena", "extra"], parsed[0][1])

    def test_python_39_toml_fallback_parses_policy_relevant_values(self):
        parsed = _load_toml_fallback(
            'sandbox_mode = "workspace-write"\n'
            '[projects."/tmp/repository"]\n'
            'trust_level = "trusted"\n'
            '[mcp_servers.serena]\n'
            'enabled = true\n'
            'enabled_tools = ["find_symbol", "read_memory"]\n'
        )

        self.assertEqual("workspace-write", parsed["sandbox_mode"])
        self.assertEqual(
            "trusted", parsed["projects"]["/tmp/repository"]["trust_level"]
        )
        self.assertEqual(
            ["find_symbol", "read_memory"],
            parsed["mcp_servers"]["serena"]["enabled_tools"],
        )

    def test_python_39_tomli_parses_inline_table_value(self):
        parsed = _load_toml_fallback(
            'disabled_tools = [{ type = "plugin", id = "sample" }]\n'
        )

        self.assertEqual(
            [{"type": "plugin", "id": "sample"}], parsed["disabled_tools"]
        )

    def test_python_39_toml_fallback_parses_multiline_string_array(self):
        parsed = _load_toml_fallback(
            "enabled_tools = [\n"
            '  "find_symbol",\n'
            '  "read_memory", # reviewed read operation\n'
            "]\n"
        )

        self.assertEqual(
            ["find_symbol", "read_memory"], parsed["enabled_tools"]
        )

    def test_python_39_tomli_rejects_invalid_toml(self):
        invalid_sources = (
            "sandbox_mode = workspace-write\n",
            "enabled_tools = [find_symbol, read_memory]\n",
            'sandbox_mode = "read-only"\nsandbox_mode = "workspace-write"\n',
            "not a key value\n",
            'enabled_tools = ["find_symbol"\n',
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    _load_toml_fallback(source)

    def test_serena_global_and_project_fixtures_cover_failure_modes(self):
        safe_global = {
            "trusted_project_path_patterns": [],
            "excluded_tools": ["replace_in_files"],
            "default_modes": ["interactive"],
            "default_max_tool_answer_chars": 50000,
            "log_level": 30,
            "web_dashboard": False,
        }
        safe_project = {
            "project_name": "owned-repository",
            "languages": ["python"],
            "read_only": True,
            "activation_command": None,
        }
        self.assertEqual([], validate_serena_config(safe_global, POLICY))
        self.assertEqual(
            [], validate_serena_project(safe_project, POLICY, "project.yml")
        )

        unsafe_global = dict(safe_global)
        unsafe_global.update(
            {
                "trusted_project_path_patterns": ["**"],
                "excluded_tools": [],
                "default_modes": ["interactive", "editing"],
                "log_level": 20,
                "web_dashboard": True,
            }
        )
        unsafe_project = dict(safe_project)
        unsafe_project.pop("languages")
        unsafe_project.update(
            {
                "language_servers": ["python"],
                "read_only": False,
                "activation_command": "run project command",
            }
        )

        global_codes = issue_codes(validate_serena_config(unsafe_global, POLICY))
        project_codes = issue_codes(
            validate_serena_project(unsafe_project, POLICY, "project.yml")
        )
        self.assertTrue(
            {
                "serena.trust_all",
                "serena.missing_tool_exclusion",
                "serena.unsafe_default_mode",
                "serena.log_level",
                "serena.required_value",
            }.issubset(global_codes)
        )
        self.assertTrue(
            {
                "serena.project_languages",
                "serena.project_forbidden_key",
                "serena.project_required_value",
                "serena.project_activation_command",
            }.issubset(project_codes)
        )

    def test_serena_exact_trust_modes_and_version_are_enforced(self):
        base = {
            "trusted_project_path_patterns": ["/**"],
            "excluded_tools": ["replace_in_files"],
            "default_modes": ["interactive", "planning"],
            "default_max_tool_answer_chars": 50000,
            "log_level": 30,
            "web_dashboard": False,
        }
        self.assertEqual(
            {"serena.trust_all", "serena.unsafe_default_mode"},
            issue_codes(validate_serena_config(base, POLICY)),
        )
        self.assertEqual([], validate_serena_version("1.6.1", POLICY))
        self.assertEqual(
            {"serena.unsupported_version"},
            issue_codes(validate_serena_version("2.0.0", POLICY)),
        )

    def test_project_inventory_and_local_reviewer_shadowing_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = home / "dev" / "frecto_web"
            second = home / "dev" / "frecto_web_gallery"
            for root in (first, second):
                (root / ".serena").mkdir(parents=True)
                (root / ".serena" / "project.yml").write_text("{}\n", encoding="utf-8")
            errors, roots = validate_project_inventory(
                [("project-one", first / ".serena" / "project.yml")], POLICY
            )
            self.assertIn("serena.project_inventory", issue_codes(errors))

            shadow = first / ".codex" / "agents"
            shadow.mkdir(parents=True)
            (shadow / "security-reviewer.toml").write_text("", encoding="utf-8")
            shadow_errors = validate_project_agent_shadowing(roots, POLICY)

        self.assertIn("agents.project_local_shadow", issue_codes(shadow_errors))

    def test_reviewer_routing_must_match_protected_registry(self):
        routing = {
            "lens_agents": dict(POLICY["agents"]["reviewer_lens_agents"])
        }
        self.assertEqual([], validate_reviewer_routing(routing, POLICY))
        routing["lens_agents"]["qa"] = "developer"
        self.assertIn(
            "agents.reviewer_routing",
            issue_codes(validate_reviewer_routing(routing, POLICY)),
        )

    def test_cli_end_to_end_returns_structured_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = write_cli_fixture(Path(directory))
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("ok", payload["status"])
        self.assertEqual([], payload["errors"])
        self.assertTrue(payload["audit"]["policy_sha256"])
        self.assertEqual("validate_host_policy.py@3", payload["audit"]["validator"])
        self.assertRegex(payload["audit"]["inputs_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual("0.147.0", payload["audit"]["codex_version"])
        self.assertEqual("1.6.1", payload["audit"]["serena_version"])
        self.assertEqual("full", payload["audit"]["stage"])
        self.assertEqual("live", payload["audit"]["scope"])
        self.assertIsNone(payload["audit"]["target_inventory_sha256"])
        self.assertGreaterEqual(len(payload["audit"]["inputs"]), 10)
        self.assertNotIn(
            "MCP executable db-mcp",
            {item["label"] for item in payload["audit"]["inputs"]},
        )
        receipt_labels = {item["label"] for item in payload["audit"]["inputs"]}
        self.assertIn("codex adapter security-reviewer", receipt_labels)
        self.assertIn("codex active link security-reviewer", receipt_labels)

    def test_cli_rejects_a_self_consistent_substitute_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            args, policy_path = write_cli_fixture(Path(directory))
            substitute = json.loads(policy_path.read_text(encoding="utf-8"))
            substitute["agents"]["reviewer_runtime"]["codex"][
                "disabled_mcp_servers"
            ].remove("db-mcp")
            policy_path.write_text(json.dumps(substitute) + "\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(1, result.returncode)
        self.assertIn("differs from the authoritative policy", result.stdout)

    def test_audit_validates_the_same_sealed_bytes_recorded_in_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = write_cli_fixture(Path(directory))
            config_path = Path(args[args.index("--codex-config") + 1])
            original_bytes = config_path.read_bytes()
            original = host_policy._sealed_input
            mutated = False

            def mutate_after_seal(path, label):
                nonlocal mutated
                result = original(path, label)
                if label == "Codex config" and not mutated:
                    mutated = True
                    path.write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")
                return result

            output = io.StringIO()
            with mock.patch.object(
                host_policy, "_sealed_input", side_effect=mutate_after_seal
            ), contextlib.redirect_stdout(output):
                exit_code = host_policy.main(args)

        self.assertEqual(0, exit_code, output.getvalue())
        payload = json.loads(output.getvalue())
        receipt = next(
            item for item in payload["audit"]["inputs"] if item["label"] == "Codex config"
        )
        self.assertEqual(
            hashlib.sha256(original_bytes).hexdigest(),
            receipt["sha256"],
        )

    def test_cli_stage_audit_accepts_only_profiles_installed_through_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = write_cli_fixture(Path(directory))
            profile_paths = cli_profile_paths(args)
            profile_paths["builder"].unlink()
            profile_paths["operator"].unlink()
            baseline_args = select_cli_profile_stage(
                args, "baseline", {"scout"}
            )
            baseline = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *baseline_args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        with tempfile.TemporaryDirectory() as directory:
            unexpected_args, _ = write_cli_fixture(Path(directory))
            unexpected_profile_paths = cli_profile_paths(unexpected_args)
            unexpected_profile_paths["operator"].unlink()
            unexpected_baseline_args = select_cli_profile_stage(
                unexpected_args, "baseline", {"scout"}
            )
            wrong_inventory = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *unexpected_baseline_args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, baseline.returncode, baseline.stdout)
        baseline_payload = json.loads(baseline.stdout)
        self.assertEqual("ok", baseline_payload["status"])
        self.assertEqual("baseline", baseline_payload["audit"]["stage"])
        self.assertEqual(1, wrong_inventory.returncode)
        self.assertIn(
            "codex.profile_stage_inventory",
            issue_codes(json.loads(wrong_inventory.stdout)["errors"]),
        )

    def test_target_audit_requires_full_stage_and_inventory_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args, _ = write_cli_fixture(root)
            target_args, manifest = target_audit_args(args, root)
            accepted = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *target_args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            baseline_args = select_cli_profile_stage(
                target_args, "baseline", {"scout"}
            )
            rejected = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *baseline_args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            changed = json.loads(manifest.read_text(encoding="utf-8"))
            changed["targets"][0]["target_sha256"] = "f" * 64
            manifest.write_text(json.dumps(changed) + "\n", encoding="utf-8")
            mismatched = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *target_args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, accepted.returncode, accepted.stdout)
        audit = json.loads(accepted.stdout)["audit"]
        self.assertEqual("target", audit["scope"])
        self.assertEqual("/", audit["target_root"])
        original_manifest = {
            "schema_version": 3,
            "audit": {},
            "stages": [{"name": "baseline", "labels": ["codex-config"]}],
            "targets": [
                {
                    **changed["targets"][0],
                    "target_sha256": changed["targets"][0]["current_sha256"],
                }
            ],
        }
        self.assertEqual(
            host_migration_apply._target_inventory_sha256(original_manifest),
            audit["target_inventory_sha256"],
        )
        self.assertEqual(1, rejected.returncode)
        self.assertIn("target audit scope requires stage full", rejected.stdout)
        self.assertEqual(1, mismatched.returncode)
        self.assertIn("differs from audited input", mismatched.stdout)

    def test_db_mcp_stays_disabled_in_every_profile_until_runtime_read_only_proof(self):
        self.assertFalse(
            POLICY["codex"]["mcp_servers"]["db-mcp"]["required_values"]["enabled"]
        )
        for profile in POLICY["codex"]["profiles"].values():
            self.assertFalse(profile["mcp_enabled"]["db-mcp"])

    def test_cli_argument_and_manifest_errors_are_structured_json(self):
        missing = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "scripts" / "validate_host_policy.py"),
                "--json",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, missing.returncode)
        self.assertEqual("", missing.stderr)
        self.assertEqual(
            {"validator.input_error"},
            issue_codes(json.loads(missing.stdout)["errors"]),
        )

        with tempfile.TemporaryDirectory() as directory:
            args, policy_path = write_cli_fixture(Path(directory))
            malformed = json.loads(policy_path.read_text(encoding="utf-8"))
            malformed["codex"]["allowed_sandbox_modes"] = 1
            policy_path.write_text(json.dumps(malformed), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "validate_host_policy.py"),
                    *args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("error", payload["status"])
        self.assertEqual({"validator.input_error"}, issue_codes(payload["errors"]))


if __name__ == "__main__":
    unittest.main()
