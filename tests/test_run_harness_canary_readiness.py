import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import scripts.run_harness_canary_readiness as readiness_cli
from scripts.live_eval.native_canary_readiness import (
    NATIVE_CANARY_READINESS_POLICY_DIGEST,
    NativeCanaryReadinessResult,
)


def _result(status, reason_code):
    digest = "sha256:" + "1" * 64
    success = status == "native_primitive_observations_only"
    return NativeCanaryReadinessResult(
        status=status,
        model_calls=0,
        policy_digest=digest,
        codex_executable_identity_digest=digest if success else None,
        python_executable_identity_digest=digest if success else None,
        permission_profile_evidence_digest=digest if success else None,
        supervisor_environment_evidence_digest=digest if success else None,
        ledger_probe_evidence_digest=digest if success else None,
        complete_evidence_digest=digest if success else None,
        cleanup_state="removed" if success else "not_started",
        reason_code=reason_code,
    )


class NativeCanaryReadinessCliTests(unittest.TestCase):
    def _run(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = readiness_cli.main(arguments)
        return return_code, stdout.getvalue(), stderr.getvalue()

    def _arguments(self):
        return (
            "--codex-executable", "/private/codex",
            "--python-executable", "/private/python",
            "--temp-parent", "/private/temp",
            "--private-root", "/private/ledger",
        )

    def _assert_compact_line(self, output):
        self.assertEqual(output.count("\n"), 1)
        self.assertTrue(output.endswith("\n"))
        payload = json.loads(output)
        self.assertEqual(
            output,
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ) + "\n",
        )
        return payload

    def test_parser_has_only_four_required_path_arguments(self):
        parser = readiness_cli._build_parser()
        actions = tuple(action for action in parser._actions if action.option_strings)
        self.assertEqual(
            {
                option
                for action in actions
                for option in action.option_strings
            },
            {"-h", "--help", "--codex-executable", "--python-executable", "--temp-parent", "--private-root"},
        )
        self.assertEqual(
            {
                action.option_strings[-1]
                for action in actions
                if action.required
            },
            {"--codex-executable", "--python-executable", "--temp-parent", "--private-root"},
        )

    def test_success_calls_library_once_with_path_request_and_emits_sorted_json(self):
        result = _result(
            "native_primitive_observations_only",
            "native_primitive_observations_recorded",
        )
        with mock.patch.object(
            readiness_cli, "run_native_canary_readiness", return_value=result
        ) as runner:
            return_code, stdout, stderr = self._run(self._arguments())

        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(runner.call_count, 1)
        request = runner.call_args.args[0]
        self.assertEqual(request.codex_executable, Path("/private/codex"))
        self.assertEqual(request.python_executable, Path("/private/python"))
        self.assertEqual(request.temp_parent, Path("/private/temp"))
        self.assertEqual(request.private_root, Path("/private/ledger"))
        self.assertEqual(self._assert_compact_line(stdout), result.__dict__)

    def test_blocked_result_has_exit_two_and_one_json_line(self):
        result = _result("blocked", "request_invalid")
        with mock.patch.object(
            readiness_cli, "run_native_canary_readiness", return_value=result
        ) as runner:
            return_code, stdout, stderr = self._run(self._arguments())

        self.assertEqual(return_code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self._assert_compact_line(stdout), result.__dict__)

    def test_missing_and_unknown_arguments_are_sanitized_without_path_leakage(self):
        private_path = "/private/should-not-leak"
        for arguments in (
            ("--codex-executable", private_path),
            (*self._arguments(), "--unexpected", private_path),
        ):
            with self.subTest(arguments=arguments):
                return_code, stdout, stderr = self._run(arguments)
                self.assertEqual(return_code, 2)
                self.assertEqual(stderr, "")
                payload = self._assert_compact_line(stdout)
                self.assertEqual(payload["status"], "blocked")
                self.assertEqual(payload["reason_code"], "request_invalid")
                self.assertEqual(
                    payload["policy_digest"],
                    NATIVE_CANARY_READINESS_POLICY_DIGEST,
                )
                self.assertNotIn(private_path, stdout)
                self.assertNotIn("Traceback", stdout)

    def test_entrypoint_maps_runner_interrupt_to_130_without_traceback(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            readiness_cli,
            "run_native_canary_readiness",
            side_effect=KeyboardInterrupt(),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(readiness_cli._entrypoint(self._arguments()), 130)
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_help_lists_only_the_approved_path_arguments(self):
        return_code, stdout, stderr = self._run(("--help",))
        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("--codex-executable", stdout)
        self.assertIn("--python-executable", stdout)
        self.assertIn("--temp-parent", stdout)
        self.assertIn("--private-root", stdout)
        self.assertNotIn("\"status\"", stdout)

    def test_help_wins_over_other_arguments_without_running_readiness(self):
        with mock.patch.object(
            readiness_cli, "run_native_canary_readiness"
        ) as runner:
            return_code, stdout, stderr = self._run(
                ("--help", *self._arguments())
            )
        self.assertEqual(return_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(runner.call_count, 0)
        self.assertIn("--private-root", stdout)

    def test_direct_script_parser_failure_has_no_traceback_or_path_leakage(self):
        root = Path(__file__).parents[1]
        private_path = "/private/should-not-leak"
        completed = subprocess.run(
            (sys.executable, str(root / "scripts" / "run_harness_canary_readiness.py"), "--unknown", private_path),
            cwd=str(root),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self._assert_compact_line(completed.stdout)
        self.assertNotIn(private_path, completed.stdout)
        self.assertNotIn("Traceback", completed.stdout)


if __name__ == "__main__":
    unittest.main()
