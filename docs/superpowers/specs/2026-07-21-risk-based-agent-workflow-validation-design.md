# Risk-Based Agent Workflow Validation Design

## Goal

Make multi-agent and cross-layer delivery reproducible without adding full
workflow ceremony to every small edit. The system should require explicit
interfaces, ownership, and handoff evidence only when the work benefits from
parallel execution or touches a high-risk contract.

## Scope

This design covers three coordinated changes:

1. Add a versioned `parallel_work_contract` for independent frontend, backend,
   API, data, or other workstreams.
2. Add deterministic contract checks plus opt-in live Codex evaluations for the
   workflow skills.
3. Align the shared reviewer roles as read-only roles and activate the existing
   Claude global adapters through symlinks.

The public workflow skill repository remains portable. Personal global role
rules remain under `$HOME/.agents/` and must not become a runtime
dependency of the public plugin.

## Non-Goals

- Do not run the full acceptance suite for ordinary application edits.
- Do not require a durable specification file for every task.
- Do not add a production dependency or hosted evaluation service.
- Do not make live model output a blocking signal for normal application CI.
- Do not define project-specific frontend, backend, database, or deployment
  commands in global rules.
- Do not claim to defend against a malicious root agent or compromised local
  runtime that can rewrite manifests, validator code, receipts, or artifacts.

## Threat Model

The primary goal is to prevent failures from incomplete context, accidental
omission, stale evidence, incorrect routing, prompt drift, and fallible agents.
The validator also reduces policy bypass by deriving decisions instead of
trusting route and required-set self-declarations.

The following trust levels are explicit:

- **Covered:** cooperative or fallible agents when the manifest can be
  cross-checked against an approved plan inventory, repository-owned interface
  inventory, or actual diff. This includes stale evidence and incorrect route
  selection.
- **Detected as unverified:** dependency or interface completeness when no
  independent inventory exists. The workflow does not claim the manifest is
  complete and cannot auto-select the independent parallel route.
- **Partially covered:** a subagent that attempts to shrink its declared scope;
  the validator cross-checks the shared manifest, hashes, and derived sets.
- **Not covered:** a malicious root agent, compromised runtime, or actor able to
  alter the validator, shared manifest, checkout, or receipt store. Defending
  that boundary requires runtime-issued identities, signed receipts, append-only
  logs, and dispatch hooks, which are deferred until an observed need justifies
  their platform cost.

Hashes and receipts in this design provide integrity and traceability inside the
declared trust boundary. They are not cryptographic proof against an actor that
controls both the input and validator.

## Risk-Based Activation

The workflow must select the smallest route that can catch the relevant failure.

| Change class | Parallel contract | Deterministic checks | Live Codex eval |
|---|---|---|---|
| A0/A1 copy, docs, isolated styling, single-file maintenance | Not used | Existing smallest relevant check | Not used |
| A2 multi-file, single workstream | Optional lightweight task/test plan | Relevant repository tests | Not used |
| Concurrent workstreams with a shared interface or explicit integration dependency | Required | Contract schema and handoff checks | Not used by default |
| Concurrent workstreams with overlapping exclusive write paths | Not executable in v1; split ownership or serialize, then revalidate | Path-overlap rejection | Not used |
| Concurrent workstreams with no shared interface, write surface, or integration dependency | Not used; validate the canonical manifest only | Ownership, inventory completeness, and path-disjointness checks | Not used |
| Public API, auth, database, or shared integration contract | Required when multiple consumers or implementers are involved | Contract and integration checks | Targeted when agent/workflow behavior changed |
| Shared agent or workflow instruction change | Required only when the change itself creates parallel work | Full deterministic policy checks | Three tagged scenarios by default, five maximum before release |
| Workflow release candidate | As applicable | Full repository checks | Selected suite or all 26 scenarios by explicit release decision |

Live evaluation is separated from the default validation command because model
calls add latency, cost, and nondeterminism. A live failure records its command,
model/runtime metadata, raw artifact path, and whether the failure is an
invocation error, contract failure, or model variance. Model variance is stored
as a separate `live_eval_classification`; verification evidence continues to use
the existing `partial` or `blocked` result values.

## Parallel Work Contract

The canonical contract is documented in the workflow plugin under the intake
references. Shared global roles repeat only the minimum fields needed to work
when the plugin is not active.

Machine inputs use canonical UTF-8 JSON parsed with Python's standard-library
`json` module. YAML examples may be shown for readability but are non-authoritative
and must be generated from canonical JSON. This avoids a PyYAML dependency and
supports the repository's current Python 3.9 baseline.

Canonical bytes use these exact Python 3.9-compatible rules:

- parse objects with duplicate-key rejection;
- allow only objects, arrays, strings, integers, booleans, and null; reject
  floating-point values, NaN, and Infinity;
- require every string to already be Unicode NFC;
- serialize with `sort_keys=True`, `separators=(",", ":")`,
  `ensure_ascii=False`, and `allow_nan=False`;
- encode as UTF-8 without BOM or trailing newline;
- compute lowercase SHA-256 over those bytes.

Golden tests cover key order and whitespace equivalence, Unicode rejection,
duplicate-key rejection, forbidden numbers, and stable bytes across supported
Python versions.

Every concurrent route starts from one manifest. The manifest is the sole source
of truth for workstream IDs, owners, paths, dependencies, inputs, and outputs.
Agents do not submit `selected_route`, affected consumers, required handoffs, or
required reviewers; the validator derives them.

```json
{
  "schema_version": 1,
  "plan_hash": "sha256:<canonical-plan-hash>",
  "inventory_hash": "sha256:<approved-inventory-hash-or-null>",
  "changed_surfaces": ["api", "ui"],
  "workstreams": [
    {
      "id": "frontend",
      "owner": "developer-frontend",
      "scope": ["src/ui"],
      "exclusive_write_paths": ["src/ui"],
      "depends_on": [],
      "consumes": [{"kind": "api", "id": "settings-v1"}],
      "produces": [{"kind": "component", "id": "settings-form"}]
    },
    {
      "id": "backend",
      "owner": "developer-backend",
      "scope": ["src/api"],
      "exclusive_write_paths": ["src/api"],
      "depends_on": [],
      "consumes": [],
      "produces": [{"kind": "api", "id": "settings-v1"}]
    }
  ]
}
```

The inventory is an independent projection derived automatically from an
explicitly approved task plan, repository-owned API/event/schema catalog, or
review-time actual diff. It lists expected workstreams, known interface IDs, and
changed-surface triggers. Users and agents do not manually duplicate the
manifest into a second document: `workflow prepare-coordination` generates both
canonical files and their hashes from the approved plan plus repository evidence.
When no durable plan file exists, the command accepts the current approved
session-plan snapshot as canonical JSON.
The validator reports manifest completeness as `verified`, `mismatch`, or
`unverified`. An independent route requires `verified`; `mismatch` blocks, and
`unverified` falls back to single-owner sequential execution unless the user or
Architect explicitly approves the recorded uncertainty.

When the derived route is contracted, immutable contract core references the
manifest and inventory hashes instead of copying workstream fields. Mutable
execution evidence is stored separately to avoid circular hashing.

```json
{
  "contract_core": {
    "schema_version": 1,
    "manifest_hash": "sha256:<canonical-manifest-hash>",
    "inventory_hash": "sha256:<approved-inventory-hash>",
    "revision": 1,
    "parent_contract_core_hash": null,
    "contract_owner": "architect",
    "integration_owner": "developer-integration",
    "derived_profile": {
      "shared_interface": true,
      "path_overlap": false,
      "integration_dependency": true
    },
    "extension_requirements": {
      "interface_contract": {},
      "path_ownership": {},
      "integration": {}
    }
  },
  "execution_ledger": {
    "contract_core_hash": "sha256:<canonical-contract-core-hash>",
    "status": "draft",
    "entries": [],
    "integration_gate": {"status": "open"}
  }
}
```

`contract_core_hash` covers only canonical `contract_core` bytes. Each ordered
ledger entry references that core hash, its artifact digest, and the previous
entry hash. `ledger_hash` covers the ordered canonical entry hashes. Evidence is
not included in the core hash domain.

Every evidence entry binds to immutable context within the declared trust model:

```json
{
  "contract_core_hash": "sha256:<canonical-contract-core-hash>",
  "previous_entry_hash": "sha256:<previous-ledger-entry-or-null>",
  "checkout_tree_hash": "<git-tree-or-content-hash>",
  "producer_id": "<canonical-runtime-agent-id>",
  "command_or_scenario_id": "<command-or-stable-scenario-id>",
  "artifact_digest": "sha256:<artifact-hash>",
  "run_id": "<unique-run-id>",
  "recorded_at": "<UTC-RFC3339>"
}
```

The validator rejects `producer_id: "unverified"`: schema version 1 has no
authority-defer receipt. A handoff or checkpoint producer must be either the
source workstream owner or the current integration owner. An acknowledgement
producer must be either its subject workstream owner or the contract owner.
Session-scoped canonical task IDs are sufficient only when they equal the
corresponding canonical owner identity.

`status: frozen` is the entry condition for parallel implementation when the
validator derives a shared interface or an explicit integration dependency.
Completely disjoint workstreams use only the validated manifest. Overlapping
exclusive write paths are a diagnostic, blocked-only condition in version 1:
split the paths or serialize the writers, then rerun validation. Every accepted
version-1 receipt therefore has `path_overlap: false`.

A contract-core change increments `revision`, moves ledger status to `changed`, and marks
all earlier handoffs, acknowledgements, and checkpoints `stale`.

### State Transitions And Authority

| Profile and transition | Actor | Prerequisites |
|---|---|---|
| independent manifest → dispatch | PM | unique owners; path-disjointness and root-boundary checks pass |
| path-overlap diagnostic → revalidation | PM | split exclusive paths or serialize writers; revise the manifest; rerun validation before dispatch |
| shared-interface `draft → frozen` | contract owner | Architect definition and derived consumer approvals complete; API/security review only when the trigger matrix requires it; validator passes |
| integration-dependency `draft → frozen` | contract owner | derived handoffs, checkpoints, and required reviewers defined; affected owners approve; validator passes |
| `frozen → changed` | contract owner or approved change requester | change reason and derived affected consumers recorded; revision incremented; prior evidence marked stale |
| `changed → frozen` | contract owner | affected consumers acknowledge the current contract hash; required checks and reviews rerun; validator passes |

The contract owner controls contract state. The integration owner cannot freeze
or modify the contract.

Contract extensions are conditionally required to avoid ceremony for narrow
conflicts:

- Every concurrent route requires only the manifest, unique owners, declared
  inputs/outputs, and normalized exclusive write paths.
- `shared_interface: true` additionally requires `interface`, affected consumer
  approvals, acknowledgements, compatibility, errors, auth when applicable, and
  consumer-facing examples.
- `path_overlap: true` is diagnostic-only and blocks version-1 validation. The
  `path_ownership` extension is reserved for a future executable profile; it is
  not active and cannot make an overlapping manifest dispatchable.
- `integration_dependency: true` additionally requires revision-bound handoffs,
  checkpoints, required reviewers, and the integration gate.

The contract can remain an in-session structured block. A durable SPEC or TASK
file is created only when the repository requires it, the user requests it, or
the existing artifact-level rules recommend and authorize it.

## Role Responsibilities

- PM activates the contract only for covered concurrent workstreams, assigns
  owners, records dependencies and write paths, and names the integration owner.
- Architect defines the interface, data flow, failure modes, compatibility, and
  contract revision before parallel implementation begins.
- API Reviewer checks request/response shapes, status and error behavior, auth,
  compatibility, and consumer expectations before the contract is frozen.
- Developer refuses to infer a shared contract silently, edits only its assigned
  write paths, records the contract revision and hash consumed, and returns
  produced artifacts plus hash-bound validation evidence.
- QA Engineer owns reproduction, validation planning, execution, and evidence.
- Test Coverage Reviewer independently audits whether assertions would catch the
  recorded failure modes. It does not implement fixes.
- Other reviewers remain read-only and hand accepted changes to Developer or the
  applicable implementation role.

The adversarial review matrix maps changed-surface tags to canonical agent names.
The validator loads that routing artifact as authority, derives the required
reviewer set from it, and requires the registry to exactly cover each canonical
`(lens, canonical_agent)` pair. A caller-supplied trigger matrix is accepted only
when it exactly matches the loaded routing artifact. A manual
exclusion requires a user-approved defer receipt and residual risk. Reviewer
execution uses this registry:

```json
{
  "reviewer_registry": [
    {
      "lens": "security",
      "canonical_agent": "security-reviewer",
      "required": true,
      "contract_core_hash": "sha256:<canonical-contract-core-hash>",
      "status": "dispatched | completed | failed | not_available | skipped | stale",
      "dispatch_evidence": {},
      "completion_evidence": {},
      "defer_receipt": null
    }
  ]
}
```

A required reviewer in `failed` or `not_available` blocks integration unless the
user explicitly approves a defer with an owner, reason, and residual risk.
`reviewers_run` is derived only from completed registry entries for the current
contract hash. A revision change marks earlier reviewer entries `stale`.

## Validation Architecture

### Deterministic Checks

A dependency-free Python policy checker validates repository-controlled rules:

- required workflow files and exact enum vocabularies;
- `parallel_work_contract` required keys and version;
- canonical reviewer lens-to-agent mappings;
- acceptance scenario identifiers and machine-readable expectations;
- sample outputs against evidence rules, including unsupported HIGH findings;
- tracked-file public hygiene rather than a hand-maintained directory list.

The existing `scripts/validate_repo.sh` invokes this checker. These checks must
run without network access or a model call and should remain fast enough for each
workflow repository change.

A stable, versioned CLI prepares and validates coordination artifacts. The
entrypoint is resolved from the installed workflow skill/plugin, not from the
target repository's current directory:

```bash
workflow prepare-coordination \
  --repo-root <approved-repo-root> \
  --plan <approved-plan.json> \
  --out-dir <temporary-coordination-dir> \
  --json
```

This command generates the manifest and inventory together; independent work
does not require a manually authored second artifact. Validation then uses:

```bash
workflow validate-coordination \
  --repo-root <approved-repo-root> \
  --manifest <manifest.json> \
  --inventory <approved-inventory.json> \
  --contract <contract.json> \
  --json
```

`--repo-root` is the sole filesystem root authority; the manifest cannot declare
or override it. The validation command canonicalizes the generated manifest and inventory,
computes their hashes, reports completeness, derives the route, profiles,
affected consumers, handoffs, acknowledgements, and reviewer set, then validates
the conditionally required contract extensions. Before every
concurrent dispatch, PM must run the CLI. No route may dispatch without exit code
0 and a machine-readable receipt containing CLI/schema version, manifest and
contract hashes, derived profiles and required sets, normalized path results,
checkout tree hash, validation timestamp, and a unique run ID.

If the CLI is missing, incompatible, or cannot produce a valid dispatch receipt,
the workflow falls back to one owner executing the workstreams sequentially and
records `parallel_validation: blocked`; it never silently skips validation.

The validator rejects:

- missing owners or duplicate workstream IDs;
- dependencies that reference missing workstreams or form a cycle;
- dependency dispatch before a current-revision handoff is ready;
- exact and ancestor/descendant path overlap across exclusive write sets;
- absolute paths, `..` traversal, or symlink resolution outside the approved root;
- shared paths without exactly one owner;
- stale revision handoffs, acknowledgements, checkpoints, or reviewer evidence;
- any submitted contract profile or required set that differs from its derived
  value;
- an independent derived route when `consumes`/`produces` identifiers, normalized
  path overlap, or dependency edges show shared integration;
- empty required sets unless the derivation report proves no matching consumer,
  handoff, acknowledgement, or reviewer exists;
- manifest workstreams, interfaces, or changed surfaces that conflict with an
  approved plan inventory, repository-owned interface catalog, or review-time
  actual diff;
- an independent route whose manifest completeness is not `verified` unless an
  explicit user/Architect uncertainty receipt is present.

The initial implementation forbids glob metacharacters in write paths, normalizes
Unicode and case according to the target filesystem, resolves every existing
component with `lstat`/`realpath`, validates nonexistent outputs through their
nearest existing parent, and repeats path validation at handoff. This remains an
executable workflow gate, not a claim that the runtime makes policy
bypass technically impossible. A future collaboration-tool hook can strengthen
enforcement only after hook support and failure behavior are verified.

Each dispatch receipt records the base checkout tree hash. Parallel writers must
use isolated worktrees or produce isolated patch artifacts; if the runtime cannot
attribute changes to a workstream, it falls back to sequential execution. At
handoff, the CLI collects tracked and untracked changed paths relative to the
base tree and rejects any path outside that workstream's exclusive paths or its
derived shared-path ownership. This detects out-of-scope writes at handoff; it
does not claim to block them during execution without a runtime hook.

### Live Evaluation

An explicit runner accepts scenario IDs or a release-suite flag. It launches a
fresh Codex process with an isolated temporary `CODEX_HOME`, read-only sandbox,
non-interactive approvals, and network, MCP, plugins, and hooks disabled. It
stores prompt and response artifacts in a mode-`0700`, untracked temporary
directory and applies deterministic assertions to the structured response.

The supported baseline invocation uses a neutral temporary working directory,
`--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `--sandbox read-only`,
top-level `-a never`, and `--output-schema`. Runner integration tests must prove
that project instructions, MCP servers, plugins, hooks, shell network access, and
unexpected skills are absent before this isolation claim is enabled. If the
installed CLI cannot prove a required isolation property, the run is `blocked`,
not degraded silently.

Authentication is process-local and separate from configuration. CI and
automated live eval accept only an explicitly supplied API-key environment
variable and never copy, link, print, or retain credential files. An environment
with OAuth login only records `live_eval_classification: blocked_auth` and
`verification_evidence.result: blocked` until Codex provides a verified,
separate auth-directory mechanism. Existing OAuth credentials are not copied
into the temporary `CODEX_HOME`.

The API key is available only to the Codex client transport. Agent tool
subprocesses use a minimal explicit environment allowlist that excludes the key
name and value and all other credential-like variables. Before live eval is
enabled, an isolation test must prove shell commands, hooks, plugins, and MCP
servers cannot observe either the authentication variable name or its value. If
the installed CLI cannot enforce or prove that separation, the runner records
`live_eval_classification: blocked_isolation` with a `blocked` result.

Each scenario has a stable ID, schema version, changed-surface tags, prompt
fixture, required fields, forbidden behavior, expected status, assertion rules,
and timeout. The run manifest records the selected scenario IDs, selection
reason, model, reasoning effort, Codex CLI version, working directory, timeout,
exit code, prompt hash, and artifact paths. Artifacts are redacted before
retention and deleted after seven days unless the operator explicitly preserves
them for a release investigation.

The runner requires an explicit model from a checked-in allowlist and never
falls back to a personal default model. A scenario defines maximum prompt bytes
and expected response-schema size. The run records available token usage and
cost metadata; when the CLI does not expose monetary cost, the runner reports
cost as `unknown` and uses the model-call and wall-time caps as its enforceable
budget. It must not claim a monetary cap it cannot measure.

Targeted and release budgets are separate:

- targeted run: three scenarios by default, five model calls maximum, ten
  minutes total wall time, concurrency one;
- release suite: explicit operator approval, 26 scenarios maximum, 30 model calls
  including infrastructure retries, 45 minutes total wall time, concurrency two;
- budget exhaustion records `live_eval_classification: blocked_budget` with a
  `blocked` verification result and cannot be converted to PASS;
- one comparison run for model-variance diagnosis is pre-approved only inside
  the release budget and preserves the original failure.

`live_eval_classification: model_variance` requires two completed runs with the
same scenario, checkout hash, model, reasoning effort, prompt hash, and runner
configuration that produce different deterministic assertion outcomes. A single
failed outcome is a contract failure unless an operator explicitly authorizes a
comparison run; it cannot be labeled variance by judgment alone.

Default behavior:

- no live evaluation for application code changes;
- three affected scenarios by default and at most five for workflow or
  shared-agent behavior changes, selected through changed-surface tags;
- all scenarios only for an explicit release-candidate decision;
- live invocation failures are reported separately from behavioral failures;
- no behavioral retry; one retry is allowed only for a recorded infrastructure
  invocation failure;
- each targeted run is capped at five model calls and ten minutes of wall time.

The first implementation may provide the runner and scenario selection contract
without forcing model calls during local validation. A live execution requires
an available Codex CLI and explicitly supplied process-local API-key access.

Before any model call, the runner installs only the workflow skills from the
exact checkout under test into the isolated home using supported local links. A
fail-closed preflight resolves each loaded skill source and verifies the expected
skill names, repository commit or tree hash, skill content hashes, and plugin
manifest hash. A missing skill, unexpected additional copy, or hash mismatch
blocks the run. The manifest records the resolved checkout and hashes so results
cannot be attributed to another installed copy.

JSONL and final responses pass through streaming redaction before any retained
write. Retained files use mode `0600`; the parent directory uses `0700`.
Redaction covers credential-like values, configured secret environment names,
home-directory paths, and sensitive fixture markers. A redaction error fails
closed: raw output is deleted immediately, no artifact is retained, and the run
records `live_eval_classification: blocked_redaction` with a `blocked` result.
Negative tests must prove raw secrets and paths
cannot reach retained artifacts or logs.

## Claude Adapter Alignment

All existing files in `$HOME/.agents/adapters/claude/` can be activated by
symlinks in `$HOME/.claude/agents/`. Installation is idempotent: an
existing correct link is retained, a conflicting file or link is reported and
not overwritten silently.

The public repository validates the installer only against a temporary target
root. Actual global installation is a separate, opt-in local operation:

1. run tests against a temporary target;
2. run `--dry-run` against the requested global target;
3. present the exact link manifest and all conflicts;
4. obtain explicit approval for the global write;
5. create links atomically where possible and stop on conflicts;
6. run an integrity check and report any partial installation.

The installer uses `lstat`, validates source and parent directories, rejects
targets outside the approved root, and never replaces a conflicting user-owned
file silently. Public plugin release status does not depend on personal global
installation state.

Reviewer roles are consistently read-only across common rules and adapters.
When a finding needs a change, the reviewer returns a concrete remediation and
handoff target instead of editing. Developer and explicitly designated
implementation roles retain write capability.

## Error Handling

- Missing or ambiguous interface ownership blocks contract freezing.
- Overlapping exclusive write paths block parallel dispatch.
- Invalid dependency references or cycles block parallel dispatch.
- A changed contract blocks integration until consumer acknowledgement and
  targeted revalidation are recorded.
- A missing reviewer agent becomes `not_available`, not `completed`.
- A live eval tool failure becomes `blocked`; model variance becomes `partial`
  with `live_eval_classification: model_variance`, never PASS.
- A conflicting Claude adapter target is reported for manual disposition rather
  than replaced.

Every blocked, stale, failed, unavailable, or deferred state records an owner,
blocker, next recovery action, and expiry or explicit re-review condition. A
state that reaches its expiry remains blocked; it never auto-converts to PASS.

## Testing Strategy

Implementation follows red-green verification:

1. Add failing deterministic tests for contract activation, required fields,
   reviewer mapping, exact canonical JSON bytes and hash-domain stability,
   duplicate/number/Unicode rejection, sample evidence consistency, and
   tracked-file hygiene.
2. Implement the smallest checker and policy changes needed to pass them.
3. Add failing contract-validator tests for duplicate IDs, missing dependencies,
   cycles, path ancestor overlap, root escape, stale revisions, missing consumer
   acknowledgement, stale reviewer evidence, required reviewers that are
   unavailable, a manifest that omits a known producer/consumer edge, submitted
   required sets that differ from derived sets, and an independent route that
   hides overlap or integration. The omission case uses an approved inventory as
   its independent oracle.
4. Implement the contract validator and require its evidence before covered
   dispatches.
5. Add failing handoff tests that compare base-tree tracked/untracked changes to
   workstream ownership and reject cross-workstream writes.
6. Add failing installer tests using temporary directories for correct links,
   idempotency, and conflict preservation.
7. Implement the installer and run only its dry-run against the real Claude
   adapter target; actual installation remains separately approval-gated.
8. Add structured live-eval scenario fixtures and runner isolation tests without
   making a model call, including exact-checkout discovery, hash mismatch,
   blocked OAuth-only auth, API-key shell non-inheritance, unavailable tool
   isolation, budget exhaustion, and fail-closed streaming redaction.
9. Run the complete repository validator, skill validators, plugin validator,
   adapter parsers, symlink integrity checks, and `git diff --check`.

Once implemented, the public release validator treats the policy checker,
coordination CLI, canonical JSON fixtures, negative tests, live-eval runner, and
installer tests as required files and required commands. Missing components fail
release validation rather than producing an optional skip.

Live Codex evaluation is not required to claim deterministic contract checks
pass. If it is not executed, final reporting must state that behavioral model
conformance remains `not_run` or `partial`.

## Rollout

1. Land the deterministic contract format and tests without changing ordinary
   A0/A1 routing.
2. Update shared roles and reviewer mapping.
3. Test the Claude installer in a temporary root and produce a real-target
   dry-run manifest.
4. Ask separately before installing global Claude links.
5. Run targeted live scenarios only after deterministic checks pass.
6. Observe false-positive and runtime cost before considering any broader live
   release gate.

## Acceptance Criteria

- A0/A1 and completely disjoint concurrent workstreams do not produce a full
  parallel contract or invoke live eval.
- Canonical JSON is the only machine input; human-readable YAML cannot be used as
  an independent source of truth.
- Workstream owners, paths, dependencies, inputs, and outputs exist only in the
  manifest; contracts reference its canonical hash and do not duplicate them.
- Independent parallel routing requires manifest completeness verified against
  an approved plan/repository inventory or actual diff; otherwise execution is
  sequential unless uncertainty is explicitly approved.
- Concurrent workstreams with a shared interface or integration dependency are
  derived by the validator and cannot start until the
  current contract core is frozen, the validator issues a current-core-hash
  receipt, and write ownership is unambiguous.
- Overlapping exclusive write paths are blocked in version 1; the paths must be
  split or the writers serialized and the revised manifest revalidated. An
  accepted receipt always records `path_overlap: false`.
- Contract changes invalidate affected handoffs until acknowledgement and
  revalidation are recorded.
- DAG validation rejects duplicate IDs, missing references, cycles, premature
  dependency dispatch, and normalized path overlap or repo-root escape.
- Handoff validation compares actual tracked and untracked changes from the base
  tree with workstream ownership and rejects out-of-scope writes.
- Contract core and execution ledger use separate, explicitly defined hash
  domains; evidence binds to the core and ordered ledger chain.
- The lightweight route uses the same validator and cannot hide a shared
  interface, path overlap, integration dependency, or root escape.
- A missing or incompatible validator produces a recorded blocked state and
  sequential single-owner fallback, never an unvalidated parallel dispatch.
- Deterministic validation detects missing contract fields, invalid reviewer
  mappings, weak sample evidence, and hygiene issues in any tracked text file.
- Live evaluation is opt-in and scenario-selective outside an explicit release.
- Automated live evaluation uses process-local API-key auth only; OAuth-only
  environments are blocked without copying credentials.
- Agent tool subprocesses cannot inherit or observe the live-eval API key; an
  environment that cannot prove this separation is blocked.
- Targeted and release live-eval runs enforce separate total call and wall-time
  budgets, and redaction failure prevents artifact retention.
- Codex and Claude reviewers are read-only and return implementation handoffs.
- Installer tests prove all 16 Claude role entries can resolve to shared adapters
  without overwriting conflicting files; actual global installation remains an
  explicit local approval step and is not a public release criterion.

### Coordination CLI v1 Acceptance Boundary

Coordination CLI v1 ends at `validate-handoff`.

In v1, `integration_gate.status` is open-only; caller-submitted `closed` is rejected.

`close-integration` and its closure receipt are a future v2 milestone and a v1 non-goal.

Until v2 exists, do not claim integration status `verified` or `closed`.

## Future Milestone: Closure v2

Closure v2 is a separate additive API and schema milestone. It does not change
the v1 dispatch or handoff acceptance boundary.

A closure receipt binds `contract_core_hash`, `ledger_hash`, derived required
sets, and the checkout tree hash. Neither evidence nor the closure receipt is
included in the core hash domain.

| Profile and transition | Actor | Prerequisites |
|---|---|---|
| integration gate open → closed | integration owner through validator | all derived current-contract-hash handoffs, acknowledgements, checkpoints, and reviewers complete; validator issues a closure receipt |

Closure v2 acceptance requires:

- `close-integration` emits the integration-closure receipt rather than trusting
  a user-editable contract field.
- The integration gate accepts only current-core-hash handoffs,
  acknowledgements, checkpoints, and required reviewer evidence.
- Reviewer completion from an earlier revision is stale and cannot satisfy the
  current integration gate.
- The integration owner cannot freeze or modify the contract and may close the
  integration gate only through the validator.
