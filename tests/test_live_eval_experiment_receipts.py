from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from fractions import Fraction
import hashlib
import inspect
import json
from pathlib import Path
from types import MappingProxyType
import unittest

from scripts.live_eval.experiment_plan import (
    CANARY_ARGV_TEMPLATE_DIGEST,
    CANARY_OVERLAY_RECIPE_DIGEST,
    CANARY_RESPONSE_SCHEMA_DIGEST,
    ENVIRONMENT_POLICY_DIGEST,
    PILOT_ARGV_TEMPLATE_DIGEST,
    PILOT_RESPONSE_SCHEMA_DIGEST,
    ROOT_CAPABILITY_POLICY_DIGEST,
    ExperimentPlanError,
    CanaryInvocationTemplate,
    PilotInvocationPlan,
    ValidatedAnalysisDataset,
    analyze_pairs,
    build_analysis_contract,
    build_experiment_plan,
    build_pilot_schedule,
    load_experiment_input,
    sha256_bytes,
    thaw_json_value,
)
from scripts.live_eval.experiment_receipts import (
    CanonicalReceipt,
    ExperimentReceiptError,
    RuntimeState,
    analyze_runtime_history,
    make_receipt,
    project_analysis_dataset,
    replay_runtime_history,
    validate_runtime_transition,
    validate_static_receipt_graph,
)
from scripts.live_eval.experiment_telemetry import (
    telemetry_summary_digest,
    telemetry_summary_from_document,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


FIXTURE = Path(__file__).parent / "fixtures" / "harness_experiment"
VALID_INPUT = FIXTURE / "valid-plan-input.json"


def _digest(label):
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _oid(label, length):
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return (digest * 2)[:length]


class _IntSubclass(int):
    pass


class _StringSubclass(str):
    pass


class _ChangingMapping(Mapping):
    def __init__(self, initial, changed):
        self._initial = dict(initial)
        self._changed = dict(changed)
        self._read_counts = {}

    def __getitem__(self, key):
        count = self._read_counts.get(key, 0)
        self._read_counts[key] = count + 1
        if count == 0:
            return self._initial[key]
        return self._changed.get(key, self._initial[key])

    def __iter__(self):
        return iter(self._initial)

    def __len__(self):
        return len(self._initial)

    @property
    def read_counts(self):
        return dict(self._read_counts)


@dataclass(frozen=True)
class _CanonicalReceiptSubclass(CanonicalReceipt):
    extra: int = 0


def _source_payload(candidate, index):
    object_format = "sha1" if len(candidate["commit_oid"]) == 40 else "sha256"
    source_identity = _digest("source-identity-{}".format(index))
    topology = _digest("source-topology-{}".format(index))
    return {
        "task_id": candidate["task_id"],
        "provisioning_class": candidate["source_provisioning_class"],
        "operator_attested": candidate["operator_attested"],
        "local_clone_policy": candidate["local_clone_policy"],
        "object_format": object_format,
        "source_identity_before_digest": source_identity,
        "object_topology_before_digest": topology,
        "source_identity_after_digest": source_identity,
        "object_topology_after_digest": topology,
        "git_process_policy_digest": _digest(
            "git-process-policy-{}".format(index)
        ),
        "inventory_file_count": index + 1,
        "inventory_total_bytes": (index + 1) * 100,
    }


def _snapshot_payload(candidate, source_receipt, index):
    object_format = source_receipt.payload["object_format"]
    oid_length = 40 if object_format == "sha1" else 64
    return {
        "task_id": candidate["task_id"],
        "object_format": object_format,
        "commit_oid": candidate["commit_oid"],
        "tree_oid": _oid("tree-{}".format(index), oid_length),
        "entry_digest": _digest("entries-{}".format(index)),
        "materialized_tree_digest": _digest(
            "materialized-tree-{}".format(index)
        ),
        "materializer_policy_version": "task-object-materializer-v1",
        "file_count": index + 2,
        "total_bytes": (index + 2) * 200,
        "source_trust_receipt_digest": source_receipt.receipt_digest,
    }


def _static_receipts(experiment_input=None):
    if experiment_input is None:
        experiment_input = load_experiment_input(VALID_INPUT.read_bytes())
    candidates = tuple(experiment_input.value["candidates"])
    sources = tuple(
        make_receipt(
            "task_source_trust",
            experiment_input.input_digest,
            None,
            None,
            _source_payload(candidate, index),
        )
        for index, candidate in enumerate(candidates)
    )
    snapshots = tuple(
        make_receipt(
            "task_snapshot",
            experiment_input.input_digest,
            None,
            None,
            _snapshot_payload(candidate, sources[index], index),
        )
        for index, candidate in enumerate(candidates)
    )
    selection = make_receipt(
        "task_selection",
        experiment_input.input_digest,
        None,
        None,
        {
            "candidate_set_digest": _digest("candidate-set"),
            "selection_rule": "sha256-rank-paired-v1",
            "selection_seed_digest": _digest("selection-seed"),
            "selected_task_ids": [
                candidate["task_id"] for candidate in candidates
            ],
            "pilot_schedule_digest": _digest("pilot-schedule"),
        },
    )
    corpus = make_receipt(
        "task_corpus",
        experiment_input.input_digest,
        None,
        None,
        {
            "candidate_set_digest": selection.payload[
                "candidate_set_digest"
            ],
            "selection_receipt_digest": selection.receipt_digest,
            "selected_snapshot_receipt_digests": [
                snapshot.receipt_digest for snapshot in snapshots
            ],
            "qualification_digest": _digest("qualification"),
            "qualification_evidence_classification": (
                "operator_attested_static"
            ),
            "prompt_digests": [
                candidate["prompt_digest"] for candidate in candidates
            ],
            "validator_digests": [
                candidate["validator_digest"] for candidate in candidates
            ],
            "assertion_digests": [
                candidate["assertion_digest"] for candidate in candidates
            ],
            "reference_result_digest": _digest("reference-result"),
            "mutation_sensitivity_digest": _digest(
                "mutation-sensitivity"
            ),
            "difficulty_assignment_digest": _digest(
                "difficulty-assignment"
            ),
        },
    )
    return experiment_input, sources, snapshots, selection, corpus


def _build_plan(experiment_input, sources, snapshots, selection, corpus):
    model = experiment_input.value["model"]
    invocation = experiment_input.value["invocation_policy"]
    containment = experiment_input.value["containment_policy_version"]
    current_profile_digest = _digest("profile-current")
    lean_profile_digest = _digest("profile-lean")
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
            base_profile_digest=current_profile_digest,
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
            base_profile_digest=lean_profile_digest,
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
    snapshot_by_task = {
        snapshot.payload["task_id"]: snapshot.receipt_digest
        for snapshot in snapshots
    }
    pilot_plans = []
    for run in build_pilot_schedule(
        experiment_input.value["selection_seed"],
        experiment_input.value["candidates"],
    ):
        codex_home = _digest("codex-home-{}".format(run.ordinal))
        task_root = _digest("task-root-{}".format(run.ordinal))
        temp_root = _digest("temp-root-{}".format(run.ordinal))
        pilot_plans.append(
            PilotInvocationPlan(
                ordinal=run.ordinal + 2,
                run=run,
                snapshot_receipt_digest=snapshot_by_task[run.task_id],
                model_id=model["model_id"],
                reasoning_effort=model["reasoning_effort"],
                sandbox="workspace-write",
                approval_policy="never",
                provider_transport_allowed=True,
                tool_network_disabled=True,
                codex_home_identity_digest=codex_home,
                task_root_identity_digest=task_root,
                temp_root_identity_digest=temp_root,
                tool_read_root_identity_digests=tuple(
                    sorted((codex_home, task_root), key=str.encode)
                ),
                tool_write_root_identity_digests=(task_root,),
                validator_read_root_identity_digests=tuple(
                    sorted((task_root, temp_root), key=str.encode)
                ),
                validator_write_root_identity_digests=(temp_root,),
                child_process_policy=invocation["child_process_policy"],
                validator_policy=invocation["validator_policy"],
                output_schema_digest=PILOT_RESPONSE_SCHEMA_DIGEST,
                environment_policy_digest=ENVIRONMENT_POLICY_DIGEST,
                argv_template_digest=PILOT_ARGV_TEMPLATE_DIGEST,
                containment_policy_version=containment,
            )
        )
    return build_experiment_plan(
        experiment_input,
        bundle_digest=_digest("bundle"),
        current_profile_digest=current_profile_digest,
        lean_profile_digest=lean_profile_digest,
        task_source_trust_receipt_digests=tuple(
            sorted(
                (receipt.receipt_digest for receipt in sources),
                key=str.encode,
            )
        ),
        task_selection_receipt_digest=selection.receipt_digest,
        task_corpus_receipt_digest=corpus.receipt_digest,
        canary_templates=templates,
        pilot_invocation_plans=tuple(pilot_plans),
    )


def _preflight_payload(plan):
    return {
        "evidence_state": "static_only",
        "live_backend_state": "live_backend_not_implemented",
        "global_agents_marker_state": "global_agents_marker_not_run",
        "pilot_state": "pilot_not_run",
        "qualification_evidence_classification": (
            "operator_attested_static"
        ),
        "model_calls": 0,
        "bundle_digest": plan.plan_document["bundle_digest"],
        "current_profile_digest": plan.plan_document[
            "current_profile_digest"
        ],
        "lean_profile_digest": plan.plan_document["lean_profile_digest"],
        "task_corpus_receipt_digest": plan.plan_document[
            "task_corpus_receipt_digest"
        ],
        "experiment_plan_digest": plan.plan_digest,
        "canary_template_set_digest": sha256_bytes(
            canonical_bytes(
                {
                    "document_type": "canary_template_set",
                    "schema_version": 1,
                    "digests": list(
                        plan.plan_document["canary_template_digests"]
                    ),
                }
            )
        ),
        "pilot_invocation_plan_set_digest": sha256_bytes(
            canonical_bytes(
                {
                    "document_type": "pilot_invocation_plan_set",
                    "schema_version": 1,
                    "digests": list(
                        plan.plan_document[
                            "pilot_invocation_plan_digests"
                        ]
                    ),
                }
            )
        ),
        "materialization_result": "verified",
        "cleanup_state": "removed",
    }


def _runtime_fixture():
    experiment_input, sources, snapshots, selection, corpus = (
        _static_receipts()
    )
    plan = _build_plan(
        experiment_input, sources, snapshots, selection, corpus
    )
    preflight = make_receipt(
        "preflight",
        plan.input_digest,
        plan.plan_digest,
        None,
        _preflight_payload(plan),
    )
    return plan, preflight


def _runtime_receipt(plan, preflight, history, receipt_type, payload):
    return make_receipt(
        receipt_type,
        plan.input_digest,
        plan.plan_digest,
        history[-1].receipt_digest if history else preflight.receipt_digest,
        payload,
    )


def _append_runtime(plan, preflight, history, receipt_type, payload):
    receipt = _runtime_receipt(
        plan, preflight, history, receipt_type, payload
    )
    validate_runtime_transition(plan, preflight, history, receipt)
    history.append(receipt)
    return receipt


def _containment_payload():
    return {
        "backend_identity_digest": _digest("backend-identity"),
        "model_tool_capability_receipt_digest": _digest(
            "model-tool-capability"
        ),
        "validator_capability_receipt_digest": _digest(
            "validator-capability"
        ),
        "probe_set_digest": _digest("probe-set"),
        "result": "pass",
    }


def _model_policy_digest(plan, template):
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


def _append_containment_and_canaries(plan, preflight, history):
    containment = _append_runtime(
        plan,
        preflight,
        history,
        "runtime_containment",
        _containment_payload(),
    )
    canary_receipts = []
    for index, template in enumerate(plan.canary_templates):
        reservation = _append_runtime(
            plan,
            preflight,
            history,
            "canary_reservation",
            {
                "reservation_id": "canary-reservation-{}".format(index + 1),
                "profile": template.profile,
                "base_profile_digest": template.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][index],
            },
        )
        terminal = _append_runtime(
            plan,
            preflight,
            history,
            "canary_terminal",
            {
                "reservation_id": reservation.payload["reservation_id"],
                "profile": template.profile,
                "reservation_receipt_digest": reservation.receipt_digest,
                "terminal_status": "completed",
                "telemetry_summary_digest": _digest(
                    "canary-telemetry-{}".format(index)
                ),
                "response_digest": _digest(
                    "canary-response-{}".format(index)
                ),
            },
        )
        canary_receipts.append(
            _append_runtime(
                plan,
                preflight,
                history,
                "canary_receipt",
                {
                    "reservation_id": reservation.payload[
                        "reservation_id"
                    ],
                    "profile": template.profile,
                    "terminal_receipt_digest": terminal.receipt_digest,
                    "base_bundle_digest": plan.plan_document[
                        "bundle_digest"
                    ],
                    "base_profile_digest": template.base_profile_digest,
                    "canary_overlay_recipe_digest": (
                        template.overlay_recipe_policy_digest
                    ),
                    "marker_digest": _digest(
                        "canary-marker-{}".format(index)
                    ),
                    "marker_entropy_bits": 128,
                    "marker_occurrence_count": 1,
                    "marker_occurrence_receipt_digest": _digest(
                        "marker-occurrence-{}".format(index)
                    ),
                    "derived_home_digest": _digest(
                        "derived-home-{}".format(index)
                    ),
                    "model_policy_digest": _model_policy_digest(
                        plan, template
                    ),
                    "containment_capability_digest": (
                        containment.receipt_digest
                    ),
                },
            )
        )
    return containment, tuple(canary_receipts)


def _estimated_cost(plan, usage):
    price = plan.plan_document["price_snapshot"]
    numerator = (
        (usage["input_tokens"] - usage["cached_input_tokens"])
        * price["input_microunits_per_million"]
        + usage["cached_input_tokens"]
        * price["cached_input_microunits_per_million"]
        + usage["output_tokens"]
        * price["output_microunits_per_million"]
    )
    return (numerator + 999_999) // 1_000_000


def _telemetry_document(plan, index):
    usage = {
        "input_tokens": 100 + index * 5,
        "cached_input_tokens": 10 + index,
        "output_tokens": 20 + index,
        "reasoning_output_tokens": 5 + index,
        "total_reported_tokens": 120 + index * 6,
        "estimated_cost_microunits": 0,
    }
    usage["estimated_cost_microunits"] = _estimated_cost(plan, usage)
    return {
        "classification": "completed",
        "response_digest": _digest(
            "pilot-response-{}".format(index)
        ),
        "usage": usage,
        "event_count": index + 1,
        "raw_retention": "discard",
    }


def _pilot_terminal_payload(
    plan,
    reservation,
    index,
    *,
    terminal_status="completed",
    machine_assertion_result="pass",
    assertion_id=None,
    safety_basis=None,
    telemetry_document=None,
    wall_time_milliseconds=None
):
    run = plan.pilot_schedule[index]
    if terminal_status != "completed":
        return {
            "reservation_id": reservation.payload["reservation_id"],
            "task_id": run.task_id,
            "condition": run.condition,
            "reservation_receipt_digest": reservation.receipt_digest,
            "terminal_status": terminal_status,
            "telemetry_summary": None,
            "telemetry_summary_digest": None,
            "wall_time_milliseconds": None,
            "machine_assertion_result": None,
            "sanitized_diff_digest": None,
            "allowed_write_inventory_digest": None,
            "absolute_safety_assertion_id": None,
            "absolute_safety_basis_digest": None,
        }
    document = (
        _telemetry_document(plan, index)
        if telemetry_document is None
        else telemetry_document
    )
    summary = telemetry_summary_from_document(
        document, plan.plan_document["price_snapshot"]
    )
    return {
        "reservation_id": reservation.payload["reservation_id"],
        "task_id": run.task_id,
        "condition": run.condition,
        "reservation_receipt_digest": reservation.receipt_digest,
        "terminal_status": "completed",
        "telemetry_summary": document,
        "telemetry_summary_digest": telemetry_summary_digest(summary),
        "wall_time_milliseconds": (
            1_000 + index * 100
            if wall_time_milliseconds is None
            else wall_time_milliseconds
        ),
        "machine_assertion_result": machine_assertion_result,
        "sanitized_diff_digest": _digest(
            "sanitized-diff-{}".format(index)
        ),
        "allowed_write_inventory_digest": _digest(
            "write-inventory-{}".format(index)
        ),
        "absolute_safety_assertion_id": assertion_id,
        "absolute_safety_basis_digest": safety_basis,
    }


def _append_completed_pilots(
    plan,
    preflight,
    history,
    *,
    stop_after=None,
    terminal_overrides=None
):
    terminal_overrides = terminal_overrides or {}
    terminals = []
    for index, (run, child) in enumerate(
        zip(plan.pilot_schedule, plan.pilot_invocation_plans)
    ):
        if stop_after is not None and index >= stop_after:
            break
        reservation = _append_runtime(
            plan,
            preflight,
            history,
            "pilot_reservation",
            {
                "reservation_id": "pilot-reservation-{}".format(index + 1),
                "task_id": run.task_id,
                "condition": run.condition,
                "snapshot_receipt_digest": (
                    child.snapshot_receipt_digest
                ),
                "invocation_plan_digest": plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][index],
            },
        )
        override = terminal_overrides.get(index, {})
        terminals.append(
            _append_runtime(
                plan,
                preflight,
                history,
                "pilot_terminal",
                _pilot_terminal_payload(
                    plan, reservation, index, **override
                ),
            )
        )
    return tuple(terminals)


def _review_chain_payloads(plan, terminals, *, high=False):
    eligible = [terminal.receipt_digest for terminal in terminals]
    randomized_order = [
        "neutral-{:02d}".format(index)
        for index in range(len(terminals), 0, -1)
    ]
    rubric_digest = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "masked_review_rubric_policy",
                "schema_version": 1,
                "analysis_contract_version": plan.plan_document[
                    "analysis_contract_version"
                ],
                "masking_contract_version": plan.plan_document[
                    "masking_contract_version"
                ],
            }
        )
    )
    packet_payload = {
        "eligible_pilot_terminal_digests": eligible,
        "packet_digest": _digest("masked-packet"),
        "randomized_order": randomized_order,
        "leakage_scan_result": "pass",
        "rubric_digest": rubric_digest,
        "reviewer_ids": ["reviewer-a", "reviewer-b"],
    }
    score_records = [
        {
            "neutral_id": neutral_id,
            "correctness_score": 80 + index,
            "active_review_milliseconds": 500 + index * 10,
        }
        for index, neutral_id in enumerate(randomized_order)
    ]
    score_digest = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "locked_score_records",
                "schema_version": 1,
                "records": score_records,
            }
        )
    )
    score_payload = {
        "masked_packet_receipt_digest": None,
        "locked_score_records": score_records,
        "locked_score_records_digest": score_digest,
        "review_findings_digest": _digest("review-findings"),
        "high_regression_basis_digest": None,
    }
    terminal_by_neutral = dict(
        zip(randomized_order, reversed(terminals))
    )
    mapping_records = []
    schedule_by_terminal = {
        terminal.receipt_digest: run
        for terminal, run in zip(terminals, plan.pilot_schedule)
    }
    for neutral_id in randomized_order:
        terminal = terminal_by_neutral[neutral_id]
        run = schedule_by_terminal[terminal.receipt_digest]
        mapping_records.append(
            {
                "neutral_id": neutral_id,
                "task_id": run.task_id,
                "condition": run.condition,
                "pilot_terminal_receipt_digest": terminal.receipt_digest,
            }
        )
    mapping_digest = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "condition_mapping_records",
                "schema_version": 1,
                "records": mapping_records,
            }
        )
    )
    mapping_payload = {
        "score_lock_receipt_digest": None,
        "condition_mapping_records": mapping_records,
        "condition_mapping_digest": mapping_digest,
    }
    return packet_payload, score_payload, mapping_payload


def _append_review_chain(
    plan, preflight, history, terminals, *, high=False
):
    packet_payload, score_payload, mapping_payload = (
        _review_chain_payloads(plan, terminals, high=high)
    )
    packet = _append_runtime(
        plan,
        preflight,
        history,
        "masked_review_packet",
        packet_payload,
    )
    score_payload["masked_packet_receipt_digest"] = packet.receipt_digest
    if high:
        score_payload["high_regression_basis_digest"] = sha256_bytes(
            canonical_bytes(
                {
                    "document_type": "masked_high_regression_basis",
                    "schema_version": 1,
                    "masked_packet_receipt_digest": packet.receipt_digest,
                    "review_findings_digest": score_payload[
                        "review_findings_digest"
                    ],
                    "rubric_digest": packet.payload["rubric_digest"],
                    "adjudication": "high_regression",
                }
            )
        )
    score = _append_runtime(
        plan, preflight, history, "score_lock", score_payload
    )
    mapping_payload["score_lock_receipt_digest"] = score.receipt_digest
    unmask = _append_runtime(
        plan, preflight, history, "unmask", mapping_payload
    )
    return packet, score, unmask


def _controlled_telemetry_document(plan, condition, index):
    if condition == "current":
        input_tokens, output_tokens = 80, 20
    else:
        input_tokens, output_tokens = 60, 20
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 5,
        "total_reported_tokens": input_tokens + output_tokens,
        "estimated_cost_microunits": 0,
    }
    usage["estimated_cost_microunits"] = _estimated_cost(plan, usage)
    return {
        "classification": "completed",
        "response_digest": _digest(
            "controlled-response-{}-{}".format(condition, index)
        ),
        "usage": usage,
        "event_count": index + 1,
        "raw_retention": "discard",
    }


def _append_controlled_pilots(
    plan,
    preflight,
    history,
    *,
    machine_results=None,
    safety_overrides=None
):
    machine_results = machine_results or {}
    safety_overrides = safety_overrides or {}
    terminal_overrides = {}
    for index, run in enumerate(plan.pilot_schedule):
        override = {
            "telemetry_document": _controlled_telemetry_document(
                plan, run.condition, index
            ),
            "wall_time_milliseconds": (
                1_000 if run.condition == "current" else 800
            ),
            "machine_assertion_result": machine_results.get(
                (run.task_id, run.condition), "pass"
            ),
        }
        override.update(
            safety_overrides.get((run.task_id, run.condition), {})
        )
        terminal_overrides[index] = override
    return _append_completed_pilots(
        plan,
        preflight,
        history,
        terminal_overrides=terminal_overrides,
    )


def _append_controlled_review_chain(
    plan, preflight, history, terminals, *, high=False
):
    packet_payload, score_payload, mapping_payload = (
        _review_chain_payloads(plan, terminals)
    )
    condition_by_neutral = {
        record["neutral_id"]: record["condition"]
        for record in mapping_payload["condition_mapping_records"]
    }
    for record in score_payload["locked_score_records"]:
        condition = condition_by_neutral[record["neutral_id"]]
        record["correctness_score"] = (
            80 if condition == "current" else 78
        )
        record["active_review_milliseconds"] = (
            1_000 if condition == "current" else 800
        )
    score_payload["locked_score_records_digest"] = sha256_bytes(
        canonical_bytes(
            {
                "document_type": "locked_score_records",
                "schema_version": 1,
                "records": score_payload["locked_score_records"],
            }
        )
    )
    packet = _append_runtime(
        plan,
        preflight,
        history,
        "masked_review_packet",
        packet_payload,
    )
    score_payload["masked_packet_receipt_digest"] = packet.receipt_digest
    if high:
        score_payload["high_regression_basis_digest"] = sha256_bytes(
            canonical_bytes(
                {
                    "document_type": "masked_high_regression_basis",
                    "schema_version": 1,
                    "masked_packet_receipt_digest": packet.receipt_digest,
                    "review_findings_digest": score_payload[
                        "review_findings_digest"
                    ],
                    "rubric_digest": packet.payload["rubric_digest"],
                    "adjudication": "high_regression",
                }
            )
        )
    score = _append_runtime(
        plan, preflight, history, "score_lock", score_payload
    )
    mapping_payload["score_lock_receipt_digest"] = score.receipt_digest
    unmask = _append_runtime(
        plan, preflight, history, "unmask", mapping_payload
    )
    return packet, score, unmask


def _stop_payload(
    *,
    stage,
    reason,
    canary_count,
    pilot_count,
    evidence,
    outcome="inconclusive",
    basis=None
):
    return {
        "stop_stage": stage,
        "reason_code": reason,
        "consumed_canary_reservations": canary_count,
        "consumed_pilot_reservations": pilot_count,
        "consumed_total_reservations": canary_count + pilot_count,
        "unresolved_reservation_ids": [],
        "supporting_evidence_receipt_digests": list(evidence),
        "safety_basis_digest": basis,
        "outcome": outcome,
    }


class AnalysisProjectionTests(unittest.TestCase):
    def assertHistoryInvalid(self, function, *args):
        with self.assertRaises(ExperimentReceiptError) as raised:
            function(*args)
        self.assertEqual(
            str(raised.exception),
            "experiment_runtime_history_invalid",
        )

    def assertDatasetInvalid(self, contract, dataset):
        with self.assertRaises(ExperimentPlanError) as raised:
            analyze_pairs(contract, dataset)
        self.assertEqual(
            str(raised.exception), "analysis_dataset_invalid"
        )

    def test_projection_and_history_analysis_signatures_accept_no_observations(self):
        self.assertEqual(
            tuple(inspect.signature(project_analysis_dataset).parameters),
            ("plan", "preflight", "history"),
        )
        self.assertEqual(
            tuple(inspect.signature(analyze_runtime_history).parameters),
            ("contract", "plan", "preflight", "history"),
        )

    def test_stop_before_score_lock_projects_joint_null_scores_and_partial_decision(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_controlled_pilots(
            plan, preflight, history
        )
        packet_payload, _, _ = _review_chain_payloads(
            plan, terminals
        )
        packet = _append_runtime(
            plan,
            preflight,
            history,
            "masked_review_packet",
            packet_payload,
        )
        stop = _append_runtime(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="score_lock",
                reason="operator_stop",
                canary_count=2,
                pilot_count=8,
                evidence=(packet.receipt_digest,),
            ),
        )

        dataset = project_analysis_dataset(plan, preflight, history)
        contract = build_analysis_contract(plan)
        decision = analyze_pairs(contract, dataset)

        self.assertEqual(len(dataset.pairs), 4)
        self.assertTrue(
            all(pair.current is not None for pair in dataset.pairs)
        )
        self.assertTrue(
            all(pair.lean is not None for pair in dataset.pairs)
        )
        for pair in dataset.pairs:
            for observation in (pair.current, pair.lean):
                self.assertIsNone(observation.correctness_score)
                self.assertIsNone(
                    observation.active_review_milliseconds
                )
        self.assertIsNone(dataset.masked_review)
        self.assertEqual(
            dataset.partial_reason_codes,
            ("masked_review_chain_incomplete", "operator_stop"),
        )
        expected_history_digest = sha256_bytes(
            canonical_bytes(
                {
                    "document_type": "runtime_analysis_history",
                    "schema_version": 1,
                    "plan_digest": plan.plan_digest,
                    "preflight_receipt_digest": preflight.receipt_digest,
                    "runtime_receipt_digests": [
                        receipt.receipt_digest for receipt in history
                    ],
                }
            )
        )
        self.assertEqual(
            dataset.runtime_history_digest, expected_history_digest
        )
        self.assertEqual(history[-1], stop)
        self.assertEqual(decision.outcome, "inconclusive")
        self.assertEqual(decision.reason_code, "experiment_partial")
        self.assertFalse(decision.comparative_aggregate_emitted)
        self.assertIsNone(decision.median_correctness_delta)
        self.assertEqual(
            tuple(decision.efficiency_medians),
            (
                "reported_tokens",
                "wall_time_milliseconds",
                "active_review_milliseconds",
            ),
        )
        self.assertTrue(
            all(
                value is None
                for value in decision.efficiency_medians.values()
            )
        )
        self.assertEqual(decision.qualifying_efficiency_metrics, ())

    def test_complete_projection_uses_only_receipt_values_and_exact_medians(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_controlled_pilots(
            plan, preflight, history
        )
        packet, score, unmask = _append_controlled_review_chain(
            plan, preflight, history, terminals
        )

        dataset = project_analysis_dataset(plan, preflight, history)
        contract = build_analysis_contract(plan)
        decision = analyze_pairs(contract, dataset)
        wrapped = analyze_runtime_history(
            contract, plan, preflight, history
        )

        self.assertEqual(dataset.plan_digest, plan.plan_digest)
        self.assertEqual(dataset.partial_reason_codes, ())
        self.assertEqual(
            tuple(pair.task_id for pair in dataset.pairs),
            tuple(
                plan.pilot_schedule[index].task_id
                for index in range(0, 8, 2)
            ),
        )
        self.assertEqual(
            dataset.masked_review.packet_receipt_digest,
            packet.receipt_digest,
        )
        self.assertEqual(
            dataset.masked_review.score_lock_receipt_digest,
            score.receipt_digest,
        )
        self.assertEqual(
            dataset.masked_review.unmask_receipt_digest,
            unmask.receipt_digest,
        )
        terminal_by_digest = {
            terminal.receipt_digest: terminal for terminal in terminals
        }
        score_by_neutral = {
            record["neutral_id"]: record
            for record in score.payload["locked_score_records"]
        }
        score_by_terminal = {
            mapping["pilot_terminal_receipt_digest"]: score_by_neutral[
                mapping["neutral_id"]
            ]
            for mapping in unmask.payload["condition_mapping_records"]
        }
        for pair in dataset.pairs:
            for observation in (pair.current, pair.lean):
                terminal = terminal_by_digest[
                    observation.terminal_receipt_digest
                ]
                usage = terminal.payload["telemetry_summary"]["usage"]
                score_record = score_by_terminal[
                    terminal.receipt_digest
                ]
                self.assertEqual(
                    observation.correctness_score,
                    score_record["correctness_score"],
                )
                self.assertEqual(
                    observation.active_review_milliseconds,
                    score_record["active_review_milliseconds"],
                )
                self.assertEqual(
                    observation.input_tokens, usage["input_tokens"]
                )
                self.assertEqual(
                    observation.cached_input_tokens,
                    usage["cached_input_tokens"],
                )
                self.assertEqual(
                    observation.output_tokens, usage["output_tokens"]
                )
                self.assertEqual(
                    observation.reasoning_output_tokens,
                    usage["reasoning_output_tokens"],
                )
                self.assertEqual(
                    observation.wall_time_milliseconds,
                    terminal.payload["wall_time_milliseconds"],
                )
                self.assertEqual(
                    observation.machine_assertion_passed,
                    terminal.payload["machine_assertion_result"] == "pass",
                )

        self.assertEqual(decision.outcome, "advance_to_larger_study")
        self.assertEqual(decision.reason_code, "screening_thresholds_met")
        self.assertTrue(decision.comparative_aggregate_emitted)
        self.assertEqual(
            decision.median_correctness_delta, Fraction(-2, 1)
        )
        self.assertEqual(
            decision.efficiency_medians,
            {
                "reported_tokens": Fraction(1, 5),
                "wall_time_milliseconds": Fraction(1, 5),
                "active_review_milliseconds": Fraction(1, 5),
            },
        )
        self.assertEqual(
            decision.qualifying_efficiency_metrics,
            (
                "reported_tokens",
                "wall_time_milliseconds",
                "active_review_milliseconds",
            ),
        )
        self.assertEqual(wrapped, decision)

    def test_decision_receipt_binds_exact_calculation_and_preserves_source_identity(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_controlled_pilots(
            plan, preflight, history
        )
        _, _, unmask = _append_controlled_review_chain(
            plan, preflight, history, terminals
        )
        contract = build_analysis_contract(plan)
        dataset = project_analysis_dataset(plan, preflight, history)
        decision = analyze_pairs(contract, dataset)

        fraction = lambda value: (
            None
            if value is None
            else {
                "numerator": value.numerator,
                "denominator": value.denominator,
            }
        )
        calculation_document = {
            "analysis_contract_digest": (
                contract.assertion_contract_digest
            ),
            "comparative_aggregate_emitted": (
                decision.comparative_aggregate_emitted
            ),
            "document_type": "experiment_decision_calculation",
            "efficiency_medians": {
                name: fraction(decision.efficiency_medians[name])
                for name in (
                    "reported_tokens",
                    "wall_time_milliseconds",
                    "active_review_milliseconds",
                )
            },
            "median_correctness_delta": fraction(
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
        calculation_digest = sha256_bytes(
            canonical_bytes(calculation_document)
        )
        decision_receipt = _runtime_receipt(
            plan,
            preflight,
            history,
            "decision",
            {
                "unmask_receipt_digest": unmask.receipt_digest,
                "decision_calculation_digest": calculation_digest,
                "outcome": decision.outcome,
            },
        )
        state = validate_runtime_transition(
            plan, preflight, history, decision_receipt
        )
        self.assertEqual(state.terminal, "decision")
        history.append(decision_receipt)
        replayed = replay_runtime_history(plan, preflight, history)
        self.assertEqual(replayed.terminal, "decision")
        decision_dataset = project_analysis_dataset(
            plan, preflight, history
        )
        self.assertEqual(
            decision_dataset.runtime_history_digest,
            dataset.runtime_history_digest,
        )
        self.assertEqual(
            analyze_runtime_history(
                contract, plan, preflight, history
            ),
            decision,
        )

        for field_name, invalid in (
            ("outcome", "inconclusive"),
            (
                "decision_calculation_digest",
                _digest("forged-calculation"),
            ),
        ):
            forged_payload = thaw_json_value(decision_receipt.payload)
            forged_payload[field_name] = invalid
            forged = _runtime_receipt(
                plan,
                preflight,
                history[:-1],
                "decision",
                forged_payload,
            )
            self.assertHistoryInvalid(
                validate_runtime_transition,
                plan,
                preflight,
                history[:-1],
                forged,
            )

    def test_absolute_safety_high_and_machine_regression_precedence(self):
        plan, preflight = _runtime_fixture()
        task_id = plan.pilot_schedule[0].task_id
        assertion_id = next(
            candidate["absolute_safety_assertion_ids"][0]
            for candidate in plan.plan_document["candidates"]
            if candidate["task_id"] == task_id
        )
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        lean_index = next(
            index
            for index, run in enumerate(plan.pilot_schedule)
            if run.task_id == task_id and run.condition == "lean"
        )
        terminals = _append_completed_pilots(
            plan,
            preflight,
            history,
            stop_after=lean_index + 1,
            terminal_overrides={
                lean_index: {
                    "machine_assertion_result": "fail",
                    "assertion_id": assertion_id,
                    "safety_basis": _digest("partial-safety-basis"),
                }
            },
        )
        safety_terminal = terminals[-1]
        _append_runtime(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="pilot",
                reason="absolute_lean_safety",
                canary_count=2,
                pilot_count=lean_index + 1,
                evidence=(safety_terminal.receipt_digest,),
                outcome="reject_for_safety",
                basis=_digest("partial-safety-basis"),
            ),
        )
        partial_safety = analyze_runtime_history(
            build_analysis_contract(plan), plan, preflight, history
        )
        self.assertEqual(
            partial_safety.reason_code,
            "absolute_lean_safety_regression",
        )
        self.assertEqual(partial_safety.outcome, "reject_for_safety")
        self.assertFalse(
            partial_safety.comparative_aggregate_emitted
        )

        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_controlled_pilots(
            plan, preflight, history
        )
        _append_controlled_review_chain(
            plan, preflight, history, terminals, high=True
        )
        high = analyze_runtime_history(
            build_analysis_contract(plan), plan, preflight, history
        )
        self.assertEqual(high.reason_code, "masked_high_regression")
        self.assertEqual(high.outcome, "reject_for_safety")
        self.assertTrue(high.comparative_aggregate_emitted)
        self.assertEqual(
            high.median_correctness_delta, Fraction(-2, 1)
        )

        plan, preflight = _runtime_fixture()
        regression_task = plan.pilot_schedule[0].task_id
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_controlled_pilots(
            plan,
            preflight,
            history,
            machine_results={
                (regression_task, "current"): "pass",
                (regression_task, "lean"): "fail",
            },
        )
        _append_controlled_review_chain(
            plan, preflight, history, terminals
        )
        regression = analyze_runtime_history(
            build_analysis_contract(plan), plan, preflight, history
        )
        self.assertEqual(
            regression.reason_code, "machine_acceptance_regression"
        )
        self.assertEqual(regression.outcome, "reject_for_safety")

    def test_analysis_boundary_rejects_raw_pairs_tokens_and_caller_datasets(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_controlled_pilots(
            plan, preflight, history
        )
        _append_controlled_review_chain(
            plan, preflight, history, terminals
        )
        dataset = project_analysis_dataset(plan, preflight, history)
        contract = build_analysis_contract(plan)
        forged_condition = replace(
            dataset.pairs[0].current,
            condition=_StringSubclass("current"),
        )
        forged_pair = replace(
            dataset.pairs[0], current=forged_condition
        )

        invalid_values = (
            {"pairs": dataset.pairs},
            dataset.pairs,
            ValidatedAnalysisDataset(
                plan_digest=dataset.plan_digest,
                runtime_history_digest=dataset.runtime_history_digest,
                pairs=dataset.pairs,
                masked_review=dataset.masked_review,
                partial_reason_codes=dataset.partial_reason_codes,
                _provenance="arbitrary-provenance-token",
            ),
            replace(dataset, pairs=tuple(reversed(dataset.pairs))),
            replace(
                dataset,
                pairs=(forged_pair,) + dataset.pairs[1:],
            ),
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                self.assertDatasetInvalid(contract, invalid)
        self.assertDatasetInvalid(
            replace(
                contract,
                contract_version=_StringSubclass(
                    "four-pair-screening-v1"
                ),
            ),
            dataset,
        )
        self.assertHistoryInvalid(
            project_analysis_dataset, plan, preflight, {"history": history}
        )
        with self.assertRaises(ExperimentPlanError):
            analyze_runtime_history(
                replace(contract, plan_digest=_digest("other-plan")),
                plan,
                preflight,
                history,
            )

    def test_replay_and_projection_both_reject_rebuilt_nested_mutations(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        run = plan.pilot_schedule[0]
        child = plan.pilot_invocation_plans[0]
        reservation = _append_runtime(
            plan,
            preflight,
            history,
            "pilot_reservation",
            {
                "reservation_id": "projection-telemetry",
                "task_id": run.task_id,
                "condition": run.condition,
                "snapshot_receipt_digest": (
                    child.snapshot_receipt_digest
                ),
                "invocation_plan_digest": plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][0],
            },
        )
        payload = _pilot_terminal_payload(plan, reservation, 0)
        payload["telemetry_summary"]["usage"]["input_tokens"] += 1
        payload["telemetry_summary"]["usage"][
            "total_reported_tokens"
        ] += 1
        payload["telemetry_summary"]["usage"][
            "estimated_cost_microunits"
        ] = _estimated_cost(
            plan, payload["telemetry_summary"]["usage"]
        )
        changed_terminal = _runtime_receipt(
            plan, preflight, history, "pilot_terminal", payload
        )
        telemetry_stop = make_receipt(
            "experiment_stop",
            plan.input_digest,
            plan.plan_digest,
            changed_terminal.receipt_digest,
            _stop_payload(
                stage="pilot",
                reason="operator_stop",
                canary_count=2,
                pilot_count=1,
                evidence=(changed_terminal.receipt_digest,),
            ),
        )
        bad_telemetry_history = history + [
            changed_terminal,
            telemetry_stop,
        ]
        for boundary in (
            replay_runtime_history,
            project_analysis_dataset,
        ):
            self.assertHistoryInvalid(
                boundary, plan, preflight, bad_telemetry_history
            )

        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_completed_pilots(
            plan, preflight, history
        )
        packet_payload, score_payload, mapping_payload = (
            _review_chain_payloads(plan, terminals)
        )
        packet = _append_runtime(
            plan,
            preflight,
            history,
            "masked_review_packet",
            packet_payload,
        )
        score_payload["masked_packet_receipt_digest"] = (
            packet.receipt_digest
        )
        score_payload["locked_score_records"][0][
            "correctness_score"
        ] += 1
        changed_score = _runtime_receipt(
            plan, preflight, history, "score_lock", score_payload
        )
        score_stop = make_receipt(
            "experiment_stop",
            plan.input_digest,
            plan.plan_digest,
            changed_score.receipt_digest,
            _stop_payload(
                stage="unmask",
                reason="operator_stop",
                canary_count=2,
                pilot_count=8,
                evidence=(changed_score.receipt_digest,),
            ),
        )
        bad_score_history = history + [changed_score, score_stop]
        for boundary in (
            replay_runtime_history,
            project_analysis_dataset,
        ):
            self.assertHistoryInvalid(
                boundary, plan, preflight, bad_score_history
            )

        valid_score = _append_runtime(
            plan,
            preflight,
            history,
            "score_lock",
            {
                **_review_chain_payloads(plan, terminals)[1],
                "masked_packet_receipt_digest": packet.receipt_digest,
            },
        )
        mapping_payload["score_lock_receipt_digest"] = (
            valid_score.receipt_digest
        )
        mapping_payload["condition_mapping_records"][0][
            "task_id"
        ] = plan.pilot_schedule[0].task_id
        changed_mapping = _runtime_receipt(
            plan, preflight, history, "unmask", mapping_payload
        )
        mapping_stop = make_receipt(
            "experiment_stop",
            plan.input_digest,
            plan.plan_digest,
            changed_mapping.receipt_digest,
            _stop_payload(
                stage="decision",
                reason="operator_stop",
                canary_count=2,
                pilot_count=8,
                evidence=(changed_mapping.receipt_digest,),
            ),
        )
        bad_mapping_history = history + [
            changed_mapping,
            mapping_stop,
        ]
        for boundary in (
            replay_runtime_history,
            project_analysis_dataset,
        ):
            self.assertHistoryInvalid(
                boundary, plan, preflight, bad_mapping_history
            )


class RuntimeReplayTests(unittest.TestCase):
    def assertHistoryInvalid(self, function, *args):
        with self.assertRaises(ExperimentReceiptError) as raised:
            function(*args)
        self.assertIs(type(raised.exception), ExperimentReceiptError)
        self.assertEqual(
            str(raised.exception),
            "experiment_runtime_history_invalid",
        )

    def test_runtime_state_signatures_and_complete_ten_reservation_chain(self):
        self.assertEqual(
            tuple(item.name for item in fields(RuntimeState)),
            (
                "next_expected",
                "canary_reservations",
                "pilot_reservations",
                "total_reservations",
                "unresolved_reservation_ids",
                "terminal",
            ),
        )
        self.assertEqual(
            tuple(inspect.signature(replay_runtime_history).parameters),
            ("plan", "preflight", "history"),
        )
        self.assertEqual(
            tuple(inspect.signature(validate_runtime_transition).parameters),
            ("plan", "preflight", "history", "candidate"),
        )
        plan, preflight = _runtime_fixture()
        initial = replay_runtime_history(plan, preflight, ())
        self.assertEqual(initial.next_expected, ("runtime_containment",))
        self.assertEqual(initial.canary_reservations, 0)
        self.assertEqual(initial.pilot_reservations, 0)
        self.assertEqual(initial.total_reservations, 0)
        self.assertEqual(initial.unresolved_reservation_ids, ())
        self.assertIsNone(initial.terminal)

        history = []
        _append_containment_and_canaries(plan, preflight, history)
        after_canaries = replay_runtime_history(plan, preflight, history)
        self.assertEqual(after_canaries.canary_reservations, 2)
        self.assertEqual(after_canaries.pilot_reservations, 0)
        self.assertEqual(
            after_canaries.next_expected,
            ("pilot_reservation", "experiment_stop"),
        )

        terminals = _append_completed_pilots(
            plan, preflight, history
        )
        after_pilots = replay_runtime_history(plan, preflight, history)
        self.assertEqual(after_pilots.canary_reservations, 2)
        self.assertEqual(after_pilots.pilot_reservations, 8)
        self.assertEqual(after_pilots.total_reservations, 10)
        self.assertEqual(
            after_pilots.next_expected,
            ("masked_review_packet", "experiment_stop"),
        )
        packet, score, unmask = _append_review_chain(
            plan, preflight, history, terminals
        )
        final = replay_runtime_history(plan, preflight, tuple(history))
        self.assertEqual(
            final.next_expected, ("decision", "experiment_stop")
        )
        self.assertEqual(final.total_reservations, 10)
        self.assertEqual(final.unresolved_reservation_ids, ())
        self.assertIsNone(final.terminal)
        self.assertEqual(
            packet.payload["eligible_pilot_terminal_digests"],
            tuple(terminal.receipt_digest for terminal in terminals),
        )
        self.assertEqual(
            score.payload["masked_packet_receipt_digest"],
            packet.receipt_digest,
        )
        self.assertEqual(
            unmask.payload["score_lock_receipt_digest"],
            score.receipt_digest,
        )

        copied_history = list(history)
        replayed = replay_runtime_history(
            plan, preflight, copied_history
        )
        copied_history.clear()
        self.assertEqual(replayed, final)
        self.assertHistoryInvalid(
            replay_runtime_history, plan, preflight, "runtime-history"
        )

    def test_pending_reservation_requires_terminal_and_recovery_then_stop(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_runtime(
            plan,
            preflight,
            history,
            "runtime_containment",
            _containment_payload(),
        )
        template = plan.canary_templates[0]
        reservation = _append_runtime(
            plan,
            preflight,
            history,
            "canary_reservation",
            {
                "reservation_id": "pending-canary",
                "profile": "current",
                "base_profile_digest": template.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][0],
            },
        )
        pending = replay_runtime_history(plan, preflight, history)
        self.assertEqual(pending.next_expected, ("canary_terminal",))
        self.assertEqual(
            pending.unresolved_reservation_ids, ("pending-canary",)
        )
        stop = _runtime_receipt(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="canary",
                reason="operator_stop",
                canary_count=1,
                pilot_count=0,
                evidence=(reservation.receipt_digest,),
            ),
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            stop,
        )

        malformed_payload = {
            "reservation_id": "pending-canary",
            "profile": "current",
            "reservation_receipt_digest": reservation.receipt_digest,
            "terminal_status": "malformed",
            "telemetry_summary_digest": None,
            "response_digest": None,
        }
        with self.assertRaisesRegex(
            ExperimentReceiptError, "^experiment_receipt_invalid$"
        ):
            _runtime_receipt(
                plan,
                preflight,
                history,
                "canary_terminal",
                malformed_payload,
            )

        abandoned = _append_runtime(
            plan,
            preflight,
            history,
            "canary_terminal",
            {
                **malformed_payload,
                "terminal_status": "abandoned_after_recovery",
            },
        )
        forced = replay_runtime_history(plan, preflight, history)
        self.assertEqual(forced.next_expected, ("experiment_stop",))
        self.assertEqual(forced.canary_reservations, 1)
        self.assertEqual(forced.unresolved_reservation_ids, ())

        retry = _runtime_receipt(
            plan,
            preflight,
            history,
            "canary_reservation",
            {
                "reservation_id": "retry",
                "profile": "lean",
                "base_profile_digest": plan.canary_templates[
                    1
                ].base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][1],
            },
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            retry,
        )
        stop_receipt = _append_runtime(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="canary",
                reason="abandoned_after_recovery",
                canary_count=1,
                pilot_count=0,
                evidence=(abandoned.receipt_digest,),
            ),
        )
        terminal = replay_runtime_history(plan, preflight, history)
        self.assertEqual(terminal.next_expected, ())
        self.assertEqual(terminal.terminal, "experiment_stop")
        later = replace(
            stop_receipt,
            previous_record_hash=stop_receipt.receipt_digest,
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            later,
        )

    def test_each_noncompleted_status_consumes_allowance_and_forces_exact_stop(self):
        reasons = {
            "failed": "terminal_failed",
            "timed_out": "terminal_timed_out",
            "crashed": "terminal_crashed",
            "abandoned_after_recovery": "abandoned_after_recovery",
        }
        for status, reason in reasons.items():
            with self.subTest(status=status):
                plan, preflight = _runtime_fixture()
                history = []
                _append_runtime(
                    plan,
                    preflight,
                    history,
                    "runtime_containment",
                    _containment_payload(),
                )
                template = plan.canary_templates[0]
                reservation = _append_runtime(
                    plan,
                    preflight,
                    history,
                    "canary_reservation",
                    {
                        "reservation_id": "canary-{}".format(status),
                        "profile": "current",
                        "base_profile_digest": (
                            template.base_profile_digest
                        ),
                        "canary_template_digest": plan.plan_document[
                            "canary_template_digests"
                        ][0],
                    },
                )
                terminal_receipt = _append_runtime(
                    plan,
                    preflight,
                    history,
                    "canary_terminal",
                    {
                        "reservation_id": reservation.payload[
                            "reservation_id"
                        ],
                        "profile": "current",
                        "reservation_receipt_digest": (
                            reservation.receipt_digest
                        ),
                        "terminal_status": status,
                        "telemetry_summary_digest": None,
                        "response_digest": None,
                    },
                )
                state = replay_runtime_history(
                    plan, preflight, history
                )
                self.assertEqual(state.canary_reservations, 1)
                self.assertEqual(state.total_reservations, 1)
                wrong_reason = _runtime_receipt(
                    plan,
                    preflight,
                    history,
                    "experiment_stop",
                    _stop_payload(
                        stage="canary",
                        reason="operator_stop",
                        canary_count=1,
                        pilot_count=0,
                        evidence=(terminal_receipt.receipt_digest,),
                    ),
                )
                self.assertHistoryInvalid(
                    validate_runtime_transition,
                    plan,
                    preflight,
                    history,
                    wrong_reason,
                )
                _append_runtime(
                    plan,
                    preflight,
                    history,
                    "experiment_stop",
                    _stop_payload(
                        stage="canary",
                        reason=reason,
                        canary_count=1,
                        pilot_count=0,
                        evidence=(terminal_receipt.receipt_digest,),
                    ),
                )

    def test_canary_order_profile_templates_markers_and_limits_are_exact(self):
        plan, preflight = _runtime_fixture()
        history = []
        containment = _append_runtime(
            plan,
            preflight,
            history,
            "runtime_containment",
            _containment_payload(),
        )
        current = plan.canary_templates[0]
        lean = plan.canary_templates[1]
        invalid_reservations = (
            {
                "reservation_id": "wrong-profile",
                "profile": "lean",
                "base_profile_digest": lean.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][1],
            },
            {
                "reservation_id": "wrong-base",
                "profile": "current",
                "base_profile_digest": lean.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][0],
            },
            {
                "reservation_id": "wrong-template",
                "profile": "current",
                "base_profile_digest": current.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][1],
            },
        )
        for payload in invalid_reservations:
            candidate = _runtime_receipt(
                plan,
                preflight,
                history,
                "canary_reservation",
                payload,
            )
            self.assertHistoryInvalid(
                validate_runtime_transition,
                plan,
                preflight,
                history,
                candidate,
            )

        _, canary_receipts = _append_containment_and_canaries(
            plan, preflight, []
        )
        complete_history = []
        _append_containment_and_canaries(
            plan, preflight, complete_history
        )
        third = _runtime_receipt(
            plan,
            preflight,
            complete_history,
            "canary_reservation",
            {
                "reservation_id": "third-canary",
                "profile": "current",
                "base_profile_digest": current.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][0],
            },
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            complete_history,
            third,
        )
        self.assertNotEqual(
            canary_receipts[0].payload["marker_digest"],
            canary_receipts[1].payload["marker_digest"],
        )
        receipt_indexes = [
            index
            for index, receipt in enumerate(complete_history)
            if receipt.receipt_type == "canary_receipt"
        ]
        duplicate_marker_payload = thaw_json_value(
            complete_history[receipt_indexes[1]].payload
        )
        duplicate_marker_payload["marker_digest"] = complete_history[
            receipt_indexes[0]
        ].payload["marker_digest"]
        duplicate_marker_candidate = _runtime_receipt(
            plan,
            preflight,
            complete_history[: receipt_indexes[1]],
            "canary_receipt",
            duplicate_marker_payload,
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            complete_history[: receipt_indexes[1]],
            duplicate_marker_candidate,
        )

        history = []
        _append_runtime(
            plan,
            preflight,
            history,
            "runtime_containment",
            _containment_payload(),
        )
        first_reservation = _append_runtime(
            plan,
            preflight,
            history,
            "canary_reservation",
            {
                "reservation_id": "first-canary",
                "profile": "current",
                "base_profile_digest": current.base_profile_digest,
                "canary_template_digest": plan.plan_document[
                    "canary_template_digests"
                ][0],
            },
        )
        first_terminal = _append_runtime(
            plan,
            preflight,
            history,
            "canary_terminal",
            {
                "reservation_id": "first-canary",
                "profile": "current",
                "reservation_receipt_digest": (
                    first_reservation.receipt_digest
                ),
                "terminal_status": "completed",
                "telemetry_summary_digest": _digest(
                    "first-canary-telemetry"
                ),
                "response_digest": _digest("first-canary-response"),
            },
        )
        pilot = plan.pilot_invocation_plans[0]
        pilot_before_receipt = _runtime_receipt(
            plan,
            preflight,
            history,
            "pilot_reservation",
            {
                "reservation_id": "early-pilot",
                "task_id": pilot.run.task_id,
                "condition": pilot.run.condition,
                "snapshot_receipt_digest": (
                    pilot.snapshot_receipt_digest
                ),
                "invocation_plan_digest": plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][0],
            },
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            pilot_before_receipt,
        )
        duplicate_marker = _runtime_receipt(
            plan,
            preflight,
            history,
            "canary_receipt",
            {
                "reservation_id": "first-canary",
                "profile": "current",
                "terminal_receipt_digest": first_terminal.receipt_digest,
                "base_bundle_digest": plan.plan_document["bundle_digest"],
                "base_profile_digest": current.base_profile_digest,
                "canary_overlay_recipe_digest": (
                    current.overlay_recipe_policy_digest
                ),
                "marker_digest": _digest("unique-marker"),
                "marker_entropy_bits": 128,
                "marker_occurrence_count": 1,
                "marker_occurrence_receipt_digest": _digest(
                    "occurrence"
                ),
                "derived_home_digest": _digest("derived"),
                "model_policy_digest": _model_policy_digest(
                    plan, current
                ),
                "containment_capability_digest": containment.receipt_digest,
            },
        )
        validate_runtime_transition(
            plan, preflight, history, duplicate_marker
        )

    def test_pilot_schedule_snapshot_invocation_ids_and_budget_are_exact(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        run = plan.pilot_schedule[0]
        child = plan.pilot_invocation_plans[0]
        other = next(
            candidate
            for candidate in plan.pilot_invocation_plans
            if candidate.run.task_id != run.task_id
        )
        base_payload = {
            "reservation_id": "pilot-first",
            "task_id": run.task_id,
            "condition": run.condition,
            "snapshot_receipt_digest": child.snapshot_receipt_digest,
            "invocation_plan_digest": plan.plan_document[
                "pilot_invocation_plan_digests"
            ][0],
        }
        mutations = (
            ("task_id", other.run.task_id),
            ("condition", other.run.condition),
            ("snapshot_receipt_digest", _digest("wrong-snapshot")),
            ("invocation_plan_digest", _digest("wrong-invocation")),
            ("reservation_id", "canary-reservation-1"),
        )
        for field_name, invalid in mutations:
            with self.subTest(field=field_name):
                payload = dict(base_payload)
                payload[field_name] = invalid
                candidate = _runtime_receipt(
                    plan,
                    preflight,
                    history,
                    "pilot_reservation",
                    payload,
                )
                self.assertHistoryInvalid(
                    validate_runtime_transition,
                    plan,
                    preflight,
                    history,
                    candidate,
                )

        terminals = _append_completed_pilots(
            plan, preflight, history
        )
        ninth = _runtime_receipt(
            plan,
            preflight,
            history,
            "pilot_reservation",
            {
                "reservation_id": "pilot-nine",
                "task_id": run.task_id,
                "condition": run.condition,
                "snapshot_receipt_digest": child.snapshot_receipt_digest,
                "invocation_plan_digest": plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][0],
            },
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            ninth,
        )
        self.assertEqual(len(terminals), 8)

    def test_cross_input_plan_stale_duplicate_hash_and_direct_forgery_fail(self):
        plan, preflight = _runtime_fixture()
        for field_name in ("input_digest", "plan_digest"):
            forged_plan = replace(
                plan,
                **{
                    field_name: _StringSubclass(
                        getattr(plan, field_name)
                    )
                },
            )
            self.assertHistoryInvalid(
                replay_runtime_history,
                forged_plan,
                preflight,
                (),
            )
        history = []
        containment = _append_runtime(
            plan,
            preflight,
            history,
            "runtime_containment",
            _containment_payload(),
        )
        template = plan.canary_templates[0]
        payload = {
            "reservation_id": "candidate-canary",
            "profile": "current",
            "base_profile_digest": template.base_profile_digest,
            "canary_template_digest": plan.plan_document[
                "canary_template_digests"
            ][0],
        }
        candidates = (
            make_receipt(
                "canary_reservation",
                _digest("other-input"),
                plan.plan_digest,
                containment.receipt_digest,
                payload,
            ),
            make_receipt(
                "canary_reservation",
                plan.input_digest,
                _digest("other-plan"),
                containment.receipt_digest,
                payload,
            ),
            make_receipt(
                "canary_reservation",
                plan.input_digest,
                plan.plan_digest,
                preflight.receipt_digest,
                payload,
            ),
        )
        for candidate in candidates:
            self.assertHistoryInvalid(
                validate_runtime_transition,
                plan,
                preflight,
                history,
                candidate,
            )

        valid = _runtime_receipt(
            plan,
            preflight,
            history,
            "canary_reservation",
            payload,
        )
        forged = (
            replace(valid, canonical_bytes=b"{}"),
            replace(valid, receipt_digest=_digest("forged")),
            _CanonicalReceiptSubclass(
                **{
                    item.name: getattr(valid, item.name)
                    for item in fields(CanonicalReceipt)
                }
            ),
        )
        for candidate in forged:
            self.assertHistoryInvalid(
                validate_runtime_transition,
                plan,
                preflight,
                history,
                candidate,
            )
        self.assertHistoryInvalid(
            replay_runtime_history,
            plan,
            preflight,
            history + [containment],
        )

    def test_nested_telemetry_score_and_mapping_digests_are_recomputed(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        run = plan.pilot_schedule[0]
        child = plan.pilot_invocation_plans[0]
        reservation = _append_runtime(
            plan,
            preflight,
            history,
            "pilot_reservation",
            {
                "reservation_id": "pilot-nested",
                "task_id": run.task_id,
                "condition": run.condition,
                "snapshot_receipt_digest": (
                    child.snapshot_receipt_digest
                ),
                "invocation_plan_digest": plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][0],
            },
        )
        valid_terminal_payload = _pilot_terminal_payload(
            plan, reservation, 0
        )
        changed_terminal_payload = json.loads(
            json.dumps(valid_terminal_payload)
        )
        usage = changed_terminal_payload["telemetry_summary"]["usage"]
        usage["input_tokens"] += 1
        usage["total_reported_tokens"] += 1
        usage["estimated_cost_microunits"] = _estimated_cost(plan, usage)
        changed_terminal = _runtime_receipt(
            plan,
            preflight,
            history,
            "pilot_terminal",
            changed_terminal_payload,
        )
        self.assertEqual(
            changed_terminal.payload["telemetry_summary_digest"],
            valid_terminal_payload["telemetry_summary_digest"],
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            changed_terminal,
        )

        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_completed_pilots(
            plan, preflight, history
        )
        packet_payload, score_payload, mapping_payload = (
            _review_chain_payloads(plan, terminals)
        )
        wrong_rubric = dict(packet_payload)
        wrong_rubric["rubric_digest"] = _digest("wrong-rubric")
        packet_candidate = _runtime_receipt(
            plan,
            preflight,
            history,
            "masked_review_packet",
            wrong_rubric,
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            packet_candidate,
        )
        packet = _append_runtime(
            plan,
            preflight,
            history,
            "masked_review_packet",
            packet_payload,
        )
        score_payload["masked_packet_receipt_digest"] = (
            packet.receipt_digest
        )
        for field_name, delta in (
            ("correctness_score", 1),
            ("active_review_milliseconds", 1),
        ):
            with self.subTest(nested_score_field=field_name):
                changed_score = json.loads(json.dumps(score_payload))
                changed_score["locked_score_records"][0][
                    field_name
                ] += delta
                score_candidate = _runtime_receipt(
                    plan,
                    preflight,
                    history,
                    "score_lock",
                    changed_score,
                )
                self.assertEqual(
                    score_candidate.payload[
                        "locked_score_records_digest"
                    ],
                    score_payload["locked_score_records_digest"],
                )
                self.assertHistoryInvalid(
                    validate_runtime_transition,
                    plan,
                    preflight,
                    history,
                    score_candidate,
                )

        forged_high = dict(score_payload)
        forged_high["high_regression_basis_digest"] = _digest(
            "forged-high"
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            _runtime_receipt(
                plan,
                preflight,
                history,
                "score_lock",
                forged_high,
            ),
        )
        score = _append_runtime(
            plan, preflight, history, "score_lock", score_payload
        )
        mapping_payload["score_lock_receipt_digest"] = (
            score.receipt_digest
        )
        mapping_mutations = (
            ("task_id", plan.pilot_schedule[0].task_id),
            (
                "condition",
                "lean"
                if mapping_payload["condition_mapping_records"][0][
                    "condition"
                ]
                == "current"
                else "current",
            ),
            (
                "pilot_terminal_receipt_digest",
                _digest("unregistered-pilot-terminal"),
            ),
        )
        for field_name, invalid in mapping_mutations:
            with self.subTest(mapping_field=field_name):
                changed_mapping = json.loads(json.dumps(mapping_payload))
                changed_mapping["condition_mapping_records"][0][
                    field_name
                ] = invalid
                candidate = _runtime_receipt(
                    plan,
                    preflight,
                    history,
                    "unmask",
                    changed_mapping,
                )
                self.assertHistoryInvalid(
                    validate_runtime_transition,
                    plan,
                    preflight,
                    history,
                    candidate,
                )

                changed_mapping["condition_mapping_digest"] = sha256_bytes(
                    canonical_bytes(
                        {
                            "document_type": (
                                "condition_mapping_records"
                            ),
                            "schema_version": 1,
                            "records": changed_mapping[
                                "condition_mapping_records"
                            ],
                        }
                    )
                )
                rebuilt = _runtime_receipt(
                    plan,
                    preflight,
                    history,
                    "unmask",
                    changed_mapping,
                )
                self.assertHistoryInvalid(
                    validate_runtime_transition,
                    plan,
                    preflight,
                    history,
                    rebuilt,
                )

    def test_registered_absolute_safety_and_masked_high_are_only_valid_rejections(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        lean_index = next(
            index
            for index, run in enumerate(plan.pilot_schedule)
            if run.condition == "lean"
        )
        basis = _digest("absolute-lean-safety")
        assertion_id = next(
            candidate["absolute_safety_assertion_ids"][0]
            for candidate in plan.plan_document["candidates"]
            if candidate["task_id"]
            == plan.pilot_schedule[lean_index].task_id
        )
        terminals = _append_completed_pilots(
            plan,
            preflight,
            history,
            stop_after=lean_index + 1,
            terminal_overrides={
                lean_index: {
                    "machine_assertion_result": "fail",
                    "assertion_id": assertion_id,
                    "safety_basis": basis,
                }
            },
        )
        safety_terminal = terminals[-1]
        stop = _append_runtime(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="pilot",
                reason="absolute_lean_safety",
                canary_count=2,
                pilot_count=lean_index + 1,
                evidence=(safety_terminal.receipt_digest,),
                outcome="reject_for_safety",
                basis=basis,
            ),
        )
        self.assertEqual(stop.payload["outcome"], "reject_for_safety")

        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_completed_pilots(
            plan, preflight, history
        )
        packet, score, unmask = _append_review_chain(
            plan, preflight, history, terminals, high=True
        )
        high_basis = score.payload["high_regression_basis_digest"]
        high_stop = _append_runtime(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="decision",
                reason="masked_high_regression",
                canary_count=2,
                pilot_count=8,
                evidence=(score.receipt_digest, unmask.receipt_digest),
                outcome="reject_for_safety",
                basis=high_basis,
            ),
        )
        self.assertEqual(
            high_stop.payload["supporting_evidence_receipt_digests"],
            (score.receipt_digest, unmask.receipt_digest),
        )
        self.assertNotEqual(packet.receipt_digest, score.receipt_digest)

    def test_absolute_safety_basis_may_identify_multiple_matching_terminals(self):
        plan, preflight = _runtime_fixture()
        lean_indexes = [
            index
            for index, run in enumerate(plan.pilot_schedule)
            if run.condition == "lean"
        ][:2]
        shared_basis = _digest("shared-absolute-safety-basis")
        overrides = {}
        for index in lean_indexes:
            task_id = plan.pilot_schedule[index].task_id
            assertion_id = next(
                candidate["absolute_safety_assertion_ids"][0]
                for candidate in plan.plan_document["candidates"]
                if candidate["task_id"] == task_id
            )
            overrides[index] = {
                "machine_assertion_result": "fail",
                "assertion_id": assertion_id,
                "safety_basis": shared_basis,
            }
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        terminals = _append_completed_pilots(
            plan,
            preflight,
            history,
            stop_after=lean_indexes[-1] + 1,
            terminal_overrides=overrides,
        )
        first_safety_terminal = terminals[lean_indexes[0]]

        stop = _runtime_receipt(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="pilot",
                reason="absolute_lean_safety",
                canary_count=2,
                pilot_count=lean_indexes[-1] + 1,
                evidence=(first_safety_terminal.receipt_digest,),
                outcome="reject_for_safety",
                basis=shared_basis,
            ),
        )

        state = validate_runtime_transition(
            plan, preflight, history, stop
        )
        self.assertEqual(state.terminal, "experiment_stop")

    def test_stop_rejects_advance_unbased_safety_wrong_stage_and_zero_reservations(self):
        plan, preflight = _runtime_fixture()
        history = []
        containment = _append_runtime(
            plan,
            preflight,
            history,
            "runtime_containment",
            _containment_payload(),
        )
        zero_stop = _runtime_receipt(
            plan,
            preflight,
            history,
            "experiment_stop",
            _stop_payload(
                stage="canary",
                reason="operator_stop",
                canary_count=0,
                pilot_count=0,
                evidence=(containment.receipt_digest,),
            ),
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            zero_stop,
        )

        _append_containment_and_canaries(plan, preflight, history := [])
        predecessor = history[-1]
        invalid_payloads = (
            _stop_payload(
                stage="pilot",
                reason="operator_stop",
                canary_count=2,
                pilot_count=0,
                evidence=(predecessor.receipt_digest,),
                outcome="advance_to_larger_study",
            ),
            _stop_payload(
                stage="pilot",
                reason="absolute_lean_safety",
                canary_count=2,
                pilot_count=0,
                evidence=(predecessor.receipt_digest,),
                outcome="reject_for_safety",
                basis=None,
            ),
            _stop_payload(
                stage="canary",
                reason="operator_stop",
                canary_count=2,
                pilot_count=0,
                evidence=(predecessor.receipt_digest,),
            ),
        )
        for payload in invalid_payloads:
            candidate = _runtime_receipt(
                plan,
                preflight,
                history,
                "experiment_stop",
                payload,
            )
            self.assertHistoryInvalid(
                validate_runtime_transition,
                plan,
                preflight,
                history,
                candidate,
            )

    def test_unregistered_cross_task_and_unpaired_safety_assertions_fail(self):
        plan, preflight = _runtime_fixture()
        history = []
        _append_containment_and_canaries(plan, preflight, history)
        run = plan.pilot_schedule[0]
        child = plan.pilot_invocation_plans[0]
        reservation = _append_runtime(
            plan,
            preflight,
            history,
            "pilot_reservation",
            {
                "reservation_id": "unregistered-assertion",
                "task_id": run.task_id,
                "condition": run.condition,
                "snapshot_receipt_digest": (
                    child.snapshot_receipt_digest
                ),
                "invocation_plan_digest": plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][0],
            },
        )
        unregistered = _runtime_receipt(
            plan,
            preflight,
            history,
            "pilot_terminal",
            _pilot_terminal_payload(
                plan,
                reservation,
                0,
                machine_assertion_result="fail",
                assertion_id="not-registered",
                safety_basis=_digest("unregistered-basis"),
            ),
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            plan,
            preflight,
            history,
            unregistered,
        )

        unpaired = _pilot_terminal_payload(plan, reservation, 0)
        unpaired["absolute_safety_basis_digest"] = _digest(
            "basis-without-id"
        )
        with self.assertRaisesRegex(
            ExperimentReceiptError, "^experiment_receipt_invalid$"
        ):
            _runtime_receipt(
                plan,
                preflight,
                history,
                "pilot_terminal",
                unpaired,
            )

        value = json.loads(VALID_INPUT.read_text(encoding="utf-8"))
        for candidate in value["candidates"]:
            candidate["absolute_safety_assertion_ids"] = [
                "safety-{}".format(candidate["task_id"])
            ]
        distinct_input = load_experiment_input(canonical_bytes(value))
        experiment_input, sources, snapshots, selection, corpus = (
            _static_receipts(distinct_input)
        )
        distinct_plan = _build_plan(
            experiment_input, sources, snapshots, selection, corpus
        )
        distinct_preflight = make_receipt(
            "preflight",
            distinct_plan.input_digest,
            distinct_plan.plan_digest,
            None,
            _preflight_payload(distinct_plan),
        )
        distinct_history = []
        _append_containment_and_canaries(
            distinct_plan, distinct_preflight, distinct_history
        )
        run = distinct_plan.pilot_schedule[0]
        child = distinct_plan.pilot_invocation_plans[0]
        reservation = _append_runtime(
            distinct_plan,
            distinct_preflight,
            distinct_history,
            "pilot_reservation",
            {
                "reservation_id": "cross-task-assertion",
                "task_id": run.task_id,
                "condition": run.condition,
                "snapshot_receipt_digest": (
                    child.snapshot_receipt_digest
                ),
                "invocation_plan_digest": distinct_plan.plan_document[
                    "pilot_invocation_plan_digests"
                ][0],
            },
        )
        other_task = next(
            candidate
            for candidate in distinct_plan.plan_document["candidates"]
            if candidate["task_id"] != run.task_id
        )
        cross_task = _runtime_receipt(
            distinct_plan,
            distinct_preflight,
            distinct_history,
            "pilot_terminal",
            _pilot_terminal_payload(
                distinct_plan,
                reservation,
                0,
                machine_assertion_result="fail",
                assertion_id=other_task[
                    "absolute_safety_assertion_ids"
                ][0],
                safety_basis=_digest("cross-task-basis"),
            ),
        )
        self.assertHistoryInvalid(
            validate_runtime_transition,
            distinct_plan,
            distinct_preflight,
            distinct_history,
            cross_task,
        )


class StaticReceiptTests(unittest.TestCase):
    def assertReceiptInvalid(self, function, *args):
        with self.assertRaises(ExperimentReceiptError) as raised:
            function(*args)
        self.assertIs(type(raised.exception), ExperimentReceiptError)
        self.assertEqual(
            str(raised.exception), "experiment_receipt_invalid"
        )

    def test_envelope_fields_error_type_and_canonical_digest_are_exact(self):
        experiment_input, sources, _, _, _ = _static_receipts()
        receipt = sources[0]

        self.assertEqual(ExperimentReceiptError.__bases__, (ValueError,))
        self.assertEqual(
            tuple(item.name for item in fields(CanonicalReceipt)),
            (
                "receipt_type",
                "input_digest",
                "plan_digest",
                "previous_record_hash",
                "payload",
                "canonical_bytes",
                "receipt_digest",
            ),
        )
        expected_document = {
            "schema_version": 1,
            "receipt_type": "task_source_trust",
            "input_digest": experiment_input.input_digest,
            "plan_digest": None,
            "previous_record_hash": None,
            "payload": thaw_json_value(receipt.payload),
        }
        self.assertEqual(receipt.canonical_bytes, canonical_bytes(expected_document))
        self.assertEqual(
            receipt.receipt_digest,
            sha256_bytes(receipt.canonical_bytes),
        )
        self.assertIsInstance(receipt.payload, MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            receipt.receipt_type = "changed"
        with self.assertRaises(TypeError):
            receipt.payload["task_id"] = "changed"

    def test_constructor_detaches_caller_containers_and_reads_stateful_mapping_once(self):
        experiment_input = load_experiment_input(VALID_INPUT.read_bytes())
        initial = _source_payload(
            experiment_input.value["candidates"][0], 0
        )
        stateful = _ChangingMapping(
            initial, {"task_id": "changed-after-first-read"}
        )

        receipt = make_receipt(
            "task_source_trust",
            experiment_input.input_digest,
            None,
            None,
            stateful,
        )

        self.assertEqual(receipt.payload["task_id"], initial["task_id"])
        self.assertEqual(
            stateful.read_counts,
            {key: 1 for key in initial},
        )
        initial["task_id"] = "mutated"
        self.assertNotEqual(receipt.payload["task_id"], "mutated")
        self.assertEqual(
            receipt.receipt_digest,
            sha256_bytes(receipt.canonical_bytes),
        )

    def test_all_static_payloads_and_preflight_are_exact_and_digest_bound(self):
        experiment_input, sources, snapshots, selection, corpus = (
            _static_receipts()
        )
        validate_static_receipt_graph(
            experiment_input, sources, snapshots, selection, corpus
        )
        plan = _build_plan(
            experiment_input, sources, snapshots, selection, corpus
        )
        preflight = make_receipt(
            "preflight",
            plan.input_digest,
            plan.plan_digest,
            None,
            _preflight_payload(plan),
        )

        self.assertEqual(
            set(plan.plan_document["task_source_trust_receipt_digests"]),
            {receipt.receipt_digest for receipt in sources},
        )
        self.assertEqual(
            plan.plan_document["task_selection_receipt_digest"],
            selection.receipt_digest,
        )
        self.assertEqual(
            plan.plan_document["task_corpus_receipt_digest"],
            corpus.receipt_digest,
        )
        snapshot_by_task = {
            receipt.payload["task_id"]: receipt.receipt_digest
            for receipt in snapshots
        }
        for child in plan.pilot_invocation_plans:
            self.assertEqual(
                child.snapshot_receipt_digest,
                snapshot_by_task[child.run.task_id],
            )
        self.assertEqual(preflight.payload, _preflight_payload(plan))
        self.assertEqual(preflight.previous_record_hash, None)

        cases = (
            (sources[0], "git_process_policy_digest"),
            (snapshots[0], "entry_digest"),
            (selection, "candidate_set_digest"),
            (corpus, "qualification_digest"),
            (preflight, "bundle_digest"),
        )
        for receipt, field_name in cases:
            with self.subTest(
                receipt_type=receipt.receipt_type, field=field_name
            ):
                payload = thaw_json_value(receipt.payload)
                payload[field_name] = _digest(
                    "changed-{}-{}".format(
                        receipt.receipt_type, field_name
                    )
                )
                changed = make_receipt(
                    receipt.receipt_type,
                    receipt.input_digest,
                    receipt.plan_digest,
                    receipt.previous_record_hash,
                    payload,
                )
                self.assertNotEqual(
                    changed.receipt_digest, receipt.receipt_digest
                )

    def test_static_graph_public_signature_and_same_input_reuse_are_exact(self):
        signature = inspect.signature(validate_static_receipt_graph)
        self.assertEqual(
            tuple(signature.parameters),
            (
                "experiment_input",
                "source_trust_receipts",
                "snapshot_receipts",
                "selection_receipt",
                "corpus_receipt",
            ),
        )
        experiment_input, sources, snapshots, selection, corpus = (
            _static_receipts()
        )
        reloaded = load_experiment_input(VALID_INPUT.read_bytes())

        self.assertIsNone(
            validate_static_receipt_graph(
                reloaded, sources, snapshots, selection, corpus
            )
        )

    def test_constructor_rejects_unknown_missing_envelope_and_strict_scalar_attacks(self):
        experiment_input, sources, snapshots, selection, corpus = (
            _static_receipts()
        )
        valid_receipts = sources[:1] + snapshots[:1] + (selection, corpus)
        for receipt in valid_receipts:
            payload = thaw_json_value(receipt.payload)
            first_key = next(iter(payload))
            with self.subTest(
                receipt_type=receipt.receipt_type, mutation="missing"
            ):
                changed = dict(payload)
                del changed[first_key]
                self.assertReceiptInvalid(
                    make_receipt,
                    receipt.receipt_type,
                    receipt.input_digest,
                    receipt.plan_digest,
                    receipt.previous_record_hash,
                    changed,
                )
            with self.subTest(
                receipt_type=receipt.receipt_type, mutation="unknown"
            ):
                changed = dict(payload)
                changed["unknown"] = "value"
                self.assertReceiptInvalid(
                    make_receipt,
                    receipt.receipt_type,
                    receipt.input_digest,
                    receipt.plan_digest,
                    receipt.previous_record_hash,
                    changed,
                )

        source_payload = thaw_json_value(sources[0].payload)
        for field_name in ("inventory_file_count", "inventory_total_bytes"):
            for invalid in (True, _IntSubclass(1), -1):
                with self.subTest(field=field_name, invalid=repr(invalid)):
                    changed = dict(source_payload)
                    changed[field_name] = invalid
                    self.assertReceiptInvalid(
                        make_receipt,
                        "task_source_trust",
                        experiment_input.input_digest,
                        None,
                        None,
                        changed,
                    )
        for invalid in (
            _StringSubclass(source_payload["task_id"]),
            "e\u0301",
        ):
            changed = dict(source_payload)
            changed["task_id"] = invalid
            self.assertReceiptInvalid(
                make_receipt,
                "task_source_trust",
                experiment_input.input_digest,
                None,
                None,
                changed,
            )
        selection_payload = thaw_json_value(selection.payload)
        selection_payload["selected_task_ids"] = "task-sequence"
        self.assertReceiptInvalid(
            make_receipt,
            "task_selection",
            experiment_input.input_digest,
            None,
            None,
            selection_payload,
        )
        self.assertReceiptInvalid(
            make_receipt,
            "task_source_trust",
            experiment_input.input_digest,
            _digest("plan"),
            None,
            source_payload,
        )
        self.assertReceiptInvalid(
            make_receipt,
            "unknown_receipt",
            experiment_input.input_digest,
            None,
            None,
            source_payload,
        )

    def test_preflight_requires_exact_success_values_and_plan_identity(self):
        experiment_input, sources, snapshots, selection, corpus = (
            _static_receipts()
        )
        plan = _build_plan(
            experiment_input, sources, snapshots, selection, corpus
        )
        payload = _preflight_payload(plan)
        for field_name, invalid in (
            ("evidence_state", "live"),
            ("live_backend_state", "implemented"),
            ("global_agents_marker_state", "run"),
            ("pilot_state", "run"),
            ("qualification_evidence_classification", "verified"),
            ("model_calls", True),
            ("model_calls", _IntSubclass(0)),
            ("materialization_result", "unverified"),
            ("cleanup_state", "retained"),
            ("experiment_plan_digest", _digest("other-plan")),
        ):
            with self.subTest(field=field_name, invalid=repr(invalid)):
                changed = dict(payload)
                changed[field_name] = invalid
                self.assertReceiptInvalid(
                    make_receipt,
                    "preflight",
                    plan.input_digest,
                    plan.plan_digest,
                    None,
                    changed,
                )
        self.assertReceiptInvalid(
            make_receipt,
            "preflight",
            plan.input_digest,
            None,
            None,
            payload,
        )
        self.assertReceiptInvalid(
            make_receipt,
            "preflight",
            plan.input_digest,
            plan.plan_digest,
            _digest("previous"),
            payload,
        )

    def test_static_graph_rejects_direct_forgery_cross_input_task_and_evidence(self):
        experiment_input, sources, snapshots, selection, corpus = (
            _static_receipts()
        )

        forged_digest = replace(
            sources[0], receipt_digest=_digest("forged-receipt")
        )
        self.assertReceiptInvalid(
            validate_static_receipt_graph,
            experiment_input,
            (forged_digest,) + sources[1:],
            snapshots,
            selection,
            corpus,
        )
        subclass = _CanonicalReceiptSubclass(
            **{
                item.name: getattr(sources[0], item.name)
                for item in fields(CanonicalReceipt)
            }
        )
        self.assertReceiptInvalid(
            validate_static_receipt_graph,
            experiment_input,
            (subclass,) + sources[1:],
            snapshots,
            selection,
            corpus,
        )

        changed_input_value = json.loads(
            VALID_INPUT.read_text(encoding="utf-8")
        )
        changed_input_value["experiment_id"] = "changed-experiment"
        changed_input = load_experiment_input(
            canonical_bytes(changed_input_value)
        )
        self.assertReceiptInvalid(
            validate_static_receipt_graph,
            changed_input,
            sources,
            snapshots,
            selection,
            corpus,
        )
        self.assertReceiptInvalid(
            validate_static_receipt_graph,
            replace(
                experiment_input,
                input_digest=_StringSubclass(
                    experiment_input.input_digest
                ),
            ),
            sources,
            snapshots,
            selection,
            corpus,
        )

        wrong_snapshot_payload = thaw_json_value(snapshots[0].payload)
        wrong_snapshot_payload["task_id"] = snapshots[1].payload["task_id"]
        wrong_snapshot_payload["source_trust_receipt_digest"] = (
            sources[1].receipt_digest
        )
        wrong_snapshot = make_receipt(
            "task_snapshot",
            experiment_input.input_digest,
            None,
            None,
            wrong_snapshot_payload,
        )
        self.assertReceiptInvalid(
            validate_static_receipt_graph,
            experiment_input,
            sources,
            (wrong_snapshot,) + snapshots[1:],
            selection,
            corpus,
        )

        wrong_selection_payload = thaw_json_value(selection.payload)
        wrong_selection_payload["selected_task_ids"][0:2] = reversed(
            wrong_selection_payload["selected_task_ids"][0:2]
        )
        wrong_selection = make_receipt(
            "task_selection",
            experiment_input.input_digest,
            None,
            None,
            wrong_selection_payload,
        )
        self.assertReceiptInvalid(
            validate_static_receipt_graph,
            experiment_input,
            sources,
            snapshots,
            wrong_selection,
            corpus,
        )

        corpus_mutations = (
            (
                "selection_receipt_digest",
                _digest("wrong-selection"),
            ),
            (
                "selected_snapshot_receipt_digests",
                list(reversed(corpus.payload[
                    "selected_snapshot_receipt_digests"
                ])),
            ),
            (
                "prompt_digests",
                [_digest("wrong-prompt")]
                + list(corpus.payload["prompt_digests"][1:]),
            ),
            (
                "validator_digests",
                [_digest("wrong-validator")]
                + list(corpus.payload["validator_digests"][1:]),
            ),
            (
                "assertion_digests",
                [_digest("wrong-assertion")]
                + list(corpus.payload["assertion_digests"][1:]),
            ),
        )
        for field_name, invalid in corpus_mutations:
            with self.subTest(field=field_name):
                changed = thaw_json_value(corpus.payload)
                changed[field_name] = invalid
                wrong_corpus = make_receipt(
                    "task_corpus",
                    experiment_input.input_digest,
                    None,
                    None,
                    changed,
                )
                self.assertReceiptInvalid(
                    validate_static_receipt_graph,
                    experiment_input,
                    sources,
                    snapshots,
                    selection,
                    wrong_corpus,
                )


if __name__ == "__main__":
    unittest.main()
