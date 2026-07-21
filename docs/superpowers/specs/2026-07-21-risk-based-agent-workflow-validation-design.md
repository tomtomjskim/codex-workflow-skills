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

## Risk-Based Activation

The workflow must select the smallest route that can catch the relevant failure.

| Change class | Parallel contract | Deterministic checks | Live Codex eval |
|---|---|---|---|
| A0/A1 copy, docs, isolated styling, single-file maintenance | Not used | Existing smallest relevant check | Not used |
| A2 multi-file, single workstream | Optional lightweight task/test plan | Relevant repository tests | Not used |
| Concurrent workstreams with a shared interface, overlapping write surface, or explicit integration dependency | Required | Contract schema and handoff checks | Not used by default |
| Concurrent workstreams with no shared interface, write surface, or integration dependency | Not used; record a lightweight coordination registry | Ownership and path-disjointness checks | Not used |
| Public API, auth, database, or shared integration contract | Required when multiple consumers or implementers are involved | Contract and integration checks | Targeted when agent/workflow behavior changed |
| Shared agent or workflow instruction change | Required only when the change itself creates parallel work | Full deterministic policy checks | Targeted 3-5 scenarios before release |
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

Every concurrent route starts with a versioned coordination envelope. Completely
disjoint workstreams use only the lightweight registry; covered workstreams add
the full contract.

```yaml
parallel_coordination:
  version: 1
  activation_decision:
    concurrent_execution:
    shared_interface:
    overlapping_write_surface:
    integration_dependency:
    selected_route: independent | contracted
    reason:
  lightweight_registry:
    - id:
      owner:
      scope:
      exclusive_write_paths: []
```

```yaml
parallel_work_contract:
  version: 1
  revision: 1
  status: draft | frozen | changed
  activation_decision:
    concurrent_execution:
    shared_interface:
    overlapping_write_surface:
    integration_dependency:
    reason:
  contract_owner:
  affected_consumers: []
  interface:
    kind: api | schema | event | file | component | other
    source:
    compatibility:
    errors:
    auth:
    examples:
  workstreams:
    - id:
      owner:
      depends_on: []
      exclusive_write_paths: []
      shared_paths: []
      consumes: []
      produces: []
      ready_when: []
      validation: []
      handoff_to: []
  shared_path_ownership:
    - path:
      owner:
  integration_owner:
  approvals:
    - actor:
      role:
      revision:
      status: approved | rejected | pending
  handoffs:
    - from:
      to:
      contract_revision:
      artifacts: []
      validation_evidence: []
      status: pending | ready | accepted | stale
  consumer_acknowledgements:
    - consumer:
      revision:
      status: pending | accepted | rejected | stale
  checkpoints:
    contract_approved:
      revision:
      status: pending | passed | failed | stale
      evidence:
      decided_by:
    independent_checks_passed:
      revision:
      status: pending | passed | failed | stale
      evidence:
      decided_by:
    integrated_flow_verified:
      revision:
      status: pending | passed | failed | stale
      evidence:
      decided_by:
  integration_gate:
    revision:
    status: open | closed | blocked | stale
    required_handoffs: []
    required_acknowledgements: []
    required_reviewers: []
    evidence: []
    decided_by:
  change_protocol:
    revision_required: true
    consumer_ack_required: true
    revalidation_required: true
```

`status: frozen` is the entry condition for parallel implementation when
concurrent workstreams share an interface, an overlapping write surface, or an
explicit integration dependency. Completely disjoint workstreams use a
lightweight registry containing only task ID, owner, scope, and exclusive write
paths. Only the designated owner for a shared path may modify shared schemas,
generated types, route registries, or shared fixtures.

A contract change increments `revision`, moves status to `changed`, and marks
all earlier handoffs, acknowledgements, and checkpoints `stale`. Integration can
close only when every required record references the current revision.

### State Transitions And Authority

| Transition | Actor | Prerequisites |
|---|---|---|
| `draft → frozen` | contract owner | Architect definition complete; required API/security review complete; every affected workstream owner approved the current revision; contract validator passed |
| `frozen → changed` | contract owner or approved change requester | change reason and affected consumers recorded; revision incremented; prior evidence marked stale |
| `changed → frozen` | contract owner | affected consumers acknowledged the current revision; required checks and reviews rerun; validator passed |
| integration gate open → closed | integration owner | all current-revision handoffs, acknowledgements, checkpoints, and required reviewers complete |

The contract owner controls contract state. The integration owner cannot freeze
or modify the contract and may only close the integration gate.

The schema is conditionally required to avoid ceremony for narrow conflicts:

- Every concurrent route requires only the coordination envelope, registry,
  unique owners, and normalized exclusive write paths.
- `shared_interface: true` additionally requires `interface`, affected consumer
  approvals, acknowledgements, compatibility, errors, auth when applicable, and
  consumer-facing examples.
- `overlapping_write_surface: true` additionally requires a single owner per
  shared path, an explicit path split or serialization decision, and a handoff.
  It does not require unrelated interface, auth, or consumer fields.
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
  write paths, records the contract revision consumed, and returns produced
  artifacts plus validation evidence.
- QA Engineer owns reproduction, validation planning, execution, and evidence.
- Test Coverage Reviewer independently audits whether assertions would catch the
  recorded failure modes. It does not implement fixes.
- Other reviewers remain read-only and hand accepted changes to Developer or the
  applicable implementation role.

The adversarial review matrix maps each reviewer lens to a canonical agent name.
Reviewer execution uses this registry:

```yaml
reviewer_registry:
  - lens:
    canonical_agent:
    required:
    contract_revision:
    status: dispatched | completed | failed | not_available | skipped | stale
    dispatch_evidence:
    completion_evidence:
    defer_owner:
    defer_reason:
```

A required reviewer in `failed` or `not_available` blocks integration unless the
user explicitly approves a defer with an owner, reason, and residual risk.
`reviewers_run` is derived only from completed registry entries for the current
contract revision. A revision change marks earlier reviewer entries `stale`.

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

A separate versioned coordination validator accepts the actual in-session
coordination envelope plus its lightweight registry and optional full contract
through stdin or a file. Before every concurrent dispatch, PM must record the
activation decision and run the validator. The independent route validates the
registry and proves path disjointness; the contracted route additionally
validates the conditionally required contract sections. Neither route may
dispatch without exit code 0 plus evidence containing schema version, selected
route, contract revision when applicable, and normalized path overlap results.
The validator rejects:

- missing owners or duplicate workstream IDs;
- dependencies that reference missing workstreams or form a cycle;
- dependency dispatch before a current-revision handoff is ready;
- exact and ancestor/descendant path overlap across exclusive write sets;
- absolute paths, `..` traversal, or symlink resolution outside the approved root;
- shared paths without exactly one owner;
- stale revision handoffs, acknowledgements, checkpoints, or reviewer evidence;
- an `independent` route when any shared interface, normalized path overlap, or
  integration dependency is present.

This is an executable workflow gate, not a claim that the runtime makes policy
bypass technically impossible. A future collaboration-tool hook can strengthen
enforcement only after hook support and failure behavior are verified.

### Live Evaluation

An explicit runner accepts scenario IDs or a release-suite flag. It launches a
fresh Codex process with an isolated temporary `CODEX_HOME`, read-only sandbox,
non-interactive approvals, and network, MCP, plugins, and hooks disabled. It
stores prompt and response artifacts in a mode-`0700`, untracked temporary
directory and applies deterministic assertions to the structured response.

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
an available Codex CLI and the user's existing model access.

Before any model call, the runner installs only the workflow skills from the
exact checkout under test into the isolated home using supported local links. A
fail-closed preflight resolves each loaded skill source and verifies the expected
skill names, repository commit or tree hash, skill content hashes, and plugin
manifest hash. A missing skill, unexpected additional copy, or hash mismatch
blocks the run. The manifest records the resolved checkout and hashes so results
cannot be attributed to another installed copy.

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

## Testing Strategy

Implementation follows red-green verification:

1. Add failing deterministic tests for contract activation, required fields,
   reviewer mapping, sample evidence consistency, and tracked-file hygiene.
2. Implement the smallest checker and policy changes needed to pass them.
3. Add failing contract-validator tests for duplicate IDs, missing dependencies,
   cycles, path ancestor overlap, root escape, stale revisions, missing consumer
   acknowledgement, stale reviewer evidence, required reviewers that are
   unavailable, and an independent route that hides overlap or integration.
4. Implement the contract validator and require its evidence before covered
   dispatches.
5. Add failing installer tests using temporary directories for correct links,
   idempotency, and conflict preservation.
6. Implement the installer and run only its dry-run against the real Claude
   adapter target; actual installation remains separately approval-gated.
7. Add structured live-eval scenario fixtures and runner isolation tests without
   making a model call, including exact-checkout discovery and hash mismatch.
8. Run the complete repository validator, skill validators, plugin validator,
   adapter parsers, symlink integrity checks, and `git diff --check`.

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
- Concurrent workstreams with a shared interface, overlapping write surface, or
  integration dependency cannot start until the current revision is frozen,
  owners are assigned, the contract validator passes, and write ownership is
  unambiguous.
- Contract changes invalidate affected handoffs until acknowledgement and
  revalidation are recorded.
- The integration gate accepts only current-revision handoffs,
  acknowledgements, checkpoints, and required reviewer evidence.
- Reviewer completion from an earlier revision is stale and cannot satisfy the
  current integration gate.
- DAG validation rejects duplicate IDs, missing references, cycles, premature
  dependency dispatch, and normalized path overlap or repo-root escape.
- The lightweight route uses the same validator and cannot hide a shared
  interface, path overlap, integration dependency, or root escape.
- Deterministic validation detects missing contract fields, invalid reviewer
  mappings, weak sample evidence, and hygiene issues in any tracked text file.
- Live evaluation is opt-in and scenario-selective outside an explicit release.
- Codex and Claude reviewers are read-only and return implementation handoffs.
- Installer tests prove all 16 Claude role entries can resolve to shared adapters
  without overwriting conflicting files; actual global installation remains an
  explicit local approval step and is not a public release criterion.
