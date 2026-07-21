import tempfile
import unittest
from pathlib import Path

from scripts.workflow_coordination.receipts import ValidationReceipt
from scripts.workflow_coordination.validate import ValidationError, validate_handoff


class HandoffValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo_root = Path(self.temporary_directory.name)
        (self.repo_root / "src" / "ui").mkdir(parents=True)
        self.receipt = ValidationReceipt(
            schema_version=1,
            manifest_hash="sha256:" + "1" * 64,
            inventory_hash="sha256:" + "2" * 64,
            contract_core_hash=None,
            checkout_tree_hash="3" * 40,
            derived_route="independent",
            required_sets={"nested": [["value"]]},
            normalized_paths={"frontend": ["src/ui"]},
            run_id="test-run",
            recorded_at="2026-07-21T00:00:00Z",
            derived_profiles={"shared_interface": False},
        )

    def test_handoff_rejects_cross_workstream_write(self):
        with self.assertRaisesRegex(ValidationError, "outside owned paths"):
            validate_handoff(
                self.repo_root,
                self.receipt,
                "frontend",
                ["src/api/settings.py"],
            )

    def test_handoff_accepts_descendants_and_rejects_unsafe_or_unknown_input(self):
        validate_handoff(
            self.repo_root,
            self.receipt,
            "frontend",
            ["src/ui/settings.py", "src/ui/new/untracked.py"],
        )

        with self.assertRaisesRegex(ValidationError, "unknown workstream"):
            validate_handoff(self.repo_root, self.receipt, "missing", [])
        with self.assertRaisesRegex(ValidationError, "parent traversal"):
            validate_handoff(
                self.repo_root, self.receipt, "frontend", ["src/ui/../api.py"]
            )

    def test_receipt_is_recursively_immutable_and_to_dict_is_detached(self):
        with self.assertRaises(TypeError):
            self.receipt.normalized_paths["frontend"] = ("src/api",)
        with self.assertRaises(AttributeError):
            self.receipt.normalized_paths["frontend"].append("src/api")
        with self.assertRaises(TypeError):
            self.receipt.required_sets["nested"][0][0] = "changed"
        with self.assertRaises(TypeError):
            self.receipt.derived_profiles["shared_interface"] = True

        exported = self.receipt.to_dict()
        exported["normalized_paths"]["frontend"][0] = "src/api"
        exported["required_sets"]["nested"][0][0] = "changed"
        exported["derived_profiles"]["shared_interface"] = True

        with self.assertRaisesRegex(ValidationError, "outside owned paths"):
            validate_handoff(
                self.repo_root, self.receipt, "frontend", ["src/api/settings.py"]
            )

    def test_receipt_does_not_alias_constructor_inputs(self):
        paths = {"frontend": ["src/ui"]}
        receipt = ValidationReceipt(
            schema_version=1,
            manifest_hash="sha256:" + "1" * 64,
            inventory_hash="sha256:" + "2" * 64,
            contract_core_hash=None,
            checkout_tree_hash="3" * 40,
            derived_route="independent",
            required_sets={},
            normalized_paths=paths,
            run_id="test-run",
            recorded_at="2026-07-21T00:00:00Z",
        )
        paths["frontend"][0] = "src/api"

        with self.assertRaisesRegex(ValidationError, "outside owned paths"):
            validate_handoff(
                self.repo_root, receipt, "frontend", ["src/api/settings.py"]
            )

    def test_handoff_uses_canonical_internal_symlink_identity(self):
        (self.repo_root / "src" / "link").symlink_to(
            self.repo_root / "src" / "ui", target_is_directory=True
        )

        validate_handoff(
            self.repo_root,
            self.receipt,
            "frontend",
            ["src/link/future/settings.py"],
        )


if __name__ == "__main__":
    unittest.main()
