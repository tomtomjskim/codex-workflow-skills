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


class CoordinationAcceptanceDocsTests(unittest.TestCase):
    def test_authoritative_docs_share_v1_acceptance_boundary(self):
        for path in AUTHORITATIVE_DOCS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                for statement in V1_ACCEPTANCE_BOUNDARY:
                    self.assertIn(statement, text)

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
