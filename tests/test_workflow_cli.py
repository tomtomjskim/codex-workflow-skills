import json
import importlib.machinery
import io
import errno
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scripts.workflow_coordination.canonical_json import sha256_id
from scripts.workflow_coordination.derive import derive_coordination
from scripts.workflow_coordination import reviewer_routing


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
        matrix = WORKFLOW_MODULE.build_trigger_matrix(
            WORKFLOW_MODULE.load_reviewer_routing()
        )
        derived = derive_coordination(
            prepared["manifest"],
            prepared["inventory"],
            matrix,
            normalized_paths={
                workstream["id"]: workstream["exclusive_write_paths"]
                for workstream in prepared["manifest"]["workstreams"]
            },
        )
        tree_hash = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        profile = {
            "shared_interface": derived.profiles.shared_interface,
            "path_overlap": derived.profiles.path_overlap,
            "integration_dependency": derived.profiles.integration_dependency,
        }
        core = {
            "schema_version": 1,
            "manifest_hash": prepared["manifest_hash"],
            "inventory_hash": prepared["inventory_hash"],
            "revision": 1,
            "parent_contract_core_hash": None,
            "contract_owner": "architect",
            "integration_owner": "integration-owner",
            "derived_profile": profile,
            "extension_requirements": {
                "interface_contract": (
                    {"status": "approved"} if profile["shared_interface"] else {}
                ),
                "path_ownership": (
                    {"status": "approved"} if profile["path_overlap"] else {}
                ),
                "integration": (
                    {"status": "approved"}
                    if profile["integration_dependency"]
                    else {}
                ),
            },
        }
        core_hash = sha256_id(core)
        records = []
        records.extend(
            ("handoff", list(edge), edge[0])
            for edge in derived.required_handoffs
        )
        records.extend(
            ("checkpoint", list(edge), edge[0])
            for edge in derived.required_checkpoints
        )
        records.extend(
            ("acknowledgement", workstream_id, workstream_id)
            for workstream_id in derived.required_acknowledgements
        )
        entries = []
        previous_hash = sha256_id(None)
        for index, (record_type, subject_id, producer_id) in enumerate(records):
            body = {
                "contract_core_hash": core_hash,
                "previous_entry_hash": previous_hash,
                "checkout_tree_hash": tree_hash,
                "producer_id": producer_id,
                "command_or_scenario_id": "workflow-cli-record-{}".format(index),
                "artifact_digest": "sha256:" + format(index + 1, "064x"),
                "run_id": "workflow-cli-entry-{}".format(index),
                "recorded_at": "2026-07-21T00:00:00Z",
                "record_type": record_type,
                "subject_id": subject_id,
                "status": "completed",
            }
            entry = dict(body, entry_hash=sha256_id(body))
            entries.append(entry)
            previous_hash = entry["entry_hash"]
        contract = {
            "contract_core": core,
            "execution_ledger": {
                "contract_core_hash": core_hash,
                "status": "frozen",
                "entries": entries,
                "ledger_hash": sha256_id(
                    [entry["entry_hash"] for entry in entries]
                ),
                "integration_gate": {"status": "open"},
                "reviewer_registry": [
                    {
                        "lens": reviewer,
                        "canonical_agent": reviewer,
                        "required": True,
                        "contract_core_hash": core_hash,
                        "status": "completed",
                        "dispatch_evidence": {
                            "run_id": "dispatch-{}".format(reviewer)
                        },
                        "completion_evidence": {
                            "run_id": "complete-{}".format(reviewer)
                        },
                        "defer_receipt": None,
                    }
                    for reviewer in derived.required_reviewers
                ],
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

    def test_handoff_omitted_changed_path_rejects_authoritative_untracked_outside(self):
        prepared = json.loads(self._prepare().stdout)
        contract_path = self._write_contract(prepared)
        receipt_path = self.out_dir / "receipt.json"
        receipt_path.write_text(self._validate(contract_path).stdout, encoding="utf-8")
        (self.repo_root / "src" / "api" / "outside.py").write_text(
            "outside\n", encoding="utf-8"
        )

        result = run_cli(
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
            "--json",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("src/api/outside.py", json.loads(result.stderr)["error"]["message"])

    def test_handoff_declared_subset_cannot_hide_authoritative_outside(self):
        prepared = json.loads(self._prepare().stdout)
        contract_path = self._write_contract(prepared)
        receipt_path = self.out_dir / "receipt.json"
        receipt_path.write_text(self._validate(contract_path).stdout, encoding="utf-8")
        (self.repo_root / "src" / "ui" / "settings.py").write_text(
            "owned\n", encoding="utf-8"
        )
        (self.repo_root / "src" / "api" / "outside.py").write_text(
            "outside\n", encoding="utf-8"
        )

        result = self._handoff(receipt_path)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("src/api/outside.py", json.loads(result.stderr)["error"]["message"])

    def test_handoff_without_declaration_accepts_owned_git_changes(self):
        prepared = json.loads(self._prepare().stdout)
        contract_path = self._write_contract(prepared)
        receipt_path = self.out_dir / "receipt.json"
        receipt_path.write_text(self._validate(contract_path).stdout, encoding="utf-8")
        (self.repo_root / "src" / "ui" / "settings.py").write_text(
            "owned\n", encoding="utf-8"
        )

        result = run_cli(
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
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["changed_paths"], ["src/ui/settings.py"])
        self.assertEqual(payload["declared_changed_paths"], [])
        self.assertEqual(payload["validated_paths"], ["src/ui/settings.py"])
        self.assertEqual(payload["changed_path_source"], "git_status_plus_declarations")

    def test_validate_coordination_rejects_dirty_base_and_mismatched_override(self):
        prepared = json.loads(self._prepare().stdout)
        contract_path = self._write_contract(prepared)
        (self.repo_root / "src" / "ui" / "dirty.py").write_text(
            "dirty\n", encoding="utf-8"
        )

        dirty = self._validate(contract_path)
        mismatch = run_cli(
            "validate-coordination",
            "--repo-root",
            self.repo_root,
            "--manifest",
            self.out_dir / "manifest.json",
            "--inventory",
            self.out_dir / "inventory.json",
            "--contract",
            contract_path,
            "--checkout-tree-hash",
            "0" * 40,
            "--json",
        )

        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("base worktree must be clean", json.loads(dirty.stderr)["error"]["message"])
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("does not match", json.loads(mismatch.stderr)["error"]["message"])

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

    def test_atomic_publish_rejects_concurrent_creator_without_clobber(self):
        parent = Path(self.temporary_directory.name)
        target = parent / "concurrent-output"
        marker = target / "external-marker"

        def concurrent_creator(staged, destination):
            destination.mkdir()
            marker.write_text("external", encoding="utf-8")
            raise FileExistsError(errno.EEXIST, "target exists", str(destination))

        with self.assertRaisesRegex(
            WORKFLOW_MODULE.ValidationError, "must not already exist"
        ):
            WORKFLOW_MODULE._publish_prepared_output(
                target, b"{}\n", b"{}\n", publish=concurrent_creator
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "external")
        self.assertEqual(list(parent.glob(".concurrent-output.*")), [])

    def test_atomic_publish_never_clobbers_existing_target_types(self):
        parent = Path(self.temporary_directory.name)
        external = parent / "external"
        external.mkdir()
        cases = []

        empty_dir = parent / "existing-empty"
        empty_dir.mkdir()
        cases.append((empty_dir, lambda: self.assertEqual(list(empty_dir.iterdir()), [])))

        nonempty_dir = parent / "existing-nonempty"
        nonempty_dir.mkdir()
        marker = nonempty_dir / "marker"
        marker.write_text("keep", encoding="utf-8")
        cases.append((nonempty_dir, lambda: self.assertEqual(marker.read_text(), "keep")))

        existing_file = parent / "existing-file"
        existing_file.write_text("keep", encoding="utf-8")
        cases.append((existing_file, lambda: self.assertEqual(existing_file.read_text(), "keep")))

        existing_link = parent / "existing-link"
        existing_link.symlink_to(external, target_is_directory=True)
        cases.append((existing_link, lambda: self.assertTrue(existing_link.is_symlink())))

        for target, assert_preserved in cases:
            with self.subTest(target=target.name):
                with self.assertRaisesRegex(
                    WORKFLOW_MODULE.ValidationError, "must not already exist"
                ):
                    WORKFLOW_MODULE._publish_prepared_output(
                        target, b"{}\n", b"{}\n"
                    )
                assert_preserved()
                self.assertEqual(list(parent.glob(".{}.*".format(target.name))), [])

    def test_atomic_publish_success_contains_both_files(self):
        target = Path(self.temporary_directory.name) / "successful-output"

        WORKFLOW_MODULE._publish_prepared_output(
            target, b'{"manifest":1}\n', b'{"inventory":1}\n'
        )

        self.assertTrue(target.is_dir())
        self.assertEqual(
            (target / "manifest.json").read_bytes(), b'{"manifest":1}\n'
        )
        self.assertEqual(
            (target / "inventory.json").read_bytes(), b'{"inventory":1}\n'
        )

    def test_atomic_publish_unavailable_blocks_without_fallback(self):
        parent = Path(self.temporary_directory.name)
        target = parent / "unsupported-output"

        def unavailable(_staged, _target):
            raise WORKFLOW_MODULE.ValidationError(
                "atomic no-replace publish is unavailable"
            )

        with self.assertRaisesRegex(
            WORKFLOW_MODULE.ValidationError, "no-replace publish is unavailable"
        ):
            WORKFLOW_MODULE._publish_prepared_output(
                target, b"{}\n", b"{}\n", publish=unavailable
            )

        self.assertFalse(target.exists())
        self.assertEqual(list(parent.glob(".unsupported-output.*")), [])

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

    def test_import_does_not_load_reviewer_routing(self):
        module_name = "workflow_cli_lazy_import_{}".format(uuid.uuid4().hex)
        with mock.patch.object(
            reviewer_routing,
            "load_reviewer_routing",
            side_effect=reviewer_routing.ReviewerRoutingError("injected routing failure"),
        ) as loader:
            imported = importlib.machinery.SourceFileLoader(
                module_name, str(CLI)
            ).load_module()

        self.assertEqual(imported.CLI_VERSION, 1)
        loader.assert_not_called()

    def test_routing_failure_is_canonical_json_with_sequential_fallback(self):
        commands = {
            "validate-coordination": [
                "validate-coordination",
                "--repo-root",
                str(self.repo_root),
                "--manifest",
                "missing-manifest.json",
                "--inventory",
                "missing-inventory.json",
                "--json",
            ],
            "validate-handoff": [
                "validate-handoff",
                "--repo-root",
                str(self.repo_root),
                "--manifest",
                "missing-manifest.json",
                "--inventory",
                "missing-inventory.json",
                "--receipt",
                "missing-receipt.json",
                "--workstream-id",
                "frontend",
                "--json",
            ],
        }
        failures = (
            "cannot load reviewer routing: file is missing",
            "cannot load reviewer routing: corrupt JSON",
            "reviewer routing schema_version must be 1",
        )
        for failure in failures:
            for command, arguments in commands.items():
                with self.subTest(failure=failure, command=command):
                    stderr = mock.Mock(buffer=io.BytesIO())
                    with mock.patch.object(
                        WORKFLOW_MODULE,
                        "load_reviewer_routing",
                        side_effect=WORKFLOW_MODULE.ReviewerRoutingError(failure),
                    ):
                        with mock.patch.object(WORKFLOW_MODULE.sys, "stderr", stderr):
                            result = WORKFLOW_MODULE.main(arguments)

                    raw = stderr.buffer.getvalue()
                    payload = json.loads(raw)
                    self.assertEqual(result, 2)
                    self.assertEqual(payload["command"], command)
                    self.assertEqual(payload["error"]["type"], "ReviewerRoutingError")
                    self.assertEqual(payload["error"]["message"], failure)
                    self.assertEqual(payload["fallback"]["execution"], "sequential")
                    self.assertEqual(
                        raw,
                        WORKFLOW_MODULE.canonical_bytes(payload) + b"\n",
                    )

    def test_prepare_argument_error_and_version_do_not_load_routing(self):
        loader = mock.Mock(
            side_effect=WORKFLOW_MODULE.ReviewerRoutingError("must not load")
        )
        stderr = mock.Mock(buffer=io.BytesIO())
        with mock.patch.object(WORKFLOW_MODULE, "load_reviewer_routing", loader):
            with mock.patch.object(WORKFLOW_MODULE.sys, "stderr", stderr):
                self.assertEqual(
                    WORKFLOW_MODULE.main(["prepare-coordination", "--json"]),
                    2,
                )
            with mock.patch.object(WORKFLOW_MODULE.sys, "stdout", io.StringIO()):
                with self.assertRaises(SystemExit) as exit_context:
                    WORKFLOW_MODULE.main(["--version"])

        self.assertEqual(exit_context.exception.code, 0)
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
