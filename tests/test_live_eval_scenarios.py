import copy
import json
import re
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


def valid_entry():
    return {
        "scenario_id": "VALID",
        "schema_version": 1,
        "tags": ["workflow"],
        "prompt": "prompt",
        "required_paths": ["status"],
        "required_values": {"status": "pass"},
        "forbidden_values": [],
        "expected_status": "pass",
        "timeout_seconds": 30,
    }


class ScenarioTests(unittest.TestCase):
    def load_payload(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_scenarios(path)

    def test_loads_versioned_corpus_with_all_documented_scenarios(self):
        scenarios = load_scenarios(FIXTURE)

        self.assertEqual(len(scenarios), 26)
        self.assertEqual(tuple(scenarios), tuple(sorted(scenarios)))
        self.assertTrue(all(item.schema_version == 1 for item in scenarios.values()))
        self.assertTrue(all(item.required_values for item in scenarios.values()))

    def test_documentation_and_corpus_have_the_same_unique_ids(self):
        scenarios = load_scenarios(FIXTURE)
        documentation = DOCUMENTATION.read_text(encoding="utf-8")
        documented_ids = re.findall(r"^\d+\. \[([A-Z0-9-]+)\]", documentation, re.M)

        self.assertEqual(len(documented_ids), len(set(documented_ids)))
        self.assertEqual(set(documented_ids), set(scenarios))

    def test_selects_three_tagged_scenarios_by_default_in_stable_order(self):
        scenarios = load_scenarios(FIXTURE)

        selected = select_scenarios(scenarios, {"workflow-intake"})

        self.assertEqual(len(selected), 3)
        self.assertEqual(
            tuple(item.scenario_id for item in selected),
            tuple(sorted(item.scenario_id for item in selected)),
        )
        self.assertTrue(all("workflow-intake" in item.tags for item in selected))

    def test_selection_empty_tags_means_all_and_multiple_tags_use_or(self):
        scenarios = load_scenarios(FIXTURE)

        all_selected = select_scenarios(scenarios, set(), limit=5)
        one_selected = select_scenarios(scenarios, set(), limit=1)
        mixed_selected = select_scenarios(
            scenarios, {"ai-eval", "simple-task"}, limit=20
        )

        self.assertEqual(len(all_selected), 5)
        self.assertEqual(len(one_selected), 1)
        self.assertEqual(
            tuple(item.scenario_id for item in all_selected), tuple(sorted(scenarios)[:5])
        )
        self.assertTrue(
            all({"ai-eval", "simple-task"}.intersection(item.tags) for item in mixed_selected)
        )
        self.assertGreater(len(mixed_selected), 1)
        self.assertEqual(select_scenarios(scenarios, {"unknown"}, limit=1), ())

    def test_selection_rejects_invalid_tags_and_limits(self):
        scenarios = load_scenarios(FIXTURE)

        for tags in ("workflow-intake", {"workflow-intake", 1}, {" "}):
            with self.subTest(tags=tags):
                with self.assertRaisesRegex(ValueError, "tags"):
                    select_scenarios(scenarios, tags)
        for limit in (True, 0):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "limit"):
                    select_scenarios(scenarios, set(), limit=limit)

    def test_required_values_reject_semantically_wrong_response(self):
        scenario = load_scenarios(FIXTURE)["WI-MISSING-REPO"]

        report = assert_response(
            scenario,
            {"next_step": "Which repo?", "autonomy_level": "L4"},
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.missing_paths, ())
        self.assertEqual(
            report.value_mismatches,
            (("autonomy_level", "L0", ("L4",)),),
        )
        self.assertEqual(report.expected_status, "blocked")

    def test_required_and_forbidden_assertions_are_deterministic(self):
        scenario = load_scenarios(FIXTURE)["WI-MISSING-REPO"]

        report = assert_response(
            scenario,
            {"next_step": "Which repo?", "autonomy_level": "L0"},
        )
        failure = assert_response(scenario, {"next_step": "Implemented it"})

        self.assertTrue(report.passed)
        self.assertEqual(report.missing_paths, ())
        self.assertEqual(report.value_mismatches, ())
        self.assertEqual(report.forbidden_matches, ())
        self.assertFalse(failure.passed)
        self.assertEqual(failure.missing_paths, ("autonomy_level",))
        self.assertEqual(
            failure.forbidden_matches,
            (("next_step", "Implemented it"),),
        )

    def test_list_path_assertions_are_existential(self):
        scenario = load_scenarios(FIXTURE)["WI-ARTIFACT-APPROVAL"]
        response = {
            "artifact_decision": {
                "planning_docs": [{"create_now": "no"}, {"create_now": "ask"}],
                "design_docs": [{"create_now": "ask"}],
            }
        }

        report = assert_response(scenario, response)
        failure = assert_response(
            scenario,
            {
                "artifact_decision": {
                    "planning_docs": [{"create_now": "no"}],
                    "design_docs": [{"create_now": "ask"}],
                }
            },
        )

        self.assertTrue(report.passed)
        self.assertFalse(failure.passed)
        self.assertEqual(
            failure.value_mismatches,
            (("artifact_decision.planning_docs[].create_now", "ask", ("no",)),),
        )

    def test_forbidden_values_support_exact_json_values_recursively(self):
        entry = valid_entry()
        entry["forbidden_values"] = [False, {"bad": [1]}]
        scenario = self.load_payload(
            {"schema_version": 1, "scenarios": [entry]}
        )["VALID"]

        report = assert_response(
            scenario,
            {"status": "pass", "flag": False, "nested": {"bad": [1]}},
        )

        self.assertFalse(report.passed)
        self.assertEqual(
            report.forbidden_matches,
            (("flag", False), ("nested", {"bad": [1]})),
        )

    def test_rejects_duplicate_scenario_ids(self):
        first = valid_entry()
        second = copy.deepcopy(first)
        second["prompt"] = "second"

        with self.assertRaisesRegex(ScenarioCorpusError, "duplicate scenario_id"):
            self.load_payload({"schema_version": 1, "scenarios": [first, second]})

    def test_rejects_invalid_root_and_scenario_keys(self):
        payloads = []
        root_extra = {"schema_version": 1, "scenarios": [valid_entry()], "extra": True}
        payloads.append(root_extra)
        scenario_extra = valid_entry()
        scenario_extra["extra"] = True
        payloads.append({"schema_version": 1, "scenarios": [scenario_extra]})

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ScenarioCorpusError, "invalid fields"):
                    self.load_payload(payload)

    def test_rejects_invalid_versions_status_timeout_and_tags(self):
        mutations = (
            ("root bool version", lambda payload: payload.update(schema_version=True)),
            (
                "scenario bool version",
                lambda payload: payload["scenarios"][0].update(schema_version=True),
            ),
            (
                "unknown status",
                lambda payload: payload["scenarios"][0].update(expected_status="done"),
            ),
            (
                "non-string status",
                lambda payload: payload["scenarios"][0].update(expected_status=[]),
            ),
            (
                "bool timeout",
                lambda payload: payload["scenarios"][0].update(timeout_seconds=True),
            ),
            (
                "large timeout",
                lambda payload: payload["scenarios"][0].update(timeout_seconds=301),
            ),
            (
                "zero timeout",
                lambda payload: payload["scenarios"][0].update(timeout_seconds=0),
            ),
            (
                "duplicate tags",
                lambda payload: payload["scenarios"][0].update(tags=["workflow", "workflow"]),
            ),
        )
        for name, mutate in mutations:
            payload = {"schema_version": 1, "scenarios": [valid_entry()]}
            mutate(payload)
            with self.subTest(name=name):
                with self.assertRaises(ScenarioCorpusError):
                    self.load_payload(payload)

    def test_accepts_timeout_boundaries(self):
        for timeout in (1, 300):
            entry = valid_entry()
            entry["timeout_seconds"] = timeout
            with self.subTest(timeout=timeout):
                scenarios = self.load_payload(
                    {"schema_version": 1, "scenarios": [entry]}
                )
                self.assertEqual(scenarios["VALID"].timeout_seconds, timeout)

    def test_rejects_invalid_canonical_paths(self):
        invalid_paths = (
            "",
            ".status",
            "status.",
            "a..b",
            "items[0].status",
            "items[][].status",
            "9status",
        )
        for field in ("required_paths", "required_values"):
            for invalid_path in invalid_paths:
                entry = valid_entry()
                if field == "required_paths":
                    entry[field] = [invalid_path]
                    entry["required_values"] = {}
                else:
                    entry[field] = {invalid_path: "pass"}
                    entry["required_paths"] = [invalid_path]
                with self.subTest(field=field, path=invalid_path):
                    with self.assertRaisesRegex(ScenarioCorpusError, "canonical path"):
                        self.load_payload({"schema_version": 1, "scenarios": [entry]})

    def test_rejects_non_json_compatible_values_recursively(self):
        for field in ("required_values", "forbidden_values"):
            entry = valid_entry()
            if field == "required_values":
                entry[field] = {"status": float("nan")}
            else:
                entry[field] = [[float("inf")]]
            with self.subTest(field=field):
                with self.assertRaisesRegex(ScenarioCorpusError, "JSON-compatible"):
                    self.load_payload({"schema_version": 1, "scenarios": [entry]})


if __name__ == "__main__":
    unittest.main()
