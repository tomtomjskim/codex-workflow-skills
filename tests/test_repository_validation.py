import os
import unittest
from pathlib import Path
from unittest import mock

from tests import test_shared_role_contracts as shared_contracts


class RepositoryValidationTests(unittest.TestCase):
    def test_validator_runs_full_repository_owned_discovery(self):
        root = Path(__file__).parents[1]
        validator = (root / "scripts" / "validate_repo.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "run python3 -m unittest discover -s tests -v",
            validator,
        )

    def test_repo_owned_reviewer_mutation_tests_are_environment_independent(self):
        test_case = shared_contracts.ReviewerContractMutationTests(
            "test_reviewer_authority_mutations_cannot_enable_direct_edits"
        )
        result = unittest.TestResult()

        with mock.patch.dict(os.environ, {}, clear=True):
            test_case.run(result)

        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])

    def test_external_shared_audits_are_not_run_without_explicit_root(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for case in (
                shared_contracts.SharedRoleContractTests,
                shared_contracts.SharedAdapterAuditTests,
            ):
                with self.subTest(case=case.__name__), self.assertRaisesRegex(
                    unittest.SkipTest,
                    "not_run: SHARED_AGENTS_ROOT",
                ):
                    case.setUpClass()


if __name__ == "__main__":
    unittest.main()
