import json
import importlib.machinery
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.workflow_coordination.canonical_json import sha256_id


ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "workflow"
PLAN_FIXTURE = ROOT / "tests" / "fixtures" / "coordination" / "approved-plan.json"
WORKFLOW_MODULE = importlib.machinery.SourceFileLoader(
    "workflow_cli_test_module", str(CLI)
).load_module()


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

    def _validate(self, contract_path):
        return run_cli(
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

    def _handoff(self, receipt_path, *extra):
        return run_cli(
            "validate-handoff",
            "--repo-root",
            self.repo_root,
            "--manifest",
            self.out_dir / "manifest.json",
            "--inventory",
            self.out_dir / "inventory.json",
            "--contract",
            self.out_dir / "contract.json",
            "--receipt",
            receipt_path,
            "--workstream-id",
            "frontend",
            "--changed-path",
            "src/ui/settings.py",
            "--json",
            *extra,
        )

    def test_prepare_and_validate_emit_json_with_canonical_matrix(self):
        prepare_result = self._prepare()

        self.assertEqual(prepare_result.returncode, 0, prepare_result.stderr)
        prepared = json.loads(prepare_result.stdout)
        self.assertIn("manifest_hash", prepared)
        self.assertTrue((self.out_dir / "manifest.json").is_file())
        contract_path = self._write_contract(prepared)

        validation_result = self._validate(contract_path)

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
        validation_result = self._validate(contract_path)
        receipt_path = self.out_dir / "receipt.json"
        receipt_path.write_text(validation_result.stdout, encoding="utf-8")

        accepted = self._handoff(receipt_path)
        rejected = run_cli(
            "validate-handoff",
            "--repo-root",
            self.repo_root,
            "--manifest",
            self.out_dir / "manifest.json",
            "--inventory",
            self.out_dir / "inventory.json",
            "--contract",
            contract_path,
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

    def test_handoff_rejects_tampered_receipt_fields_and_stale_artifact(self):
        prepared = json.loads(self._prepare().stdout)
        contract_path = self._write_contract(prepared)
        receipt = json.loads(self._validate(contract_path).stdout)

        mutations = {
            "normalized_paths": lambda value: value["frontend"].append("src/api"),
            "required_sets": lambda value: value["required_reviewers"].append("fake"),
            "manifest_hash": lambda _: "sha256:" + "0" * 64,
            "checkout_tree_hash": lambda _: "0" * 40,
            "run_id": lambda _: "tampered-run",
            "recorded_at": lambda _: "2026-07-20T00:00:00Z",
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field):
                tampered = json.loads(json.dumps(receipt))
                replacement = mutate(tampered[field])
                if replacement is not None:
                    tampered[field] = replacement
                path = self.out_dir / "tampered.json"
                path.write_text(json.dumps(tampered), encoding="utf-8")
                result = self._handoff(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["fallback"]["execution"], "sequential")

        for invalid in (
            dict(receipt, unexpected=True),
            dict(receipt, normalized_paths=[]),
        ):
            path = self.out_dir / "invalid-schema.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            self.assertNotEqual(self._handoff(path).returncode, 0)

        inventory_path = self.out_dir / "inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["known_interface_ids"].append("stale-v1")
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
        receipt_path = self.out_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assertNotEqual(self._handoff(receipt_path).returncode, 0)

    def test_prepare_requires_fresh_output_directory_without_partial_publish(self):
        first = self._prepare()
        before = {
            path.name: path.read_bytes() for path in self.out_dir.iterdir()
        }
        second = self._prepare()
        self.assertEqual(first.returncode, 0)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.out_dir.iterdir()}
        )

        target = Path(self.temporary_directory.name) / "atomic-output"
        manifest = b"{}\n"
        inventory = b"{}\n"
        original = WORKFLOW_MODULE._write_staged_file
        calls = []

        def fail_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("injected write failure")
            return original(path, data)

        with mock.patch.object(WORKFLOW_MODULE, "_write_staged_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                WORKFLOW_MODULE._publish_prepared_output(target, manifest, inventory)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".atomic-output.*")), [])

        published_target = Path(self.temporary_directory.name) / "publish-fsync"
        real_fsync = WORKFLOW_MODULE.os.fsync
        fsync_calls = []

        def fail_parent_fsync(file_descriptor):
            fsync_calls.append(file_descriptor)
            if len(fsync_calls) == 4:
                raise OSError("injected parent fsync failure")
            return real_fsync(file_descriptor)

        with mock.patch.object(WORKFLOW_MODULE.os, "fsync", side_effect=fail_parent_fsync):
            with self.assertRaises(OSError):
                WORKFLOW_MODULE._publish_prepared_output(
                    published_target, manifest, inventory
                )
        self.assertFalse(published_target.exists())
        self.assertEqual(list(target.parent.glob(".publish-fsync.*")), [])

    def test_repo_root_must_be_canonical_git_toplevel(self):
        plan = PLAN_FIXTURE
        subdir_result = run_cli(
            "prepare-coordination",
            "--repo-root",
            self.repo_root / "src",
            "--plan",
            plan,
            "--out-dir",
            self.out_dir,
            "--json",
        )
        nonrepo = Path(self.temporary_directory.name) / "nonrepo"
        nonrepo.mkdir()
        nonrepo_result = run_cli(
            "prepare-coordination",
            "--repo-root",
            nonrepo,
            "--plan",
            plan,
            "--out-dir",
            self.out_dir,
            "--json",
        )
        self.assertNotEqual(subdir_result.returncode, 0)
        self.assertNotEqual(nonrepo_result.returncode, 0)

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
