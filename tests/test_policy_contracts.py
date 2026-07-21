import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.validate_policy_contracts import (
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
