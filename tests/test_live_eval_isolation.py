import hashlib
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.live_eval.isolation import (
    CodexHomeSeal,
    CliCapabilities,
    EvalConfig,
    build_invocation,
    is_credential_like_name,
    preflight_auth,
    preflight_isolation,
    seal_codex_home,
    verify_codex_home_seal,
    toml_string,
)
from scripts.workflow_coordination.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
)


SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
REQUIRED_FLAGS = frozenset(
    {
        "-a",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        "--sandbox",
        "--strict-config",
        "-c",
    }
)


class RaisingCapabilities:
    @property
    def supported_flags(self):
        raise RuntimeError("malformed capability object")


class IsolationTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name).resolve()
        self.temp_root = root / "trusted-temp"
        self.temp_root.mkdir(mode=0o700)
        self.codex = root / "codex"
        self.codex.write_text("fake executable", encoding="utf-8")
        self.codex.chmod(0o700)
        self.config = EvalConfig(
            codex_executable=self.codex,
            model="gpt-5.4",
            model_allowlist=("gpt-5.4",),
            temp_root=self.temp_root,
            api_key="process-local-secret",
        )

    def capabilities(self, invocation, **overrides):
        values = {
            "selected_executable": invocation.executable,
            "selected_executable_identity": invocation.executable_identity,
            "cli_version": (0, 142, 4),
            "supported_flags": REQUIRED_FLAGS,
            "argv_digest": invocation.argv_digest,
            "child_env_policy_id": invocation.child_env_policy_id,
            "child_env_policy_digest": invocation.child_env_policy_digest,
            "non_profile_child_env": True,
            "network_disabled": True,
            "mcp_disabled": True,
            "plugins_disabled": True,
            "hooks_disabled": True,
            "unexpected_skills_absent": True,
        }
        values.update(overrides)
        return CliCapabilities(**values)

    def test_builds_exact_canonical_command_with_enforced_child_environment(self):
        invocation = build_invocation(self.config)
        expected_set = (
            'shell_environment_policy.set={PATH="%s",HOME=%s,TMPDIR=%s}'
            % (
                SAFE_PATH,
                toml_string(str(invocation.codex_home)),
                toml_string(str(invocation.tmpdir)),
            )
        )

        self.assertEqual(
            invocation.argv,
            (
                str(self.codex),
                "-a",
                "never",
                "exec",
                "--json",
                "--strict-config",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--model",
                "gpt-5.4",
                "--output-schema",
                str(invocation.output_schema),
                "-c",
                'shell_environment_policy.inherit="none"',
                "-c",
                "shell_environment_policy.experimental_use_profile=false",
                "-c",
                "shell_environment_policy.ignore_default_excludes=false",
                "-c",
                expected_set,
            ),
        )

    def test_creates_fresh_private_run_paths_under_trusted_root(self):
        invocation = build_invocation(self.config)

        self.assertEqual(invocation.run_dir.parent, self.temp_root)
        self.assertEqual(
            {item.name for item in invocation.run_dir.iterdir()},
            {"codex-home", "cwd", "tmp", "response.schema.json"},
        )
        for path in (
            invocation.run_dir,
            invocation.codex_home,
            invocation.cwd,
            invocation.tmpdir,
        ):
            self.assertTrue(path.is_dir())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(invocation.output_schema.parent, invocation.run_dir)
        self.assertTrue(invocation.output_schema.is_file())
        self.assertEqual(
            stat.S_IMODE(invocation.output_schema.stat().st_mode), 0o600
        )

    def test_writes_canonical_schema_with_digest_and_identity(self):
        schema = {
            "type": "object",
            "required": ["status"],
            "properties": {"status": {"type": "string"}},
        }
        config = replace(self.config, output_schema=schema)
        schema["required"].append("mutated-after-config")

        invocation = build_invocation(config)
        expected = canonical_bytes(
            {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            }
        )

        self.assertEqual(invocation.output_schema.read_bytes(), expected)
        self.assertEqual(
            invocation.schema_digest,
            "sha256:" + hashlib.sha256(expected).hexdigest(),
        )
        metadata = invocation.output_schema.lstat()
        self.assertEqual(
            (invocation.schema_identity.device, invocation.schema_identity.inode),
            (metadata.st_dev, metadata.st_ino),
        )
        with self.assertRaises(TypeError):
            config.output_schema["type"] = "array"

    def test_rejects_noncanonical_schema_before_creating_run_artifacts(self):
        invalid_schemas = (
            {"value": 1.5},
            {"value": "e\u0301"},
            {"value": object()},
        )
        for schema in invalid_schemas:
            with self.subTest(schema=schema):
                with self.assertRaises(CanonicalJSONError):
                    replace(self.config, output_schema=schema)
                self.assertEqual(tuple(self.temp_root.iterdir()), ())

    def test_preflight_blocks_schema_mutation_replacement_symlink_and_extra_file(self):
        mutations = ("content", "replacement", "symlink", "extra")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invocation = build_invocation(self.config)
                if mutation == "content":
                    invocation.output_schema.write_bytes(b'{"type":"array"}')
                elif mutation == "replacement":
                    invocation.output_schema.unlink()
                    invocation.output_schema.write_bytes(b'{"type":"object"}')
                    invocation.output_schema.chmod(0o600)
                elif mutation == "symlink":
                    external = self.temp_root / "external-schema"
                    external.write_bytes(b'{"type":"object"}')
                    invocation.output_schema.unlink()
                    invocation.output_schema.symlink_to(external)
                else:
                    (invocation.run_dir / "extra").write_text(
                        "unexpected", encoding="utf-8"
                    )
                called = []

                report = preflight_isolation(
                    invocation, probe=lambda item: called.append(item)
                )

                self.assertEqual(report.classification, "blocked_isolation")
                self.assertIn("path_integrity", report.missing_guarantees)
                self.assertEqual(called, [])

    def test_schema_write_failure_cleans_only_owned_fresh_run(self):
        with patch(
            "scripts.live_eval.isolation.os.write",
            side_effect=OSError("simulated write failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                build_invocation(self.config)

        self.assertEqual(tuple(self.temp_root.iterdir()), ())

    def test_transport_and_tool_environments_are_exact_and_immutable(self):
        invocation = build_invocation(self.config)
        expected_tool = {
            "PATH": SAFE_PATH,
            "HOME": str(invocation.codex_home),
            "TMPDIR": str(invocation.tmpdir),
        }

        self.assertEqual(dict(invocation.tool_env), expected_tool)
        self.assertEqual(
            dict(invocation.transport_env),
            {
                **expected_tool,
                "CODEX_HOME": str(invocation.codex_home),
                "OPENAI_API_KEY": "process-local-secret",
            },
        )
        with self.assertRaises(TypeError):
            invocation.tool_env["PATH"] = "tampered"
        with self.assertRaises(TypeError):
            invocation.transport_env["TOKEN"] = "tampered"

    def test_secret_fields_and_environment_mappings_are_not_in_repr(self):
        invocation = build_invocation(self.config)

        self.assertNotIn("process-local-secret", repr(self.config))
        self.assertNotIn("process-local-secret", repr(invocation))
        self.assertNotIn("OPENAI_API_KEY", repr(invocation))

    def test_rejects_symlink_or_non_directory_temp_root(self):
        target = self.temp_root.parent / "target"
        target.mkdir()
        symlink = self.temp_root.parent / "linked-root"
        symlink.symlink_to(target, target_is_directory=True)
        file_root = self.temp_root.parent / "file-root"
        file_root.write_text("not a directory", encoding="utf-8")

        for root in (symlink, file_root):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, "temp_root"):
                    build_invocation(replace(self.config, temp_root=root))

    def test_rejects_symlink_in_temp_root_components(self):
        real_parent = self.temp_root.parent / "real-parent"
        real_parent.mkdir()
        nested = real_parent / "nested"
        nested.mkdir()
        linked_parent = self.temp_root.parent / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            build_invocation(replace(self.config, temp_root=linked_parent / "nested"))

    def test_rejects_untrusted_existing_run_contents(self):
        compromised = self.temp_root / "existing-run"
        compromised.mkdir()
        (compromised / "marker").write_text("unexpected", encoding="utf-8")

        with patch(
            "scripts.live_eval.isolation.tempfile.mkdtemp",
            return_value=str(compromised),
        ):
            with self.assertRaisesRegex(ValueError, "fresh run"):
                build_invocation(self.config)

    def test_preflight_rechecks_path_identity_and_emptiness_before_probe(self):
        invocation = build_invocation(self.config)
        (invocation.cwd / "unexpected").write_text("data", encoding="utf-8")
        called = []

        report = preflight_isolation(
            invocation, probe=lambda item: called.append(item)
        )

        self.assertEqual(report.classification, "blocked_isolation")
        self.assertIn("path_integrity", report.missing_guarantees)
        self.assertEqual(called, [])

    def test_preflight_rejects_symlink_swap(self):
        invocation = build_invocation(self.config)
        external = self.temp_root / "external"
        external.mkdir()
        invocation.tmpdir.rmdir()
        invocation.tmpdir.symlink_to(external, target_is_directory=True)

        report = preflight_isolation(
            invocation, probe=lambda item: self.capabilities(item)
        )

        self.assertEqual(report.classification, "blocked_isolation")
        self.assertIn("path_integrity", report.missing_guarantees)

    def test_preflight_requires_matching_executable_policy_and_argv_proof(self):
        invocation = build_invocation(self.config)
        cases = (
            {"selected_executable": self.temp_root / "other"},
            {"selected_executable_identity": (0, 0)},
            {"argv_digest": "sha256:wrong"},
            {"child_env_policy_id": "wrong-policy"},
            {"child_env_policy_digest": "sha256:wrong"},
            {"non_profile_child_env": False},
            {"cli_version": (0, 142, 3)},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                report = preflight_isolation(
                    invocation,
                    probe=lambda item, values=overrides: self.capabilities(
                        item, **values
                    ),
                )
                self.assertEqual(report.classification, "blocked_isolation")

    def test_preflight_blocks_none_malformed_and_raising_capability_results(self):
        invocation = build_invocation(self.config)
        probes = (
            lambda _: None,
            lambda _: object(),
            lambda _: RaisingCapabilities(),
            lambda item: self.capabilities(item, cli_version=("0", 142, 4)),
            lambda item: self.capabilities(item, supported_flags={"--ephemeral"}),
        )
        for probe in probes:
            with self.subTest(probe=probe):
                report = preflight_isolation(invocation, probe=probe)
                self.assertEqual(report.classification, "blocked_isolation")
                self.assertEqual(report.result, "blocked")

    def test_preflight_without_probe_or_with_probe_error_is_blocked(self):
        invocation = build_invocation(self.config)

        missing = preflight_isolation(invocation)

        def failing_probe(_):
            raise OSError("probe unavailable")

        failed = preflight_isolation(invocation, probe=failing_probe)
        self.assertEqual(missing.classification, "blocked_isolation")
        self.assertEqual(failed.classification, "blocked_isolation")

    def test_preflight_accepts_only_untampered_same_invocation_contract(self):
        invocation = build_invocation(self.config)

        report = preflight_isolation(
            invocation, probe=lambda item: self.capabilities(item)
        )

        self.assertEqual(report.classification, "ready")
        self.assertEqual(report.result, "pass")
        self.assertEqual(report.invocation_id, invocation.invocation_id)
        self.assertEqual(report.invocation_instance_id, id(invocation))

    def test_duplicate_unexpected_or_changed_model_argv_is_blocked(self):
        invocation = build_invocation(self.config)
        variants = (
            invocation.argv + ("--ephemeral",),
            invocation.argv + ("--unknown",),
            tuple(
                "other-model" if item == "gpt-5.4" else item
                for item in invocation.argv
            ),
        )
        for argv in variants:
            with self.subTest(argv=argv[-2:]):
                tampered = replace(invocation, argv=argv)
                report = preflight_isolation(
                    tampered, probe=lambda item: self.capabilities(item)
                )
                self.assertEqual(report.classification, "blocked_isolation")
                self.assertIn("invocation_policy", report.missing_guarantees)

    def test_rejects_model_outside_immutable_allowlist(self):
        config = replace(self.config, model_allowlist=["gpt-5.4"])
        self.assertEqual(config.model_allowlist, ("gpt-5.4",))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            build_invocation(replace(config, model="other-model"))

    def test_oauth_only_is_blocked_and_explicit_key_is_ready(self):
        blocked = preflight_auth(api_key=None, oauth_files_present=True)
        ready = preflight_auth(
            api_key="process-local-secret", oauth_files_present=True
        )

        self.assertEqual(blocked.classification, "blocked_auth")
        self.assertEqual(blocked.result, "blocked")
        self.assertEqual(ready.classification, "ready")
        self.assertNotIn("process-local-secret", repr(ready))

    def test_rejects_credential_name_variants_and_embedded_secret_values(self):
        names = (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GITHUB_PAT",
            "PGPASSFILE",
            "PGPASSWORD",
            "AUTHORIZATION",
            "SERVICE_APIKEY",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
            "SESSION_TOKEN",
        )
        self.assertTrue(all(is_credential_like_name(name) for name in names))
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            build_invocation(replace(self.config, api_key_env_name="GITHUB_PAT"))

        secret_root = self.temp_root.parent / "prefix-process-local-secret-suffix"
        secret_root.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "secret"):
            build_invocation(replace(self.config, temp_root=secret_root))

    def test_toml_string_escapes_untrusted_path_text(self):
        self.assertEqual(toml_string('a"b\\c\n'), '"a\\"b\\\\c\\n"')

    def test_sealed_codex_home_is_accepted_without_weakening_empty_default(self):
        invocation = build_invocation(self.config)
        skills = invocation.codex_home / "skills"
        skills.mkdir(mode=0o700)
        (skills / "SKILL.md").write_text("sealed\n", encoding="utf-8")
        seal = seal_codex_home(invocation.codex_home, ("skills",))

        self.assertIsInstance(seal, CodexHomeSeal)
        report = preflight_isolation(
            invocation,
            probe=lambda item: self.capabilities(item),
            expected_codex_home_seal=seal,
        )

        self.assertEqual(report.classification, "ready")
        self.assertTrue(verify_codex_home_seal(invocation, seal))

        unsealed = build_invocation(self.config)
        (unsealed.codex_home / "unexpected").write_text("x", encoding="utf-8")
        blocked = preflight_isolation(
            unsealed, probe=lambda item: self.capabilities(item)
        )
        self.assertEqual(blocked.classification, "blocked_isolation")

    def test_sealed_codex_home_mutation_is_blocked_before_consumption(self):
        invocation = build_invocation(self.config)
        installed = invocation.codex_home / "skills"
        installed.mkdir(mode=0o700)
        policy = installed / "SKILL.md"
        policy.write_text("original\n", encoding="utf-8")
        seal = seal_codex_home(invocation.codex_home, ("skills",))

        policy.write_text("tampered\n", encoding="utf-8")

        report = preflight_isolation(
            invocation,
            probe=lambda item: self.capabilities(item),
            expected_codex_home_seal=seal,
        )
        self.assertEqual(report.classification, "blocked_isolation")
        self.assertFalse(verify_codex_home_seal(invocation, seal))


if __name__ == "__main__":
    unittest.main()
