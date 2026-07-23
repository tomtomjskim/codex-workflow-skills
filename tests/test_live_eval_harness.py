import dataclasses
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.live_eval.harness as harness_module
from scripts.live_eval.harness import (
    HarnessManifest,
    HarnessSourceManifest,
    load_harness_source,
    materialize_harness_home,
    verify_loaded_harness,
)


EXPECTED_SKILLS = (
    "adversarial-review-loop",
    "workflow",
    "workflow-intake",
)
EXPECTED_ROLES = (
    "accessibility-reviewer",
    "api-reviewer",
    "architect",
    "code-reviewer",
    "dba",
    "designer",
    "developer",
    "documenter",
    "explorer",
    "performance-reviewer",
    "pm",
    "publisher",
    "qa-engineer",
    "security-reviewer",
    "test-coverage-reviewer",
    "ux-reviewer",
)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.repo = self.root / "repo"
        self.bundle = self.root / "bundle"
        self._make_skill_repo()
        self._make_bundle(self.bundle)

    def _make_skill_repo(self):
        self.repo.mkdir(mode=0o700)
        (self.repo / ".codex-plugin").mkdir()
        (self.repo / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "harness-test",
                    "skills": "./skills/",
                    "version": "1.0.0",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        for index, name in enumerate(EXPECTED_SKILLS):
            skill = self.repo / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: {}\n---\npolicy {}\n".format(name, index),
                encoding="utf-8",
            )
        self._git("init", "-q")
        self._git("config", "user.email", "harness@example.invalid")
        self._git("config", "user.name", "Harness Test")
        self._git("add", ".")
        self._git("commit", "-qm", "fixture")

    def _git(self, *arguments):
        return subprocess.run(
            ("git",) + arguments,
            cwd=str(self.repo),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _make_bundle(self, root):
        (root / "profiles" / "current").mkdir(parents=True, mode=0o700)
        (root / "profiles" / "lean").mkdir(mode=0o700)
        (root / "shared" / "agents").mkdir(parents=True, mode=0o700)
        (root / "shared" / "common-agents").mkdir(mode=0o700)
        files = {
            "harness.json": json.dumps(
                {"bundle_id": "fixture-v1", "schema_version": 1},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "profiles/current/AGENTS.md": "# Current\nUse the complete policy.\n",
            "profiles/lean/AGENTS.md": "# Lean\nUse the compact policy.\n",
        }
        for role in EXPECTED_ROLES:
            files["shared/agents/{}.toml".format(role)] = (
                'name = "{}"\n'.format(role)
                + 'description = "Common {} role. Uses '
                '~/.agents/common-agents/{}.md."\n'.format(role, role)
                + 'model_reasoning_effort = "medium"\n'
                + 'developer_instructions = """\n'
                + "# {} Adapter\n\n".format(role)
                + 'Before acting, read and follow '
                '`/private/fixture/.agents/common-agents/{}.md`.\n\n'.format(role)
                + "Project-local instructions override this adapter.\n"
                + '"""\n'
            )
            files["shared/common-agents/{}.md".format(role)] = (
                "# {}\nRole policy.\n".format(role)
            )
        for relative, content in files.items():
            path = root / relative
            path.write_text(content, encoding="utf-8")
            path.chmod(0o600)
        for path in (root,) + tuple(
            item for item in root.rglob("*") if item.is_dir()
        ):
            path.chmod(0o700)

    def _new_home(self, name):
        home = self.root / name
        home.mkdir(mode=0o700)
        return home

    def _materialize(self, profile="current", name="home"):
        home = self._new_home(name)
        manifest = materialize_harness_home(
            self.repo, self.bundle, profile, home
        )
        return home, manifest

    def assertSanitized(self, value):
        rendered = repr(value)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("Before acting", rendered)
        self.assertNotIn("Build safely", rendered)

    def test_profiles_materialize_fixed_inventory_with_stable_shared_hashes(self):
        current_home, current = self._materialize("current", "current-home")
        lean_home, lean = self._materialize("lean", "lean-home")

        self.assertIsInstance(current, HarnessManifest)
        self.assertNotEqual(current.agents_hash, lean.agents_hash)
        self.assertEqual(current.adapter_source_hash, lean.adapter_source_hash)
        self.assertEqual(
            current.adapter_materialized_hash, lean.adapter_materialized_hash
        )
        self.assertEqual(current.common_role_hash, lean.common_role_hash)
        self.assertEqual(current.skill_routing_hash, lean.skill_routing_hash)
        self.assertEqual(current.bundle_digest, lean.bundle_digest)
        self.assertEqual(current.adapter_count, len(EXPECTED_ROLES))
        self.assertEqual(current.role_count, len(EXPECTED_ROLES))
        expected = (
            ".agents",
            ".live-eval-checkout.json",
            "AGENTS.md",
            "agents",
            "skills",
        )
        for home in (current_home, lean_home):
            self.assertEqual(tuple(sorted(item.name for item in home.iterdir())), expected)
            self.assertEqual(stat.S_IMODE((home / "AGENTS.md").lstat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE((home / "agents").lstat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE((home / ".agents").lstat().st_mode), 0o555)
        self.assertSanitized(current)
        self.assertSanitized(lean)

    def test_adapter_include_is_rewritten_once_without_reformatting_toml(self):
        home, _manifest = self._materialize()
        source = (self.bundle / "shared/agents/developer.toml").read_text(
            encoding="utf-8"
        )
        materialized = (home / "agents/developer.toml").read_text(encoding="utf-8")

        self.assertEqual(
            materialized,
            source.replace(
                "/private/fixture/.agents/common-agents/developer.md",
                "~/.agents/common-agents/developer.md",
                1,
            ),
        )
        self.assertEqual(materialized.count("~/.agents/common-agents/developer.md"), 2)

    def test_realistic_multiline_adapter_schema_is_accepted(self):
        manifest = load_harness_source(self.bundle, "current")

        self.assertEqual(manifest.adapter_count, len(EXPECTED_ROLES))

    def test_trailing_adapter_content_is_rejected(self):
        adapter = self.bundle / "shared/agents/developer.toml"
        adapter.write_text(
            adapter.read_text(encoding="utf-8") + 'unexpected = "value"\n',
            encoding="utf-8",
        )
        adapter.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "^invalid_bundle$"):
            load_harness_source(self.bundle, "current")

    def test_missing_duplicate_and_wrong_role_includes_are_rejected(self):
        adapter = self.bundle / "shared/agents/developer.toml"
        original = adapter.read_text(encoding="utf-8")
        mutations = {
            "missing": original.replace(
                "`/private/fixture/.agents/common-agents/developer.md`", "no include"
            ),
            "duplicate": original.replace(
                "developer.md`", "developer.md` and `/other/.agents/common-agents/developer.md`"
            ),
            "wrong-role": original.replace("developer.md`", "reviewer.md`"),
        }
        for name, content in mutations.items():
            with self.subTest(name=name):
                adapter.write_text(content, encoding="utf-8")
                adapter.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "^invalid_bundle$") as raised:
                    load_harness_source(self.bundle, "current")
                self.assertSanitized(raised.exception)
                adapter.write_text(original, encoding="utf-8")
                adapter.chmod(0o600)

    def test_invalid_manifest_schema_and_profile_are_rejected(self):
        manifest = self.bundle / "harness.json"
        cases = (
            {"bundle_id": "", "schema_version": 1},
            {"bundle_id": "fixture-v1", "schema_version": 2},
            {"bundle_id": "fixture-v1", "schema_version": 1, "paths": []},
            {"bundle_id": "UPPER", "schema_version": 1},
            {"bundle_id": "a" * 65, "schema_version": 1},
            {"bundle_id": "white space", "schema_version": 1},
        )
        for value in cases:
            with self.subTest(value=value):
                manifest.write_text(json.dumps(value), encoding="utf-8")
                manifest.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "^invalid_bundle$"):
                    load_harness_source(self.bundle, "current")
        with self.assertRaisesRegex(ValueError, "^invalid_bundle$"):
            load_harness_source(self.bundle, "CURRENT")

    def test_exact_role_allowlist_rejects_matched_pair_deletion_or_addition(self):
        missing = self.root / "bundle-missing-pair"
        self._make_bundle(missing)
        (missing / "shared/agents/architect.toml").unlink()
        (missing / "shared/common-agents/architect.md").unlink()
        with self.assertRaisesRegex(ValueError, "^invalid_bundle$"):
            load_harness_source(missing, "current")

        extra = self.root / "bundle-extra-pair"
        self._make_bundle(extra)
        adapter = extra / "shared/agents/intruder.toml"
        adapter.write_text(
            'name = "intruder"\n'
            'developer_instructions = "Read '
            '`/private/fixture/.agents/common-agents/intruder.md`."\n',
            encoding="utf-8",
        )
        role = extra / "shared/common-agents/intruder.md"
        role.write_text("intruder\n", encoding="utf-8")
        adapter.chmod(0o600)
        role.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "^invalid_bundle$"):
            load_harness_source(extra, "current")

    def test_bundle_digest_distinguishes_same_id_content_and_profiles_must_differ(self):
        current = load_harness_source(self.bundle, "current")
        changed = self.root / "bundle-same-id"
        self._make_bundle(changed)
        changed_agents = changed / "profiles/lean/AGENTS.md"
        changed_agents.write_text("# Lean\nUse a different compact policy.\n", encoding="utf-8")
        changed_agents.chmod(0o600)
        changed_manifest = load_harness_source(changed, "current")
        self.assertEqual(current.bundle_id, changed_manifest.bundle_id)
        self.assertNotEqual(current.bundle_digest, changed_manifest.bundle_digest)

        equal = self.root / "bundle-equal-profiles"
        self._make_bundle(equal)
        lean = equal / "profiles/lean/AGENTS.md"
        lean.write_bytes((equal / "profiles/current/AGENTS.md").read_bytes())
        lean.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "^invalid_bundle$"):
            load_harness_source(equal, "current")

    def test_invalid_inventory_objects_modes_and_sizes_are_rejected(self):
        mutations = (
            ("extra", lambda root: (root / "extra").write_text("x", encoding="utf-8")),
            ("missing", lambda root: (root / "profiles/lean/AGENTS.md").unlink()),
            (
                "casefold",
                lambda root: (root / "Harness.JSON").write_text("x", encoding="utf-8"),
            ),
            (
                "unicode-alias",
                lambda root: (root / "shared/common-agents/e\u0301.md").write_text(
                    "x", encoding="utf-8"
                ),
            ),
            (
                "symlink",
                lambda root: self._replace_with_symlink(
                    root / "profiles/current/AGENTS.md", root / "profiles/lean/AGENTS.md"
                ),
            ),
            (
                "special",
                lambda root: self._replace_with_fifo(root / "profiles/current/AGENTS.md"),
            ),
            ("hardlink", lambda root: self._replace_with_hardlink(root)),
            ("unsafe-mode", lambda root: (root / "harness.json").chmod(0o644)),
            (
                "oversized",
                lambda root: (root / "profiles/current/AGENTS.md").write_bytes(
                    b"x" * (2 * 1024 * 1024)
                ),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                bundle = self.root / "bundle-{}".format(name)
                self._make_bundle(bundle)
                mutate(bundle)
                with self.assertRaisesRegex(ValueError, "^invalid_bundle$") as raised:
                    load_harness_source(bundle, "current")
                self.assertSanitized(raised.exception)

    @staticmethod
    def _replace_with_symlink(path, target):
        path.unlink()
        path.symlink_to(target)

    @staticmethod
    def _replace_with_fifo(path):
        path.unlink()
        os.mkfifo(path, 0o600)

    @staticmethod
    def _replace_with_hardlink(root):
        lean = root / "profiles/lean/AGENTS.md"
        lean.unlink()
        os.link(root / "profiles/current/AGENTS.md", lean)

    def test_detected_source_mutation_uses_sanitized_reason(self):
        target = self.bundle / "profiles/current/AGENTS.md"
        original_read = os.read
        changed = []

        def mutate_after_read(descriptor, size):
            content = original_read(descriptor, size)
            if not changed:
                target.write_text("# Mutated\nUse another complete policy.\n", encoding="utf-8")
                target.chmod(0o600)
                changed.append(True)
            return content

        with patch("scripts.live_eval.harness.os.read", side_effect=mutate_after_read):
            with self.assertRaisesRegex(ValueError, "^source_changed$") as raised:
                load_harness_source(self.bundle, "current")
        self.assertSanitized(raised.exception)

    def test_source_manifest_is_immutable_and_contains_no_paths_or_source_text(self):
        manifest = load_harness_source(self.bundle, "current")

        self.assertIsInstance(manifest, HarnessSourceManifest)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.bundle_id = "changed"
        self.assertSanitized(manifest)
        for field in dataclasses.fields(manifest):
            self.assertIsNot(field.type, Path)

    def test_materialized_tampering_is_blocked_with_fixed_reason(self):
        for mutation in ("content", "mode", "link", "extra"):
            with self.subTest(mutation=mutation):
                home, manifest = self._materialize("current", "home-{}".format(mutation))
                target = home / "AGENTS.md"
                if mutation == "content":
                    target.chmod(0o600)
                    target.write_text("tampered\n", encoding="utf-8")
                    target.chmod(0o400)
                elif mutation == "mode":
                    target.chmod(0o600)
                elif mutation == "link":
                    adapter = home / "agents/developer.toml"
                    adapter.parent.chmod(0o755)
                    adapter.unlink()
                    adapter.symlink_to(self.bundle / "shared/agents/developer.toml")
                else:
                    agents = home / "agents"
                    agents.chmod(0o755)
                    extra = agents / "extra.toml"
                    extra.write_text("x", encoding="utf-8")
                    extra.chmod(0o400)

                result = verify_loaded_harness(
                    self.repo, self.bundle, home, manifest
                )

                self.assertEqual(result.classification, "blocked_isolation")
                self.assertEqual(result.result, "blocked")
                self.assertEqual(result.reason, "materialized_harness_mismatch")
                self.assertIsNone(result.manifest)
                self.assertSanitized(result)

    def test_source_identity_fields_cannot_be_forged_for_a_verified_home(self):
        home, manifest = self._materialize()
        replacements = (
            dataclasses.replace(manifest, bundle_id="forged-v1"),
            dataclasses.replace(manifest, profile="lean"),
            dataclasses.replace(
                manifest,
                bundle_digest="sha256:" + ("0" * 64),
            ),
            dataclasses.replace(
                manifest,
                adapter_source_hash="sha256:" + ("0" * 64),
            ),
        )

        for forged in replacements:
            with self.subTest(forged=forged):
                result = verify_loaded_harness(
                    self.repo,
                    self.bundle,
                    home,
                    forged,
                )

                self.assertEqual(result.classification, "blocked_isolation")
                self.assertEqual(result.result, "blocked")
                self.assertEqual(result.reason, "source_changed")
                self.assertNotEqual(result.reason, "fixed_inventory_verified")
                self.assertIsNone(result.manifest)
                self.assertSanitized(result)

    def test_checkout_and_home_seal_mismatches_have_distinct_fixed_reasons(self):
        checkout_home, checkout_manifest = self._materialize("current", "checkout-home")
        skill = checkout_home / "skills/workflow/SKILL.md"
        skill.chmod(0o600)
        skill.write_text("tampered\n", encoding="utf-8")
        skill.chmod(0o444)
        checkout_result = verify_loaded_harness(
            self.repo,
            self.bundle,
            checkout_home,
            checkout_manifest,
        )
        self.assertEqual(checkout_result.reason, "skill_checkout_mismatch")

        seal_home, seal_manifest = self._materialize("current", "seal-home")
        wrong_seal = dataclasses.replace(
            seal_manifest, home_digest="sha256:" + ("0" * 64)
        )
        seal_result = verify_loaded_harness(
            self.repo,
            self.bundle,
            seal_home,
            wrong_seal,
        )
        self.assertEqual(seal_result.reason, "home_seal_mismatch")

    def test_verified_manifest_is_returned_without_path_or_content_leakage(self):
        home, manifest = self._materialize()

        result = verify_loaded_harness(
            self.repo, self.bundle, home, manifest
        )

        self.assertEqual(result.classification, "ready")
        self.assertEqual(result.result, "pass")
        self.assertEqual(result.reason, "fixed_inventory_verified")
        self.assertEqual(result.manifest, manifest)
        self.assertSanitized(result)


if __name__ == "__main__":
    unittest.main()
