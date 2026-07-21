import copy
import unittest

from scripts.workflow_coordination.derive import derive_coordination


MANIFEST = {
    "schema_version": 1,
    "plan_hash": "sha256:plan",
    "inventory_hash": "sha256:inventory",
    "changed_surfaces": ["api/settings"],
    "workstreams": [
        {
            "id": "frontend",
            "owner": "frontend-owner",
            "scope": ["web/settings"],
            "exclusive_write_paths": ["web/settings"],
            "depends_on": ["backend"],
            "consumes": ["settings-v1"],
            "produces": [],
        },
        {
            "id": "backend",
            "owner": "backend-owner",
            "scope": ["api/settings"],
            "exclusive_write_paths": ["api/settings"],
            "depends_on": [],
            "consumes": [],
            "produces": ["settings-v1"],
        },
    ],
}
INVENTORY = {
    "schema_version": 1,
    "plan_hash": "sha256:plan",
    "expected_workstreams": copy.deepcopy(MANIFEST["workstreams"]),
    "changed_surfaces": ["api/settings"],
    "known_interface_ids": ["settings-v1"],
}
TRIGGER_MATRIX = {
    "shared_interface": ["api-reviewer"],
}


class DerivationTests(unittest.TestCase):
    def test_derives_shared_api_and_reviewers(self):
        result = derive_coordination(MANIFEST, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.completeness, "verified")
        self.assertEqual(result.route, "contracted")
        self.assertTrue(result.profiles.shared_interface)
        self.assertEqual(result.affected_consumers, ("frontend",))
        self.assertEqual(result.required_handoffs, (("backend", "frontend"),))
        self.assertEqual(result.required_acknowledgements, ("frontend",))
        self.assertIn("api-reviewer", result.required_reviewers)

    def test_inventory_mismatch_blocks(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["workstreams"][0]["consumes"] = []

        result = derive_coordination(manifest, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.completeness, "mismatch")
        self.assertEqual(result.route, "blocked")

    def test_ignores_submitted_route_and_required_sets(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["route"] = "blocked"
        manifest["required_handoffs"] = []
        manifest["required_acknowledgements"] = []
        manifest["required_reviewers"] = ["submitted-reviewer"]

        result = derive_coordination(manifest, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.route, "contracted")
        self.assertEqual(result.required_handoffs, (("backend", "frontend"),))
        self.assertEqual(result.required_acknowledgements, ("frontend",))
        self.assertEqual(result.required_reviewers, ("api-reviewer",))


if __name__ == "__main__":
    unittest.main()
