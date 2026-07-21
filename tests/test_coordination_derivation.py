import copy
import unittest

from scripts.workflow_coordination.derive import derive_coordination
from scripts.workflow_coordination.prepare import prepare_coordination


PLAN = {
    "changed_surfaces": ["database", "api/settings"],
    "workstreams": [
        {
            "id": "frontend",
            "owner": "frontend-owner",
            "scope": ["web/settings"],
            "exclusive_write_paths": ["web/settings"],
            "depends_on": ["backend"],
            "consumes": [{"kind": "api", "id": "settings-v1"}],
            "produces": [],
        },
        {
            "id": "backend",
            "owner": "backend-owner",
            "scope": ["api/settings"],
            "exclusive_write_paths": ["api/settings"],
            "depends_on": [],
            "consumes": [],
            "produces": [{"kind": "api", "id": "settings-v1"}],
        },
    ],
}
PREPARED = prepare_coordination(PLAN, None)
MANIFEST = PREPARED.manifest
INVENTORY = PREPARED.inventory
TRIGGER_MATRIX = {
    "schema_version": 1,
    "profile_reviewers": {"shared_interface": ["api-reviewer"]},
    "changed_surface_reviewers": {"database": ["data-reviewer"]},
}


def _external_prepared(catalog):
    plan = copy.deepcopy(PLAN)
    plan["workstreams"][0]["consumes"] = [
        {"kind": "api", "id": "external-v1"}
    ]
    plan["workstreams"][1]["produces"] = []
    return prepare_coordination(plan, catalog)


class DerivationTests(unittest.TestCase):
    def test_derives_object_ref_shared_api_and_reviewer_union(self):
        result = derive_coordination(MANIFEST, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.completeness, "verified")
        self.assertEqual(result.route, "contracted")
        self.assertTrue(result.profiles.shared_interface)
        self.assertEqual(result.affected_consumers, ("frontend",))
        self.assertEqual(result.required_handoffs, (("backend", "frontend"),))
        self.assertEqual(result.required_acknowledgements, ("frontend",))
        self.assertEqual(
            result.required_reviewers, ("api-reviewer", "data-reviewer")
        )

    def test_inventory_content_mismatch_blocks(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["workstreams"][0]["consumes"] = []

        result = derive_coordination(manifest, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.completeness, "mismatch")
        self.assertEqual(result.route, "blocked")

    def test_empty_artifacts_are_missing_and_blocked(self):
        for manifest, inventory in (({}, INVENTORY), (MANIFEST, {}), ({}, {})):
            with self.subTest(manifest=manifest, inventory=inventory):
                result = derive_coordination(manifest, inventory, TRIGGER_MATRIX)
                self.assertEqual(result.completeness, "missing")
                self.assertEqual(result.route, "blocked")

    def test_incompatible_artifacts_are_blocked(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["schema_version"] = 2

        result = derive_coordination(manifest, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.completeness, "incompatible")
        self.assertEqual(result.route, "blocked")

    def test_inventory_hash_mismatch_blocks(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["inventory_hash"] = "sha256:" + "0" * 64

        result = derive_coordination(manifest, INVENTORY, TRIGGER_MATRIX)

        self.assertEqual(result.completeness, "mismatch")
        self.assertEqual(result.route, "blocked")

    def test_external_consume_without_catalog_evidence_is_unverified(self):
        prepared = _external_prepared(None)

        result = derive_coordination(
            prepared.manifest, prepared.inventory, TRIGGER_MATRIX
        )

        self.assertEqual(result.completeness, "unverified")
        self.assertEqual(result.route, "blocked")

    def test_known_external_consume_is_verified_and_independent(self):
        prepared = _external_prepared({"known_interface_ids": ["external-v1"]})

        result = derive_coordination(
            prepared.manifest, prepared.inventory, TRIGGER_MATRIX
        )

        self.assertEqual(result.completeness, "verified")
        self.assertEqual(result.route, "independent")

    def test_explicit_catalog_omission_is_inventory_mismatch(self):
        prepared = _external_prepared({"known_interface_ids": ["different-v1"]})

        result = derive_coordination(
            prepared.manifest, prepared.inventory, TRIGGER_MATRIX
        )

        self.assertEqual(result.completeness, "mismatch")
        self.assertEqual(result.route, "blocked")

    def test_missing_or_malformed_trigger_matrix_is_incompatible_and_blocked(self):
        malformed = {
            "schema_version": 1,
            "profile_reviewers": {"shared_interface": "api-reviewer"},
            "changed_surface_reviewers": {},
        }

        for matrix in ({}, malformed):
            with self.subTest(matrix=matrix):
                result = derive_coordination(MANIFEST, INVENTORY, matrix)
                self.assertEqual(result.completeness, "incompatible")
                self.assertEqual(result.route, "blocked")
                self.assertEqual(result.required_reviewers, ())

    def test_outputs_are_deterministically_sorted(self):
        plan = copy.deepcopy(PLAN)
        plan["workstreams"].insert(
            0,
            {
                "id": "admin",
                "owner": "admin-owner",
                "scope": ["admin/settings"],
                "exclusive_write_paths": ["admin/settings"],
                "depends_on": ["backend"],
                "consumes": [{"id": "settings-v1", "kind": "api"}],
                "produces": [],
            },
        )
        prepared = prepare_coordination(plan, None)
        matrix = copy.deepcopy(TRIGGER_MATRIX)
        matrix["profile_reviewers"]["shared_interface"] = [
            "z-reviewer",
            "api-reviewer",
        ]

        result = derive_coordination(prepared.manifest, prepared.inventory, matrix)

        self.assertEqual(result.affected_consumers, ("admin", "frontend"))
        self.assertEqual(
            result.required_handoffs,
            (("backend", "admin"), ("backend", "frontend")),
        )
        self.assertEqual(result.required_acknowledgements, ("admin", "frontend"))
        self.assertEqual(
            result.required_reviewers,
            ("api-reviewer", "data-reviewer", "z-reviewer"),
        )

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
        self.assertEqual(
            result.required_reviewers, ("api-reviewer", "data-reviewer")
        )


if __name__ == "__main__":
    unittest.main()
