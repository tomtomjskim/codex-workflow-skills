import os
import re
import unittest
from pathlib import Path


REVIEWER_ROLES = (
    "accessibility-reviewer",
    "api-reviewer",
    "code-reviewer",
    "performance-reviewer",
    "security-reviewer",
    "test-coverage-reviewer",
    "ux-reviewer",
)

READ_ONLY_BOUNDARY = "- Read-only. Do not edit files."
HANDOFF_BOUNDARY = (
    "- When TOM asks for a fix, return a concrete handoff to the applicable "
    "implementation role."
)
HANDOFF_TARGET = (
    "- `handoff_target`: `developer`, `qa-engineer`, or another explicit "
    "implementation role"
)
HANDOFF_REASON = "- `handoff_reason`: accepted finding and required change"

DIRECT_EDIT_OVERRIDES = (
    (
        "authority override",
        r"\b(?:user requests?|project-local instructions?|local instructions?)"
        r"\b[^.\n]*\boverride\b",
    ),
    ("requested edit permission", r"\bmay edit\b[^.\n]*\bwhen requested\b"),
    ("direct fix implementation", r"\bimplement fixes directly\b"),
)


def section(text, heading):
    marker = "## {}".format(heading)
    start = text.index(marker) + len(marker)
    remainder = text[start:]
    end = remainder.find("\n## ")
    return remainder if end < 0 else remainder[:end]


def reviewer_contract_violations(text):
    boundary = section(text, "Boundary")
    authority_context = "\n".join((section(text, "Applicability"), boundary))
    normalized_authority = re.sub(r"\s+", " ", authority_context).lower()
    violations = []
    if READ_ONLY_BOUNDARY not in boundary:
        violations.append("missing read-only boundary")
    if HANDOFF_BOUNDARY not in boundary:
        violations.append("missing fix handoff boundary")
    for label, pattern in DIRECT_EDIT_OVERRIDES:
        if re.search(pattern, normalized_authority):
            violations.append(label)
    return violations


class SharedRoleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("SHARED_AGENTS_ROOT")
        if not configured:
            raise unittest.SkipTest("SHARED_AGENTS_ROOT is not configured")
        cls.common = Path(configured) / "common-agents"

    def read_role(self, role):
        return (self.common / "{}.md".format(role)).read_text(encoding="utf-8")

    def test_reviewers_are_unconditionally_read_only_with_handoffs(self):
        for role in REVIEWER_ROLES:
            with self.subTest(role=role):
                text = self.read_role(role)
                returned = section(text, "Return")
                self.assertEqual([], reviewer_contract_violations(text))
                self.assertIn(HANDOFF_TARGET, returned)
                self.assertIn(HANDOFF_REASON, returned)

    def test_reviewer_authority_mutations_cannot_enable_direct_edits(self):
        valid = """# Reviewer

## Applicability

Project-local instructions may route work to this reviewer.

## Return

- `recommendation`: concrete fix
- `handoff_reason`: accepted finding and required change

## Boundary

- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
"""
        self.assertEqual([], reviewer_contract_violations(valid))

        mutations = {
            "user requests override": (
                "Project-local instructions may route work to this reviewer.",
                "Project-local instructions and user requests override this common rule.",
                "authority override",
            ),
            "may edit when requested": (
                READ_ONLY_BOUNDARY,
                READ_ONLY_BOUNDARY + "\n- This reviewer may edit files when requested.",
                "requested edit permission",
            ),
            "implement fixes directly": (
                HANDOFF_BOUNDARY,
                HANDOFF_BOUNDARY + "\n- This reviewer may implement fixes directly.",
                "direct fix implementation",
            ),
        }
        for label, (original, mutation, expected_violation) in mutations.items():
            with self.subTest(mutation=label):
                mutated = valid.replace(original, mutation)
                self.assertIn(
                    expected_violation,
                    reviewer_contract_violations(mutated),
                )

    def test_qa_and_coverage_have_distinct_ownership(self):
        qa = self.read_role("qa-engineer")
        coverage = self.read_role("test-coverage-reviewer")
        self.assertIn("test planning and execution", qa)
        self.assertIn("test evidence owner", qa)
        self.assertIn("independent read-only assertion audit", coverage)
        self.assertNotIn("test planning and execution", coverage)
        self.assertIn(READ_ONLY_BOUNDARY, section(coverage, "Boundary"))

    def test_pm_stops_at_approval_and_hands_off_implementation(self):
        pm = self.read_role("pm")
        boundary = section(pm, "Boundary")
        returned = section(pm, "Return")
        self.assertIn(
            "Planning and coordination only. Do not edit implementation files.",
            boundary,
        )
        self.assertIn("Do not cross an approval gate", boundary)
        self.assertIn("`handoff_target`", returned)
        self.assertIn("`handoff_reason`", returned)


if __name__ == "__main__":
    unittest.main()
