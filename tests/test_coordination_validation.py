import copy
import tempfile
import unittest
from pathlib import Path

from scripts.workflow_coordination.canonical_json import sha256_id
from scripts.workflow_coordination.prepare import prepare_coordination
from scripts.workflow_coordination.validate import ValidationError, validate_coordination


PLAN = {
    "changed_surfaces": ["api", "ui"],
    "workstreams": [
        {
            "id": "frontend",
            "owner": "frontend-owner",
            "scope": ["src/ui"],
            "exclusive_write_paths": ["src/ui"],
            "depends_on": ["backend"],
            "consumes": [{"kind": "api", "id": "settings-v1"}],
            "produces": [],
        },
        {
            "id": "backend",
            "owner": "backend-owner",
            "scope": ["src/api"],
            "exclusive_write_paths": ["src/api"],
            "depends_on": [],
            "consumes": [],
            "produces": [{"kind": "api", "id": "settings-v1"}],
        },
    ],
}
PREPARED = prepare_coordination(PLAN, None)
TRIGGER_MATRIX = {
    "schema_version": 1,
    "profile_reviewers": {"shared_interface": ["api-reviewer"]},
    "changed_surface_reviewers": {},
}


def _contract(prepared=PREPARED):
    core = {
        "schema_version": 1,
        "manifest_hash": prepared.manifest_hash,
        "inventory_hash": prepared.inventory_hash,
        "revision": 1,
    }
    return {
        "contract_core": core,
        "execution_ledger": {
            "contract_core_hash": sha256_id(core),
            "status": "frozen",
            "entries": [],
            "ledger_hash": sha256_id([]),
            "integration_gate": {"status": "open"},
        },
    }


class CoordinationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo_root = Path(self.temporary_directory.name)
        (self.repo_root / "src" / "ui").mkdir(parents=True)
        (self.repo_root / "src" / "api").mkdir(parents=True)

    def test_rejects_cycle_and_ancestor_path_overlap(self):
        cyclic = copy.deepcopy(PREPARED.manifest)
        cyclic["workstreams"][1]["depends_on"] = ["frontend"]
        with self.assertRaisesRegex(ValidationError, "dependency cycle"):
            validate_coordination(
                self.repo_root, cyclic, PREPARED.inventory, None
            )

        overlap_plan = copy.deepcopy(PLAN)
        overlap_plan["workstreams"][1]["exclusive_write_paths"] = ["src"]
        overlap = prepare_coordination(overlap_plan, None)
        with self.assertRaisesRegex(ValidationError, "path overlap"):
            validate_coordination(
                self.repo_root, overlap.manifest, overlap.inventory, None
            )

    def test_rejects_stale_ledger_entry(self):
        stale_contract = _contract()
        stale_contract["execution_ledger"]["contract_core_hash"] = (
            "sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(ValidationError, "contract core hash mismatch"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                stale_contract,
            )

    def test_rejects_unsafe_paths_missing_dependencies_and_duplicate_owners(self):
        cases = [
            ("/tmp/outside", "absolute path"),
            ("src/../outside", "parent traversal"),
            ("src/*", "glob metacharacter"),
        ]
        for path, message in cases:
            with self.subTest(path=path):
                plan = copy.deepcopy(PLAN)
                plan["workstreams"][0]["exclusive_write_paths"] = [path]
                prepared = prepare_coordination(plan, None)
                with self.assertRaisesRegex(ValidationError, message):
                    validate_coordination(
                        self.repo_root, prepared.manifest, prepared.inventory, None
                    )

        missing = copy.deepcopy(PREPARED.manifest)
        missing["workstreams"][0]["depends_on"] = ["missing"]
        with self.assertRaisesRegex(ValidationError, "unknown dependency"):
            validate_coordination(self.repo_root, missing, PREPARED.inventory, None)

        duplicate_owner_plan = copy.deepcopy(PLAN)
        duplicate_owner_plan["workstreams"][1]["owner"] = "frontend-owner"
        duplicate_owner = prepare_coordination(duplicate_owner_plan, None)
        with self.assertRaisesRegex(ValidationError, "duplicate owner"):
            validate_coordination(
                self.repo_root,
                duplicate_owner.manifest,
                duplicate_owner.inventory,
                None,
            )

    def test_rejects_submitted_required_sets_and_returns_bound_receipt(self):
        submitted = copy.deepcopy(PREPARED.manifest)
        submitted["required_handoffs"] = []
        with self.assertRaisesRegex(ValidationError, "submitted required sets"):
            validate_coordination(
                self.repo_root,
                submitted,
                PREPARED.inventory,
                None,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash="sha256:" + "a" * 64,
            )

        receipt = validate_coordination(
            self.repo_root,
            PREPARED.manifest,
            PREPARED.inventory,
            _contract(),
            trigger_matrix=TRIGGER_MATRIX,
            checkout_tree_hash="sha256:" + "a" * 64,
            run_id_factory=lambda: "test-run",
            clock=lambda: "2026-07-21T00:00:00Z",
        )
        self.assertEqual(receipt.manifest_hash, sha256_id(PREPARED.manifest))
        self.assertEqual(receipt.inventory_hash, sha256_id(PREPARED.inventory))
        self.assertEqual(receipt.derived_route, "contracted")
        self.assertEqual(receipt.derived_profiles, {"shared_interface": True})
        self.assertEqual(
            receipt.required_sets["required_handoffs"],
            [["backend", "frontend"]],
        )
        self.assertEqual(
            receipt.normalized_paths,
            {"backend": ["src/api"], "frontend": ["src/ui"]},
        )
        self.assertTrue(receipt.checkout_tree_hash.startswith("sha256:"))
        self.assertTrue(receipt.run_id)
        self.assertTrue(receipt.recorded_at.endswith("Z"))

    def test_rejects_missing_policy_tree_hash_and_contract(self):
        with self.assertRaisesRegex(ValidationError, "trigger matrix"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                _contract(),
                checkout_tree_hash="sha256:" + "a" * 64,
            )
        with self.assertRaisesRegex(ValidationError, "current contract required"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                None,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash="sha256:" + "a" * 64,
            )

    def test_rejects_broken_ledger_chain_and_hash_domain(self):
        contract = _contract()
        core_hash = contract["execution_ledger"]["contract_core_hash"]
        entry_body = {
            "contract_core_hash": core_hash,
            "previous_entry_hash": sha256_id(None),
            "checkout_tree_hash": "sha256:" + "a" * 64,
            "producer_id": "frontend",
            "command_or_scenario_id": "unit-test",
            "artifact_digest": "sha256:" + "b" * 64,
            "run_id": "entry-run",
            "recorded_at": "2026-07-21T00:00:00Z",
        }
        entry = dict(entry_body, entry_hash=sha256_id(entry_body))
        contract["execution_ledger"]["entries"] = [entry]
        contract["execution_ledger"]["ledger_hash"] = sha256_id(
            [entry["entry_hash"]]
        )
        validate_coordination(
            self.repo_root,
            PREPARED.manifest,
            PREPARED.inventory,
            contract,
            trigger_matrix=TRIGGER_MATRIX,
            checkout_tree_hash="sha256:" + "a" * 64,
        )

        broken = copy.deepcopy(contract)
        broken["execution_ledger"]["entries"][0]["previous_entry_hash"] = (
            "sha256:" + "c" * 64
        )
        with self.assertRaisesRegex(ValidationError, "ledger chain"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                broken,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash="sha256:" + "a" * 64,
            )

    def test_rejects_unfrozen_contracted_contract(self):
        contract = _contract()
        contract["execution_ledger"]["status"] = "draft"
        with self.assertRaisesRegex(ValidationError, "must be frozen"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                contract,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash="sha256:" + "a" * 64,
            )

    def test_wraps_noncanonical_artifact_as_validation_error(self):
        invalid_inventory = copy.deepcopy(PREPARED.inventory)
        invalid_inventory["unsupported_number"] = 1.5
        with self.assertRaisesRegex(ValidationError, "canonical JSON"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                invalid_inventory,
                None,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash="sha256:" + "a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
