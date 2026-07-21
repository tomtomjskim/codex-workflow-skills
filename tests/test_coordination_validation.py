import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
TREE_HASH = "a" * 40


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
                checkout_tree_hash=TREE_HASH,
            )

        receipt = validate_coordination(
            self.repo_root,
            PREPARED.manifest,
            PREPARED.inventory,
            _contract(),
            trigger_matrix=TRIGGER_MATRIX,
            checkout_tree_hash=TREE_HASH,
            run_id_factory=lambda: "test-run",
            clock=lambda: "2026-07-21T00:00:00Z",
        )
        self.assertEqual(receipt.manifest_hash, sha256_id(PREPARED.manifest))
        self.assertEqual(receipt.inventory_hash, sha256_id(PREPARED.inventory))
        self.assertEqual(receipt.derived_route, "contracted")
        self.assertEqual(dict(receipt.derived_profiles), {"shared_interface": True})
        self.assertEqual(
            receipt.required_sets["required_handoffs"],
            (("backend", "frontend"),),
        )
        self.assertEqual(
            receipt.to_dict()["normalized_paths"],
            {"backend": ["src/api"], "frontend": ["src/ui"]},
        )
        self.assertEqual(receipt.checkout_tree_hash, TREE_HASH)
        self.assertTrue(receipt.run_id)
        self.assertTrue(receipt.recorded_at.endswith("Z"))

    def test_rejects_missing_policy_tree_hash_and_contract(self):
        with self.assertRaisesRegex(ValidationError, "trigger matrix"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                _contract(),
                checkout_tree_hash=TREE_HASH,
            )
        with self.assertRaisesRegex(ValidationError, "current contract required"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                None,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash=TREE_HASH,
            )

    def test_rejects_broken_ledger_chain_and_hash_domain(self):
        contract = _contract()
        core_hash = contract["execution_ledger"]["contract_core_hash"]
        entry_body = {
            "contract_core_hash": core_hash,
            "previous_entry_hash": sha256_id(None),
            "checkout_tree_hash": TREE_HASH,
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
            checkout_tree_hash=TREE_HASH,
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
                checkout_tree_hash=TREE_HASH,
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
                checkout_tree_hash=TREE_HASH,
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
                checkout_tree_hash=TREE_HASH,
            )

    def test_requires_explicit_real_git_tree_hash(self):
        for tree_hash in (None, "sha256:" + "a" * 64, "A" * 40, "a" * 39):
            with self.subTest(tree_hash=tree_hash):
                with self.assertRaisesRegex(ValidationError, "checkout tree hash"):
                    validate_coordination(
                        self.repo_root,
                        PREPARED.manifest,
                        PREPARED.inventory,
                        _contract(),
                        trigger_matrix=TRIGGER_MATRIX,
                        checkout_tree_hash=tree_hash,
                    )

    def test_rejects_case_alias_and_non_nfc_paths(self):
        alias_plan = copy.deepcopy(PLAN)
        alias_plan["workstreams"][0]["exclusive_write_paths"] = [
            "src/UI/component"
        ]
        alias_plan["workstreams"][1]["exclusive_write_paths"] = ["src/ui"]
        alias = prepare_coordination(alias_plan, None)
        with self.assertRaisesRegex(ValidationError, "path alias"):
            validate_coordination(
                self.repo_root, alias.manifest, alias.inventory, None
            )

        non_nfc = copy.deepcopy(PREPARED.manifest)
        non_nfc["workstreams"][0]["exclusive_write_paths"] = ["src/cafe\u0301"]
        with self.assertRaisesRegex(ValidationError, "NFC"):
            validate_coordination(self.repo_root, non_nfc, PREPARED.inventory, None)

    def test_rejects_existing_filesystem_component_case_alias(self):
        independent_plan = {
            "changed_surfaces": [],
            "workstreams": [
                {
                    "id": "wrong-case",
                    "owner": "wrong-case-owner",
                    "scope": ["src/UI/future"],
                    "exclusive_write_paths": ["src/UI/future"],
                    "depends_on": [],
                    "consumes": [],
                    "produces": [],
                }
            ],
        }
        prepared = prepare_coordination(independent_plan, None)
        with self.assertRaisesRegex(ValidationError, "path alias"):
            validate_coordination(
                self.repo_root, prepared.manifest, prepared.inventory, None
            )

    def test_rejects_symlink_escape_and_allows_nonexistent_in_root_parent(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.repo_root / "src" / "link").symlink_to(Path(outside), target_is_directory=True)
            escaped_plan = copy.deepcopy(PLAN)
            escaped_plan["workstreams"][0]["exclusive_write_paths"] = [
                "src/link/future"
            ]
            escaped = prepare_coordination(escaped_plan, None)
            with self.assertRaisesRegex(ValidationError, "escapes repository root"):
                validate_coordination(
                    self.repo_root, escaped.manifest, escaped.inventory, None
                )

        independent_plan = {
            "changed_surfaces": [],
            "workstreams": [
                {
                    "id": "future",
                    "owner": "future-owner",
                    "scope": ["src/future/component"],
                    "exclusive_write_paths": ["src/future/component"],
                    "depends_on": [],
                    "consumes": [],
                    "produces": [],
                }
            ],
        }
        independent = prepare_coordination(independent_plan, None)
        receipt = validate_coordination(
            self.repo_root,
            independent.manifest,
            independent.inventory,
            None,
            trigger_matrix=TRIGGER_MATRIX,
            checkout_tree_hash=TREE_HASH,
        )
        self.assertEqual(
            receipt.to_dict()["normalized_paths"],
            {"future": ["src/future/component"]},
        )

    def test_rejects_internal_symlink_exact_and_ancestor_aliases(self):
        (self.repo_root / "src" / "link").symlink_to(
            self.repo_root / "src" / "ui", target_is_directory=True
        )
        cases = (
            ("src/link/future", "src/ui/future"),
            ("src/link/future/child", "src/ui/future"),
        )
        for linked_path, direct_path in cases:
            with self.subTest(linked_path=linked_path, direct_path=direct_path):
                plan = copy.deepcopy(PLAN)
                plan["workstreams"][0]["exclusive_write_paths"] = [linked_path]
                plan["workstreams"][1]["exclusive_write_paths"] = [direct_path]
                prepared = prepare_coordination(plan, None)
                with self.assertRaisesRegex(ValidationError, "path overlap"):
                    validate_coordination(
                        self.repo_root,
                        prepared.manifest,
                        prepared.inventory,
                        None,
                    )

    def test_clock_requires_parseable_rfc3339_utc(self):
        invalid_clocks = (
            lambda: datetime(2026, 7, 21, 0, 0, 0),
            lambda: datetime(
                2026, 7, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=9))
            ),
            lambda: "not-a-timestampZ",
            lambda: "2026-07-21 00:00:00Z",
        )
        for clock in invalid_clocks:
            with self.subTest(clock=clock):
                with self.assertRaisesRegex(ValidationError, "UTC RFC3339"):
                    validate_coordination(
                        self.repo_root,
                        PREPARED.manifest,
                        PREPARED.inventory,
                        _contract(),
                        trigger_matrix=TRIGGER_MATRIX,
                        checkout_tree_hash=TREE_HASH,
                        clock=clock,
                    )


if __name__ == "__main__":
    unittest.main()
