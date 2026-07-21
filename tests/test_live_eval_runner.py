import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scripts.live_eval.isolation import CliCapabilities
from scripts.run_live_eval import (
    EvalRequest,
    InvocationFailure,
    InvocationTimeout,
    ProcessOutput,
    main,
    run_eval,
)


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


class FakeCodex:
    def __init__(self, outcomes=(), mutate_after_probe=False):
        self.outcomes = list(outcomes)
        self.mutate_after_probe = mutate_after_probe
        self.probed = []
        self.invoked = []

    def probe(self, invocation):
        self.probed.append(invocation)
        capabilities = CliCapabilities(
            selected_executable=invocation.executable,
            selected_executable_identity=invocation.executable_identity,
            cli_version=(0, 142, 4),
            supported_flags=REQUIRED_FLAGS,
            argv_digest=invocation.argv_digest,
            child_env_policy_id=invocation.child_env_policy_id,
            child_env_policy_digest=invocation.child_env_policy_digest,
            non_profile_child_env=True,
            network_disabled=True,
            mcp_disabled=True,
            plugins_disabled=True,
            hooks_disabled=True,
            unexpected_skills_absent=True,
        )
        if self.mutate_after_probe:
            target = invocation.codex_home / "skills" / "workflow" / "SKILL.md"
            target.chmod(0o644)
            target.write_text("tampered after preflight\n", encoding="utf-8")
        return capabilities

    def invoke(self, invocation, prompt, timeout_seconds):
        self.invoked.append((invocation, prompt, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def final_output(response):
    return ProcessOutput(
        (
            json.dumps(
                {"type": "final_response", "response": response},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n",
        )
    )


def codex_json_output(response):
    return ProcessOutput(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(response, separators=(",", ":")),
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n",
        )
    )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.runtime.chmod(0o700)
        self.executable = self.root / "codex"
        self.executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.scenarios = (
            Path(__file__).resolve().parent / "live-eval-scenarios.json"
        )
        self._create_clean_checkout_fixture()

    def _git(self, *arguments):
        return subprocess.run(
            ("git",) + arguments,
            cwd=str(self.repo),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _create_clean_checkout_fixture(self):
        plugin = self.repo / ".codex-plugin"
        plugin.mkdir()
        (plugin / "plugin.json").write_text(
            '{"name":"fixture","skills":"./skills/","version":"1.0.0"}\n',
            encoding="utf-8",
        )
        for name in (
            "adversarial-review-loop",
            "workflow",
            "workflow-intake",
        ):
            skill = self.repo / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: {}\n---\n".format(name), encoding="utf-8"
            )
        self._git("init", "-q")
        self._git("config", "user.email", "runner@example.invalid")
        self._git("config", "user.name", "Runner Test")
        self._git("add", ".")
        self._git("commit", "-qm", "fixture")

    def request(self, *, scenario_ids=("WI-MISSING-REPO",), **overrides):
        values = {
            "scenario_ids": scenario_ids,
            "model": "gpt-5.6-sol",
            "repo_root": self.repo,
            "scenario_path": self.scenarios,
            "temp_root": self.runtime,
            "codex_executable": self.executable,
            "api_key": "process-local-secret",
        }
        values.update(overrides)
        return EvalRequest.targeted(**values)

    def test_dry_run_is_deterministic_planning_only_without_runtime_dependencies(self):
        fake = FakeCodex()

        result = run_eval(
            EvalRequest.dry_run(tags=("workflow-intake",), model="gpt-5.6-sol"),
            fake,
        )

        self.assertEqual(result.status, "preflight_only")
        self.assertEqual(result.verification_result, "not_run")
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(len(result.manifest.scenario_ids), 3)
        self.assertEqual(fake.probed, [])
        self.assertEqual(fake.invoked, [])

    def test_planning_preflight_rejects_unknown_selection_and_model(self):
        fake = FakeCodex()

        with self.assertRaisesRegex(ValueError, "selected"):
            run_eval(EvalRequest.dry_run(tags=("not-a-real-tag",)), fake)
        with self.assertRaisesRegex(ValueError, "allowlist"):
            run_eval(
                EvalRequest.dry_run(
                    tags=("workflow-intake",), model="unsupported-model"
                ),
                fake,
            )

        self.assertEqual(fake.probed, [])
        self.assertEqual(fake.invoked, [])

    def test_release_suite_dry_run_selects_all_without_model_calls(self):
        fake = FakeCodex()

        result = run_eval(EvalRequest.dry_run(release_suite=True), fake)

        self.assertGreater(len(result.manifest.scenario_ids), 3)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(fake.invoked, [])

    def test_live_run_uses_same_sealed_invocation_and_asserts_redacted_response(self):
        fake = FakeCodex(
            [final_output({"next_step": "ask", "autonomy_level": "L0"})]
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "pass")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.attempts, 1)
        self.assertIs(fake.probed[0], fake.invoked[0][0])
        scenario = result.scenarios[0]
        self.assertTrue(scenario.assertion_report.passed)
        self.assertTrue(scenario.artifact_path.is_file())
        self.assertEqual(stat.S_IMODE(scenario.artifact_path.stat().st_mode), 0o600)
        self.assertTrue(result.manifest.checkout_tree_hash)

    def test_parses_codex_json_agent_message_as_final_structured_response(self):
        fake = FakeCodex(
            [codex_json_output({"next_step": "ask", "autonomy_level": "L0"})]
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "pass")
        self.assertEqual(result.attempts, 1)

    def test_infrastructure_failure_retries_once_and_never_becomes_pass(self):
        fake = FakeCodex(
            [InvocationFailure("transport"), InvocationFailure("transport")]
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "blocked")
        self.assertEqual(result.status, "blocked_infrastructure")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(len(fake.invoked), 2)

    def test_retry_success_does_not_erase_infrastructure_failure(self):
        fake = FakeCodex(
            [
                InvocationFailure("transport"),
                final_output({"next_step": "ask", "autonomy_level": "L0"}),
            ]
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "blocked")
        self.assertEqual(result.status, "blocked_infrastructure")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.model_calls, 2)

    def test_assertion_failure_is_not_retried(self):
        fake = FakeCodex([final_output({"next_step": "implemented"})])

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "fail")
        self.assertEqual(result.status, "failed_assertion")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(fake.invoked), 1)

    def test_post_preflight_checkout_mutation_blocks_before_invoke(self):
        fake = FakeCodex(mutate_after_probe=True)

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "blocked")
        self.assertEqual(result.status, "blocked_isolation")
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(fake.invoked, [])

    def test_timeout_blocks_without_retry(self):
        fake = FakeCodex([InvocationTimeout()])

        result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_timeout")
        self.assertEqual(result.verification_result, "blocked")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(fake.invoked), 1)

    def test_results_and_errors_never_repr_secret_values(self):
        secret = "must-not-appear-secret"
        fake = FakeCodex([RuntimeError(secret), RuntimeError(secret)])

        result = run_eval(self.request(api_key=secret), fake)

        self.assertNotIn(secret, repr(self.request(api_key=secret)))
        self.assertNotIn(secret, repr(result))

    def test_cli_dry_run_does_not_require_api_key_or_codex_process(self):
        output = io.StringIO()
        environment = dict(os.environ)
        environment.pop("OPENAI_API_KEY", None)
        with mock.patch.dict(os.environ, environment, clear=True):
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--tags",
                        "workflow-intake",
                        "--model",
                        "gpt-5.6-sol",
                        "--dry-run",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "preflight_only")
        self.assertEqual(payload["model_calls"], 0)

    def test_script_entrypoint_runs_from_repository_root(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            (
                "python3",
                "scripts/run_live_eval.py",
                "--tags",
                "workflow-intake",
                "--model",
                "gpt-5.6-sol",
                "--dry-run",
            ),
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "preflight_only")

    def test_release_gate_and_docs_distinguish_deterministic_from_live_evidence(self):
        root = Path(__file__).resolve().parents[1]
        validation = (root / "scripts" / "validate_repo.sh").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        report = (root / "docs" / "forward-test-report.md").read_text(encoding="utf-8")

        self.assertIn("require_file scripts/run_live_eval.py", validation)
        self.assertIn("test_live_eval_*.py", validation)
        self.assertIn("preflight_only", readme)
        self.assertIn("model_calls=0", readme)
        self.assertIn("live model execution: not_run", report)


if __name__ == "__main__":
    unittest.main()
