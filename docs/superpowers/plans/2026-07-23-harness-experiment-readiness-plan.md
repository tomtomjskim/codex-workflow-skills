# Harness Experiment Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved zero-model-call Phase A foundation for a future `current` versus `lean` harness experiment, with immutable plan identities, hardened task snapshots, synthetic telemetry and receipt validation, and a preflight-only CLI.

**Architecture:** Add a new experiment module family and CLI beside the legacy live-eval runner. Canonical plan bytes are authoritative, task repositories are read only through a separately hardened full-OID Git-object adapter, and retained outputs exclude absolute and host-local paths while the plan intentionally retains repository-relative allowed-write authority. Phase A materializes and verifies both harness profiles and distinct condition task trees, constructs two future canary templates and eight pilot invocation plans, validates future runtime transitions with synthetic records, emits a `static_only` preflight receipt, and contains no path that can authenticate or launch Codex.

**Tech Stack:** Python 3.9 standard library, existing canonical JSON and fixed harness public APIs, `unittest`, local temporary Git repositories, GitHub Actions.

**Design source:** `docs/superpowers/specs/2026-07-23-harness-experiment-readiness-design.md`

## Global Constraints

- Phase A supports only `preflight` and always reports `model_calls=0`.
- Do not import or call `build_invocation()`, `preflight_auth()`, `preflight_isolation()`, `run_eval()`, or `run_harness_dry_run()` from the new experiment path.
- Do not modify the public behavior, fields, serializers, argv, JSON bytes, classifications, reasons, or exit codes in:
  - `scripts/live_eval/isolation.py`
  - `scripts/live_eval/checkout.py`
  - `scripts/live_eval/harness.py`
  - `scripts/workflow_coordination/canonical_json.py`
  - `scripts/run_live_eval.py`
- Reuse only these existing public helpers:
  - `load_canonical_input()` and `canonical_bytes()`
  - `canonical_name_key()` and `require_unique_canonical_names()`
  - `load_harness_source()`, `materialize_harness_home()`, and `verify_loaded_harness()`
  - `seal_codex_home()`
- Do not import private helpers from the legacy modules. The task loader requires its own absolute-`--git-dir`, bounded subprocess, topology-seal, and no-worktree implementation.
- Use `@dataclass(frozen=True)`, tuples, `frozenset`, and `MappingProxyType` for immutable public values. Avoid Python features introduced after 3.9.
- Canonical input digests are SHA-256 over the exact accepted input bytes. A parsed-and-reserialized value is not a substitute for the authoritative bytes.
- Durable JSON contains no absolute or host-local paths, private policy text,
  prompts, raw model output, or raw reasoning. The canonical input and
  experiment plan intentionally retain validated repository-relative
  `allowed_write_paths`; other durable fields use opaque identifiers, fixed
  classifications, counts, and `sha256:` digests.
- No production dependency, model/API call, credential lookup, Codex/model/auth/validator executable resolution, network access, or live ledger write is permitted. The literal `git` executable used only by `task_snapshot.py`'s fixed, bounded adapter is the sole executable exception.
- Focused tests run after each small implementation slice. Full live-eval discovery runs at the task-snapshot security checkpoint and final integration; `./scripts/validate_repo.sh` runs only at branch-completion checkpoints.
- The untracked `.serena/` directory is user state and must not be staged or modified.

## File and Dependency Map

| File | Responsibility | Allowed dependencies |
|---|---|---|
| `scripts/live_eval/experiment_plan.py` | Exact canonical input schema, immutable plan, deterministic schedule, analysis rules, invocation-plan schema | `canonical_json`, Python standard library |
| `scripts/live_eval/experiment_receipts.py` | Static receipts, runtime record schemas, pure transition validator | `experiment_plan`, `experiment_telemetry`, `canonical_json`, standard library |
| `scripts/live_eval/experiment_telemetry.py` | Bounded JSONL parsing, usage projection, token and estimated-cost arithmetic | `experiment_plan`, `canonical_json`, standard library |
| `scripts/live_eval/task_snapshot.py` | Operator-source gate, object-DB inventory, bounded Git object loading, regular-file materialization and sealing | Public name-collision helpers, receipt constructors, standard library |
| `scripts/live_eval/experiment.py` | Phase A orchestration, owned temporary state, harness and task composition, preflight result | All four new core modules plus public harness and seal APIs |
| `scripts/run_harness_experiment.py` | Thin `preflight` CLI and canonical result printing | `experiment`, `experiment_plan`, standard library |
| `tests/fixtures/harness_experiment/` | Canonical plan and telemetry bytes that are safe to publish | No Git administration directories or private policy text |

Import direction is:

```text
experiment_plan -> experiment_telemetry
experiment_plan + experiment_telemetry -> experiment_receipts
experiment_plan + experiment_receipts -> task_snapshot
experiment_plan + experiment_telemetry + experiment_receipts + task_snapshot
  -> experiment -> run_harness_experiment
```

`experiment_plan.py` must not import receipts, telemetry, snapshots, orchestration, or either runner. This keeps plan identity free of runtime outcomes and prevents an import cycle.

## Exact Phase A Data Contract

The canonical plan input has these top-level keys and no others:

| Key | Contract |
|---|---|
| `schema_version` | integer `1`; boolean is invalid |
| `experiment_id` | NFC opaque identifier matching `[a-z0-9][a-z0-9._-]{0,63}` |
| `selection_seed` | NFC opaque string of 16 through 128 ASCII bytes |
| `selection_rule` | exact value `sha256-rank-paired-v1` |
| `model` | exact model object described below |
| `candidates` | exactly four qualified task objects, two `low` and two `medium`, with unique task IDs |
| `analysis_contract_version` | exact value `four-pair-screening-v1` |
| `masking_contract_version` | exact value `masked-review-chain-v1` |
| `containment_policy_version` | non-empty opaque identifier; Phase A records the policy but does not implement it |
| `retention` | exact value `{"durable_summary":"typed_allowlist","raw_jsonl":"discard"}` |
| `budgets` | exact budget object described below |
| `price_snapshot` | exact price object described below |
| `provider_cap_evidence` | `not_supplied`, `operator_attested_only`, or `independently_verified` |
| `external_prerequisite_receipt_digests` | sorted unique tuple of `sha256:` digests; Phase A permits an empty tuple |
| `invocation_policy` | exact future invocation policy described below |

The exact `model` object is:

```json
{"model_id":"gpt-5.6-sol","reasoning_effort":"high","required_cli_version":"0.145.0","required_cli_capability_policy":"codex-experiment-cli-v1"}
```

The fixture may use another non-empty NFC model ID, reasoning effort, or CLI version, but all four keys are required and unknown keys are rejected.

Each candidate has these keys and no others:

| Key | Contract |
|---|---|
| `task_id` | unique opaque identifier matching the experiment-ID pattern |
| `difficulty` | `low` or `medium` |
| `commit_oid` | full lowercase 40- or 64-hex object ID, never a ref |
| `prompt_digest` | `sha256:` digest |
| `validator_digest` | `sha256:` digest |
| `assertion_digest` | `sha256:` digest |
| `allowed_write_paths` | sorted unique literal repository-relative NFC paths |
| `provenance_id` | non-empty opaque NFC identifier |
| `inclusion_rule_ids` | non-empty sorted unique opaque identifiers |
| `exclusion_rule_ids` | sorted unique opaque identifiers |
| `offline_executable` | exact boolean `true` |
| `reference_result` | exact value `pass` |
| `negative_controls` | non-empty tuple of `{control_id, result}`, with `result=fail` |
| `behavior_mutants` | non-empty tuple of `{category, mutant_digest, result}`, with unique non-formatting categories, `sha256:` digest, and `result=fail` |
| `difficulty_rubric_digest` | `sha256:` digest |
| `qualification_evidence_classification` | exact value `operator_attested_static` |
| `source_provisioning_class` | exact value `operator_owned_trusted_git_local_clone` |
| `operator_attested` | exact boolean `true` |
| `local_clone_policy` | exact value `remote_or_no_local_or_no_hardlinks` |
| `absolute_safety_assertion_ids` | sorted unique opaque identifiers; may be empty |

The first pilot requires at least one behavioral mutant category per candidate. Categories equal to `formatting`, `parsing`, or `syntax_only` do not satisfy this requirement.

Phase A validates the shape, internal consistency, and plan binding of the qualification claims; it does not execute a private validator. `operator_attested_static` is therefore retained in the corpus receipt and preflight evidence. Independent validator execution remains blocked on the future containment backend.

The exact budget object keys are:

```text
total_calls
canary_calls
pilot_calls
retry_calls
concurrency
max_elapsed_seconds
max_retained_bytes
max_reported_tokens
max_output_tokens_per_call
max_estimated_cost_microunits
currency
```

The loader requires `10`, `2`, `8`, `0`, and `1` for the first five fields. Remaining counts are non-negative integers, with positive elapsed, token, and per-call output limits. `currency` is a three-letter uppercase ISO-style code and must equal the price snapshot currency.

The exact price snapshot keys are:

```text
model_id
currency
input_microunits_per_million
cached_input_microunits_per_million
output_microunits_per_million
effective_at
source_label
```

The model and currency must match their top-level counterparts. Rates are non-negative integers. Timestamp and source label are retained as opaque NFC strings; Phase A does not claim provider billing authority.

The exact invocation policy keys are:

```text
canary_sandbox
pilot_sandbox
approval_policy
ignore_user_config
ignore_rules
provider_transport_allowed
tool_network_disabled
web_search_disabled
mcp_disabled
plugins_disabled
hooks_disabled
skills_disabled
child_process_policy
validator_policy
executable_identity_policy
```

The required scalar values are `canary_sandbox=read-only`,
`pilot_sandbox=workspace-write`, and `approval_policy=never`. The nine exact
booleans are:

```json
{"hooks_disabled":true,"ignore_rules":true,"ignore_user_config":true,"mcp_disabled":true,"plugins_disabled":true,"provider_transport_allowed":true,"skills_disabled":true,"tool_network_disabled":true,"web_search_disabled":true}
```

Every value is an actual JSON boolean; integer `0` or `1` is invalid.
`provider_transport_allowed=true` reserves only a future approved model
transport. It does not authorize a Phase A connection and does not weaken
tool or validator network denial. `ignore_rules=true` refers only to
execpolicy `.rules`, never `AGENTS.md`. The final three policy identifiers
must be non-empty. The serializer binds reproducible content and policy
digests, never local path strings or transient filesystem identities.

---

## Task 1: Canonical Input, Immutable Plan, and Deterministic Schedule

**Files:**

- Create: `scripts/live_eval/experiment_plan.py`
- Create: `tests/test_live_eval_experiment_plan.py`
- Create: `tests/fixtures/harness_experiment/valid-plan-input.json`
- Create: `tests/fixtures/harness_experiment/analysis-boundaries.json`

### Step 1: Add failing authoritative-byte and schema tests

- [ ] Write tests proving:
  - the tracked valid input is already canonical UTF-8 JSON and round-trips byte-for-byte;
  - whitespace, reordered keys, duplicate keys, floats, non-NFC strings, and unknown or missing keys fail;
  - booleans fail in every integer field;
  - input `dict` and `list` values cannot be mutated after loading;
  - mutating each nested bound field changes the input digest;
  - task IDs, write paths, and receipt digests are unique and canonically sorted;
  - exactly two low and two medium candidates are required;
  - qualification fails for a reference failure, a passing negative control, or formatting-only mutants.
  - canary templates contain no marker, derived-home, executable, credential, or runtime-root identity;
  - pilot plans bind the root-capability policy digest, defer actual
    capability-root sets to Phase B runtime evidence, and reject an unknown or
    `danger-full-access` sandbox.

Use this test shape:

```python
class CanonicalExperimentInputTests(unittest.TestCase):
    def test_accepts_only_authoritative_canonical_bytes_and_freezes_nested_values(self):
        data = FIXTURE.joinpath("valid-plan-input.json").read_bytes()

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
```

### Step 2: Run the focused test and confirm the expected failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
```

Expected: import failure because `scripts.live_eval.experiment_plan` does not exist.

### Step 3: Implement the authoritative experiment codec

- [ ] Add the immutable wrapper and experiment-only freeze logic. Keep the legacy codec unchanged.

```python
@dataclass(frozen=True)
class CanonicalExperimentInput:
    canonical_bytes: bytes = field(repr=False)
    value: Mapping[str, object]
    input_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))
        object.__setattr__(self, "value", freeze_json_value(self.value))


def freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item) for item in value)
    return value


def thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_experiment_input(data: bytes) -> CanonicalExperimentInput:
    if not isinstance(data, bytes):
        raise ExperimentPlanError("experiment_input_not_bytes")
    value = load_canonical_input(data)
    if canonical_bytes(value) != data:
        raise ExperimentPlanError("experiment_input_not_canonical")
    _validate_experiment_document(value)
    return CanonicalExperimentInput(
        canonical_bytes=data,
        value=value,
        input_digest=sha256_bytes(data),
    )


def experiment_input_bytes(value: CanonicalExperimentInput) -> bytes:
    if not isinstance(value, CanonicalExperimentInput):
        raise TypeError("value must be CanonicalExperimentInput")
    encoded = canonical_bytes(thaw_json_value(value.value))
    if encoded != value.canonical_bytes:
        raise ExperimentPlanError("experiment_input_changed")
    return encoded
```

- [ ] Implement exact-schema validators with these properties:
  - `_require_mapping(value, exact_keys, label)` compares exact key sets;
  - `_require_integer()` checks `isinstance(value, int) and not isinstance(value, bool)`;
  - `_require_identifier()`, `_require_digest()`, `_require_full_oid()`, and `_require_relative_path()` enforce the contract above;
  - `_require_sorted_unique()` compares the provided order with a UTF-8 byte sort and rejects canonical aliases;
  - errors expose one fixed reason code, never the rejected value or local path.

### Step 4: Add immutable plan and invocation-plan types

- [ ] Implement these public types:

```python
@dataclass(frozen=True)
class PlannedRun:
    ordinal: int
    task_id: str
    difficulty: str
    condition: str


@dataclass(frozen=True)
class CanaryInvocationTemplate:
    ordinal: int
    profile: str
    model_id: str
    reasoning_effort: str
    sandbox: str
    approval_policy: str
    provider_transport_allowed: bool
    tool_network_disabled: bool
    base_profile_digest: str
    overlay_recipe_policy_digest: str
    root_capability_policy_digest: str
    child_process_policy: str
    validator_policy: str
    output_schema_digest: str
    environment_policy_digest: str
    argv_template_digest: str
    containment_policy_version: str


@dataclass(frozen=True)
class PilotInvocationPlan:
    ordinal: int
    run: PlannedRun
    snapshot_receipt_digest: str
    allowed_write_policy_digest: str
    base_profile_digest: str
    root_capability_policy_digest: str
    model_id: str
    reasoning_effort: str
    sandbox: str
    approval_policy: str
    provider_transport_allowed: bool
    tool_network_disabled: bool
    child_process_policy: str
    validator_policy: str
    output_schema_digest: str
    environment_policy_digest: str
    argv_template_digest: str
    containment_policy_version: str


@dataclass(frozen=True)
class ExperimentPlan:
    input_digest: str
    plan_document: Mapping[str, object]
    canonical_bytes: bytes = field(repr=False)
    plan_digest: str
    pilot_schedule: Tuple[PlannedRun, ...]
    canary_templates: Tuple[CanaryInvocationTemplate, ...]
    pilot_invocation_plans: Tuple[PilotInvocationPlan, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "plan_document", freeze_json_value(self.plan_document)
        )
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))
        object.__setattr__(self, "pilot_schedule", tuple(self.pilot_schedule))
        object.__setattr__(
            self, "canary_templates", tuple(self.canary_templates)
        )
        object.__setattr__(
            self,
            "pilot_invocation_plans",
            tuple(self.pilot_invocation_plans),
        )
```

- [ ] Expose this exact host-local-path-free builder:

```python
def build_experiment_plan(
    experiment_input: CanonicalExperimentInput,
    *,
    bundle_digest: str,
    current_profile_digest: str,
    lean_profile_digest: str,
    task_source_trust_receipt_digests: Sequence[str],
    task_selection_receipt_digest: str,
    task_corpus_receipt_digest: str,
    canary_templates: Sequence[CanaryInvocationTemplate],
    pilot_invocation_plans: Sequence[PilotInvocationPlan],
) -> ExperimentPlan:
```

The builder accepts no `Path`, caller-built `plan_document`, caller-supplied
policy/template/plan digest, separate pilot schedule, or call-allocation
digest. It recomputes the schedule from the authoritative frozen input,
requires the source-trust digest sequence to be UTF-8-byte sorted and unique,
and validates the child plans against that schedule. Canary ordinals/profiles
are exactly `(1, current)` and `(2, lean)`. Pilot plan ordinals are 3 through
10, embedded run ordinals are 1 through 8, and every pilot plan ordinal equals
its run ordinal plus two. The builder computes every child canonical digest
and the call-allocation digest itself.

`plan_document` contains the exact approved contracts plus:

```text
input_digest
bundle_digest
current_profile_digest
lean_profile_digest
task_source_trust_receipt_digests
task_selection_receipt_digest
task_corpus_receipt_digest
pilot_schedule
canary_template_digests
pilot_invocation_plan_digests
call_allocation_digest
```

It does not contain runtime containment, canary, pilot, review, unmask, stop, or decision outcomes.

The argv template includes `--ignore-user-config` and `--ignore-rules`. The latter is documented and tested as ignoring execpolicy `.rules`; it must not be represented as disabling `AGENTS.md`.

Canary templates use ordinals 1 and 2 for `current` and `lean`. They bind a
base profile and the approved overlay, root-capability, argv, and containment
policies, but not a marker, derived-home identity, executable identity, or
runtime task root. Those values exist only in future runtime child receipts
after Phase B approval. Pilot plans use ordinals 3 through 10 and carry the
corresponding scheduled task and condition plus stable snapshot,
allowed-write, base-profile, root-capability, argv, environment, and
containment policy digests. Actual derived-home/task/temp identities and
capability-root sets are Phase B runtime containment/child evidence.

The module owns the following exact V1 canonical policy documents. It hashes
each with `sha256_bytes(canonical_bytes(document))`; only the resulting digest
enters a template or pilot plan. A policy-document change changes the plan
digest.

`canary_overlay_recipe`:

```json
{"assembly_order":["base_bytes","prefix_utf8","runtime_marker","suffix_utf8"],"base_encoding":"utf-8","base_file_kind":"regular","document_type":"canary_overlay_recipe","marker":{"encoded_length":32,"encoding":"lowercase_hex","entropy_bits":128},"operation":"append_exact_utf8","prefix_utf8":"\n\nReturn this exact opaque canary marker in the required response field: ","preserve_base_bytes":true,"require_marker_absent_before_append":true,"required_derived_marker_occurrences":1,"schema_version":1,"suffix_utf8":"\n","target_relative_path":"AGENTS.md"}
```

`canary_response_schema`:

```json
{"$id":"urn:codex-workflow-skills:harness-experiment:canary-response:v1","additionalProperties":false,"properties":{"marker":{"maxLength":32,"minLength":32,"pattern":"^[0-9a-f]{32}$","type":"string"}},"required":["marker"],"type":"object"}
```

`pilot_response_schema`:

```json
{"$id":"urn:codex-workflow-skills:harness-experiment:pilot-response:v1","additionalProperties":false,"properties":{"status":{"enum":["completed","blocked"],"type":"string"},"summary":{"maxLength":2048,"minLength":1,"type":"string"}},"required":["status","summary"],"type":"object"}
```

The argv documents use typed atoms rather than magic string placeholders.
`runtime_executable_binding=future_child_receipt` means the document binds no
executable path or identity in Phase A. Slots are declarative only; Task 1
does not implement a renderer.

`canary_argv_template`:

```json
{"argv_tail":[{"literal":"-a"},{"literal":"never"},{"literal":"exec"},{"literal":"--json"},{"literal":"--strict-config"},{"literal":"--ephemeral"},{"literal":"--ignore-user-config"},{"literal":"--ignore-rules"},{"literal":"--sandbox"},{"literal":"read-only"},{"literal":"--model"},{"encoding":"one_argv_token","slot":"model_id"},{"literal":"--config"},{"encoding":"canonical_json_string_as_toml_basic_string","prefix":"model_reasoning_effort=","slot":"reasoning_effort"},{"literal":"--output-schema"},{"encoding":"one_argv_token","slot":"output_schema_path"},{"literal":"-"}],"document_type":"canary_argv_template","ignore_rules_semantics":"execpolicy_dot_rules_only","runtime_executable_binding":"future_child_receipt","schema_version":1,"stdin_utf8":"Return the exact opaque canary marker specified by the applicable global AGENTS.md instructions as JSON matching the output schema."}
```

`pilot_argv_template`:

```json
{"argv_tail":[{"literal":"-a"},{"literal":"never"},{"literal":"exec"},{"literal":"--json"},{"literal":"--strict-config"},{"literal":"--ephemeral"},{"literal":"--ignore-user-config"},{"literal":"--ignore-rules"},{"literal":"--sandbox"},{"literal":"workspace-write"},{"literal":"--model"},{"encoding":"one_argv_token","slot":"model_id"},{"literal":"--config"},{"encoding":"canonical_json_string_as_toml_basic_string","prefix":"model_reasoning_effort=","slot":"reasoning_effort"},{"literal":"--output-schema"},{"encoding":"one_argv_token","slot":"output_schema_path"},{"literal":"-"}],"document_type":"pilot_argv_template","ignore_rules_semantics":"execpolicy_dot_rules_only","runtime_executable_binding":"future_child_receipt","schema_version":1,"stdin_binding":"plan_candidate_prompt_digest"}
```

These argv tails match the locally verified pinned `codex-cli 0.145.0`
grammar: the root approval option precedes `exec`, while strict config,
ephemeral mode, user/rules isolation, sandbox, model, config, output schema,
and stdin selection are `exec` options. Runtime compatibility remains a
future capability-probe requirement; Phase A stores only this declarative
hash contract.

For the `reasoning_effort` atom, the exact argv token is
`"model_reasoning_effort=" + canonical_bytes(reasoning_effort).decode("utf-8")`.
Canonical JSON string encoding is also valid TOML basic-string encoding for
the already validated NFC, non-surrogate value, so quoting and escaping cannot
vary under one template digest. The pilot stdin binding resolves only to the
candidate `prompt_digest` already bound into the plan; Task 1 stores no raw
pilot prompt.

`environment_policy`:

```json
{"document_type":"experiment_environment_policy","hooks_disabled":true,"mcp_disabled":true,"parent_environment_inherited":false,"plugins_disabled":true,"provider_transport_allowed":true,"schema_version":1,"skills_disabled":true,"tool_credentials_allowed":false,"tool_network_disabled":true,"transport_credential_binding":"future_runtime_only","validator_network_disabled":true,"web_search_disabled":true}
```

`root_capability_policy`:

```json
{"canary_runtime_identities":"future_child_receipt_only","document_type":"root_capability_policy","implicit_roots_allowed":false,"required_sets":["tool_read_root_identity_digests","tool_write_root_identity_digests","validator_read_root_identity_digests","validator_write_root_identity_digests"],"root_identity_format":"sha256_prefixed_digest","schema_version":1,"sets_are_utf8_sorted_unique":true,"tool_and_validator_write_sets_disjoint":true,"tool_write_must_be_readable":true,"unlisted_host_roots_allowed":false,"validator_only_roots_visible_to_tool":false,"validator_write_must_be_readable":true}
```

The call-allocation digest is derived, not caller supplied. Its canonical
document is:

```json
{"canary_calls":2,"concurrency":1,"document_type":"call_allocation","pilot_calls":8,"retry_calls":0,"schema_version":1,"total_calls":10}
```

The builder rejects input-budget values that cannot produce this exact first
pilot allocation.

### Step 5: Implement deterministic pair scheduling

- [ ] Use SHA-256 ranking instead of `random.shuffle()` so ordering is stable across supported Python versions.

```python
def _rank(seed: str, domain: str, task_id: str) -> bytes:
    payload = {
        "domain": domain,
        "seed": seed,
        "task_id": task_id,
    }
    return hashlib.sha256(canonical_bytes(payload)).digest()


def build_pilot_schedule(
    seed: str, candidates: Sequence[Mapping[str, object]]
) -> Tuple[PlannedRun, ...]:
    by_difficulty = {"low": [], "medium": []}
    for candidate in candidates:
        by_difficulty[candidate["difficulty"]].append(candidate)
    orientations = {}
    for difficulty, values in by_difficulty.items():
        ordered = sorted(
            values,
            key=lambda item: _rank(
                seed, "condition-order:" + difficulty, item["task_id"]
            ),
        )
        orientations[ordered[0]["task_id"]] = ("current", "lean")
        orientations[ordered[1]["task_id"]] = ("lean", "current")
    pair_order = sorted(
        candidates,
        key=lambda item: _rank(seed, "pair-order", item["task_id"]),
    )
    runs = []
    for candidate in pair_order:
        for condition in orientations[candidate["task_id"]]:
            runs.append(
                PlannedRun(
                    ordinal=len(runs) + 1,
                    task_id=candidate["task_id"],
                    difficulty=candidate["difficulty"],
                    condition=condition,
                )
            )
    return tuple(runs)
```

- [ ] Test pair adjacency, ordinals 1 through 8, one current-first and one lean-first task per stratum, deterministic repetition, and a changed seed changing the sealed order.

### Step 6: Run focused tests and commit the slice

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
git diff --check
```

- [ ] Commit:

```bash
git add scripts/live_eval/experiment_plan.py tests/test_live_eval_experiment_plan.py tests/fixtures/harness_experiment/valid-plan-input.json tests/fixtures/harness_experiment/analysis-boundaries.json
git commit -m "feat(eval): add immutable experiment plans"
```

---

## Task 2: Exact-Rational Analysis and Decision Rules

**Files:**

- Modify: `scripts/live_eval/experiment_plan.py`
- Modify: `tests/test_live_eval_experiment_plan.py`
- Modify: `tests/fixtures/harness_experiment/analysis-boundaries.json`

### Step 1: Add failing analysis boundary tests

- [ ] Add table-driven tests for:
  - correctness delta is `lean - current`;
  - four-value median is the arithmetic mean of the middle two values;
  - `_reduction()` rejects a zero or negative current baseline;
  - exactly 20 percent passes and the adjacent exact fractions fall on the correct side;
  - display rounding never changes a scalar threshold result.

Use `fractions.Fraction` in expectations:

```python
with self.subTest(case=case["name"]):
    reduction = _reduction(case["current"], case["lean"])
    self.assertEqual(reduction, Fraction(*case["expected_fraction"]))
```

`analysis-boundaries.json` contains only scalar integer inputs and expected
fractions. It must not serialize or directly construct a
`ValidatedAnalysisDataset`. Receipt-source decision tests begin in Task 4
after `project_analysis_dataset()` exists.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
```

Expected: analysis types and functions are missing.

### Step 3: Implement the fixed analysis types

- [ ] Add:

```python
@dataclass(frozen=True)
class ValidatedConditionObservation:
    task_id: str
    condition: str
    terminal_receipt_digest: str
    correctness_score: Optional[int]
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    wall_time_milliseconds: int
    active_review_milliseconds: Optional[int]
    machine_assertion_passed: bool
    absolute_safety_assertion_id: Optional[str]
    absolute_safety_basis_digest: Optional[str]

    @property
    def reported_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ValidatedPairObservation:
    task_id: str
    current: Optional[ValidatedConditionObservation]
    lean: Optional[ValidatedConditionObservation]


@dataclass(frozen=True)
class ValidatedMaskedReviewEvidence:
    packet_receipt_digest: str
    score_lock_receipt_digest: str
    unmask_receipt_digest: str
    high_regression_basis_digest: Optional[str]


@dataclass(frozen=True)
class ValidatedAnalysisDataset:
    plan_digest: str
    runtime_history_digest: str
    pairs: Tuple[ValidatedPairObservation, ...]
    masked_review: Optional[ValidatedMaskedReviewEvidence]
    partial_reason_codes: Tuple[str, ...]
    _provenance: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pairs", tuple(self.pairs))
        object.__setattr__(
            self, "partial_reason_codes", tuple(self.partial_reason_codes)
        )


@dataclass(frozen=True)
class ExperimentDecision:
    outcome: str
    comparative_aggregate_emitted: bool
    median_correctness_delta: Optional[Fraction]
    efficiency_medians: Mapping[str, Optional[Fraction]]
    qualifying_efficiency_metrics: Tuple[str, ...]
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "efficiency_medians",
            MappingProxyType(dict(self.efficiency_medians)),
        )
        object.__setattr__(
            self,
            "qualifying_efficiency_metrics",
            tuple(self.qualifying_efficiency_metrics),
        )
```

- [ ] Add the exact plan-derived analysis contract API:

```python
@dataclass(frozen=True)
class TaskAssertionContract:
    task_id: str
    assertion_digest: str
    absolute_safety_assertion_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "absolute_safety_assertion_ids",
            tuple(self.absolute_safety_assertion_ids),
        )


@dataclass(frozen=True)
class AnalysisContract:
    contract_version: str
    plan_digest: str
    assertion_contract_digest: str
    task_assertions: Tuple[TaskAssertionContract, ...]
    required_pair_count: int
    score_minimum: int
    score_maximum: int
    minimum_median_correctness_delta: Fraction
    efficiency_reduction_threshold: Fraction
    required_efficiency_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_assertions", tuple(self.task_assertions))


def build_analysis_contract(plan: ExperimentPlan) -> AnalysisContract:
    ...
```

`build_analysis_contract()` accepts no override arguments. It requires exact
`ExperimentPlan` type, verifies canonical plan bytes and digest, verifies the
eight-run/four-adjacent-pair schedule, and uses each pair's first occurrence as
task order. That order must exactly cover the four candidate tasks. It projects
each candidate's `task_id`, `assertion_digest`, and sorted
`absolute_safety_assertion_ids` into `TaskAssertionContract`.
Before deriving that order, it projects the public `pilot_schedule` through
the same `_planned_run_document()` representation and requires exact equality
with `plan.plan_document["pilot_schedule"]`. A public schedule paired with
different embedded canonical bytes is invalid even when both are independently
well formed.

The assertion contract digest is SHA-256 over this canonical document:

```python
{
    "contract_version": "four-pair-screening-v1",
    "document_type": "analysis_assertion_contract",
    "plan_digest": plan.plan_digest,
    "schema_version": 1,
    "tasks": [
        {
            "task_id": item.task_id,
            "assertion_digest": item.assertion_digest,
            "absolute_safety_assertion_ids": list(
                item.absolute_safety_assertion_ids
            ),
        }
        for item in task_assertions
    ],
}
```

The returned fixed values are `contract_version=four-pair-screening-v1`,
`required_pair_count=4`, `score_minimum=0`, `score_maximum=100`,
`minimum_median_correctness_delta=Fraction(-5, 1)`,
`efficiency_reduction_threshold=Fraction(1, 5)`, and
`required_efficiency_count=2`. A malformed plan, schedule/candidate mismatch,
or canonical/digest mismatch raises
`ExperimentPlanError("experiment_plan_invalid")`.

- [ ] Add the exact module-private dataset factory:

```python
_ANALYSIS_DATASET_PROVENANCE = object()


def _make_validated_analysis_dataset(
    *,
    plan_digest: str,
    runtime_history_digest: str,
    pairs: Sequence[ValidatedPairObservation],
    masked_review: Optional[ValidatedMaskedReviewEvidence],
    partial_reason_codes: Sequence[str],
) -> ValidatedAnalysisDataset:
    return ValidatedAnalysisDataset(
        plan_digest=plan_digest,
        runtime_history_digest=runtime_history_digest,
        pairs=tuple(pairs),
        masked_review=masked_review,
        partial_reason_codes=tuple(partial_reason_codes),
        _provenance=_ANALYSIS_DATASET_PROVENANCE,
    )
```

The factory exposes no provenance argument and performs tuple detachment plus
token injection only. It does not establish receipt authenticity or trust any
score, usage, duration, machine result, assertion, or validity value. Task 4's
`experiment_receipts.project_analysis_dataset()` is the only supported public
producer and calls this factory only after canonical replay. The provenance
identity is a misuse barrier, not a security boundary.

`analyze_pairs()` is entirely absent in Task 2—no stub or placeholder is
added. Task 4 implements it only after receipt projection exists, and then
requires the provenance identity plus full contract/plan/dataset
cross-validation.

For each completed-terminal observation, `correctness_score` and
`active_review_milliseconds` are jointly null only on a replayed stop branch
before score lock. They are jointly non-null only after a valid score-lock and
unmask chain. Mixed nullability is invalid. A missing or non-completed terminal
is represented by an absent condition observation, not by zero token/time or a
partially populated observation.

### Step 4: Implement exact-rational primitives

- [ ] Implement:

```python
def _median(values: Sequence[Fraction]) -> Fraction:
    ordered = sorted(values)
    if len(ordered) != 4:
        raise ExperimentPlanError("analysis_requires_four_pairs")
    return (ordered[1] + ordered[2]) / 2


def _reduction(current: int, lean: int) -> Fraction:
    if current <= 0:
        raise ExperimentPlanError("analysis_baseline_not_positive")
    return Fraction(current - lean, current)
```

- [ ] Add `_correctness_delta(current, lean)` as exact integer subtraction and
  keep every threshold comparison in integer or `Fraction` space. Decision
  precedence and complete-dataset aggregation are implemented and tested in
  Task 4 only after receipt projection exists.

### Step 5: Run focused tests and commit

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
git diff --check
```

- [ ] Commit:

```bash
git add scripts/live_eval/experiment_plan.py tests/test_live_eval_experiment_plan.py tests/fixtures/harness_experiment/analysis-boundaries.json
git commit -m "feat(eval): add experiment analysis primitives"
```

---

## Task 3: Canonical Typed Telemetry Summary Codec

### Binding clarification

- Task 3 summaries represent only a successfully completed terminal
  projection. `classification` is exactly `completed`,
  `response_digest` is a non-null lowercase
  `sha256:<64 hexadecimal characters>` value, and `event_count` is an exact
  non-boolean integer greater than or equal to one.
- `price_snapshot` has exactly the seven Task 1 price-snapshot keys. Its three
  rates are exact non-boolean non-negative integers; `model_id`,
  `effective_at`, and `source_label` are non-empty NFC strings; and `currency`
  is three uppercase ASCII letters. Task 3 does not repeat the Task 1
  model/currency cross-object equality checks because those counterpart
  values are not arguments to the codec.
- Define `TelemetryError(ValueError)`. Any Task 3 typed-value, document,
  price-snapshot, serialization, or digest validation failure raises exactly
  `TelemetryError("telemetry_summary_invalid")`.
- `telemetry_summary_document()` and `telemetry_summary_digest()` accept only
  exact `TelemetrySummary` and nested exact `UsageSummary` instances. They
  revalidate intrinsic scalar types, non-negativity, subset and total
  invariants, the fixed classification, digest, positive event count, and
  `raw_retention=discard` before serialization.
- Price-bound estimated-cost equality is validated by
  `telemetry_summary_from_document()`, the only Task 3 API that receives a
  price snapshot. The serializer and digest API still require a non-negative
  integer retained estimate but cannot independently recompute it without
  changing their approved signatures. Task 5 must construct parser output
  through `telemetry_summary_from_document()` rather than treating a directly
  constructed dataclass as validated.
- This task adds no JSONL parsing, event-order state machine, size limits, or
  Task 5 transport error codes.

**Files:**

- Create: `scripts/live_eval/experiment_telemetry.py`
- Create: `tests/test_live_eval_experiment_telemetry.py`

### Step 1: Add failing typed-summary codec tests

- [ ] Test:
  - `UsageSummary` contains exactly four provider counts, total reported
    tokens, and the estimated cost;
  - booleans, negative values, floats, strings, unknown keys, missing keys,
    cached input greater than input, and reasoning output greater than output
    fail;
  - total reported tokens must equal input plus output;
  - estimated cost applies cached, non-cached input, and output rates exactly
    once and rejects a mismatched retained estimate;
  - `raw_retention` is exactly `discard`;
  - the typed document and nested usage mapping have exact keys and contain no
    arbitrary event value or raw JSONL;
  - semantically equal summaries produce byte-identical documents and equal
    digests;
  - changing any typed field changes the summary digest;
  - `telemetry_summary_from_document()` returns deeply immutable typed values
    and round-trips to the exact same document and digest.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_telemetry -v
```

Expected: import failure because the telemetry module does not exist.

### Step 3: Implement the typed values

- [ ] Add:

```python
@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_reported_tokens: int
    estimated_cost_microunits: int


@dataclass(frozen=True)
class TelemetrySummary:
    classification: str
    response_digest: str
    usage: UsageSummary
    event_count: int
    raw_retention: str
```

`raw_retention` is always `discard` in Phase A.

### Step 4: Implement canonical document, digest, and validation boundaries

- [ ] Add `telemetry_summary_document(summary)` returning an exact-schema
  mapping with `classification`, `response_digest`, `usage`, `event_count`,
  and `raw_retention`. The nested `usage` mapping has exactly
  `input_tokens`, `cached_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`, `total_reported_tokens`, and
  `estimated_cost_microunits`.
- [ ] Add:

```python
def telemetry_summary_from_document(
    document: Mapping[str, object],
    price_snapshot: Mapping[str, object],
) -> TelemetrySummary:
    """Validate exact keys, integer/non-bool fields, subset invariants,
    total_reported_tokens, and estimated cost, then return the typed value."""
```

- [ ] Add `telemetry_summary_digest(summary)` as SHA-256 over
  `canonical_bytes(telemetry_summary_document(summary))`.
- [ ] Use integer micro-units per million tokens and round a fractional final
  micro-unit upward:

```python
def _estimated_cost_microunits(
    usage: Mapping[str, int], price: Mapping[str, object]
) -> int:
    non_cached = usage["input_tokens"] - usage["cached_input_tokens"]
    numerator = (
        non_cached * price["input_microunits_per_million"]
        + usage["cached_input_tokens"]
        * price["cached_input_microunits_per_million"]
        + usage["output_tokens"] * price["output_microunits_per_million"]
    )
    return (numerator + 999999) // 1000000
```

`reasoning_output_tokens` is retained as a diagnostic subset but does not
enter the cost expression separately.

### Step 5: Run focused tests and commit

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_telemetry -v
git diff --check
```

- [ ] Commit:

```bash
git add scripts/live_eval/experiment_telemetry.py tests/test_live_eval_experiment_telemetry.py
git commit -m "feat(eval): add canonical telemetry summaries"
```

---

## Task 4: Canonical Receipts and Pure Runtime Transition Validation

### Binding clarification

The normative ambiguity-resolution contract for this task is:

`docs/superpowers/specs/2026-07-23-harness-experiment-task4-binding.md`

Read and apply it in full. It closes the plan-bound snapshot seam, envelope
and error rules, runtime recovery/stop conflict, nested digest documents,
analysis-source identity, projection joins, and deterministic decision output.
For Task 4, that document takes precedence where this task's prose is
underspecified. It does not expand Phase A into live execution.

**Files:**

- Create: `scripts/live_eval/experiment_receipts.py`
- Create: `tests/test_live_eval_experiment_receipts.py`

### Step 1: Add failing receipt mutation and state-machine tests

- [ ] Cover static receipt construction:
  - every receipt is canonical, deeply immutable, and has a digest over its exact bytes;
  - one-field mutation changes the receipt digest;
  - `TaskCorpusReceipt` binds all four selected snapshot digests and the selection receipt;
  - the plan binds source-trust, selection, and corpus receipt digests;
  - a post-result candidate replacement cannot reuse a plan digest;
  - plan-independent component receipts may be reused only with the same authoritative input and component-policy digests;
  - changed-input static receipts and cross-plan preflight/runtime, cross-task, cross-profile, stale, duplicate, and hash-invalid receipts fail.

- [ ] Cover runtime transitions:
  - `PreflightReceipt -> RuntimeContainmentReceipt`;
  - exactly one canary reservation/terminal/receipt per profile before pilot;
  - exactly eight pilot reservations and terminals in the plan's schedule;
  - third canary, duplicate profile, pilot before both canaries, ninth pilot, duplicate task-condition, retry, and eleventh total reservation fail;
  - every reservation consumes allowance regardless of terminal classification;
  - a missing terminal permits only `abandoned_after_recovery` followed by stop;
  - failed, timed-out, crashed, malformed, or abandoned terminal blocks new reservations;
  - packet, score lock, unmask, and decision order is one-way;
  - every post-reservation nonterminal state can append a valid stop;
  - stop or decision is the unique terminal and no later record is accepted;
  - stop rejects `advance_to_larger_study`;
  - stop permits `reject_for_safety` only with an already valid basis.

- [ ] Cover analysis-source and decision behavior with canonical histories:
  - rebuild a valid outer pilot-terminal receipt after changing a nested token
    value while retaining the prior telemetry-summary digest;
  - independently rebuild valid outer score-lock receipts after changing
    `correctness_score` and `active_review_milliseconds` while retaining the
    prior locked-records digest;
  - independently rebuild valid outer unmask receipts after changing
    `task_id`, `condition`, and `pilot_terminal_receipt_digest` while retaining
    the prior mapping digest;
  - require both `replay_runtime_history()` and
    `project_analysis_dataset()` to reject every one of those mutations;
  - reject an unregistered absolute-safety assertion ID, a task-A assertion ID
    attached to task B, and a non-null safety basis with a null assertion ID;
  - project a valid stop before score lock with jointly null score/review time
    and an explicit partial reason, then require `inconclusive`, no comparative
    aggregate, and all aggregate fields null;
  - project a complete eight-terminal packet/score-lock/unmask history and
    calculate the expected medians only from its receipt-carried values;
  - reject a raw mapping, direct pair sequence, arbitrary provenance token,
    and caller-constructed dataset at the analysis boundary.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_receipts -v
```

Expected: import failure because the receipt module does not exist.

### Step 3: Implement the canonical receipt envelope

- [ ] Use one immutable envelope with exact type-specific payload validation:
- [ ] Import `freeze_json_value`, `thaw_json_value`, and `sha256_bytes` from `experiment_plan.py`; these are the only shared experiment codec primitives.
- [ ] Import `telemetry_summary_from_document()`,
  `telemetry_summary_document()`, and `telemetry_summary_digest()` from
  `experiment_telemetry.py`; do not duplicate its schema or cost checks.

```python
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
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))


def make_receipt(
    receipt_type: str,
    input_digest: str,
    plan_digest: Optional[str],
    previous_record_hash: Optional[str],
    payload: Mapping[str, object],
) -> CanonicalReceipt:
    _validate_receipt_payload(receipt_type, payload)
    document = {
        "schema_version": 1,
        "receipt_type": receipt_type,
        "input_digest": input_digest,
        "plan_digest": plan_digest,
        "previous_record_hash": previous_record_hash,
        "payload": thaw_json_value(payload),
    }
    encoded = canonical_bytes(document)
    return CanonicalReceipt(
        receipt_type=receipt_type,
        input_digest=input_digest,
        plan_digest=plan_digest,
        previous_record_hash=previous_record_hash,
        payload=payload,
        canonical_bytes=encoded,
        receipt_digest=sha256_bytes(encoded),
    )
```

Static component receipts require the authoritative experiment input digest and a null plan digest because they are inputs to plan construction. `preflight` and every runtime receipt require both the unchanged input digest and final plan digest. Only runtime children carry `previous_record_hash`.

Static receipt types are:

```text
task_source_trust
task_snapshot
task_selection
task_corpus
preflight
```

Runtime receipt types are:

```text
runtime_containment
canary_reservation
canary_terminal
canary_receipt
pilot_reservation
pilot_terminal
masked_review_packet
score_lock
unmask
decision
experiment_stop
```

### Step 4: Implement exact static payloads

- [ ] Require these path-free fields:

`task_source_trust`:

```text
task_id
provisioning_class
operator_attested
local_clone_policy
object_format
source_identity_before_digest
object_topology_before_digest
source_identity_after_digest
object_topology_after_digest
git_process_policy_digest
inventory_file_count
inventory_total_bytes
```

`task_snapshot`:

```text
task_id
object_format
commit_oid
tree_oid
entry_digest
materialized_tree_digest
materializer_policy_version
file_count
total_bytes
source_trust_receipt_digest
```

`task_selection`:

```text
candidate_set_digest
selection_rule
selection_seed_digest
selected_task_ids
pilot_schedule_digest
```

`task_corpus`:

```text
candidate_set_digest
selection_receipt_digest
selected_snapshot_receipt_digests
qualification_digest
qualification_evidence_classification
prompt_digests
validator_digests
assertion_digests
reference_result_digest
mutation_sensitivity_digest
difficulty_assignment_digest
```

`preflight`:

```text
evidence_state
live_backend_state
global_agents_marker_state
pilot_state
qualification_evidence_classification
model_calls
bundle_digest
current_profile_digest
lean_profile_digest
task_corpus_receipt_digest
experiment_plan_digest
canary_template_set_digest
pilot_invocation_plan_set_digest
materialization_result
cleanup_state
```

The preflight values must include `static_only`, `live_backend_not_implemented`, `global_agents_marker_not_run`, `pilot_not_run`, `operator_attested_static`, and integer `0` for the corresponding fields above.

### Step 5: Implement the runtime envelope and transition validator

- [ ] A runtime record always binds the unchanged plan digest and the preceding receipt digest. The first runtime record points to the `PreflightReceipt` digest.

```python
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


def validate_runtime_transition(
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    history: Sequence[CanonicalReceipt],
    candidate: CanonicalReceipt,
) -> RuntimeState:
    state = replay_runtime_history(plan, preflight, history)
    _require_record_hash(candidate, history[-1] if history else preflight)
    _require_unchanged_input(candidate, plan.input_digest)
    _require_unchanged_plan(candidate, plan.plan_digest)
    _require_allowed_next(state, candidate)
    return _apply_runtime_record(plan, state, candidate)
```

- [ ] Represent canary order as `("current", "lean")` and pilot order as the eight `PlannedRun` entries bound into the plan. Reservation IDs are opaque and unique.
- [ ] Validate every canary profile, base-profile digest, and template digest against the next plan-bound template. Validate every pilot task, condition, snapshot digest, and invocation-plan digest against the next scheduled pilot plan; do not trust matching reservation IDs alone.
- [ ] Terminal statuses are exactly `completed`, `failed`, `timed_out`, `crashed`, or `abandoned_after_recovery`.
- [ ] A non-`completed` terminal changes the only allowed next record to `experiment_stop`.
- [ ] Require these exact runtime payload fields:

`runtime_containment`:

```text
backend_identity_digest
model_tool_capability_receipt_digest
validator_capability_receipt_digest
probe_set_digest
result
```

`result` must be `pass`; a failed or incomplete containment probe cannot enter the runtime chain.

`canary_reservation`:

```text
reservation_id
profile
base_profile_digest
canary_template_digest
```

`canary_terminal`:

```text
reservation_id
profile
reservation_receipt_digest
terminal_status
telemetry_summary_digest
response_digest
```

Completed canary terminals require both digests. Non-completed terminals retain the exact keys with null digest values and immediately force the stop branch.

`canary_receipt`:

```text
reservation_id
profile
terminal_receipt_digest
base_bundle_digest
base_profile_digest
canary_overlay_recipe_digest
marker_digest
marker_entropy_bits
marker_occurrence_count
marker_occurrence_receipt_digest
derived_home_digest
model_policy_digest
containment_capability_digest
```

The validator requires a distinct marker digest per profile, at least 128 entropy bits, and an occurrence count of one in the derived global `AGENTS.md`; no raw marker enters a public receipt.

`pilot_reservation`:

```text
reservation_id
task_id
condition
snapshot_receipt_digest
invocation_plan_digest
```

`pilot_terminal`:

```text
reservation_id
task_id
condition
reservation_receipt_digest
terminal_status
telemetry_summary
telemetry_summary_digest
wall_time_milliseconds
machine_assertion_result
sanitized_diff_digest
allowed_write_inventory_digest
absolute_safety_assertion_id
absolute_safety_basis_digest
```

`telemetry_summary` is the exact typed document defined in Task 3, not caller-selected aggregate values. Receipt replay reconstructs it through `telemetry_summary_from_document()` using the plan's price snapshot, regenerates the document, and compares both the exact document and `telemetry_summary_digest()`. `wall_time_milliseconds` is a non-negative integer captured by the runner. `machine_assertion_result` is exactly `pass` or `fail`. The assertion-ID and safety-basis fields must be both null or both non-null; a non-null ID must be registered for this task in the plan-bound assertion contract. All other digest fields are required for a `completed` terminal.

`masked_review_packet`:

```text
eligible_pilot_terminal_digests
packet_digest
randomized_order
leakage_scan_result
rubric_digest
reviewer_ids
```

`score_lock`:

```text
masked_packet_receipt_digest
locked_score_records
locked_score_records_digest
review_findings_digest
high_regression_basis_digest
```

Each `locked_score_records` entry has exactly `neutral_id`, `correctness_score`, and `active_review_milliseconds`. Neutral IDs are unique and exactly cover the packet's randomized order, correctness is a 0-to-100 integer, and active time is a non-negative integer. The records digest is SHA-256 over the canonical records document. The HIGH-basis field may be null.

`unmask`:

```text
score_lock_receipt_digest
condition_mapping_records
condition_mapping_digest
```

Each `condition_mapping_records` entry has exactly `neutral_id`, `task_id`, `condition`, and `pilot_terminal_receipt_digest`. The records are unique, exactly cover the score lock, and must reconstruct the eight plan-scheduled task-condition-terminal bindings. The mapping digest is SHA-256 over the canonical records document.

`decision`:

```text
unmask_receipt_digest
decision_calculation_digest
outcome
```

`experiment_stop`:

```text
stop_stage
reason_code
consumed_canary_reservations
consumed_pilot_reservations
consumed_total_reservations
unresolved_reservation_ids
supporting_evidence_receipt_digests
safety_basis_digest
outcome
```

- [ ] Require the packet leakage result to be `pass`; bind the eight eligible pilot terminal digests, randomized order, rubric, and opaque reviewers.
- [ ] Require score lock after the packet, unmask after score lock, and decision after unmask. Recompute and compare the canonical nested telemetry, locked-score, and unmask-mapping digests during replay; a valid outer receipt digest never substitutes for those semantic checks.
- [ ] Permit a human HIGH basis on the stop branch only after the complete packet, score-lock, and unmask chain. Permit a deterministic absolute lean safety basis from a valid completed terminal even when another pair is partial.
- [ ] Require one of the three allowed outcomes for `decision`. Require `inconclusive` or a valid `reject_for_safety` basis for `experiment_stop`; reject `advance_to_larger_study` on the stop branch.
- [ ] Implement `project_analysis_dataset(plan, preflight, history)` in this module. It accepts no score, duration, usage, machine-result, assertion, or validity arguments. It first replays the canonical chain, then:
  1. reconstructs each terminal's nested telemetry summary through Task 3's exact validator, verifies the regenerated document and digest, and reads its token counts plus the terminal's wall time, machine result, assertion ID, and safety basis;
  2. validates any non-null assertion ID against the task's registered plan-bound assertion and requires the ID/basis pair to be jointly null or non-null;
  3. recomputes the canonical locked-score records digest and, after unmask, joins each neutral ID to exactly one plan-scheduled terminal through the canonical condition mapping;
  4. emits `ValidatedConditionObservation` values with nullable score/review time only for a stop branch before score lock, four plan-ordered pairs, the complete masked-review chain when present, explicit partial reasons, the plan digest, and the canonical replayed-history digest.
- [ ] Call the plan-owned `_make_validated_analysis_dataset(...)` only after
  those checks. `project_analysis_dataset()` is the only supported public
  producer; the substantive trust comes from canonical replay and nested
  validation, while the plan-owned identity token is only a misuse barrier.
- [ ] Add positive assertions proving the projected scores, token counts, wall
  times, active-review times, machine results, task/condition bindings, and
  registered absolute-safety evidence equal the receipt history and require no
  caller observation object.

### Step 6: Bind projected evidence to the decision engine

- [ ] Implement `analyze_pairs()` in `experiment_plan.py`. It accepts only a
  `ValidatedAnalysisDataset` carrying the plan-owned provenance identity. It
  accepts no caller-supplied score, token, time, machine-result, validity
  boolean, or safety digest. It validates the contract/plan digest, pair
  cardinality and order, task-condition-terminal bindings, joint score/time
  nullability, and registered assertion IDs before applying a decision.
- [ ] Apply decision precedence in this exact order:
  1. a valid lean absolute-safety basis whose assertion ID and assertion digest
     were pre-registered returns `reject_for_safety`, including when another
     pair is partial;
  2. any missing validated terminal, partial reason, missing complete masked
     chain, or unresolved abstention returns `inconclusive` with all aggregate
     fields absent;
  3. a validated masked-review HIGH basis or complete-pair
     current-pass/lean-fail returns `reject_for_safety`;
  4. otherwise calculate four-pair aggregates and return
     `advance_to_larger_study` only when correctness and two of three
     efficiency thresholds pass;
  5. every other complete result returns `inconclusive`.
- [ ] Compute an efficiency median only when all four current baselines are
  positive. Represent an ineligible metric as `None`; it does not count.
- [ ] Add:

```python
def analyze_runtime_history(
    contract: AnalysisContract,
    plan: ExperimentPlan,
    preflight: CanonicalReceipt,
    history: Sequence[CanonicalReceipt],
) -> ExperimentDecision:
    return analyze_pairs(
        contract,
        project_analysis_dataset(plan, preflight, history),
    )
```

This history-only wrapper is the supported public decision entry point.
Direct `analyze_pairs()` use is limited to module tests of already projected
values.

### Step 7: Run focused tests and commit

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_receipts -v
git diff --check
```

- [ ] Commit:

```bash
git add scripts/live_eval/experiment_plan.py scripts/live_eval/experiment_receipts.py tests/test_live_eval_experiment_plan.py tests/test_live_eval_experiment_receipts.py
git commit -m "feat(eval): add experiment receipt transitions"
```

---

## Task 5: Bounded Codex JSONL Parsing and Terminal Projection

**Normative clarification:**
`docs/superpowers/specs/2026-07-23-harness-experiment-task5-binding.md`.
It supersedes this task wherever the original four-field usage sketch or
underspecified event lifecycle conflicts with the pinned five-field,
zero-cache-write source-shaped subset.

**Files:**

- Modify: `scripts/live_eval/experiment_telemetry.py`
- Modify: `tests/test_live_eval_experiment_telemetry.py`
- Create: `tests/fixtures/harness_experiment/valid-terminal.jsonl`
- Create: `tests/fixtures/harness_experiment/duplicate-usage.jsonl`
- Create: `tests/fixtures/harness_experiment/usage-after-error.jsonl`
- Create: `tests/fixtures/harness_experiment/truncated-terminal.jsonl`
- Create: `tests/fixtures/harness_experiment/semantic-order-spacing.jsonl`

### Step 1: Add failing JSONL state and usage tests

- [ ] Test:
  - one semantic `turn.completed` event with exactly five wire usage
    integers succeeds when `cache_write_input_tokens=0`;
  - a four-field wire event and non-zero cache-write usage fail closed;
  - exactly one structured agent response is required before the terminal event;
  - missing, duplicate, negative, boolean, float, string, or unknown usage fields fail;
  - cached input greater than input and reasoning output greater than output fail;
  - a second response, a second terminal event, any event after terminal, usage after error, unsupported order, empty line, missing final newline, oversized line, oversized stream, and too many events fail;
  - public summary contains only fixed IDs, digests, counts, durations, classifications, and estimates;
  - semantically identical Codex events with different key order or insignificant spacing produce the same typed projection;
  - total tokens are input plus output;
  - estimated cost applies cached, non-cached input, and output rates exactly once;
  - raw JSONL bytes and arbitrary event values do not appear in `repr()` or serialized summary.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_telemetry -v
```

Expected: failure because `parse_terminal_telemetry()` and its state machine do
not exist.

### Step 3: Add bounded parsing limits

- [ ] Add:

```python
@dataclass(frozen=True)
class TelemetryLimits:
    max_total_bytes: int
    max_line_bytes: int
    max_events: int
    max_token_value: int
```

### Step 4: Implement the pure JSONL state machine

- [ ] Accept one `bytes` value so truncation and total-size checks occur before JSON decoding:

```python
def parse_terminal_telemetry(
    data: bytes,
    limits: TelemetryLimits,
    price_snapshot: Mapping[str, object],
) -> TelemetrySummary:
    if type(data) is not bytes:
        raise TelemetryError("telemetry_not_bytes")
    checked_limits = _validate_telemetry_limits(limits)
    if not data or len(data) > checked_limits.max_total_bytes:
        raise TelemetryError("telemetry_size_invalid")
    if not data.endswith(b"\n"):
        raise TelemetryError("telemetry_truncated")
    if b"\r" in data:
        raise TelemetryError("telemetry_line_invalid")
    lines = data[:-1].split(b"\n")
    if not lines or len(lines) > checked_limits.max_events:
        raise TelemetryError("telemetry_event_count_invalid")
    state = _TelemetryState()
    for line in lines:
        if not line or len(line) > checked_limits.max_line_bytes:
            raise TelemetryError("telemetry_line_invalid")
        event = _load_bounded_semantic_json(line)
        state = _consume_event(state, event, checked_limits)
    return _finish_telemetry(state, price_snapshot)
```

- [ ] Support the lifecycle types `thread.started`, `turn.started`, `item.started`, `item.updated`, `item.completed`, `turn.completed`, and `error`.
- [ ] Recognize `turn.failed` only as a rejection marker and never project
  it. Implement the exact event/item schemas, lifecycle matrix, and
  single-turn DFA in the Task 5 binding contract.
- [ ] Treat raw Codex JSONL as semantic JSON, not canonical plan input. Preserve duplicate-key, float, non-finite, UTF-8, NFC, exact-schema, ordering, and size rejection, but do not require wire key order or whitespace to equal `canonical_bytes(event)`.
- [ ] Count a structured response only from `item.completed` where `item.type=agent_message` and `item.text` parses to one JSON object. Store only `sha256:` over its canonical bytes.
- [ ] Finish through Task 3's typed summary constructor so parser output,
  receipt reconstruction, canonical document generation, and digest
  calculation share one schema and invariant path.
- [ ] Test that semantically equivalent terminal streams produce byte-identical typed summary documents and equal summary digests, while changing any accepted retained usage value changes the summary digest. Non-zero cache-write fails before projection.
- [ ] Require `turn.completed` to be final and to contain the pinned
  five-field wire shape:

```json
{"cache_write_input_tokens":0,"cached_input_tokens":0,"input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0}
```

The values shown are shape examples. Accepted values satisfy the configured
cap and subset constraints, and cache-write must be exactly zero. The typed
summary retains the original four provider counts; non-zero cache-write
support requires a later versioned price and receipt contract.

### Step 5: Run focused tests and commit

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_telemetry -v
git diff --check
```

- [ ] Commit:

```bash
git add scripts/live_eval/experiment_telemetry.py tests/test_live_eval_experiment_telemetry.py tests/fixtures/harness_experiment/valid-terminal.jsonl tests/fixtures/harness_experiment/duplicate-usage.jsonl tests/fixtures/harness_experiment/usage-after-error.jsonl tests/fixtures/harness_experiment/truncated-terminal.jsonl tests/fixtures/harness_experiment/semantic-order-spacing.jsonl
git commit -m "feat(eval): project bounded experiment telemetry"
```

---

## Task 6: Task-Source Trust Gate and Object-Database Topology Seal

**Normative binding:** Before implementation, read
`docs/superpowers/specs/2026-07-23-harness-experiment-task6-binding.md`.
That contract takes precedence over illustrative snippets below.

**Files:**

- Create: `scripts/live_eval/task_snapshot.py`
- Create: `tests/test_live_eval_task_snapshot.py`

### Step 1: Build failing temporary-repository attack tests

- [ ] Create Git repositories dynamically inside `TemporaryDirectory`; do not track `.git` fixtures.
- [ ] Test supported intake:
  - absolute operator-owned repository root;
  - plain in-tree `.git` directory;
  - current-user ownership and no group/other write permission across Git administration and object-store entries;
  - full lowercase SHA-1 or SHA-256 commit OID;
  - operator attestation and allowed provisioning class;
  - local clone policy bound into the prepared source evidence; Task 6 emits
    no receipt.
- [ ] Test rejection before object reads:
  - relative root, root symlink, gitfile, linked worktree, external `commondir`, `core.worktree`, `extensions.worktreeConfig`;
  - system/global/include configuration influence;
  - filters, non-sample hooks, fsmonitor, LFS, submodules, replace refs, alternates, promisor state, quarantine, and external object directories;
  - symlink, hardlink, FIFO, socket, device, or other special entry anywhere under the object database;
  - loose object, pack, index, bitmap, commit-graph, and multi-pack-index topology attacks;
  - canonical filename aliases, inventory file-count cap, and byte cap;
  - changed root, `.git`, configuration, or object topology between the before and after seals.
- [ ] Each sentinel test must assert the sentinel was not read or executed.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_task_snapshot -v
```

Expected: import failure because the task snapshot module does not exist.

### Step 3: Implement source specifications and policy

- [ ] Implement the binding contract's exact `TaskSnapshotError`,
  `TaskSourceSpec`, `TaskSnapshotPolicy`, `ObjectTopologySeal`,
  `PreparedTaskSource`, and `prepare_task_source()` surface.
- [ ] Validate every scalar and policy integer before source access without
  accepting booleans. Preserve local paths only in `repr=False` in-memory
  fields.
- [ ] Support only POSIX Darwin/Linux with the required no-follow,
  identity, nanosecond-time, and process-group facilities. Treat mount and
  clone provenance as operator-attested residual assumptions.

### Step 4: Implement no-follow identities and bounded object inventory

- [ ] Validate every physical component from the anchor through root,
  `.git`, config, controls, and objects with `lstat()`/`fstat()` equality.
  Require current ownership, no group/other writes, one device, plain
  directories, and regular single-link control files.
- [ ] Bounded-read and digest config and optional packed refs. Reject
  `commondir`, worktree/module administration, replace refs, alternates,
  non-sample hooks, promisor state, and every other forbidden control before
  object reads.
- [ ] Parse the exact NUL/LF config record format and apply the binding's
  closed allowlist and SHA-1/SHA-256 repository-format pairing.
- [ ] Walk `.git/objects` iteratively without following links. Enforce the
  binding's exact loose/info/pack/commit-graph/MIDX grammar, pairing,
  ownership, modes, aliases, depth, component/path, entry, and byte limits.
- [ ] Canonicalize the exact path/kind/mode/device/inode/uid/gid/link/size/
  nanosecond-time records in UTF-8 path order without an absolute path.
- [ ] Build the five exact versioned canonical evidence documents in the
  binding and map `task-source-identity-v1(F,C)` plus
  `task-object-topology-v1` into the unchanged Task 4 receipt fields.

### Step 5: Implement the bounded Git adapter

- [ ] Use only the binding's literal `git`, exact global prefix, exact
  replacement environment, `cwd="/"`, and six closed operation templates.
  Task 6 may call only config and storage-format metadata operations.
- [ ] Use `Popen` with no shell, no stdin, a new session, concurrent bounded
  drains, monotonic deadline, inclusive caps, TERM/grace/KILL group cleanup,
  mandatory reap, and fixed path-free errors.
- [ ] Hash the path-placeholder process-policy document rather than actual
  argv paths.

### Step 6: Return a prepared source without a receipt

- [ ] Capture raw filesystem/control and object seals, run bounded config,
  object-format, and second config probes, then recapture raw filesystem and
  object seals. Require exact F/C/O pair equality and derive the prepared
  source identity from the versioned canonical documents.
- [ ] Return one deeply immutable `PreparedTaskSource`. Do not call commit or
  blob object operations and do not import or construct a receipt.
- [ ] Reserve the sole authoritative `task_source_trust` receipt for Task 7,
  after its full capture transaction and final equal seals.

### Step 7: Run the security checkpoint and commit

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_task_snapshot -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_live_eval_checkout tests.test_live_eval_harness -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
python3 -m py_compile scripts/live_eval/task_snapshot.py
git diff --check
```

- [ ] Request an independent security/isolation review of this slice. Dispose each material finding as `apply`, `ask`, `defer`, or `reject-with-reason`; rerun the smallest test capable of detecting accepted findings.
- [ ] Commit:

```bash
git add scripts/live_eval/task_snapshot.py tests/test_live_eval_task_snapshot.py
git commit -m "feat(eval): gate task source object stores"
```

---

## Task 7: Full-OID Object Materialization and Snapshot Receipts

### Binding clarification

`docs/superpowers/specs/2026-07-23-harness-experiment-task7-binding.md`
is normative for this task and its Task 8 handoff. It supersedes the older
single-target materializer sketch below with a capture-once,
`materialize_pair()`-once transaction; adds exact tree/trie/unique-blob and
60-second capture limits, including a hard 256-unique-blob ceiling; amends the
fixed Git templates; defines canonical entry, materialized-tree, and
root-identity documents; and fixes receipt, simple single-task lifecycle,
identity-based overlap, descriptor, conservative whole-unit rollback,
exclusion-scope, and allowed-write-policy timing. Phase A verifies logical
state but does not promise crash durability for its transient trees.

**Files:**

- Modify: `scripts/live_eval/task_snapshot.py`
- Modify: `tests/test_live_eval_task_snapshot.py`

### Step 1: Add failing tree and target-materialization tests

- [ ] Test:
  - a full commit OID resolves to the same fixed tree after a branch ref moves;
  - working-tree changes and cleanliness are never read;
  - only regular `100644` and `100755` blobs materialize;
  - symlink, gitlink, submodule, special mode, traversal, absolute path, empty component, NUL, duplicate, parent/file alias, Unicode alias, and case alias fail;
  - root, nested, case-aliased, or Unicode-aliased `AGENTS.md` and `AGENTS.override.md` fail;
  - missing object, changed object, per-file, file-count, and total-byte limits fail;
  - declared `ls-tree` size and streamed `cat-file blob` byte count mismatch fails;
  - writes use exclusive no-follow creation and preserve only `0444` or `0555` file modes;
  - target replacement, extra entry, mode change, content change, hardlink, symlink, or special entry fails the target seal;
  - the same commit produces equal entry and tree digests in two distinct condition directories;
  - an empty tree, repeated blobs, derived-directory count, tree depth,
    unique-blob count, and capture-transaction timeout obey their exact
    inclusive limits;
  - pair failure first inspects the whole pair; any replacement or inspection
    mismatch causes no cleanup mutation and preserves the whole pair, while a
    later deletion error remains observable and overrides the original error;
  - source-root and `.git` descendant targets reject before write by opened
    `(dev, ino, kind)` identity regardless of mutable metadata changes,
    including case-insensitive or Unicode-normalizing path aliases;
  - simple capture/pair/close transitions require the exact captured object,
    repeatable stateless verification detects mutation, and peak descriptors
    stay within `max_tree_depth + 8`;
  - fresh filesystem, config, and topology seals are identical before the
    first and after the final object read; the object database is not rescanned
    around each blob.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_task_snapshot -v
```

Expected: source gating tests pass while materialization tests fail because the materializer is incomplete.

### Step 3: Parse a fixed tree from one full OID

- [ ] Validate OID length and lowercase hex before Git.
- [ ] Verify object format with
  `rev-parse --show-object-format=storage`.
- [ ] Resolve only `FULL_COMMIT_OID^{commit}` and `FULL_COMMIT_OID^{tree}` using the already validated in-memory OID; reject output that is not one full OID of the declared format.
- [ ] Run the binding's exact formatted
  `ls-tree -r -z --full-tree --format=... TREE_OID` operation with no
  pathspec, then parse mode, object type, full OID, canonical declared size,
  and strict UTF-8 path from every NUL-terminated record:

```python
@dataclass(frozen=True)
class TaskTreeEntry:
    path: str
    git_mode: str
    blob_oid: str
    size: int
    content_digest: str
```

- [ ] Validate the complete path trie, exclusions, derived-directory count,
  aliases, conflicts, and all declared limits before calling
  `cat-file blob`; fetch once per unique OID in canonical OID order and
  recompute its Git object OID.

### Step 4: Materialize regular blobs with exclusive no-follow writes

- [ ] Validate one exact private target parent and create both absent
  condition roots in one ownership-tracked `materialize_pair()` transaction.
  Create directories one component at a time and verify their identities.
- [ ] Make overlap identity independent of mutable owner/mode metadata:
  matching `(dev, ino, kind)` with the captured source root or `.git` rejects
  before any target mutation.
- [ ] Open each destination with `O_WRONLY | O_CREAT | O_EXCL` plus
  `O_NOFOLLOW`, verify `fstat()` is a single-link regular file, write the
  bounded blob, set `0444` or `0555`, close/reopen it through the verified
  parent, and verify final identity, size, and content hash.
- [ ] Compute:
  - `entry_digest` over the complete exact `task-tree-entries-v1` document,
    including commit/tree/format, derived-directory and file counts, logical
    and unique byte counts, and every path/mode/blob/content record;
  - `materialized_tree_digest` independently over target-relative path, target mode, size, and bytes.
- [ ] Make all target directories `0555`, verify exact inventories by bounded
  descriptor-relative re-read, and construct one condition-independent
  snapshot receipt only after both roots pass. Do not add `fsync()` calls to
  the new task-snapshot materializer or its cleanup; crash durability is
  outside Phase A. This does not change the existing verified harness
  checkout helper's internal durability calls.

Implement the exact `TaskTreeEntry`, `CapturedTaskObjects`,
`MaterializedTaskSnapshot`, and `TaskSnapshotMaterializer` public surface in
Task 7 binding Section 2. There is no public single-target `materialize()`.

Inside `capture()`, issue the sole source-trust receipt only after fresh
filesystem/config/object seals first match the exact prepared source and then
match across the complete fixed-object read transaction. Require the
materializer policy and recomputed process-policy digest to equal the
prepared values. A capture failure emits no source receipt. A later pair
failure occurs after that receipt exists but emits no snapshot receipt.
Capture each selected task once, materialize both roots from the same capture
with `materialize_pair()`, require the same snapshot receipt object and
distinct target identities, then call idempotent `close()` in `finally` and
release captured/snapshot references before processing the next task. The pair
operation already performs independent full verification of both roots, so
Task 8 does not immediately duplicate `verify()` calls.

### Step 5: Enforce first-pilot repository exclusions

- [ ] Reject only the binding's exact enumerated path/basename denylist. Do
  not claim it detects every credential or live integration; unlisted secret
  names/content remain an operator-attested corpus residual.
- [ ] Apply the binding's exact depth scopes: agent basenames and hidden
  `.codex`/`.agents`/`.claude`/`.mcp` directory components at any depth;
  root-level `.git-hooks`/`hooks`/`plugins` directories; and the enumerated
  sensitive basenames at any depth. A policy change creates a new
  materializer-policy version and plan digest.
- [ ] Keep validators, assertions, reference fixtures, and expected results outside the materialized model-writable tree. Candidate input binds only their digests.
- [ ] Defer candidate allowed-write validation to Task 8 after capture. Bind
  only the digest of the exact transient `task-allowed-write-policy-v1`
  document into each pilot invocation plan. Keep the authoritative
  repository-relative paths in canonical input and the experiment plan, but
  never put an absolute/host-local path or allowed-write path in a pilot
  reservation receipt.

### Step 6: Run focused and live-eval regression tests

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_task_snapshot -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
git diff --check
```

- [ ] Commit:

```bash
git add scripts/live_eval/task_snapshot.py tests/test_live_eval_task_snapshot.py
git commit -m "feat(eval): materialize fixed task snapshots"
```

---

## Task 8: Zero-Call Preflight Orchestration

### Binding clarification

Task 7 binding Section 11 is normative. Task 8 validates each candidate's
leaf allowed-write paths against the captured tree, hashes the exact transient
`task-allowed-write-policy-v1` document, and makes
`PilotInvocationPlan` a reproducible content/policy plan by binding
`allowed_write_policy_digest`, `base_profile_digest`, and
`root_capability_policy_digest` while removing transient local-root identities
and runtime capability sets. The authoritative repository-relative paths
remain in the existing canonical input preserved and digest-bound by
`ExperimentPlan`. Absolute and host-local paths remain forbidden.

**Files:**

- Modify: `scripts/live_eval/experiment_plan.py`
- Modify: `tests/test_live_eval_experiment_plan.py`
- Modify: `tests/test_live_eval_experiment_receipts.py`
- Create: `scripts/live_eval/experiment.py`
- Create: `tests/test_live_eval_experiment.py`

### Step 1: Add failing orchestration and poison-pill tests

- [ ] Build synthetic private harness and task repositories in `TemporaryDirectory`.
- [ ] Test:
  - both `current` and `lean` source manifests are loaded and their profile identities differ;
  - both homes are materialized, verified, and sealed with existing public APIs;
  - every selected task is captured once and materialized with one pair
    transaction into distinct condition roots sharing the same snapshot
    receipt object but distinct root identities;
  - existing-file and missing-leaf allowed-write policies are validated
    against the captured trie, change the pilot-plan digest, retain their
    authoritative repository-relative paths in input/plan, and leak no
    absolute or host-local path;
  - same-task conditions share one allowed-write-policy digest and a digest
    cannot map to two task IDs;
  - the task selection and corpus receipts bind the four selected task snapshots;
  - two future canary templates and eight pilot invocation plans are constructed, but none execute;
  - schedule, content receipts, and canonical policy documents are deterministic for the same immutable inputs;
  - the final plan remains host-local-path-free while intentionally retaining
    repository-relative allowed-write authority, remains stable across equal
    target rematerializations at different trusted local paths when the same
    captured source/snapshot evidence is reused, and changes when snapshot
    evidence or bound policy changes;
  - actual baseline/derived-home/derived-task/temp identities and all four
    capability-root sets are absent from Phase A plans and required in future
    Phase B runtime containment/child evidence before reservation;
  - before the first write, the trusted temporary parent is physically
    ancestry-disjoint in both directions from bundle, skill, every selected
    task source, and every selected `.git`, including case/Unicode aliases;
  - source, bundle, plan, or materialized-tree mutation blocks;
  - cleanup pre-inspects the whole owned tree; a replacement or inspection
    failure causes no chmod/delete and produces `cleanup_required`;
  - success produces the existing success-only preflight receipt, while
    post-plan cleanup failure retains completed path-free result digests with
    `preflight_receipt_digest=None`;
  - ownership authority comes from creation/acquisition rather than a cleanup
    scan; replacements are never chmodded and production never invokes
    recursive `TemporaryDirectory` cleanup;
  - sealed task roots remain immutable baselines and no future
    `workspace-write` plan makes them directly writable;
  - no credential-like environment name is read;
  - no Codex executable is resolved, probed, or launched;
  - no socket, HTTP client, live ledger, or model process seam exists;
  - permitted subprocesses are limited to the new bounded task-object Git adapter and the existing trusted `skill_repo` checkout Git calls reached through `materialize_harness_home()`; Codex/model, authentication, validator, shell, and network processes remain forbidden;
  - every result has `model_calls=0`, including blocked results.

### Step 2: Run the focused test and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment -v
```

Expected: import failure because the orchestrator does not exist.

### Step 3: Implement request and result contracts

- [ ] Add:

```python
@dataclass(frozen=True)
class ExperimentPreflightRequest:
    experiment_input: CanonicalExperimentInput
    bundle_root: Path = field(repr=False)
    skill_repo: Path = field(repr=False)
    temp_parent: Path = field(repr=False)
    task_sources: Mapping[str, TaskSourceSpec] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_root", Path(self.bundle_root).absolute())
        object.__setattr__(self, "skill_repo", Path(self.skill_repo).absolute())
        object.__setattr__(self, "temp_parent", Path(self.temp_parent).absolute())
        object.__setattr__(
            self, "task_sources", MappingProxyType(dict(self.task_sources))
        )


@dataclass(frozen=True)
class ExperimentPreflightResult:
    status: str
    live_backend_state: str
    global_agents_marker_state: str
    pilot_state: str
    qualification_evidence_classification: str
    model_calls: int
    materialization_result: str
    plan_digest: Optional[str]
    task_corpus_receipt_digest: Optional[str]
    preflight_receipt_digest: Optional[str]
    bundle_digest: Optional[str]
    current_profile_digest: Optional[str]
    lean_profile_digest: Optional[str]
    cleanup_state: str
    reason_code: str
```

The binding's exact validation supersedes the sketch's `.absolute()` calls:
detach each path only after validating its original exact physical absolute
spelling. `temp_parent` is a caller-owned empty current-UID `0700` directory.
Task 8 preserves it and creates/removes only its exact owned `phase-a` child.

Success values are:

```text
status=static_only
live_backend_state=live_backend_not_implemented
global_agents_marker_state=global_agents_marker_not_run
pilot_state=pilot_not_run
qualification_evidence_classification=operator_attested_static
model_calls=0
materialization_result=verified
cleanup_state=removed
reason_code=static_preflight_verified
```

Blocked results before final-plan construction use `status=blocked`,
`materialization_result=blocked`, the same three not-run states,
`model_calls=0`, nullable digests, and a fixed sanitized reason.
Qualification is `operator_attested_static` only after the canonical corpus
contract is accepted; earlier failures use `not_validated`. The one
post-plan cleanup alternative retains every final digest and uses
`status=blocked`, `qualification_evidence_classification=operator_attested_static`,
`materialization_result=blocked`, `cleanup_state=cleanup_required`, and
`reason_code=task_snapshot_cleanup_required` exactly as specified by Task 7
binding Section 11.2. It also requires `preflight_receipt_digest=None`; no
blocked `PreflightReceipt` exists.

### Step 4: Compose preflight in one direction

- [ ] Implement this orchestration sequence:

```text
load and validate authoritative plan input
select and schedule four tasks
require exactly the selected task-source mapping and trusted empty temp parent
physically validate bundle, skill, selected task roots, and each .git
require temp-parent ancestry disjointness from every protected root
create exact owned temp_parent/phase-a and its creation-time ledger
load current and lean harness source identities
materialize, verify, and seal two base homes
for each selected task: create a fresh single-task materializer
prepare/capture once and materialize one distinct current/lean pair
use the pair's two independent full verifications and digest the allowed-write policy
in finally close the materializer and drop capture/snapshot references
collect one source-trust and one snapshot receipt per selected task
build selection and corpus receipts
build two canary templates and eight pilot invocation plans
build the immutable experiment plan and plan digest
identity-check and remove only the owned temporary tree
build the success preflight receipt only after complete cleanup
project the public result
```

- [ ] Build profile digests from path-free `HarnessManifest` fields only. Do not serialize dataclass `Path` values.
- [ ] Canary templates bind `read-only`; pilot invocation plans bind the
  future `workspace-write` policy. Phase A task roots are immutable baselines,
  not writable execution roots. A future child must create and bind a
  separate derived root/overlay and derivation-policy digest without chmodding
  the baseline for execution. Canary marker, derived-home, executable, and
  runtime-root identities remain future child evidence.
- [ ] Represent the future executable only by the required executable-identity policy and CLI version. Phase A must not resolve an executable or claim continuity.

### Step 5: Build selection, corpus, and plan receipts

- [ ] The selection receipt binds candidate-set digest, seed digest, rule, selected IDs, and schedule digest.
- [ ] The corpus receipt binds selection, qualification, each task snapshot, prompts, validators, assertions, reference outcomes, mutant outcomes, and difficulty assignments.
- [ ] Extend `PilotInvocationPlan` and its exact child document with
  `allowed_write_policy_digest`, `base_profile_digest`, and
  `root_capability_policy_digest` immediately after
  `snapshot_receipt_digest`. Remove the three transient local-root identity
  fields and four runtime capability-set tuples. Require the pair for one task
  to share its allowed-write digest, reject a digest mapped to a different
  task, and require each condition's matching base-profile digest. The
  existing pilot reservation binds these fields transitively through
  `invocation_plan_digest`.
- [ ] Keep repository-relative paths in the existing authoritative canonical
  input preserved and digest-bound by `ExperimentPlan`; do not add a duplicate
  top-level path projection. The three content/policy digests are the only new
  durable fields, and absolute/host-local paths remain forbidden.
- [ ] A future Phase B reservation must reload authoritative input bytes,
  verify `input_digest`, rebuild the allowed-write policy against the freshly
  verified baseline/derived namespace, compare its digest before use, and bind
  actual root identities and capability sets in versioned runtime
  containment/child evidence before reservation.
- [ ] Keep existing preflight receipt validation and replay success-only:
  exactly `verified/removed`. Fixture updates in
  `tests/test_live_eval_experiment_receipts.py` cover the amended invocation
  plan shape; production `experiment_receipts.py` does not change.
- [ ] After identity-aware cleanup, emit a success `PreflightReceipt` only for
  `verified/removed`. A cleanup-required public result retains completed
  path-free digests but has `preflight_receipt_digest=None`. A failure before
  final plan construction also has no preflight receipt digest.

### Step 6: Implement identity-aware cleanup

- [ ] Validate the explicit caller-owned `temp_parent` through physical
  descriptor traversal, require exact mode `0700` and an empty inventory, and
  retain its descriptor. Never infer `/tmp`, `TMPDIR`, or another ambient
  parent.
- [ ] Before creating `phase-a`, physically traverse `bundle_root`,
  `skill_repo`, every selected task source root, and every exact `.git`.
  Compare terminal `(dev, ino, kind)` identities against opened ancestor
  chains (each including its terminal and every opened ancestor through the
  filesystem anchor) in both directions and reject all overlap, including
  case/Unicode aliases, before any write.
- [ ] Record each root and descendant stable token (`dev`, `ino`, `uid`, `gid`,
  `kind`) and expected component inventory when created or acquired; never
  establish ownership from a cleanup-time scan or maintain latest-mutation
  identity records.
- [ ] Before mutation, inspect the whole owned `phase-a` tree
  descriptor-relatively. If every inventory, token, kind, and single-link-file
  check passes, `fchmod(0700)` directories top-down and delete entries
  bottom-up with immediate token rechecks. The new Task 8 ownership-cleanup
  layer adds no `fsync()` calls for its temporary files, directories, or
  parents. Existing `materialize_harness_home()` checkout internals remain
  unchanged and may retain their established `fsync()` calls; they do not
  create crash-durable preflight evidence.
- [ ] If any entry is replaced, linked, becomes special, or cannot be
  inspected during the read-only pass, perform no chmod/delete, preserve the
  whole owned tree, and return `cleanup_required`. An OS error or same-UID race
  after mutation begins may leave partial residue and also returns
  `cleanup_required`.
- [ ] Production orchestration must not invoke `TemporaryDirectory` recursive
  cleanup or another unverified recursive deleter. After removing the exact
  owned `phase-a` leaf, preserve the empty caller-owned `temp_parent`.
  A success receipt proves the observed logical cleanup state, not
  crash-persistent absence; after a host crash the caller must identity-check
  this parent before reuse.

### Step 7: Run focused and legacy golden tests

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_live_eval_experiment -v
python3 -m unittest tests.test_canonical_json tests.test_live_eval_checkout tests.test_live_eval_harness tests.test_live_eval_isolation tests.test_live_eval_runner -v
git diff --check
```

- [ ] Confirm `git diff --` shows no modification to the five frozen legacy production modules.
- [ ] Commit:

```bash
git add scripts/live_eval/experiment_plan.py scripts/live_eval/experiment.py tests/test_live_eval_experiment_plan.py tests/test_live_eval_experiment_receipts.py tests/test_live_eval_experiment.py
git commit -m "feat(eval): orchestrate zero-call experiment preflight"
```

---

## Task 9: Preflight-Only CLI, Repository Gate, CI Baseline, and Docs

**Files:**

- Create: `scripts/run_harness_experiment.py`
- Modify: `tests/test_live_eval_experiment.py`
- Modify: `scripts/validate_repo.sh`
- Modify: `tests/test_repository_validation.py`
- Modify: `.github/workflows/validate.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/forward-test-report.md`

### Step 1: Add failing CLI and repository-contract tests

- [ ] Test:
  - only the `preflight` subcommand exists;
  - `canary`, `pilot`, approval, API-key, executable, and live-ledger options exit `2` before orchestration;
  - required options are `--input`, `--bundle-root`, `--skill-repo`,
    `--temp-parent`, and four
    `--task-source task_id=/absolute/repository` bindings matching the
    selected task set;
  - duplicate, missing, extra, relative, or malformed task-source bindings fail;
  - success prints one compact sorted JSON object and exits `0`;
  - blocked input prints the fixed blocked schema and exits `2`;
  - output contains no input path, task path, private fixture bytes, or synthetic secret;
  - the legacy runner CLI, JSON, and exit-code goldens remain unchanged;
  - repository validation requires every new module, runner, test, and stable fixture;
  - CI uses `actions/setup-python@v7` with `python-version: "3.9"`;
  - full unittest discovery remains in `scripts/validate_repo.sh`.
  - an AST-based dependency test rejects authentication, Codex runner, socket, HTTP-client, and live-ledger imports or calls from every new Phase A module;
  - the AST allowlist permits `subprocess` only in `task_snapshot.py`; trusted skill-checkout Git remains reachable only through the unchanged public harness API.

### Step 2: Run focused tests and confirm failure

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment tests.test_repository_validation -v
```

Expected: CLI and integration contract tests fail because the entry point and repository integration are absent.

### Step 3: Implement the thin CLI

- [ ] Follow the repository's direct-script import bootstrap:

```python
#!/usr/bin/env python3
"""Run the zero-model-call harness experiment preflight."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```

- [ ] Build `argparse` with one required subparser named `preflight`. Parse
  task-source bindings with `partition("=")`, validate opaque task IDs before
  paths, and require absolute source paths. Pass the exact required
  `--temp-parent` value into `ExperimentPreflightRequest`; never fall back to
  `TMPDIR`, `tempfile.gettempdir()`, or `/tmp`.
- [ ] Read the plan file with a CLI-local 1 MiB cap through a no-follow descriptor. Require a regular single-link file, compare `lstat()` and `fstat()` identity, read in bounded chunks, and reject mutation before calling `load_experiment_input()`.
- [ ] Construct `ExperimentPreflightRequest` from the accepted canonical bytes and call `run_experiment_preflight()`.
- [ ] Serialize `asdict(result)` with `sort_keys=True`, `separators=(",", ":")`, and `ensure_ascii=False`.
- [ ] Catch only expected input, OS, plan, snapshot, receipt, and orchestration exceptions. Convert them to the fixed blocked result without exposing exception text.
- [ ] Do not import `scripts.run_live_eval` or any authentication, executable, network, Codex-process, or ledger module.

### Step 4: Extend repository validation once

- [ ] Add `require_file` entries for:

```text
.github/workflows/validate.yml
scripts/run_harness_experiment.py
scripts/live_eval/experiment.py
scripts/live_eval/experiment_plan.py
scripts/live_eval/experiment_receipts.py
scripts/live_eval/experiment_telemetry.py
scripts/live_eval/task_snapshot.py
tests/test_live_eval_experiment.py
tests/test_live_eval_experiment_plan.py
tests/test_live_eval_experiment_receipts.py
tests/test_live_eval_experiment_telemetry.py
tests/test_live_eval_task_snapshot.py
tests/fixtures/harness_experiment/valid-plan-input.json
tests/fixtures/harness_experiment/valid-terminal.jsonl
```

Do not add duplicate focused test execution to `validate_repo.sh`; its existing full discovery already executes the new tests.

### Step 5: Pin the required Python baseline in CI

- [ ] Insert after checkout:

```yaml
      - name: Set up Python 3.9
        uses: actions/setup-python@v7
        with:
          python-version: "3.9"

      - name: Verify Python baseline
        run: python3 -c 'import sys; assert sys.version_info[:2] == (3, 9), sys.version'
```

Keep one required 3.9 job. A newer-version matrix is optional future coverage and is not required for Phase A acceptance.

### Step 6: Document only the implemented and verified boundary

- [ ] Add a separate README subsection with the new command shape and explicit distinctions:
  - legacy planning dry-run;
  - legacy fixed harness materialization preflight;
  - Phase A experiment `static_only` preflight;
  - future live canary and pilot remain unavailable.
- [ ] Add one `Unreleased` changelog entry for the zero-call experiment foundation.
- [ ] Update `docs/forward-test-report.md` only after commands run. Record exact validation commands, actual pass/fail state, Python version, `model_calls=0`, and remaining containment/live-approval gates. Do not describe planned evidence as completed evidence.

### Step 7: Run the CLI smoke and integration tests

- [ ] In `test_cli_fixture_backed_preflight_is_static_only`, let the fixture builder create four deterministic temporary commits, write a canonical temporary plan containing those full OIDs, and invoke `main()` with that plan plus the exact private harness and task repositories. Capture stdout, parse the one JSON object, and assert that none of the temporary absolute paths occur in the captured bytes.

- [ ] Assert the JSON contains `status=static_only`, all three not-run states, `qualification_evidence_classification=operator_attested_static`, and `model_calls=0`.
- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment tests.test_repository_validation -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
git diff --check
```

### Step 8: Commit the integration slice

- [ ] Commit:

```bash
git add scripts/run_harness_experiment.py tests/test_live_eval_experiment.py scripts/validate_repo.sh tests/test_repository_validation.py .github/workflows/validate.yml README.md CHANGELOG.md docs/forward-test-report.md
git commit -m "feat(eval): expose experiment preflight CLI"
```

---

## Task 10: Independent Review, Final Verification, and Closure

**Files:**

- Modify only files required by accepted review findings.

### Step 1: Run four independent review lenses

- [ ] Review sequentially with evidence:
  1. security and isolation;
  2. architecture and legacy compatibility;
  3. experiment validity and analysis;
  4. failure-mode and test-detection coverage.
- [ ] For every material finding, record `apply`, `ask`, `defer`, or `reject-with-reason`.
- [ ] Accepted findings receive a failing regression test before the fix, followed by the smallest relevant test.
- [ ] Do not repeat the whole review after each small edit. Re-review the affected lens after a material accepted fix and run the final gate once.

### Step 2: Run the complete acceptance gate

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_live_eval_experiment_telemetry -v
python3 -m unittest tests.test_live_eval_task_snapshot -v
python3 -m unittest tests.test_live_eval_experiment -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
./scripts/validate_repo.sh
git diff --check
git status --short
```

Expected:

- all focused and repository-owned tests pass;
- any external shared-agent audit without `SHARED_AGENTS_ROOT` is reported as `not_run`, not hidden;
- no model, API, or network call occurs;
- `.serena/` remains untracked and unstaged.

### Step 3: Perform contract and hygiene scans

- [ ] Confirm frozen legacy modules are byte-identical to their pre-implementation commit:

```bash
git diff 3a784f3 -- scripts/live_eval/isolation.py scripts/live_eval/checkout.py scripts/live_eval/harness.py scripts/workflow_coordination/canonical_json.py scripts/run_live_eval.py
```

Expected: no output.

- [ ] Scan the new implementation for forbidden live seams and private-path retention:

```bash
rg -n "preflight_auth|build_invocation|preflight_isolation|run_eval|run_harness_dry_run|OPENAI_API_KEY|subprocess.*codex|socket|urllib|requests" scripts/live_eval/experiment*.py scripts/run_harness_experiment.py
rg -n "Path|repository_root|bundle_root|skill_repo|target_root" scripts/live_eval/experiment.py scripts/live_eval/experiment_plan.py scripts/live_eval/experiment_receipts.py
```

The first scan must have no executable live seam. Matches in the second scan are acceptable only for private in-memory fields or local orchestration and must not enter canonical receipts or result JSON.

- [ ] Check tracked text for the repository's existing public hygiene policy through `./scripts/validate_repo.sh`; do not add a second inconsistent scanner.

### Step 4: Commit accepted review fixes and report residual risk

- [ ] If review changed files, commit one bounded fix set:

```bash
git add -u scripts tests README.md CHANGELOG.md docs/forward-test-report.md .github/workflows/validate.yml
git commit -m "fix(eval): close experiment preflight review findings"
```

- [ ] Final handoff must report:
  - exact commits;
  - exact commands and outcomes;
  - `model_calls=0`;
  - evidence state `static_only`;
  - whether cleanup and host-local-path-free output were verified;
  - deferred Phase B containment, canary, live ledger, and paid-call work;
  - trusted Git/operator topology, operator-attested task qualification, and same-user tampering as residual assumptions.

## Completion Criteria

Phase A implementation is complete only when:

- canonical plan bytes are authoritative and the derived value is deeply immutable;
- both harness profiles and four task snapshots are bound into one plan digest;
- each task condition has a distinct sealed tree from the same full commit OID;
- task-source topology attacks are rejected before object content is read;
- telemetry and future transition schemas pass their mutation matrices;
- the planner emits exactly two canary templates and eight pilot invocation plans but executes none;
- the CLI exposes only `preflight` and every result reports `model_calls=0`;
- durable output excludes absolute/host-local paths, retains only the approved
  repository-relative allowed-write authority, and discards raw JSONL;
- legacy live-eval golden contracts are unchanged;
- CI explicitly runs Python 3.9;
- focused tests, full live-eval discovery, repository validation, and diff checks pass;
- independent findings are dispositioned and residual risks are explicit.

Phase A completion does not authorize:

- creating or changing TOM's real private bundle;
- choosing a winning harness;
- implementing a containment backend;
- enabling canary or pilot commands;
- resolving credentials or a Codex executable;
- making any model/API call;
- promoting the experiment to a larger study.
