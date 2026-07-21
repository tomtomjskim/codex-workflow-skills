import gc
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import scripts.run_live_eval as live_runner
from scripts.live_eval.isolation import CliCapabilities
from scripts.live_eval.isolation import EvalConfig, build_invocation
from scripts.run_live_eval import (
    EvalResult,
    EvalRequest,
    InvocationTimeout,
    OutputLimit,
    OutputProtocolError,
    ProcessOutput,
    RetryableTransportFailure,
    RunManifest,
    SubprocessCodex,
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
    def __init__(
        self,
        outcomes=(),
        mutate_after_probe=False,
        mutate_on_failure=None,
        probe_readiness=(),
    ):
        self.outcomes = list(outcomes)
        self.mutate_after_probe = mutate_after_probe
        self.mutate_on_failure = mutate_on_failure
        self.probe_readiness = list(probe_readiness)
        self.probed = []
        self.invoked = []

    def probe(self, invocation):
        self.probed.append(invocation)
        ready = self.probe_readiness.pop(0) if self.probe_readiness else True
        capabilities = CliCapabilities(
            selected_executable=invocation.executable,
            selected_executable_identity=invocation.executable_identity,
            cli_version=(0, 142, 4),
            supported_flags=REQUIRED_FLAGS,
            argv_digest=invocation.argv_digest,
            child_env_policy_id=invocation.child_env_policy_id,
            child_env_policy_digest=invocation.child_env_policy_digest,
            non_profile_child_env=True,
            network_disabled=ready,
            mcp_disabled=ready,
            plugins_disabled=ready,
            hooks_disabled=ready,
            unexpected_skills_absent=ready,
        )
        if self.mutate_after_probe:
            target = invocation.codex_home / "skills" / "workflow" / "SKILL.md"
            target.chmod(0o644)
            target.write_text("tampered after preflight\n", encoding="utf-8")
        return capabilities

    def invoke(
        self,
        invocation,
        prompt,
        timeout_seconds,
        *,
        max_stdin_bytes,
        max_stdout_bytes,
        max_stderr_bytes,
    ):
        self.invoked.append(
            (
                invocation,
                prompt,
                timeout_seconds,
                max_stdin_bytes,
                max_stdout_bytes,
                max_stderr_bytes,
            )
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            if self.mutate_on_failure is not None:
                self.mutate_on_failure(invocation)
            raise outcome
        return outcome


class FakePopen:
    outputs = []
    calls = []
    next_pid = 4100

    def __init__(self, argv, **kwargs):
        specification = self.outputs.pop(0)
        self.argv = tuple(argv)
        self.kwargs = kwargs
        self.stdout = io.BytesIO(specification.get("stdout", b""))
        self.stderr = io.BytesIO(specification.get("stderr", b""))
        self._child_stdin_read_fd = None
        if specification.get("pipe_stdin"):
            self._child_stdin_read_fd, write_fd = os.pipe()
            self.stdin = os.fdopen(write_fd, "wb", buffering=0)
        elif "stdin" in specification:
            self.stdin = specification["stdin"]
        else:
            self.stdin = tempfile.TemporaryFile("w+b")
        self.stdin_fd = self.stdin.fileno()
        self.returncode = specification.get("returncode", 0)
        self.pid = self.next_pid
        type(self).next_pid += 1
        type(self).calls.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        self.close_child_stdin()
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def close_child_stdin(self):
        if self._child_stdin_read_fd is None:
            return
        descriptor = self._child_stdin_read_fd
        self._child_stdin_read_fd = None
        try:
            os.close(descriptor)
        except OSError:
            pass


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

    def invocation(self):
        return build_invocation(
            EvalConfig(
                codex_executable=self.executable,
                model="gpt-5.6-sol",
                model_allowlist=("gpt-5.6-sol",),
                temp_root=self.runtime,
                api_key="process-local-secret",
            )
        )

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
        self.assertEqual(scenario.retention, "retained_redacted")
        self.assertTrue(scenario.manual_cleanup_required)

    def test_parses_codex_json_agent_message_as_final_structured_response(self):
        fake = FakeCodex(
            [codex_json_output({"next_step": "ask", "autonomy_level": "L0"})]
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.verification_result, "pass")
        self.assertEqual(result.attempts, 1)

    def test_infrastructure_failure_retries_once_and_never_becomes_pass(self):
        fake = FakeCodex(
            [RetryableTransportFailure(), RetryableTransportFailure()]
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
                RetryableTransportFailure(),
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
        self.assertFalse(fake.probed[0].run_dir.exists())

    def test_full_consumption_recheck_blocks_schema_cwd_tmp_and_argv_mutations(self):
        def mutate_schema(invocation):
            invocation.output_schema.write_text("{}\n", encoding="utf-8")

        def mutate_cwd(invocation):
            (invocation.cwd / "changed").write_text("x", encoding="utf-8")

        def mutate_tmp(invocation):
            (invocation.tmpdir / "changed").write_text("x", encoding="utf-8")

        def mutate_argv(invocation):
            object.__setattr__(invocation, "argv", invocation.argv + ("--changed",))

        for name, mutation in (
            ("schema", mutate_schema),
            ("cwd", mutate_cwd),
            ("tmp", mutate_tmp),
            ("argv", mutate_argv),
        ):
            with self.subTest(name=name):
                fake = FakeCodex()
                original_probe = fake.probe

                def probe_then_mutate(invocation, callback=mutation):
                    capabilities = original_probe(invocation)
                    callback(invocation)
                    return capabilities

                fake.probe = probe_then_mutate
                result = run_eval(self.request(), fake)
                self.assertEqual(result.status, "blocked_isolation")
                self.assertEqual(result.attempts, 0)
                self.assertEqual(fake.invoked, [])

    def test_retry_rechecks_full_isolation_after_first_transport_failure(self):
        fake = FakeCodex(
            [RetryableTransportFailure(), final_output({"next_step": "ask", "autonomy_level": "L0"})],
            mutate_on_failure=lambda invocation: (invocation.cwd / "changed").write_text(
                "x", encoding="utf-8"
            ),
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_isolation")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(len(fake.invoked), 1)

    def test_budget_lease_entry_is_followed_by_final_isolation_recheck(self):
        fake = FakeCodex(
            [final_output({"next_step": "ask", "autonomy_level": "L0"})]
        )

        class MutatingLease:
            def __enter__(self):
                (fake.probed[0].cwd / "changed-during-lease").write_text(
                    "x", encoding="utf-8"
                )

            def __exit__(self, *_args):
                return False

        with mock.patch(
            "scripts.run_live_eval.Budget.acquire_call", return_value=MutatingLease()
        ):
            result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_isolation")
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(fake.invoked, [])

    def test_lease_time_probe_must_return_new_ready_capabilities(self):
        fake = FakeCodex(
            [final_output({"next_step": "ask", "autonomy_level": "L0"})],
            probe_readiness=(True, False),
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_isolation")
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(fake.invoked, [])
        self.assertEqual(len(fake.probed), 2)

    def test_retry_time_probe_failure_blocks_second_invoke(self):
        fake = FakeCodex(
            [
                RetryableTransportFailure(),
                final_output({"next_step": "ask", "autonomy_level": "L0"}),
            ],
            probe_readiness=(True, True, False),
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_isolation")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(len(fake.invoked), 1)
        self.assertEqual(len(fake.probed), 3)

    def test_timeout_blocks_without_retry(self):
        fake = FakeCodex([InvocationTimeout()])

        result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_timeout")
        self.assertEqual(result.verification_result, "blocked")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(fake.invoked), 1)

    def test_only_transport_failure_is_retryable(self):
        cases = (
            (OutputLimit(), "blocked_output_limit"),
            (OutputProtocolError(), "blocked_output_protocol"),
            (RuntimeError("secret-internal"), "blocked_internal"),
        )
        for error, status in cases:
            with self.subTest(status=status):
                fake = FakeCodex([error, final_output({"next_step": "ask", "autonomy_level": "L0"})])
                result = run_eval(self.request(), fake)
                self.assertEqual(result.status, status)
                self.assertEqual(result.attempts, 1)
                self.assertEqual(len(fake.invoked), 1)

    def test_completed_nonzero_process_exit_is_nonretryable(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"returncode": 1, "stderr": b"secret-auth-error"}]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen):
            with self.assertRaisesRegex(RuntimeError, "process execution failed") as raised:
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    10,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        self.assertNotIn("secret-auth-error", repr(raised.exception))
        self.assertNotIsInstance(raised.exception, RetryableTransportFailure)

    def test_process_execution_failure_blocks_after_one_attempt(self):
        failure_type = getattr(live_runner, "ProcessExecutionFailure", RuntimeError)
        fake = FakeCodex(
            [
                failure_type(),
                final_output({"next_step": "ask", "autonomy_level": "L0"}),
            ]
        )

        result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_process")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(len(fake.invoked), 1)

    def test_popen_spawn_oserror_is_sanitized_retryable_transport_failure(self):
        invocation = self.invocation()
        with mock.patch(
            "scripts.run_live_eval.subprocess.Popen",
            side_effect=OSError("secret-spawn-detail"),
        ):
            with self.assertRaises(RetryableTransportFailure) as raised:
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    10,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        self.assertNotIn("secret-spawn-detail", repr(raised.exception))

    def test_probe_parse_failure_is_not_retryable_transport_failure(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"stdout": b"not-a-version"}]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen):
            with self.assertRaises(live_runner.ProcessExecutionFailure) as raised:
                SubprocessCodex().probe(invocation)

        self.assertNotIsInstance(raised.exception, RetryableTransportFailure)

    def test_duplicate_or_malformed_terminal_response_is_protocol_block(self):
        duplicate = ProcessOutput(
            final_output({"next_step": "ask", "autonomy_level": "L0"}).events
            + codex_json_output({"next_step": "ask", "autonomy_level": "L0"}).events
        )
        malformed = ProcessOutput((b'{"type":"item.completed","item":{}}\n',))
        for output in (duplicate, malformed):
            fake = FakeCodex([output])
            result = run_eval(self.request(), fake)
            self.assertEqual(result.status, "blocked_output_protocol")
            self.assertEqual(result.attempts, 1)
            self.assertEqual(len(fake.invoked), 1)

    def test_budget_denial_does_not_increment_attempt_or_model_call(self):
        fake = FakeCodex([final_output({"next_step": "ask", "autonomy_level": "L0"})])
        from scripts.live_eval.budget import BudgetDecision, BudgetExceeded

        with mock.patch(
            "scripts.run_live_eval.Budget.acquire_call",
            side_effect=BudgetExceeded(BudgetDecision.BLOCKED_BUDGET),
        ):
            result = run_eval(self.request(), fake)

        self.assertEqual(result.status, "blocked_budget")
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(fake.invoked, [])

    def test_production_probe_never_synthesizes_unproven_capabilities_or_key_env(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [
            {"stdout": b"codex-cli 0.142.4\n"},
            {"stdout": " ".join(REQUIRED_FLAGS).encode("utf-8")},
            {"stdout": " ".join(REQUIRED_FLAGS).encode("utf-8")},
        ]
        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.subprocess.run", side_effect=AssertionError("run forbidden")
        ):
            capabilities = SubprocessCodex().probe(invocation)

        self.assertFalse(capabilities.network_disabled)
        self.assertFalse(capabilities.mcp_disabled)
        self.assertFalse(capabilities.plugins_disabled)
        self.assertFalse(capabilities.hooks_disabled)
        self.assertFalse(capabilities.unexpected_skills_absent)
        self.assertEqual(len(FakePopen.calls), 3)
        self.assertTrue(
            all("OPENAI_API_KEY" not in process.kwargs["env"] for process in FakePopen.calls)
        )
        self.assertTrue(all(process.kwargs["start_new_session"] for process in FakePopen.calls))

    def test_subprocess_output_is_bounded_and_raw_stderr_is_never_exposed(self):
        invocation = self.invocation()
        secret = b"secret-stderr-value"
        FakePopen.calls = []
        FakePopen.outputs = [
            {"stdout": b"x" * 33, "stderr": secret, "returncode": None}
        ]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.subprocess.run", side_effect=AssertionError("run forbidden")
        ), mock.patch("scripts.run_live_eval.os.killpg") as kill_group:
            with self.assertRaises(OutputLimit) as raised:
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    10,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        self.assertNotIn(secret.decode("utf-8"), repr(raised.exception))
        self.assertTrue(FakePopen.calls[0].kwargs["start_new_session"])
        kill_group.assert_called_once_with(FakePopen.calls[0].pid, signal.SIGTERM)

    def test_output_overflow_kills_process_group_after_parent_exit(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"stdout": b"x" * 33, "returncode": 0}]

        with mock.patch(
            "scripts.run_live_eval.subprocess.Popen", FakePopen
        ), mock.patch("scripts.run_live_eval.os.killpg") as kill_group:
            with self.assertRaises(OutputLimit):
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    10,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        kill_group.assert_called_once_with(FakePopen.calls[0].pid, signal.SIGTERM)

    def test_stdin_policy_rejects_zero_and_oversized_limits(self):
        for limit in (0, 1024 * 1024 + 1):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "stdin"):
                    run_eval(self.request(max_stdin_bytes=limit), FakeCodex())

    def test_timeout_starts_before_large_stdin_write_and_kills_group(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"pipe_stdin": True, "returncode": None}]
        raised = []

        def invoke():
            try:
                SubprocessCodex().invoke(
                    invocation,
                    "x" * (1024 * 1024),
                    0.05,
                    max_stdin_bytes=1024 * 1024,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )
            except BaseException as error:
                raised.append(error)

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.os.killpg"
        ) as kill_group:
            worker = threading.Thread(target=invoke, daemon=True)
            worker.start()
            worker.join(timeout=0.5)
            was_still_blocked = worker.is_alive()
            if was_still_blocked:
                FakePopen.calls[0].close_child_stdin()
                worker.join(timeout=1)

        self.assertFalse(was_still_blocked)
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], InvocationTimeout)
        kill_group.assert_called_once_with(FakePopen.calls[0].pid, signal.SIGTERM)

    def test_stdin_writer_error_is_sanitized_nonretryable_process_failure(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"returncode": None}]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.os.killpg"
        ), mock.patch(
            "scripts.run_live_eval.os.write",
            side_effect=RuntimeError("secret-writer-detail"),
        ):
            with self.assertRaises(live_runner.ProcessExecutionFailure) as raised:
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    1,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        self.assertNotIn("secret-writer-detail", repr(raised.exception))

    def test_normal_stdin_writer_owns_dup_and_never_closes_reused_fd(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"returncode": 0}]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.os.dup", wraps=os.dup
        ) as duplicate:
            SubprocessCodex().invoke(
                invocation,
                "prompt",
                1,
                max_stdin_bytes=32,
                max_stdout_bytes=32,
                max_stderr_bytes=32,
            )

        process = FakePopen.calls[0]
        original_closed = process.stdin.closed
        unrelated_read, unrelated_write = os.pipe()
        FakePopen.calls.clear()
        del process
        gc.collect()
        unrelated_valid = True
        try:
            os.write(unrelated_write, b"x")
            unrelated_valid = os.read(unrelated_read, 1) == b"x"
        except OSError:
            unrelated_valid = False
        finally:
            for descriptor in (unrelated_read, unrelated_write):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        self.assertTrue(original_closed)
        duplicate.assert_called_once()
        self.assertTrue(unrelated_valid)

    def test_timeout_cleanup_never_closes_fd_reused_after_original_stream_close(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"pipe_stdin": True, "returncode": None}]
        raised = []

        def invoke():
            try:
                SubprocessCodex().invoke(
                    invocation,
                    "x" * (1024 * 1024),
                    0.2,
                    max_stdin_bytes=1024 * 1024,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )
            except BaseException as error:
                raised.append(error)

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.os.killpg"
        ), mock.patch("scripts.run_live_eval.os.dup", wraps=os.dup) as duplicate:
            worker = threading.Thread(target=invoke, daemon=True)
            worker.start()
            deadline = time.monotonic() + 0.1
            while not FakePopen.calls and time.monotonic() < deadline:
                time.sleep(0.005)
            process = FakePopen.calls[0]
            while not process.stdin.closed and time.monotonic() < deadline:
                time.sleep(0.005)
            original_closed = process.stdin.closed
            unrelated_read, unrelated_write = os.pipe()
            reused_original_fd = process.stdin_fd in (unrelated_read, unrelated_write)
            worker.join(timeout=1)

        FakePopen.calls.clear()
        del process
        gc.collect()
        unrelated_valid = True
        try:
            os.write(unrelated_write, b"x")
            unrelated_valid = os.read(unrelated_read, 1) == b"x"
        except OSError:
            unrelated_valid = False
        finally:
            for descriptor in (unrelated_read, unrelated_write):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        self.assertTrue(original_closed)
        self.assertTrue(reused_original_fd)
        duplicate.assert_called_once()
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], InvocationTimeout)
        self.assertTrue(unrelated_valid)

    def test_stdin_dup_failure_is_sanitized_and_cleans_process_group(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"returncode": None}]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.os.dup", side_effect=OSError("secret-dup-detail")
        ), mock.patch("scripts.run_live_eval.os.killpg") as kill_group:
            with self.assertRaises(live_runner.ProcessExecutionFailure) as raised:
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    0.05,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        self.assertNotIn("secret-dup-detail", repr(raised.exception))
        self.assertTrue(FakePopen.calls[0].stdin.closed)
        kill_group.assert_called_once_with(FakePopen.calls[0].pid, signal.SIGTERM)

    def test_writer_thread_start_failure_never_creates_main_owned_dup(self):
        invocation = self.invocation()
        FakePopen.calls = []
        FakePopen.outputs = [{"returncode": None}]

        with mock.patch("scripts.run_live_eval.subprocess.Popen", FakePopen), mock.patch(
            "scripts.run_live_eval.threading.Thread.start",
            side_effect=RuntimeError("secret-thread-detail"),
        ), mock.patch("scripts.run_live_eval.os.dup", wraps=os.dup) as duplicate, mock.patch(
            "scripts.run_live_eval.os.killpg"
        ) as kill_group:
            with self.assertRaises(live_runner.ProcessExecutionFailure) as raised:
                SubprocessCodex().invoke(
                    invocation,
                    "prompt",
                    0.05,
                    max_stdin_bytes=32,
                    max_stdout_bytes=32,
                    max_stderr_bytes=32,
                )

        self.assertNotIn("secret-thread-detail", repr(raised.exception))
        duplicate.assert_not_called()
        self.assertTrue(FakePopen.calls[0].stdin.closed)
        kill_group.assert_called_once_with(FakePopen.calls[0].pid, signal.SIGTERM)

    def test_original_stdin_close_failure_is_sanitized_and_cleans_group(self):
        class CloseFailingStream:
            def __init__(self):
                self.backing = tempfile.TemporaryFile("w+b")

            def fileno(self):
                return self.backing.fileno()

            def write(self, value):
                return self.backing.write(value)

            def close(self):
                raise RuntimeError("secret-close-detail")

        invocation = self.invocation()
        stream = CloseFailingStream()
        FakePopen.calls = []
        FakePopen.outputs = [{"stdin": stream, "returncode": None}]
        try:
            with mock.patch(
                "scripts.run_live_eval.subprocess.Popen", FakePopen
            ), mock.patch("scripts.run_live_eval.os.killpg") as kill_group:
                with self.assertRaises(live_runner.ProcessExecutionFailure) as raised:
                    SubprocessCodex().invoke(
                        invocation,
                        "prompt",
                        0.05,
                        max_stdin_bytes=32,
                        max_stdout_bytes=32,
                        max_stderr_bytes=32,
                    )

            self.assertNotIn("secret-close-detail", repr(raised.exception))
            kill_group.assert_called_once_with(FakePopen.calls[0].pid, signal.SIGTERM)
        finally:
            try:
                stream.backing.close()
            except OSError:
                pass

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

    def test_cli_removes_owned_temp_root_when_no_artifact_is_retained(self):
        cli_temp_root = self.runtime / "cli-owned-root"

        def make_temp_root(**_kwargs):
            cli_temp_root.mkdir(mode=0o700)
            return str(cli_temp_root)

        blocked = EvalResult(
            "blocked_isolation",
            "blocked",
            0,
            0,
            RunManifest(("intake-ambiguous-safe",), "gpt-5.6-sol", False, None),
        )
        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "process-local-secret"}, clear=True
        ), mock.patch("scripts.run_live_eval.shutil.which", return_value=str(self.executable)), mock.patch(
            "scripts.run_live_eval.tempfile.mkdtemp", side_effect=make_temp_root
        ), mock.patch("scripts.run_live_eval.run_eval", return_value=blocked):
            with redirect_stdout(output):
                exit_code = main(["--scenario", "intake-ambiguous-safe"])

        self.assertEqual(exit_code, 2)
        self.assertFalse(cli_temp_root.exists())

    def test_setup_cleanup_failure_is_reported_with_safe_residual_path(self):
        with mock.patch(
            "scripts.run_live_eval.install_checkout_skills",
            side_effect=ValueError("setup failed"),
        ), mock.patch(
            "scripts.run_live_eval._cleanup_invocation_run",
            side_effect=lambda invocation: (invocation.run_dir,),
        ):
            result = run_eval(self.request(), FakeCodex())

        scenario = result.scenarios[0]
        self.assertEqual(scenario.status, "blocked_isolation")
        self.assertEqual(scenario.retention, "cleanup_required")
        self.assertTrue(scenario.manual_cleanup_required)
        self.assertEqual(scenario.retained_paths, (scenario.retained_paths[0].resolve(),))
        self.assertTrue(scenario.retained_paths[0].is_relative_to(self.runtime))
        self.assertEqual(result.retention, "cleanup_required")
        self.assertTrue(result.manual_cleanup_required)

    def test_blocked_no_artifact_cleanup_failure_is_reported(self):
        fake = FakeCodex([InvocationTimeout()])
        with mock.patch(
            "scripts.run_live_eval._cleanup_invocation_run",
            side_effect=lambda invocation: (invocation.run_dir,),
        ):
            result = run_eval(self.request(), fake)

        scenario = result.scenarios[0]
        self.assertEqual(scenario.status, "blocked_timeout")
        self.assertIsNone(scenario.artifact_path)
        self.assertEqual(scenario.retention, "cleanup_required")
        self.assertTrue(scenario.manual_cleanup_required)
        self.assertEqual(len(scenario.retained_paths), 1)

    def test_cli_root_cleanup_failure_adjusts_serialized_result(self):
        cli_temp_root = self.runtime / "cli-residual-root"

        def make_temp_root(**_kwargs):
            cli_temp_root.mkdir(mode=0o700)
            return str(cli_temp_root)

        blocked = EvalResult(
            "blocked_isolation",
            "blocked",
            0,
            0,
            RunManifest(("intake-ambiguous-safe",), "gpt-5.6-sol", False, None),
        )
        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "process-local-secret"}, clear=True
        ), mock.patch("scripts.run_live_eval.shutil.which", return_value=str(self.executable)), mock.patch(
            "scripts.run_live_eval.tempfile.mkdtemp", side_effect=make_temp_root
        ), mock.patch("scripts.run_live_eval.run_eval", return_value=blocked), mock.patch(
            "scripts.run_live_eval._cleanup_cli_temp_root",
            return_value=(cli_temp_root.resolve(),),
        ):
            with redirect_stdout(output):
                exit_code = main(["--scenario", "intake-ambiguous-safe"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["retention"], "cleanup_required")
        self.assertTrue(payload["manual_cleanup_required"])
        self.assertEqual(payload["retained_paths"], [str(cli_temp_root.resolve())])

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
