import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
AUTHORITATIVE_DOCS = (
    ROOT
    / "docs/superpowers/specs/2026-07-21-risk-based-agent-workflow-validation-design.md",
    ROOT / "docs/superpowers/plans/2026-07-21-coordination-cli-plan.md",
    ROOT / "skills/workflow-intake/references/parallel-coordination.md",
    ROOT / "README.md",
)
V1_ACCEPTANCE_BOUNDARY = (
    "Coordination CLI v1 ends at `validate-handoff`.",
    "In v1, `integration_gate.status` is open-only; caller-submitted `closed` is rejected.",
    "`close-integration` and its closure receipt are a future v2 milestone and a v1 non-goal.",
    "Until v2 exists, do not claim integration status `verified` or `closed`.",
)
CONFLICTING_V1_ASSERTIONS = (
    "In v1, the validator issues a closure receipt.",
    "`validate-coordination` issues a closure receipt in v1.",
    "`validate-handoff` issues a closure receipt in v1.",
    "In v1, `integration_gate.status: closed` is accepted and trusted.",
    "After `validate-handoff`, integration is `verified` and `closed`.",
)


def validate_v1_acceptance_document(text):
    v1_text = text.partition("## Future Milestone: Closure v2")[0]
    return [
        f"conflicting v1 assertion: {assertion}"
        for assertion in CONFLICTING_V1_ASSERTIONS
        if assertion in v1_text
    ]


def insert_before_future_v2(text, assertion):
    marker = "## Future Milestone: Closure v2"
    if marker not in text:
        return f"{text}\n{assertion}\n"
    before, after = text.split(marker, 1)
    return f"{before}{assertion}\n\n{marker}{after}"


class CoordinationAcceptanceDocsTests(unittest.TestCase):
    def test_authoritative_docs_share_v1_acceptance_boundary(self):
        for path in AUTHORITATIVE_DOCS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                for statement in V1_ACCEPTANCE_BOUNDARY:
                    self.assertIn(statement, text)
                self.assertEqual(validate_v1_acceptance_document(text), [])

    def test_conflicting_v1_assertions_are_rejected_in_every_authoritative_doc(self):
        for path in AUTHORITATIVE_DOCS:
            text = path.read_text(encoding="utf-8")
            for assertion in CONFLICTING_V1_ASSERTIONS:
                with self.subTest(
                    path=path.relative_to(ROOT), assertion=assertion
                ):
                    mutated = insert_before_future_v2(text, assertion)
                    self.assertNotEqual(
                        validate_v1_acceptance_document(mutated), []
                    )

    def test_future_v2_closure_receipt_description_is_allowed(self):
        text = "\n".join(V1_ACCEPTANCE_BOUNDARY) + """

## Future Milestone: Closure v2

`close-integration` emits the integration-closure receipt rather than trusting
a user-editable contract field.
"""

        self.assertEqual(validate_v1_acceptance_document(text), [])

    def test_design_defers_gate_closure_to_future_v2_milestone(self):
        path = AUTHORITATIVE_DOCS[0]
        text = path.read_text(encoding="utf-8")

        milestone = text.index("## Future Milestone: Closure v2")
        transition = text.index("| integration gate open → closed |")
        receipt = text.index("A closure receipt binds")

        self.assertGreater(transition, milestone)
        self.assertGreater(receipt, milestone)


if __name__ == "__main__":
    unittest.main()
