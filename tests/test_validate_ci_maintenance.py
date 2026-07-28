import tempfile
import unittest
from pathlib import Path

from scripts import validate_ci_maintenance


VALID_DEPENDABOT = """version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""


class ValidateCiMaintenanceTests(unittest.TestCase):
    def _write_repository(
        self,
        root,
        workflow="steps:\n  - uses: actions/checkout@v7\n",
        dependabot=VALID_DEPENDABOT,
    ):
        workflows = root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "validate.yml").write_text(
            workflow,
            encoding="utf-8",
        )
        if dependabot is not None:
            (root / ".github" / "dependabot.yml").write_text(
                dependabot,
                encoding="utf-8",
            )

    def test_valid_v7_and_future_v8_references_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(root)
            (root / ".github" / "workflows" / "release.yaml").write_text(
                "steps:\n  - uses: actions/checkout@v8\n",
                encoding="utf-8",
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_stale_checkout_in_any_workflow_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(root)
            (root / ".github" / "workflows" / "stale.yml").write_text(
                "steps:\n  - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("stale.yml:2" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_commented_checkout_is_not_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=(
                    "steps:\n"
                    "  # uses: actions/checkout@v4\n"
                    "  - uses: actions/checkout@v7 # active step\n"
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_non_major_checkout_reference_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow="steps:\n  - uses: actions/checkout@main\n",
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("must use a vN major tag" in error for error in errors),
                errors,
            )

    def test_missing_or_wrong_dependabot_contract_fails(self):
        cases = (
            (None, "missing .github/dependabot.yml"),
            (
                VALID_DEPENDABOT.replace('"weekly"', '"daily"'),
                "weekly GitHub Actions update",
            ),
            (
                VALID_DEPENDABOT.replace('directory: "/"', 'directory: "/ci"'),
                "weekly GitHub Actions update",
            ),
        )
        for dependabot, expected in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(
                        root,
                        dependabot=dependabot,
                    )

                    errors = validate_ci_maintenance.validate_repository(root)

                    self.assertTrue(
                        any(expected in error for error in errors),
                        errors,
                    )

    def test_current_repository_satisfies_policy(self):
        root = Path(__file__).parents[1]

        self.assertEqual(
            validate_ci_maintenance.validate_repository(root),
            [],
        )

    def test_repository_validator_runs_ci_maintenance_policy(self):
        root = Path(__file__).parents[1]
        validator = (root / "scripts" / "validate_repo.sh").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            validator.count(
                "require_file scripts/validate_ci_maintenance.py"
            ),
            1,
        )
        self.assertEqual(
            validator.count(
                "run python3 scripts/validate_ci_maintenance.py"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
