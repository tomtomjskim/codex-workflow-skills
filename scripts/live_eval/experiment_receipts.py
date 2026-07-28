"""Canonical receipts and pure validation for the Phase A experiment."""

from collections.abc import Mapping as ABCMapping
from collections.abc import Sequence as ABCSequence
from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import hmac
import re
from typing import Mapping, Optional, Sequence, Tuple
import unicodedata

from scripts.live_eval.experiment_plan import (
    AnalysisContract,
    CanonicalExperimentInput,
    ExperimentDecision,
    ExperimentPlan,
    ExperimentPlanError,
    ValidatedConditionObservation,
    ValidatedMaskedReviewEvidence,
    ValidatedPairObservation,
    _make_validated_analysis_dataset,
    analyze_pairs,
    build_analysis_contract,
    build_experiment_plan,
    derive_static_evidence_digests,
    experiment_input_bytes,
    freeze_json_value,
    load_experiment_input,
    masked_review_seed_commitment_digest,
    sha256_bytes,
    thaw_json_value,
)
from scripts.live_eval.experiment_telemetry import (
    telemetry_summary_digest,
    telemetry_summary_document,
    telemetry_summary_from_document,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


class ExperimentReceiptError(ValueError):
    """Raised when a receipt or runtime history is invalid."""


_RECEIPT_ERROR = "experiment_receipt_invalid"
_HISTORY_ERROR = "experiment_runtime_history_invalid"
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OID_PATTERNS = {
    "sha1": re.compile(r"^[0-9a-f]{40}$"),
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
}
_SEED_REVEAL_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_MASKED_REVIEW_PRESENTATION_POLICY_DOCUMENT = {
    "document_type": "masked_review_presentation_policy",
    "schema_version": 1,
    "presentation_contract_version": (
        "content-addressed-masked-review-v1"
    ),
    "artifact_commitment": (
        "seed_hmac_sha256_over_content_addressed_canonical_manifest"
    ),
    "visible_sources": [
        "task_prompt_bytes",
        "assertion_context",
        "sanitized_diff_bytes",
        "masked_review_rubric",
    ],
    "forbidden_identity_fields": [
        "task_id",
        "condition",
        "profile",
        "pilot_terminal_receipt_digest",
    ],
    "reviewer_instructions": [
        "evaluate_against_requirements",
        "apply_exact_rubric",
        "do_not_infer_condition",
        "report_score_time_high_or_abstain",
    ],
}
MASKED_REVIEW_PRESENTATION_POLICY = freeze_json_value(
    _MASKED_REVIEW_PRESENTATION_POLICY_DOCUMENT
)
MASKED_REVIEW_PRESENTATION_POLICY_DIGEST = sha256_bytes(
    canonical_bytes(_MASKED_REVIEW_PRESENTATION_POLICY_DOCUMENT)
)

_STATIC_COMPONENT_TYPES = frozenset(
    {
        "task_source_trust",
        "task_snapshot",
        "task_selection",
        "task_corpus",
    }
)
_RUNTIME_TYPES = frozenset(
    {
        "runtime_containment",
        "canary_reservation",
        "canary_terminal",
        "canary_receipt",
        "pilot_reservation",
        "pilot_terminal",
        "masked_review_packet",
        "score_lock",
        "unmask",
        "decision",
        "experiment_stop",
    }
)

_SOURCE_TRUST_KEYS = frozenset(
    {
        "task_id",
        "provisioning_class",
        "operator_attested",
        "local_clone_policy",
        "object_format",
        "source_identity_before_digest",
        "object_topology_before_digest",
        "source_identity_after_digest",
        "object_topology_after_digest",
        "git_process_policy_digest",
        "inventory_file_count",
        "inventory_total_bytes",
    }
)
_SNAPSHOT_KEYS = frozenset(
    {
        "task_id",
        "object_format",
        "commit_oid",
        "tree_oid",
        "entry_digest",
        "materialized_tree_digest",
        "materializer_policy_version",
        "file_count",
        "total_bytes",
        "source_trust_receipt_digest",
    }
)
_SELECTION_KEYS = frozenset(
    {
        "candidate_set_digest",
        "selection_rule",
        "selection_seed_digest",
        "selected_task_ids",
        "pilot_schedule_digest",
    }
)
_CORPUS_KEYS = frozenset(
    {
        "candidate_set_digest",
        "selection_receipt_digest",
        "selected_snapshot_receipt_digests",
        "qualification_digest",
        "qualification_evidence_classification",
        "prompt_digests",
        "validator_digests",
        "assertion_digests",
        "reference_result_digest",
        "mutation_sensitivity_digest",
        "difficulty_assignment_digest",
    }
)
_PREFLIGHT_KEYS = frozenset(
    {
        "evidence_state",
        "live_backend_state",
        "global_agents_marker_state",
        "pilot_state",
        "qualification_evidence_classification",
        "model_calls",
        "bundle_digest",
        "current_profile_digest",
        "lean_profile_digest",
        "task_corpus_receipt_digest",
        "experiment_plan_digest",
        "canary_template_set_digest",
        "pilot_invocation_plan_set_digest",
        "materialization_result",
        "cleanup_state",
    }
)
_RUNTIME_CONTAINMENT_KEYS = frozenset(
    {
        "backend_identity_digest",
        "model_tool_capability_receipt_digest",
        "validator_capability_receipt_digest",
        "probe_set_digest",
        "result",
    }
)
_CANARY_RESERVATION_KEYS = frozenset(
    {
        "reservation_id",
        "profile",
        "base_profile_digest",
        "canary_template_digest",
    }
)
_CANARY_TERMINAL_KEYS = frozenset(
    {
        "reservation_id",
        "profile",
        "reservation_receipt_digest",
        "terminal_status",
        "telemetry_summary_digest",
        "response_digest",
    }
)
_CANARY_RECEIPT_KEYS = frozenset(
    {
        "reservation_id",
        "profile",
        "terminal_receipt_digest",
        "base_bundle_digest",
        "base_profile_digest",
        "canary_overlay_recipe_digest",
        "marker_digest",
        "marker_entropy_bits",
        "marker_occurrence_count",
        "marker_occurrence_receipt_digest",
        "derived_home_digest",
        "model_policy_digest",
        "containment_capability_digest",
    }
)
_PILOT_RESERVATION_KEYS = frozenset(
    {
        "reservation_id",
        "task_id",
        "condition",
        "snapshot_receipt_digest",
        "invocation_plan_digest",
    }
)
_PILOT_TERMINAL_KEYS = frozenset(
    {
        "reservation_id",
        "task_id",
        "condition",
        "reservation_receipt_digest",
        "terminal_status",
        "telemetry_summary",
        "telemetry_summary_digest",
        "wall_time_milliseconds",
        "machine_assertion_result",
        "sanitized_diff_digest",
        "allowed_write_inventory_digest",
        "absolute_safety_assertion_id",
        "absolute_safety_basis_digest",
    }
)
_MASKED_REVIEW_PACKET_KEYS = frozenset(
    {
        "condition_mapping_commitment_digest",
        "eligible_pilot_terminal_digests",
        "packet_digest",
        "randomized_order",
        "review_artifact_records",
        "leakage_scan_result",
        "review_evidence_classification",
        "rubric_digest",
        "reviewer_ids",
    }
)
_REVIEW_ARTIFACT_RECORD_KEYS = frozenset(
    {
        "neutral_id",
        "review_artifact_commitment_digest",
    }
)
_SCORE_LOCK_KEYS = frozenset(
    {
        "masked_packet_receipt_digest",
        "locked_score_records",
        "locked_score_records_digest",
        "review_findings_digest",
        "review_evidence_classification",
    }
)
_LOCKED_SCORE_RECORD_KEYS = frozenset(
    {
        "neutral_id",
        "correctness_score",
        "active_review_milliseconds",
        "confirmed_high",
    }
)
_UNMASK_KEYS = frozenset(
    {
        "score_lock_receipt_digest",
        "masked_review_seed_reveal",
        "condition_mapping_records",
        "condition_mapping_digest",
        "high_regression_basis_digest",
    }
)
_CONDITION_MAPPING_RECORD_KEYS = frozenset(
    {
        "neutral_id",
        "task_id",
        "condition",
        "pilot_terminal_receipt_digest",
    }
)
_DECISION_KEYS = frozenset(
    {
        "unmask_receipt_digest",
        "decision_calculation_digest",
        "outcome",
    }
)
_STOP_KEYS = frozenset(
    {
        "stop_stage",
        "reason_code",
        "consumed_canary_reservations",
        "consumed_pilot_reservations",
        "consumed_total_reservations",
        "unresolved_reservation_ids",
        "supporting_evidence_receipt_digests",
        "safety_basis_digest",
        "outcome",
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "crashed",
        "abandoned_after_recovery",
    }
)
_DECISION_OUTCOMES = frozenset(
    {
        "reject_for_safety",
        "advance_to_larger_study",
        "inconclusive",
    }
)
_STOP_STAGES = frozenset(
    {"canary", "pilot", "masked_review", "score_lock", "unmask", "decision"}
)
_STOP_REASONS = frozenset(
    {
        "operator_stop",
        "terminal_failed",
        "terminal_timed_out",
        "terminal_crashed",
        "abandoned_after_recovery",
        "reviewer_abstention",
        "absolute_lean_safety",
        "masked_high_regression",
    }
)
_PLAN_ADDED_KEYS = frozenset(
    {
        "bundle_digest",
        "call_allocation_digest",
        "canary_template_digests",
        "current_profile_digest",
        "input_digest",
        "lean_profile_digest",
        "masked_review_context_digest",
        "pilot_invocation_plan_digests",
        "pilot_schedule",
        "task_corpus_receipt_digest",
        "task_selection_receipt_digest",
        "task_source_trust_receipt_digests",
    }
)


def _raise_receipt_invalid() -> None:
    raise ExperimentReceiptError(_RECEIPT_ERROR)


def _raise_history_invalid() -> None:
    raise ExperimentReceiptError(_HISTORY_ERROR)


def _exact_projection_matches(value: object, expected: object) -> bool:
    if is_dataclass(expected) and not isinstance(expected, type):
        if type(value) is not type(expected):
            return False
        expected_fields = tuple(item.name for item in fields(expected))
        if set(vars(value)) != set(expected_fields):
            return False
        return all(
            _exact_projection_matches(
                getattr(value, name), getattr(expected, name)
            )
            for name in expected_fields
        )
    if isinstance(expected, ABCMapping):
        if not isinstance(value, ABCMapping):
            return False
        try:
            actual_mapping = dict(value)
            expected_mapping = dict(expected)
        except Exception:
            return False
        if (
            set(actual_mapping) != set(expected_mapping)
            or any(type(key) is not str for key in actual_mapping)
        ):
            return False
        return all(
            _exact_projection_matches(
                actual_mapping[key], expected_mapping[key]
            )
            for key in expected_mapping
        )
    if type(expected) is tuple:
        if type(value) is not tuple or len(value) != len(expected):
            return False
        return all(
            _exact_projection_matches(item, expected_item)
            for item, expected_item in zip(value, expected)
        )
    return type(value) is type(expected) and value == expected


def _snapshot_json_value(value: object) -> object:
    if value is None or type(value) in (bool, int, str):
        if type(value) is str:
            _require_nfc_text(value, allow_empty=True)
        return value
    if isinstance(value, ABCMapping):
        snapshot = dict(value)
        result = {}
        for key, item in snapshot.items():
            if type(key) is not str:
                _raise_receipt_invalid()
            _require_nfc_text(key, allow_empty=False)
            result[key] = _snapshot_json_value(item)
        return result
    if isinstance(value, ABCSequence):
        if isinstance(value, (str, bytes, bytearray)):
            _raise_receipt_invalid()
        return [_snapshot_json_value(item) for item in tuple(value)]
    _raise_receipt_invalid()


def _require_mapping(
    value: object, exact_keys: frozenset
) -> ABCMapping:
    if not isinstance(value, ABCMapping):
        _raise_receipt_invalid()
    if set(value) != set(exact_keys):
        _raise_receipt_invalid()
    return value


def _require_sequence(
    value: object, *, length: Optional[int] = None
) -> Tuple[object, ...]:
    if (
        not isinstance(value, ABCSequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        _raise_receipt_invalid()
    items = tuple(value)
    if length is not None and len(items) != length:
        _raise_receipt_invalid()
    return items


def _require_nfc_text(value: object, *, allow_empty: bool) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _raise_receipt_invalid()
    try:
        value.encode("utf-8")
    except UnicodeError:
        _raise_receipt_invalid()
    if not unicodedata.is_normalized("NFC", value):
        _raise_receipt_invalid()
    return value


def _require_opaque_id(value: object) -> str:
    return _require_nfc_text(value, allow_empty=False)


def _require_task_id(value: object) -> str:
    text = _require_opaque_id(value)
    if _IDENTIFIER_PATTERN.fullmatch(text) is None:
        _raise_receipt_invalid()
    return text


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        _raise_receipt_invalid()
    return value


def _require_optional_digest(value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_digest(value)


def _require_non_negative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        _raise_receipt_invalid()
    return value


def _require_exact(value: object, expected: object) -> None:
    if type(value) is not type(expected) or value != expected:
        _raise_receipt_invalid()


def _require_digest_sequence(
    value: object, *, length: int, unique: bool = False
) -> Tuple[str, ...]:
    checked = tuple(
        _require_digest(item)
        for item in _require_sequence(value, length=length)
    )
    if unique and len(set(checked)) != len(checked):
        _raise_receipt_invalid()
    return checked


def _require_task_sequence(value: object) -> Tuple[str, ...]:
    checked = tuple(
        _require_task_id(item)
        for item in _require_sequence(value, length=4)
    )
    if len(set(checked)) != len(checked):
        _raise_receipt_invalid()
    return checked


def _validate_source_trust_payload(payload: object) -> None:
    checked = _require_mapping(payload, _SOURCE_TRUST_KEYS)
    _require_task_id(checked["task_id"])
    _require_exact(
        checked["provisioning_class"],
        "operator_owned_trusted_git_local_clone",
    )
    _require_exact(checked["operator_attested"], True)
    _require_exact(
        checked["local_clone_policy"],
        "remote_or_no_local_or_no_hardlinks",
    )
    if checked["object_format"] not in _OID_PATTERNS:
        _raise_receipt_invalid()
    for name in (
        "source_identity_before_digest",
        "object_topology_before_digest",
        "source_identity_after_digest",
        "object_topology_after_digest",
        "git_process_policy_digest",
    ):
        _require_digest(checked[name])
    if (
        checked["source_identity_before_digest"]
        != checked["source_identity_after_digest"]
        or checked["object_topology_before_digest"]
        != checked["object_topology_after_digest"]
    ):
        _raise_receipt_invalid()
    _require_non_negative_integer(checked["inventory_file_count"])
    _require_non_negative_integer(checked["inventory_total_bytes"])


def _validate_snapshot_payload(payload: object) -> None:
    checked = _require_mapping(payload, _SNAPSHOT_KEYS)
    _require_task_id(checked["task_id"])
    object_format = checked["object_format"]
    if type(object_format) is not str or object_format not in _OID_PATTERNS:
        _raise_receipt_invalid()
    for name in ("commit_oid", "tree_oid"):
        value = checked[name]
        if (
            type(value) is not str
            or _OID_PATTERNS[object_format].fullmatch(value) is None
        ):
            _raise_receipt_invalid()
    for name in (
        "entry_digest",
        "materialized_tree_digest",
        "source_trust_receipt_digest",
    ):
        _require_digest(checked[name])
    _require_exact(
        checked["materializer_policy_version"],
        "task-object-materializer-v1",
    )
    _require_non_negative_integer(checked["file_count"])
    _require_non_negative_integer(checked["total_bytes"])


def _validate_selection_payload(payload: object) -> None:
    checked = _require_mapping(payload, _SELECTION_KEYS)
    _require_digest(checked["candidate_set_digest"])
    _require_exact(checked["selection_rule"], "sha256-rank-paired-v1")
    _require_digest(checked["selection_seed_digest"])
    _require_task_sequence(checked["selected_task_ids"])
    _require_digest(checked["pilot_schedule_digest"])


def _validate_corpus_payload(payload: object) -> None:
    checked = _require_mapping(payload, _CORPUS_KEYS)
    for name in (
        "candidate_set_digest",
        "selection_receipt_digest",
        "qualification_digest",
        "reference_result_digest",
        "mutation_sensitivity_digest",
        "difficulty_assignment_digest",
    ):
        _require_digest(checked[name])
    _require_digest_sequence(
        checked["selected_snapshot_receipt_digests"],
        length=4,
        unique=True,
    )
    _require_exact(
        checked["qualification_evidence_classification"],
        "operator_attested_static",
    )
    for name in ("prompt_digests", "validator_digests", "assertion_digests"):
        _require_digest_sequence(checked[name], length=4)


def _validate_preflight_payload(payload: object) -> None:
    checked = _require_mapping(payload, _PREFLIGHT_KEYS)
    for name, expected in (
        ("evidence_state", "static_only"),
        ("live_backend_state", "live_backend_not_implemented"),
        ("global_agents_marker_state", "global_agents_marker_not_run"),
        ("pilot_state", "pilot_not_run"),
        (
            "qualification_evidence_classification",
            "operator_attested_static",
        ),
        ("model_calls", 0),
        ("materialization_result", "verified"),
        ("cleanup_state", "removed"),
    ):
        _require_exact(checked[name], expected)
    for name in (
        "bundle_digest",
        "current_profile_digest",
        "lean_profile_digest",
        "task_corpus_receipt_digest",
        "experiment_plan_digest",
        "canary_template_set_digest",
        "pilot_invocation_plan_set_digest",
    ):
        _require_digest(checked[name])


def _require_condition(value: object) -> str:
    checked = _require_opaque_id(value)
    if checked not in ("current", "lean"):
        _raise_receipt_invalid()
    return checked


def _require_optional_opaque_id(value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_opaque_id(value)


def _require_opaque_sequence(
    value: object,
    *,
    length: Optional[int] = None,
    nonempty: bool = False,
    sorted_unique: bool = False
) -> Tuple[str, ...]:
    items = tuple(
        _require_opaque_id(item)
        for item in _require_sequence(value, length=length)
    )
    if nonempty and not items:
        _raise_receipt_invalid()
    if len(set(items)) != len(items):
        _raise_receipt_invalid()
    if sorted_unique and items != tuple(
        sorted(items, key=lambda item: item.encode("utf-8"))
    ):
        _raise_receipt_invalid()
    return items


def _validate_runtime_containment_payload(payload: object) -> None:
    checked = _require_mapping(payload, _RUNTIME_CONTAINMENT_KEYS)
    for name in (
        "backend_identity_digest",
        "model_tool_capability_receipt_digest",
        "validator_capability_receipt_digest",
        "probe_set_digest",
    ):
        _require_digest(checked[name])
    _require_exact(checked["result"], "pass")


def _validate_canary_reservation_payload(payload: object) -> None:
    checked = _require_mapping(payload, _CANARY_RESERVATION_KEYS)
    _require_opaque_id(checked["reservation_id"])
    profile = _require_opaque_id(checked["profile"])
    if profile not in ("current", "lean"):
        _raise_receipt_invalid()
    _require_digest(checked["base_profile_digest"])
    _require_digest(checked["canary_template_digest"])


def _validate_canary_terminal_payload(payload: object) -> None:
    checked = _require_mapping(payload, _CANARY_TERMINAL_KEYS)
    _require_opaque_id(checked["reservation_id"])
    profile = _require_opaque_id(checked["profile"])
    if profile not in ("current", "lean"):
        _raise_receipt_invalid()
    _require_digest(checked["reservation_receipt_digest"])
    status = _require_opaque_id(checked["terminal_status"])
    if status not in _TERMINAL_STATUSES:
        _raise_receipt_invalid()
    if status == "completed":
        _require_digest(checked["telemetry_summary_digest"])
        _require_digest(checked["response_digest"])
    elif (
        checked["telemetry_summary_digest"] is not None
        or checked["response_digest"] is not None
    ):
        _raise_receipt_invalid()


def _validate_canary_receipt_payload(payload: object) -> None:
    checked = _require_mapping(payload, _CANARY_RECEIPT_KEYS)
    _require_opaque_id(checked["reservation_id"])
    profile = _require_opaque_id(checked["profile"])
    if profile not in ("current", "lean"):
        _raise_receipt_invalid()
    for name in (
        "terminal_receipt_digest",
        "base_bundle_digest",
        "base_profile_digest",
        "canary_overlay_recipe_digest",
        "marker_digest",
        "marker_occurrence_receipt_digest",
        "derived_home_digest",
        "model_policy_digest",
        "containment_capability_digest",
    ):
        _require_digest(checked[name])
    _require_exact(checked["marker_entropy_bits"], 128)
    _require_exact(checked["marker_occurrence_count"], 1)


def _validate_pilot_reservation_payload(payload: object) -> None:
    checked = _require_mapping(payload, _PILOT_RESERVATION_KEYS)
    _require_opaque_id(checked["reservation_id"])
    _require_task_id(checked["task_id"])
    _require_condition(checked["condition"])
    _require_digest(checked["snapshot_receipt_digest"])
    _require_digest(checked["invocation_plan_digest"])


def _validate_pilot_terminal_payload(payload: object) -> None:
    checked = _require_mapping(payload, _PILOT_TERMINAL_KEYS)
    _require_opaque_id(checked["reservation_id"])
    _require_task_id(checked["task_id"])
    _require_condition(checked["condition"])
    _require_digest(checked["reservation_receipt_digest"])
    status = _require_opaque_id(checked["terminal_status"])
    if status not in _TERMINAL_STATUSES:
        _raise_receipt_invalid()
    result_fields = (
        "telemetry_summary",
        "telemetry_summary_digest",
        "wall_time_milliseconds",
        "machine_assertion_result",
        "sanitized_diff_digest",
        "allowed_write_inventory_digest",
        "absolute_safety_assertion_id",
        "absolute_safety_basis_digest",
    )
    if status != "completed":
        if any(checked[name] is not None for name in result_fields):
            _raise_receipt_invalid()
        return
    if not isinstance(checked["telemetry_summary"], ABCMapping):
        _raise_receipt_invalid()
    _require_digest(checked["telemetry_summary_digest"])
    _require_non_negative_integer(checked["wall_time_milliseconds"])
    if checked["machine_assertion_result"] not in ("pass", "fail"):
        _raise_receipt_invalid()
    if type(checked["machine_assertion_result"]) is not str:
        _raise_receipt_invalid()
    _require_digest(checked["sanitized_diff_digest"])
    _require_digest(checked["allowed_write_inventory_digest"])
    assertion_id = _require_optional_opaque_id(
        checked["absolute_safety_assertion_id"]
    )
    safety_basis = _require_optional_digest(
        checked["absolute_safety_basis_digest"]
    )
    if (assertion_id is None) is not (safety_basis is None):
        _raise_receipt_invalid()


def _validate_masked_review_packet_payload(payload: object) -> None:
    checked = _require_mapping(payload, _MASKED_REVIEW_PACKET_KEYS)
    _require_digest(checked["condition_mapping_commitment_digest"])
    _require_digest_sequence(
        checked["eligible_pilot_terminal_digests"],
        length=8,
        unique=True,
    )
    _require_digest(checked["packet_digest"])
    randomized_order = _require_opaque_sequence(
        checked["randomized_order"], length=8
    )
    if len(set(randomized_order)) != len(randomized_order):
        _raise_receipt_invalid()
    artifact_records = _require_sequence(
        checked["review_artifact_records"], length=8
    )
    artifact_neutral_ids = []
    for record in artifact_records:
        item = _require_mapping(
            record, _REVIEW_ARTIFACT_RECORD_KEYS
        )
        artifact_neutral_ids.append(
            _require_opaque_id(item["neutral_id"])
        )
        _require_digest(item["review_artifact_commitment_digest"])
    if tuple(artifact_neutral_ids) != tuple(randomized_order):
        _raise_receipt_invalid()
    _require_exact(checked["leakage_scan_result"], "pass")
    _require_exact(
        checked["review_evidence_classification"],
        "operator_attested_aggregated_review",
    )
    _require_digest(checked["rubric_digest"])
    reviewers = _require_opaque_sequence(
        checked["reviewer_ids"],
        nonempty=True,
        sorted_unique=True,
    )
    if len(reviewers) < 2:
        _raise_receipt_invalid()


def _validate_score_lock_payload(payload: object) -> None:
    checked = _require_mapping(payload, _SCORE_LOCK_KEYS)
    _require_digest(checked["masked_packet_receipt_digest"])
    records = _require_sequence(
        checked["locked_score_records"], length=8
    )
    neutral_ids = []
    for record in records:
        item = _require_mapping(record, _LOCKED_SCORE_RECORD_KEYS)
        neutral_ids.append(_require_opaque_id(item["neutral_id"]))
        score = _require_non_negative_integer(
            item["correctness_score"]
        )
        if score > 100:
            _raise_receipt_invalid()
        _require_non_negative_integer(
            item["active_review_milliseconds"]
        )
        if type(item["confirmed_high"]) is not bool:
            _raise_receipt_invalid()
    if len(set(neutral_ids)) != len(neutral_ids):
        _raise_receipt_invalid()
    _require_digest(checked["locked_score_records_digest"])
    _require_digest(checked["review_findings_digest"])
    _require_exact(
        checked["review_evidence_classification"],
        "operator_attested_aggregated_review",
    )


def _validate_unmask_payload(payload: object) -> None:
    checked = _require_mapping(payload, _UNMASK_KEYS)
    _require_digest(checked["score_lock_receipt_digest"])
    if (
        type(checked["masked_review_seed_reveal"]) is not str
        or _SEED_REVEAL_PATTERN.fullmatch(
            checked["masked_review_seed_reveal"]
        )
        is None
    ):
        _raise_receipt_invalid()
    records = _require_sequence(
        checked["condition_mapping_records"], length=8
    )
    neutral_ids = []
    terminal_digests = []
    for record in records:
        item = _require_mapping(
            record, _CONDITION_MAPPING_RECORD_KEYS
        )
        neutral_ids.append(_require_opaque_id(item["neutral_id"]))
        _require_task_id(item["task_id"])
        _require_condition(item["condition"])
        terminal_digests.append(
            _require_digest(item["pilot_terminal_receipt_digest"])
        )
    if (
        len(set(neutral_ids)) != len(neutral_ids)
        or len(set(terminal_digests)) != len(terminal_digests)
    ):
        _raise_receipt_invalid()
    _require_digest(checked["condition_mapping_digest"])
    _require_optional_digest(checked["high_regression_basis_digest"])


def _validate_decision_payload(payload: object) -> None:
    checked = _require_mapping(payload, _DECISION_KEYS)
    _require_digest(checked["unmask_receipt_digest"])
    _require_digest(checked["decision_calculation_digest"])
    outcome = _require_opaque_id(checked["outcome"])
    if outcome not in _DECISION_OUTCOMES:
        _raise_receipt_invalid()


def _validate_stop_payload(payload: object) -> None:
    checked = _require_mapping(payload, _STOP_KEYS)
    stage = _require_opaque_id(checked["stop_stage"])
    if stage not in _STOP_STAGES:
        _raise_receipt_invalid()
    reason = _require_opaque_id(checked["reason_code"])
    if reason not in _STOP_REASONS:
        _raise_receipt_invalid()
    for name in (
        "consumed_canary_reservations",
        "consumed_pilot_reservations",
        "consumed_total_reservations",
    ):
        _require_non_negative_integer(checked[name])
    _require_opaque_sequence(checked["unresolved_reservation_ids"])
    evidence = tuple(
        _require_digest(item)
        for item in _require_sequence(
            checked["supporting_evidence_receipt_digests"]
        )
    )
    if not evidence or len(set(evidence)) != len(evidence):
        _raise_receipt_invalid()
    _require_optional_digest(checked["safety_basis_digest"])
    outcome = _require_opaque_id(checked["outcome"])
    if outcome not in _DECISION_OUTCOMES:
        _raise_receipt_invalid()


_PAYLOAD_VALIDATORS = {
    "task_source_trust": _validate_source_trust_payload,
    "task_snapshot": _validate_snapshot_payload,
    "task_selection": _validate_selection_payload,
    "task_corpus": _validate_corpus_payload,
    "preflight": _validate_preflight_payload,
    "runtime_containment": _validate_runtime_containment_payload,
    "canary_reservation": _validate_canary_reservation_payload,
    "canary_terminal": _validate_canary_terminal_payload,
    "canary_receipt": _validate_canary_receipt_payload,
    "pilot_reservation": _validate_pilot_reservation_payload,
    "pilot_terminal": _validate_pilot_terminal_payload,
    "masked_review_packet": _validate_masked_review_packet_payload,
    "score_lock": _validate_score_lock_payload,
    "unmask": _validate_unmask_payload,
    "decision": _validate_decision_payload,
    "experiment_stop": _validate_stop_payload,
}


def _validate_receipt_payload(receipt_type: str, payload: object) -> None:
    validator = _PAYLOAD_VALIDATORS.get(receipt_type)
    if validator is None:
        _raise_receipt_invalid()
    validator(payload)


def _validate_envelope(
    receipt_type: object,
    input_digest: object,
    plan_digest: object,
    previous_record_hash: object,
    payload: object,
) -> None:
    receipt_name = _require_opaque_id(receipt_type)
    _require_digest(input_digest)
    if receipt_name in _STATIC_COMPONENT_TYPES:
        if plan_digest is not None or previous_record_hash is not None:
            _raise_receipt_invalid()
    elif receipt_name == "preflight":
        checked_plan = _require_digest(plan_digest)
        if previous_record_hash is not None:
            _raise_receipt_invalid()
        if (
            not isinstance(payload, ABCMapping)
            or payload.get("experiment_plan_digest") != checked_plan
        ):
            _raise_receipt_invalid()
    elif receipt_name in _RUNTIME_TYPES:
        _require_digest(plan_digest)
        _require_digest(previous_record_hash)
    else:
        _raise_receipt_invalid()


def _receipt_document(
    receipt_type: str,
    input_digest: str,
    plan_digest: Optional[str],
    previous_record_hash: Optional[str],
    payload: object,
) -> Mapping[str, object]:
    return {
        "schema_version": 1,
        "receipt_type": receipt_type,
        "input_digest": input_digest,
        "plan_digest": plan_digest,
        "previous_record_hash": previous_record_hash,
        "payload": thaw_json_value(payload),
    }


@dataclass(frozen=True)
class CanonicalReceipt:
    receipt_type: str
    input_digest: str
    plan_digest: Optional[str]
    previous_record_hash: Optional[str]
    payload: Mapping[str, object]
    canonical_bytes: bytes = field(repr=False)
    receipt_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json_value(self.payload))
        object.__setattr__(
            self, "canonical_bytes", bytes(self.canonical_bytes)
        )


@dataclass(frozen=True)
class RuntimeState:
    next_expected: Tuple[str, ...]
    canary_reservations: int
    pilot_reservations: int
    total_reservations: int
    unresolved_reservation_ids: Tuple[str, ...]
    terminal: Optional[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "next_expected", tuple(self.next_expected))
        object.__setattr__(
            self,
            "unresolved_reservation_ids",
            tuple(self.unresolved_reservation_ids),
        )


@dataclass
class _ReplayState:
    preflight_receipt: CanonicalReceipt
    last_receipt: CanonicalReceipt
    records: list
    seen_receipt_digests: set
    seen_reservation_ids: set
    pending_reservation: Optional[CanonicalReceipt] = None
    awaiting_canary_terminal: Optional[CanonicalReceipt] = None
    containment_receipt: Optional[CanonicalReceipt] = None
    canary_receipts: list = field(default_factory=list)
    pilot_terminals: list = field(default_factory=list)
    seen_marker_digests: set = field(default_factory=set)
    packet_receipt: Optional[CanonicalReceipt] = None
    score_lock_receipt: Optional[CanonicalReceipt] = None
    unmask_receipt: Optional[CanonicalReceipt] = None
    forced_stop_status: Optional[str] = None
    safety_sources: dict = field(default_factory=dict)
    canary_reservations: int = 0
    pilot_reservations: int = 0
    terminal: Optional[str] = None


def make_receipt(
    receipt_type: str,
    input_digest: str,
    plan_digest: Optional[str],
    previous_record_hash: Optional[str],
    payload: Mapping[str, object],
) -> CanonicalReceipt:
    """Build one detached canonical receipt with an exact payload schema."""
    try:
        snapshot = _snapshot_json_value(payload)
        if not isinstance(snapshot, dict):
            _raise_receipt_invalid()
        _validate_receipt_payload(receipt_type, snapshot)
        _validate_envelope(
            receipt_type,
            input_digest,
            plan_digest,
            previous_record_hash,
            snapshot,
        )
        document = _receipt_document(
            receipt_type,
            input_digest,
            plan_digest,
            previous_record_hash,
            snapshot,
        )
        encoded = canonical_bytes(document)
        return CanonicalReceipt(
            receipt_type=receipt_type,
            input_digest=input_digest,
            plan_digest=plan_digest,
            previous_record_hash=previous_record_hash,
            payload=snapshot,
            canonical_bytes=encoded,
            receipt_digest=sha256_bytes(encoded),
        )
    except Exception:
        raise ExperimentReceiptError(_RECEIPT_ERROR) from None


def _validate_canonical_receipt(
    receipt: object, *, expected_type: Optional[str] = None
) -> Mapping[str, object]:
    if type(receipt) is not CanonicalReceipt:
        _raise_receipt_invalid()
    if set(vars(receipt)) != {
        item.name for item in fields(CanonicalReceipt)
    }:
        _raise_receipt_invalid()
    payload = _snapshot_json_value(receipt.payload)
    if not isinstance(payload, dict):
        _raise_receipt_invalid()
    if expected_type is not None and receipt.receipt_type != expected_type:
        _raise_receipt_invalid()
    _validate_receipt_payload(receipt.receipt_type, payload)
    _validate_envelope(
        receipt.receipt_type,
        receipt.input_digest,
        receipt.plan_digest,
        receipt.previous_record_hash,
        payload,
    )
    _require_digest(receipt.receipt_digest)
    if type(receipt.canonical_bytes) is not bytes:
        _raise_receipt_invalid()
    document = _receipt_document(
        receipt.receipt_type,
        receipt.input_digest,
        receipt.plan_digest,
        receipt.previous_record_hash,
        payload,
    )
    encoded = canonical_bytes(document)
    if encoded != receipt.canonical_bytes:
        _raise_receipt_invalid()
    if sha256_bytes(encoded) != receipt.receipt_digest:
        _raise_receipt_invalid()
    return payload


def _receipt_sequence(
    value: object, *, expected_type: str, length: int
) -> Tuple[CanonicalReceipt, ...]:
    items = _require_sequence(value, length=length)
    for item in items:
        _validate_canonical_receipt(item, expected_type=expected_type)
    return items


def _validate_static_receipt_graph(
    experiment_input: CanonicalExperimentInput,
    source_trust_receipts: Sequence[CanonicalReceipt],
    snapshot_receipts: Sequence[CanonicalReceipt],
    selection_receipt: CanonicalReceipt,
    corpus_receipt: CanonicalReceipt,
) -> None:
    if type(experiment_input) is not CanonicalExperimentInput:
        _raise_receipt_invalid()
    authoritative = experiment_input_bytes(experiment_input)
    verified_input = load_experiment_input(authoritative)
    if not _exact_projection_matches(experiment_input, verified_input):
        _raise_receipt_invalid()
    static_digests = derive_static_evidence_digests(experiment_input)

    sources = _receipt_sequence(
        source_trust_receipts,
        expected_type="task_source_trust",
        length=4,
    )
    snapshots = _receipt_sequence(
        snapshot_receipts,
        expected_type="task_snapshot",
        length=4,
    )
    selection_payload = _validate_canonical_receipt(
        selection_receipt, expected_type="task_selection"
    )
    corpus_payload = _validate_canonical_receipt(
        corpus_receipt, expected_type="task_corpus"
    )
    all_receipts = sources + snapshots + (
        selection_receipt,
        corpus_receipt,
    )
    if any(
        receipt.input_digest != verified_input.input_digest
        for receipt in all_receipts
    ):
        _raise_receipt_invalid()
    if len({receipt.receipt_digest for receipt in sources}) != 4:
        _raise_receipt_invalid()
    if len({receipt.receipt_digest for receipt in snapshots}) != 4:
        _raise_receipt_invalid()

    candidates = tuple(verified_input.value["candidates"])
    task_ids = tuple(candidate["task_id"] for candidate in candidates)
    for candidate, source, snapshot in zip(
        candidates, sources, snapshots
    ):
        source_payload = source.payload
        snapshot_payload = snapshot.payload
        expected_format = (
            "sha1" if len(candidate["commit_oid"]) == 40 else "sha256"
        )
        if (
            source_payload["task_id"] != candidate["task_id"]
            or source_payload["provisioning_class"]
            != candidate["source_provisioning_class"]
            or source_payload["operator_attested"]
            is not candidate["operator_attested"]
            or source_payload["local_clone_policy"]
            != candidate["local_clone_policy"]
            or source_payload["object_format"] != expected_format
            or snapshot_payload["task_id"] != candidate["task_id"]
            or snapshot_payload["commit_oid"] != candidate["commit_oid"]
            or snapshot_payload["object_format"] != expected_format
            or snapshot_payload["source_trust_receipt_digest"]
            != source.receipt_digest
        ):
            _raise_receipt_invalid()

    if (
        tuple(selection_payload["selected_task_ids"]) != task_ids
        or selection_payload["selection_rule"]
        != verified_input.value["selection_rule"]
        or selection_payload["candidate_set_digest"]
        != static_digests.candidate_set_digest
        or selection_payload["selection_seed_digest"]
        != static_digests.selection_seed_digest
        or selection_payload["pilot_schedule_digest"]
        != static_digests.pilot_schedule_digest
        or corpus_payload["candidate_set_digest"]
        != selection_payload["candidate_set_digest"]
        or corpus_payload["candidate_set_digest"]
        != static_digests.candidate_set_digest
        or corpus_payload["selection_receipt_digest"]
        != selection_receipt.receipt_digest
        or tuple(
            corpus_payload["selected_snapshot_receipt_digests"]
        )
        != tuple(receipt.receipt_digest for receipt in snapshots)
        or tuple(corpus_payload["prompt_digests"])
        != tuple(candidate["prompt_digest"] for candidate in candidates)
        or tuple(corpus_payload["validator_digests"])
        != tuple(candidate["validator_digest"] for candidate in candidates)
        or tuple(corpus_payload["assertion_digests"])
        != tuple(candidate["assertion_digest"] for candidate in candidates)
        or corpus_payload["qualification_digest"]
        != static_digests.qualification_digest
        or corpus_payload["reference_result_digest"]
        != static_digests.reference_result_digest
        or corpus_payload["mutation_sensitivity_digest"]
        != static_digests.mutation_sensitivity_digest
        or corpus_payload["difficulty_assignment_digest"]
        != static_digests.difficulty_assignment_digest
    ):
        _raise_receipt_invalid()


def validate_static_receipt_graph(
    experiment_input: CanonicalExperimentInput,
    source_trust_receipts: Sequence[CanonicalReceipt],
    snapshot_receipts: Sequence[CanonicalReceipt],
    selection_receipt: CanonicalReceipt,
    corpus_receipt: CanonicalReceipt,
) -> None:
    """Validate one candidate-ordered static receipt authority graph."""
    try:
        _validate_static_receipt_graph(
            experiment_input,
            source_trust_receipts,
            snapshot_receipts,
            selection_receipt,
            corpus_receipt,
        )
    except Exception:
        raise ExperimentReceiptError(_RECEIPT_ERROR) from None


def _validated_runtime_plan(plan: object) -> ExperimentPlan:
    if type(plan) is not ExperimentPlan:
        _raise_history_invalid()
    if set(vars(plan)) != {
        "input_digest",
        "plan_document",
        "canonical_bytes",
        "plan_digest",
        "pilot_schedule",
        "canary_templates",
        "pilot_invocation_plans",
    }:
        _raise_history_invalid()
    document = thaw_json_value(plan.plan_document)
    if not isinstance(document, dict):
        _raise_history_invalid()
    input_document = {
        key: value
        for key, value in document.items()
        if key not in _PLAN_ADDED_KEYS
    }
    experiment_input = load_experiment_input(canonical_bytes(input_document))
    rebuilt = build_experiment_plan(
        experiment_input,
        bundle_digest=document["bundle_digest"],
        current_profile_digest=document["current_profile_digest"],
        lean_profile_digest=document["lean_profile_digest"],
        task_source_trust_receipt_digests=document[
            "task_source_trust_receipt_digests"
        ],
        task_selection_receipt_digest=document[
            "task_selection_receipt_digest"
        ],
        task_corpus_receipt_digest=document[
            "task_corpus_receipt_digest"
        ],
        canary_templates=plan.canary_templates,
        pilot_invocation_plans=plan.pilot_invocation_plans,
    )
    if not _exact_projection_matches(plan, rebuilt):
        _raise_history_invalid()
    return plan


def _validated_preflight(
    plan: ExperimentPlan, preflight: object
) -> CanonicalReceipt:
    payload = _validate_canonical_receipt(
        preflight, expected_type="preflight"
    )
    if (
        preflight.input_digest != plan.input_digest
        or preflight.plan_digest != plan.plan_digest
    ):
        _raise_history_invalid()
    expected_canary_set = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "canary_template_set",
                "schema_version": 1,
                "digests": list(
                    plan.plan_document["canary_template_digests"]
                ),
            }
        )
    )
    expected_pilot_set = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "pilot_invocation_plan_set",
                "schema_version": 1,
                "digests": list(
                    plan.plan_document["pilot_invocation_plan_digests"]
                ),
            }
        )
    )
    expected = {
        "bundle_digest": plan.plan_document["bundle_digest"],
        "current_profile_digest": plan.plan_document[
            "current_profile_digest"
        ],
        "lean_profile_digest": plan.plan_document["lean_profile_digest"],
        "task_corpus_receipt_digest": plan.plan_document[
            "task_corpus_receipt_digest"
        ],
        "experiment_plan_digest": plan.plan_digest,
        "canary_template_set_digest": expected_canary_set,
        "pilot_invocation_plan_set_digest": expected_pilot_set,
    }
    if any(payload[name] != value for name, value in expected.items()):
        _raise_history_invalid()
    return preflight


def _expected_model_policy_digest(
    plan: ExperimentPlan, template: object
) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "document_type": "canary_model_policy",
                "schema_version": 1,
                "model": thaw_json_value(plan.plan_document["model"]),
                "argv_template_digest": template.argv_template_digest,
                "environment_policy_digest": (
                    template.environment_policy_digest
                ),
            }
        )
    )


def _expected_rubric_digest(plan: ExperimentPlan) -> str:
    return plan.plan_document["masked_review_rubric_digest"]


def _nested_records_digest(
    document_type: str, records: object
) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "document_type": document_type,
                "schema_version": 1,
                "records": thaw_json_value(records),
            }
        )
    )


def _packet_manifest_digest(payload: Mapping[str, object]) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "document_type": "masked_review_packet_manifest",
                "schema_version": 2,
                "eligible_pilot_terminal_digests": thaw_json_value(
                    payload["eligible_pilot_terminal_digests"]
                ),
                "condition_mapping_commitment_digest": payload[
                    "condition_mapping_commitment_digest"
                ],
                "randomized_order": thaw_json_value(
                    payload["randomized_order"]
                ),
                "review_artifact_records": thaw_json_value(
                    payload["review_artifact_records"]
                ),
                "leakage_scan_result": payload["leakage_scan_result"],
                "rubric_digest": payload["rubric_digest"],
                "reviewer_ids": thaw_json_value(
                    payload["reviewer_ids"]
                ),
                "review_evidence_classification": payload[
                    "review_evidence_classification"
                ],
            }
        )
    )


def _masked_review_hmac(seed: bytes, document: object) -> bytes:
    return hmac.new(
        seed, canonical_bytes(document), hashlib.sha256
    ).digest()


def _review_artifact_manifest_document(
    plan: ExperimentPlan,
    neutral_id: str,
    run: object,
    terminal: CanonicalReceipt,
) -> Mapping[str, object]:
    candidate = _candidate_for_task(plan, run.task_id)
    return {
        "document_type": "masked_review_artifact_manifest",
        "schema_version": 1,
        "masked_review_context_digest": plan.plan_document[
            "masked_review_context_digest"
        ],
        "neutral_id": neutral_id,
        "prompt_digest": candidate["prompt_digest"],
        "assertion_digest": candidate["assertion_digest"],
        "absolute_safety_assertion_ids": list(
            candidate["absolute_safety_assertion_ids"]
        ),
        "sanitized_diff_digest": terminal.payload[
            "sanitized_diff_digest"
        ],
        "rubric_digest": plan.plan_document[
            "masked_review_rubric_digest"
        ],
        "presentation_contract_version": (
            "content-addressed-masked-review-v1"
        ),
        "presentation_policy_digest": (
            MASKED_REVIEW_PRESENTATION_POLICY_DIGEST
        ),
    }


def _review_artifact_commitment_digest(
    seed: bytes,
    plan: ExperimentPlan,
    neutral_id: str,
    run: object,
    terminal: CanonicalReceipt,
) -> str:
    manifest = dict(
        _review_artifact_manifest_document(
            plan, neutral_id, run, terminal
        )
    )
    manifest["document_type"] = "masked_review_artifact_commitment"
    return "sha256:" + _masked_review_hmac(seed, manifest).hex()


def _expected_masked_binding(
    plan: ExperimentPlan,
    terminals: Sequence[CanonicalReceipt],
    seed_reveal: str,
):
    seed = bytes.fromhex(seed_reveal)
    ranked = []
    neutral_ids = set()
    for schedule_ordinal, (run, terminal) in enumerate(
        zip(plan.pilot_schedule, terminals), 1
    ):
        common = {
            "schema_version": 1,
            "masked_review_context_digest": plan.plan_document[
                "masked_review_context_digest"
            ],
            "schedule_ordinal": schedule_ordinal,
        }
        rank = _masked_review_hmac(
            seed,
            dict(
                common,
                document_type="masked_review_order_rank",
            ),
        )
        neutral_id = "neutral-" + _masked_review_hmac(
            seed,
            dict(
                common,
                document_type="masked_review_neutral_id",
            ),
        ).hex()[:32]
        if neutral_id in neutral_ids:
            _raise_history_invalid()
        neutral_ids.add(neutral_id)
        ranked.append(
            (rank, schedule_ordinal, neutral_id, run, terminal)
        )
    ranked.sort(key=lambda item: (item[0], item[1]))
    records = tuple(
        {
            "neutral_id": neutral_id,
            "task_id": run.task_id,
            "condition": run.condition,
            "pilot_terminal_receipt_digest": terminal.receipt_digest,
        }
        for _, _, neutral_id, run, terminal in ranked
    )
    artifact_records = tuple(
        {
            "neutral_id": neutral_id,
            "review_artifact_commitment_digest": (
                _review_artifact_commitment_digest(
                    seed, plan, neutral_id, run, terminal
                )
            ),
        }
        for _, _, neutral_id, run, terminal in ranked
    )
    mapping_salt = _masked_review_hmac(
        seed,
        {
            "document_type": "masked_review_mapping_salt",
            "schema_version": 1,
            "masked_review_context_digest": plan.plan_document[
                "masked_review_context_digest"
            ],
            "plan_digest": plan.plan_digest,
        },
    ).hex()
    commitment = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "condition_mapping_commitment",
                "schema_version": 1,
                "masked_review_context_digest": plan.plan_document[
                    "masked_review_context_digest"
                ],
                "plan_digest": plan.plan_digest,
                "mapping_salt": mapping_salt,
                "records": list(records),
            }
        )
    )
    mapping_digest = _nested_records_digest(
        "condition_mapping_records", records
    )
    return (
        tuple(record["neutral_id"] for record in records),
        records,
        commitment,
        mapping_digest,
        artifact_records,
    )


def _regression_task_ids(
    score_lock: CanonicalReceipt,
    records: Sequence[Mapping[str, object]],
) -> Tuple[str, ...]:
    score_by_neutral = {
        score["neutral_id"]: score
        for score in score_lock.payload["locked_score_records"]
    }
    confirmed_by_task = {}
    for record in records:
        confirmed_by_task.setdefault(record["task_id"], {})[
            record["condition"]
        ] = score_by_neutral[record["neutral_id"]]["confirmed_high"]
    return tuple(
        sorted(
            (
                task_id
                for task_id, conditions in confirmed_by_task.items()
                if conditions["lean"] is True
                and conditions["current"] is False
            ),
            key=lambda item: item.encode("utf-8"),
        )
    )


def _expected_high_basis(
    packet: CanonicalReceipt,
    score_lock: CanonicalReceipt,
    condition_mapping_digest: str,
    regression_task_ids: Sequence[str],
) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "document_type": "masked_high_regression_basis",
                "schema_version": 2,
                "masked_packet_receipt_digest": packet.receipt_digest,
                "score_lock_receipt_digest": score_lock.receipt_digest,
                "review_findings_digest": score_lock.payload[
                    "review_findings_digest"
                ],
                "rubric_digest": packet.payload["rubric_digest"],
                "condition_mapping_digest": condition_mapping_digest,
                "regression_task_ids": list(regression_task_ids),
                "adjudication": (
                    "post_unmask_same_task_lean_high_current_not_high"
                ),
            }
        )
    )


def _next_expected(state: _ReplayState) -> Tuple[str, ...]:
    if state.terminal is not None:
        return ()
    if state.containment_receipt is None:
        return ("runtime_containment",)
    if state.pending_reservation is not None:
        if state.pending_reservation.receipt_type == "canary_reservation":
            return ("canary_terminal",)
        return ("pilot_terminal",)
    if state.forced_stop_status is not None:
        return ("experiment_stop",)
    if state.awaiting_canary_terminal is not None:
        return ("canary_receipt", "experiment_stop")
    stop_allowed = (
        state.canary_reservations + state.pilot_reservations > 0
    )
    if len(state.canary_receipts) < 2:
        expected = ("canary_reservation",)
    elif len(state.pilot_terminals) < 8:
        expected = ("pilot_reservation",)
    elif state.packet_receipt is None:
        expected = ("masked_review_packet",)
    elif state.score_lock_receipt is None:
        expected = ("score_lock",)
    elif state.unmask_receipt is None:
        expected = ("unmask",)
    else:
        expected = ("decision",)
    if stop_allowed:
        return expected + ("experiment_stop",)
    return expected


def _public_runtime_state(state: _ReplayState) -> RuntimeState:
    unresolved = ()
    if state.pending_reservation is not None:
        unresolved = (
            state.pending_reservation.payload["reservation_id"],
        )
    return RuntimeState(
        next_expected=_next_expected(state),
        canary_reservations=state.canary_reservations,
        pilot_reservations=state.pilot_reservations,
        total_reservations=(
            state.canary_reservations + state.pilot_reservations
        ),
        unresolved_reservation_ids=unresolved,
        terminal=state.terminal,
    )


def _candidate_for_task(
    plan: ExperimentPlan, task_id: str
) -> Mapping[str, object]:
    for candidate in plan.plan_document["candidates"]:
        if candidate["task_id"] == task_id:
            return candidate
    _raise_history_invalid()


def _validate_nested_telemetry(
    plan: ExperimentPlan, payload: Mapping[str, object]
) -> None:
    document = thaw_json_value(payload["telemetry_summary"])
    summary = telemetry_summary_from_document(
        document, plan.plan_document["price_snapshot"]
    )
    regenerated = telemetry_summary_document(summary)
    if regenerated != document:
        _raise_history_invalid()
    if telemetry_summary_digest(summary) != payload[
        "telemetry_summary_digest"
    ]:
        _raise_history_invalid()


def _apply_runtime_containment(
    state: _ReplayState, candidate: CanonicalReceipt
) -> None:
    if state.containment_receipt is not None:
        _raise_history_invalid()
    state.containment_receipt = candidate


def _apply_canary_reservation(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    index = state.canary_reservations
    if index >= 2 or len(state.canary_receipts) != index:
        _raise_history_invalid()
    payload = candidate.payload
    reservation_id = payload["reservation_id"]
    if reservation_id in state.seen_reservation_ids:
        _raise_history_invalid()
    template = plan.canary_templates[index]
    if (
        payload["profile"] != template.profile
        or payload["base_profile_digest"]
        != template.base_profile_digest
        or payload["canary_template_digest"]
        != plan.plan_document["canary_template_digests"][index]
    ):
        _raise_history_invalid()
    state.seen_reservation_ids.add(reservation_id)
    state.canary_reservations += 1
    state.pending_reservation = candidate


def _apply_canary_terminal(
    state: _ReplayState, candidate: CanonicalReceipt
) -> None:
    reservation = state.pending_reservation
    if (
        reservation is None
        or reservation.receipt_type != "canary_reservation"
    ):
        _raise_history_invalid()
    payload = candidate.payload
    if (
        payload["reservation_id"]
        != reservation.payload["reservation_id"]
        or payload["profile"] != reservation.payload["profile"]
        or payload["reservation_receipt_digest"]
        != reservation.receipt_digest
    ):
        _raise_history_invalid()
    state.pending_reservation = None
    if payload["terminal_status"] == "completed":
        state.awaiting_canary_terminal = candidate
    else:
        state.forced_stop_status = payload["terminal_status"]


def _apply_canary_receipt(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    terminal = state.awaiting_canary_terminal
    if terminal is None:
        _raise_history_invalid()
    index = len(state.canary_receipts)
    if index >= 2:
        _raise_history_invalid()
    template = plan.canary_templates[index]
    payload = candidate.payload
    if (
        payload["reservation_id"] != terminal.payload["reservation_id"]
        or payload["profile"] != terminal.payload["profile"]
        or payload["terminal_receipt_digest"] != terminal.receipt_digest
        or payload["base_bundle_digest"]
        != plan.plan_document["bundle_digest"]
        or payload["base_profile_digest"]
        != template.base_profile_digest
        or payload["canary_overlay_recipe_digest"]
        != template.overlay_recipe_policy_digest
        or payload["containment_capability_digest"]
        != state.containment_receipt.receipt_digest
        or payload["model_policy_digest"]
        != _expected_model_policy_digest(plan, template)
        or payload["marker_digest"] in state.seen_marker_digests
    ):
        _raise_history_invalid()
    state.seen_marker_digests.add(payload["marker_digest"])
    state.canary_receipts.append(candidate)
    state.awaiting_canary_terminal = None


def _apply_pilot_reservation(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    index = state.pilot_reservations
    if (
        len(state.canary_receipts) != 2
        or index >= 8
        or len(state.pilot_terminals) != index
    ):
        _raise_history_invalid()
    payload = candidate.payload
    reservation_id = payload["reservation_id"]
    if reservation_id in state.seen_reservation_ids:
        _raise_history_invalid()
    run = plan.pilot_schedule[index]
    child = plan.pilot_invocation_plans[index]
    if (
        payload["task_id"] != run.task_id
        or payload["condition"] != run.condition
        or payload["snapshot_receipt_digest"]
        != child.snapshot_receipt_digest
        or payload["invocation_plan_digest"]
        != plan.plan_document["pilot_invocation_plan_digests"][index]
    ):
        _raise_history_invalid()
    state.seen_reservation_ids.add(reservation_id)
    state.pilot_reservations += 1
    state.pending_reservation = candidate


def _apply_pilot_terminal(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    reservation = state.pending_reservation
    if (
        reservation is None
        or reservation.receipt_type != "pilot_reservation"
    ):
        _raise_history_invalid()
    payload = candidate.payload
    if (
        payload["reservation_id"]
        != reservation.payload["reservation_id"]
        or payload["task_id"] != reservation.payload["task_id"]
        or payload["condition"] != reservation.payload["condition"]
        or payload["reservation_receipt_digest"]
        != reservation.receipt_digest
    ):
        _raise_history_invalid()
    state.pending_reservation = None
    if payload["terminal_status"] != "completed":
        state.forced_stop_status = payload["terminal_status"]
        return
    _validate_nested_telemetry(plan, payload)
    registered = _candidate_for_task(plan, payload["task_id"])[
        "absolute_safety_assertion_ids"
    ]
    assertion_id = payload["absolute_safety_assertion_id"]
    if assertion_id is not None and (
        assertion_id not in registered
        or payload["condition"] != "lean"
        or payload["machine_assertion_result"] != "fail"
    ):
        _raise_history_invalid()
    state.pilot_terminals.append(candidate)
    if (
        payload["condition"] == "lean"
        and payload["machine_assertion_result"] == "fail"
        and assertion_id is not None
    ):
        state.safety_sources[candidate.receipt_digest] = payload[
            "absolute_safety_basis_digest"
        ]


def _apply_masked_packet(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    if len(state.pilot_terminals) != 8:
        _raise_history_invalid()
    payload = candidate.payload
    if (
        tuple(payload["eligible_pilot_terminal_digests"])
        != tuple(
            terminal.receipt_digest
            for terminal in state.pilot_terminals
        )
        or payload["rubric_digest"] != _expected_rubric_digest(plan)
        or payload["packet_digest"] != _packet_manifest_digest(payload)
    ):
        _raise_history_invalid()
    state.packet_receipt = candidate


def _apply_score_lock(
    state: _ReplayState, candidate: CanonicalReceipt
) -> None:
    packet = state.packet_receipt
    if packet is None:
        _raise_history_invalid()
    payload = candidate.payload
    if (
        payload["masked_packet_receipt_digest"]
        != packet.receipt_digest
        or payload["review_evidence_classification"]
        != packet.payload["review_evidence_classification"]
        or tuple(
            record["neutral_id"]
            for record in payload["locked_score_records"]
        )
        != tuple(packet.payload["randomized_order"])
        or payload["locked_score_records_digest"]
        != _nested_records_digest(
            "locked_score_records",
            payload["locked_score_records"],
        )
    ):
        _raise_history_invalid()
    state.score_lock_receipt = candidate


def _apply_unmask(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    score_lock = state.score_lock_receipt
    packet = state.packet_receipt
    if score_lock is None or packet is None:
        _raise_history_invalid()
    payload = candidate.payload
    records = tuple(payload["condition_mapping_records"])
    seed_reveal = payload["masked_review_seed_reveal"]
    try:
        expected_commitment = masked_review_seed_commitment_digest(
            plan.plan_document["masked_review_context_digest"],
            plan.plan_document[
                "masked_review_seed_source_receipt_digest"
            ],
            seed_reveal,
        )
    except ExperimentPlanError:
        _raise_history_invalid()
    if (
        expected_commitment
        != plan.plan_document["masked_review_seed_commitment_digest"]
    ):
        _raise_history_invalid()
    (
        expected_order,
        expected_records,
        expected_mapping_commitment,
        expected_mapping_digest,
        expected_artifact_records,
    ) = _expected_masked_binding(
        plan, tuple(state.pilot_terminals), seed_reveal
    )
    regression_task_ids = _regression_task_ids(
        score_lock, expected_records
    )
    expected_high_basis = (
        _expected_high_basis(
            packet,
            score_lock,
            expected_mapping_digest,
            regression_task_ids,
        )
        if regression_task_ids
        else None
    )
    if (
        payload["score_lock_receipt_digest"]
        != score_lock.receipt_digest
        or tuple(packet.payload["randomized_order"]) != expected_order
        or records != expected_records
        or packet.payload["condition_mapping_commitment_digest"]
        != expected_mapping_commitment
        or tuple(packet.payload["review_artifact_records"])
        != expected_artifact_records
        or payload["condition_mapping_digest"] != expected_mapping_digest
        or payload["high_regression_basis_digest"]
        != expected_high_basis
    ):
        _raise_history_invalid()
    state.unmask_receipt = candidate


def _runtime_analysis_history_digest(
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    source_records: Sequence[CanonicalReceipt],
) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "document_type": "runtime_analysis_history",
                "schema_version": 1,
                "plan_digest": plan.plan_digest,
                "preflight_receipt_digest": preflight.receipt_digest,
                "runtime_receipt_digests": [
                    receipt.receipt_digest
                    for receipt in source_records
                ],
            }
        )
    )


def _score_records_by_terminal(
    state: _ReplayState,
) -> Mapping[str, Mapping[str, object]]:
    if state.unmask_receipt is None:
        return {}
    score_by_neutral = {
        record["neutral_id"]: record
        for record in state.score_lock_receipt.payload[
            "locked_score_records"
        ]
    }
    return {
        mapping["pilot_terminal_receipt_digest"]: score_by_neutral[
            mapping["neutral_id"]
        ]
        for mapping in state.unmask_receipt.payload[
            "condition_mapping_records"
        ]
    }


def _project_analysis_dataset_from_state(
    plan: ExperimentPlan,
    state: _ReplayState,
    source_records: Sequence[CanonicalReceipt],
):
    terminal_by_binding = {
        (
            terminal.payload["task_id"],
            terminal.payload["condition"],
        ): terminal
        for terminal in state.pilot_terminals
    }
    score_by_terminal = _score_records_by_terminal(state)
    pairs = []
    for index in range(0, len(plan.pilot_schedule), 2):
        runs = plan.pilot_schedule[index : index + 2]
        task_id = runs[0].task_id
        observations = {}
        for condition in ("current", "lean"):
            terminal = terminal_by_binding.get((task_id, condition))
            if terminal is None:
                observations[condition] = None
                continue
            payload = terminal.payload
            document = thaw_json_value(payload["telemetry_summary"])
            summary = telemetry_summary_from_document(
                document, plan.plan_document["price_snapshot"]
            )
            if (
                telemetry_summary_document(summary) != document
                or telemetry_summary_digest(summary)
                != payload["telemetry_summary_digest"]
            ):
                _raise_history_invalid()
            score = score_by_terminal.get(terminal.receipt_digest)
            if state.unmask_receipt is not None and score is None:
                _raise_history_invalid()
            observations[condition] = ValidatedConditionObservation(
                task_id=task_id,
                condition=condition,
                terminal_receipt_digest=terminal.receipt_digest,
                correctness_score=(
                    None
                    if score is None
                    else score["correctness_score"]
                ),
                input_tokens=summary.usage.input_tokens,
                cached_input_tokens=summary.usage.cached_input_tokens,
                output_tokens=summary.usage.output_tokens,
                reasoning_output_tokens=(
                    summary.usage.reasoning_output_tokens
                ),
                wall_time_milliseconds=payload[
                    "wall_time_milliseconds"
                ],
                active_review_milliseconds=(
                    None
                    if score is None
                    else score["active_review_milliseconds"]
                ),
                machine_assertion_passed=(
                    payload["machine_assertion_result"] == "pass"
                ),
                absolute_safety_assertion_id=payload[
                    "absolute_safety_assertion_id"
                ],
                absolute_safety_basis_digest=payload[
                    "absolute_safety_basis_digest"
                ],
            )
        pairs.append(
            ValidatedPairObservation(
                task_id=task_id,
                current=observations["current"],
                lean=observations["lean"],
            )
        )

    masked_review = None
    if state.unmask_receipt is not None:
        masked_review = ValidatedMaskedReviewEvidence(
            packet_receipt_digest=state.packet_receipt.receipt_digest,
            score_lock_receipt_digest=(
                state.score_lock_receipt.receipt_digest
            ),
            unmask_receipt_digest=state.unmask_receipt.receipt_digest,
            high_regression_basis_digest=state.unmask_receipt.payload[
                "high_regression_basis_digest"
            ],
        )

    reasons = set()
    if len(state.pilot_terminals) != 8:
        reasons.add("missing_or_noncompleted_terminal")
    if state.unmask_receipt is None:
        reasons.add("masked_review_chain_incomplete")
    if source_records and source_records[-1].receipt_type == "experiment_stop":
        stop_reason = source_records[-1].payload["reason_code"]
        if stop_reason == "reviewer_abstention":
            reasons.add("reviewer_abstention")
        elif stop_reason == "operator_stop":
            reasons.add("operator_stop")
    ordered_reasons = tuple(
        reason
        for reason in (
            "missing_or_noncompleted_terminal",
            "masked_review_chain_incomplete",
            "reviewer_abstention",
            "operator_stop",
        )
        if reason in reasons
    )
    return _make_validated_analysis_dataset(
        plan_digest=plan.plan_digest,
        runtime_history_digest=_runtime_analysis_history_digest(
            plan,
            state.preflight_receipt,
            source_records,
        ),
        pairs=pairs,
        masked_review=masked_review,
        partial_reason_codes=ordered_reasons,
    )


def _fraction_document(value: object) -> object:
    if value is None:
        return None
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
    }


def _decision_calculation_digest(
    contract: AnalysisContract,
    dataset: object,
    decision: ExperimentDecision,
) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "analysis_contract_digest": (
                    contract.assertion_contract_digest
                ),
                "comparative_aggregate_emitted": (
                    decision.comparative_aggregate_emitted
                ),
                "document_type": "experiment_decision_calculation",
                "efficiency_medians": {
                    name: _fraction_document(
                        decision.efficiency_medians[name]
                    )
                    for name in (
                        "reported_tokens",
                        "wall_time_milliseconds",
                        "active_review_milliseconds",
                    )
                },
                "median_correctness_delta": _fraction_document(
                    decision.median_correctness_delta
                ),
                "outcome": decision.outcome,
                "plan_digest": dataset.plan_digest,
                "qualifying_efficiency_metrics": list(
                    decision.qualifying_efficiency_metrics
                ),
                "reason_code": decision.reason_code,
                "runtime_history_digest": dataset.runtime_history_digest,
                "schema_version": 1,
                "unmask_receipt_digest": (
                    dataset.masked_review.unmask_receipt_digest
                ),
            }
        )
    )


def _apply_decision(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: CanonicalReceipt,
) -> None:
    if state.unmask_receipt is None:
        _raise_history_invalid()
    contract = build_analysis_contract(plan)
    dataset = _project_analysis_dataset_from_state(
        plan, state, tuple(state.records)
    )
    decision = analyze_pairs(contract, dataset)
    payload = candidate.payload
    if (
        payload["unmask_receipt_digest"]
        != state.unmask_receipt.receipt_digest
        or payload["outcome"] != decision.outcome
        or payload["decision_calculation_digest"]
        != _decision_calculation_digest(contract, dataset, decision)
    ):
        _raise_history_invalid()
    state.terminal = "decision"


def _expected_stop_stage(state: _ReplayState) -> str:
    if len(state.canary_receipts) < 2:
        return "canary"
    if len(state.pilot_terminals) < 8:
        return "pilot"
    if state.packet_receipt is None:
        return "masked_review"
    if state.score_lock_receipt is None:
        return "score_lock"
    if state.unmask_receipt is None:
        return "unmask"
    return "decision"


def _validate_stop_combination(
    state: _ReplayState, payload: Mapping[str, object]
) -> None:
    reason = payload["reason_code"]
    outcome = payload["outcome"]
    basis = payload["safety_basis_digest"]
    evidence = tuple(payload["supporting_evidence_receipt_digests"])
    if state.forced_stop_status is not None:
        expected_reason = {
            "failed": "terminal_failed",
            "timed_out": "terminal_timed_out",
            "crashed": "terminal_crashed",
            "abandoned_after_recovery": "abandoned_after_recovery",
        }[state.forced_stop_status]
        if (
            reason != expected_reason
            or outcome != "inconclusive"
            or basis is not None
            or evidence != (state.last_receipt.receipt_digest,)
        ):
            _raise_history_invalid()
        return
    if reason == "operator_stop":
        if (
            outcome != "inconclusive"
            or basis is not None
            or evidence != (state.last_receipt.receipt_digest,)
        ):
            _raise_history_invalid()
        return
    if reason == "reviewer_abstention":
        if (
            _expected_stop_stage(state) in ("canary", "pilot")
            or outcome != "inconclusive"
            or basis is not None
            or evidence != (state.last_receipt.receipt_digest,)
        ):
            _raise_history_invalid()
        return
    if reason == "absolute_lean_safety":
        if (
            len(evidence) != 1
            or basis is None
            or evidence[0] not in state.safety_sources
            or state.safety_sources.get(evidence[0]) != basis
            or outcome != "reject_for_safety"
        ):
            _raise_history_invalid()
        return
    if reason == "masked_high_regression":
        score_lock = state.score_lock_receipt
        unmask = state.unmask_receipt
        if (
            score_lock is None
            or unmask is None
            or basis
            != unmask.payload["high_regression_basis_digest"]
            or basis is None
            or outcome != "reject_for_safety"
            or evidence
            != (score_lock.receipt_digest, unmask.receipt_digest)
        ):
            _raise_history_invalid()
        return
    _raise_history_invalid()


def _apply_stop(
    state: _ReplayState, candidate: CanonicalReceipt
) -> None:
    payload = candidate.payload
    if (
        state.pending_reservation is not None
        or state.canary_reservations + state.pilot_reservations == 0
        or payload["stop_stage"] != _expected_stop_stage(state)
        or payload["consumed_canary_reservations"]
        != state.canary_reservations
        or payload["consumed_pilot_reservations"]
        != state.pilot_reservations
        or payload["consumed_total_reservations"]
        != state.canary_reservations + state.pilot_reservations
        or tuple(payload["unresolved_reservation_ids"]) != ()
    ):
        _raise_history_invalid()
    _validate_stop_combination(state, payload)
    state.terminal = "experiment_stop"


def _apply_runtime_record(
    plan: ExperimentPlan,
    state: _ReplayState,
    candidate: object,
) -> None:
    _validate_canonical_receipt(candidate)
    if (
        candidate.input_digest != plan.input_digest
        or candidate.plan_digest != plan.plan_digest
        or candidate.previous_record_hash
        != state.last_receipt.receipt_digest
        or candidate.receipt_digest in state.seen_receipt_digests
        or candidate.receipt_type not in _next_expected(state)
    ):
        _raise_history_invalid()
    handlers = {
        "runtime_containment": lambda: _apply_runtime_containment(
            state, candidate
        ),
        "canary_reservation": lambda: _apply_canary_reservation(
            plan, state, candidate
        ),
        "canary_terminal": lambda: _apply_canary_terminal(
            state, candidate
        ),
        "canary_receipt": lambda: _apply_canary_receipt(
            plan, state, candidate
        ),
        "pilot_reservation": lambda: _apply_pilot_reservation(
            plan, state, candidate
        ),
        "pilot_terminal": lambda: _apply_pilot_terminal(
            plan, state, candidate
        ),
        "masked_review_packet": lambda: _apply_masked_packet(
            plan, state, candidate
        ),
        "score_lock": lambda: _apply_score_lock(state, candidate),
        "unmask": lambda: _apply_unmask(plan, state, candidate),
        "decision": lambda: _apply_decision(plan, state, candidate),
        "experiment_stop": lambda: _apply_stop(state, candidate),
    }
    handler = handlers.get(candidate.receipt_type)
    if handler is None:
        _raise_history_invalid()
    if (
        candidate.receipt_type == "decision"
        and candidate.payload["unmask_receipt_digest"]
        != state.unmask_receipt.receipt_digest
    ):
        _raise_history_invalid()
    handler()
    state.records.append(candidate)
    state.seen_receipt_digests.add(candidate.receipt_digest)
    state.last_receipt = candidate


def _replay_runtime_private(
    plan: object,
    preflight: object,
    history: object,
) -> Tuple[ExperimentPlan, _ReplayState]:
    checked_plan = _validated_runtime_plan(plan)
    checked_preflight = _validated_preflight(checked_plan, preflight)
    records = _require_sequence(history)
    state = _ReplayState(
        preflight_receipt=checked_preflight,
        last_receipt=checked_preflight,
        records=[],
        seen_receipt_digests={checked_preflight.receipt_digest},
        seen_reservation_ids=set(),
    )
    for record in records:
        _apply_runtime_record(checked_plan, state, record)
    return checked_plan, state


def replay_runtime_history(
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    history: Sequence[CanonicalReceipt],
) -> RuntimeState:
    """Replay a canonical runtime chain without external side effects."""
    try:
        _, state = _replay_runtime_private(plan, preflight, history)
        return _public_runtime_state(state)
    except Exception:
        raise ExperimentReceiptError(_HISTORY_ERROR) from None


def validate_runtime_transition(
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    history: Sequence[CanonicalReceipt],
    candidate: CanonicalReceipt,
) -> RuntimeState:
    """Validate and project one candidate transition without mutating history."""
    try:
        checked_plan, state = _replay_runtime_private(
            plan, preflight, history
        )
        _apply_runtime_record(checked_plan, state, candidate)
        return _public_runtime_state(state)
    except Exception:
        raise ExperimentReceiptError(_HISTORY_ERROR) from None


def project_analysis_dataset(
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    history: Sequence[CanonicalReceipt],
):
    """Project only replayed receipt-carried observations for analysis."""
    try:
        records = _require_sequence(history)
        checked_plan, state = _replay_runtime_private(
            plan, preflight, records
        )
        if not records:
            _raise_history_invalid()
        ending_type = records[-1].receipt_type
        if ending_type not in ("unmask", "decision", "experiment_stop"):
            _raise_history_invalid()
        source_records = (
            records[:-1] if ending_type == "decision" else records
        )
        return _project_analysis_dataset_from_state(
            checked_plan, state, source_records
        )
    except Exception:
        raise ExperimentReceiptError(_HISTORY_ERROR) from None


def analyze_runtime_history(
    contract: AnalysisContract,
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    history: Sequence[CanonicalReceipt],
) -> ExperimentDecision:
    """Analyze a canonical history after requiring its plan-owned contract."""
    expected_contract = build_analysis_contract(plan)
    if type(contract) is not AnalysisContract or contract != expected_contract:
        raise ExperimentPlanError("analysis_dataset_invalid")
    dataset = project_analysis_dataset(plan, preflight, history)
    return analyze_pairs(contract, dataset)
