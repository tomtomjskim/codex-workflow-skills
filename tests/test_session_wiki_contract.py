import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "session-wiki" / "SKILL.md"


class SessionWikiContractTests(unittest.TestCase):
    def test_bare_call_shows_options_and_stops_before_discovery(self):
        source = SKILL.read_text(encoding="utf-8")

        for required in (
            "Bare Or Help Call",
            "Stop without repository scanning",
            "scope: project | personal | both",
            "write: none | project | personal-inbox | personal-generated | both",
            "result: candidates | updated",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_default_updates_only_verified_project_knowledge(self):
        source = SKILL.read_text(encoding="utf-8")

        for required in (
            "scope: project",
            "source: session+diff",
            "depth: standard",
            "review: standard",
            "write: project",
            "promotion: forbidden",
            "commit: forbidden",
            "push: forbidden",
            "task-log material",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_personal_wiki_promotion_is_hard_stopped(self):
        source = SKILL.read_text(encoding="utf-8")
        personal = (
            ROOT
            / "skills"
            / "session-wiki"
            / "references"
            / "personal-wiki-routing.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Never write or move content into `reviewed` or `canonical`", source)
        self.assertIn("Promotion Hard Stop", personal)
        self.assertIn("is not promotion", personal)
        self.assertIn("local absolute paths", personal)

    def test_source_precedence_and_no_update_outcome_are_explicit(self):
        source = SKILL.read_text(encoding="utf-8")
        contract = (
            ROOT / "skills" / "session-wiki" / "references" / "knowledge-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("current code, schema, tests", contract)
        self.assertIn("session statements, generated summaries", contract)
        self.assertIn("not_needed", source)
        self.assertIn("manufacture documentation", source)

    def test_references_and_plugin_surface_are_wired(self):
        source = SKILL.read_text(encoding="utf-8")
        manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        metadata = (
            ROOT / "skills" / "session-wiki" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")

        for name in (
            "knowledge-contract.md",
            "project-routing.md",
            "personal-wiki-routing.md",
        ):
            with self.subTest(name=name):
                self.assertIn(name, source)
                self.assertTrue(
                    (ROOT / "skills" / "session-wiki" / "references" / name).is_file()
                )
        self.assertEqual(manifest["version"], "0.4.0")
        self.assertTrue(
            any(
                "$session-wiki" in prompt
                for prompt in manifest["interface"]["defaultPrompt"]
            )
        )
        self.assertIn("allow_implicit_invocation: false", metadata)

    def test_readme_links_the_sample(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("[sample-session-wiki.md](docs/sample-session-wiki.md)", readme)


if __name__ == "__main__":
    unittest.main()
