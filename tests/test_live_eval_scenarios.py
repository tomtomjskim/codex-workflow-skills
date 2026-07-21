import json
import tempfile
import unittest
from pathlib import Path

from scripts.live_eval.scenarios import (
    ScenarioCorpusError,
    assert_response,
    load_scenarios,
    select_scenarios,
)


FIXTURE = Path(__file__).with_name("live-eval-scenarios.json")
DOCUMENTATION = Path(__file__).with_name("acceptance-scenarios.md")


class ScenarioTests(unittest.TestCase):
    def test_loads_versioned_corpus_with_all_documented_scenarios(self):
        scenarios = load_scenarios(FIXTURE)

        self.assertEqual(len(scenarios), 26)
        self.assertEqual(tuple(scenarios), tuple(sorted(scenarios)))
        self.assertTrue(all(item.schema_version == 1 for item in scenarios.values()))

    def test_documentation_links_each_canonical_scenario_id_once(self):
        scenarios = load_scenarios(FIXTURE)
        documentation = DOCUMENTATION.read_text(encoding="utf-8")

        for scenario_id in scenarios:
            self.assertEqual(
                documentation.count("[{}]".format(scenario_id)),
                1,
                scenario_id,
            )

    def test_selects_three_tagged_scenarios_by_default_in_stable_order(self):
        scenarios = load_scenarios(FIXTURE)

        selected = select_scenarios(scenarios, {"workflow-intake"})

        self.assertEqual(len(selected), 3)
        self.assertEqual(
            tuple(item.scenario_id for item in selected),
            tuple(sorted(item.scenario_id for item in selected)),
        )
        self.assertTrue(all("workflow-intake" in item.tags for item in selected))

    def test_required_and_forbidden_assertions_are_deterministic(self):
        scenario = load_scenarios(FIXTURE)["WI-MISSING-REPO"]

        report = assert_response(
            scenario,
            {"next_step": "Which repo?", "autonomy_level": "L0"},
        )
        failure = assert_response(scenario, {"next_step": "Implemented it"})

        self.assertTrue(report.passed)
        self.assertEqual(report.missing_paths, ())
        self.assertEqual(report.forbidden_matches, ())
        self.assertFalse(failure.passed)
        self.assertEqual(failure.missing_paths, ("autonomy_level",))
        self.assertEqual(
            failure.forbidden_matches,
            (("next_step", "Implemented it"),),
        )

    def test_assertions_traverse_nested_mapping_and_list_paths(self):
        scenario = load_scenarios(FIXTURE)["WI-ARTIFACT-APPROVAL"]
        response = {
            "artifact_decision": {
                "planning_docs": [{"create_now": "ask"}],
                "design_docs": [{"create_now": "ask"}],
            }
        }

        report = assert_response(scenario, response)

        self.assertTrue(report.passed)

    def test_rejects_duplicate_scenario_ids(self):
        payload = {
            "schema_version": 1,
            "scenarios": [
                {
                    "scenario_id": "DUPLICATE",
                    "schema_version": 1,
                    "tags": ["workflow"],
                    "prompt": "first",
                    "required_paths": ["status"],
                    "forbidden_values": [],
                    "expected_status": "pass",
                    "timeout_seconds": 30,
                },
                {
                    "scenario_id": "DUPLICATE",
                    "schema_version": 1,
                    "tags": ["workflow"],
                    "prompt": "second",
                    "required_paths": ["status"],
                    "forbidden_values": [],
                    "expected_status": "pass",
                    "timeout_seconds": 30,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ScenarioCorpusError, "duplicate scenario_id"):
                load_scenarios(path)

    def test_rejects_non_string_forbidden_values(self):
        payload = {
            "schema_version": 1,
            "scenarios": [
                {
                    "scenario_id": "INVALID-FORBIDDEN-VALUE",
                    "schema_version": 1,
                    "tags": ["workflow"],
                    "prompt": "prompt",
                    "required_paths": ["status"],
                    "forbidden_values": [False],
                    "expected_status": "pass",
                    "timeout_seconds": 30,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ScenarioCorpusError, "forbidden_values must be a string list"
            ):
                load_scenarios(path)


if __name__ == "__main__":
    unittest.main()
