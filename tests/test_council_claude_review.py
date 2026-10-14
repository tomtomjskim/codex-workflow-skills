import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "skills/council/scripts/claude_review.py"
SPEC = importlib.util.spec_from_file_location("claude_review", SCRIPT)
reviewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewer)


def request(phase="independent"):
    value = {"target_revision": "proposal-v7", "phase": phase, "packet": "검토할 소스와 기준"}
    if phase == "critique":
        value.update(prior_review="Initial finding", chair_response="Counter-evidence")
    return value


def envelope(**changes):
    value = {
        "type": "result", "subtype": "success", "is_error": False,
        "stop_reason": "end_turn", "terminal_reason": "completed",
        "structured_output": {
            "target_revision": "proposal-v7", "phase": "independent",
            "review": "[MED] Source shows missing retry boundary; add a failure test.",
        },
        "duration_ms": 12, "total_cost_usd": 0.01,
        "modelUsage": {"claude-example": {}, "unexpected metadata": {}},
        "usage": {"input_tokens": 3, "output_tokens": 12, "private": "SECRET"},
    }
    value.update(changes)
    return value


def completed(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout, "SECRET STDERR")


class ClaudeReviewTests(unittest.TestCase):
    def run_attempt(self, response=None, code=0, data=None):
        responses = [
            completed("2.1.178 (Claude Code)\n"),
            completed(json.dumps({"loggedIn": True, "email": "SECRET EMAIL"})),
            completed(json.dumps(response if response is not None else envelope()), code),
        ]
        with patch.object(reviewer.shutil, "which", return_value="/fake/claude"), \
                patch.object(reviewer, "run_command", side_effect=responses) as run:
            receipt = reviewer.run_review(data or request(), authorized=True)
        return receipt, run

    def test_no_authorization_never_checks_cli(self):
        with patch.object(reviewer.shutil, "which") as resolve:
            receipt = reviewer.run_review(request())
        resolve.assert_not_called()
        self.assertEqual(receipt["failure_class"], "not_authorized")

    def test_invalid_packets_and_deadlines_never_invoke_provider(self):
        invalid = [None, {}, [], request() | {"prior_review": "Contaminated"},
                   request() | {"chair_response": "Contaminated"},
                   request() | {"packet": " "}, request() | {"phase": "unknown"},
                   request() | {"packet": "\ud800"},
                   request("critique") | {"prior_review": ""},
                   request() | {"packet": "x" * reviewer.MAX_PACKET_BYTES}]
        with patch.object(reviewer.shutil, "which") as resolve:
            for item in invalid:
                with self.subTest(item=str(item)[:50]):
                    self.assertEqual(reviewer.run_review(item, authorized=True)["failure_class"],
                                     "invalid_input")
            for timeout in (0, -1, 901, True, 1.5):
                self.assertEqual(reviewer.run_review(request(), authorized=True, timeout=timeout)
                                 ["failure_class"], "invalid_input")
        resolve.assert_not_called()

    def test_missing_cli_is_unavailable(self):
        with patch.object(reviewer.shutil, "which", return_value=None):
            receipt = reviewer.run_review(request(), authorized=True)
        self.assertEqual(receipt["attempt_state"], "unavailable")
        self.assertEqual(receipt["failure_class"], "cli_unavailable")

    def test_exact_input_byte_limit_is_accepted_and_over_limit_is_rejected(self):
        prefix = b'{"target_revision":"v1","phase":"independent","packet":"'
        suffix = b'"}'
        body = b'x' * (reviewer.MAX_PACKET_BYTES - len(prefix) - len(suffix))
        with patch.object(reviewer.shutil, "which", return_value=None) as resolve:
            receipt = reviewer.run_review(json.loads(prefix + body + suffix), authorized=True)
            self.assertEqual(receipt["failure_class"], "cli_unavailable")
            resolve.assert_called_once()
            resolve.reset_mock()
            receipt = reviewer.run_review(json.loads(prefix + body + b'x' + suffix), authorized=True)
            self.assertEqual(receipt["failure_class"], "invalid_input")
            resolve.assert_not_called()

    def test_auth_failure_never_calls_model_or_exposes_auth(self):
        for status, code in (({"loggedIn": False}, 0), ({"loggedIn": "true"}, 0),
                             ({"loggedIn": True}, 1), ([], 0)):
            status_text = json.dumps(status)
            with patch.object(reviewer.shutil, "which", return_value="claude"), \
                    patch.object(reviewer, "run_command", side_effect=[
                        completed("2.1.178 (Claude Code)"), completed(status_text, code),
                    ]) as run:
                receipt = reviewer.run_review(request(), authorized=True)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(receipt["failure_class"], "auth_unavailable")
            self.assertNotIn("SECRET", json.dumps(receipt))

    def test_logged_in_but_revoked_cannot_complete(self):
        receipt, run = self.run_attempt(envelope(api_error_status=401, structured_output="SECRET TOKEN"), 1)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(receipt["attempt_state"], "unavailable")
        self.assertEqual(receipt["failure_class"], "auth_unavailable")
        self.assertNotIn("SECRET", json.dumps(receipt))

    def test_auth_failure_carries_pending_action_without_claiming_expiry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                receipt, run = self.run_attempt(envelope(api_error_status=status), 1)
                follow_up = receipt['follow_up']
                self.assertEqual(follow_up['status'], 'pending')
                self.assertFalse(follow_up['automatic_retry'])
                self.assertIn('account access', follow_up['user_action'])
                self.assertIn('fresh successful review', follow_up['resume_condition'])
                self.assertEqual(run.call_count, 3)
                self.assertNotIn('expired', json.dumps(follow_up))

    def test_success_and_untyped_provider_failure_do_not_create_auth_reminder(self):
        for response, code in ((envelope(), 0), (envelope(is_error=True), 1)):
            receipt, _ = self.run_attempt(response, code)
            self.assertNotIn('follow_up', receipt)

    def test_cli_auth_failure_reports_follow_up_without_writing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'claude'
            fake.write_text(
                '#!/usr/bin/env python3\n'
                'import sys\n'
                'if "--version" in sys.argv: print("2.1.178 (Claude Code)")\n'
                'elif "auth" in sys.argv: print(\'{"loggedIn":false}\')\n'
                'else: raise RuntimeError("model must not run")\n', encoding='utf-8',
            )
            fake.chmod(0o700)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ.get('PATH', ''))
            original = {p.name: p.read_bytes() for p in root.iterdir()}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--authorized'], input=json.dumps(request()),
                text=True, capture_output=True, env=env, cwd=root, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, '')
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt['failure_class'], 'auth_unavailable')
            self.assertEqual(receipt['follow_up']['status'], 'pending')
            self.assertFalse(receipt['follow_up']['automatic_retry'])
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, original)

    def test_transport_and_shape_failures_never_complete(self):
        cases = [
            (envelope(), 1, "provider_error"),
            (envelope(is_error=True), 0, "provider_error"),
            (envelope(api_error_status=500), 0, "provider_error"),
            (envelope(subtype="error_max_budget_usd"), 0, "provider_error"),
            (envelope(permission_denials=["denied"]), 0, "provider_error"),
            (envelope(stop_reason="max_tokens"), 0, "incomplete_output"),
            (envelope(terminal_reason="cancelled"), 0, "incomplete_output"),
            (envelope(structured_output=None), 0, "empty_output"),
            (envelope(structured_output=None, result='{"review":"free-form fallback"}'),
             0, "empty_output"),
            (envelope(structured_output="   "), 0, "invalid_output"),
            (envelope(structured_output="not JSON"), 0, "invalid_output"),
            (envelope(structured_output=[]), 0, "invalid_output"),
            (envelope(structured_output={"review": "Looks good"}), 0, "invalid_output"),
        ]
        for response, code, expected in cases:
            with self.subTest(expected=expected, response=response):
                receipt, _ = self.run_attempt(response, code)
                self.assertEqual(receipt["attempt_state"], "failed")
                self.assertEqual(receipt["failure_class"], expected)
                self.assertIsNone(receipt["review"])

    def test_revision_and_phase_must_match_exactly(self):
        for field, value in (("target_revision", "proposal-v6"), ("phase", "critique")):
            output = envelope()["structured_output"]
            output[field] = value
            receipt, _ = self.run_attempt(envelope(structured_output=output))
            expected = "revision_mismatch" if field == "target_revision" else "phase_mismatch"
            self.assertEqual(receipt["failure_class"], expected)

    def test_invalid_unicode_in_review_is_a_failure_receipt(self):
        output = envelope()["structured_output"]
        output["review"] = "\ud800"
        receipt, _ = self.run_attempt(envelope(structured_output=output))
        self.assertEqual(receipt["failure_class"], "invalid_output")
        self.assertIsNone(receipt["review"])

    def test_success_uses_isolation_and_stdin_and_only_allowed_metadata(self):
        receipt, run = self.run_attempt()
        self.assertEqual(receipt["attempt_state"], "completed")
        self.assertIsNone(receipt["failure_class"])
        call = run.call_args
        command = call.args[0]
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--system-prompt") + 1], reviewer.SYSTEM_PROMPT)
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1]),
                         reviewer.REVIEW_SCHEMA)
        self.assertNotIn(request()["packet"], " ".join(command))
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertEqual(json.loads(call.kwargs["input_text"]), request())
        self.assertEqual(call.kwargs["timeout"], 600)
        self.assertEqual(receipt["provider_metrics"]["models"], ["claude-example"])
        self.assertNotIn("SECRET", json.dumps(receipt))
        self.assertNotIn("meeting_status", receipt)
        self.assertNotIn("validation_status", receipt)

    def test_critique_is_explicitly_labeled(self):
        output = envelope()["structured_output"]
        output["phase"] = "critique"
        receipt, run = self.run_attempt(envelope(structured_output=output), data=request("critique"))
        self.assertEqual(receipt["attempt_state"], "completed")
        self.assertEqual(receipt["phase"], "critique")
        self.assertEqual(json.loads(run.call_args.kwargs["input_text"]), request("critique"))

    def test_timeout_stages_and_no_automatic_retry(self):
        for stage, previous in (
            ("preflight", []),
            ("completion", [completed("2.1.178 (Claude Code)"),
                            completed('{"loggedIn":true}')]),
        ):
            with patch.object(reviewer.shutil, "which", return_value="claude"), \
                    patch.object(reviewer, "run_command", side_effect=previous + [
                        subprocess.TimeoutExpired("claude", 10, output="SECRET"),
                    ]) as run:
                receipt = reviewer.run_review(request(), authorized=True)
            self.assertEqual(receipt["failure_class"], stage + "_timeout")
            self.assertEqual(run.call_count, len(previous) + 1)
            self.assertNotIn("SECRET", json.dumps(receipt))

    def test_raw_invalid_output_is_not_echoed(self):
        for text, expected in (("", "empty_output"), ("SECRET", "invalid_output")):
            with patch.object(reviewer.shutil, "which", return_value="claude"), \
                    patch.object(reviewer, "run_command", side_effect=[
                        completed("2.1.178 (Claude Code)"), completed('{"loggedIn":true}'),
                        completed(text),
                    ]):
                receipt = reviewer.run_review(request(), authorized=True)
            self.assertEqual(receipt["failure_class"], expected)
            self.assertNotIn("SECRET", json.dumps(receipt))

    def test_owned_subprocess_is_reaped_after_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            reviewer.run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                                 timeout=0.05)

    def test_deep_cli_json_returns_sanitized_failure(self):
        data = '[' * 1100 + '0' + ']' * 1100
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--authorized'], input=data,
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, '')
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['attempt_state'], 'failed')
        self.assertEqual(receipt['failure_class'], 'invalid_input')

    def test_deep_auth_and_provider_json_return_failure_receipts(self):
        deep = '[' * 1100 + '0' + ']' * 1100
        version = completed('2.1.178 (Claude Code)')
        for responses, expected in (
            ([version, completed(deep)], 'auth_unavailable'),
            ([version, completed('{"loggedIn":true}'), completed(deep)], 'invalid_output'),
        ):
            with self.subTest(expected=expected), \
                    patch.object(reviewer.shutil, 'which', return_value='claude'), \
                    patch.object(reviewer, 'run_command', side_effect=responses) as run:
                receipt = reviewer.run_review(request(), authorized=True)
            self.assertEqual(receipt['failure_class'], expected)
            self.assertNotEqual(receipt['attempt_state'], 'completed')
            self.assertIsNone(receipt['review'])
            self.assertEqual(run.call_count, len(responses))

    @unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
    def test_timeout_stops_descendant_and_preserves_other_process(self):
        import fcntl

        child_code = (
            'import fcntl, pathlib, sys, time\n'
            'with open(sys.argv[1], "w") as lock:\n'
            ' fcntl.flock(lock, fcntl.LOCK_EX)\n'
            ' pathlib.Path(sys.argv[2]).write_text("ready")\n'
            ' time.sleep(30)\n'
        )
        parent_code = (
            'import subprocess, sys, time\n'
            'subprocess.Popen([sys.executable, "-c", sys.argv[1], *sys.argv[2:]], '
            'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
            'time.sleep(30)\n'
        )
        real_popen = subprocess.Popen
        owned = []

        def capture_owned(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            owned.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'child.lock'
            ready_path = Path(directory) / 'child.ready'
            sentinel = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  start_new_session=True)
            try:
                with patch.object(reviewer.subprocess, 'Popen', side_effect=capture_owned):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        reviewer.run_command(
                            [sys.executable, '-c', parent_code, child_code,
                             str(lock_path), str(ready_path)], timeout=2,
                        )
                self.assertTrue(ready_path.exists(), 'descendant must acquire lock before timeout')
                self.assertEqual(owned[0].returncode, -signal.SIGKILL)
                with lock_path.open('r+') as lock:
                    # A live descendant would still own this lock; zombies hold no locks.
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(sentinel.poll(), 'unrelated process must remain alive')
            finally:
                for process in owned:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                sentinel.terminate()
                sentinel.wait(timeout=5)

    def test_cli_end_to_end_with_fake_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "claude"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if '--version' in sys.argv: print('2.1.178 (Claude Code)')\n"
                "elif 'auth' in sys.argv: print('{\"loggedIn\":true}')\n"
                "else:\n"
                " data = json.load(sys.stdin)\n"
                " data.pop('packet')\n"
                " data['review'] = 'Bounded fixture review'\n"
                " print(json.dumps({'type':'result','subtype':'success',"
                "'is_error':False,'structured_output':data}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ.get("PATH", ""))
            result = subprocess.run([sys.executable, str(SCRIPT), "--authorized"],
                                    input=json.dumps(request()), text=True, capture_output=True,
                                    env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["attempt_state"], "completed")
        self.assertEqual(receipt["target_revision"], "proposal-v7")


if __name__ == "__main__":
    unittest.main()
