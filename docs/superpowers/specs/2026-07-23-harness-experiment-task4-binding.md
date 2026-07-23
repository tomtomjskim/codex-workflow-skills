# Harness Experiment Task 4 Binding Contract

**Status:** Normative clarification for Task 4 of
`2026-07-23-harness-experiment-readiness-plan.md`.

This contract resolves only ambiguities that block deterministic receipt,
runtime-replay, projection, and decision behavior. It adds no live ledger,
subprocess, authentication, network, lock, or Task 5 telemetry-parser
behavior.

## 1. Plan-bound snapshot authority

Task 4 amends the pre-runtime plan seam by adding this exact field to
`PilotInvocationPlan`, immediately after `run`:

```python
snapshot_receipt_digest: str
```

The field is an exact lowercase `sha256:` digest and enters the canonical
pilot-invocation-plan document. The two condition plans for one task carry
the same snapshot receipt digest; the four task digests are distinct. A
pilot reservation must match the next plan-bound invocation plan's task,
condition, snapshot receipt digest, and canonical invocation-plan digest.

This is the authoritative runtime snapshot binding. A task-root identity is a
condition-specific local root identity and is not a substitute for the
task's condition-independent snapshot receipt.

## 2. Receipt envelope and errors

Expose:

```python
class ExperimentReceiptError(ValueError):
    pass
```

`make_receipt()` normalizes any structural, payload, envelope, or codec
failure to:

```text
ExperimentReceiptError("experiment_receipt_invalid")
```

`replay_runtime_history()`, `validate_runtime_transition()`,
`project_analysis_dataset()`, and `analyze_runtime_history()` normalize an
invalid preflight, history, nested digest, linkage, or transition to:

```text
ExperimentReceiptError("experiment_runtime_history_invalid")
```

All public boundaries reject dataclass subclasses, booleans supplied as
integers, string or bytes values supplied as sequences, unknown or missing
keys, and non-NFC strings. Public constructors snapshot and detach caller
mappings and sequences before validation and canonicalization. Later mutation
of caller-owned containers cannot change a receipt, dataset, canonical bytes,
or digest.

Every digest is an exact `str` matching `^sha256:[0-9a-f]{64}$`. Every
non-null opaque ID is an exact non-empty NFC `str`. Task IDs use the plan
task-ID domain. Integer fields are exact, non-boolean integers within their
stated range.

A receipt is accepted at every public boundary only when
`type(value) is CanonicalReceipt` and recomputing its exact schema-version-1
document from detached fields produces:

- byte-for-byte equality with `canonical_bytes`;
- `sha256_bytes(canonical_bytes) == receipt_digest`; and
- the required envelope nullability.

The envelope classes are disjoint:

- `task_source_trust`, `task_snapshot`, `task_selection`, and `task_corpus`
  require `plan_digest=None` and `previous_record_hash=None`;
- `preflight` requires a non-null plan digest equal to
  `payload.experiment_plan_digest` and `previous_record_hash=None`;
- every runtime type requires non-null plan and previous-record digests.

## 3. Static receipt ordering and intrinsic domains

Candidate task order means the exact canonical-input candidate order.

- `task_source_trust` requires the fixed provisioning and clone-policy values
  from the canonical input, exact `operator_attested=True`, object format
  `sha1` or `sha256`, matching before/after source-identity digests, matching
  before/after topology digests, a process-policy digest, and non-negative
  inventory counts.
- `task_snapshot` requires the matching task and source-trust receipt,
  matching object format, full commit and tree OIDs of that format,
  `materializer_policy_version=task-object-materializer-v1`, three evidence
  digests, and non-negative file and byte counts.
- `task_selection.selected_task_ids` is exactly the four task IDs in
  candidate order. Its rule is `sha256-rank-paired-v1`; its other scalar
  evidence values are digests.
- `task_corpus.selected_snapshot_receipt_digests`, `prompt_digests`,
  `validator_digests`, and `assertion_digests` each contain exactly four
  values in candidate order. Snapshot receipt digests are unique. The
  selection receipt and all snapshot receipts use the same input digest.
  Qualification is exactly `operator_attested_static`; every remaining
  evidence scalar is a digest.

The plan independently binds the source-trust, selection, corpus, and
pilot-invocation-plan digests. The zero-call orchestrator later proves the
cross-receipt equality before calling the plan builder.

Expose the exact static authority boundary:

```python
def validate_static_receipt_graph(
    experiment_input: CanonicalExperimentInput,
    source_trust_receipts: Sequence[CanonicalReceipt],
    snapshot_receipts: Sequence[CanonicalReceipt],
    selection_receipt: CanonicalReceipt,
    corpus_receipt: CanonicalReceipt,
) -> None:
    ...
```

It requires an exact canonical experiment input, exactly four source-trust
and four snapshot receipts in candidate order, and exact receipt types,
canonical bytes, digests, and input-digest equality. Each candidate task and
commit matches one source-trust-to-snapshot link; each snapshot references
that task's source-trust receipt and object format. Selection task order
matches candidate order. Corpus candidate-set and selection digests match the
selection receipt; its snapshot, prompt, validator, and assertion digest
sequences exactly match the candidate-ordered receipts and canonical input.
Failure raises `ExperimentReceiptError("experiment_receipt_invalid")`.

Task 8 calls this function before passing receipt digests and plan-bound
snapshot receipt digests to the plan builder.

The successful Phase A preflight values are exact:

```text
evidence_state=static_only
live_backend_state=live_backend_not_implemented
global_agents_marker_state=global_agents_marker_not_run
pilot_state=pilot_not_run
qualification_evidence_classification=operator_attested_static
model_calls=0
materialization_result=verified
cleanup_state=removed
```

All remaining preflight fields are non-null digests and equal their plan
identities. The two set digests are:

```python
sha256_bytes(canonical_bytes({
    "document_type": "canary_template_set",
    "schema_version": 1,
    "digests": list(plan.plan_document["canary_template_digests"]),
}))
```

and the same document with
`document_type="pilot_invocation_plan_set"` and the plan's pilot invocation
digests.

Blocked preflight receipt behavior remains Task 8 scope.

## 4. Runtime replay state and sequencing

`RuntimeState` is only the immutable public projection. Replay uses a private
state containing at least:

- the last receipt;
- globally seen reservation IDs and receipt digests;
- the pending reservation with its receipt digest and bound profile or
  task/condition;
- the completed canary terminal awaiting its canary receipt;
- seen marker digests;
- plan-ordered completed pilot terminals;
- packet, score-lock, and unmask receipts; and
- validated safety-basis sources.

Reservation IDs are globally unique across canary and pilot reservations. A
reservation consumes allowance when appended, regardless of its later
terminal status.

A terminal immediately follows and exactly matches the one pending
reservation, including `reservation_receipt_digest`. A state with a pending
reservation accepts no direct stop. It accepts a matching terminal; recovery
uses `abandoned_after_recovery`. Because candidate validation is pure, a
rejected malformed candidate does not mutate history. After a valid
non-completed terminal, only `experiment_stop` is allowed.

When no reservation is pending and at least one reservation has been
recorded, a valid stop may replace any otherwise allowed next record. Stop is
not allowed immediately after preflight or runtime containment before the
first reservation.

Canary order is `current`, then `lean`. A completed canary terminal permits
its matching `canary_receipt` or stop. Both completed canary receipts are
required before the first pilot reservation. Pilot reservation and terminal
order is exactly `plan.pilot_schedule`. After eight completed pilot terminals,
the only progress path is packet, score lock, unmask, then decision. Stop
remains allowed at each no-pending nonterminal point. Decision and stop are
unique terminal records.

The five terminal statuses are:

```text
completed
failed
timed_out
crashed
abandoned_after_recovery
```

`malformed` is not a status; such a candidate is rejected without changing
the replayed state.

For a completed `pilot_terminal`:

- `telemetry_summary` is Task 3's exact mapping;
- its regenerated document and digest must match;
- `wall_time_milliseconds` is a non-negative integer;
- `machine_assertion_result` is `pass` or `fail`;
- diff and inventory values are digests; and
- assertion ID and basis are jointly null or jointly non-null.
- A non-null assertion ID and basis additionally require `condition=lean`
  and `machine_assertion_result=fail`.

For a non-completed `pilot_terminal`, `telemetry_summary`,
`telemetry_summary_digest`, `wall_time_milliseconds`,
`machine_assertion_result`, both diff/inventory digests, and both assertion
fields are all null. `reservation_receipt_digest`, task, condition, and status
remain required.

Completed canary terminals require telemetry and response digests.
Non-completed canary terminals require both to be null.

## 5. Canary evidence bindings

A canary receipt matches the immediately preceding completed canary terminal,
reservation ID, profile, and terminal receipt digest.

- `base_bundle_digest` equals the plan bundle digest;
- `base_profile_digest` equals the applicable template base-profile digest;
- `canary_overlay_recipe_digest` equals the template overlay-policy digest;
- `containment_capability_digest` equals the accepted runtime-containment
  receipt digest;
- `marker_entropy_bits` is exactly `128`;
- `marker_occurrence_count` is exactly `1`; and
- marker digests differ between profiles.

Occurrence-evidence and derived-home digests are opaque Phase B evidence
identities whose format and receipt linkage are validated in Phase A.

`model_policy_digest` is the digest of:

```json
{
  "document_type": "canary_model_policy",
  "schema_version": 1,
  "model": "<the plan's exact model mapping>",
  "argv_template_digest": "<template argv digest>",
  "environment_policy_digest": "<template environment digest>"
}
```

## 6. Review records and nested digests

Eligible pilot terminal digests are in plan schedule order. The packet's
`randomized_order` contains eight unique neutral IDs. Score and mapping
records are each in exactly that order. Reviewer IDs are non-empty, UTF-8-byte
sorted, and unique. Supporting evidence receipt digests are unique, in ledger
order, and reference only earlier accepted receipts.

The score-record digest is:

```python
sha256_bytes(canonical_bytes({
    "document_type": "locked_score_records",
    "schema_version": 1,
    "records": ordered_records,
}))
```

The condition-mapping digest uses the same shape with
`document_type="condition_mapping_records"`.

The mapping reconstructs every plan-scheduled task, condition, and eligible
pilot terminal exactly once. Outer receipt validity never substitutes for
recomputing telemetry, score-record, mapping, or decision-calculation
digests.

The packet rubric digest must equal:

```python
sha256_bytes(canonical_bytes({
    "document_type": "masked_review_rubric_policy",
    "schema_version": 1,
    "analysis_contract_version":
        plan.plan_document["analysis_contract_version"],
    "masking_contract_version":
        plan.plan_document["masking_contract_version"],
}))
```

The HIGH-basis calculation uses this already validated rubric identity.

A non-null HIGH basis must equal:

```python
sha256_bytes(canonical_bytes({
    "document_type": "masked_high_regression_basis",
    "schema_version": 1,
    "masked_packet_receipt_digest": packet.receipt_digest,
    "review_findings_digest": score_lock.payload["review_findings_digest"],
    "rubric_digest": packet.payload["rubric_digest"],
    "adjudication": "high_regression",
}))
```

It is usable only after valid unmask.

## 7. Stop evidence

`stop_stage` is replay-derived and exactly one of:

```text
canary
pilot
masked_review
score_lock
unmask
decision
```

It names the progress stage replaced by stop:

- fewer than two valid canary receipts: `canary`;
- otherwise fewer than eight completed pilot terminals: `pilot`;
- otherwise no masked packet: `masked_review`;
- otherwise no score lock: `score_lock`;
- otherwise no unmask: `unmask`;
- otherwise: `decision`.

A non-completed canary or pilot terminal remains in its respective stage.

Consumed counts equal replay counters and total equals canary plus pilot.
Direct stop while a reservation is pending is invalid, so a valid stop's
`unresolved_reservation_ids` is empty.

Allowed reason, outcome, basis, and evidence combinations are:

| reason | outcome | basis | supporting evidence |
|---|---|---|---|
| `operator_stop` | `inconclusive` | null | immediate predecessor |
| `terminal_failed` | `inconclusive` | null | matching failed terminal |
| `terminal_timed_out` | `inconclusive` | null | matching timed-out terminal |
| `terminal_crashed` | `inconclusive` | null | matching crashed terminal |
| `abandoned_after_recovery` | `inconclusive` | null | matching abandoned terminal |
| `reviewer_abstention` | `inconclusive` | null | immediate predecessor |
| `absolute_lean_safety` | `reject_for_safety` | registered lean basis | matching pilot terminal |
| `masked_high_regression` | `reject_for_safety` | valid HIGH basis | score-lock then unmask |

`advance_to_larger_study` is never a stop outcome. The supporting evidence
tuple is exact for the selected row. When the immediate predecessor is a
non-completed terminal, its status-specific reason is required instead of
`operator_stop`.

## 8. Analysis-source identity and projection

The runtime-history digest identifies the analysis-source prefix:

- a history ending at unmask includes records through unmask;
- a history ending at decision excludes decision and therefore retains the
  pre-decision identity;
- a stop history includes stop because its reason controls partial
  projection.

```python
sha256_bytes(canonical_bytes({
    "document_type": "runtime_analysis_history",
    "schema_version": 1,
    "plan_digest": plan.plan_digest,
    "preflight_receipt_digest": preflight.receipt_digest,
    "runtime_receipt_digests": [
        receipt.receipt_digest for receipt in analysis_source_prefix
    ],
}))
```

Scores and active-review times become condition-owned only after valid
unmask. They are jointly null for every stop before unmask, including a stop
after score lock. After unmask, the neutral-ID join is:

```text
score record -> mapping record -> eligible pilot terminal
             -> exact plan-scheduled task and condition
```

A missing or non-completed terminal is an absent condition observation,
never a zero or partially populated observation.

`partial_reason_codes` is the applicable filtered subsequence of this fixed
order:

```text
missing_or_noncompleted_terminal
masked_review_chain_incomplete
reviewer_abstention
operator_stop
```

Invalid receipts, telemetry, nested digests, or joins raise a history error;
they are not projected as partial values. A safety stop with incomplete
execution retains applicable partial codes. A complete masked-HIGH stop has
no partial reason.

`project_analysis_dataset()` supports a valid source ending at unmask,
decision, or stop. It is the only public producer of a provenance-bearing
dataset.

## 9. Decision values and aggregates

The efficiency metric names and normative order are:

```text
reported_tokens
wall_time_milliseconds
active_review_milliseconds
```

`efficiency_medians` always has exactly these keys in this order.
`qualifying_efficiency_metrics` is the qualifying subset in the same order.

A dataset is aggregate-complete only when all eight terminals are completed,
the full packet/score-lock/unmask chain is valid, and
`partial_reason_codes` is empty.

For an aggregate-complete dataset, including a safety rejection:

- `comparative_aggregate_emitted=True`;
- median correctness is an exact `Fraction`;
- each eligible efficiency median is an exact `Fraction`; and
- a metric whose four current baselines are not all positive is `None` and
  does not qualify.

For a partial dataset:

- `comparative_aggregate_emitted=False`;
- median correctness is `None`;
- all three efficiency values are `None`; and
- qualifying metrics is empty.

Decision precedence is:

1. a registered lean absolute-safety basis rejects even when partial;
2. otherwise any partial condition is inconclusive;
3. otherwise a valid masked HIGH or a complete-pair current-pass/lean-fail
   machine regression rejects;
4. otherwise the fixed correctness and two-of-three efficiency thresholds
   determine advancement;
5. otherwise the result is inconclusive.

The only decision outcomes are `reject_for_safety`,
`advance_to_larger_study`, and `inconclusive`.

An absolute-safety basis is valid only for `condition=lean`, a task-registered
assertion ID, a non-null digest basis, and
`machine_assertion_passed=False`. A machine regression requires an
aggregate-complete pair with current `True` and lean `False`.

Decision reason codes are exact:

```text
absolute_lean_safety_regression
experiment_partial
masked_high_regression
machine_acceptance_regression
screening_thresholds_met
screening_thresholds_not_met
```

## 10. Decision-calculation digest

Optional fractions use JSON null or:

```json
{"numerator": 1, "denominator": 5}
```

The denominator is positive and the fraction is normalized.

`decision_calculation_digest` is SHA-256 over:

```text
{
  "analysis_contract_digest": contract.assertion_contract_digest,
  "comparative_aggregate_emitted":
      decision.comparative_aggregate_emitted,
  "document_type": "experiment_decision_calculation",
  "efficiency_medians": {
    "reported_tokens": <fraction-or-null>,
    "wall_time_milliseconds": <fraction-or-null>,
    "active_review_milliseconds": <fraction-or-null>
  },
  "median_correctness_delta": <fraction-or-null>,
  "outcome": decision.outcome,
  "plan_digest": dataset.plan_digest,
  "qualifying_efficiency_metrics":
      list(decision.qualifying_efficiency_metrics),
  "reason_code": decision.reason_code,
  "runtime_history_digest": dataset.runtime_history_digest,
  "schema_version": 1,
  "unmask_receipt_digest":
      dataset.masked_review.unmask_receipt_digest
}
```

A decision receipt is valid only when replay projects the source prefix,
`analyze_pairs()` regenerates the decision, both outcome values match, and the
calculation digest matches.

## 11. Analysis boundary

The exact signature is:

```python
def analyze_pairs(
    contract: AnalysisContract,
    dataset: ValidatedAnalysisDataset,
) -> ExperimentDecision:
    ...
```

It requires exact types, the module-owned dataset provenance identity, equal
plan digests, fixed contract constants, a recomputed assertion-contract
digest, four contract-ordered pairs, exact current/lean bindings, strict
scalar types, unique terminal receipt digests, registered assertion IDs, and
joint score/time and assertion/basis nullability.

Any violation raises:

```text
ExperimentPlanError("analysis_dataset_invalid")
```

`analyze_runtime_history()` recomputes `build_analysis_contract(plan)` and
requires exact equality with the supplied contract before projection.
Receipt/history failures remain receipt history errors rather than becoming
decisions.
