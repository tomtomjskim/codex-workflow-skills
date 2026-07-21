import copy
import unittest
from pathlib import Path

from scripts.workflow_coordination.canonical_json import load_canonical_input, sha256_id
from scripts.workflow_coordination.prepare import PreparationError, prepare_coordination


FIXTURE = Path(__file__).parent / "fixtures" / "coordination" / "approved-plan.json"
APPROVED_PLAN = load_canonical_input(FIXTURE.read_bytes())
API_CATALOG = {
    "schema_version": 1,
    "known_interface_ids": ["settings-v1", "audit-event-v2"],
}


class PrepareCoordinationTests(unittest.TestCase):
    def test_generates_manifest_and_inventory_from_one_plan(self):
        prepared = prepare_coordination(APPROVED_PLAN, API_CATALOG)

        self.assertEqual(prepared.manifest["workstreams"][0]["id"], "frontend")
        self.assertEqual(
            prepared.inventory["expected_workstreams"], APPROVED_PLAN["workstreams"]
        )
        self.assertEqual(
            prepared.inventory["changed_surfaces"], APPROVED_PLAN["changed_surfaces"]
        )
        self.assertEqual(
            prepared.inventory["known_interface_ids"],
            ["audit-event-v2", "settings-v1"],
        )
        self.assertEqual(prepared.manifest_hash, sha256_id(prepared.manifest))
        self.assertEqual(prepared.inventory_hash, sha256_id(prepared.inventory))
        self.assertEqual(
            prepared.manifest["inventory_hash"], prepared.inventory_hash
        )

    def test_rejects_duplicate_workstream_ids(self):
        plan = copy.deepcopy(APPROVED_PLAN)
        plan["workstreams"] = [APPROVED_PLAN["workstreams"][0]] * 2

        with self.assertRaisesRegex(PreparationError, "duplicate workstream id"):
            prepare_coordination(plan, API_CATALOG)

    def test_ignores_user_supplied_route_and_reviewer_sets(self):
        plan = copy.deepcopy(APPROVED_PLAN)
        plan["selected_route"] = "contracted"
        plan["required_reviewers"] = ["security-reviewer"]
        plan["required_reviewer_set"] = ["api-reviewer"]

        prepared = prepare_coordination(plan, API_CATALOG)

        for artifact in (prepared.manifest, prepared.inventory):
            self.assertNotIn("selected_route", artifact)
            self.assertNotIn("required_reviewers", artifact)
            self.assertNotIn("required_reviewer_set", artifact)

    def test_catalog_is_optional_and_outputs_do_not_alias_plan(self):
        plan = copy.deepcopy(APPROVED_PLAN)
        prepared = prepare_coordination(plan, None)

        self.assertEqual(prepared.inventory["known_interface_ids"], [])
        plan["workstreams"][0]["scope"].append("src/changed-after-prepare")
        self.assertNotIn(
            "src/changed-after-prepare",
            prepared.manifest["workstreams"][0]["scope"],
        )
        self.assertNotIn(
            "src/changed-after-prepare",
            prepared.inventory["expected_workstreams"][0]["scope"],
        )


if __name__ == "__main__":
    unittest.main()
