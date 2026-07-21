import importlib.machinery
import itertools
import re
import tempfile
import unittest
from pathlib import Path

from scripts.workflow_coordination.reviewer_routing import (
    ReviewerRoutingError,
    build_trigger_matrix,
    derive_lenses,
    derive_reviewers,
    load_reviewer_routing,
)


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "adversarial-review-loop" / "SKILL.md"
MATRIX = (
    ROOT
    / "skills"
    / "adversarial-review-loop"
    / "references"
    / "reviewer-trigger-matrix.md"
)
ROUTING = MATRIX.with_name("reviewer-routing.json")
PACKET = MATRIX.with_name("review-packet.md")
README = ROOT / "README.md"
CLI = ROOT / "scripts" / "workflow"

EXPECTED_LENS_AGENTS = {
    "accessibility": "accessibility-reviewer",
    "api": "api-reviewer",
    "architecture": "architect",
    "code": "code-reviewer",
    "database": "dba",
    "performance": "performance-reviewer",
    "qa": "qa-engineer",
    "security": "security-reviewer",
    "test-coverage": "test-coverage-reviewer",
    "ux": "ux-reviewer",
}


def table_rows(text, heading):
    marker = "## {}".format(heading)
    if marker not in text:
        return ()
    body = text.split(marker, 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in body.splitlines():
        if not line.startswith("|") or re.match(r"^\|[- |]+\|$", line):
            continue
        cells = tuple(cell.strip().strip("`") for cell in line.strip("|").split("|"))
        rows.append(cells)
    return tuple(rows[1:])


def split_values(cell):
    return tuple(value.strip().strip("`") for value in cell.split(","))


def markdown_mapping(text, heading):
    mapping = {}
    for keys_cell, values_cell in table_rows(text, heading):
        values = split_values(values_cell)
        for key in split_values(keys_cell):
            mapping[key] = values
    return mapping


class ReviewerRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routing = load_reviewer_routing(ROUTING)
        cls.matrix = MATRIX.read_text(encoding="utf-8")
        cls.packet = PACKET.read_text(encoding="utf-8")
        cls.skill = SKILL.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.cli = importlib.machinery.SourceFileLoader(
            "workflow_reviewer_routing_test", str(CLI)
        ).load_module()

    def test_json_is_the_exact_canonical_lens_registry(self):
        self.assertEqual(1, self.routing.schema_version)
        self.assertEqual(EXPECTED_LENS_AGENTS, self.routing.lens_agents)

    def test_markdown_and_cli_exactly_mirror_json_authority(self):
        markdown_lens_agents = {
            lens: agent
            for lens, agent in table_rows(self.matrix, "Canonical Lens Registry")
        }
        self.assertEqual(self.routing.lens_agents, markdown_lens_agents)
        self.assertEqual(
            self.routing.profile_lenses,
            markdown_mapping(self.matrix, "Profile Triggers"),
        )
        self.assertEqual(
            self.routing.changed_surface_lenses,
            markdown_mapping(self.matrix, "Changed-Surface Triggers"),
        )
        self.assertEqual(
            build_trigger_matrix(self.routing),
            self.cli.CANONICAL_TRIGGER_MATRIX,
        )
        self.assertEqual(1, self.cli.CLI_VERSION)
        self.assertIn("reviewer-routing.json` is authoritative", self.matrix)
        self.assertIn("shared reviewer routing artifact is authoritative", self.readme)
        self.assertNotIn("built-in reviewer matrix", self.readme)

    def test_auth_api_migration_union_is_order_independent_and_deduplicated(self):
        surfaces = ("auth", "api", "migration")
        expected_lenses = ("api", "code", "database", "qa", "security")
        expected_reviewers = (
            "api-reviewer",
            "code-reviewer",
            "dba",
            "qa-engineer",
            "security-reviewer",
        )
        for permutation in itertools.permutations(surfaces):
            with self.subTest(permutation=permutation):
                repeated = permutation + (permutation[0],)
                self.assertEqual(expected_lenses, derive_lenses(self.routing, repeated))
                self.assertEqual(
                    expected_reviewers,
                    derive_reviewers(self.routing, repeated),
                )

    def test_profiles_are_derived_through_the_same_lens_registry(self):
        self.assertEqual(
            ("api-reviewer",),
            derive_reviewers(self.routing, (), profiles=("shared_interface",)),
        )

    def test_unknown_material_surface_blocks_derivation(self):
        with self.assertRaisesRegex(ReviewerRoutingError, "unknown changed-surface"):
            derive_reviewers(self.routing, ("unmapped-material-surface",))

    def test_loader_strictly_rejects_unknown_keys(self):
        source = ROUTING.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing.json"
            path.write_text(source.replace('"schema_version": 1', '"extra": true'), encoding="utf-8")
            with self.assertRaisesRegex(ReviewerRoutingError, "schema"):
                load_reviewer_routing(path)

    def test_required_reviewer_starts_pending_without_completion_evidence(self):
        required_entry = re.search(
            r"required: true.*?status: pending.*?completion_evidence: null",
            self.packet,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(required_entry)
        normalized_packet = re.sub(r"\s+", " ", self.packet)
        self.assertIn("Do not accept self-declared completion", normalized_packet)

    def test_review_packet_names_all_registry_evidence_fields(self):
        for field in (
            "required",
            "status",
            "dispatch_evidence",
            "completion_evidence",
            "contract_core_hash",
            "defer_receipt",
        ):
            with self.subTest(field=field):
                self.assertRegex(self.packet, r"(?m)^\s+{}:".format(field))

    def test_skill_states_when_to_read_each_routing_reference(self):
        self.assertIn(
            "Before deriving reviewers, read `references/reviewer-trigger-matrix.md`.",
            self.skill,
        )
        self.assertIn(
            "Before creating or updating the review packet, read `references/review-packet.md`.",
            self.skill,
        )


if __name__ == "__main__":
    unittest.main()
