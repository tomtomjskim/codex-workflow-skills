import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import validate_ci_maintenance


VALID_DEPENDABOT = """version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
"""


def workflow_with_steps(*lines):
    workflow = (
        "jobs:\n"
        "  validate:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
    )
    return workflow + "".join("      " + line + "\n" for line in lines)


class ValidateCiMaintenanceTests(unittest.TestCase):
    def _write_repository(
        self,
        root,
        workflow=workflow_with_steps("- uses: actions/checkout@v7"),
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
                workflow_with_steps("- uses: actions/checkout@v8"),
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
                workflow_with_steps("- uses: actions/checkout@v4"),
                encoding="utf-8",
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("stale.yml:5" in error for error in errors),
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
                workflow=workflow_with_steps(
                    "# uses: actions/checkout@v4",
                    "- uses: actions/checkout@v7 # active step",
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_spaced_and_quoted_uses_keys_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    '- "uses" : actions/checkout@v7',
                    "- 'uses' : actions/checkout@v4",
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("validate.yml:6" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_flow_mapping_checkout_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- { uses: actions/checkout@v4, name: stale } "
                    "# active step",
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("validate.yml:5" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_block_scalar_checkout_text_is_not_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- uses: actions/checkout@v7",
                    "- run: |",
                    "    uses: actions/checkout@v4",
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_anchored_uses_value_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- uses: &checkout actions/checkout@v4",
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("validate.yml:5" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_unresolved_uses_alias_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps("- uses: *checkout"),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any(
                    "workflow YAML is invalid" in error
                    for error in errors
                ),
                errors,
            )

    def test_scalar_uses_alias_resolves_file_local_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- uses: &checkout actions/checkout@v7",
                    "- uses: *checkout",
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_non_executable_nested_uses_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=(
                    "jobs:\n"
                    "  validate:\n"
                    "    runs-on: ubuntu-latest\n"
                    "    env:\n"
                    "      uses: actions/checkout@v4\n"
                    "    steps:\n"
                    "      - uses: actions/checkout@v7\n"
                    "        with:\n"
                    "          uses: actions/checkout@v4\n"
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_anchored_job_container_preserves_uses_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=(
                    "jobs:\n"
                    "  base: &base_job\n"
                    "    runs-on: ubuntu-latest\n"
                    "    env:\n"
                    "      uses: actions/checkout@v4\n"
                    "    steps:\n"
                    "      - uses: actions/checkout@v7\n"
                    "  copied: *base_job\n"
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_cross_context_mapping_alias_is_scanned_at_use_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=(
                    "jobs:\n"
                    "  validate:\n"
                    "    runs-on: ubuntu-latest\n"
                    "    env:\n"
                    "      checkout-step: &checkout_step\n"
                    "        uses: actions/checkout@v4\n"
                    "    steps:\n"
                    "      - *checkout_step\n"
                    "      - uses: actions/checkout@v7\n"
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_flow_mapping_run_text_is_not_an_action_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    '- { run: "echo { uses: actions/checkout@v4 }" }',
                    "- uses: actions/checkout@v7",
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_multiline_quoted_run_text_is_not_an_action_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    '- run: "echo start',
                    '    uses: actions/checkout@v4"',
                    "- uses: actions/checkout@v7",
                ),
            )

            self.assertEqual(
                validate_ci_maintenance.validate_repository(root),
                [],
            )

    def test_flow_style_job_or_steps_containers_are_scanned(self):
        cases = (
            (
                "jobs:\n"
                "  validate:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps: [{ uses: actions/checkout@v4 }]\n",
                True,
            ),
            (
                "jobs: { validate: { runs-on: ubuntu-latest, "
                "steps: [{ uses: actions/checkout@v4 }] } }\n",
                True,
            ),
            (
                "jobs:\n"
                "  validate:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - {\n"
                "          uses: actions/checkout@v7,\n"
                "          name: checkout\n"
                "        }\n",
                False,
            ),
            (
                "jobs:\n"
                "  validate:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps: !!seq "
                "[{ uses: actions/checkout@v4 }]\n",
                True,
            ),
            (
                "jobs:\n"
                "  validate:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - !!map { uses: actions/checkout@v4 }\n",
                True,
            ),
        )
        for workflow, stale in cases:
            with self.subTest(workflow=workflow):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(root, workflow=workflow)

                    errors = validate_ci_maintenance.validate_repository(root)

                    if stale:
                        self.assertTrue(
                            any(
                                "minimum is v7" in error
                                for error in errors
                            ),
                            errors,
                        )
                    else:
                        self.assertEqual(errors, [])

    def test_anchored_flow_mapping_checkout_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- &checkout { uses: actions/checkout@v4 }",
                    "- uses: actions/checkout@v7",
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("validate.yml:5" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_checkout_repository_identity_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- uses: actions/checkout@v7",
                    "- uses: Actions/Checkout@v4",
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_anchored_block_mapping_alias_is_scanned(self):
        cases = (
            (
                "actions/checkout@v7",
                [],
            ),
            (
                "actions/checkout@v4",
                ["minimum is v7"],
            ),
        )
        for reference, expected_errors in cases:
            with self.subTest(reference=reference):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(
                        root,
                        workflow=workflow_with_steps(
                            "- &checkout",
                            "  uses: " + reference,
                            "- *checkout",
                        ),
                    )

                    errors = validate_ci_maintenance.validate_repository(root)

                    for expected in expected_errors:
                        self.assertTrue(
                            any(expected in error for error in errors),
                            errors,
                        )
                    if not expected_errors:
                        self.assertEqual(errors, [])

    def test_explicit_uses_key_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- ? uses",
                    "  : actions/checkout@v4",
                    "- uses: actions/checkout@v7",
                ),
            )

            errors = validate_ci_maintenance.validate_repository(root)

            self.assertTrue(
                any("validate.yml:6" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("minimum is v7" in error for error in errors),
                errors,
            )

    def test_non_major_checkout_reference_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_repository(
                root,
                workflow=workflow_with_steps(
                    "- uses: actions/checkout@main"
                ),
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

    def test_duplicate_or_misnested_dependabot_keys_fail_closed(self):
        cases = (
            VALID_DEPENDABOT.replace(
                "version: 2",
                "version: 1\nversion: 2",
            ),
            VALID_DEPENDABOT.replace(
                "version: 2",
                "version: 2\nversion: 1",
            ),
            VALID_DEPENDABOT.replace(
                "version: 2",
                'version: 2\n"version": 1',
            ),
            VALID_DEPENDABOT.replace(
                "version: 2",
                '"vers\\u0069on": 1\nversion: 2',
            ),
            VALID_DEPENDABOT.replace(
                "version: 2",
                'version: 2\n"vers\\u0069on": 1',
            ),
            VALID_DEPENDABOT.replace(
                '    directory: "/"',
                '    directory: "/ci"\n    directory: "/"',
            ),
            VALID_DEPENDABOT.replace(
                '    directory: "/"',
                '    directory: "/"\n    directory: "/ci"',
            ),
            VALID_DEPENDABOT.replace(
                '    directory: "/"',
                '    directory: "/"\n    "directory": "/ci"',
            ),
            VALID_DEPENDABOT.replace(
                '    directory: "/"',
                '    "direct\\u006fry": "/ci"\n    directory: "/"',
            ),
            VALID_DEPENDABOT.replace(
                '    directory: "/"',
                '    directory: "/"\n    "direct\\u006fry": "/ci"',
            ),
            VALID_DEPENDABOT.replace(
                '      interval: "weekly"',
                '      interval: "daily"\n      interval: "weekly"',
            ),
            VALID_DEPENDABOT.replace(
                '      interval: "weekly"',
                '      interval: "weekly"\n      interval: "daily"',
            ),
            VALID_DEPENDABOT.replace(
                '      interval: "weekly"',
                '      interval: "weekly"\n      "interval": "daily"',
            ),
            VALID_DEPENDABOT.replace(
                '      interval: "weekly"',
                '      "interv\\u0061l": "daily"\n'
                '      interval: "weekly"',
            ),
            VALID_DEPENDABOT.replace(
                '      interval: "weekly"',
                '      interval: "weekly"\n'
                '      "interv\\u0061l": "daily"',
            ),
            VALID_DEPENDABOT.replace(
                '    directory: "/"',
                '    metadata:\n      directory: "/"',
            ),
            VALID_DEPENDABOT.replace(
                '      interval: "weekly"',
                '      metadata:\n        interval: "weekly"',
            ),
        )
        for dependabot in cases:
            with self.subTest(dependabot=dependabot):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(root, dependabot=dependabot)

                    errors = validate_ci_maintenance.validate_repository(root)

                    self.assertTrue(
                        any(
                            "weekly GitHub Actions update" in error
                            for error in errors
                        ),
                        errors,
                    )

    def test_wrong_pip_dependabot_contract_fails(self):
        pip_update = (
            '  - package-ecosystem: "pip"\n'
            '    directory: "/"\n'
            "    schedule:\n"
            '      interval: "weekly"\n'
        )
        cases = (
            pip_update.replace('"weekly"', '"daily"'),
            pip_update.replace('directory: "/"', 'directory: "/ci"'),
            (
                '  - package-ecosystem: "pip"\n'
                '    directory: "/"\n'
            ),
            pip_update.replace(
                '      interval: "weekly"',
                '      interval: "daily"\n'
                '      "interv\\u0061l": "weekly"',
            ),
            pip_update + pip_update,
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(
                        root,
                        dependabot=VALID_DEPENDABOT.replace(
                            pip_update,
                            replacement,
                        ),
                    )

                    errors = validate_ci_maintenance.validate_repository(root)

                    self.assertTrue(
                        any("weekly pip update" in error for error in errors),
                        errors,
                    )

    def test_duplicate_required_dependabot_updates_fail(self):
        duplicates = (
            (
                '  - package-ecosystem: "pip"\n'
                '    directory: "/"\n'
                "    schedule:\n"
                '      interval: "daily"\n'
            ),
            (
                '  - package-ecosystem: "pip"\n'
                '    directory: "/"\n'
                "    schedule:\n"
                '      interval: "monthly"\n'
            ),
            (
                '  - package-ecosystem: "github-actions"\n'
                '    directory: "/"\n'
                "    schedule:\n"
                '      interval: "daily"\n'
            ),
            (
                '  - package-ecosystem: "github-actions"\n'
                '    directory: "/"\n'
                "    schedule:\n"
                '      interval: "monthly"\n'
            ),
        )
        for duplicate in duplicates:
            with self.subTest(duplicate=duplicate):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(
                        root,
                        dependabot=VALID_DEPENDABOT + duplicate,
                    )

                    errors = validate_ci_maintenance.validate_repository(root)

                    self.assertTrue(errors)

    def test_unsupported_yaml_tags_fail_closed(self):
        cases = (
            (
                workflow_with_steps(
                    "- uses: actions/checkout@v7",
                )
                + "extra: !unsupported value\n",
                VALID_DEPENDABOT,
            ),
            (
                workflow_with_steps(
                    "- uses: actions/checkout@v7",
                ),
                VALID_DEPENDABOT + "extra: !unsupported value\n",
            ),
            (
                workflow_with_steps(
                    "- uses: actions/checkout@v7",
                ),
                VALID_DEPENDABOT + "!unsupported extra: value\n",
            ),
        )
        for workflow, dependabot in cases:
            with self.subTest(dependabot=dependabot):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(
                        root,
                        workflow=workflow,
                        dependabot=dependabot,
                    )

                    errors = validate_ci_maintenance.validate_repository(root)

                    self.assertTrue(errors)

    def test_malformed_dependabot_yaml_fails_closed(self):
        cases = (
            'broken: "unterminated',
            "not: [closed",
            ":",
            "broken",
            "\tbad-tab: value",
        )
        for malformed in cases:
            with self.subTest(malformed=malformed):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_repository(
                        root,
                        dependabot=VALID_DEPENDABOT + malformed + "\n",
                    )

                    errors = validate_ci_maintenance.validate_repository(root)

                    self.assertTrue(
                        any(
                            "weekly GitHub Actions update" in error
                            for error in errors
                        ),
                        errors,
                    )

    def test_ci_dependency_is_pinned_and_installed(self):
        root = Path(__file__).parents[1]
        requirements = (root / "requirements-ci.txt").read_text(
            encoding="utf-8"
        )
        workflow = (
            root / ".github" / "workflows" / "validate.yml"
        ).read_text(encoding="utf-8")
        dependabot = (root / ".github" / "dependabot.yml").read_text(
            encoding="utf-8"
        )
        validator = (root / "scripts" / "validate_repo.sh").read_text(
            encoding="utf-8"
        )
        steps = yaml.safe_load(workflow)["jobs"]["validate"]["steps"]
        run_commands = [
            step["run"]
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        ]

        self.assertEqual(requirements, "PyYAML==6.0.3\ntomli==2.4.1\n")
        self.assertEqual(
            workflow.count(
                "python3 -m pip install --disable-pip-version-check "
                "-r requirements-ci.txt"
            ),
            1,
        )
        self.assertEqual(
            dependabot.count('package-ecosystem: "pip"'),
            1,
        )
        self.assertLess(
            run_commands.index(
                "python3 -m pip install --disable-pip-version-check "
                "-r requirements-ci.txt"
            ),
            run_commands.index("./scripts/validate_repo.sh"),
        )
        self.assertEqual(
            validator.count("require_file requirements-ci.txt"),
            1,
        )
        self.assertEqual(validator.count("python3 -c 'import tomli, yaml'"), 1)

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
