import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "council" / "SKILL.md"
REFERENCES = ROOT / "skills" / "council" / "references"


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
        packet = (REFERENCES / "council-packet.md").read_text(encoding="utf-8")
        loop = (REFERENCES / "loop-control.md").read_text(encoding="utf-8")

        self.assertIn("provisional_main_only", source)
        self.assertIn("Never emit `complete` when no reviewer", source)
        self.assertIn("confirm that the current runtime exposes a", source)
        self.assertIn("45 seconds", source)
        self.assertIn("reviewer_failure:", packet)
        self.assertIn("thread limit", loop)

    def test_start_and_completion_clocks_are_distinct(self):
        source = SKILL.read_text(encoding="utf-8")
        packet = (REFERENCES / "council-packet.md").read_text(encoding="utf-8")
        loop = (REFERENCES / "loop-control.md").read_text(encoding="utf-8")

        for state in (
            "dispatch_requested",
            "registered_started",
            "heartbeat_observed",
            "completed",
            "failed",
            "interrupted",
        ):
            with self.subTest(state=state):
                self.assertIn(state, packet)
        self.assertIn("must not be reused as a completion timeout", source)
        self.assertIn("at most 60 seconds", source)
        self.assertIn("quick review` | 300", loop)
        self.assertIn("default` | 600", loop)
        self.assertIn("deep refinement` | 900", loop)

    def test_failed_attempt_has_bounded_fresh_replacement_and_provenance(self):
        source = SKILL.read_text(encoding="utf-8")
        normalized_source = " ".join(source.split())
        packet = (REFERENCES / "council-packet.md").read_text(encoding="utf-8")
        loop = (REFERENCES / "loop-control.md").read_text(encoding="utf-8")

        for required in (
            "max_reviewer_attempts: 3",
            "Do not launch a replacement while the original attempt remains active",
            "fresh alternate attempt",
            "must not receive the failed attempt's partial conclusion",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized_source)
        for field in (
            "seat_id:",
            "attempt_id:",
            "target_id:",
            "supersedes_attempt_id:",
            "wait_slices:",
            "elapsed_seconds:",
        ):
            with self.subTest(field=field):
                self.assertIn(field, packet)
        self.assertIn("Rendered duplicate status lines", packet)
        self.assertIn("Failed attempts do not consume a Council loop", loop)

    def test_status_is_derived_from_required_completed_seats(self):
        source = SKILL.read_text(encoding="utf-8")
        normalized_source = " ".join(source.split())
        loop = (REFERENCES / "loop-control.md").read_text(encoding="utf-8")

        self.assertIn("all required reviewer seats completed independently", normalized_source)
        self.assertIn("at least one required reviewer seat did not complete", normalized_source)
        self.assertIn("no independent reviewer completed", normalized_source)
        self.assertIn("cannot satisfy a reviewer seat", normalized_source)
        self.assertIn("Required seats, not raw attempts", loop)

    def test_quality_is_not_downgraded_to_save_usage(self):
        loop = (REFERENCES / "loop-control.md").read_text(encoding="utf-8")

        self.assertIn(
            "Do not weaken evidence, drop a material lens, interrupt a valid active reviewer, or "
            "downgrade a required recheck merely to save tokens, latency, or model usage.",
            loop,
        )

    def test_explicit_target_revision_is_preserved_end_to_end(self):
        source = " ".join(SKILL.read_text(encoding="utf-8").split())
        packet = " ".join(
            (REFERENCES / "council-packet.md").read_text(encoding="utf-8").split()
        )

        self.assertIn("Preserve an explicit user-supplied target revision exactly", source)
        self.assertIn("Do not rename, decrement, normalize, or replace it", source)
        self.assertIn("reviewer prompt, attempt record, and final receipt", source)
        self.assertIn("source-bound revision", packet)

    def test_references_and_plugin_surface_are_wired(self):
        source = SKILL.read_text(encoding="utf-8")
        manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        metadata = (
            ROOT / "skills" / "council" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")

        for name in ("council-packet.md", "loop-control.md", "panel-routing.md"):
            with self.subTest(name=name):
                self.assertIn(name, source)
                self.assertTrue(
                    (ROOT / "skills" / "council" / "references" / name).is_file()
                )
        self.assertEqual(manifest["version"], "0.4.0")
        self.assertTrue(
            any("$council" in prompt for prompt in manifest["interface"]["defaultPrompt"])
        )
        self.assertIn("Use $council", metadata)
        self.assertIn("allow_implicit_invocation: false", metadata)

    def test_readme_links_the_sample(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("[sample-council.md](docs/sample-council.md)", readme)


if __name__ == "__main__":
    unittest.main()
