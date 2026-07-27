# Harness Experiment Task 8 Binding

Date: 2026-07-23

Status: normative for Task 8

This binding closes the static-evidence, base-profile, public-result, and
acquisition-authority gaps left open by the readiness plan and the Task 7
binding. Where those documents say that production
`experiment_receipts.py` remains unchanged, this binding supersedes that
statement: the public receipt schema and five-argument
`validate_static_receipt_graph()` signature remain unchanged, but the
validator must independently recompute all seven static-evidence digests.

## 1. Canonical digest rule

For every document in this binding:

```text
D(document) = sha256_bytes(canonical_bytes(document))
```

Every document has the exact key set shown below. JSON object-key ordering is
owned by the canonical serializer. Array ordering is semantic and must be
preserved as specified. No document contains an absolute path, local root,
inode, UID/GID, mode, dataclass representation, or caller-supplied digest.

Static-evidence projections are rebuilt only from a revalidated exact
`CanonicalExperimentInput`. The implementation must:

1. require the exact dataclass type and field set;
2. reproduce its authoritative bytes with `experiment_input_bytes()`;
3. reload those bytes with `load_experiment_input()`;
4. derive every document from that revalidated value without normalizing a
   malformed ordering.

The public result is:

```python
@dataclass(frozen=True)
class StaticEvidenceDigests:
    candidate_set_digest: str
    selection_seed_digest: str
    pilot_schedule_digest: str
    qualification_digest: str
    reference_result_digest: str
    mutation_sensitivity_digest: str
    difficulty_assignment_digest: str
```

The only public derivation entry point is:

```python
def derive_static_evidence_digests(
    experiment_input: CanonicalExperimentInput,
) -> StaticEvidenceDigests:
```

Document builders remain private. The producer, tests, and receipt validator
must not duplicate the derivation algorithm.

## 2. Base profile

`base_profile_digest` is the digest of this exact path-free projection:

```json
{
  "adapter_count": 16,
  "adapter_materialized_hash": "sha256:<64-lowercase-hex>",
  "adapter_source_hash": "sha256:<64-lowercase-hex>",
  "agents_hash": "sha256:<64-lowercase-hex>",
  "bundle_digest": "sha256:<64-lowercase-hex>",
  "bundle_id": "opaque-bundle-id",
  "checkout": {
    "materialized_hashes": {
      "skill-name": "sha256:<64-lowercase-hex>"
    },
    "object_format": "sha1",
    "plugin_blob_oid": "sha1:<40-lowercase-hex>",
    "plugin_manifest_hash": "sha256:<64-lowercase-hex>",
    "skill_hashes": {
      "skill-name": "sha256:<64-lowercase-hex>"
    },
    "skill_names": [
      "skill-name"
    ],
    "tree_hash": "sha1:<40-lowercase-hex>"
  },
  "common_role_hash": "sha256:<64-lowercase-hex>",
  "document_type": "harness-experiment-base-profile-v1",
  "home_digest": "sha256:<64-lowercase-hex>",
  "profile": "current",
  "role_count": 16,
  "schema_version": 1,
  "skill_routing_hash": "sha256:<64-lowercase-hex>"
}
```

The builder requires exact `HarnessManifest` and `CheckoutManifest` types and
field sets. `skill_names` preserves manifest order. Both checkout hash
mappings must have exactly the `skill_names` key set. Full object IDs must
be domain-qualified as `sha1:<40-lowercase-hex>` or
`sha256:<64-lowercase-hex>` and match the declared object format.

For the current/lean pair, these fields must be equal:

```text
bundle_id
bundle_digest
checkout
skill_routing_hash
adapter_source_hash
adapter_materialized_hash
common_role_hash
adapter_count
role_count
```

These values must be distinct:

```text
profile
agents_hash
home_digest
base_profile_digest
```

Profiles are exactly `current` and `lean`.

## 3. Candidate set

`candidate_set_digest` binds task identity and repository-relative write
authority:

```json
{
  "candidates": [
    {
      "allowed_write_paths": [
        "src/example.py"
      ],
      "commit_oid": "<40-or-64-lowercase-hex>",
      "task_id": "task-id"
    }
  ],
  "document_type": "harness-experiment-candidate-set-v1",
  "schema_version": 1
}
```

Candidate records preserve the authoritative candidate order, which the input
validator already requires to be ascending `task_id.encode("utf-8")`.
`allowed_write_paths` preserves its validated UTF-8-byte order. The helper
must not sort either sequence.

## 4. Selection seed

`selection_seed_digest` binds:

```json
{
  "document_type": "harness-experiment-selection-seed-v1",
  "schema_version": 1,
  "selection_seed": "<exact-ASCII-seed>"
}
```

The selection rule remains a separate exact field in the selection receipt.

## 5. Pilot schedule

`pilot_schedule_digest` binds:

```json
{
  "document_type": "harness-experiment-pilot-schedule-v1",
  "pilot_schedule": [
    {
      "condition": "current",
      "difficulty": "low",
      "ordinal": 1,
      "task_id": "task-id"
    }
  ],
  "schema_version": 1
}
```

The sequence is exactly the eight-record return from
`build_pilot_schedule()`, in ordinal order. It must not be reordered by task
ID or condition.

## 6. Reference results

`reference_result_digest` binds:

```json
{
  "document_type": "harness-experiment-reference-results-v1",
  "reference_results": [
    {
      "reference_result": "pass",
      "task_id": "task-id"
    }
  ],
  "schema_version": 1
}
```

Records preserve candidate order. These are operator-attested input claims,
not executed validator results.

## 7. Mutation sensitivity

`mutation_sensitivity_digest` binds:

```json
{
  "candidate_mutation_evidence": [
    {
      "assertion_digest": "sha256:<64-lowercase-hex>",
      "behavior_mutants": [
        {
          "category": "behavior-category",
          "mutant_digest": "sha256:<64-lowercase-hex>",
          "result": "fail"
        }
      ],
      "negative_controls": [
        {
          "control_id": "control-id",
          "result": "fail"
        }
      ],
      "task_id": "task-id",
      "validator_digest": "sha256:<64-lowercase-hex>"
    }
  ],
  "document_type": "harness-experiment-mutation-sensitivity-v1",
  "schema_version": 1
}
```

Outer records preserve candidate order. `negative_controls` and
`behavior_mutants` preserve their input order and are never sorted.
Validator and assertion digests are included so that mutation outcomes cannot
be rebound to different checking authority.

## 8. Difficulty assignments

`difficulty_assignment_digest` binds:

```json
{
  "difficulty_assignments": [
    {
      "difficulty": "low",
      "difficulty_rubric_digest": "sha256:<64-lowercase-hex>",
      "task_id": "task-id"
    }
  ],
  "document_type": "harness-experiment-difficulty-assignments-v1",
  "schema_version": 1
}
```

Records preserve candidate order and therefore bind each rubric to a task and
difficulty.

## 9. Qualification aggregate

`qualification_digest` is computed after the four referenced leaf digests and
binds:

```json
{
  "candidate_qualification_records": [
    {
      "absolute_safety_assertion_ids": [
        "assertion-id"
      ],
      "exclusion_rule_ids": [
        "rule-id"
      ],
      "inclusion_rule_ids": [
        "rule-id"
      ],
      "local_clone_policy": "remote_or_no_local_or_no_hardlinks",
      "offline_executable": true,
      "operator_attested": true,
      "provenance_id": "opaque-provenance-id",
      "source_provisioning_class": "operator_owned_trusted_git_local_clone",
      "task_id": "task-id"
    }
  ],
  "candidate_set_digest": "sha256:<64-lowercase-hex>",
  "difficulty_assignment_digest": "sha256:<64-lowercase-hex>",
  "document_type": "harness-experiment-qualification-v1",
  "mutation_sensitivity_digest": "sha256:<64-lowercase-hex>",
  "qualification_evidence_classification": "operator_attested_static",
  "reference_result_digest": "sha256:<64-lowercase-hex>",
  "schema_version": 1
}
```

Records preserve candidate order. Each nested identifier sequence preserves
its validated input order. Classification is exactly
`operator_attested_static`; Task 8 must not describe this evidence as executed
or independently verified.

## 10. Static receipt graph

`validate_static_receipt_graph()` keeps its existing five-argument public
signature and independently calls `derive_static_evidence_digests()`.

The selection payload must match:

```text
candidate_set_digest
selection_seed_digest
pilot_schedule_digest
selection_rule
selected_task_ids
```

The corpus payload must match:

```text
candidate_set_digest
qualification_digest
reference_result_digest
mutation_sensitivity_digest
difficulty_assignment_digest
qualification_evidence_classification
prompt_digests
validator_digests
assertion_digests
selected_snapshot_receipt_digests
selection_receipt_digest
```

The first five digest values above are compared to the shared derivation.
Rehashing internally consistent receipts around unrelated valid SHA-256
values must fail with the fixed receipt error.

## 11. Public result states

The nullable digest presence order is:

```text
bundle_digest
current_profile_digest
lean_profile_digest
task_corpus_receipt_digest
plan_digest
preflight_receipt_digest
```

Only these prefix masks are valid:

```text
000000
100000
110000
111000
111100
111110
111111
```

`111111` is only the success
`static_only/verified/removed/static_preflight_verified` result. Every blocked
result has no preflight receipt. `111110` is the completed-plan,
cleanup-required result. Qualification is `not_validated` before `111100` and
`operator_attested_static` from `111100` onward. Every returned result has
exact integer `model_calls=0`.
`not_started` is valid only with mask `000000`; any published digest proves
that `phase-a` creation already began.

`cleanup_state` is exactly:

```text
not_started       phase-a was never created
removed           a created phase-a tree passed verified cleanup
cleanup_required  an unacquired, replaced, uninspectable, or incompletely
                  removed tree remains quarantined
```

`reason_code` is one of:

```text
experiment_preflight_invalid
harness_preflight_blocked
task_snapshot_preflight_blocked
static_receipt_preflight_blocked
experiment_plan_preflight_blocked
task_snapshot_cleanup_required
static_preflight_verified
```

Downstream exception text is never copied into a public result.

## 12. Acquisition and quarantine linearization

Creation or return from a trusted callee does not itself grant Task 8 cleanup
authority over descendants.

For each returned harness home and task pair, the acquisition interval begins
before the callee call and ends only after:

1. all returned roots are scanned descriptor-relatively within fixed bounds;
2. every descendant stable token and exact inventory is validated;
3. task roots independently reproduce the captured file modes, sizes, full
   content digests, canonical materialized-tree digest, snapshot receipt, and
   per-condition root-identity digest without calling the public
   `TaskSnapshotMaterializer.verify()`;
4. the complete acquisition record is published to the outer ownership
   ledger as one logical commit.

Before that commit, cleanup-time scanning must not invent authority.

- A directory created directly by Task 8 is acquired only as the exact empty
  leaf and full metadata token first captured after `mkdir`; a descendant or
  post-capture replacement prevents publication.
- An ordinary `Exception` with a non-empty unacquired subtree preserves the
  whole `phase-a` tree, returns `cleanup_required`, and emits no preflight
  receipt.
- A non-`Exception` `BaseException` anywhere in the interval propagates
  without a public result or receipt and quarantines the whole tree.
- Once acquisition is committed, later ordinary failures use only the
  creation/acquisition ledger for identity-aware cleanup.
- Replacement, inspection, link-count, kind, inventory, or token failure
  during the read-only cleanup pass causes zero cleanup mutation and whole-tree
  preservation.
- A final cleanup failure after plan completion retains the five completed
  path-free digests and uses mask `111110`.
- A success preflight receipt is created only after complete identity-aware
  cleanup and uses mask `111111`.

The caller-owned empty `temp_parent` is never adopted or removed.
Its absolute chain is opened without following links. Every ancestor must be
root- or current-user-owned and non-writable by group/other, except for the
root-owned sticky-parent transition to a current-user-owned non-writable
child. The exact chain is reopened and compared before every path-based
materialization or harness verification boundary. Ancestor comparison binds
stable `dev/ino/uid/gid/kind/mode`; it intentionally does not bind mutable
directory inventory metadata such as size or timestamps. The exclusive
terminal retains its full-entry validation.

Every creation, acquisition, and final-cleanup scan uses these hard ceilings:

```text
total owned ledger entries: 1,000,000
maximum relative depth: 72
maximum component UTF-8 bytes: 255
maximum relative path UTF-8 bytes: 4096
```

Crossing a ceiling before ownership publication is an acquisition failure and
therefore quarantines any non-empty unacquired subtree.

## 13. Required adversarial coverage

Task 8 is not complete without:

- literal known-answer canonical documents independent of the production
  builder;
- seven per-field and one all-fields rebuilt-receipt forgery tests;
- domain-separation and order-preservation tests;
- validator/assertion/control/mutant mutation coverage;
- qualification subdigest mutation coverage;
- exact path-free base-profile projection and pair-invariant tests;
- all 64 result-presence combinations, accepting only the seven masks;
- rejection of every nonzero mask paired with `not_started`;
- untrusted writable-ancestor, intermediate-rebind, and gate-construction FD
  fault injection;
- unrelated shared-ancestor inventory mutation accepted without weakening
  stable identity, owner, or mode checks;
- direct-directory descendant injection and post-capture leaf replacement;
- same-length task content mutation and same-content/mode inode replacement,
  while retaining zero immediate public task `verify()` calls;
- compare-equal forged snapshot scalars or non-canonical receipts rejected
  before acquisition publication;
- descriptor close-fault injection proving exactly-once child close attempts
  and guaranteed parent-close traversal;
- returned-home and returned-pair scan/publication fault injection;
- ordinary-exception quarantine and non-`Exception` propagation tests;
- poison pills proving zero model, network, authentication, executable
  resolution, and unbounded recursive cleanup calls.

Known residuals remain explicit: negative-control IDs and cross-category
mutant digests are not newly required to be unique in schema v1; all
qualification evidence is operator-attested rather than executed; and
portable POSIX cannot atomically return the new directory descriptor from
`mkdir`, so replacement by another same-UID actor between `mkdir` and the
first no-follow descriptor/token capture remains outside the recoverable
ownership guarantee. The caller must provide an exclusive private
`temp_parent`.
