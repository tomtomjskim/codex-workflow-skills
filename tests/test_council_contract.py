import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "council" / "SKILL.md"


class CouncilContractTests(unittest.TestCase):
    def test_bare_call_shows_options_and_stops_before_side_effects(self):
        source = SKILL.read_text(encoding="utf-8")

        self.assertIn("Bare Or Help Call", source)
        self.assertIn("Stop without dispatching reviewers", source)
        self.assertIn("mode: review | ideate | decide | refine", source)
        self.assertIn("write: none | in-scope", source)

    def test_default_is_bounded_read_only_and_main_owned(self):
        source = SKILL.read_text(encoding="utf-8")

        for required in (
            "mode: refine",
            "depth: standard",
            "max_loops: 1",
            "result: redefined",
            "write: none",
            "max_subagents: 2",
            "subagent_authority: read-only",
            "writer: main",
            "sole writer",
            "prohibit recursive delegation",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_failure_fallback_cannot_claim_consensus(self):
        source = SKILL.read_text(encoding="utf-8")
        packet = (
            ROOT / "skills" / "council" / "references" / "council-packet.md"
        ).read_text(encoding="utf-8")
        loop = (
            ROOT / "skills" / "council" / "references" / "loop-control.md"
        ).read_text(encoding="utf-8")

        self.assertIn("provisional_main_only", source)
        self.assertIn("Never emit `complete` when no reviewer", source)
        self.assertIn("confirm that the current runtime exposes a", source)
        self.assertIn("45 seconds", source)
        self.assertIn("reviewer_failure:", packet)
        self.assertIn("thread limit", loop)

    def test_references_and_plugin_surface_are_wired(self):
        source = SKILL.read_text(encoding="utf-8")
        manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        for name in ("council-packet.md", "loop-control.md", "panel-routing.md"):
            with self.subTest(name=name):
                self.assertIn(name, source)
                self.assertTrue(
                    (ROOT / "skills" / "council" / "references" / name).is_file()
                )
        self.assertEqual(manifest["version"], "0.3.0")
        self.assertTrue(
            any("$council" in prompt for prompt in manifest["interface"]["defaultPrompt"])
        )

    def test_readme_links_the_sample(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("[sample-council.md](docs/sample-council.md)", readme)


if __name__ == "__main__":
    unittest.main()
