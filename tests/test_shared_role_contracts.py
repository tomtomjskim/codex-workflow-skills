import os
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


def section(text, heading):
    marker = "## {}".format(heading)
    start = text.index(marker) + len(marker)
    remainder = text[start:]
    end = remainder.find("\n## ")
    return remainder if end < 0 else remainder[:end]


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
                boundary = section(text, "Boundary")
                returned = section(text, "Return")
                self.assertIn(READ_ONLY_BOUNDARY, boundary)
                self.assertIn(HANDOFF_BOUNDARY, boundary)
                self.assertNotIn("Read-only unless TOM", text)
                self.assertNotIn("unless TOM explicitly asks for a fix", text)
                self.assertNotIn("unless TOM explicitly asks for test changes", text)
                self.assertIn(HANDOFF_TARGET, returned)
                self.assertIn(HANDOFF_REASON, returned)

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
