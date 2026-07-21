import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.workflow_coordination.canonical_json import sha256_id
from scripts.workflow_coordination.prepare import prepare_coordination
from scripts.workflow_coordination.reviewer_routing import ReviewerRouting
from scripts.workflow_coordination.validate import (
    ValidationError,
    validate_coordination as _validate_coordination,
)


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
    "changed_surface_reviewers": {"api": [], "ui": []},
}
ROUTING = ReviewerRouting(
    schema_version=1,
    lens_agents={"api": "api-reviewer"},
    profile_lenses={"shared_interface": ("api",)},
    changed_surface_lenses={"api": (), "ui": ()},
)
TREE_HASH = "a" * 40


def validate_coordination(*args, **kwargs):
    if kwargs.get("trigger_matrix") == TRIGGER_MATRIX:
        kwargs.setdefault("reviewer_routing", ROUTING)
    return _validate_coordination(*args, **kwargs)


def _contract(prepared=PREPARED):
    core = {
        "schema_version": 1,
        "manifest_hash": prepared.manifest_hash,
        "inventory_hash": prepared.inventory_hash,
        "revision": 1,
        "parent_contract_core_hash": None,
        "contract_owner": "architect",
        "integration_owner": "integration-owner",
        "derived_profile": {
            "shared_interface": True,
            "path_overlap": False,
            "integration_dependency": True,
        },
        "extension_requirements": {
            "interface_contract": {"status": "approved"},
            "path_ownership": {},
            "integration": {"status": "approved"},
        },
    }
    core_hash = sha256_id(core)
    records = (
        ("handoff", ["backend", "frontend"], "backend-owner"),
        ("checkpoint", ["backend", "frontend"], "backend-owner"),
        ("acknowledgement", "frontend", "frontend-owner"),
    )
    entries = []
    previous_hash = sha256_id(None)
    for index, (record_type, subject_id, producer_id) in enumerate(records):
        body = {
            "contract_core_hash": core_hash,
            "previous_entry_hash": previous_hash,
            "checkout_tree_hash": TREE_HASH,
            "producer_id": producer_id,
            "command_or_scenario_id": "coordination-record-{}".format(index),
            "artifact_digest": "sha256:" + format(index + 1, "064x"),
            "run_id": "entry-run-{}".format(index),
            "recorded_at": "2026-07-21T00:00:00Z",
            "record_type": record_type,
            "subject_id": subject_id,
            "status": "completed",
        }
        entry = dict(body, entry_hash=sha256_id(body))
        entries.append(entry)
        previous_hash = entry["entry_hash"]
    return {
        "contract_core": core,
        "execution_ledger": {
            "contract_core_hash": core_hash,
            "status": "frozen",
            "entries": entries,
            "ledger_hash": sha256_id([entry["entry_hash"] for entry in entries]),
            "integration_gate": {"status": "open"},
            "reviewer_registry": [
                {
                    "lens": "api",
                    "canonical_agent": "api-reviewer",
                    "required": True,
                    "contract_core_hash": core_hash,
                    "status": "completed",
                    "dispatch_evidence": {"run_id": "dispatch-api"},
                    "completion_evidence": {"run_id": "complete-api"},
                    "defer_receipt": None,
                }
            ],
        },
    }


def _rehash_ledger(contract):
    previous_hash = sha256_id(None)
    entry_hashes = []
    for entry in contract["execution_ledger"]["entries"]:
        entry["previous_entry_hash"] = previous_hash
        body = {key: value for key, value in entry.items() if key != "entry_hash"}
        entry["entry_hash"] = sha256_id(body)
        previous_hash = entry["entry_hash"]
        entry_hashes.append(entry["entry_hash"])
    contract["execution_ledger"]["ledger_hash"] = sha256_id(entry_hashes)
    return contract


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
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash=TREE_HASH,
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
        self.assertEqual(
            dict(receipt.derived_profiles),
            {
                "shared_interface": True,
                "path_overlap": False,
                "integration_dependency": True,
            },
        )
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
        with self.assertRaisesRegex(ValidationError, "reviewer routing authority"):
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

    def test_rejects_frozen_contracted_contract_with_empty_ledger(self):
        contract = _contract()
        contract["execution_ledger"]["entries"] = []
        contract["execution_ledger"]["ledger_hash"] = sha256_id([])
        with self.assertRaisesRegex(ValidationError, "required ledger evidence"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                contract,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash=TREE_HASH,
            )

    def test_rejects_incomplete_core_profile_and_self_closed_gate(self):
        cases = []
        missing_owner = _contract()
        del missing_owner["contract_core"]["contract_owner"]
        cases.append((missing_owner, "contract core schema"))
        wrong_profile = _contract()
        wrong_profile["contract_core"]["derived_profile"][
            "integration_dependency"
        ] = False
        cases.append((wrong_profile, "derived profile"))
        closed_gate = _contract()
        closed_gate["execution_ledger"]["integration_gate"]["status"] = "closed"
        cases.append((closed_gate, "integration gate"))

        for contract, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_coordination(
                        self.repo_root,
                        PREPARED.manifest,
                        PREPARED.inventory,
                        contract,
                        trigger_matrix=TRIGGER_MATRIX,
                        checkout_tree_hash=TREE_HASH,
                    )

    def test_rejects_missing_required_edge_and_reviewer_records(self):
        missing_edge = _contract()
        missing_edge["execution_ledger"]["entries"] = missing_edge[
            "execution_ledger"
        ]["entries"][1:]
        first = missing_edge["execution_ledger"]["entries"][0]
        first["previous_entry_hash"] = sha256_id(None)
        first_body = {key: value for key, value in first.items() if key != "entry_hash"}
        first["entry_hash"] = sha256_id(first_body)
        second = missing_edge["execution_ledger"]["entries"][1]
        second["previous_entry_hash"] = first["entry_hash"]
        second_body = {key: value for key, value in second.items() if key != "entry_hash"}
        second["entry_hash"] = sha256_id(second_body)
        missing_edge["execution_ledger"]["ledger_hash"] = sha256_id(
            [first["entry_hash"], second["entry_hash"]]
        )

        missing_reviewer = _contract()
        missing_reviewer["execution_ledger"]["reviewer_registry"] = []
        cases = (
            (missing_edge, "required ledger evidence"),
            (missing_reviewer, "required reviewer"),
        )
        for contract, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_coordination(
                        self.repo_root,
                        PREPARED.manifest,
                        PREPARED.inventory,
                        contract,
                        trigger_matrix=TRIGGER_MATRIX,
                        checkout_tree_hash=TREE_HASH,
                    )

    def test_rejects_invalid_extensions_revision_and_current_core_evidence(self):
        empty_extension = _contract()
        empty_extension["contract_core"]["extension_requirements"][
            "interface_contract"
        ] = {}

        missing_parent = _contract()
        missing_parent["contract_core"]["revision"] = 2

        stale_reviewer = _contract()
        stale_reviewer["execution_ledger"]["reviewer_registry"][0][
            "contract_core_hash"
        ] = "sha256:" + "0" * 64

        malformed_entry = _contract()
        del malformed_entry["execution_ledger"]["entries"][0]["record_type"]

        cases = (
            (empty_extension, "non-empty interface_contract extension"),
            (missing_parent, "parent contract core hash"),
            (stale_reviewer, "reviewer registry contract core hash"),
            (malformed_entry, "ledger entry schema"),
        )
        for contract, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_coordination(
                        self.repo_root,
                        PREPARED.manifest,
                        PREPARED.inventory,
                        contract,
                        trigger_matrix=TRIGGER_MATRIX,
                        checkout_tree_hash=TREE_HASH,
                    )

    def test_rejects_unauthorized_and_unverified_evidence_producers(self):
        cases = []
        wrong_handoff_owner = _contract()
        wrong_handoff_owner["execution_ledger"]["entries"][0][
            "producer_id"
        ] = "frontend-owner"
        cases.append((_rehash_ledger(wrong_handoff_owner), "evidence producer"))

        wrong_ack_owner = _contract()
        wrong_ack_owner["execution_ledger"]["entries"][2][
            "producer_id"
        ] = "backend-owner"
        cases.append((_rehash_ledger(wrong_ack_owner), "evidence producer"))

        unverified = _contract()
        unverified["execution_ledger"]["entries"][0]["producer_id"] = "unverified"
        cases.append((_rehash_ledger(unverified), "unverified"))

        for contract, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_coordination(
                        self.repo_root,
                        PREPARED.manifest,
                        PREPARED.inventory,
                        contract,
                        trigger_matrix=TRIGGER_MATRIX,
                        checkout_tree_hash=TREE_HASH,
                    )

    def test_reviewer_registry_requires_canonical_lens_agent_pair(self):
        contract = _contract()
        contract["execution_ledger"]["reviewer_registry"][0]["lens"] = "security"
        with self.assertRaisesRegex(ValidationError, "canonical reviewer pair"):
            validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                contract,
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash=TREE_HASH,
            )

    def test_custom_trigger_matrix_requires_matching_routing_authority(self):
        with self.assertRaisesRegex(ValidationError, "reviewer routing authority"):
            _validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                _contract(),
                trigger_matrix=TRIGGER_MATRIX,
                checkout_tree_hash=TREE_HASH,
            )

        mismatched = ReviewerRouting(
            schema_version=1,
            lens_agents={"security": "security-reviewer"},
            profile_lenses={"shared_interface": ("security",)},
            changed_surface_lenses={"api": (), "ui": ()},
        )
        with self.assertRaisesRegex(ValidationError, "reviewer routing authority"):
            _validate_coordination(
                self.repo_root,
                PREPARED.manifest,
                PREPARED.inventory,
                _contract(),
                trigger_matrix=TRIGGER_MATRIX,
                reviewer_routing=mismatched,
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
