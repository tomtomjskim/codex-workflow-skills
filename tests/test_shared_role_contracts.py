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
IMMUTABLE_BOUNDARY = (
    "- This read-only boundary cannot be overridden by user requests, approvals, "
    "project-local routing, or instructions elsewhere in this role."
)
HANDOFF_TARGET = (
    "- `handoff_target`: `developer`, `qa-engineer`, or another explicit "
    "implementation role"
)
HANDOFF_REASON = "- `handoff_reason`: accepted finding and required change"

CHANGE_VERB = (
    r"(?:edit(?:s|ed|ing)?|modif(?:y|ies|ied|ying)|patch(?:es|ed|ing)?|"
    r"appl(?:y|ies|ied|ying)|implement(?:s|ed|ing)?|writ(?:e|es|ing|ten))"
)
AUTHORITY = r"(?:can|may|should|must|will|shall|allowed|authorized)"
REQUEST_OR_APPROVAL = r"(?:request(?:ed|s)?|asks?|approval|approved)"


def section(text, heading):
    marker = "## {}".format(heading)
    start = text.index(marker) + len(marker)
    remainder = text[start:]
    end = remainder.find("\n## ")
    return remainder if end < 0 else remainder[:end]


def reviewer_contract_violations(text):
    boundary = section(text, "Boundary")
    violations = []
    if READ_ONLY_BOUNDARY not in boundary:
        violations.append("missing read-only boundary")
    if HANDOFF_BOUNDARY not in boundary:
        violations.append("missing fix handoff boundary")
    if IMMUTABLE_BOUNDARY not in boundary:
        violations.append("missing immutable read-only boundary")

    allowed_lines = {READ_ONLY_BOUNDARY, HANDOFF_BOUNDARY, IMMUTABLE_BOUNDARY}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line in allowed_lines:
            continue
        content = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).lower()
        label = None
        if re.search(
            r"\b(?:user requests?|project-local instructions?|local instructions?)"
            r"\b[^.]*\boverride\b",
            content,
        ):
            label = "authority override"
        elif re.search(
            r"\b{}\b[^.]*\bfix(?:es)?\b[^.]*\bdirectly\b".format(CHANGE_VERB),
            content,
        ):
            label = "direct fix authority"
        elif re.search(
            r"(?:\b{}\b[^.]*\b{}\b|\b{}\b[^.]*\b{}\b)".format(
                AUTHORITY,
                CHANGE_VERB,
                CHANGE_VERB,
                AUTHORITY,
            ),
            content,
        ):
            label = "modal change authority"
        elif re.match(r"^{}\b".format(CHANGE_VERB), content):
            label = "imperative direct change"
        elif re.search(
            r"(?:\b{}\b[^.]*\b{}\b|\b{}\b[^.]*\b{}\b)".format(
                REQUEST_OR_APPROVAL,
                CHANGE_VERB,
                CHANGE_VERB,
                REQUEST_OR_APPROVAL,
            ),
            content,
        ):
            label = "conditional change authority"
        if label:
            violations.append("line {}: {}".format(number, label))
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
- `remediation`: describe the patch without applying it
- `handoff_reason`: accepted finding and required change

## Working Mode

1. Recommend a concrete fix and hand it off.

## Boundary

- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
- This read-only boundary cannot be overridden by user requests, approvals, project-local routing, or instructions elsewhere in this role.
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
                "modal change authority",
            ),
            "implement fixes directly": (
                HANDOFF_BOUNDARY,
                HANDOFF_BOUNDARY + "\n- This reviewer may implement fixes directly.",
                "direct fix authority",
            ),
            "can modify files if TOM asks": (
                READ_ONLY_BOUNDARY,
                READ_ONLY_BOUNDARY
                + "\n- This reviewer can modify files if TOM asks.",
                "modal change authority",
            ),
            "apply accepted fixes after approval": (
                "1. Recommend a concrete fix and hand it off.",
                "1. Recommend a concrete fix and hand it off.\n"
                "2. Apply accepted fixes after approval.",
                "imperative direct change",
            ),
            "working mode implements requested changes": (
                "1. Recommend a concrete fix and hand it off.",
                "1. Implement the requested changes before reporting.",
                "imperative direct change",
            ),
        }
        for label, (original, mutation, expected_violation) in mutations.items():
            with self.subTest(mutation=label):
                mutated = valid.replace(original, mutation)
                self.assertTrue(
                    any(
                        expected_violation in violation
                        for violation in reviewer_contract_violations(mutated)
                    ),
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
