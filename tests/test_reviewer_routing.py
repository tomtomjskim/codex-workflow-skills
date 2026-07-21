import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "adversarial-review-loop" / "SKILL.md"
MATRIX = (
    ROOT
    / "skills"
    / "adversarial-review-loop"
    / "references"
    / "reviewer-trigger-matrix.md"
)
PACKET = (
    ROOT
    / "skills"
    / "adversarial-review-loop"
    / "references"
    / "review-packet.md"
)

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
    body = text.split(marker, 1)[1]
    body = body.split("\n## ", 1)[0]
    rows = []
    for line in body.splitlines():
        if not line.startswith("|") or re.match(r"^\|[- |]+\|$", line):
            continue
        cells = tuple(cell.strip().strip("`") for cell in line.strip("|").split("|"))
        rows.append(cells)
    return tuple(rows[1:])


def split_values(cell):
    return tuple(value.strip().strip("`") for value in cell.split(","))


def parse_lens_agents(text):
    return {
        lens: agent
        for lens, agent in table_rows(text, "Canonical Lens Registry")
    }


def parse_trigger_lenses(text):
    triggers = {}
    for tags_cell, lenses_cell in table_rows(text, "Changed-Surface Triggers"):
        lenses = split_values(lenses_cell)
        for tag in split_values(tags_cell):
            triggers[tag] = lenses
    return triggers


def derive_reviewers(text, changed_surfaces):
    lens_agents = parse_lens_agents(text)
    triggers = parse_trigger_lenses(text)
    lenses = sorted(
        {
            lens
            for surface in changed_surfaces
            for lens in triggers.get(surface, ())
        }
    )
    return tuple(lenses), tuple(sorted(lens_agents[lens] for lens in lenses))


class ReviewerRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = MATRIX.read_text(encoding="utf-8")
        cls.packet = PACKET.read_text(encoding="utf-8")
        cls.skill = SKILL.read_text(encoding="utf-8")

    def test_matrix_is_the_exact_canonical_lens_registry(self):
        self.assertEqual(EXPECTED_LENS_AGENTS, parse_lens_agents(self.matrix))

    def test_auth_surface_derives_security_and_qa(self):
        lenses, agents = derive_reviewers(self.matrix, {"auth"})
        self.assertEqual(("qa", "security"), lenses)
        self.assertEqual(("qa-engineer", "security-reviewer"), agents)

    def test_representative_surfaces_derive_exact_roles(self):
        cases = {
            "api": (("api", "code"), ("api-reviewer", "code-reviewer")),
            "tests": (
                ("qa", "test-coverage"),
                ("qa-engineer", "test-coverage-reviewer"),
            ),
            "ui": (
                ("accessibility", "qa", "ux"),
                ("accessibility-reviewer", "qa-engineer", "ux-reviewer"),
            ),
            "architecture": (
                ("architecture", "code"),
                ("architect", "code-reviewer"),
            ),
        }
        for surface, expected in cases.items():
            with self.subTest(surface=surface):
                self.assertEqual(expected, derive_reviewers(self.matrix, {surface}))

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
