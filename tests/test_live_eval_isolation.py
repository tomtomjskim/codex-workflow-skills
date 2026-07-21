import tempfile
import unittest
from pathlib import Path

from scripts.live_eval.isolation import (
    CliCapabilities,
    EvalConfig,
    Invocation,
    build_invocation,
    preflight_auth,
    preflight_isolation,
)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.codex_home = root / "codex-home"
        self.cwd = root / "neutral-cwd"
        self.schema = root / "response.schema.json"
        self.config = EvalConfig(
            codex_executable=Path("/opt/bin/codex"),
            model="gpt-5.4",
            model_allowlist=("gpt-5.4",),
            codex_home=self.codex_home,
            cwd=self.cwd,
            api_key_env_name="OPENAI_API_KEY",
            api_key="process-local-secret",
            output_schema=self.schema,
            process_env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "aws-secret",
                "TERM": "xterm-256color",
                "HOME": "/neutral-home",
                "OPENAI_API_KEY": "ambient-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
                "DATABASE_URL": "postgres://secret",
                "SAFE_BUT_SECRET_VALUE": "process-local-secret",
            },
        )

    def test_builds_fail_closed_codex_command(self):
        invocation = build_invocation(self.config)

        self.assertEqual(
            invocation.argv[:4],
            ("/opt/bin/codex", "-a", "never", "exec"),
        )
        self.assertIn("--ephemeral", invocation.argv)
        self.assertIn("--ignore-user-config", invocation.argv)
        self.assertIn("--ignore-rules", invocation.argv)
        self.assertIn("read-only", invocation.argv)
        self.assertIn("--output-schema", invocation.argv)
        self.assertEqual(
            invocation.argv[invocation.argv.index("--output-schema") + 1],
            str(self.schema),
        )

    def test_builds_neutral_directories_and_minimal_transport_environment(self):
        invocation = build_invocation(self.config)

        self.assertTrue(invocation.codex_home.is_dir())
        self.assertTrue(invocation.cwd.is_dir())
        self.assertEqual(invocation.transport_env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(
            invocation.transport_env["OPENAI_API_KEY"], "process-local-secret"
        )
        self.assertEqual(invocation.transport_env["PATH"], "/usr/bin:/bin")
        self.assertEqual(invocation.transport_env["LANG"], "C.UTF-8")
        self.assertNotIn("HOME", invocation.transport_env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", invocation.transport_env)
        self.assertNotIn("aws-secret", invocation.transport_env.values())

    def test_agent_environment_excludes_key_name_value_and_credentials(self):
        invocation = build_invocation(self.config)

        self.assertEqual(
            invocation.tool_env,
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "TERM": "xterm-256color",
            },
        )
        self.assertNotIn("OPENAI_API_KEY", invocation.tool_env)
        self.assertNotIn("process-local-secret", invocation.tool_env.values())

    def test_rejects_model_outside_allowlist(self):
        config = EvalConfig(
            **{
                **self.config.__dict__,
                "model": "unapproved-model",
            }
        )

        with self.assertRaisesRegex(ValueError, "allowlist"):
            build_invocation(config)

    def test_oauth_only_is_blocked(self):
        report = preflight_auth(api_key=None, oauth_files_present=True)

        self.assertEqual(report.classification, "blocked_auth")
        self.assertEqual(report.result, "blocked")

    def test_explicit_process_key_is_ready_without_oauth_copy(self):
        report = preflight_auth(
            api_key="process-local-secret", oauth_files_present=True
        )

        self.assertEqual(report.classification, "ready")
        self.assertEqual(report.result, "pass")

    def test_preflight_passes_only_when_all_isolation_features_are_proven(self):
        invocation = build_invocation(self.config)
        capabilities = CliCapabilities(
            supported_flags=frozenset(
                {
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--sandbox",
                    "--output-schema",
                    "-a",
                }
            ),
            tool_env_separation=True,
            network_disabled=True,
            mcp_disabled=True,
            plugins_disabled=True,
            hooks_disabled=True,
            unexpected_skills_absent=True,
            cli_version=(0, 142, 4),
        )

        report = preflight_isolation(invocation, probe=lambda _: capabilities)

        self.assertEqual(report.classification, "ready")
        self.assertEqual(report.result, "pass")
        self.assertEqual(report.missing_guarantees, ())

    def test_preflight_blocks_when_cli_or_environment_proof_is_missing(self):
        invocation = build_invocation(self.config)
        capabilities = CliCapabilities(
            supported_flags=frozenset({"--ephemeral"}),
            tool_env_separation=False,
            network_disabled=False,
            mcp_disabled=False,
            plugins_disabled=False,
            hooks_disabled=False,
            unexpected_skills_absent=False,
            cli_version=(0, 142, 4),
        )

        report = preflight_isolation(invocation, probe=lambda _: capabilities)

        self.assertEqual(report.classification, "blocked_isolation")
        self.assertEqual(report.result, "blocked")
        self.assertIn("tool_env_separation", report.missing_guarantees)
        self.assertIn("network_disabled", report.missing_guarantees)
        self.assertIn("mcp_disabled", report.missing_guarantees)
        self.assertIn("plugins_disabled", report.missing_guarantees)
        self.assertIn("hooks_disabled", report.missing_guarantees)
        self.assertIn("unexpected_skills_absent", report.missing_guarantees)
        self.assertIn("--ignore-rules", report.missing_guarantees)

    def test_preflight_rejects_invocation_missing_required_command_policy(self):
        invocation = build_invocation(self.config)
        tampered = Invocation(
            argv=tuple(item for item in invocation.argv if item != "--ignore-rules"),
            transport_env=invocation.transport_env,
            tool_env=invocation.tool_env,
            codex_home=invocation.codex_home,
            cwd=invocation.cwd,
        )
        capabilities = CliCapabilities(
            supported_flags=frozenset(
                {
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--sandbox",
                    "--output-schema",
                    "-a",
                }
            ),
            tool_env_separation=True,
            network_disabled=True,
            mcp_disabled=True,
            plugins_disabled=True,
            hooks_disabled=True,
            unexpected_skills_absent=True,
            cli_version=(0, 142, 4),
        )

        report = preflight_isolation(tampered, probe=lambda _: capabilities)

        self.assertEqual(report.classification, "blocked_isolation")
        self.assertIn("invocation_policy", report.missing_guarantees)

    def test_preflight_fails_closed_when_probe_errors(self):
        invocation = build_invocation(self.config)

        def failing_probe(_):
            raise OSError("CLI unavailable")

        report = preflight_isolation(invocation, probe=failing_probe)

        self.assertEqual(report.classification, "blocked_isolation")
        self.assertEqual(report.result, "blocked")
        self.assertEqual(report.missing_guarantees, ("cli_feature_probe",))

    def test_preflight_without_injected_capabilities_is_blocked(self):
        invocation = build_invocation(self.config)

        report = preflight_isolation(invocation)

        self.assertEqual(report.classification, "blocked_isolation")
        self.assertEqual(report.result, "blocked")
        self.assertEqual(report.missing_guarantees, ("cli_capability_probe",))


if __name__ == "__main__":
    unittest.main()
