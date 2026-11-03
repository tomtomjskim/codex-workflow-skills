import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validate_plugin_manifest import validate_plugin

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "validate_plugin_manifest.py"

VALID_MANIFEST = {
    "name": "sample-plugin",
    "version": "1.2.3",
    "description": "Sample plugin.",
    "author": {"name": "TOM"},
    "license": "MIT",
    "keywords": ["sample"],
    "skills": "./skills/",
    "interface": {
        "displayName": "Sample",
        "shortDescription": "Short.",
        "longDescription": "Long.",
        "developerName": "TOM",
        "category": "Productivity",
        "capabilities": ["Review"],
        "defaultPrompt": ["Use $sample."],
    },
}


class ValidatePluginManifestTests(unittest.TestCase):
    def make_plugin(self, manifest, skills=("sample",)):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".codex-plugin").mkdir()
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        for name in skills:
            skill_dir = root / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: test\n---\n", encoding="utf-8"
            )
        return root

    def assert_error(self, manifest, fragment, skills=("sample",)):
        errors = validate_plugin(self.make_plugin(manifest, skills))
        self.assertTrue(
            any(fragment in error for error in errors),
            f"expected {fragment!r} in {errors}",
        )

    def test_repository_manifest_is_valid(self):
        self.assertEqual(validate_plugin(ROOT), [])

    def test_valid_fixture_passes(self):
        self.assertEqual(validate_plugin(self.make_plugin(VALID_MANIFEST)), [])

    def test_missing_manifest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                validate_plugin(tmp), ["manifest not found: .codex-plugin/plugin.json"]
            )

    def test_non_kebab_name_fails(self):
        manifest = dict(VALID_MANIFEST, name="Sample_Plugin")
        self.assert_error(manifest, "kebab-case")

    def test_non_semver_version_fails(self):
        manifest = dict(VALID_MANIFEST, version="1.2")
        self.assert_error(manifest, "semantic")

    def test_missing_author_name_fails(self):
        manifest = dict(VALID_MANIFEST, author={})
        self.assert_error(manifest, "author.name")

    def test_skills_path_outside_repo_fails(self):
        manifest = dict(VALID_MANIFEST, skills="./../elsewhere/")
        self.assert_error(manifest, "inside the repository")

    def test_skill_name_mismatch_fails(self):
        root = self.make_plugin(VALID_MANIFEST)
        (root / "skills" / "sample" / "SKILL.md").write_text(
            "---\nname: other\ndescription: test\n---\n", encoding="utf-8"
        )
        self.assertTrue(any("must match" in e for e in validate_plugin(root)))

    def test_too_many_default_prompts_fails(self):
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["interface"]["defaultPrompt"] = ["a", "b", "c", "d"]
        self.assert_error(manifest, "at most 3")

    def test_long_default_prompt_fails(self):
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["interface"]["defaultPrompt"] = ["x" * 129]
        self.assert_error(manifest, "exceeds 128")

    def test_cli_exit_codes(self):
        ok = subprocess.run(
            [sys.executable, str(SCRIPT), str(ROOT)], capture_output=True, text=True
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        bad_root = self.make_plugin(dict(VALID_MANIFEST, version="x"))
        bad = subprocess.run(
            [sys.executable, str(SCRIPT), str(bad_root)], capture_output=True, text=True
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("semantic", bad.stderr)


if __name__ == "__main__":
    unittest.main()
