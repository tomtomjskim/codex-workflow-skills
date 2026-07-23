# Harness Experiment Readiness Design

**Status:** Draft revised after adversarial review; awaiting written-spec final review
**Date:** 2026-07-23
**Scope:** Zero-model-call foundation for a future `current` versus `lean`
Codex harness experiment

## Decision

Add a separate, preflight-only harness experiment path instead of extending
the existing workflow-conformance live-eval contract.

This implementation phase builds and validates immutable schemas, private
harness materialization, hardened task snapshots, invocation plans, evidence
identities, analysis rules, and zero-call receipts. It does not expose a live
`canary` or `pilot` command, authenticate, reserve a paid call, execute
model-produced code, or launch Codex.

Live execution is a later design and approval boundary. It requires a proven
containment backend for both the model tool environment and the validator
environment before either live command can be added.

The existing `EvalConfig`, `Invocation`, `_canonical_argv()`, `run_eval()`,
`run_harness_dry_run()`, result dataclasses, serializers, CLI behavior, argv,
and JSON schemas remain unchanged.

## Why the work is staged

A useful preflight foundation can be implemented and tested without building a
host-isolation engine prematurely. Combining static preparation, live model
execution, untrusted validator execution, cost accounting, and experiment
analysis in one first implementation would create a large security boundary
before its operating environment is selected.

The phases are therefore:

| Phase | Repository capability | Model-call cap | Approval |
|---|---|---:|---|
| A | `preflight` only | 0 | This design |
| B | consumption canary, after containment proof | 2 | Separate |
| C | four-pair pilot, after canary review | 8 | Separate |

One future experiment plan has a total cap of ten reserved calls: two canary
calls and eight pilot calls. There are no contingency calls and no retries.
No phase authorizes the next phase automatically.

## Goals

1. Prove statically that a future run is bound to one exact private
   `current|lean` bundle, one exact task corpus, one explicit model and
   reasoning effort, one analysis contract, and one bounded experiment plan.
2. Materialize private harness homes and task trees from immutable inputs
   without executing repository-controlled Git hooks, filters, or checkout
   behavior.
3. Treat canonical input bytes as authoritative, derive one deeply immutable
   value from those bytes, and ensure that frozen value is the only value
   later consumed.
4. Produce a path-free `ExperimentPlanDigest`, `TaskCorpusReceipt`, and
   `PreflightReceipt` without reading credentials or resolving a live
   transport.
5. Define narrow future canary evidence that proves only observation of a
   marker in global `AGENTS.md`, not semantic compliance with the whole
   harness.
6. Define a strict telemetry and future ledger contract that can be tested
   with synthetic inputs without enabling live execution.
7. Freeze task selection, order, scoring, missing-data treatment, and decision
   rules before any result exists.
8. Keep policy text, representative private repositories, credentials, raw
   prompts, raw model logs, and raw model reasoning out of the public
   repository.
9. Fail closed when an identity, capability, budget, receipt, containment
   proof, or analysis invariant cannot be established.

## Non-goals

- Running a consumption canary or paid pilot in this implementation phase.
- Adding dormant live subprocess code behind an undocumented flag.
- Selecting `current`, `lean`, or prohibition-only as the winning policy.
- Editing global `AGENTS.md`, Codex configuration, shared agents, adapters, or
  installed skills.
- Committing TOM's real current or lean policy text to this public repository.
- Treating a four-pair pilot as a superiority study or as authority for a
  global policy replacement.
- Claiming that a marker proves shared-agent, skill, tool, or policy
  compliance.
- Treating local token-cost estimates as authoritative provider billing.
- Evaluating uncommitted working-tree or linked-worktree state.
- Accepting arbitrary prebuilt, archived, shared, FUSE-mounted, or
  network-mounted Git administration directories.
- Building a safe object-database clone subsystem for unsupported external
  source intake.
- Protecting against a malicious kernel, compromised interpreter, malicious
  operator, or an attacker who already controls the same OS user.
- Adding a production dependency.

## Threat and trust boundary

Treat as untrusted data:

- task tracked contents, object contents, and Git configuration values
- task prompts, manifests, fixtures, and project files
- model events, responses, diffs, generated code, and command output
- every executable path or import target that a task can modify

Trust is limited to the OS and kernel, the operator, the checked-out runner
source, the selected Python and Git distributions, and a separately verified
Codex distribution. A task source must also be an operator-owned local clone
created by trusted Git. Its object-store filesystem topology is a trusted
prerequisite only after the Phase A metadata gate passes. Object contents and
configuration values remain untrusted. These assumptions are recorded as
residual risk.

Same-user tampering with a complete local ledger is outside the threat model.
A hash chain detects accidental corruption and inconsistent append history; it
is not an authenticity mechanism against a same-user adversary.

Prebuilt `.git` directories supplied by a task author, repositories unpacked
from archives with Git administration files, shared working copies, and
FUSE or network-mounted object stores are unsupported. A clone from another
local repository must be created with `git clone --no-local` or
`--no-hardlinks`.

Provider transport and tool-side network are separate capabilities. A future
Codex process may use provider transport while its tool subprocesses and the
validator remain network-denied. Absence of a network event in JSONL is only
supplemental evidence, never proof of network isolation.

## Evidence states

The experiment path uses these exact evidence states:

| State | Meaning |
|---|---|
| `static_only` | Canonical inputs, hashes, materialization, plans, and budgets were validated without launching Codex. |
| `live_backend_not_implemented` | This phase intentionally has no live execution capability. |
| `global_agents_marker_not_run` | No approved canary attempted to observe the global `AGENTS.md` marker. |
| `global_agents_marker_observed` | One approved derived-profile canary returned the exact opaque marker under its sealed identity. |
| `pilot_not_run` | No quality-comparison task was executed. |
| `blocked_isolation` | A required containment capability was absent or unproven. |
| `blocked` | Another required invariant, identity, receipt, or approval was absent. |
| `partial` | At least one call was reserved, but the declared phase receipts or the four-pair decision chain did not complete. |
| `machine_assertion_pass` | Only the explicitly named deterministic assertion passed. |

Static preflight must serialize `static_only`,
`global_agents_marker_not_run`, and `pilot_not_run`. It must never serialize
model consumption, quality, or overall experiment status as `pass`.

## Architecture

### 1. Separate preflight boundary

Add:

- `scripts/live_eval/experiment.py`
- `scripts/live_eval/experiment_plan.py`
- `scripts/live_eval/experiment_receipts.py`
- `scripts/live_eval/task_snapshot.py`
- `scripts/live_eval/experiment_telemetry.py`
- `scripts/run_harness_experiment.py`
- `tests/test_live_eval_experiment.py`
- `tests/test_live_eval_experiment_plan.py`
- `tests/test_live_eval_experiment_receipts.py`
- `tests/test_live_eval_task_snapshot.py`
- `tests/test_live_eval_experiment_telemetry.py`
- synthetic, non-sensitive fixtures under
  `tests/fixtures/harness_experiment/`

`experiment.py` owns preflight orchestration and its result contract.
`experiment_plan.py` owns immutable schemas, planning, and analysis rules.
`experiment_receipts.py` owns receipts and the pure future ledger transition
validator.
`task_snapshot.py` owns safe Git-object loading and materialization.
`experiment_telemetry.py` owns bounded JSONL state parsing and typed summary
projection. The CLI is a thin preflight composition layer.

The CLI supports only `preflight`. A `canary`, `pilot`, model-call approval
flag, or other live-only input is rejected before credential lookup,
executable resolution, subprocess construction, network access, or ledger
reservation.

Tests may use pure synthetic records to exercise future receipt and transition
schemas. They must not add a process seam that can launch Codex.

### 2. Legacy contract freeze

The experiment path does not add optional fields to an existing live-eval
config, invocation, result, or serializer. It uses new
`ExperimentPlan` and `ExperimentInvocationPlan` values.

Only low-level canonical JSON, bounded-file, hashing, and safe-path helpers may
be reused. Extracting a helper from a legacy module is allowed only when its
observable behavior remains byte-for-byte identical and the legacy golden
tests pass. The experiment codec may wrap an existing canonical parser, but it
does not change the accepted types, serialization, or public behavior of the
legacy canonical JSON helper.

### 3. Canonical immutable inputs

All external JSON inputs use exact schemas and canonical UTF-8 bytes.
Parsing rejects:

- duplicate or unknown keys
- missing required keys
- booleans where integers are required
- floats, non-finite values, negative counts, or ambiguous numeric units
- non-NFC identifiers or canonically aliased paths
- mutable nested values after construction
- bytes that do not round-trip to the canonical representation

The experiment codec first validates and retains the authoritative canonical
bytes, then creates one deeply frozen value from those bytes. Digests cover
the authoritative bytes. Validation, planning, and future consumption use
only the derived frozen value. The experiment serializer must reproduce the
same bytes from that value before publication.

Money uses integer micro-units in one declared ISO currency. A future price
snapshot has exact model, currency, input, cached-input, and output rates, an
effective timestamp, and a source label. `reasoning_output_tokens` is a subset
of output tokens for the pinned CLI contract and is not charged twice. The
snapshot is an estimate input, not a billing receipt.

### 4. Experiment plan identity

One canonical `ExperimentPlanDigest` binds:

- schema version and opaque experiment ID
- private bundle and base profile digests
- task-source trust, task corpus, and selection receipt digests
- prompts, expected assertions, and trusted validator digests
- model, reasoning effort, and required CLI capability policy
- planned task and condition order
- analysis and masking contracts
- containment-policy version
- evidence-retention policy
- call, time, byte, token, and estimated-cost budgets
- price snapshot and provider-cap evidence classification
- external prerequisite receipt digests supplied before the first live
  reservation

A `TaskCorpusReceipt` freezes the candidate set, selection rule and seed,
difficulty assignment, prompt digest, validator digest, reference-solution
result, and mutation-sensitivity result before a condition is evaluated. Each
machine assertion must reject at least one pre-registered representative
mutant before the task is eligible.

Each task has a `TaskSnapshotReceipt` containing:

- Git object format
- full lowercase commit OID and tree OID
- canonical path, mode, and blob-entry digest
- materialized-tree digest
- materializer-policy version

`TaskCorpusReceipt` binds every selected `TaskSnapshotReceipt` digest, and the
experiment plan binds the corpus receipt digest. Changing a commit, tree,
blob, mode, path, or materializer policy therefore changes the full plan
identity.

`TaskSourceTrustReceipt` records the supported provisioning class, operator
attestation, object-store topology inventory digest, source identities before
and after object reads, and bounded Git-process policy. It proves conformance
to this design's input prerequisite; it does not prove arbitrary repository
safety.

The corpus qualification contract is fixed before selection. It records
opaque task provenance, inclusion and exclusion rules, offline executability,
reference-solution success, negative-control failures, a behavior-category
mutant matrix, and difficulty classification under a pre-registered rubric.
A trivial mutant that exercises only parsing or formatting is not sufficient
for a behavioral assertion. Once the corpus receipt exists, a task cannot be
reclassified, excluded, or replaced based on experiment output; any change
requires a new plan.

Changing any bound value creates a new plan. Receipts from another plan,
profile, task corpus, or policy version are not reusable.

### 5. Private bundle boundary

The real bundle remains outside the public repository:

```text
harness.json
profiles/current/AGENTS.md
profiles/lean/AGENTS.md
shared/agents/*.toml
shared/common-agents/*.md
```

The existing fixed-inventory loader remains authoritative for this bundle.
The experiment path records only opaque identifiers, file counts, modes, and
SHA-256 digests.

The bundle must be a real, non-symlink directory with strict private modes and
the exact inventory. It must not contain credentials, customer data, private
repository contents, or absolute local paths in retained output.

Creating or updating TOM's real private bundle is a separate local action
after Phase A is implemented and reviewed.

### 6. Hardened task snapshots

Generic `git worktree`, checkout, archive extraction, and recursive filesystem
copy are not allowed for untrusted task materialization.

`TaskSnapshotMaterializer`:

1. accepts an object format and one full lowercase commit OID, never a ref;
2. validates an absolute repository root and its plain `.git` directory
   without repository discovery;
3. reads the fixed tree through the verified absolute `--git-dir` with
   hardened `git ls-tree` and `git cat-file blob`;
4. never reads or evaluates working-tree files or cleanliness state;
5. materializes regular blobs only through exclusive, no-follow writes;
6. verifies a source-entry digest and an independently computed target-tree
   digest before returning a receipt.

The repository root, `.git`, object database, and every Git control file read
by the materializer must have verified no-follow identities. The root and
required directory components are symlink-free plain directories. Gitfiles,
`commondir`, linked worktrees, `core.worktree`,
`extensions.worktreeConfig`, object quarantine, external object directories,
alternates, and promisor repositories are rejected before object reads.

Before the first Git subprocess, the materializer performs a bounded
no-follow metadata inventory of the object database entries that Git can
open. It rejects symlinks, FIFOs, sockets, devices, other non-regular files,
regular files with a link count other than one, canonical path aliases, and
inventory size-limit violations. The inventory digest and path identities are
rechecked after object reads. This detects unsupported or accidentally changed
topology; it does not claim to stop a malicious same-user TOCTOU attacker.

Every Git command uses the same sanitized environment, verified `--git-dir`,
and explicit configuration overrides. Preparation disables or rejects system,
global, included, and local behavior that can affect object loading, including
hooks, filters, fsmonitor, LFS, submodules, replace refs, alternates, and
promisor lazy fetch. It clears Git object-directory and quarantine environment
variables and sets `GIT_NO_LAZY_FETCH=1`. Each Git subprocess has a fixed
timeout and bounded stdout, stderr, file-count, and byte policy.

Materialization rejects:

- symlinks, gitlinks, submodules, special files, and unsupported modes
- absolute paths, traversal, empty components, and NUL bytes
- Unicode, case-folding, or platform-canonical filename collisions
- duplicate entries and parent/file aliases
- per-file, total-file, and total-byte limit violations
- missing objects, changed fixed objects, or source/target seal mismatch

Every condition receives a distinct sealed task tree from the same fixed
commit. It never reuses a conversation, tree, temporary directory, local
cache namespace, or model artifact from another condition.

The first pilot forbids every repository-scoped instruction file recognized
by Codex, including root or nested `AGENTS.md`, as well as project Codex
configuration, hooks, MCP configuration, plugins, credentials, and live
external integrations. Project-instruction interaction is a separate future
cohort. Task integrations use offline synthetic substitutes; a text scan
alone is not treated as proof that production access is impossible.

The manifest contains literal repository-relative allowed write paths.
Future pilot validation compares an exact pre/post inventory and blocks any
out-of-contract change, special entry, canonical alias, or untracked output.
Trusted validators, assertions, and reference fixtures live outside the
model-writable tree.

### 7. Invocation plan

Phase A constructs but does not execute an immutable
`ExperimentInvocationPlan`. It pins:

- model ID and `model_reasoning_effort`
- required Codex CLI version and executable identity policy
- exact sandbox mode: future canary `read-only`, future pilot
  `workspace-write`
- approval policy `never`
- exact `CODEX_HOME`, task root, and temporary root identities
- output schema and environment allowlist
- web search, MCP, plugins, hooks, and skills disabled unless explicitly part
  of a future reviewed treatment
- `--ignore-user-config`
- `--ignore-rules`, which ignores execpolicy `.rules`, not `AGENTS.md`

The plan separates:

- `provider_transport_allowed`
- `tool_network_disabled`
- `tool_read_roots`
- `tool_write_roots`
- `child_process_policy`
- `validator_read_roots`
- `validator_write_roots`

`danger-full-access`, an omitted sandbox, or an unknown sandbox value is
invalid.

A future executable identity includes an absolute non-symlink path, owner,
mode, link count, device, inode, size, modification and change times, and
SHA-256 content digest. It is rechecked immediately before capability probing,
reservation, and launch. Phase A may validate the identity policy and
synthetic records, but it does not claim live identity continuity.

### 8. Future containment backend

Live work requires an `ExecutionContainmentBackend` selected and approved in a
later design. The model tool environment and validator environment have
separate capability receipts.

The backend must prove with real subprocess probes that:

- allowed reads and writes succeed;
- host reads outside declared roots fail;
- writes outside declared roots fail;
- tool-side and validator network access fail;
- credentials and parent environment values are absent from tool and
  validator environments;
- process groups, time, output, and child-process limits are enforced.

The validator is untrusted execution. It uses `shell=False`, an absolute
executable allowlist, a credential-free environment allowlist, trusted
read-only validator sources outside the model tree, declared task mounts,
network denial, a bounded process group, and independent time and output
limits.

Fake process tests verify orchestration only. They cannot satisfy containment
acceptance. If a dedicated ephemeral VM, container, or secret-free execution
account cannot prove host read, host write, and tool network denial, live work
returns `blocked_isolation`.

### 9. Future global-instruction canary

The canary is not implemented in Phase A. A later approved canary derives one
temporary profile by one exact append-only overlay and records:

- `base_bundle_digest`
- `base_profile_digest`
- `canary_overlay_recipe_digest`
- `marker_digest`
- `derived_home_digest`
- model, effort, CLI, and containment capability digests

Each profile receives a distinct cryptographically random marker with at
least 128 bits of entropy. It is generated after all other model-visible
inputs are frozen. An occurrence receipt proves that the raw marker exists
exactly once across the prompt, argv, environment, task tree, output schema,
and other model-visible metadata: inside the derived global `AGENTS.md`.
The prompt asks for the marker without containing its value, and the expected
raw value remains in validator-only storage outside the model-visible roots.

The receipt proves only that the opaque marker in the derived global
`AGENTS.md` was observed under that invocation. It does not prove that the
base policy, shared agents, skills, tools, or semantic instructions were
followed.

Each profile requires its own canary call and receipt. Arbitrary edits,
cross-profile markers, low-entropy markers, stale overlays, duplicate marker
occurrences, and receipt reuse are rejected.
Before a pilot, the base profile is materialized and sealed again without the
marker. The pilot gate checks the canary's base identity and separately checks
the new pilot home identity.

### 10. Usage telemetry and evidence retention

Future Codex JSONL parsing requires exactly one supported terminal usage event
with four non-negative integer values:

- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`

It rejects missing or duplicate terminal usage, booleans, floats, strings,
negative values, unknown usage keys, cached input greater than input, usage
after an error, reasoning output greater than output, unsupported terminal
order, multiple structured responses, truncation, overflow, or redaction
failure.

For the pinned CLI usage contract, total reported tokens are
`input_tokens + output_tokens`. `cached_input_tokens` is a subset of input and
`reasoning_output_tokens` is a subset of output; neither is added again.
Estimated cost applies the cached-input rate to cached input, the input rate
to input minus cached input, and the output rate to all output.

The public or durable summary is a typed allowlist projection. It contains
only opaque IDs, digests, counts, durations, classifications, scores, and
labeled estimates. It never copies arbitrary event payloads.

Raw JSONL is not retained by default. Forensic retention requires separate
approval and a private destination. Known-secret literals, forbidden keys,
private-root paths, and secret-pattern checks must pass; otherwise the raw
artifact is discarded. This scanning reduces exposure but is not represented
as proof that every unknown secret format was detected.

### 11. Future ledger and receipt state machine

Phase A implements only canonical schemas and a pure transition validator.
It does not create a live ledger or reserve calls.

The later live state machine is:

```text
PreflightReceipt
  -> RuntimeContainmentReceipt
  -> [CanaryReservation(profile)
      -> CanaryTerminal(profile)
      -> CanaryReceipt(profile)] x 2
  -> [PilotReservation(task, condition)
      -> PilotTerminal(task, condition)] x 8
  -> MaskedReviewPacketReceipt
  -> ScoreLockReceipt
  -> UnmaskReceipt
  -> DecisionReceipt

(any post-reservation nonterminal state)
  -> ExperimentStopReceipt
```

Both profile canary receipts are required before the first pilot reservation.
Every runtime record is an append-only child that binds the unchanged plan
digest and previous-record hash. Adding a containment, canary, pilot, review,
or decision record never changes the plan digest. A plan change after the
first runtime child blocks continuation.

A reservation has at most one call-terminal record, classified as
`completed`, `failed`, `timed_out`, `crashed`, or
`abandoned_after_recovery`. Every classification consumes its phase and
experiment call allowance. A missing terminal blocks continuation; recovery
may append only `abandoned_after_recovery` followed by
`ExperimentStopReceipt`.

An infrastructure failure, invalid terminal, orphan reservation, failed
canary, or unresolved review state stops further reservations. Behavioral
failure under a valid terminal remains an observed result and follows the
pre-registered analysis contract.

`ExperimentStopReceipt` binds the unchanged plan digest, previous-record hash,
stop stage and fixed reason code, phase-level consumed reservation counts,
unresolved reservation IDs, supporting evidence-receipt digests, and outcome.
Its default and only non-safety outcome is `inconclusive`.
`reject_for_safety` is allowed only when the receipt chain already contains a
valid basis permitted by the decision rules. `advance_to_larger_study` is
never valid on the stop branch.

Exactly one of `DecisionReceipt` and `ExperimentStopReceipt` terminates an
experiment. No record, reservation, review, unmask, or decision may be
appended after either terminal receipt.

One future private mode-0600 canonical JSONL ledger resides in an owned
mode-0700 directory. A regular, non-symlink, single-link lock file is held
with an OS advisory lock for the entire run. Appends are fsynced, and directory
creation and durable state transitions include directory fsync. Replaced,
partial, stale, replayed, cross-plan, duplicate, or hash-invalid state blocks
automatic continuation.

The hard call allocation is:

- experiment total: 10
- canary: 2, exactly one per profile
- pilot: 8, exactly four complete pairs
- contingency and retry: 0
- concurrency: 1

Other local stops cover elapsed time, retained bytes, cumulative reported
tokens between calls, supported per-call output ceilings, and subprocess
timeouts clamped to the remaining experiment time.

Estimated cost uses the fixed micro-unit price snapshot and is labeled
`estimated_cost`. A provider-side cap is recorded as either independently
verified or `operator_attested_only`. The latter is residual risk and requires
a separate explicit live approval; it is not described as a guaranteed
monetary hard stop.

External approvals, budgets, provider evidence, backend identity, and other
authorization prerequisites are supplied before the first live preflight and
bound to the plan. Phase A may record them as `not_supplied` and still emit a
`static_only` receipt, but its live gate remains blocked. Supplying or changing
an external prerequisite creates a new plan and requires a new static
preflight.

For live work, external gates are checked before credentials or executables
are resolved. The selected executable and backend are then verified and real
no-model containment probes produce `RuntimeContainmentReceipt`. Only after
that child receipt is valid may the runner read authentication material and
reserve a call. Runtime child receipts are outcomes of the sealed plan, not
new plan inputs.

## Experiment methodology

### 1. Four-pair exploratory pilot

The first pilot has four unique tasks and eight calls:

- two `low` tasks
- two `medium` tasks
- one `current` and one `lean` run per task
- one current-first and one lean-first task inside each stratum

`high_simulated` remains a deterministic zero-call adversarial fixture in this
pilot. A study that counterbalances all three strata needs at least six pairs
and a separately approved budget.

The task order is generated from a fixed seed before any result and bound to
the plan digest. Pair members run adjacent to reduce provider-time drift. No
failed or unfavorable result is rerun.

### 2. Analysis contract

The pre-registered `AnalysisContract` fixes:

- exact task and pair cardinality
- task and review order
- scoring rubric and integer 0-to-100 correctness scale
- machine acceptance assertions
- task weights, all equal in the first pilot
- paired estimators, threshold rounding, and tie rules
- behavioral and infrastructure failure treatment
- score-lock and unmasking sequence
- the HIGH-severity rubric and adjudication rule

For each complete pair:

- correctness delta is `lean_score - current_score` in scale points;
- reported token total is input plus output tokens; cached input and reasoning
  output are diagnostic subsets and are not added twice;
- wall time is measured from model-process launch through its terminal event
  and excludes the separate machine validator;
- each efficiency reduction is
  `(current_value - lean_value) / current_value`;
- an efficiency metric is eligible for the decision gate only when all four
  pairs are complete and all four current baselines are greater than zero;
- efficiency ratios and threshold comparisons use exact rational arithmetic;
  decimal rounding is display-only;
- an ineligible metric does not count toward the two-of-three efficiency gate.

A valid run that fails behavior remains in the table and receives its actual
machine result and blind-review score. An infrastructure failure, malformed
telemetry, missing condition, invalid receipt, or unresolved reviewer
abstention makes the execution state `partial`; no comparative aggregate is
emitted.

For four values, the median is the arithmetic mean of the two middle values
after ascending sort. The full paired table is retained. Aggregates never
replace it.

### 3. Masked review

Condition-neutral IDs, sanitized diffs, and independently randomized review
order are generated before review. The condition mapping remains sealed until
all correctness scores and active-review-time records are locked.

The review contract defines the rubric, abstention, tie, adjudication, and
active-time pause rules. A leakage scan rejects explicit profile labels,
private paths, receipt names, and telemetry from the reviewer packet.
Reviewer familiarity with output style may still reveal a condition, so the
result is called masked review rather than guaranteed blinding.

A `MaskedReviewPacketReceipt` binds the plan, eligible pilot terminal records,
packet digest, randomized order, leakage-scan result, rubric digest, and opaque
reviewer identifiers. `ScoreLockReceipt` then binds the scores and active-time
records. `UnmaskReceipt` is valid only as a child of that score lock, and
`DecisionReceipt` binds the unmasked mapping and decision calculation. Packet
replacement, order mutation, rubric mutation, premature unmasking, or
post-lock score changes invalidate the chain.

### 4. Decision rules

The four-pair pilot may emit only:

- `reject_for_safety`
- `advance_to_larger_study`
- `inconclusive`

Decision precedence is:

1. a pre-registered deterministic absolute safety or approval-boundary
   assertion in a valid `lean` terminal emits `reject_for_safety`, even if
   another pair is partial;
2. otherwise, a partial or incomplete experiment emits `inconclusive` without
   comparative aggregates;
3. otherwise, a complete masked-review chain emits `reject_for_safety` for a
   receipt-valid HIGH regression or complete-pair machine regression;
4. otherwise, the complete experiment applies the advancement thresholds.

A machine-acceptance regression can emit `reject_for_safety` only from a
complete pair in which `current` passes a pre-registered assertion and `lean`
fails it. A human or comparative safety finding cannot override a partial
experiment without the complete masked-review and unmask chain.

It may emit `advance_to_larger_study` only when:

- every pair and receipt is complete and valid;
- there is no safety or machine-acceptance rejection;
- median paired correctness delta is at least negative 5 scale points; and
- at least two of median token reduction, wall-time reduction, and
  active-review-time reduction are at least 20 percent.

All other results are `inconclusive`. These thresholds are screening
guardrails for deciding whether a larger study is worth running. They do not
establish superiority, equivalence, or permission to replace the global
policy.

## Failure handling

Phase A fails closed for:

- non-canonical or mutable input
- changed bundle, task source, plan, schema, or receipt
- unverified repository topology or aliased, unsupported, or oversized Git
  objects
- task materialization or seal mismatch
- project instructions or configuration forbidden by the first-pilot contract
- an inconsistent four-pair plan or analysis contract
- a live-only option, approval, credential, executable, subprocess, or network
  access attempt

Future live phases additionally fail closed for:

- missing or stale containment and canary receipts
- changed executable, model, effort, argv, environment, validator, or price
  snapshot
- invalid ledger, active lock, replay, or budget exhaustion
- malformed telemetry, overflow, timeout, process failure, or redaction
  failure
- unexpected write, host read, tool network, plugin, MCP, hook, skill, or
  project configuration

Cleanup removes only paths whose captured identities still match. Replacements
are preserved and reported for manual inspection.

## Validation strategy

Implementation follows test-first slices and Python 3.9 standard-library
compatibility.

### Phase A focused tests

- exact canonical schemas, duplicate-key rejection, deep immutability, and
  canonical-bytes-to-frozen-value round-trip identity without changing the
  legacy codec
- `TaskSourceTrustReceipt` and `TaskSnapshotReceipt` to corpus to
  `ExperimentPlanDigest` cascade and one-field mutation matrices
- corpus qualification, reference and negative controls, behavior-category
  mutant coverage, difficulty and exclusion mutation, and post-result
  replacement rejection
- exact four-pair planner, stratum balance, order sealing, incomplete-pair
  classification, score locking, and 5-point/20-percent boundary tables
- token-total subset semantics, zero-baseline eligibility, unrounded threshold
  comparison, even median, and safety-over-partial decision precedence
- bounded telemetry JSONL event-order and subset-boundary fixtures
- synthetic runtime transition tests proving child receipts do not mutate the
  plan digest, every post-reservation stage can stop, and exactly one
  experiment terminal is allowed
- stop-receipt reason, consumed allowance, unresolved reservation, safety
  basis, stop-after-terminal, and `advance_to_larger_study` rejection tables
- preflight process, authentication, executable, and network poison pills that
  prove `model_calls=0`
- legacy argv, CLI JSON bytes, classifications, and exit-code golden fixtures

### Task materializer conformance tests

- malicious fsmonitor, hooks, filters, includes, alternates, promisor state,
  replace refs, LFS, and submodule fixtures do not execute a sentinel
- gitfile, external `commondir`, linked worktree, `core.worktree`, quarantine,
  and external object-directory fixtures are blocked without reading their
  sentinels
- loose object, pack, index, bitmap, commit-graph, and multi-pack-index
  symlink, hardlink, FIFO, socket, and special-file topology rejection
- operator-owned local-clone provisioning receipt, no-local-hardlink
  requirement, pre/post topology seal, and Git timeout/output cap enforcement
- ref input is rejected, while movement of a ref after full-OID input does not
  change the selected objects
- symlink, gitlink, special mode, traversal, Unicode/case alias, duplicate,
  missing-object, and size-limit rejection
- object mutation, target replacement, and source/target seal mismatch
- deterministic materialization and digest equality for both conditions

Existing hardened Git attack fixtures should be shared where their observable
contract applies. The task materializer still receives its own conformance
suite because the existing loader is inventory-specific.

### Future live acceptance tests

These tests are required before Phase B, not implemented as passing fakes in
Phase A:

- real subprocess sentinel tests for model-tool and validator host read,
  external write, environment secret, PATH/import hijack, child-process, and
  loopback/network denial
- base-to-derived canary receipt mutation, marker-source duplication,
  low-entropy, stale, cross-profile, and cross-plan rejection
- masked packet, review order, rubric, score lock, premature unmask, and
  decision receipt mutation
- actual multi-process ledger lock, reservation crash, restart, partial tail,
  duplicate terminal, and replay rejection
- third or duplicate-profile canary, pilot before both canaries, ninth pilot,
  duplicate task-condition, incomplete-reservation retry, exact tenth total
  reservation, and eleventh total reservation rejection
- provider-cap evidence classification and estimated-cost boundary behavior

### Test organization

Tests assert observable contracts. They do not freeze private helper call
counts or internal dataclass field order except where canonical serialization
is itself public evidence. Deterministic one-field mutation tables are
preferred over a new property-testing dependency.

### Repository gate

During development, run the smallest focused test first. At branch completion:

```bash
python3 -m unittest tests.test_live_eval_experiment -v
python3 -m unittest tests.test_live_eval_experiment_plan -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_live_eval_task_snapshot -v
python3 -m unittest tests.test_live_eval_experiment_telemetry -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
./scripts/validate_repo.sh
git diff --check
```

CI must explicitly select Python 3.9 and run full test discovery plus repository
validation. A newer default interpreter is additional coverage, not evidence
of Python 3.9 compatibility.

No repository validation command may make a model or network call.

## Review plan

After a meaningful diff, run sequential independent lenses for:

1. security and isolation
2. architecture and legacy compatibility
3. experiment validity
4. test coverage and failure-mode detection

Every material finding receives `apply`, `ask`, `defer`, or
`reject-with-reason`, followed by verification strong enough to catch the
reported failure.

The same-user authenticity threat is explicitly deferred as out of scope.
OS-specific containment implementation, real live subprocess tests, canary
calls, and pilot calls are deferred to later separately approved phases.
A safe object-database clone or VM-based source intake is deferred unless a
future scope explicitly requires arbitrary externally prepared Git
administration directories.

## Rollout

1. Approve this revised written design.
2. Implement and validate Phase A with synthetic fixtures and zero model calls.
3. Create and review TOM's private bundle as a separate local action.
4. Select a dedicated containment environment and write the Phase B design.
5. Pass real containment acceptance tests before enabling canary code.
6. Separately approve and reserve two canary calls.
7. Review the two narrow marker-observation receipts.
8. Separately approve and reserve eight pilot calls.
9. Emit only the pre-registered exploratory decision.
10. Design a larger study only when the result is
    `advance_to_larger_study` or materially ambiguous.

No phase automatically authorizes the next.
