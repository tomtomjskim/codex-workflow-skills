import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.live_eval.checkout as checkout_module
from scripts.live_eval.checkout import (
    canonical_name_key,
    install_checkout_skills,
    require_unique_canonical_names,
    verify_loaded_checkout,
)


EXPECTED_SKILLS = (
    "adversarial-review-loop",
    "workflow",
    "workflow-intake",
)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.repo = self.root / "repo"
        self.codex_home = self.root / "codex-home"
        self.repo.mkdir()
        self.codex_home.mkdir(mode=0o700)
        (self.repo / ".codex-plugin").mkdir()
        (self.repo / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "checkout-test",
                    "skills": "./skills/",
                    "version": "1.0.0",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for index, name in enumerate(EXPECTED_SKILLS):
            skill = self.repo / "skills" / name
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: {}\n---\n".format(name), encoding="utf-8"
            )
            (skill / "references" / "policy.md").write_text(
                "policy {}\n".format(index), encoding="utf-8"
            )
        executable = self.repo / "skills" / "workflow" / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self.git("init", "-q")
        self.git("config", "user.email", "checkout@example.invalid")
        self.git("config", "user.name", "Checkout Test")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *arguments):
        return subprocess.run(
            ("git",) + arguments,
            cwd=str(self.repo),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def new_home(self, name):
        home = self.root / name
        home.mkdir(mode=0o700)
        return home

    def test_materializes_only_expected_read_only_skills_from_head_objects(self):
        manifest = install_checkout_skills(self.repo, self.codex_home)

        object_format = self.git("rev-parse", "--show-object-format")
        self.assertEqual(manifest.object_format, object_format)
        self.assertEqual(
            manifest.tree_hash,
            "{}:{}".format(object_format, self.git("rev-parse", "HEAD^{tree}")),
        )
        self.assertEqual(
            manifest.plugin_blob_oid,
            "{}:{}".format(
                object_format,
                self.git("rev-parse", "HEAD:.codex-plugin/plugin.json"),
            ),
        )
        self.assertEqual(manifest.skill_names, EXPECTED_SKILLS)
        self.assertEqual(tuple(manifest.skill_hashes), EXPECTED_SKILLS)
        self.assertEqual(tuple(manifest.materialized_hashes), EXPECTED_SKILLS)
        for hashes in (manifest.skill_hashes, manifest.materialized_hashes):
            self.assertTrue(all(value.startswith("sha256:") for value in hashes.values()))

        installed = self.codex_home / "skills"
        self.assertEqual(
            tuple(sorted(item.name for item in installed.iterdir())), EXPECTED_SKILLS
        )
        for name in EXPECTED_SKILLS:
            skill = installed / name
            self.assertTrue(skill.is_dir())
            self.assertFalse(skill.is_symlink())
            self.assertEqual(stat.S_IMODE(skill.stat().st_mode), 0o555)
        self.assertEqual(
            (installed / "workflow" / "SKILL.md").read_bytes(),
            (self.repo / "skills" / "workflow" / "SKILL.md").read_bytes(),
        )
        self.assertEqual(
            stat.S_IMODE((installed / "workflow" / "SKILL.md").stat().st_mode),
            0o444,
        )
        self.assertEqual(
            stat.S_IMODE((installed / "workflow" / "run.sh").stat().st_mode),
            0o555,
        )

        result = verify_loaded_checkout(self.repo, self.codex_home)
        self.assertEqual(result.classification, "ready")
        self.assertEqual(result.result, "pass")
        self.assertEqual(result.manifest, manifest)

    def test_plugin_hash_is_canonical_head_blob_content(self):
        manifest = install_checkout_skills(self.repo, self.codex_home)
        plugin = subprocess.run(
            ("git", "cat-file", "blob", "HEAD:.codex-plugin/plugin.json"),
            cwd=str(self.repo),
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        canonical = json.dumps(
            json.loads(plugin),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        self.assertEqual(
            manifest.plugin_manifest_hash,
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def test_dirty_checkout_is_rejected_before_install(self):
        (self.repo / "skills" / "workflow" / "SKILL.md").write_text(
            "dirty\n", encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)
        self.assertEqual(tuple(self.codex_home.iterdir()), ())

    def test_ignored_source_content_is_not_read_or_materialized(self):
        (self.repo / ".gitignore").write_text("ignored-*\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore fixture")
        secret = self.root / "outside-secret"
        secret.write_text("must not be loaded\n", encoding="utf-8")
        ignored = self.repo / "skills" / "workflow" / "ignored-link"
        ignored.symlink_to(secret)

        install_checkout_skills(self.repo, self.codex_home)

        self.assertFalse((self.codex_home / "skills" / "workflow" / "ignored-link").exists())
        self.assertEqual(
            verify_loaded_checkout(self.repo, self.codex_home).classification, "ready"
        )

    def test_worktree_mutation_after_install_cannot_change_loaded_bytes(self):
        installed = self.codex_home / "skills" / "workflow" / "SKILL.md"
        original = (self.repo / "skills" / "workflow" / "SKILL.md").read_bytes()
        install_checkout_skills(self.repo, self.codex_home)
        source = self.repo / "skills" / "workflow" / "SKILL.md"
        source.write_text("changed worktree only\n", encoding="utf-8")

        self.assertEqual(installed.read_bytes(), original)
        self.assertEqual(
            verify_loaded_checkout(self.repo, self.codex_home).classification, "ready"
        )

    def test_head_tree_change_after_install_blocks_preflight(self):
        install_checkout_skills(self.repo, self.codex_home)
        marker = self.repo / "tracked-marker"
        marker.write_text("next\n", encoding="utf-8")
        self.git("add", "tracked-marker")
        self.git("commit", "-qm", "next")

        result = verify_loaded_checkout(self.repo, self.codex_home)

        self.assertEqual(result.classification, "blocked_isolation")

    def test_changed_mode_content_extra_or_link_blocks_preflight(self):
        mutations = ("mode", "content", "extra", "link")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                home = self.new_home("home-{}".format(mutation))
                install_checkout_skills(self.repo, home)
                target = home / "skills" / "workflow" / "SKILL.md"
                if mutation == "mode":
                    target.chmod(0o644)
                elif mutation == "content":
                    target.chmod(0o644)
                    target.write_text("tampered\n", encoding="utf-8")
                elif mutation == "extra":
                    directory = home / "skills" / "workflow"
                    directory.chmod(0o755)
                    (directory / "extra").write_text("extra\n", encoding="utf-8")
                else:
                    target.parent.chmod(0o755)
                    target.unlink()
                    target.symlink_to(self.repo / "skills" / "workflow" / "SKILL.md")

                result = verify_loaded_checkout(self.repo, home)

                self.assertEqual(result.classification, "blocked_isolation")
                self.assertEqual(result.result, "blocked")

    def test_unexpected_skill_and_tampered_manifest_block_preflight(self):
        unexpected_home = self.new_home("home-unexpected")
        install_checkout_skills(self.repo, unexpected_home)
        skills = unexpected_home / "skills"
        skills.chmod(0o755)
        (skills / "surprise").mkdir()
        self.assertEqual(
            verify_loaded_checkout(self.repo, unexpected_home).classification,
            "blocked_isolation",
        )

        manifest_home = self.new_home("home-manifest")
        install_checkout_skills(self.repo, manifest_home)
        manifest_path = manifest_home / ".live-eval-checkout.json"
        manifest_path.chmod(0o600)
        manifest_path.write_text("{}", encoding="utf-8")
        self.assertEqual(
            verify_loaded_checkout(self.repo, manifest_home).classification,
            "blocked_isolation",
        )

    def test_tracked_symlink_entry_is_rejected_without_reading_target(self):
        secret = self.root / "outside-secret"
        secret.write_text("must not be read\n", encoding="utf-8")
        external_link = self.repo / "skills" / "workflow" / "external"
        external_link.symlink_to(secret)
        self.git("add", "skills/workflow/external")
        self.git("commit", "-qm", "external link")
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o600)

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)

    def test_requires_private_empty_codex_home_without_clobbering(self):
        occupied = self.new_home("occupied-home")
        marker = occupied / "marker"
        marker.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, occupied)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

        permissive = self.new_home("permissive-home")
        permissive.chmod(0o755)
        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, permissive)

    def test_publish_race_does_not_delete_competing_skills_directory(self):
        marker = self.codex_home / "skills" / "competitor"

        def competing_publish(_staged, target):
            target.mkdir()
            marker.write_text("keep\n", encoding="utf-8")
            raise FileExistsError("simulated concurrent publisher")

        with patch(
            "scripts.live_eval.checkout._publish_directory_noreplace",
            side_effect=competing_publish,
        ):
            with self.assertRaises(FileExistsError):
                install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_manifest_race_does_not_delete_competing_manifest(self):
        original_write = checkout_module._write_exclusive
        marker = self.codex_home / ".live-eval-checkout.json"

        def competing_write(path, content, mode):
            if path == marker:
                marker.write_text("competitor\n", encoding="utf-8")
                raise FileExistsError("simulated concurrent manifest writer")
            return original_write(path, content, mode)

        with patch(
            "scripts.live_eval.checkout._write_exclusive",
            side_effect=competing_write,
        ):
            with self.assertRaises(FileExistsError):
                install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(marker.read_text(encoding="utf-8"), "competitor\n")

    def test_head_change_during_materialization_blocks_publish(self):
        original_verify = checkout_module._verify_materialized
        changed = []

        def change_head_after_verify(root, snapshot, directory_mode=0o555):
            result = original_verify(root, snapshot, directory_mode)
            if not changed and directory_mode == 0o700:
                marker = self.repo / "concurrent-head-change"
                marker.write_text("next\n", encoding="utf-8")
                self.git("add", "concurrent-head-change")
                self.git("commit", "-qm", "concurrent head change")
                changed.append(True)
            return result

        with patch(
            "scripts.live_eval.checkout._verify_materialized",
            side_effect=change_head_after_verify,
        ):
            with self.assertRaises(ValueError):
                install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(tuple(self.codex_home.iterdir()), ())

    def test_post_publish_path_swap_preserves_replacement_on_later_failure(self):
        original_write = checkout_module._write_exclusive
        skills = self.codex_home / "skills"
        replacement = skills / "competitor"

        def swap_skills_then_fail(path, content, mode):
            if path.name == ".live-eval-checkout.json":
                skills.chmod(0o755)
                skills.rename(self.codex_home / "owned-skills-moved-away")
                skills.mkdir()
                replacement.write_text("keep\n", encoding="utf-8")
                raise OSError("simulated post-publish failure")
            return original_write(path, content, mode)

        with patch(
            "scripts.live_eval.checkout._write_exclusive",
            side_effect=swap_skills_then_fail,
        ):
            with self.assertRaises(OSError) as raised:
                install_checkout_skills(self.repo, self.codex_home)

        self.assertTrue(raised.exception.cleanup_warnings)
        self.assertEqual(replacement.read_text(encoding="utf-8"), "keep\n")

    def test_post_manifest_path_swap_preserves_replacement_on_later_failure(self):
        original_fsync = checkout_module._fsync_directory
        manifest = self.codex_home / ".live-eval-checkout.json"

        def swap_manifest_then_fail(path):
            if path == self.codex_home:
                manifest.rename(self.codex_home / "owned-manifest-moved-away")
                manifest.write_text("competitor\n", encoding="utf-8")
                raise OSError("simulated post-manifest failure")
            return original_fsync(path)

        with patch(
            "scripts.live_eval.checkout._fsync_directory",
            side_effect=swap_manifest_then_fail,
        ):
            with self.assertRaises(OSError) as raised:
                install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(manifest.read_text(encoding="utf-8"), "competitor\n")
        self.assertFalse((self.codex_home / "skills").exists())
        self.assertTrue(raised.exception.cleanup_warnings)

    def test_staging_path_recreation_is_preserved_on_failure(self):
        original_identity = checkout_module._require_install_identity
        replacement_paths = []

        def swap_staging_then_fail(repo, manifest):
            staged = next(self.codex_home.glob(".skills-stage-*"))
            staged.rename(self.codex_home / "owned-stage-moved-away")
            staged.mkdir()
            replacement = staged / "competitor"
            replacement.write_text("keep\n", encoding="utf-8")
            replacement_paths.append(replacement)
            original_identity(repo, manifest)
            raise OSError("simulated staging failure")

        with patch(
            "scripts.live_eval.checkout._require_install_identity",
            side_effect=swap_staging_then_fail,
        ):
            with self.assertRaises(OSError) as raised:
                install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(replacement_paths[0].read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(raised.exception.cleanup_warnings)

    def test_unchanged_owned_paths_are_removed_after_late_failure(self):
        original_fsync = checkout_module._fsync_directory

        def fail_home_fsync(path):
            if path == self.codex_home:
                raise OSError("simulated final fsync failure")
            return original_fsync(path)

        with patch(
            "scripts.live_eval.checkout._fsync_directory",
            side_effect=fail_home_fsync,
        ):
            with self.assertRaises(OSError):
                install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(tuple(self.codex_home.iterdir()), ())

    def test_canonical_name_key_rejects_case_and_unicode_aliases_deterministically(self):
        self.assertEqual(canonical_name_key("workflow"), canonical_name_key("WORKFLOW"))
        self.assertEqual(canonical_name_key("é"), canonical_name_key("e\u0301"))
        with self.assertRaises(ValueError):
            require_unique_canonical_names(("workflow", "WORKFLOW"))
        with self.assertRaises(ValueError):
            require_unique_canonical_names(("é", "e\u0301"))

    def test_source_checkout_is_not_mutated_by_installer(self):
        before = self.git("status", "--porcelain=v1", "--untracked-files=all")

        install_checkout_skills(self.repo, self.codex_home)

        after = self.git("status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
