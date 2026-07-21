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
rules remain under `/Users/jeongsik/.agents/` and must not become a runtime
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
| Two or more independent workstreams | Required | Contract schema and handoff checks | Not used by default |
| Public API, auth, database, or shared integration contract | Required when multiple consumers or implementers are involved | Contract and integration checks | Targeted when agent/workflow behavior changed |
| Shared agent or workflow instruction change | Required only when the change itself creates parallel work | Full deterministic policy checks | Targeted 3-5 scenarios before release |
| Workflow release candidate | As applicable | Full repository checks | Selected suite or all 26 scenarios by explicit release decision |

Live evaluation is separated from the default validation command because model
calls add latency, cost, and nondeterminism. A live failure records its command,
model/runtime metadata, raw artifact path, and whether the failure is an
invocation error, contract failure, or inconclusive model variance.

## Parallel Work Contract

The canonical contract is documented in the workflow plugin under the intake
references. Shared global roles repeat only the minimum fields needed to work
when the plugin is not active.

```yaml
parallel_work_contract:
  version: 1
  revision: 1
  status: draft | frozen | changed
  activation_reason:
  contract_owner:
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
  shared_path_owner:
  integration_owner:
  checkpoints:
    contract_approved:
    independent_checks_passed:
    integrated_flow_verified:
  change_protocol:
    revision_required: true
    consumer_ack_required: true
    revalidation_required: true
```

`status: frozen` is the entry condition for parallel implementation when two
workstreams consume the same interface. Only the designated shared-path owner
may modify shared schemas, generated types, route registries, or shared fixtures.
A contract change increments `revision`, moves status to `changed`, and blocks
integration until affected consumers acknowledge and rerun their checks.

The contract can remain an in-session structured block. A durable SPEC or TASK
file is created only when the repository requires it, the user requests it, or
the existing artifact-level rules recommend and authorize it.

## Role Responsibilities

- PM activates the contract only for independent workstreams, assigns owners,
  records dependencies and write paths, and names the integration owner.
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
It records `dispatched`, `completed`, `failed`, `not_available`, or `skipped`, so
`reviewers_run` cannot mean an unverified self-declaration.

## Validation Architecture

### Deterministic Checks

A dependency-free Python checker validates repository-controlled contracts:

- required workflow files and exact enum vocabularies;
- `parallel_work_contract` required keys and version;
- canonical reviewer lens-to-agent mappings;
- acceptance scenario identifiers and machine-readable expectations;
- sample outputs against evidence rules, including unsupported HIGH findings;
- tracked-file public hygiene rather than a hand-maintained directory list.

The existing `scripts/validate_repo.sh` invokes this checker. These checks must
run without network access or a model call and should remain fast enough for each
workflow repository change.

### Live Evaluation

An explicit runner accepts scenario IDs or a release-suite flag. It launches a
fresh Codex process, stores prompt and response artifacts outside tracked source
by default, and applies deterministic assertions to the structured response.

Default behavior:

- no live evaluation for application code changes;
- 3-5 affected scenarios for workflow or shared-agent behavior changes;
- all scenarios only for an explicit release-candidate decision;
- live invocation failures are reported separately from behavioral failures;
- no silent retry that converts a failure into PASS.

The first implementation may provide the runner and scenario selection contract
without forcing model calls during local validation. A live execution requires
an available Codex CLI and the user's existing model access.

## Claude Adapter Alignment

All existing files in `/Users/jeongsik/.agents/adapters/claude/` are activated by
symlinks in `/Users/jeongsik/.claude/agents/`. Installation is idempotent: an
existing correct link is retained, a conflicting file or link is reported and
not overwritten silently.

Reviewer roles are consistently read-only across common rules and adapters.
When a finding needs a change, the reviewer returns a concrete remediation and
handoff target instead of editing. Developer and explicitly designated
implementation roles retain write capability.

## Error Handling

- Missing or ambiguous interface ownership blocks contract freezing.
- Overlapping exclusive write paths block parallel dispatch.
- A changed contract blocks integration until consumer acknowledgement and
  targeted revalidation are recorded.
- A missing reviewer agent becomes `not_available`, not `completed`.
- A live eval tool or model failure becomes `blocked` or `inconclusive`, not PASS.
- A conflicting Claude adapter target is reported for manual disposition rather
  than replaced.

## Testing Strategy

Implementation follows red-green verification:

1. Add failing deterministic tests for contract activation, required fields,
   reviewer mapping, sample evidence consistency, and tracked-file hygiene.
2. Implement the smallest checker and policy changes needed to pass them.
3. Add failing installer tests using temporary directories for correct links,
   idempotency, and conflict preservation.
4. Implement the installer and run it against the real Claude adapter target.
5. Run the complete repository validator, skill validators, plugin validator,
   adapter parsers, symlink integrity checks, and `git diff --check`.

Live Codex evaluation is not required to claim deterministic contract checks
pass. If it is not executed, final reporting must state that behavioral model
conformance remains `not_run` or `partial`.

## Rollout

1. Land the deterministic contract format and tests without changing ordinary
   A0/A1 routing.
2. Update shared roles and reviewer mapping.
3. Install Claude links with the conflict-safe installer.
4. Run targeted live scenarios only after deterministic checks pass.
5. Observe false-positive and runtime cost before considering any broader live
   release gate.

## Acceptance Criteria

- Ordinary A0/A1 work does not produce a parallel contract or live eval.
- Two independent workstreams sharing an interface cannot start until revision 1
  is frozen, owners are assigned, and write paths do not overlap.
- Contract changes invalidate affected handoffs until acknowledgement and
  revalidation are recorded.
- Deterministic validation detects missing contract fields, invalid reviewer
  mappings, weak sample evidence, and hygiene issues in any tracked text file.
- Live evaluation is opt-in and scenario-selective outside an explicit release.
- Codex and Claude reviewers are read-only and return implementation handoffs.
- All 16 Claude global role entries resolve to the shared Claude adapters without
  overwriting conflicting user-owned files.

