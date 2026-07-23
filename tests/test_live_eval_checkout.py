import hashlib
import inspect
import json
import os
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

    def test_public_legacy_verifier_rejects_harness_only_entries(self):
        install_checkout_skills(self.repo, self.codex_home)
        agents = self.codex_home / "AGENTS.md"
        agents.write_text("harness only\n", encoding="utf-8")

        result = verify_loaded_checkout(self.repo, self.codex_home)

        self.assertEqual(result.classification, "blocked_isolation")
        self.assertEqual(result.result, "blocked")

    def test_public_api_and_manifest_schema_remain_legacy(self):
        self.assertEqual(
            tuple(inspect.signature(verify_loaded_checkout).parameters),
            ("repo", "codex_home"),
        )
        self.assertEqual(
            tuple(checkout_module.CheckoutManifest.__dataclass_fields__),
            (
                "object_format",
                "tree_hash",
                "plugin_blob_oid",
                "plugin_manifest_hash",
                "skill_hashes",
                "materialized_hashes",
                "skill_names",
            ),
        )
        install_checkout_skills(self.repo, self.codex_home)
        value = json.loads(
            (self.codex_home / ".live-eval-checkout.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(value), checkout_module._MANIFEST_FIELDS)

    def test_materialized_hardlink_blocks_preflight(self):
        install_checkout_skills(self.repo, self.codex_home)
        target = self.codex_home / "skills/workflow/SKILL.md"
        outside = self.root / "outside-hardlink"
        os.link(target, outside)

        result = verify_loaded_checkout(self.repo, self.codex_home)

        self.assertEqual(result.classification, "blocked_isolation")

    def test_git_snapshot_disables_malicious_fsmonitor_and_filters_environment(self):
        sentinel = self.root / "fsmonitor-ran"
        leaked = self.root / "fsmonitor-env"
        hook = self.root / "fsmonitor-hook.sh"
        hook.write_text(
            "#!/bin/sh\n"
            "touch '{}'\n"
            "env > '{}'\n"
            "exit 0\n".format(sentinel, leaked),
            encoding="utf-8",
        )
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        original_run = checkout_module.subprocess.run
        git_environments = []

        def capture_git_environment(*arguments, **keywords):
            git_environments.append(dict(keywords["env"]))
            return original_run(*arguments, **keywords)

        with patch.dict(os.environ, {"HARNESS_TEST_SECRET": "do-not-leak"}), patch(
            "scripts.live_eval.checkout.subprocess.run",
            side_effect=capture_git_environment,
        ):
            install_checkout_skills(self.repo, self.codex_home)

        self.assertFalse(sentinel.exists())
        self.assertFalse(leaked.exists())
        self.assertTrue(git_environments)
        for environment in git_environments:
            self.assertEqual(environment["GIT_ATTR_NOSYSTEM"], "1")
            self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertNotIn("HARNESS_TEST_SECRET", environment)

    def test_external_core_files_are_rejected_before_git_root_lookup(self):
        external_attributes = self.root / "external-attributes"
        external_attributes.write_text("*.md filter=external\n", encoding="utf-8")
        external_excludes = self.root / "external-excludes"
        external_excludes.write_text("hidden-untracked\n", encoding="utf-8")
        cases = (
            ("core.attributesFile", str(external_attributes)),
            ("core.excludesFile", str(external_excludes)),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                home = self.new_home("external-core-home-{}".format(index))
                self.git("config", "--local", key, value)
                try:
                    with patch(
                        "scripts.live_eval.checkout._require_git_root",
                        side_effect=AssertionError("ordinary Git root lookup occurred"),
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "^unsupported local Git configuration$",
                        ) as raised:
                            install_checkout_skills(self.repo, home)
                    self.assertNotIn(str(self.root), str(raised.exception))
                finally:
                    self.git("config", "--local", "--unset-all", key)

    def test_local_filter_drivers_are_rejected_before_git_root_lookup(self):
        cases = (
            ("filter.external.clean", "cat"),
            ("filter.external.smudge", "cat"),
            ("filter.external.process", "cat"),
            ("filter.external.required", "false"),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                home = self.new_home("filter-home-{}".format(index))
                self.git("config", "--local", key, value)
                try:
                    with patch(
                        "scripts.live_eval.checkout._require_git_root",
                        side_effect=AssertionError("ordinary Git root lookup occurred"),
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "^unsupported local Git configuration$",
                        ) as raised:
                            install_checkout_skills(self.repo, home)
                    self.assertNotIn(str(self.root), str(raised.exception))
                finally:
                    self.git("config", "--local", "--unset-all", key)

    def test_tracked_attributes_filter_never_executes_before_policy_rejection(self):
        sentinel = self.root / "filter-ran"
        filter_driver = self.root / "filter-driver.sh"
        filter_driver.write_text(
            "#!/bin/sh\n"
            'touch "$(dirname "$0")/filter-ran"\n'
            "cat\n",
            encoding="utf-8",
        )
        filter_driver.chmod(0o700)
        (self.repo / ".gitattributes").write_text(
            "skills/workflow/SKILL.md filter=external\n",
            encoding="utf-8",
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes fixture")
        self.git(
            "config",
            "--local",
            "filter.external.clean",
            str(filter_driver),
        )
        try:
            with patch(
                "scripts.live_eval.checkout._require_git_root",
                side_effect=AssertionError("ordinary Git root lookup occurred"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^unsupported local Git configuration$",
                ):
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git(
                "config",
                "--local",
                "--unset-all",
                "filter.external.clean",
            )

        self.assertFalse(sentinel.exists())
        self.assertEqual(tuple(self.codex_home.iterdir()), ())

    def test_raw_local_include_directives_are_rejected_before_git_root_or_object_reads(self):
        included = self.root / "included-config"
        included.write_text(
            '[remote "included"]\n\tpromisor = true\n',
            encoding="utf-8",
        )
        cases = (
            ("include.path", str(included)),
            (
                "includeIf.gitdir:{}/.path".format(self.repo),
                str(included),
            ),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                home = self.new_home("include-home-{}".format(index))
                self.git("config", "--local", "--add", key, value)
                try:
                    with patch(
                        "scripts.live_eval.checkout._require_git_root",
                        side_effect=AssertionError("ordinary Git root lookup occurred"),
                    ), patch(
                        "scripts.live_eval.checkout._cat_blob",
                        side_effect=AssertionError("object read occurred"),
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "^unsupported local Git configuration$",
                        ) as raised:
                            install_checkout_skills(self.repo, home)
                    self.assertNotIn(str(self.root), str(raised.exception))
                    self.assertNotIn("promisor", str(raised.exception))
                finally:
                    self.git("config", "--local", "--unset-all", key)

    def test_external_promisor_include_is_not_expanded_by_raw_config_snapshot(self):
        included = self.root / "external-promisor-config"
        included.write_text(
            '[remote "external"]\n\tpromisor = true\n',
            encoding="utf-8",
        )
        self.git("config", "--local", "--add", "include.path", str(included))
        original_run = checkout_module._run_git
        calls = []

        def capture_raw_config(repo, *arguments):
            calls.append(arguments)
            return original_run(repo, *arguments)

        try:
            with patch(
                "scripts.live_eval.checkout._run_git",
                side_effect=capture_raw_config,
            ), patch(
                "scripts.live_eval.checkout._require_git_root",
                side_effect=AssertionError("ordinary Git root lookup occurred"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^unsupported local Git configuration$",
                ):
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git("config", "--local", "--unset-all", "include.path")

        self.assertEqual(
            calls,
            [("config", "--local", "--no-includes", "--null", "--list")],
        )

    def test_worktree_config_extension_is_rejected_before_git_root_lookup(self):
        self.git(
            "config",
            "--local",
            "extensions.worktreeConfig",
            "true",
        )
        try:
            with patch(
                "scripts.live_eval.checkout._require_git_root",
                side_effect=AssertionError("ordinary Git root lookup occurred"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^unsupported local Git configuration$",
                ):
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git(
                "config",
                "--local",
                "--unset-all",
                "extensions.worktreeConfig",
            )

    def test_local_config_mutation_during_snapshot_is_rejected(self):
        original_cat_blob = checkout_module._cat_blob
        mutated = []

        def mutate_after_object_read(repo, oid):
            content = original_cat_blob(repo, oid)
            if not mutated:
                self.git("config", "--local", "harness.snapshot", "changed")
                mutated.append(True)
            return content

        try:
            with patch(
                "scripts.live_eval.checkout._cat_blob",
                side_effect=mutate_after_object_read,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^local Git configuration changed$",
                ) as raised:
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git("config", "--local", "--unset-all", "harness.snapshot")

        self.assertEqual(tuple(self.codex_home.iterdir()), ())
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_forbidden_config_mutation_is_normalized_as_local_config_change(self):
        original_cat_blob = checkout_module._cat_blob
        included = self.root / "late-included-config"
        included.write_text(
            '[remote "late"]\n\tpromisor = true\n',
            encoding="utf-8",
        )
        mutated = []

        def add_forbidden_key_after_object_read(repo, oid):
            content = original_cat_blob(repo, oid)
            if not mutated:
                self.git(
                    "config",
                    "--local",
                    "include.path",
                    str(included),
                )
                mutated.append(True)
            return content

        try:
            with patch(
                "scripts.live_eval.checkout._cat_blob",
                side_effect=add_forbidden_key_after_object_read,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^local Git configuration changed$",
                ):
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git(
                "config",
                "--local",
                "--unset-all",
                "include.path",
            )

        self.assertEqual(tuple(self.codex_home.iterdir()), ())

    def test_local_config_mutation_before_publication_is_rejected(self):
        original_identity = checkout_module._require_install_identity
        mutated = []

        def mutate_before_identity_check(repo, captured):
            if not mutated:
                self.git("config", "--local", "harness.publication", "changed")
                mutated.append(True)
            return original_identity(repo, captured)

        try:
            with patch(
                "scripts.live_eval.checkout._require_install_identity",
                side_effect=mutate_before_identity_check,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^local Git configuration changed$",
                ) as raised:
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git("config", "--local", "--unset-all", "harness.publication")

        self.assertEqual(tuple(self.codex_home.iterdir()), ())
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_local_config_mutation_during_publication_identity_git_reads_is_rejected(self):
        original_identity = checkout_module._require_install_identity
        original_git_text = checkout_module._git_text
        checking_identity = []
        mutated = []

        def mutate_after_identity_git_read(repo, *arguments):
            value = original_git_text(repo, *arguments)
            if checking_identity and not mutated:
                self.git(
                    "config",
                    "--local",
                    "harness.identity-read",
                    "changed",
                )
                mutated.append(True)
            return value

        def check_identity(repo, captured):
            checking_identity.append(True)
            try:
                return original_identity(repo, captured)
            finally:
                checking_identity.pop()

        try:
            with patch(
                "scripts.live_eval.checkout._require_install_identity",
                side_effect=check_identity,
            ), patch(
                "scripts.live_eval.checkout._git_text",
                side_effect=mutate_after_identity_git_read,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^local Git configuration changed$",
                ):
                    install_checkout_skills(self.repo, self.codex_home)
        finally:
            self.git("config", "--local", "--unset-all", "harness.identity-read")

        self.assertEqual(tuple(self.codex_home.iterdir()), ())

    def test_raw_config_fixture_rejects_empty_remote_promisor_key(self):
        raw = subprocess.CompletedProcess(
            args=("git", "config"),
            returncode=0,
            stdout=b"remote..promisor\nfalse\0",
            stderr=b"",
        )

        with patch(
            "scripts.live_eval.checkout._run_git",
            return_value=raw,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "^unsupported partial or promisor Git repository$",
            ):
                checkout_module._raw_local_config_snapshot(self.repo)

    def test_partial_or_promisor_repository_is_rejected_before_object_reads(self):
        cases = (
            ("extensions.partialClone", "origin"),
            ("remote.origin.promisor", "true"),
            ("remote.backup.promisor", "false"),
        )
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key):
                home = self.new_home("partial-home-{}".format(index))
                if key == "extensions.partialClone":
                    self.git("config", "core.repositoryFormatVersion", "1")
                self.git("config", key, value)
                try:
                    with patch(
                        "scripts.live_eval.checkout._cat_blob",
                        side_effect=AssertionError("object read occurred"),
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "^unsupported partial or promisor Git repository$",
                        ):
                            install_checkout_skills(self.repo, home)
                finally:
                    self.git("config", "--unset-all", key)
                    if key == "extensions.partialClone":
                        self.git("config", "core.repositoryFormatVersion", "0")

    def test_replace_ref_cannot_change_materialized_head_blob_bytes(self):
        source = self.repo / "skills/workflow/SKILL.md"
        original = source.read_bytes()
        original_oid = self.git("rev-parse", "HEAD:skills/workflow/SKILL.md")
        replacement = subprocess.run(
            ("git", "hash-object", "-w", "--stdin"),
            cwd=str(self.repo),
            check=True,
            input=b"replacement bytes\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode("ascii").strip()
        self.git("replace", original_oid, replacement)

        install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(
            (self.codex_home / "skills/workflow/SKILL.md").read_bytes(),
            original,
        )


if __name__ == "__main__":
    unittest.main()
