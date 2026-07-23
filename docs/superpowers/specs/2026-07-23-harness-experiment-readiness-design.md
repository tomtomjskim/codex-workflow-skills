# Harness Experiment Readiness Design

**Status:** Draft for written-spec review; high-level design approved
**Date:** 2026-07-23
**Scope:** Zero-model-call implementation readiness for a future `current` versus
`lean` Codex harness experiment

## Decision

Add a separate harness experiment runner instead of extending the existing
workflow-conformance live-eval contract.

The new path will prepare and validate immutable harness variants, controlled
coding fixtures, invocation provenance, usage telemetry, and an experiment-wide
budget ledger. It will contain explicit live-execution gates, but this
implementation phase will not make a Codex, API, or model call.

The existing `run_eval()` and `run_harness_dry_run()` behavior and JSON schemas
remain backward-compatible.

## Goals

1. Prove statically that a future run would use one exact private
   `current|lean` bundle, one exact task snapshot, one explicit model and
   reasoning effort, and one bounded experiment budget.
2. Materialize a harness into a fresh private `CODEX_HOME`, bind it to immutable
   hashes, and reverify it immediately before a model-call reservation.
3. Provide a separate conformance-canary mode that can later establish that
   Codex consumes global guidance from the materialized `CODEX_HOME`.
4. Parse Codex JSONL usage telemetry strictly and retain only sanitized,
   structured evidence.
5. Count every reserved model call, including crashes and infrastructure
   failures, against one experiment-wide cap.
6. Keep actual policy text, representative private repositories, credentials,
   raw prompts, and raw model logs out of the public repository.
7. Fail closed when model, effort, CLI version, task snapshot, harness
   identity, telemetry, budget state, or isolation evidence cannot be proven.

## Non-goals

- Running the conformance canary or paid A/B pilot in this implementation phase.
- Selecting `current`, `lean`, or prohibition-only as the winning policy.
- Editing global `AGENTS.md`, Codex configuration, shared agents, adapters, or
  installed skills.
- Committing TOM's real current or lean policy text to this public repository.
- Treating a local token-cost estimate as an authoritative provider bill.
- Claiming that a small coding pilot proves the rate of rare security,
  authorization, production, or data-loss failures.
- Replacing deterministic policy tests, adversarial fixtures, or human approval
  gates with model evaluation.
- Adding a production dependency.

## Evidence states

The experiment path uses the following evidence vocabulary:

| State | Meaning |
|---|---|
| `static_only` | Files, hashes, manifests, invocation policy, and budgets were validated without launching Codex. |
| `model_conformance_not_run` | No canary proved that Codex consumed the selected harness. |
| `model_conformance_pass` | A separately approved canary returned the expected opaque marker under the same sealed invocation contract. |
| `pilot_not_run` | No quality-comparison task was executed. |
| `blocked` | A required invariant, capability, budget, telemetry field, or approval was missing. |
| `partial` | Some approved calls completed, but the experiment stopped before its declared set completed. |
| `pass` | Only the explicitly named machine assertion passed; it never implies overall harness superiority. |

Static preflight must never serialize model conformance or model quality as
`pass`.

## Architecture

### 1. Separate experiment boundary

Add:

- `scripts/live_eval/experiment.py`
- `scripts/run_harness_experiment.py`
- `tests/test_live_eval_experiment.py`
- synthetic, non-sensitive fixture data under
  `tests/fixtures/harness_experiment/`

The existing workflow scenario runner remains responsible for structured
workflow-policy conformance. The experiment runner is responsible for paired
coding-task evaluation. Its result types, CLI flags, artifacts, and ledger are
separate.

The CLI exposes three modes:

1. `preflight`: zero-call materialization, task validation, invocation-plan
   validation, and budget validation.
2. `canary`: separately approved live proof of profile consumption; excluded
   from quality samples.
3. `pilot`: separately approved paired coding-task execution.

`preflight` is the only mode executed in this implementation phase. Unit and
integration tests exercise `canary` and `pilot` through injected fake process
objects only.

### 2. Private bundle boundary

The real bundle remains outside the public repository:

```text
harness.json
profiles/current/AGENTS.md
profiles/lean/AGENTS.md
shared/agents/*.toml
shared/common-agents/*.md
```

The existing fixed-inventory loader remains authoritative. The experiment
runner records only path-free identifiers, file counts, modes, and SHA-256
digests.

The private bundle must be a real, non-symlink directory with the existing
strict modes and inventory. It must not contain credentials, customer data,
private repository contents, or absolute local paths in retained output.

No command in this phase copies TOM's real global policy into a durable bundle.
Creating or updating that private bundle is a separate local action after the
runner is implemented and reviewed.

### 3. Controlled task snapshots

Each task comes from a clean Git commit and a canonical task manifest. The
manifest contains:

- schema version and task ID
- source tree hash
- prompt hash
- difficulty stratum: `low`, `medium`, or `high_simulated`
- literal repository-relative allowed write paths
- sandbox mode
- fixed validation argv
- timeout and output-byte limits
- expected machine assertions

The public repository contains only synthetic fixtures that test the contract.
TOM's representative task snapshots remain private external inputs.

Task preparation rejects dirty sources, symlink escapes, untracked files,
non-canonical paths, mutable source identity, project-level Codex configuration,
hooks, MCP configuration, plugins, credentials, and production endpoints.
Project `AGENTS.md` may be present only when it is part of the task snapshot and
identical for both profiles.

Every condition receives a fresh private worktree or equivalent clean copied
tree from the same commit. A task run never reuses a conversation, worktree,
cache namespace, or model artifact from the other condition.

### 4. Invocation policy

The invocation explicitly pins:

- model ID
- `model_reasoning_effort`
- Codex CLI executable identity and version
- sandbox mode
- approval policy `never`
- `sandbox_workspace_write.network_access=false`
- web search disabled
- empty user configuration
- exact `CODEX_HOME`
- exact working directory and temporary directory
- output schema
- environment allowlist

`--ignore-user-config` remains enabled so `$CODEX_HOME/config.toml` cannot
change the run. `--ignore-rules` also remains enabled because current Codex CLI
defines it as ignoring user and project execpolicy `.rules` files, not
`AGENTS.md`.

Official Codex instruction discovery reads the global `AGENTS.md` from
`CODEX_HOME`. Static preflight proves only that the intended file is present,
sealed, and selected by the invocation environment. Actual consumption remains
`model_conformance_not_run` until the live canary passes.

Legacy live-eval invocations keep their existing read-only argv byte-for-byte.
Experiment-specific sandbox, effort, network, and working-directory options are
added through a separate immutable configuration path or optional fields whose
defaults reproduce the legacy invocation exactly.

### 5. Consumption canary

The canary uses a canary-only copy of each profile. It appends an opaque random
marker to the copied global `AGENTS.md`, updates the canary manifest, seals the
home, and asks Codex for that marker through a restrictive output schema.

The marker:

- is generated for one experiment
- contains no policy text or profile name
- is never used in a quality sample
- is retained only as a digest after verification

The canary passes only when:

- exactly one structured response is present
- its marker hash matches
- exactly one valid `turn.completed` usage event is present
- the sealed harness and invocation identities remain unchanged
- no unexpected tool, network, plugin, MCP, hook, or skill event appears

Passing one profile does not pass the other. A canary result is bound to the
bundle digest, profile digest, Codex version, model, effort, and invocation
policy digest. Any change invalidates it.

### 6. Usage telemetry

Codex `--json` emits a `turn.completed` event with:

- `input_tokens`
- `cached_input_tokens`
- `output_tokens`
- `reasoning_output_tokens`

Add an immutable `UsageTelemetry` value that requires exactly those four
non-negative integers. Experiment parsing rejects:

- missing or duplicate terminal usage
- booleans, floats, strings, negative integers, or unknown usage keys
- `cached_input_tokens > input_tokens`
- terminal usage after an error or unsupported terminal event
- multiple structured final responses

The experiment record also captures monotonic wall time, model-call count,
attempt count, command/tool item count, file-change item count, and terminal
classification. Raw reasoning, command output, prompts, policy text, secret
values, and home paths are not copied into the summary.

Telemetry parsing occurs from the already bounded and redacted JSONL artifact.
A telemetry parse failure blocks the result and prevents another reservation.

### 7. Experiment-wide budget ledger

The ledger is private mode-0600 canonical JSONL under a mode-0700 experiment
directory. One exclusive lock file permits only one active experiment process.
A stale or replaced lock blocks automatic continuation and requires explicit
manual recovery.

Before each subprocess launch, the runner synchronously appends and fsyncs a
`reserved` record. A reservation permanently consumes one call even if the
process never starts, crashes, times out, or returns an infrastructure failure.

Every record contains:

- schema version, experiment ID, sequence number, and previous-record hash
- condition and task IDs as opaque identifiers
- bundle, task, model, effort, CLI, invocation-policy, and price-snapshot hashes
- state: `reserved`, `completed`, `failed`, or `blocked`
- cumulative calls, tokens, estimated cost, and elapsed time
- terminal reason when applicable

The ledger rejects missing, duplicate, out-of-order, stale, hash-invalid, or
identity-incompatible records.

Local hard stops:

- maximum reserved model calls
- maximum elapsed seconds
- maximum raw retained bytes
- maximum cumulative reported tokens between calls
- no next call when the declared per-call monetary reservation exceeds the
  remaining experiment amount

Absolute provider billing cannot be guaranteed from local token telemetry
alone because one in-flight call can exceed an estimate and ChatGPT credits do
not necessarily map to API token prices. Live mode therefore also requires:

- a numeric experiment monetary cap
- a fixed, operator-supplied price snapshot used only for labeled estimates
- an operator attestation that a provider-side spend or credit cap is active

Without all three, live execution returns `blocked` before authentication or a
subprocess. Output labels local cost as `estimated_cost`, never `actual_cost`,
unless a future authoritative billing receipt is supplied.

### 8. Approval and call gates

Live canary or pilot execution requires all of:

- explicit live mode
- explicit `--approve-model-calls`
- positive maximum model-call count
- positive numeric monetary cap
- fixed price-snapshot file
- explicit provider-cap attestation
- available process-local API key
- valid model allowlist entry
- current static preflight receipt
- current conformance receipt for pilot mode

Omitting or changing any approval or numeric-budget item blocks before reading
credentials or resolving the Codex executable. Static preflight never reads an
API key.

The initial recommended pilot policy is encoded as documentation and a
validated manifest limit, not as an automatic execution:

- compare only `current` and `lean`
- four paired tasks, eight primary calls
- at most two additional reserved calls
- maximum ten calls for the first decision gate
- concurrency one
- no behavioral retry
- infrastructure failures remain visible and consume the cap

### 9. Paired execution and evaluation

The pilot manifest fixes task order before any result exists. Each stratum
counterbalances condition order. A failed condition is retained; it is never
silently rerun.

Machine evaluation and blind review remain separate:

- machine checks run against the produced fixture after the model process ends
- blind-review material uses condition-neutral IDs and sanitized diffs
- token and timing data remain hidden until quality scoring is locked

Initial decision guardrails:

- zero HIGH safety or approval-boundary regressions
- acceptance behavior not materially worse than `current`
- blind correctness median degradation no greater than 5%
- at least two of token use, wall time, and TOM review time improve by 20%

These are operational guardrails for a small sequential evaluation, not a
statistical population claim.

## Failure handling

The experiment fails closed for:

- changed bundle, task, executable, model, effort, argv, environment, or price
  snapshot
- dirty or aliased task source
- missing or stale preflight/conformance receipt
- invalid ledger or active lock
- budget exhaustion
- missing or malformed usage
- output overflow, timeout, process failure, or redaction failure
- unexpected external write, network capability, plugin, MCP, hook, skill, or
  project configuration
- secret-like input in a retained field

Cleanup removes only paths whose identity still matches the paths created by
the experiment. Replacements are preserved and reported as manual cleanup
required.

## Security and privacy

- External task text, fixtures, logs, model events, and generated files are
  treated as untrusted data.
- No credential, API key, policy text, raw prompt, raw reasoning, private path,
  or private source code is serialized into the public summary.
- Raw redacted artifacts stay in the private experiment directory with mode
  0600 and are not committed.
- Public tests use only synthetic fixtures and fake process results.
- No production, deployment, push, migration, authentication, or external
  integration operation is part of a task fixture.

## Validation strategy

Implementation follows test-first slices.

### Focused unit tests

- experiment and task manifest schema
- canonical hashes and immutable values
- telemetry parsing and rejection cases
- ledger reservations, hash chain, crash accounting, locks, and budget stops
- price-estimate labeling and missing provider-cap blocking
- approval-gate ordering before credential or executable access

### Integration tests

- both profiles materialize into distinct sealed homes
- invocation provenance binds the selected profile and task
- fake canary proves marker matching and rejects cross-profile evidence
- fake pilot records paired calls without leaking condition labels
- source, home, task, ledger, and cleanup mutation fail closed
- legacy runner JSON and argv contracts remain unchanged

### Repository gate

During development, run the smallest focused test first. At branch completion:

```bash
python3 -m unittest tests.test_live_eval_experiment -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
./scripts/validate_repo.sh
git diff --check
```

No repository validation command may make a model or network call.

## Review plan

After a meaningful diff exists, run sequential independent review lenses:

1. security and isolation
2. experiment validity and evidence claims
3. compatibility, test coverage, and operational complexity

Every material finding receives `apply`, `ask`, `defer`, or
`reject-with-reason`, followed by the smallest verification that would catch
the reported failure.

## Rollout

1. Implement and validate the zero-call path with synthetic fixtures.
2. Create TOM's real private bundle in a separately approved local location.
3. Run static preflight and record only path-free hashes.
4. Obtain numeric call and monetary caps plus provider-side cap confirmation.
5. Run two separately approved conformance-canary calls.
6. Review canary evidence before approving the paired pilot.
7. Run at most ten pilot calls.
8. Expand toward the twelve-task study only if the first decision remains
   materially ambiguous.

No phase automatically authorizes the next.
