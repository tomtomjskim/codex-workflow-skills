import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.workflow_coordination.canonical_json import sha256_id


ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "workflow"
PLAN_FIXTURE = ROOT / "tests" / "fixtures" / "coordination" / "approved-plan.json"


def run_cli(*arguments):
    return subprocess.run(
        [str(CLI), *map(str, arguments)],
        text=True,
        capture_output=True,
        check=False,
    )


def workflow_decision_for_missing_validator(cli_path):
    if not cli_path.is_file():
        return {"parallel_validation": "blocked", "execution": "sequential"}
    return {"parallel_validation": "available", "execution": "validated"}


class WorkflowCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo_root = Path(self.temporary_directory.name) / "repo"
        self.repo_root.mkdir()
        (self.repo_root / "src" / "ui").mkdir(parents=True)
        (self.repo_root / "src" / "api").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=self.repo_root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Workflow Test",
                "-c",
                "user.email=workflow@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture",
            ],
            cwd=self.repo_root,
            check=True,
        )
        self.out_dir = Path(self.temporary_directory.name) / "coordination"

    def _prepare(self):
        return run_cli(
            "prepare-coordination",
            "--repo-root",
            self.repo_root,
            "--plan",
            PLAN_FIXTURE,
            "--out-dir",
            self.out_dir,
            "--json",
        )

    def _write_contract(self, prepared):
        core = {
            "schema_version": 1,
            "manifest_hash": prepared["manifest_hash"],
            "inventory_hash": prepared["inventory_hash"],
            "revision": 1,
        }
        contract = {
            "contract_core": core,
            "execution_ledger": {
                "contract_core_hash": sha256_id(core),
                "status": "frozen",
                "entries": [],
                "ledger_hash": sha256_id([]),
                "integration_gate": {"status": "open"},
            },
        }
        path = self.out_dir / "contract.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        return path

    def test_prepare_and_validate_emit_json_with_canonical_matrix(self):
        prepare_result = self._prepare()

        self.assertEqual(prepare_result.returncode, 0, prepare_result.stderr)
        prepared = json.loads(prepare_result.stdout)
        self.assertIn("manifest_hash", prepared)
        self.assertTrue((self.out_dir / "manifest.json").is_file())
        contract_path = self._write_contract(prepared)

        validation_result = run_cli(
            "validate-coordination",
            "--repo-root",
            self.repo_root,
            "--manifest",
            self.out_dir / "manifest.json",
            "--inventory",
            self.out_dir / "inventory.json",
            "--contract",
            contract_path,
            "--json",
        )

        self.assertEqual(validation_result.returncode, 0, validation_result.stderr)
        receipt = json.loads(validation_result.stdout)
        self.assertIn("run_id", receipt)
        self.assertEqual(receipt["cli_version"], 1)
        self.assertIn("api-reviewer", receipt["required_sets"]["required_reviewers"])
        self.assertIn("ux-reviewer", receipt["required_sets"]["required_reviewers"])

    def test_validate_handoff_emits_json_and_structured_failure(self):
        prepared_result = self._prepare()
        prepared = json.loads(prepared_result.stdout)
        contract_path = self._write_contract(prepared)
        validation_result = run_cli(
            "validate-coordination",
            "--repo-root",
            self.repo_root,
            "--manifest",
            self.out_dir / "manifest.json",
            "--inventory",
            self.out_dir / "inventory.json",
            "--contract",
            contract_path,
            "--json",
        )
        receipt_path = self.out_dir / "receipt.json"
        receipt_path.write_text(validation_result.stdout, encoding="utf-8")

        accepted = run_cli(
            "validate-handoff",
            "--repo-root",
            self.repo_root,
            "--receipt",
            receipt_path,
            "--workstream-id",
            "frontend",
            "--changed-path",
            "src/ui/settings.py",
            "--json",
        )
        rejected = run_cli(
            "validate-handoff",
            "--repo-root",
            self.repo_root,
            "--receipt",
            receipt_path,
            "--workstream-id",
            "frontend",
            "--changed-path",
            "src/api/settings.py",
            "--json",
        )

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["status"], "valid")
        self.assertNotEqual(rejected.returncode, 0)
        error = json.loads(rejected.stderr)
        self.assertEqual(error["error"]["type"], "ValidationError")
        self.assertEqual(error["fallback"]["execution"], "sequential")

    def test_missing_validator_reports_sequential_fallback(self):
        missing = Path(self.temporary_directory.name) / "missing-workflow"
        self.assertEqual(
            workflow_decision_for_missing_validator(missing),
            {"parallel_validation": "blocked", "execution": "sequential"},
        )

    def test_invalid_utf8_input_returns_json_error_and_nonzero(self):
        invalid = Path(self.temporary_directory.name) / "invalid.json"
        invalid.write_bytes(b"{\"value\": \xff}")

        result = run_cli(
            "prepare-coordination",
            "--repo-root",
            self.repo_root,
            "--plan",
            invalid,
            "--out-dir",
            self.out_dir,
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stderr)
        self.assertEqual(error["error"]["type"], "CanonicalJSONError")

    def test_argument_errors_are_structured_json(self):
        result = run_cli("prepare-coordination", "--json")

        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stderr)
        self.assertEqual(error["error"]["type"], "ArgumentError")
        self.assertEqual(error["fallback"]["execution"], "sequential")


if __name__ == "__main__":
    unittest.main()
