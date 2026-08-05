import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"


class PluginManifestContractTests(unittest.TestCase):
    def test_starter_prompts_fit_the_plugin_ui_limit(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        prompts = manifest["interface"]["defaultPrompt"]

        self.assertLessEqual(len(prompts), 3)
        self.assertTrue(all(len(prompt) <= 128 for prompt in prompts))
        self.assertTrue(any("$council" in prompt for prompt in prompts))
        self.assertTrue(any("$session-wiki" in prompt for prompt in prompts))

    def test_capabilities_cover_documentation_writes(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertIn("Write", manifest["interface"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
