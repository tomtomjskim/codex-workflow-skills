import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.install_agent_adapters import (
    InstallConflict,
    InstallError,
    apply_links,
    main,
    plan_links,
)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def write_source(self, name="architect.md", content="agent"):
        path = self.source / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_dry_run_lists_links_without_mutation(self):
        self.write_source()

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "create")
        self.assertFalse((self.target / "architect.md").exists())

    def test_correct_absolute_link_is_idempotent(self):
        source = self.write_source()
        (self.target / "architect.md").symlink_to(source)

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "keep")

    def test_correct_relative_link_is_idempotent(self):
        source = self.write_source()
        relative = os.path.relpath(str(source), str(self.target))
        (self.target / "architect.md").symlink_to(relative)

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "keep")

    def test_indirect_link_is_a_conflict(self):
        source = self.write_source()
        intermediate = self.root / "intermediate.md"
        intermediate.symlink_to(source)
        (self.target / "architect.md").symlink_to(intermediate)

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "conflict")

    def test_conflicting_file_is_never_overwritten(self):
        self.write_source()
        conflict = self.target / "architect.md"
        conflict.write_text("owned", encoding="utf-8")

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "conflict")
        with self.assertRaises(InstallConflict):
            apply_links(result)
        self.assertEqual(conflict.read_text(encoding="utf-8"), "owned")

    def test_broken_symlink_is_a_conflict(self):
        self.write_source()
        conflict = self.target / "architect.md"
        conflict.symlink_to(self.root / "missing")

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "conflict")
        self.assertTrue(conflict.is_symlink())

    def test_source_symlink_escaping_approved_root_is_an_error(self):
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.source / "architect.md").symlink_to(outside)

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "error")
        with self.assertRaises(InstallError):
            apply_links(result)

    def test_target_symlink_escaping_approved_root_is_an_error(self):
        self.write_source()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "architect.md").write_text("outside", encoding="utf-8")
        (self.target / "architect.md").symlink_to(outside / "architect.md")

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "error")

    def test_casefold_target_alias_is_rejected(self):
        self.write_source("Architect.md")
        (self.target / "architect.md").write_text("owned", encoding="utf-8")

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual([entry.action for entry in result.entries], ["error"])

    def test_non_nfc_source_name_is_rejected(self):
        self.write_source("cafe\N{COMBINING ACUTE ACCENT}.md")

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual([entry.action for entry in result.entries], ["error"])

    def test_source_mutation_after_plan_prevents_all_writes(self):
        self.write_source("a.md")
        source_b = self.write_source("b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        source_b.unlink()

        with self.assertRaises(InstallError):
            apply_links(plan)

        self.assertEqual(list(self.target.iterdir()), [])

    def test_target_change_after_plan_prevents_all_writes(self):
        self.write_source("a.md")
        self.write_source("b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        competitor = self.target / "b.md"
        competitor.write_text("competitor", encoding="utf-8")

        with self.assertRaises(InstallConflict):
            apply_links(plan)

        self.assertFalse((self.target / "a.md").exists())
        self.assertEqual(competitor.read_text(encoding="utf-8"), "competitor")

    def test_apply_race_reports_partial_result_and_preserves_competitor(self):
        self.write_source("a.md")
        self.write_source("b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        real_symlink = os.symlink

        def race(source, target, *args, **kwargs):
            target_path = Path(target)
            if target_path.name == "b.md":
                target_path.write_text("competitor", encoding="utf-8")
                raise FileExistsError(target)
            return real_symlink(source, target, *args, **kwargs)

        with mock.patch("scripts.install_agent_adapters.os.symlink", side_effect=race):
            with self.assertRaises(InstallConflict) as raised:
                apply_links(plan)

        self.assertEqual(raised.exception.result.created, ("a.md",))
        self.assertEqual(raised.exception.result.failed, "b.md")
        self.assertEqual((self.target / "b.md").read_text(encoding="utf-8"), "competitor")

    def test_missing_target_directory_is_created_only_during_apply(self):
        self.write_source()
        self.target.rmdir()

        plan = plan_links(self.source, self.target, suffix=".md")

        self.assertFalse(self.target.exists())
        result = apply_links(plan)
        self.assertEqual(result.created, ("architect.md",))
        self.assertTrue((self.target / "architect.md").is_symlink())

    def test_manifest_is_canonical_and_hash_matches_recomputed_plan(self):
        self.write_source("z.md")
        self.write_source("a.md")

        plan = plan_links(self.source, self.target, suffix=".md")
        encoded = plan.to_json().encode("utf-8")

        self.assertEqual(encoded, json.dumps(plan.to_manifest(), ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(plan.plan_hash, plan_links(self.source, self.target, ".md").plan_hash)

    def test_cli_dry_run_json_does_not_mutate(self):
        self.write_source()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main([
                "--source-root", str(self.source),
                "--target-root", str(self.target),
                "--suffix", ".md",
                "--dry-run",
                "--json",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["entries"][0]["action"], "create")
        self.assertFalse((self.target / "architect.md").exists())

    def test_rejects_invalid_suffix_without_exposing_source_contents(self):
        secret = "token-value-that-must-not-leak"
        self.write_source(content=secret)

        with self.assertRaisesRegex(ValueError, "suffix") as raised:
            plan_links(self.source, self.target, suffix="../.md")

        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
