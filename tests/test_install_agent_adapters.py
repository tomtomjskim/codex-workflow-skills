import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import install_agent_adapters as installer
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

    def test_approved_roots_must_be_absolute(self):
        self.write_source()

        with self.assertRaisesRegex(ValueError, "absolute"):
            plan_links(Path("source"), self.target, suffix=".md")
        with self.assertRaisesRegex(ValueError, "absolute"):
            plan_links(self.source, Path("target"), suffix=".md")

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

    def test_success_and_keep_results_report_stable_targets(self):
        source = self.write_source("a.md")
        self.write_source("b.md")
        (self.target / "b.md").symlink_to(self.source / "b.md")

        plan = plan_links(self.source, self.target, suffix=".md")
        result = apply_links(plan)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.created[0].target, plan.entries[0].target)
        self.assertTrue(result.created[0].target_path_stable)
        self.assertEqual(result.kept[0].target, plan.entries[1].target)
        self.assertTrue(result.kept[0].target_path_stable)
        self.assertEqual(
            (self.target / "a.md").resolve(strict=True), source.resolve(strict=True)
        )

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

    def test_target_entry_symlink_loop_is_classified_as_error(self):
        self.write_source()
        conflict = self.target / "architect.md"
        conflict.symlink_to(conflict)

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "error")

    def test_source_symlink_escaping_approved_root_is_an_error(self):
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.source / "architect.md").symlink_to(outside)

        result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "error")
        with self.assertRaises(InstallError):
            apply_links(result)

    def test_internal_source_symlink_is_rejected_without_reading_content(self):
        actual = self.write_source("actual.txt", "must-not-be-read")
        (self.source / "architect.md").symlink_to(actual)

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read")):
            result = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(result.entries[0].action, "error")
        self.assertIn("symlink", result.entries[0].reason)

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

        with self.assertRaises(InstallError) as raised:
            apply_links(plan)

        self.assertEqual(raised.exception.result.plan_hash, plan.plan_hash)
        self.assertEqual(raised.exception.result.status, "blocked")
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

        opened_directory_fds = []

        def race(source, target, *args, **kwargs):
            directory_fd = kwargs.get("dir_fd")
            self.assertIsNotNone(directory_fd)
            opened_directory_fds.append(directory_fd)
            if target == "b.md":
                competitor_fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                    dir_fd=directory_fd,
                )
                os.write(competitor_fd, b"competitor")
                os.close(competitor_fd)
                raise FileExistsError(target)
            return real_symlink(source, target, *args, **kwargs)

        with mock.patch("scripts.install_agent_adapters.os.symlink", side_effect=race):
            with self.assertRaises(InstallConflict) as raised:
                apply_links(plan)

        self.assertEqual([entry.name for entry in raised.exception.result.created], ["a.md"])
        self.assertEqual([entry.name for entry in raised.exception.result.failed], ["b.md"])
        self.assertEqual(raised.exception.result.created[0].target, plan.entries[0].target)
        self.assertTrue(raised.exception.result.created[0].target_path_stable)
        self.assertEqual(raised.exception.result.failed[0].target, plan.entries[1].target)
        self.assertTrue(raised.exception.result.failed[0].target_path_stable)
        self.assertEqual((self.target / "b.md").read_text(encoding="utf-8"), "competitor")
        for descriptor in opened_directory_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_target_root_swap_never_writes_through_replacement_symlink(self):
        self.write_source("a.md")
        self.write_source("b.md")
        (self.target / "b.md").symlink_to(self.source / "b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        moved = self.root / "moved-target"
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_text("owned", encoding="utf-8")
        real_symlink = os.symlink
        calls = []

        def swap_then_link(source, target, *args, **kwargs):
            calls.append(target)
            if len(calls) == 1:
                self.target.rename(moved)
                self.target.symlink_to(outside, target_is_directory=True)
            return real_symlink(source, target, *args, **kwargs)

        with mock.patch("scripts.install_agent_adapters.os.symlink", side_effect=swap_then_link):
            with self.assertRaises(InstallError) as raised:
                apply_links(plan)

        self.assertEqual([entry.name for entry in raised.exception.result.created], ["a.md"])
        self.assertIsNone(raised.exception.result.created[0].target)
        self.assertFalse(raised.exception.result.created[0].target_path_stable)
        payload = json.loads(raised.exception.result.to_json())
        self.assertIsNone(payload["created"][0]["target"])
        self.assertFalse(payload["created"][0]["target_path_stable"])
        self.assertIsNone(payload["kept"][0]["target"])
        self.assertFalse(payload["kept"][0]["target_path_stable"])
        self.assertFalse((outside / "a.md").exists())
        self.assertFalse((outside / "b.md").exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "owned")

    def test_root_swap_before_next_link_marks_prior_created_path_unstable(self):
        self.write_source("a.md")
        self.write_source("b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        moved = self.root / "moved-target"
        outside = self.root / "outside"
        outside.mkdir()
        real_verify = installer._verify_root_identity
        real_symlink = os.symlink
        calls = []

        def swap_before_third_verify(*args, **kwargs):
            calls.append(True)
            if len(calls) == 3:
                self.target.rename(moved)
                real_symlink(str(outside), str(self.target), target_is_directory=True)
            return real_verify(*args, **kwargs)

        with mock.patch(
            "scripts.install_agent_adapters._verify_root_identity",
            side_effect=swap_before_third_verify,
        ):
            with self.assertRaises(InstallError) as raised:
                apply_links(plan)

        self.assertEqual([entry.name for entry in raised.exception.result.created], ["a.md"])
        self.assertIsNone(raised.exception.result.created[0].target)
        self.assertFalse(raised.exception.result.created[0].target_path_stable)
        self.assertFalse((outside / "a.md").exists())
        self.assertFalse((outside / "b.md").exists())

    def test_root_swap_during_eexist_marks_cli_result_paths_unstable(self):
        self.write_source("a.md")
        self.write_source("b.md")
        moved = self.root / "moved-target"
        outside = self.root / "outside"
        outside.mkdir()
        real_symlink = os.symlink

        def swap_then_conflict(source, target, *args, **kwargs):
            if target == "b.md":
                self.target.rename(moved)
                real_symlink(str(outside), str(self.target), target_is_directory=True)
                raise FileExistsError(target)
            return real_symlink(source, target, *args, **kwargs)

        output = io.StringIO()
        with mock.patch(
            "scripts.install_agent_adapters.os.symlink", side_effect=swap_then_conflict
        ):
            with contextlib.redirect_stdout(output):
                exit_code = main([
                    "--source-root", str(self.source),
                    "--target-root", str(self.target),
                    "--suffix", ".md",
                    "--json",
                ])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "partial")
        for section in ("created", "failed"):
            self.assertIsNone(payload[section][0]["target"])
            self.assertFalse(payload[section][0]["target_path_stable"])
        self.assertFalse((outside / "a.md").exists())
        self.assertFalse((outside / "b.md").exists())

    def test_root_swap_during_generic_symlink_error_marks_result_paths_unstable(self):
        self.write_source("a.md")
        self.write_source("b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        moved = self.root / "moved-target"
        outside = self.root / "outside"
        outside.mkdir()
        real_symlink = os.symlink

        def swap_then_fail(source, target, *args, **kwargs):
            if target == "b.md":
                self.target.rename(moved)
                real_symlink(str(outside), str(self.target), target_is_directory=True)
                raise OSError("simulated link failure")
            return real_symlink(source, target, *args, **kwargs)

        with mock.patch(
            "scripts.install_agent_adapters.os.symlink", side_effect=swap_then_fail
        ):
            with self.assertRaises(InstallError) as raised:
                apply_links(plan)

        for section in (raised.exception.result.created, raised.exception.result.failed):
            self.assertIsNone(section[0].target)
            self.assertFalse(section[0].target_path_stable)

    def test_root_swap_during_source_failure_marks_result_paths_unstable(self):
        self.write_source("a.md")
        self.write_source("b.md")
        plan = plan_links(self.source, self.target, suffix=".md")
        moved = self.root / "moved-target"
        outside = self.root / "outside"
        outside.mkdir()
        real_fingerprint = installer._fingerprint
        real_symlink = os.symlink
        source_b_calls = []

        def swap_then_fail(path):
            if path.parent == plan.source_root and path.name == "b.md":
                source_b_calls.append(True)
                if len(source_b_calls) == 2:
                    self.target.rename(moved)
                    real_symlink(
                        str(outside), str(self.target), target_is_directory=True
                    )
                    raise FileNotFoundError("simulated source failure")
            return real_fingerprint(path)

        with mock.patch(
            "scripts.install_agent_adapters._fingerprint", side_effect=swap_then_fail
        ):
            with self.assertRaises(InstallError) as raised:
                apply_links(plan)

        for section in (raised.exception.result.created, raised.exception.result.failed):
            self.assertIsNone(section[0].target)
            self.assertFalse(section[0].target_path_stable)

    def test_missing_target_directory_is_blocked_without_creation(self):
        self.write_source()
        self.target.rmdir()

        plan = plan_links(self.source, self.target, suffix=".md")

        self.assertEqual(plan.entries[0].action, "error")
        self.assertEqual(plan.entries[0].reason, "target root must already exist")
        self.assertFalse(self.target.exists())
        with self.assertRaisesRegex(InstallError, "target root must already exist") as raised:
            apply_links(plan)
        self.assertEqual(raised.exception.result.status, "blocked")
        self.assertFalse(raised.exception.result.target_directory_created)
        self.assertEqual(
            raised.exception.result.failed[0].reason,
            "target root must already exist",
        )
        self.assertFalse(self.target.exists())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([
                "--source-root", str(self.source),
                "--target-root", str(self.target),
                "--suffix", ".md",
                "--json",
            ])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["target_directory_created"])
        self.assertEqual(payload["failed"][0]["reason"], "target root must already exist")
        self.assertFalse(self.target.exists())

    def test_manifest_is_canonical_and_hash_matches_recomputed_plan(self):
        self.write_source("z.md")
        self.write_source("a.md")

        plan = plan_links(self.source, self.target, suffix=".md")
        encoded = plan.to_json().encode("utf-8")

        self.assertEqual(encoded, json.dumps(plan.to_manifest(), ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(plan.plan_hash, plan_links(self.source, self.target, ".md").plan_hash)
        self.assertTrue(plan.to_manifest()["contains_local_paths"])
        self.assertTrue(plan.to_manifest()["sensitive"])

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
        self.assertTrue(json.loads(output.getvalue())["contains_local_paths"])
        self.assertFalse((self.target / "architect.md").exists())

    def test_cli_json_emits_final_partial_result_not_plan(self):
        self.write_source("a.md")
        self.write_source("b.md")
        real_symlink = os.symlink

        def race(source, target, *args, **kwargs):
            directory_fd = kwargs["dir_fd"]
            if target == "b.md":
                competitor_fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                    dir_fd=directory_fd,
                )
                os.close(competitor_fd)
                raise FileExistsError(target)
            return real_symlink(source, target, *args, **kwargs)

        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch("scripts.install_agent_adapters.os.symlink", side_effect=race):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                exit_code = main([
                    "--source-root", str(self.source),
                    "--target-root", str(self.target),
                    "--suffix", ".md",
                    "--json",
                ])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(payload["status"], "partial")
        self.assertEqual([entry["name"] for entry in payload["created"]], ["a.md"])
        self.assertEqual([entry["name"] for entry in payload["failed"]], ["b.md"])
        self.assertTrue(payload["created"][0]["target_path_stable"])
        self.assertTrue(payload["failed"][0]["target_path_stable"])
        self.assertEqual(payload["kept"], [])
        self.assertFalse(payload["target_directory_created"])
        self.assertEqual(len(payload["plan_hash"]), 64)

    def test_broken_target_root_symlink_is_controlled_json_error(self):
        self.write_source()
        self.target.rmdir()
        self.target.symlink_to(self.root / "missing", target_is_directory=True)
        output = io.StringIO()
        errors = io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exit_code = main([
                "--source-root", str(self.source),
                "--target-root", str(self.target),
                "--suffix", ".md",
                "--json",
            ])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["failed"])
        self.assertNotIn("Traceback", errors.getvalue())

    def test_target_root_symlink_loop_is_controlled_json_error(self):
        self.write_source()
        self.target.rmdir()
        self.target.symlink_to(self.target, target_is_directory=True)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main([
                "--source-root", str(self.source),
                "--target-root", str(self.target),
                "--suffix", ".md",
                "--json",
            ])

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_target_root_lstat_open_race_is_controlled_and_preserves_external(self):
        self.write_source()
        canonical_target = self.target.resolve(strict=True)
        moved = self.root / "moved-target"
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_text("owned", encoding="utf-8")
        real_open = os.open
        real_symlink = os.symlink
        raced = []

        def race(path, flags, *args, **kwargs):
            if not raced and Path(path) == canonical_target and "dir_fd" not in kwargs:
                raced.append(True)
                self.target.rename(moved)
                real_symlink(str(outside), str(self.target), target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        output = io.StringIO()
        with mock.patch("scripts.install_agent_adapters.os.open", side_effect=race):
            with contextlib.redirect_stdout(output):
                exit_code = main([
                    "--source-root", str(self.source),
                    "--target-root", str(self.target),
                    "--suffix", ".md",
                    "--json",
                ])

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")
        self.assertEqual(marker.read_text(encoding="utf-8"), "owned")
        self.assertFalse((outside / "architect.md").exists())

    def test_rejects_invalid_suffix_without_exposing_source_contents(self):
        secret = "token-value-that-must-not-leak"
        self.write_source(content=secret)

        with self.assertRaisesRegex(ValueError, "suffix") as raised:
            plan_links(self.source, self.target, suffix="../.md")

        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
