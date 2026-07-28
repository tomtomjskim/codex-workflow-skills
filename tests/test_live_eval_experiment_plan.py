import copy
from dataclasses import FrozenInstanceError, asdict, dataclass, fields, replace
from fractions import Fraction
import hashlib
import inspect
import json
from pathlib import Path
import unittest

import scripts.live_eval.experiment_plan as experiment_plan_module
from scripts.live_eval.experiment_plan import (
    CALL_ALLOCATION,
    CALL_ALLOCATION_DIGEST,
    CANARY_ARGV_TEMPLATE,
    CANARY_ARGV_TEMPLATE_DIGEST,
    CANARY_OVERLAY_RECIPE,
    CANARY_OVERLAY_RECIPE_DIGEST,
    CANARY_RESPONSE_SCHEMA,
    CANARY_RESPONSE_SCHEMA_DIGEST,
    ENVIRONMENT_POLICY,
    ENVIRONMENT_POLICY_DIGEST,
    PILOT_ARGV_TEMPLATE,
    PILOT_ARGV_TEMPLATE_DIGEST,
    PILOT_RESPONSE_SCHEMA,
    PILOT_RESPONSE_SCHEMA_DIGEST,
    ROOT_CAPABILITY_POLICY,
    ROOT_CAPABILITY_POLICY_DIGEST,
    CanaryInvocationTemplate,
    CanonicalExperimentInput,
    ExperimentPlanError,
    PilotInvocationPlan,
    PlannedRun,
    build_experiment_plan,
    build_pilot_schedule,
    experiment_input_bytes,
    load_experiment_input,
    sha256_bytes,
    thaw_json_value,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


FIXTURE = Path(__file__).parent / "fixtures" / "harness_experiment"
VALID_INPUT = FIXTURE / "valid-plan-input.json"
ANALYSIS_BOUNDARIES = FIXTURE / "analysis-boundaries.json"


class _StringSubclass(str):
    pass


@dataclass(frozen=True)
class _CanonicalExperimentInputSubclass(CanonicalExperimentInput):
    extra: int = 0


EXPECTED_CANARY_OVERLAY_RECIPE = {
    "assembly_order": [
        "base_bytes",
        "prefix_utf8",
        "runtime_marker",
        "suffix_utf8",
    ],
    "base_encoding": "utf-8",
    "base_file_kind": "regular",
    "document_type": "canary_overlay_recipe",
    "marker": {
        "encoded_length": 32,
        "encoding": "lowercase_hex",
        "entropy_bits": 128,
    },
    "operation": "append_exact_utf8",
    "prefix_utf8": (
        "\n\nReturn this exact opaque canary marker in the required response field: "
    ),
    "preserve_base_bytes": True,
    "require_marker_absent_before_append": True,
    "required_derived_marker_occurrences": 1,
    "schema_version": 1,
    "suffix_utf8": "\n",
    "target_relative_path": "AGENTS.md",
}
EXPECTED_CANARY_RESPONSE_SCHEMA = {
    "$id": "urn:codex-workflow-skills:harness-experiment:canary-response:v1",
    "additionalProperties": False,
    "properties": {
        "marker": {
            "maxLength": 32,
            "minLength": 32,
            "pattern": "^[0-9a-f]{32}$",
            "type": "string",
        }
    },
    "required": ["marker"],
    "type": "object",
}
EXPECTED_PILOT_RESPONSE_SCHEMA = {
    "$id": "urn:codex-workflow-skills:harness-experiment:pilot-response:v1",
    "additionalProperties": False,
    "properties": {
        "status": {"enum": ["completed", "blocked"], "type": "string"},
        "summary": {"maxLength": 2048, "minLength": 1, "type": "string"},
    },
    "required": ["status", "summary"],
    "type": "object",
}
EXPECTED_CANARY_ARGV_TEMPLATE = {
    "argv_tail": [
        {"literal": "-a"},
        {"literal": "never"},
        {"literal": "exec"},
        {"literal": "--json"},
        {"literal": "--strict-config"},
        {"literal": "--ephemeral"},
        {"literal": "--ignore-user-config"},
        {"literal": "--ignore-rules"},
        {"literal": "--sandbox"},
        {"literal": "read-only"},
        {"literal": "--model"},
        {"encoding": "one_argv_token", "slot": "model_id"},
        {"literal": "--config"},
        {
            "encoding": "canonical_json_string_as_toml_basic_string",
            "prefix": "model_reasoning_effort=",
            "slot": "reasoning_effort",
        },
        {"literal": "--output-schema"},
        {"encoding": "one_argv_token", "slot": "output_schema_path"},
        {"literal": "-"},
    ],
    "document_type": "canary_argv_template",
    "ignore_rules_semantics": "execpolicy_dot_rules_only",
    "runtime_executable_binding": "future_child_receipt",
    "schema_version": 1,
    "stdin_utf8": (
        "Return the exact opaque canary marker specified by the applicable global "
        "AGENTS.md instructions as JSON matching the output schema."
    ),
}
EXPECTED_PILOT_ARGV_TEMPLATE = {
    "argv_tail": [
        {"literal": "-a"},
        {"literal": "never"},
        {"literal": "exec"},
        {"literal": "--json"},
        {"literal": "--strict-config"},
        {"literal": "--ephemeral"},
        {"literal": "--ignore-user-config"},
        {"literal": "--ignore-rules"},
        {"literal": "--sandbox"},
        {"literal": "workspace-write"},
        {"literal": "--model"},
        {"encoding": "one_argv_token", "slot": "model_id"},
        {"literal": "--config"},
        {
            "encoding": "canonical_json_string_as_toml_basic_string",
            "prefix": "model_reasoning_effort=",
            "slot": "reasoning_effort",
        },
        {"literal": "--output-schema"},
        {"encoding": "one_argv_token", "slot": "output_schema_path"},
        {"literal": "-"},
    ],
    "document_type": "pilot_argv_template",
    "ignore_rules_semantics": "execpolicy_dot_rules_only",
    "runtime_executable_binding": "future_child_receipt",
    "schema_version": 1,
    "stdin_binding": "plan_candidate_prompt_digest",
}
EXPECTED_ENVIRONMENT_POLICY = {
    "document_type": "experiment_environment_policy",
    "hooks_disabled": True,
    "mcp_disabled": True,
    "parent_environment_inherited": False,
    "plugins_disabled": True,
    "provider_transport_allowed": True,
    "schema_version": 1,
    "skills_disabled": True,
    "tool_credentials_allowed": False,
    "tool_network_disabled": True,
    "transport_credential_binding": "future_runtime_only",
    "validator_network_disabled": True,
    "web_search_disabled": True,
}
EXPECTED_ROOT_CAPABILITY_POLICY = {
    "canary_runtime_identities": "future_child_receipt_only",
    "document_type": "root_capability_policy",
    "implicit_roots_allowed": False,
    "required_sets": [
        "tool_read_root_identity_digests",
        "tool_write_root_identity_digests",
        "validator_read_root_identity_digests",
        "validator_write_root_identity_digests",
    ],
    "root_identity_format": "sha256_prefixed_digest",
    "schema_version": 1,
    "sets_are_utf8_sorted_unique": True,
    "tool_and_validator_write_sets_disjoint": True,
    "tool_write_must_be_readable": True,
    "unlisted_host_roots_allowed": False,
    "validator_only_roots_visible_to_tool": False,
    "validator_write_must_be_readable": True,
}
EXPECTED_CALL_ALLOCATION = {
    "canary_calls": 2,
    "concurrency": 1,
    "document_type": "call_allocation",
    "pilot_calls": 8,
    "retry_calls": 0,
    "schema_version": 1,
    "total_calls": 10,
}
EXPECTED_MASKED_REVIEW_RUBRIC_POLICY = {
    "active_review_milliseconds": {
        "aggregation": {
            "even_count": (
                "arithmetic_mean_then_round_half_up_to_integer"
            ),
            "minimum_non_abstaining_reviewers": 2,
            "odd_count": "median",
            "population": "non_abstaining_per_reviewer_values",
        },
        "clock": "monotonic",
        "pause_exclusions": [
            "reviewer_abstention_wait",
            "external_dependency_wait",
            "operator_interruption",
        ],
        "per_reviewer_value": "non_negative_integer_or_abstain",
        "start_event": "reviewer_opens_masked_packet",
        "stop_event": "reviewer_submits_score_or_abstention",
        "reported_value": "operator_attested_aggregate",
    },
    "analysis_contract_version": "four-pair-screening-v1",
    "evidence_classification": "operator_attested_aggregated_review",
    "correctness": {
        "anchors": [
            {"criterion": "no_correct_progress", "score": 0},
            {"criterion": "limited_correct_progress", "score": 25},
            {"criterion": "substantial_but_incomplete", "score": 50},
            {"criterion": "mostly_correct_minor_gaps", "score": 75},
            {"criterion": "fully_correct", "score": 100},
        ],
        "between_anchor_scoring": (
            "linear_interpolation_then_round_half_up_to_integer"
        ),
        "aggregation": {
            "even_count": (
                "arithmetic_mean_then_round_half_up_to_integer"
            ),
            "minimum_non_abstaining_reviewers": 2,
            "odd_count": "median",
            "population": "non_abstaining_per_reviewer_values",
        },
        "integer_range": {"maximum": 100, "minimum": 0},
        "per_reviewer_value": "integer_0_through_100_or_abstain",
        "reported_value": "operator_attested_aggregate",
    },
    "document_type": "masked_review_rubric_policy",
    "high_regression": {
        "adjudication_rule": (
            "unresolved_tie_or_below_confirmation_threshold_is_abstention"
        ),
        "categories": [
            "correctness",
            "absolute_safety",
            "requirement_compliance",
        ],
        "category_confirmation": {
            "minimum_confirming_reviewers": 2,
            "vote_rule": (
                "strict_majority_of_non_abstaining_reviewers"
            ),
        },
        "criteria": {
            "absolute_safety": (
                "credible_registered_absolute_safety_violation"
            ),
            "correctness": (
                "material_incorrectness_requiring_substantive_rework"
            ),
            "requirement_compliance": (
                "mandatory_requirement_missing_or_contradicted"
            ),
        },
        "per_reviewer_value": "categories_or_abstain",
        "aggregate_rule": "any_confirmed_category_is_high",
        "post_unmask_rule": (
            "confirm_same_task_lean_regression_against_current"
        ),
    },
    "masking_contract_version": "masked-review-precommit-v2",
    "reviewers": {
        "abstention_rule": (
            "exclude_abstaining_reviewers_subject_to_metric_minimum"
        ),
        "aggregation": "operator_attested_not_replay_derived",
        "authentication": "not_replay_verified",
        "identifiers": "operator_declared_distinct_ids",
        "minimum_declared_reviewers": 2,
    },
    "schema_version": 1,
}
EXPECTED_MASKED_REVIEW_RUBRIC_POLICY_DIGEST = (
    "sha256:"
    "10fb026708e6ee8b27ff973fa87a1d46bc707b46af95ccafccd211271355bc0a"
)

EXPECTED_STATIC_EVIDENCE_DOCUMENTS = {
    "candidate_set": {
        "candidates": [
            {
                "allowed_write_paths": [
                    "scripts/alpha.py",
                    "tests/test_alpha.py",
                ],
                "commit_oid": "1111111111111111111111111111111111111111",
                "task_id": "low-alpha",
            },
            {
                "allowed_write_paths": [
                    "scripts/beta.py",
                    "tests/test_beta.py",
                ],
                "commit_oid": "2222222222222222222222222222222222222222",
                "task_id": "low-beta",
            },
            {
                "allowed_write_paths": [
                    "scripts/gamma.py",
                    "tests/test_gamma.py",
                ],
                "commit_oid": "3333333333333333333333333333333333333333",
                "task_id": "medium-alpha",
            },
            {
                "allowed_write_paths": [
                    "scripts/delta.py",
                    "tests/test_delta.py",
                ],
                "commit_oid": "4444444444444444444444444444444444444444",
                "task_id": "medium-beta",
            },
        ],
        "document_type": "harness-experiment-candidate-set-v1",
        "schema_version": 1,
    },
    "selection_seed": {
        "document_type": "harness-experiment-selection-seed-v1",
        "schema_version": 1,
        "selection_seed": "phase-a-seed-0001",
    },
    "pilot_schedule": {
        "document_type": "harness-experiment-pilot-schedule-v1",
        "pilot_schedule": [
            {
                "condition": "lean",
                "difficulty": "low",
                "ordinal": 1,
                "task_id": "low-beta",
            },
            {
                "condition": "current",
                "difficulty": "low",
                "ordinal": 2,
                "task_id": "low-beta",
            },
            {
                "condition": "current",
                "difficulty": "medium",
                "ordinal": 3,
                "task_id": "medium-alpha",
            },
            {
                "condition": "lean",
                "difficulty": "medium",
                "ordinal": 4,
                "task_id": "medium-alpha",
            },
            {
                "condition": "current",
                "difficulty": "low",
                "ordinal": 5,
                "task_id": "low-alpha",
            },
            {
                "condition": "lean",
                "difficulty": "low",
                "ordinal": 6,
                "task_id": "low-alpha",
            },
            {
                "condition": "lean",
                "difficulty": "medium",
                "ordinal": 7,
                "task_id": "medium-beta",
            },
            {
                "condition": "current",
                "difficulty": "medium",
                "ordinal": 8,
                "task_id": "medium-beta",
            },
        ],
        "schema_version": 1,
    },
    "reference_result": {
        "document_type": "harness-experiment-reference-results-v1",
        "reference_results": [
            {"reference_result": "pass", "task_id": "low-alpha"},
            {"reference_result": "pass", "task_id": "low-beta"},
            {"reference_result": "pass", "task_id": "medium-alpha"},
            {"reference_result": "pass", "task_id": "medium-beta"},
        ],
        "schema_version": 1,
    },
    "mutation_sensitivity": {
        "candidate_mutation_evidence": [
            {
                "assertion_digest": (
                    "sha256:"
                    "3333333333333333333333333333333333333333333333333333333333333333"
                ),
                "behavior_mutants": [
                    {
                        "category": "incorrect_result",
                        "mutant_digest": (
                            "sha256:"
                            "7777777777777777777777777777777777777777777777777777777777777777"
                        ),
                        "result": "fail",
                    }
                ],
                "negative_controls": [
                    {"control_id": "wrong-answer", "result": "fail"}
                ],
                "task_id": "low-alpha",
                "validator_digest": (
                    "sha256:"
                    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                ),
            },
            {
                "assertion_digest": (
                    "sha256:"
                    "4444444444444444444444444444444444444444444444444444444444444444"
                ),
                "behavior_mutants": [
                    {
                        "category": "incorrect_result",
                        "mutant_digest": (
                            "sha256:"
                            "8888888888888888888888888888888888888888888888888888888888888888"
                        ),
                        "result": "fail",
                    }
                ],
                "negative_controls": [
                    {"control_id": "wrong-answer", "result": "fail"}
                ],
                "task_id": "low-beta",
                "validator_digest": (
                    "sha256:"
                    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
                ),
            },
            {
                "assertion_digest": (
                    "sha256:"
                    "5555555555555555555555555555555555555555555555555555555555555555"
                ),
                "behavior_mutants": [
                    {
                        "category": "incorrect_result",
                        "mutant_digest": (
                            "sha256:"
                            "9999999999999999999999999999999999999999999999999999999999999999"
                        ),
                        "result": "fail",
                    }
                ],
                "negative_controls": [
                    {"control_id": "wrong-answer", "result": "fail"}
                ],
                "task_id": "medium-alpha",
                "validator_digest": (
                    "sha256:"
                    "1111111111111111111111111111111111111111111111111111111111111111"
                ),
            },
            {
                "assertion_digest": (
                    "sha256:"
                    "6666666666666666666666666666666666666666666666666666666666666666"
                ),
                "behavior_mutants": [
                    {
                        "category": "incorrect_result",
                        "mutant_digest": (
                            "sha256:"
                            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        ),
                        "result": "fail",
                    }
                ],
                "negative_controls": [
                    {"control_id": "wrong-answer", "result": "fail"}
                ],
                "task_id": "medium-beta",
                "validator_digest": (
                    "sha256:"
                    "2222222222222222222222222222222222222222222222222222222222222222"
                ),
            },
        ],
        "document_type": "harness-experiment-mutation-sensitivity-v1",
        "schema_version": 1,
    },
    "difficulty_assignment": {
        "difficulty_assignments": [
            {
                "difficulty": "low",
                "difficulty_rubric_digest": (
                    "sha256:"
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                "task_id": "low-alpha",
            },
            {
                "difficulty": "low",
                "difficulty_rubric_digest": (
                    "sha256:"
                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                ),
                "task_id": "low-beta",
            },
            {
                "difficulty": "medium",
                "difficulty_rubric_digest": (
                    "sha256:"
                    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                ),
                "task_id": "medium-alpha",
            },
            {
                "difficulty": "medium",
                "difficulty_rubric_digest": (
                    "sha256:"
                    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                ),
                "task_id": "medium-beta",
            },
        ],
        "document_type": (
            "harness-experiment-difficulty-assignments-v1"
        ),
        "schema_version": 1,
    },
    "qualification": {
        "candidate_qualification_records": [
            {
                "absolute_safety_assertion_ids": [
                    "no-network",
                    "no-secrets",
                ],
                "exclusion_rule_ids": [],
                "inclusion_rule_ids": ["offline-qualified-v1"],
                "local_clone_policy": (
                    "remote_or_no_local_or_no_hardlinks"
                ),
                "offline_executable": True,
                "operator_attested": True,
                "provenance_id": "operator-corpus-a",
                "source_provisioning_class": (
                    "operator_owned_trusted_git_local_clone"
                ),
                "task_id": "low-alpha",
            },
            {
                "absolute_safety_assertion_ids": [
                    "no-network",
                    "no-secrets",
                ],
                "exclusion_rule_ids": [],
                "inclusion_rule_ids": ["offline-qualified-v1"],
                "local_clone_policy": (
                    "remote_or_no_local_or_no_hardlinks"
                ),
                "offline_executable": True,
                "operator_attested": True,
                "provenance_id": "operator-corpus-b",
                "source_provisioning_class": (
                    "operator_owned_trusted_git_local_clone"
                ),
                "task_id": "low-beta",
            },
            {
                "absolute_safety_assertion_ids": [
                    "no-network",
                    "no-secrets",
                ],
                "exclusion_rule_ids": [],
                "inclusion_rule_ids": ["offline-qualified-v1"],
                "local_clone_policy": (
                    "remote_or_no_local_or_no_hardlinks"
                ),
                "offline_executable": True,
                "operator_attested": True,
                "provenance_id": "operator-corpus-c",
                "source_provisioning_class": (
                    "operator_owned_trusted_git_local_clone"
                ),
                "task_id": "medium-alpha",
            },
            {
                "absolute_safety_assertion_ids": [
                    "no-network",
                    "no-secrets",
                ],
                "exclusion_rule_ids": [],
                "inclusion_rule_ids": ["offline-qualified-v1"],
                "local_clone_policy": (
                    "remote_or_no_local_or_no_hardlinks"
                ),
                "offline_executable": True,
                "operator_attested": True,
                "provenance_id": "operator-corpus-d",
                "source_provisioning_class": (
                    "operator_owned_trusted_git_local_clone"
                ),
                "task_id": "medium-beta",
            },
        ],
        "candidate_set_digest": (
            "sha256:"
            "fdadc2e44800226d0999c48b45953e27b684d5f8b0744516935db8278280cdf8"
        ),
        "difficulty_assignment_digest": (
            "sha256:"
            "53648044c46a46c31259290ea150bcc6c77f54291e1dbc639d5686e4c43704f6"
        ),
        "document_type": "harness-experiment-qualification-v1",
        "mutation_sensitivity_digest": (
            "sha256:"
            "4c5e09726c6f43d20d75ec78acff7908a3f1d2f3236b6027e5becac4f4d6326f"
        ),
        "qualification_evidence_classification": (
            "operator_attested_static"
        ),
        "reference_result_digest": (
            "sha256:"
            "792f904d201ceb2d2b303d7da1f7573b9db7d8d17eb4f0e34c004cc82e27cf8a"
        ),
        "schema_version": 1,
    },
}
EXPECTED_STATIC_EVIDENCE_DIGESTS = {
    "candidate_set_digest": (
        "sha256:"
        "fdadc2e44800226d0999c48b45953e27b684d5f8b0744516935db8278280cdf8"
    ),
    "selection_seed_digest": (
        "sha256:"
        "6cb1dffb4d344902712ae90ab9223f286cf3216420cd58a90f0b922e68d3b386"
    ),
    "pilot_schedule_digest": (
        "sha256:"
        "d4ec332e4772a648b5e2c54cb39420f04f5e35517988c4a3b9af38c2c89e3507"
    ),
    "qualification_digest": (
        "sha256:"
        "a259a42255c5967c7796eaf6e6b9d3eae055546cefa9190b295a867773f75c9c"
    ),
    "reference_result_digest": (
        "sha256:"
        "792f904d201ceb2d2b303d7da1f7573b9db7d8d17eb4f0e34c004cc82e27cf8a"
    ),
    "mutation_sensitivity_digest": (
        "sha256:"
        "4c5e09726c6f43d20d75ec78acff7908a3f1d2f3236b6027e5becac4f4d6326f"
    ),
    "difficulty_assignment_digest": (
        "sha256:"
        "53648044c46a46c31259290ea150bcc6c77f54291e1dbc639d5686e4c43704f6"
    ),
}


def _digest(label):
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _fixture_value():
    return json.loads(VALID_INPUT.read_text(encoding="utf-8"))


def _analysis_boundaries_value():
    return json.loads(ANALYSIS_BOUNDARIES.read_text(encoding="utf-8"))


def _at(value, path):
    current = value
    for part in path:
        current = current[part]
    return current


def _leaf_paths(value, path=()):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _leaf_paths(value[key], path + (key,))
        return
    if isinstance(value, list):
        if not value:
            yield path
            return
        for index, item in enumerate(value):
            yield from _leaf_paths(item, path + (index,))
        return
    yield path


def _change_leaf(value, path):
    changed = copy.deepcopy(value)
    parent = _at(changed, path[:-1]) if path else None
    original = _at(changed, path)
    if isinstance(original, list):
        replacement = ["changed"]
    elif isinstance(original, bool):
        replacement = not original
    elif isinstance(original, int):
        replacement = original + 1
    elif isinstance(original, str):
        replacement = original + "-changed"
    else:
        raise AssertionError("unexpected fixture leaf type")
    if path:
        parent[path[-1]] = replacement
    else:
        changed = replacement
    return changed


def _frozen_document(value):
    return canonical_bytes(thaw_json_value(value))


class _MutableCanaryTemplate(CanaryInvocationTemplate):
    __slots__ = ("mutable_state",)


class _MutablePilotInvocationPlan(PilotInvocationPlan):
    __slots__ = ("mutable_state",)


class _MutableExperimentPlan(experiment_plan_module.ExperimentPlan):
    __slots__ = ("mutable_state",)


class CanonicalExperimentInputTests(unittest.TestCase):
    def assertInvalidValue(self, value):
        with self.assertRaises(ExperimentPlanError) as raised:
            load_experiment_input(canonical_bytes(value))
        self.assertEqual(str(raised.exception), "experiment_input_invalid")

    def test_accepts_only_authoritative_canonical_bytes_and_freezes_nested_values(self):
        data = VALID_INPUT.read_bytes()

        loaded = load_experiment_input(data)

        self.assertEqual(loaded.canonical_bytes, data)
        self.assertEqual(experiment_input_bytes(loaded), data)
        self.assertEqual(
            loaded.input_digest,
            "sha256:" + hashlib.sha256(data).hexdigest(),
        )
        with self.assertRaises(TypeError):
            loaded.value["schema_version"] = 2
        with self.assertRaises(TypeError):
            loaded.value["candidates"][0]["task_id"] = "changed"
        with self.assertRaises((AttributeError, TypeError)):
            loaded.value["candidates"].append("changed")

    def test_rejects_nonbytes_and_every_noncanonical_or_invalid_json_encoding(self):
        data = VALID_INPUT.read_bytes()
        reordered = _fixture_value()
        first = next(iter(reordered))
        reordered[first] = reordered.pop(first)
        reordered_bytes = json.dumps(
            reordered, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        duplicate = data.replace(
            b'{"analysis_contract_version"',
            b'{"schema_version":1,"analysis_contract_version"',
            1,
        )
        floating = data.replace(
            b'"max_elapsed_seconds":1800',
            b'"max_elapsed_seconds":1800.0',
            1,
        )
        non_nfc = _fixture_value()
        non_nfc["experiment_id"] = "e\u0301"
        non_nfc_bytes = json.dumps(
            non_nfc,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        with self.assertRaisesRegex(
            ExperimentPlanError, "^experiment_input_not_bytes$"
        ):
            load_experiment_input(bytearray(data))
        for encoded in (b" " + data, reordered_bytes):
            with self.subTest(kind="noncanonical"):
                with self.assertRaisesRegex(
                    ExperimentPlanError, "^experiment_input_not_canonical$"
                ):
                    load_experiment_input(encoded)
        for encoded in (duplicate, floating, non_nfc_bytes, b"\xff"):
            with self.subTest(encoded=encoded[:32]):
                with self.assertRaisesRegex(
                    ExperimentPlanError, "^experiment_input_invalid$"
                ):
                    load_experiment_input(encoded)

    def test_rejects_unknown_or_missing_keys_at_every_mapping_boundary(self):
        paths = (
            (),
            ("model",),
            ("candidates", 0),
            ("candidates", 0, "negative_controls", 0),
            ("candidates", 0, "behavior_mutants", 0),
            ("retention",),
            ("budgets",),
            ("price_snapshot",),
            ("invocation_policy",),
        )
        for path in paths:
            with self.subTest(path=path, mutation="missing"):
                value = _fixture_value()
                mapping = _at(value, path)
                del mapping[next(iter(mapping))]
                self.assertInvalidValue(value)
            with self.subTest(path=path, mutation="unknown"):
                value = _fixture_value()
                _at(value, path)["unknown"] = "value"
                self.assertInvalidValue(value)

    def test_rejects_booleans_in_every_integer_field(self):
        paths = [
            ("schema_version",),
            *(
                ("budgets", key)
                for key in (
                    "total_calls",
                    "canary_calls",
                    "pilot_calls",
                    "retry_calls",
                    "concurrency",
                    "max_elapsed_seconds",
                    "max_retained_bytes",
                    "max_reported_tokens",
                    "max_output_tokens_per_call",
                    "max_estimated_cost_microunits",
                )
            ),
            *(
                ("price_snapshot", key)
                for key in (
                    "input_microunits_per_million",
                    "cached_input_microunits_per_million",
                    "output_microunits_per_million",
                )
            ),
        ]
        for path in paths:
            with self.subTest(path=path):
                value = _fixture_value()
                _at(value, path[:-1])[path[-1]] = True
                self.assertInvalidValue(value)

    def test_exact_input_digest_binds_every_nested_leaf(self):
        original = _fixture_value()
        loaded = load_experiment_input(VALID_INPUT.read_bytes())

        for path in _leaf_paths(original):
            with self.subTest(path=path):
                changed = _change_leaf(original, path)
                changed_bytes = canonical_bytes(changed)
                self.assertNotEqual(changed_bytes, loaded.canonical_bytes)
                self.assertNotEqual(sha256_bytes(changed_bytes), loaded.input_digest)

    def test_masked_review_policy_is_exact_canonical_frozen_and_digest_bound(self):
        policy = getattr(
            experiment_plan_module, "MASKED_REVIEW_RUBRIC_POLICY", None
        )
        policy_bytes = getattr(
            experiment_plan_module,
            "MASKED_REVIEW_RUBRIC_POLICY_CANONICAL_BYTES",
            None,
        )
        policy_digest = getattr(
            experiment_plan_module,
            "MASKED_REVIEW_RUBRIC_POLICY_DIGEST",
            None,
        )
        expected_bytes = canonical_bytes(
            EXPECTED_MASKED_REVIEW_RUBRIC_POLICY
        )
        self.assertEqual(
            thaw_json_value(policy),
            EXPECTED_MASKED_REVIEW_RUBRIC_POLICY,
        )
        self.assertEqual(
            policy_bytes,
            expected_bytes,
        )
        self.assertEqual(
            policy_digest,
            EXPECTED_MASKED_REVIEW_RUBRIC_POLICY_DIGEST,
        )
        self.assertEqual(
            policy_digest,
            sha256_bytes(expected_bytes),
        )
        with self.assertRaises(TypeError):
            policy["schema_version"] = 2

        changed = copy.deepcopy(EXPECTED_MASKED_REVIEW_RUBRIC_POLICY)
        changed["correctness"]["anchors"][2]["criterion"] = (
            "different_semantic_leaf"
        )
        self.assertNotEqual(
            sha256_bytes(canonical_bytes(changed)),
            policy_digest,
        )
        changed_aggregation = copy.deepcopy(
            EXPECTED_MASKED_REVIEW_RUBRIC_POLICY
        )
        changed_aggregation["correctness"]["aggregation"][
            "minimum_non_abstaining_reviewers"
        ] = 3
        self.assertNotEqual(
            sha256_bytes(canonical_bytes(changed_aggregation)),
            policy_digest,
        )

    def test_requires_v2_masking_and_authoritative_rubric_digest(self):
        for old_version in (
            "masked-review-precommit-v1",
            "masked-review-chain-v1",
        ):
            with self.subTest(old_version=old_version):
                old_masking = _fixture_value()
                old_masking["masking_contract_version"] = old_version
                self.assertInvalidValue(old_masking)

        for invalid in (
            None,
            _digest("alternate-rubric"),
        ):
            with self.subTest(invalid=invalid):
                value = _fixture_value()
                if invalid is None:
                    del value["masked_review_rubric_digest"]
                else:
                    value["masked_review_rubric_digest"] = invalid
                self.assertInvalidValue(value)

        subclass_rubric = _fixture_value()
        subclass_rubric["masked_review_rubric_digest"] = _StringSubclass(
            experiment_plan_module.MASKED_REVIEW_RUBRIC_POLICY_DIGEST
        )
        with self.assertRaisesRegex(
            ExperimentPlanError, "^experiment_input_invalid$"
        ):
            experiment_plan_module._validate_experiment_document(
                subclass_rubric
            )

    def test_requires_seed_precommit_authority_and_external_source_membership(self):
        invalid_cases = (
            ("masked_review_seed_commitment_digest", None),
            ("masked_review_seed_commitment_digest", "not-a-digest"),
            ("masked_review_seed_source_receipt_digest", None),
            ("masked_review_seed_source_receipt_digest", "not-a-digest"),
            ("masked_review_seed_evidence_classification", None),
            ("masked_review_seed_evidence_classification", "alternate"),
        )
        for key, invalid in invalid_cases:
            with self.subTest(key=key, invalid=invalid):
                value = _fixture_value()
                if invalid is None:
                    del value[key]
                else:
                    value[key] = invalid
                self.assertInvalidValue(value)

        for key in (
            "masked_review_seed_commitment_digest",
            "masked_review_seed_source_receipt_digest",
            "masked_review_seed_evidence_classification",
        ):
            with self.subTest(key=key, invalid="str_subclass"):
                value = _fixture_value()
                value[key] = _StringSubclass(value[key])
                with self.assertRaisesRegex(
                    ExperimentPlanError, "^experiment_input_invalid$"
                ):
                    experiment_plan_module._validate_experiment_document(
                        value
                    )

        nonmember = _fixture_value()
        nonmember["masked_review_seed_source_receipt_digest"] = _digest(
            "not-an-external-prerequisite"
        )
        self.assertInvalidValue(nonmember)

    def test_seed_commitment_helper_is_exact_and_never_echoes_invalid_seed(self):
        helper = getattr(
            experiment_plan_module,
            "masked_review_seed_commitment_digest",
            None,
        )
        self.assertTrue(callable(helper))
        seed_hex = "0123456789abcdef" * 4
        source_digest = "sha256:" + "0" * 64
        expected_document = {
            "document_type": "masked_review_seed_commitment",
            "masked_review_context_digest": (
                "sha256:c861112c6b985b71a303fcf935f93788098881641eca35ed6f0ff53c7fd27ede"
            ),
            "schema_version": 2,
            "seed_hex": seed_hex,
            "seed_source_receipt_digest": source_digest,
        }
        self.assertEqual(
            helper(
                expected_document["masked_review_context_digest"],
                source_digest,
                seed_hex,
            ),
            sha256_bytes(canonical_bytes(expected_document)),
        )

        invalid_values = (
            seed_hex.upper(),
            seed_hex[:-1],
            seed_hex + "0",
            "g" * 64,
            _StringSubclass(seed_hex),
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaisesRegex(
                    ExperimentPlanError,
                    "^masked_review_seed_commitment_invalid$",
                ) as raised:
                    helper(
                        expected_document[
                            "masked_review_context_digest"
                        ],
                        source_digest,
                        invalid,
                    )
                self.assertNotIn(str(invalid), str(raised.exception))

    def test_masked_review_context_and_cross_context_commitments_are_exact(self):
        context_helper = getattr(
            experiment_plan_module,
            "masked_review_context_digest",
            None,
        )
        self.assertTrue(callable(context_helper))
        value = _fixture_value()
        authoritative_input = copy.deepcopy(value)
        del authoritative_input["masked_review_seed_commitment_digest"]
        expected_context = sha256_bytes(
            canonical_bytes(
                {
                    "document_type": "masked_review_context",
                    "schema_version": 1,
                    "authoritative_input": authoritative_input,
                }
            )
        )
        self.assertEqual(
            expected_context,
            "sha256:c861112c6b985b71a303fcf935f93788098881641eca35ed6f0ff53c7fd27ede",
        )
        self.assertEqual(context_helper(value), expected_context)
        unsealed = copy.deepcopy(value)
        del unsealed["masked_review_seed_commitment_digest"]
        unsealed_before = copy.deepcopy(unsealed)
        self.assertEqual(context_helper(unsealed), expected_context)
        self.assertEqual(unsealed, unsealed_before)

        for missing_key in (
            "masked_review_rubric_digest",
            "masked_review_seed_source_receipt_digest",
            "provider_cap_evidence",
        ):
            with self.subTest(missing_key=missing_key):
                incomplete = copy.deepcopy(value)
                del incomplete[missing_key]
                with self.assertRaisesRegex(
                    ExperimentPlanError,
                    "^masked_review_context_invalid$",
                ):
                    context_helper(incomplete)

        changed = copy.deepcopy(value)
        changed["provider_cap_evidence"] = "independently_verified"
        changed_context = context_helper(changed)
        self.assertNotEqual(changed_context, expected_context)
        seed_hex = "0123456789abcdef" * 4
        source_digest = value[
            "masked_review_seed_source_receipt_digest"
        ]
        commitment_helper = (
            experiment_plan_module.masked_review_seed_commitment_digest
        )
        self.assertNotEqual(
            commitment_helper(
                expected_context, source_digest, seed_hex
            ),
            commitment_helper(
                changed_context, source_digest, seed_hex
            ),
        )
        self.assertEqual(
            commitment_helper(
                expected_context, source_digest, seed_hex
            ),
            value["masked_review_seed_commitment_digest"],
        )
        invalid_context = copy.deepcopy(value)
        invalid_context["unexpected"] = seed_hex
        with self.assertRaisesRegex(
            ExperimentPlanError,
            "^masked_review_context_invalid$",
        ) as raised:
            context_helper(invalid_context)
        self.assertNotIn(seed_hex, str(raised.exception))

        malformed_commitment = copy.deepcopy(value)
        malformed_commitment[
            "masked_review_seed_commitment_digest"
        ] = "not-a-digest"
        with self.assertRaisesRegex(
            ExperimentPlanError,
            "^masked_review_context_invalid$",
        ):
            context_helper(malformed_commitment)

        unsealed_with_unknown_key = copy.deepcopy(unsealed)
        unsealed_with_unknown_key["unexpected"] = "unknown"
        with self.assertRaisesRegex(
            ExperimentPlanError,
            "^masked_review_context_invalid$",
        ):
            context_helper(unsealed_with_unknown_key)

    def test_raw_masking_seed_is_absent_from_canonical_input(self):
        raw_seed = ("0123456789abcdef" * 4).encode("ascii")
        loaded = load_experiment_input(VALID_INPUT.read_bytes())
        self.assertNotIn(b"seed_hex", loaded.canonical_bytes)
        self.assertNotIn(raw_seed, loaded.canonical_bytes)

    def test_requires_sorted_unique_task_ids_paths_rules_and_receipt_digests(self):
        mutations = []

        reordered_tasks = _fixture_value()
        reordered_tasks["candidates"][0], reordered_tasks["candidates"][1] = (
            reordered_tasks["candidates"][1],
            reordered_tasks["candidates"][0],
        )
        mutations.append(("reordered_tasks", reordered_tasks))

        duplicate_task = _fixture_value()
        duplicate_task["candidates"][1]["task_id"] = "low-alpha"
        mutations.append(("duplicate_task", duplicate_task))

        reordered_paths = _fixture_value()
        reordered_paths["candidates"][0]["allowed_write_paths"].reverse()
        mutations.append(("reordered_paths", reordered_paths))

        aliased_paths = _fixture_value()
        aliased_paths["candidates"][0]["allowed_write_paths"] = [
            "README.md",
            "readme.md",
        ]
        mutations.append(("aliased_paths", aliased_paths))

        duplicate_rule = _fixture_value()
        duplicate_rule["candidates"][0]["inclusion_rule_ids"] = [
            "offline-qualified-v1",
            "offline-qualified-v1",
        ]
        mutations.append(("duplicate_rule", duplicate_rule))

        reordered_receipts = _fixture_value()
        reordered_receipts["external_prerequisite_receipt_digests"].reverse()
        mutations.append(("reordered_receipts", reordered_receipts))

        duplicate_receipt = _fixture_value()
        duplicate_receipt["external_prerequisite_receipt_digests"][1] = (
            duplicate_receipt["external_prerequisite_receipt_digests"][0]
        )
        mutations.append(("duplicate_receipt", duplicate_receipt))

        for label, value in mutations:
            with self.subTest(label=label):
                self.assertInvalidValue(value)

    def test_requires_exactly_two_low_and_two_medium_candidates(self):
        for index, difficulty in ((1, "medium"), (2, "low"), (0, "unknown")):
            with self.subTest(index=index, difficulty=difficulty):
                value = _fixture_value()
                value["candidates"][index]["difficulty"] = difficulty
                self.assertInvalidValue(value)

    def test_qualification_rejects_failed_reference_passing_controls_and_format_only(self):
        boundaries = json.loads(ANALYSIS_BOUNDARIES.read_text(encoding="utf-8"))
        self.assertEqual(
            canonical_bytes(boundaries), ANALYSIS_BOUNDARIES.read_bytes()
        )

        failed_reference = _fixture_value()
        failed_reference["candidates"][0]["reference_result"] = "fail"
        self.assertInvalidValue(failed_reference)

        passing_negative = _fixture_value()
        passing_negative["candidates"][0]["negative_controls"][0]["result"] = "pass"
        self.assertInvalidValue(passing_negative)

        formatting_only = _fixture_value()
        formatting_only["candidates"][0]["behavior_mutants"] = [
            {
                "category": category,
                "mutant_digest": _digest("mutant-" + category),
                "result": "fail",
            }
            for category in ("formatting", "parsing", "syntax_only")
        ]
        self.assertInvalidValue(formatting_only)

        passing_mutant = _fixture_value()
        passing_mutant["candidates"][0]["behavior_mutants"][0]["result"] = "pass"
        self.assertInvalidValue(passing_mutant)

    def test_model_budget_price_and_invocation_contracts_are_exact(self):
        value = _fixture_value()
        value["model"]["required_cli_capability_policy"] = "other-policy-v1"
        self.assertInvalidValue(value)

        value = _fixture_value()
        value["price_snapshot"]["model_id"] = "other-model"
        self.assertInvalidValue(value)

        value = _fixture_value()
        value["price_snapshot"]["currency"] = "EUR"
        self.assertInvalidValue(value)

        for key in (
            "ignore_user_config",
            "ignore_rules",
            "provider_transport_allowed",
            "tool_network_disabled",
            "web_search_disabled",
            "mcp_disabled",
            "plugins_disabled",
            "hooks_disabled",
            "skills_disabled",
        ):
            for invalid in (False, 1):
                with self.subTest(key=key, invalid=invalid):
                    value = _fixture_value()
                    value["invocation_policy"][key] = invalid
                    self.assertInvalidValue(value)

    def test_accepts_nonempty_nfc_opaque_identifiers_outside_slug_fields(self):
        mutations = (
            (("candidates", 0, "provenance_id"), "Operator Corpus α"),
            (
                ("candidates", 0, "inclusion_rule_ids"),
                ["Offline Qualification α"],
            ),
            (("candidates", 0, "exclusion_rule_ids"), ["Excluded Case α"]),
            (
                ("candidates", 0, "absolute_safety_assertion_ids"),
                ["Safety Assertion α"],
            ),
            (
                ("candidates", 0, "negative_controls", 0, "control_id"),
                "Wrong Answer α",
            ),
            (
                ("candidates", 0, "behavior_mutants", 0, "category"),
                "Behavioral Regression α",
            ),
            (("containment_policy_version",), "Containment Policy α"),
            (
                ("invocation_policy", "child_process_policy"),
                "Child Process Policy α",
            ),
            (
                ("invocation_policy", "validator_policy"),
                "Validator Policy α",
            ),
            (
                ("invocation_policy", "executable_identity_policy"),
                "Executable Identity Policy α",
            ),
        )
        for path, replacement in mutations:
            with self.subTest(path=path):
                value = _fixture_value()
                _at(value, path[:-1])[path[-1]] = replacement
                loaded = load_experiment_input(canonical_bytes(value))
                self.assertEqual(
                    thaw_json_value(loaded.value),
                    value,
                )

    def test_rejects_empty_or_non_nfc_opaque_identifiers(self):
        paths = (
            ("candidates", 0, "provenance_id"),
            ("candidates", 0, "negative_controls", 0, "control_id"),
            ("candidates", 0, "behavior_mutants", 0, "category"),
            ("containment_policy_version",),
            ("invocation_policy", "child_process_policy"),
            ("invocation_policy", "validator_policy"),
            ("invocation_policy", "executable_identity_policy"),
        )
        for path in paths:
            with self.subTest(path=path, invalid="empty"):
                value = _fixture_value()
                _at(value, path[:-1])[path[-1]] = ""
                self.assertInvalidValue(value)
            with self.subTest(path=path, invalid="non_nfc"):
                value = _fixture_value()
                _at(value, path[:-1])[path[-1]] = "e\u0301"
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                with self.assertRaisesRegex(
                    ExperimentPlanError, "^experiment_input_invalid$"
                ):
                    load_experiment_input(encoded)


class PilotScheduleTests(unittest.TestCase):
    def test_schedule_is_adjacent_balanced_deterministic_and_seeded(self):
        loaded = load_experiment_input(VALID_INPUT.read_bytes())
        seed = loaded.value["selection_seed"]
        candidates = loaded.value["candidates"]

        schedule = build_pilot_schedule(seed, candidates)

        self.assertEqual(tuple(run.ordinal for run in schedule), tuple(range(1, 9)))
        for index in range(0, len(schedule), 2):
            pair = schedule[index : index + 2]
            self.assertEqual(pair[0].task_id, pair[1].task_id)
            self.assertEqual(pair[0].difficulty, pair[1].difficulty)
            self.assertEqual(
                {pair[0].condition, pair[1].condition}, {"current", "lean"}
            )
        for difficulty in ("low", "medium"):
            first_conditions = [
                schedule[index].condition
                for index in range(0, len(schedule), 2)
                if schedule[index].difficulty == difficulty
            ]
            self.assertEqual(sorted(first_conditions), ["current", "lean"])
        self.assertEqual(build_pilot_schedule(seed, candidates), schedule)
        changed_schedules = {
            build_pilot_schedule(seed + "-" + str(index), candidates)
            for index in range(1, 9)
        }
        self.assertTrue(any(item != schedule for item in changed_schedules))


class AnalysisPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.cases = _analysis_boundaries_value()

    def test_correctness_delta_is_lean_minus_current(self):
        for case in self.cases["correctness_delta_cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    experiment_plan_module._correctness_delta(
                        case["current"], case["lean"]
                    ),
                    case["expected"],
                )

    def test_four_value_median_is_exact_mean_of_middle_values(self):
        for case in self.cases["median_cases"]:
            with self.subTest(case=case["name"]):
                median = experiment_plan_module._median(
                    tuple(Fraction(value, 1) for value in case["values"])
                )
                self.assertEqual(
                    median, Fraction(*case["expected_fraction"])
                )
                self.assertIsInstance(median, Fraction)

        for values in ((), (Fraction(1),) * 3, (Fraction(1),) * 5):
            with self.subTest(length=len(values)):
                with self.assertRaisesRegex(
                    ExperimentPlanError,
                    "^analysis_requires_four_pairs$",
                ):
                    experiment_plan_module._median(values)

    def test_reduction_is_exact_and_rejects_nonpositive_current_baseline(self):
        for case in self.cases["reduction_cases"]:
            with self.subTest(case=case["name"]):
                reduction = experiment_plan_module._reduction(
                    case["current"], case["lean"]
                )
                self.assertEqual(
                    reduction, Fraction(*case["expected_fraction"])
                )
                self.assertIsInstance(reduction, Fraction)

        for baseline in self.cases["invalid_current_baselines"]:
            with self.subTest(current=baseline):
                with self.assertRaisesRegex(
                    ExperimentPlanError,
                    "^analysis_baseline_not_positive$",
                ):
                    experiment_plan_module._reduction(baseline, 0)

    def test_exact_twenty_percent_and_adjacent_fractions_compare_correctly(self):
        threshold = Fraction(1, 5)
        for expected, cases in (
            (False, self.cases["threshold_cases"]["below"]),
            (True, self.cases["threshold_cases"]["passes"]),
        ):
            for case in cases:
                with self.subTest(case=case["name"]):
                    reduction = experiment_plan_module._reduction(
                        case["current"], case["lean"]
                    )
                    self.assertEqual(
                        reduction, Fraction(*case["expected_fraction"])
                    )
                    self.assertEqual(reduction >= threshold, expected)

    def test_display_rounding_never_changes_scalar_threshold_result(self):
        threshold = Fraction(1, 5)
        for expected, cases in (
            (False, self.cases["display_rounding_cases"]["below"]),
            (True, self.cases["display_rounding_cases"]["passes"]),
        ):
            for case in cases:
                with self.subTest(case=case["name"]):
                    reduction = experiment_plan_module._reduction(
                        case["current"], case["lean"]
                    )
                    self.assertEqual(
                        reduction, Fraction(*case["expected_fraction"])
                    )
                    self.assertEqual(
                        "{:.0%}".format(float(reduction)),
                        "20%",
                    )
                    self.assertEqual(reduction >= threshold, expected)


class AnalysisProjectionTypeTests(unittest.TestCase):
    def make_observation(self):
        return experiment_plan_module.ValidatedConditionObservation(
            task_id="task-alpha",
            condition="current",
            terminal_receipt_digest=_digest("terminal-current"),
            correctness_score=80,
            input_tokens=100,
            cached_input_tokens=40,
            output_tokens=30,
            reasoning_output_tokens=20,
            wall_time_milliseconds=500,
            active_review_milliseconds=200,
            machine_assertion_passed=True,
            absolute_safety_assertion_id=None,
            absolute_safety_basis_digest=None,
        )

    def test_projected_analysis_types_have_exact_fixed_fields(self):
        expected = {
            "ValidatedConditionObservation": (
                "task_id",
                "condition",
                "terminal_receipt_digest",
                "correctness_score",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "wall_time_milliseconds",
                "active_review_milliseconds",
                "machine_assertion_passed",
                "absolute_safety_assertion_id",
                "absolute_safety_basis_digest",
            ),
            "ValidatedPairObservation": ("task_id", "current", "lean"),
            "ValidatedMaskedReviewEvidence": (
                "packet_receipt_digest",
                "score_lock_receipt_digest",
                "unmask_receipt_digest",
                "high_regression_basis_digest",
            ),
            "ValidatedAnalysisDataset": (
                "plan_digest",
                "runtime_history_digest",
                "pairs",
                "masked_review",
                "partial_reason_codes",
                "_provenance",
            ),
            "ExperimentDecision": (
                "outcome",
                "comparative_aggregate_emitted",
                "median_correctness_delta",
                "efficiency_medians",
                "qualifying_efficiency_metrics",
                "reason_code",
            ),
            "TaskAssertionContract": (
                "task_id",
                "assertion_digest",
                "absolute_safety_assertion_ids",
            ),
            "AnalysisContract": (
                "contract_version",
                "plan_digest",
                "assertion_contract_digest",
                "task_assertions",
                "required_pair_count",
                "score_minimum",
                "score_maximum",
                "minimum_median_correctness_delta",
                "efficiency_reduction_threshold",
                "required_efficiency_count",
            ),
        }
        for type_name, expected_fields in expected.items():
            with self.subTest(type_name=type_name):
                value_type = getattr(experiment_plan_module, type_name)
                self.assertEqual(
                    tuple(item.name for item in fields(value_type)),
                    expected_fields,
                )

    def test_observation_is_frozen_and_reported_tokens_excludes_subsets(self):
        observation = self.make_observation()

        self.assertEqual(observation.reported_tokens, 130)
        with self.assertRaises(FrozenInstanceError):
            observation.task_id = "changed"

    def test_dataset_and_decision_detach_mutable_input_containers(self):
        observation = self.make_observation()
        pair = experiment_plan_module.ValidatedPairObservation(
            task_id=observation.task_id,
            current=observation,
            lean=None,
        )
        pairs = [pair]
        partial_reason_codes = ["missing_lean"]
        dataset = experiment_plan_module._make_validated_analysis_dataset(
            plan_digest=_digest("plan"),
            runtime_history_digest=_digest("runtime-history"),
            pairs=pairs,
            masked_review=None,
            partial_reason_codes=partial_reason_codes,
        )
        efficiency_medians = {
            "reported_tokens": Fraction(1, 5),
            "wall_time": None,
        }
        qualifying_metrics = ["reported_tokens"]
        decision = experiment_plan_module.ExperimentDecision(
            outcome="inconclusive",
            comparative_aggregate_emitted=True,
            median_correctness_delta=Fraction(-5, 1),
            efficiency_medians=efficiency_medians,
            qualifying_efficiency_metrics=qualifying_metrics,
            reason_code="threshold_not_met",
        )

        pairs.append(pair)
        partial_reason_codes.append("changed")
        efficiency_medians["reported_tokens"] = Fraction(1, 1)
        qualifying_metrics.append("changed")

        self.assertEqual(dataset.pairs, (pair,))
        self.assertEqual(dataset.partial_reason_codes, ("missing_lean",))
        self.assertIs(
            dataset._provenance,
            experiment_plan_module._ANALYSIS_DATASET_PROVENANCE,
        )
        self.assertNotIn("_provenance", repr(dataset))
        self.assertEqual(
            decision.efficiency_medians["reported_tokens"],
            Fraction(1, 5),
        )
        self.assertEqual(
            decision.qualifying_efficiency_metrics,
            ("reported_tokens",),
        )
        with self.assertRaises(TypeError):
            decision.efficiency_medians["wall_time"] = Fraction(1, 5)

    def test_dataset_factory_signature_injects_token_without_claiming_validity(self):
        signature = inspect.signature(
            experiment_plan_module._make_validated_analysis_dataset
        )
        self.assertEqual(
            tuple(signature.parameters),
            (
                "plan_digest",
                "runtime_history_digest",
                "pairs",
                "masked_review",
                "partial_reason_codes",
            ),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )
        self.assertNotIn("_provenance", signature.parameters)

        dataset = experiment_plan_module._make_validated_analysis_dataset(
            plan_digest="caller-supplied",
            runtime_history_digest="caller-supplied",
            pairs=[],
            masked_review=None,
            partial_reason_codes=[],
        )
        direct = experiment_plan_module.ValidatedAnalysisDataset(
            plan_digest=dataset.plan_digest,
            runtime_history_digest=dataset.runtime_history_digest,
            pairs=dataset.pairs,
            masked_review=dataset.masked_review,
            partial_reason_codes=dataset.partial_reason_codes,
            _provenance=object(),
        )

        self.assertIs(
            dataset._provenance,
            experiment_plan_module._ANALYSIS_DATASET_PROVENANCE,
        )
        self.assertIsNot(
            direct._provenance,
            experiment_plan_module._ANALYSIS_DATASET_PROVENANCE,
        )

    def test_task_four_analysis_boundary_signature_is_narrow(self):
        signature = inspect.signature(
            experiment_plan_module.analyze_pairs
        )
        self.assertEqual(
            tuple(signature.parameters), ("contract", "dataset")
        )
        self.assertEqual(
            tuple(
                parameter.kind
                for parameter in signature.parameters.values()
            ),
            (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ),
        )


class ExperimentPlanTests(unittest.TestCase):
    def setUp(self):
        self.experiment_input = load_experiment_input(VALID_INPUT.read_bytes())
        self.bundle_digest = _digest("bundle")
        self.current_profile_digest = _digest("profile-current")
        self.lean_profile_digest = _digest("profile-lean")
        self.source_receipts = tuple(
            sorted((_digest("source-a"), _digest("source-b")), key=str.encode)
        )
        self.selection_receipt = _digest("selection-receipt")
        self.corpus_receipt = _digest("corpus-receipt")
        self.templates, self.pilot_plans = self.make_children()

    def make_children(self):
        model = self.experiment_input.value["model"]
        invocation = self.experiment_input.value["invocation_policy"]
        containment = self.experiment_input.value["containment_policy_version"]
        templates = (
            CanaryInvocationTemplate(
                ordinal=1,
                profile="current",
                model_id=model["model_id"],
                reasoning_effort=model["reasoning_effort"],
                sandbox="read-only",
                approval_policy="never",
                provider_transport_allowed=True,
                tool_network_disabled=True,
                base_profile_digest=self.current_profile_digest,
                overlay_recipe_policy_digest=CANARY_OVERLAY_RECIPE_DIGEST,
                root_capability_policy_digest=ROOT_CAPABILITY_POLICY_DIGEST,
                child_process_policy=invocation["child_process_policy"],
                validator_policy=invocation["validator_policy"],
                output_schema_digest=CANARY_RESPONSE_SCHEMA_DIGEST,
                environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
                argv_template_digest=CANARY_ARGV_TEMPLATE_DIGEST,
                containment_policy_version=containment,
            ),
            CanaryInvocationTemplate(
                ordinal=2,
                profile="lean",
                model_id=model["model_id"],
                reasoning_effort=model["reasoning_effort"],
                sandbox="read-only",
                approval_policy="never",
                provider_transport_allowed=True,
                tool_network_disabled=True,
                base_profile_digest=self.lean_profile_digest,
                overlay_recipe_policy_digest=CANARY_OVERLAY_RECIPE_DIGEST,
                root_capability_policy_digest=ROOT_CAPABILITY_POLICY_DIGEST,
                child_process_policy=invocation["child_process_policy"],
                validator_policy=invocation["validator_policy"],
                output_schema_digest=CANARY_RESPONSE_SCHEMA_DIGEST,
                environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
                argv_template_digest=CANARY_ARGV_TEMPLATE_DIGEST,
                containment_policy_version=containment,
            ),
        )
        schedule = build_pilot_schedule(
            self.experiment_input.value["selection_seed"],
            self.experiment_input.value["candidates"],
        )
        pilot_plans = []
        for run in schedule:
            pilot_plans.append(
                PilotInvocationPlan(
                    ordinal=run.ordinal + 2,
                    run=run,
                    snapshot_receipt_digest=_digest(
                        "snapshot-{}".format(run.task_id)
                    ),
                    allowed_write_policy_digest=_digest(
                        "allowed-write-{}".format(run.task_id)
                    ),
                    base_profile_digest=(
                        self.current_profile_digest
                        if run.condition == "current"
                        else self.lean_profile_digest
                    ),
                    root_capability_policy_digest=(
                        ROOT_CAPABILITY_POLICY_DIGEST
                    ),
                    model_id=model["model_id"],
                    reasoning_effort=model["reasoning_effort"],
                    sandbox="workspace-write",
                    approval_policy="never",
                    provider_transport_allowed=True,
                    tool_network_disabled=True,
                    child_process_policy=invocation["child_process_policy"],
                    validator_policy=invocation["validator_policy"],
                    output_schema_digest=PILOT_RESPONSE_SCHEMA_DIGEST,
                    environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
                    argv_template_digest=PILOT_ARGV_TEMPLATE_DIGEST,
                    containment_policy_version=containment,
                )
            )
        return templates, tuple(pilot_plans)

    def build(self, **overrides):
        arguments = {
            "bundle_digest": self.bundle_digest,
            "current_profile_digest": self.current_profile_digest,
            "lean_profile_digest": self.lean_profile_digest,
            "task_source_trust_receipt_digests": self.source_receipts,
            "task_selection_receipt_digest": self.selection_receipt,
            "task_corpus_receipt_digest": self.corpus_receipt,
            "canary_templates": self.templates,
            "pilot_invocation_plans": self.pilot_plans,
        }
        arguments.update(overrides)
        return build_experiment_plan(
            arguments.pop("experiment_input", self.experiment_input), **arguments
        )

    def assertPlanInvalid(self, **overrides):
        with self.assertRaises(ExperimentPlanError) as raised:
            self.build(**overrides)
        self.assertEqual(str(raised.exception), "experiment_plan_invalid")

    def with_schedule(self, plan, schedule):
        document = thaw_json_value(plan.plan_document)
        document["pilot_schedule"] = [
            {
                "condition": run.condition,
                "difficulty": run.difficulty,
                "ordinal": run.ordinal,
                "task_id": run.task_id,
            }
            for run in schedule
        ]
        encoded = canonical_bytes(document)
        return replace(
            plan,
            plan_document=document,
            canonical_bytes=encoded,
            plan_digest=sha256_bytes(encoded),
            pilot_schedule=schedule,
        )

    def test_static_evidence_known_answer_documents_and_public_result_are_exact(
        self,
    ):
        derive = experiment_plan_module.derive_static_evidence_digests
        result_type = experiment_plan_module.StaticEvidenceDigests

        self.assertEqual(
            tuple(inspect.signature(derive).parameters),
            ("experiment_input",),
        )
        self.assertEqual(
            tuple(item.name for item in fields(result_type)),
            tuple(EXPECTED_STATIC_EVIDENCE_DIGESTS),
        )
        self.assertEqual(
            asdict(derive(self.experiment_input)),
            EXPECTED_STATIC_EVIDENCE_DIGESTS,
        )

        document_types = set()
        expected_key_by_document = {
            "candidate_set": "candidate_set_digest",
            "selection_seed": "selection_seed_digest",
            "pilot_schedule": "pilot_schedule_digest",
            "qualification": "qualification_digest",
            "reference_result": "reference_result_digest",
            "mutation_sensitivity": "mutation_sensitivity_digest",
            "difficulty_assignment": "difficulty_assignment_digest",
        }
        for name, document in EXPECTED_STATIC_EVIDENCE_DOCUMENTS.items():
            with self.subTest(document=name):
                document_types.add(document["document_type"])
                self.assertEqual(
                    sha256_bytes(canonical_bytes(document)),
                    EXPECTED_STATIC_EVIDENCE_DIGESTS[
                        expected_key_by_document[name]
                    ],
                )
                wrong_domain = copy.deepcopy(document)
                wrong_domain["document_type"] = (
                    "harness-experiment-cross-domain-v1"
                )
                self.assertNotEqual(
                    sha256_bytes(canonical_bytes(wrong_domain)),
                    EXPECTED_STATIC_EVIDENCE_DIGESTS[
                        expected_key_by_document[name]
                    ],
                )
        self.assertEqual(len(document_types), 7)
        self.assertEqual(len(set(EXPECTED_STATIC_EVIDENCE_DIGESTS.values())), 7)

        result = derive(self.experiment_input)
        with self.assertRaises(FrozenInstanceError):
            result.candidate_set_digest = _digest("changed")

    def test_plan_carries_only_seed_precommit_leaves_and_binds_all_four(self):
        plan = self.build()
        keys = (
            "masked_review_rubric_digest",
            "masked_review_seed_commitment_digest",
            "masked_review_seed_source_receipt_digest",
            "masked_review_seed_evidence_classification",
        )
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(
                    plan.plan_document[key],
                    self.experiment_input.value[key],
                )
                changed_input = _fixture_value()
                changed_input[key] = (
                    changed_input[key] + "-changed"
                )
                changed_input_bytes = canonical_bytes(changed_input)
                self.assertNotEqual(
                    sha256_bytes(changed_input_bytes),
                    self.experiment_input.input_digest,
                )

                changed_plan = thaw_json_value(plan.plan_document)
                changed_plan[key] = changed_plan[key] + "-changed"
                self.assertNotEqual(
                    sha256_bytes(canonical_bytes(changed_plan)),
                    plan.plan_digest,
                )

        raw_seed = ("0123456789abcdef" * 4).encode("ascii")
        self.assertNotIn(b"seed_hex", plan.canonical_bytes)
        self.assertNotIn(raw_seed, plan.canonical_bytes)

    def test_static_evidence_preserves_schedule_control_and_mutant_order(self):
        derive = experiment_plan_module.derive_static_evidence_digests
        expected_schedule_document = EXPECTED_STATIC_EVIDENCE_DOCUMENTS[
            "pilot_schedule"
        ]
        self.assertNotEqual(
            [
                record["task_id"]
                for record in expected_schedule_document["pilot_schedule"]
            ],
            sorted(
                (
                    record["task_id"]
                    for record in expected_schedule_document["pilot_schedule"]
                ),
                key=str.encode,
            ),
        )

        first = _fixture_value()
        first["candidates"][0]["negative_controls"] = [
            {"control_id": "z-control", "result": "fail"},
            {"control_id": "a-control", "result": "fail"},
        ]
        first["candidates"][0]["behavior_mutants"] = [
            {
                "category": "incorrect_result",
                "mutant_digest": _digest("mutant-z"),
                "result": "fail",
            },
            {
                "category": "formatting",
                "mutant_digest": _digest("mutant-a"),
                "result": "fail",
            },
        ]
        second = copy.deepcopy(first)
        second["candidates"][0]["negative_controls"].reverse()
        second["candidates"][0]["behavior_mutants"].reverse()

        first_result = derive(load_experiment_input(canonical_bytes(first)))
        second_result = derive(load_experiment_input(canonical_bytes(second)))
        self.assertNotEqual(
            first_result.mutation_sensitivity_digest,
            second_result.mutation_sensitivity_digest,
        )
        self.assertNotEqual(
            first_result.qualification_digest,
            second_result.qualification_digest,
        )
        for field_name in (
            "candidate_set_digest",
            "selection_seed_digest",
            "pilot_schedule_digest",
            "reference_result_digest",
            "difficulty_assignment_digest",
        ):
            with self.subTest(field=field_name):
                self.assertEqual(
                    getattr(first_result, field_name),
                    getattr(second_result, field_name),
                )

    def test_static_evidence_binds_mutation_authority_and_outcomes(self):
        derive = experiment_plan_module.derive_static_evidence_digests
        baseline = derive(self.experiment_input)

        mutations = (
            (
                "validator",
                lambda candidate: candidate.__setitem__(
                    "validator_digest", _digest("changed-validator")
                ),
            ),
            (
                "assertion",
                lambda candidate: candidate.__setitem__(
                    "assertion_digest", _digest("changed-assertion")
                ),
            ),
            (
                "control",
                lambda candidate: candidate["negative_controls"][0].__setitem__(
                    "control_id", "changed-control"
                ),
            ),
            (
                "mutant-category",
                lambda candidate: candidate["behavior_mutants"][0].__setitem__(
                    "category", "changed_behavior"
                ),
            ),
            (
                "mutant-digest",
                lambda candidate: candidate["behavior_mutants"][0].__setitem__(
                    "mutant_digest", _digest("changed-mutant")
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(mutation=label):
                document = _fixture_value()
                mutate(document["candidates"][0])
                changed = derive(
                    load_experiment_input(canonical_bytes(document))
                )
                self.assertNotEqual(
                    changed.mutation_sensitivity_digest,
                    baseline.mutation_sensitivity_digest,
                )
                self.assertNotEqual(
                    changed.qualification_digest,
                    baseline.qualification_digest,
                )

    def test_qualification_binds_leaf_digests_and_operator_attested_records(
        self,
    ):
        derive = experiment_plan_module.derive_static_evidence_digests
        baseline = derive(self.experiment_input)
        cases = (
            (
                "candidate-set",
                "candidate_set_digest",
                lambda candidate: candidate.__setitem__(
                    "commit_oid", "5" * 40
                ),
            ),
            (
                "reference-result",
                "reference_result_digest",
                lambda candidate: candidate.__setitem__(
                    "task_id", "low-alphaa"
                ),
            ),
            (
                "mutation-sensitivity",
                "mutation_sensitivity_digest",
                lambda candidate: candidate.__setitem__(
                    "validator_digest", _digest("qualification-validator")
                ),
            ),
            (
                "difficulty-assignment",
                "difficulty_assignment_digest",
                lambda candidate: candidate.__setitem__(
                    "difficulty_rubric_digest",
                    _digest("qualification-rubric"),
                ),
            ),
        )
        for label, leaf_field, mutate in cases:
            with self.subTest(leaf=label):
                document = _fixture_value()
                mutate(document["candidates"][0])
                changed = derive(
                    load_experiment_input(canonical_bytes(document))
                )
                self.assertNotEqual(
                    getattr(changed, leaf_field),
                    getattr(baseline, leaf_field),
                )
                self.assertNotEqual(
                    changed.qualification_digest,
                    baseline.qualification_digest,
                )

        document = _fixture_value()
        document["candidates"][0]["provenance_id"] = (
            "operator-corpus-changed"
        )
        attestation_changed = derive(
            load_experiment_input(canonical_bytes(document))
        )
        self.assertNotEqual(
            attestation_changed.qualification_digest,
            baseline.qualification_digest,
        )
        for field_name in (
            "candidate_set_digest",
            "selection_seed_digest",
            "pilot_schedule_digest",
            "reference_result_digest",
            "mutation_sensitivity_digest",
            "difficulty_assignment_digest",
        ):
            with self.subTest(operator_record_non_leaf=field_name):
                self.assertEqual(
                    getattr(attestation_changed, field_name),
                    getattr(baseline, field_name),
                )

    def test_static_evidence_revalidates_exact_input_dataclass(self):
        derive = experiment_plan_module.derive_static_evidence_digests
        forged_digest = replace(
            self.experiment_input,
            input_digest=_digest("forged-input"),
        )
        with self.assertRaises(ExperimentPlanError):
            derive(forged_digest)

        subclass = _CanonicalExperimentInputSubclass(
            canonical_bytes=self.experiment_input.canonical_bytes,
            value=self.experiment_input.value,
            input_digest=self.experiment_input.input_digest,
        )
        with self.assertRaises(ExperimentPlanError):
            derive(subclass)

        extra_field = load_experiment_input(VALID_INPUT.read_bytes())
        object.__setattr__(extra_field, "unexpected", "field")
        with self.assertRaises(ExperimentPlanError):
            derive(extra_field)

        with self.assertRaises(ExperimentPlanError):
            derive(object())

    def test_static_evidence_rejects_malformed_order_without_normalizing(self):
        derive = experiment_plan_module.derive_static_evidence_digests
        unordered_documents = []

        candidate_order = _fixture_value()
        candidate_order["candidates"].reverse()
        unordered_documents.append(candidate_order)

        allowed_write_order = _fixture_value()
        allowed_write_order["candidates"][0][
            "allowed_write_paths"
        ].reverse()
        unordered_documents.append(allowed_write_order)

        for document in unordered_documents:
            with self.subTest(
                first_task=document["candidates"][0]["task_id"],
                paths=document["candidates"][0]["allowed_write_paths"],
            ):
                encoded = canonical_bytes(document)
                forged = CanonicalExperimentInput(
                    canonical_bytes=encoded,
                    value=document,
                    input_digest=sha256_bytes(encoded),
                )
                with self.assertRaises(ExperimentPlanError):
                    derive(forged)

    def test_fixed_v1_documents_are_exact_immutable_and_digest_bound(self):
        documents = (
            (CANARY_OVERLAY_RECIPE, EXPECTED_CANARY_OVERLAY_RECIPE),
            (CANARY_RESPONSE_SCHEMA, EXPECTED_CANARY_RESPONSE_SCHEMA),
            (PILOT_RESPONSE_SCHEMA, EXPECTED_PILOT_RESPONSE_SCHEMA),
            (CANARY_ARGV_TEMPLATE, EXPECTED_CANARY_ARGV_TEMPLATE),
            (PILOT_ARGV_TEMPLATE, EXPECTED_PILOT_ARGV_TEMPLATE),
            (ENVIRONMENT_POLICY, EXPECTED_ENVIRONMENT_POLICY),
            (ROOT_CAPABILITY_POLICY, EXPECTED_ROOT_CAPABILITY_POLICY),
            (CALL_ALLOCATION, EXPECTED_CALL_ALLOCATION),
        )
        digests = (
            CANARY_OVERLAY_RECIPE_DIGEST,
            CANARY_RESPONSE_SCHEMA_DIGEST,
            PILOT_RESPONSE_SCHEMA_DIGEST,
            CANARY_ARGV_TEMPLATE_DIGEST,
            PILOT_ARGV_TEMPLATE_DIGEST,
            ENVIRONMENT_POLICY_DIGEST,
            ROOT_CAPABILITY_POLICY_DIGEST,
            CALL_ALLOCATION_DIGEST,
        )
        for (actual, expected), digest in zip(documents, digests):
            with self.subTest(document=expected.get("document_type", expected.get("$id"))):
                self.assertEqual(thaw_json_value(actual), expected)
                self.assertEqual(digest, sha256_bytes(canonical_bytes(expected)))
                with self.assertRaises(TypeError):
                    actual["changed"] = True

    def test_argv_templates_bind_exact_order_isolation_and_reasoning_encoding(self):
        for document, sandbox in (
            (EXPECTED_CANARY_ARGV_TEMPLATE, "read-only"),
            (EXPECTED_PILOT_ARGV_TEMPLATE, "workspace-write"),
        ):
            with self.subTest(sandbox=sandbox):
                atoms = document["argv_tail"]
                literals = [
                    atom["literal"] for atom in atoms if "literal" in atom
                ]
                self.assertEqual(literals[:3], ["-a", "never", "exec"])
                self.assertIn("--ignore-user-config", literals)
                self.assertIn("--ignore-rules", literals)
                self.assertEqual(
                    document["ignore_rules_semantics"],
                    "execpolicy_dot_rules_only",
                )
                self.assertEqual(
                    atoms[literals.index("--sandbox") + 1]["literal"], sandbox
                )
                reasoning = next(
                    atom for atom in atoms if atom.get("slot") == "reasoning_effort"
                )
                self.assertEqual(
                    reasoning,
                    {
                        "encoding": "canonical_json_string_as_toml_basic_string",
                        "prefix": "model_reasoning_effort=",
                        "slot": "reasoning_effort",
                    },
                )
                self.assertEqual(
                    reasoning["prefix"]
                    + canonical_bytes("high").decode("utf-8"),
                    'model_reasoning_effort="high"',
                )
                self.assertEqual(
                    document["runtime_executable_binding"],
                    "future_child_receipt",
                )
        self.assertIn("global AGENTS.md instructions", CANARY_ARGV_TEMPLATE["stdin_utf8"])
        self.assertEqual(
            PILOT_ARGV_TEMPLATE["stdin_binding"],
            "plan_candidate_prompt_digest",
        )

    def test_public_builder_signature_is_exact_and_path_free(self):
        signature = inspect.signature(build_experiment_plan)
        self.assertEqual(
            tuple(signature.parameters),
            (
                "experiment_input",
                "bundle_digest",
                "current_profile_digest",
                "lean_profile_digest",
                "task_source_trust_receipt_digests",
                "task_selection_receipt_digest",
                "task_corpus_receipt_digest",
                "canary_templates",
                "pilot_invocation_plans",
            ),
        )
        self.assertEqual(
            signature.parameters["experiment_input"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for name in tuple(signature.parameters)[1:]:
            self.assertEqual(
                signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY
            )
        self.assertNotIn("Path", str(signature))

    def test_build_analysis_contract_is_plan_derived_fixed_and_digest_bound(self):
        plan = self.build()
        signature = inspect.signature(
            experiment_plan_module.build_analysis_contract
        )
        self.assertEqual(tuple(signature.parameters), ("plan",))
        self.assertEqual(
            signature.parameters["plan"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )

        contract = experiment_plan_module.build_analysis_contract(plan)
        candidates = {
            item["task_id"]: item
            for item in plan.plan_document["candidates"]
        }
        task_ids = tuple(
            plan.pilot_schedule[index].task_id
            for index in range(0, len(plan.pilot_schedule), 2)
        )
        expected_tasks = [
            {
                "absolute_safety_assertion_ids": list(
                    candidates[task_id]["absolute_safety_assertion_ids"]
                ),
                "assertion_digest": candidates[task_id][
                    "assertion_digest"
                ],
                "task_id": task_id,
            }
            for task_id in task_ids
        ]
        assertion_document = {
            "contract_version": "four-pair-screening-v1",
            "document_type": "analysis_assertion_contract",
            "plan_digest": plan.plan_digest,
            "schema_version": 1,
            "tasks": expected_tasks,
        }

        self.assertEqual(contract.contract_version, "four-pair-screening-v1")
        self.assertEqual(contract.plan_digest, plan.plan_digest)
        self.assertEqual(
            contract.assertion_contract_digest,
            sha256_bytes(canonical_bytes(assertion_document)),
        )
        self.assertEqual(
            tuple(item.task_id for item in contract.task_assertions),
            task_ids,
        )
        self.assertEqual(
            tuple(
                {
                    "absolute_safety_assertion_ids": list(
                        item.absolute_safety_assertion_ids
                    ),
                    "assertion_digest": item.assertion_digest,
                    "task_id": item.task_id,
                }
                for item in contract.task_assertions
            ),
            tuple(expected_tasks),
        )
        self.assertEqual(contract.required_pair_count, 4)
        self.assertEqual(contract.score_minimum, 0)
        self.assertEqual(contract.score_maximum, 100)
        self.assertEqual(
            contract.minimum_median_correctness_delta,
            Fraction(-5, 1),
        )
        self.assertEqual(
            contract.efficiency_reduction_threshold,
            Fraction(1, 5),
        )
        self.assertEqual(contract.required_efficiency_count, 2)
        self.assertIsInstance(
            contract.minimum_median_correctness_delta, Fraction
        )
        self.assertIsInstance(
            contract.efficiency_reduction_threshold, Fraction
        )

    def test_analysis_contract_types_detach_mutable_sequences(self):
        assertion_ids = ["safety-a"]
        task = experiment_plan_module.TaskAssertionContract(
            task_id="task-alpha",
            assertion_digest=_digest("assertion"),
            absolute_safety_assertion_ids=assertion_ids,
        )
        task_assertions = [task]
        contract = experiment_plan_module.AnalysisContract(
            contract_version="four-pair-screening-v1",
            plan_digest=_digest("plan"),
            assertion_contract_digest=_digest("assertion-contract"),
            task_assertions=task_assertions,
            required_pair_count=4,
            score_minimum=0,
            score_maximum=100,
            minimum_median_correctness_delta=Fraction(-5, 1),
            efficiency_reduction_threshold=Fraction(1, 5),
            required_efficiency_count=2,
        )

        assertion_ids.append("changed")
        task_assertions.append(task)

        self.assertEqual(task.absolute_safety_assertion_ids, ("safety-a",))
        self.assertEqual(contract.task_assertions, (task,))
        with self.assertRaises(FrozenInstanceError):
            contract.required_pair_count = 5

    def test_build_analysis_contract_rejects_forged_or_inconsistent_plans(self):
        plan = self.build()
        mutable = _MutableExperimentPlan(
            **{
                item.name: getattr(plan, item.name)
                for item in fields(experiment_plan_module.ExperimentPlan)
            }
        )
        object.__setattr__(mutable, "mutable_state", [])
        public_reordered = (
            plan.pilot_schedule[2:4]
            + plan.pilot_schedule[0:2]
            + plan.pilot_schedule[4:]
        )
        broken_pair = list(plan.pilot_schedule)
        broken_pair[1] = replace(
            broken_pair[1],
            task_id=broken_pair[2].task_id,
            difficulty=broken_pair[2].difficulty,
        )
        unknown_task = list(plan.pilot_schedule)
        unknown_task[0] = replace(unknown_task[0], task_id="unknown-task")
        unknown_task[1] = replace(unknown_task[1], task_id="unknown-task")
        invalid_plans = (
            object(),
            mutable,
            replace(plan, canonical_bytes=b"{}"),
            replace(plan, plan_digest=_digest("forged-plan")),
            replace(plan, pilot_schedule=public_reordered),
            self.with_schedule(plan, plan.pilot_schedule[:-1]),
            self.with_schedule(plan, tuple(broken_pair)),
            self.with_schedule(plan, tuple(unknown_task)),
        )

        for invalid in invalid_plans:
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(ExperimentPlanError) as raised:
                    experiment_plan_module.build_analysis_contract(invalid)
                self.assertEqual(
                    str(raised.exception),
                    "experiment_plan_invalid",
                )

    def test_build_analysis_contract_rejects_coherent_forged_review_context(self):
        plan = self.build()
        document = thaw_json_value(plan.plan_document)
        document["masked_review_context_digest"] = _digest(
            "other-masked-review-context"
        )
        encoded = canonical_bytes(document)
        forged = replace(
            plan,
            plan_document=document,
            canonical_bytes=encoded,
            plan_digest=sha256_bytes(encoded),
        )
        self.assertEqual(forged.input_digest, plan.input_digest)
        self.assertEqual(forged.canonical_bytes, canonical_bytes(document))
        self.assertEqual(
            forged.plan_digest,
            sha256_bytes(forged.canonical_bytes),
        )

        with self.assertRaisesRegex(
            ExperimentPlanError, "^experiment_plan_invalid$"
        ):
            experiment_plan_module.build_analysis_contract(forged)

    def test_analyze_pairs_applies_exact_thresholds_and_ineligible_metrics(self):
        plan = self.build()
        contract = experiment_plan_module.build_analysis_contract(plan)

        def make_dataset(*, lean_score, zero_baseline=None):
            pairs = []
            for task in contract.task_assertions:
                if zero_baseline == "reported_tokens":
                    current_input = current_output = 0
                    lean_input = lean_output = 0
                    current_reasoning = lean_reasoning = 0
                else:
                    current_input, current_output = 80, 20
                    lean_input, lean_output = 60, 20
                    current_reasoning = lean_reasoning = 5
                current = experiment_plan_module.ValidatedConditionObservation(
                    task_id=task.task_id,
                    condition="current",
                    terminal_receipt_digest=_digest(
                        "{}-current".format(task.task_id)
                    ),
                    correctness_score=80,
                    input_tokens=current_input,
                    cached_input_tokens=0,
                    output_tokens=current_output,
                    reasoning_output_tokens=current_reasoning,
                    wall_time_milliseconds=(
                        0
                        if zero_baseline == "wall_time_milliseconds"
                        else 1_000
                    ),
                    active_review_milliseconds=(
                        0
                        if zero_baseline
                        == "active_review_milliseconds"
                        else 1_000
                    ),
                    machine_assertion_passed=True,
                    absolute_safety_assertion_id=None,
                    absolute_safety_basis_digest=None,
                )
                lean = experiment_plan_module.ValidatedConditionObservation(
                    task_id=task.task_id,
                    condition="lean",
                    terminal_receipt_digest=_digest(
                        "{}-lean".format(task.task_id)
                    ),
                    correctness_score=lean_score,
                    input_tokens=lean_input,
                    cached_input_tokens=0,
                    output_tokens=lean_output,
                    reasoning_output_tokens=lean_reasoning,
                    wall_time_milliseconds=(
                        0
                        if zero_baseline == "wall_time_milliseconds"
                        else 800
                    ),
                    active_review_milliseconds=(
                        0
                        if zero_baseline
                        == "active_review_milliseconds"
                        else 800
                    ),
                    machine_assertion_passed=True,
                    absolute_safety_assertion_id=None,
                    absolute_safety_basis_digest=None,
                )
                pairs.append(
                    experiment_plan_module.ValidatedPairObservation(
                        task_id=task.task_id,
                        current=current,
                        lean=lean,
                    )
                )
            return experiment_plan_module._make_validated_analysis_dataset(
                plan_digest=plan.plan_digest,
                runtime_history_digest=_digest("runtime-history"),
                pairs=pairs,
                masked_review=(
                    experiment_plan_module.ValidatedMaskedReviewEvidence(
                        packet_receipt_digest=_digest("packet"),
                        score_lock_receipt_digest=_digest("score-lock"),
                        unmask_receipt_digest=_digest("unmask"),
                        high_regression_basis_digest=None,
                    )
                ),
                partial_reason_codes=(),
            )

        exact_threshold = experiment_plan_module.analyze_pairs(
            contract, make_dataset(lean_score=75)
        )
        self.assertEqual(
            exact_threshold.outcome, "advance_to_larger_study"
        )
        self.assertEqual(
            exact_threshold.median_correctness_delta,
            Fraction(-5, 1),
        )
        self.assertEqual(
            exact_threshold.efficiency_medians,
            {
                "reported_tokens": Fraction(1, 5),
                "wall_time_milliseconds": Fraction(1, 5),
                "active_review_milliseconds": Fraction(1, 5),
            },
        )

        below_correctness = experiment_plan_module.analyze_pairs(
            contract, make_dataset(lean_score=74)
        )
        self.assertEqual(below_correctness.outcome, "inconclusive")
        self.assertEqual(
            below_correctness.reason_code,
            "screening_thresholds_not_met",
        )

        metric_order = (
            "reported_tokens",
            "wall_time_milliseconds",
            "active_review_milliseconds",
        )
        for zero_baseline in metric_order:
            with self.subTest(zero_baseline=zero_baseline):
                ineligible = experiment_plan_module.analyze_pairs(
                    contract,
                    make_dataset(
                        lean_score=75,
                        zero_baseline=zero_baseline,
                    ),
                )
                self.assertEqual(
                    tuple(ineligible.efficiency_medians),
                    metric_order,
                )
                self.assertIsNone(
                    ineligible.efficiency_medians[zero_baseline]
                )
                self.assertEqual(
                    ineligible.qualifying_efficiency_metrics,
                    tuple(
                        metric
                        for metric in metric_order
                        if metric != zero_baseline
                    ),
                )
                self.assertNotIn(
                    zero_baseline,
                    ineligible.qualifying_efficiency_metrics,
                )
                self.assertEqual(
                    ineligible.outcome, "advance_to_larger_study"
                )

    def test_builds_deterministic_deeply_immutable_canonical_plan(self):
        plan = self.build()
        repeated = self.build()
        expected_schedule = build_pilot_schedule(
            self.experiment_input.value["selection_seed"],
            self.experiment_input.value["candidates"],
        )

        self.assertEqual(plan, repeated)
        self.assertEqual(plan.input_digest, self.experiment_input.input_digest)
        self.assertEqual(plan.pilot_schedule, expected_schedule)
        self.assertEqual(plan.canonical_bytes, _frozen_document(plan.plan_document))
        self.assertEqual(plan.plan_digest, sha256_bytes(plan.canonical_bytes))
        expected_added = {
            "input_digest",
            "bundle_digest",
            "current_profile_digest",
            "lean_profile_digest",
            "task_source_trust_receipt_digests",
            "task_selection_receipt_digest",
            "task_corpus_receipt_digest",
            "pilot_schedule",
            "canary_template_digests",
            "pilot_invocation_plan_digests",
            "call_allocation_digest",
            "masked_review_context_digest",
        }
        self.assertEqual(
            set(plan.plan_document),
            set(self.experiment_input.value).union(expected_added),
        )
        self.assertEqual(
            plan.plan_document["call_allocation_digest"], CALL_ALLOCATION_DIGEST
        )
        self.assertEqual(len(plan.plan_document["canary_template_digests"]), 2)
        self.assertEqual(len(plan.plan_document["pilot_invocation_plan_digests"]), 8)
        self.assertTrue(
            all(
                value.startswith("sha256:")
                for value in plan.plan_document["canary_template_digests"]
                + plan.plan_document["pilot_invocation_plan_digests"]
            )
        )
        for forbidden in (
            "runtime_containment",
            "canary_outcome",
            "pilot_outcome",
            "review_outcome",
            "unmask_outcome",
            "stop_outcome",
            "decision_outcome",
        ):
            self.assertNotIn(forbidden, plan.plan_document)
        with self.assertRaises(TypeError):
            plan.plan_document["schema_version"] = 2
        with self.assertRaises(TypeError):
            plan.plan_document["candidates"][0]["task_id"] = "changed"
        with self.assertRaises((AttributeError, TypeError)):
            plan.pilot_schedule.append("changed")
        with self.assertRaises(FrozenInstanceError):
            plan.pilot_invocation_plans[0].allowed_write_policy_digest = (
                _digest("changed")
            )

    def test_durable_plan_has_no_local_path_or_runtime_secret_values(self):
        plan = self.build()

        def walk(value):
            self.assertNotIsInstance(value, Path)
            if isinstance(value, dict) or hasattr(value, "items"):
                for key, item in value.items():
                    self.assertNotIsInstance(key, Path)
                    yield from walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    yield from walk(item)
            else:
                yield value

        for value in walk(plan.plan_document):
            if isinstance(value, str):
                self.assertFalse(value.startswith(("/", "\\", "file:")))
                self.assertNotIn("/U" "sers/", value)
                self.assertNotIn("credential-value", value)

    def test_canary_templates_exclude_runtime_only_identity_and_secret_fields(self):
        template_fields = {item.name for item in fields(CanaryInvocationTemplate)}
        for forbidden in (
            "marker",
            "marker_digest",
            "derived_home_identity_digest",
            "executable",
            "executable_identity_digest",
            "credential",
            "task_root_identity_digest",
            "runtime_root_identity_digest",
        ):
            self.assertNotIn(forbidden, template_fields)
            with self.subTest(forbidden=forbidden):
                arguments = asdict(self.templates[0])
                arguments[forbidden] = "forbidden"
                with self.assertRaises(TypeError):
                    CanaryInvocationTemplate(**arguments)

    def test_builder_rejects_child_subclasses_with_digest_excluded_mutable_slots(self):
        canary = _MutableCanaryTemplate(
            **{
                item.name: getattr(self.templates[0], item.name)
                for item in fields(CanaryInvocationTemplate)
            }
        )
        object.__setattr__(canary, "mutable_state", [])
        pilot = _MutablePilotInvocationPlan(
            **{
                item.name: getattr(self.pilot_plans[0], item.name)
                for item in fields(PilotInvocationPlan)
            }
        )
        object.__setattr__(pilot, "mutable_state", [])
        self.assertEqual(vars(canary), vars(self.templates[0]))
        self.assertEqual(vars(pilot), vars(self.pilot_plans[0]))
        self.assertNotIn("mutable_state", vars(canary))
        self.assertNotIn("mutable_state", vars(pilot))

        with self.subTest(child_type="canary"):
            self.assertPlanInvalid(
                canary_templates=(canary, self.templates[1])
            )
        with self.subTest(child_type="pilot"):
            self.assertPlanInvalid(
                pilot_invocation_plans=(pilot,) + self.pilot_plans[1:]
            )

    def test_builder_rejects_wrong_canary_ordinals_profiles_policies_and_sandbox(self):
        invalid_templates = (
            replace(self.templates[0], ordinal=2),
            replace(self.templates[0], profile="lean"),
            replace(self.templates[0], sandbox="unknown"),
            replace(self.templates[0], sandbox="danger-full-access"),
            replace(self.templates[0], provider_transport_allowed=1),
            replace(self.templates[0], tool_network_disabled=False),
            replace(self.templates[0], base_profile_digest=self.lean_profile_digest),
            replace(
                self.templates[0],
                overlay_recipe_policy_digest=_digest("wrong-overlay"),
            ),
            replace(self.templates[0], output_schema_digest=_digest("wrong-schema")),
            replace(self.templates[0], argv_template_digest=_digest("wrong-argv")),
        )
        for template in invalid_templates:
            with self.subTest(template=template):
                self.assertPlanInvalid(
                    canary_templates=(template, self.templates[1])
                )

    def test_pilot_plans_require_exact_schedule_and_policy_bindings(self):
        original = self.pilot_plans[0]
        other_profile_digest = (
            self.lean_profile_digest
            if original.run.condition == "current"
            else self.current_profile_digest
        )
        invalid_plans = (
            replace(original, ordinal=4),
            replace(original, run=replace(original.run, condition="other")),
            replace(original, sandbox="unknown"),
            replace(original, sandbox="danger-full-access"),
            replace(original, provider_transport_allowed=1),
            replace(original, tool_network_disabled=False),
            replace(
                original,
                base_profile_digest=other_profile_digest,
            ),
            replace(
                original,
                root_capability_policy_digest=_digest(
                    "wrong-root-capability-policy"
                ),
            ),
            replace(original, output_schema_digest=_digest("wrong-schema")),
        )
        for invalid in invalid_plans:
            with self.subTest(invalid=invalid):
                self.assertPlanInvalid(
                    pilot_invocation_plans=(invalid,) + self.pilot_plans[1:]
                )

        arguments = asdict(original)
        del arguments["root_capability_policy_digest"]
        with self.assertRaises(TypeError):
            PilotInvocationPlan(**arguments)

    def test_pilot_child_schema_replaces_runtime_roots_with_policy_digests(self):
        expected_fields = (
            "ordinal",
            "run",
            "snapshot_receipt_digest",
            "allowed_write_policy_digest",
            "base_profile_digest",
            "root_capability_policy_digest",
            "model_id",
            "reasoning_effort",
            "sandbox",
            "approval_policy",
            "provider_transport_allowed",
            "tool_network_disabled",
            "child_process_policy",
            "validator_policy",
            "output_schema_digest",
            "environment_policy_digest",
            "argv_template_digest",
            "containment_policy_version",
        )
        self.assertEqual(
            tuple(item.name for item in fields(PilotInvocationPlan)),
            expected_fields,
        )
        expected_document_fields = set(expected_fields).difference({"run"}).union(
            {"document_type", "run", "schema_version"}
        )
        document = experiment_plan_module._pilot_invocation_plan_document(
            self.pilot_plans[0]
        )
        self.assertEqual(set(document), expected_document_fields)

        for forbidden in (
            "codex_home_identity_digest",
            "task_root_identity_digest",
            "temp_root_identity_digest",
            "tool_read_root_identity_digests",
            "tool_write_root_identity_digests",
            "validator_read_root_identity_digests",
            "validator_write_root_identity_digests",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, document)
                arguments = asdict(self.pilot_plans[0])
                arguments[forbidden] = _digest(forbidden)
                with self.assertRaises(TypeError):
                    PilotInvocationPlan(**arguments)

    def test_pilot_snapshot_and_allowed_write_receipts_are_task_bound(self):
        plan = self.build()
        child_field_names = tuple(
            item.name for item in fields(PilotInvocationPlan)
        )
        self.assertEqual(
            child_field_names[
                child_field_names.index("run") :
                child_field_names.index("run") + 5
            ],
            (
                "run",
                "snapshot_receipt_digest",
                "allowed_write_policy_digest",
                "base_profile_digest",
                "root_capability_policy_digest",
            ),
        )

        snapshots_by_task = {}
        allowed_writes_by_task = {}
        for child in plan.pilot_invocation_plans:
            snapshots_by_task.setdefault(child.run.task_id, set()).add(
                child.snapshot_receipt_digest
            )
            allowed_writes_by_task.setdefault(child.run.task_id, set()).add(
                child.allowed_write_policy_digest
            )
            self.assertEqual(
                child.base_profile_digest,
                (
                    self.current_profile_digest
                    if child.run.condition == "current"
                    else self.lean_profile_digest
                ),
            )
            self.assertEqual(
                child.root_capability_policy_digest,
                ROOT_CAPABILITY_POLICY_DIGEST,
            )
        self.assertEqual(len(snapshots_by_task), 4)
        self.assertEqual(len(allowed_writes_by_task), 4)
        self.assertTrue(
            all(len(values) == 1 for values in snapshots_by_task.values())
        )
        self.assertTrue(
            all(len(values) == 1 for values in allowed_writes_by_task.values())
        )
        self.assertEqual(
            len(
                {
                    next(iter(values))
                    for values in snapshots_by_task.values()
                }
            ),
            4,
        )
        self.assertEqual(
            len(
                {
                    next(iter(values))
                    for values in allowed_writes_by_task.values()
                }
            ),
            4,
        )

        expected_documents = tuple(
            experiment_plan_module._pilot_invocation_plan_document(child)
            for child in plan.pilot_invocation_plans
        )
        self.assertEqual(
            tuple(
                document["snapshot_receipt_digest"]
                for document in expected_documents
            ),
            tuple(
                child.snapshot_receipt_digest
                for child in plan.pilot_invocation_plans
            ),
        )
        self.assertEqual(
            tuple(
                document["allowed_write_policy_digest"]
                for document in expected_documents
            ),
            tuple(
                child.allowed_write_policy_digest
                for child in plan.pilot_invocation_plans
            ),
        )

        task_id = plan.pilot_invocation_plans[0].run.task_id
        changed_snapshot_digest = _digest("replacement-snapshot")
        changed_children = tuple(
            replace(
                child,
                snapshot_receipt_digest=changed_snapshot_digest,
            )
            if child.run.task_id == task_id
            else child
            for child in self.pilot_plans
        )
        changed_plan = self.build(pilot_invocation_plans=changed_children)
        self.assertNotEqual(changed_plan.plan_digest, plan.plan_digest)
        self.assertNotEqual(
            changed_plan.plan_document["pilot_invocation_plan_digests"],
            plan.plan_document["pilot_invocation_plan_digests"],
        )

        changed_allowed_digest = _digest("replacement-allowed-write")
        changed_children = tuple(
            replace(
                child,
                allowed_write_policy_digest=changed_allowed_digest,
            )
            if child.run.task_id == task_id
            else child
            for child in self.pilot_plans
        )
        changed_plan = self.build(pilot_invocation_plans=changed_children)
        self.assertNotEqual(changed_plan.plan_digest, plan.plan_digest)
        self.assertNotEqual(
            changed_plan.plan_document["pilot_invocation_plan_digests"],
            plan.plan_document["pilot_invocation_plan_digests"],
        )

    def test_pilot_task_digests_reject_pair_mismatch_and_cross_task_reuse(self):
        first = self.pilot_plans[0]
        matching_index = next(
            index
            for index, child in enumerate(self.pilot_plans[1:], start=1)
            if child.run.task_id == first.run.task_id
        )
        mismatched_pair = list(self.pilot_plans)
        mismatched_pair[matching_index] = replace(
            mismatched_pair[matching_index],
            snapshot_receipt_digest=_digest("mismatched-snapshot"),
        )
        self.assertPlanInvalid(
            pilot_invocation_plans=tuple(mismatched_pair)
        )

        other_index = next(
            index
            for index, child in enumerate(self.pilot_plans)
            if child.run.task_id != first.run.task_id
        )
        other_task_id = self.pilot_plans[other_index].run.task_id
        cross_task = tuple(
            replace(
                child,
                snapshot_receipt_digest=first.snapshot_receipt_digest,
            )
            if child.run.task_id == other_task_id
            else child
            for child in self.pilot_plans
        )
        self.assertPlanInvalid(pilot_invocation_plans=cross_task)

        mismatched_pair = list(self.pilot_plans)
        mismatched_pair[matching_index] = replace(
            mismatched_pair[matching_index],
            allowed_write_policy_digest=_digest(
                "mismatched-allowed-write"
            ),
        )
        self.assertPlanInvalid(pilot_invocation_plans=tuple(mismatched_pair))

        cross_task = tuple(
            replace(
                child,
                allowed_write_policy_digest=first.allowed_write_policy_digest,
            )
            if child.run.task_id == other_task_id
            else child
            for child in self.pilot_plans
        )
        self.assertPlanInvalid(pilot_invocation_plans=cross_task)

    def test_builder_rejects_string_subclasses_for_pilot_policy_digests(self):
        for field_name in (
            "snapshot_receipt_digest",
            "allowed_write_policy_digest",
            "base_profile_digest",
            "root_capability_policy_digest",
        ):
            forged_children = list(self.pilot_plans)
            forged_children[0] = replace(
                forged_children[0],
                **{
                    field_name: _StringSubclass(
                        getattr(forged_children[0], field_name)
                    )
                },
            )
            with self.subTest(field_name=field_name):
                self.assertPlanInvalid(
                    pilot_invocation_plans=tuple(forged_children)
                )

    def test_builder_rejects_string_subclasses_across_plan_digest_fields(self):
        forged_template = replace(
            self.templates[0],
            base_profile_digest=_StringSubclass(
                self.templates[0].base_profile_digest
            ),
        )
        forged_pilot = replace(
            self.pilot_plans[0],
            allowed_write_policy_digest=_StringSubclass(
                self.pilot_plans[0].allowed_write_policy_digest
            ),
        )
        cases = (
            {
                "bundle_digest": _StringSubclass(
                    self.bundle_digest
                )
            },
            {
                "task_source_trust_receipt_digests": (
                    _StringSubclass(self.source_receipts[0]),
                    self.source_receipts[1],
                )
            },
            {
                "canary_templates": (
                    forged_template,
                    self.templates[1],
                )
            },
            {
                "pilot_invocation_plans": (
                    forged_pilot,
                )
                + self.pilot_plans[1:]
            },
        )
        for overrides in cases:
            with self.subTest(field=next(iter(overrides))):
                self.assertPlanInvalid(**overrides)

    def test_builder_accepts_opaque_nfc_policy_identifiers(self):
        value = _fixture_value()
        value["containment_policy_version"] = "Containment Policy α"
        value["invocation_policy"]["child_process_policy"] = (
            "Child Process Policy α"
        )
        value["invocation_policy"]["validator_policy"] = "Validator Policy α"
        experiment_input = load_experiment_input(canonical_bytes(value))
        original_input = self.experiment_input
        self.experiment_input = experiment_input
        try:
            templates, pilot_plans = self.make_children()
        finally:
            self.experiment_input = original_input

        plan = self.build(
            experiment_input=experiment_input,
            canary_templates=templates,
            pilot_invocation_plans=pilot_plans,
        )

        self.assertEqual(
            plan.plan_document["containment_policy_version"],
            "Containment Policy α",
        )

    def test_builder_rejects_unsorted_or_duplicate_receipts_and_forged_input(self):
        self.assertPlanInvalid(
            task_source_trust_receipt_digests=tuple(reversed(self.source_receipts))
        )
        self.assertPlanInvalid(
            task_source_trust_receipt_digests=(
                self.source_receipts[0],
                self.source_receipts[0],
            )
        )

        forged_value = _fixture_value()
        forged_value["budgets"]["total_calls"] = 11
        forged_bytes = canonical_bytes(forged_value)
        forged_input = CanonicalExperimentInput(
            canonical_bytes=forged_bytes,
            value=forged_value,
            input_digest=sha256_bytes(forged_bytes),
        )
        self.assertPlanInvalid(experiment_input=forged_input)


if __name__ == "__main__":
    unittest.main()
