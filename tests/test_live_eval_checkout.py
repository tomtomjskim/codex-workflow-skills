import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.live_eval.checkout import (
    install_checkout_skills,
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

    def test_installs_only_expected_skills_and_hashes_them(self):
        manifest = install_checkout_skills(self.repo, self.codex_home)

        self.assertEqual(manifest.skill_names, EXPECTED_SKILLS)
        self.assertEqual(manifest.tree_hash, self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(tuple(manifest.skill_hashes), EXPECTED_SKILLS)
        self.assertTrue(
            all(value.startswith("sha256:") for value in manifest.skill_hashes.values())
        )
        canonical_plugin = json.dumps(
            json.loads((self.repo / ".codex-plugin" / "plugin.json").read_text()),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            manifest.plugin_manifest_hash,
            "sha256:" + hashlib.sha256(canonical_plugin).hexdigest(),
        )
        installed = self.codex_home / "skills"
        self.assertEqual(
            tuple(sorted(item.name for item in installed.iterdir())), EXPECTED_SKILLS
        )
        for name in EXPECTED_SKILLS:
            link = installed / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (self.repo / "skills" / name).resolve())

        result = verify_loaded_checkout(self.repo, self.codex_home)
        self.assertEqual(result.classification, "ready")
        self.assertEqual(result.result, "pass")
        self.assertEqual(result.manifest, manifest)

    def test_reinstall_is_refused_instead_of_overwriting_existing_state(self):
        install_checkout_skills(self.repo, self.codex_home)

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)

    def test_dirty_checkout_is_rejected_before_install(self):
        (self.repo / "skills" / "workflow" / "SKILL.md").write_text(
            "dirty\n", encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)
        self.assertFalse((self.codex_home / "skills").exists())

    def test_untracked_checkout_content_is_rejected_before_install(self):
        (self.repo / "skills" / "workflow" / "untracked.md").write_text(
            "untracked\n", encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)
        self.assertFalse((self.codex_home / "skills").exists())

    def test_ignored_file_inside_skill_is_rejected_before_install(self):
        (self.repo / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore fixture")
        (self.repo / "skills" / "workflow" / "ignored.md").write_text(
            "ignored but loadable\n", encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)
        self.assertFalse((self.codex_home / "skills").exists())

    def test_unexpected_skill_or_copy_blocks_preflight(self):
        mutations = ("unexpected", "copy")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                home = self.root / "home-{}".format(mutation)
                home.mkdir()
                install_checkout_skills(self.repo, home)
                if mutation == "unexpected":
                    (home / "skills" / "surprise").mkdir()
                else:
                    link = home / "skills" / "workflow"
                    link.unlink()
                    shutil.copytree(self.repo / "skills" / "workflow", link)

                result = verify_loaded_checkout(self.repo, home)

                self.assertEqual(result.classification, "blocked_isolation")
                self.assertEqual(result.result, "blocked")

    def test_broken_retargeted_or_escaped_link_blocks_preflight(self):
        mutations = ("broken", "retargeted", "escaped")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                home = self.root / "home-{}".format(mutation)
                home.mkdir()
                install_checkout_skills(self.repo, home)
                link = home / "skills" / "workflow"
                link.unlink()
                if mutation == "broken":
                    link.symlink_to(self.repo / "skills" / "missing")
                elif mutation == "retargeted":
                    link.symlink_to(self.repo / "skills" / "workflow-intake")
                else:
                    external = self.root / "external-skill"
                    external.mkdir(exist_ok=True)
                    link.symlink_to(external)

                result = verify_loaded_checkout(self.repo, home)

                self.assertEqual(result.classification, "blocked_isolation")

    def test_changed_file_or_checkout_identity_after_manifest_blocks_preflight(self):
        mutations = ("skill", "plugin", "head")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                home = self.root / "home-{}".format(mutation)
                home.mkdir()
                install_checkout_skills(self.repo, home)
                if mutation == "skill":
                    (self.repo / "skills" / "workflow" / "SKILL.md").write_text(
                        "changed\n", encoding="utf-8"
                    )
                elif mutation == "plugin":
                    (self.repo / ".codex-plugin" / "plugin.json").write_text(
                        '{"name":"changed"}\n', encoding="utf-8"
                    )
                else:
                    marker = self.repo / "tracked-marker"
                    marker.write_text("next\n", encoding="utf-8")
                    self.git("add", "tracked-marker")
                    self.git("commit", "-qm", "next")

                result = verify_loaded_checkout(self.repo, home)

                self.assertEqual(result.classification, "blocked_isolation")
                self.git("reset", "--hard", "-q", "HEAD")
                if mutation != "head":
                    self.git("clean", "-fdq")

    def test_case_alias_blocks_preflight(self):
        install_checkout_skills(self.repo, self.codex_home)
        alias = self.codex_home / "skills" / "WORKFLOW"
        try:
            alias.symlink_to(self.repo / "skills" / "workflow")
        except FileExistsError:
            self.skipTest("filesystem is case-insensitive")

        result = verify_loaded_checkout(self.repo, self.codex_home)

        self.assertEqual(result.classification, "blocked_isolation")

    def test_external_symlink_in_skill_is_rejected_without_reading_target(self):
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

    def test_nested_external_symlink_escape_is_rejected(self):
        external = self.root / "external-directory"
        external.mkdir()
        (external / "policy.md").write_text("outside\n", encoding="utf-8")
        bridge = self.repo / "bridge"
        bridge.symlink_to(external, target_is_directory=True)
        link = self.repo / "skills" / "workflow" / "nested-escape"
        link.symlink_to(self.repo / "bridge" / "policy.md")
        self.git("add", "bridge", "skills/workflow/nested-escape")
        self.git("commit", "-qm", "nested link escape")

        with self.assertRaises(ValueError):
            install_checkout_skills(self.repo, self.codex_home)

    def test_source_checkout_is_not_mutated_by_installer(self):
        before = self.git("status", "--porcelain=v1", "--untracked-files=all")

        install_checkout_skills(self.repo, self.codex_home)

        after = self.git("status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
