import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.validate_policy_contracts import (
    _scan_file_stream,
    scan_tracked_text_files,
    validate_repository_contracts,
    validate_review_sample,
)


SUPPORTED_FINDING = {
    "severity": "MED",
    "finding_evidence": {
        "observed_problem": "The assertion does not exercise the failure mode.",
        "failure_mode": "A regression can pass unnoticed.",
        "source": "tests/test_example.py:12",
    },
    "disposition": "ask",
    "verification_evidence": {
        "command_or_artifact": "python3 -m unittest tests.test_example",
        "assertion_strength": "The assertion fails for the recorded regression.",
        "result": "pass",
    },
}


class PolicyContractTests(unittest.TestCase):
    def test_repository_validator_runs_authoritative_git_change_tests(self):
        root = Path(__file__).parents[1]
        validator = (root / "scripts" / "validate_repo.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("require_file tests/test_git_changes.py", validator)
        self.assertIn(
            "run python3 -m unittest tests.test_git_changes -v", validator
        )

    def test_repository_contracts_require_receipt_fallback_and_canonical_agents(self):
        errors = validate_repository_contracts(Path(__file__).parents[1])

        self.assertEqual(errors, [])

    def test_unsupported_high_sample_is_rejected(self):
        sample = copy.deepcopy(SUPPORTED_FINDING)
        sample["severity"] = "HIGH"
        sample["finding_evidence"] = {}

        errors = validate_review_sample(sample)

        self.assertIn(
            "HIGH requires direct evidence or needs-investigation", errors
        )

    def test_high_requires_conservative_source_or_needs_investigation(self):
        for source in (
            "probably auth.py",
            "probably auth.py:12",
            "https://example.invalid/claim",
            "auth.py",
        ):
            with self.subTest(source=source):
                sample = copy.deepcopy(SUPPORTED_FINDING)
                sample["severity"] = "HIGH"
                sample["finding_evidence"]["source"] = source
                self.assertIn(
                    "HIGH requires direct evidence or needs-investigation",
                    validate_review_sample(sample),
                )

        investigated = copy.deepcopy(SUPPORTED_FINDING)
        investigated["severity"] = "HIGH"
        investigated["finding_evidence"]["source"] = "guess"
        investigated["verification_status"] = "needs-investigation"
        self.assertNotIn(
            "HIGH requires direct evidence or needs-investigation",
            validate_review_sample(investigated),
        )

    def test_high_accepts_conservative_direct_source_grammar(self):
        for source in (
            "src/auth.py:12",
            "command:python3 -m unittest tests.test_auth",
            "artifact:sha256:" + "a" * 64,
        ):
            with self.subTest(source=source):
                sample = copy.deepcopy(SUPPORTED_FINDING)
                sample["severity"] = "HIGH"
                sample["finding_evidence"]["source"] = source
                self.assertNotIn(
                    "HIGH requires direct evidence or needs-investigation",
                    validate_review_sample(sample),
                )

    def test_invalid_enums_and_weak_verification_claims_are_rejected(self):
        invalid = copy.deepcopy(SUPPORTED_FINDING)
        invalid["severity"] = "CRITICAL"
        invalid["disposition"] = "ignore"
        invalid["verification_evidence"] = {
            "command_or_artifact": "smoke test",
            "assertion_strength": "does not throw",
            "result": "pass",
        }

        errors = validate_review_sample(invalid)

        self.assertIn("invalid severity: CRITICAL", errors)
        self.assertIn("invalid disposition: ignore", errors)
        self.assertIn("pass requires failure-mode assertion strength", errors)

    def test_hygiene_scans_every_tracked_text_file_and_skips_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_root = Path(directory)
            subprocess.run(
                ["git", "init", "-q"], cwd=repo_root, check=True
            )
            (repo_root / "PRIVATE.md").write_text(
                "local path: /U" "sers/example/private\n", encoding="utf-8"
            )
            (repo_root / "binary.dat").write_bytes(
                b"\x00/U" b"sers/example/private\n"
            )
            subprocess.run(
                ["git", "add", "PRIVATE.md", "binary.dat"],
                cwd=repo_root,
                check=True,
            )

            errors = scan_tracked_text_files(repo_root)

        self.assertEqual(len(errors), 1)
        self.assertIn("PRIVATE.md", errors[0])
        self.assertIn("private path", errors[0])

    def test_hygiene_does_not_follow_tracked_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = base / "repo"
            repo_root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("/U" "sers/example/private\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
            (repo_root / "external-link").symlink_to(outside)
            subprocess.run(["git", "add", "external-link"], cwd=repo_root, check=True)

            errors = scan_tracked_text_files(repo_root)

        self.assertEqual(errors, [])

    def test_hygiene_scans_symlink_text_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
            (repo_root / "sensitive-link").symlink_to(
                "/U" "sers/example/private"
            )
            subprocess.run(["git", "add", "sensitive-link"], cwd=repo_root, check=True)

            errors = scan_tracked_text_files(repo_root)

        self.assertEqual(len(errors), 1)
        self.assertIn("private path", errors[0])

    def test_binary_scan_stops_after_initial_nul_chunk(self):
        class FirstChunkBinary(io.BytesIO):
            def __init__(self):
                super().__init__(b"\0" + b"x" * 100)
                self.read_calls = 0

            def read(self, size=-1):
                self.read_calls += 1
                if self.read_calls > 1:
                    raise AssertionError("binary scanner read beyond first NUL chunk")
                return super().read(size)

        stream = FirstChunkBinary()
        self.assertEqual(_scan_file_stream(stream, Path("large.bin"), 16), [])
        self.assertEqual(stream.read_calls, 1)

    def test_policy_checker_emits_structured_json_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
            (repo_root / "tracked.txt").write_text("TO" "DO\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo_root, check=True)

            result = subprocess.run(
                [
                    "python3",
                    str(Path(__file__).parents[1] / "scripts" / "validate_policy_contracts.py"),
                    "--repo-root",
                    str(repo_root),
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertTrue(payload["errors"])


if __name__ == "__main__":
    unittest.main()
