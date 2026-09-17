# Host Agent Hardening and Profile Evaluation Plan

Status: repository tooling implemented; host configuration application requires a separate approval.

## Goal

Reduce the default blast radius of Codex, MCP, Serena, and reviewer agents while preserving an
explicit path for implementation and operator work. Detect later host drift with a read-only,
machine-readable audit instead of relying on historical notes.

## Approved Repository Scope

- Add a versioned host-policy manifest.
- Add a read-only validator for Codex config, execution rules, Serena config, Serena projects,
  shared reviewer contracts, and active adapter links.
- Add a manifest-v2 staged applier with exact stage membership, selected-stage preflight, atomic
  file replacement, and one durable partial-state journal.
- Add deterministic fixture coverage on the repository's Python 3.9 baseline.
- Define the exact host migration, rollback, and two-week profile evaluation procedure.

This phase does not change user-level configuration, execute database statements, activate an
untrusted Serena project, stop processes, install dependencies, deploy, push, or publish.

## Target Host Policy

### Codex base layer

- Use `workspace-write` as the default sandbox.
- Pin Codex CLI `0.147.0`; a version change requires policy review before migration evidence is
  accepted.
- Keep full-access warnings visible.
- Trust owned repositories individually; do not trust the home directory, general development
  root, Documents root, or Downloads root. Missing trusted paths are migration errors rather than
  latent trust grants.
- Use `approval_policy = "on-request"`; reject `never` in the reviewed base and profiles.
- Require an exact MCP server inventory and pin each stdio server's absolute command and ordered
  arguments. Do not treat the host application's generic Node binary hash as proof of DB behavior.
- Keep the DB MCP disabled in the base, Scout, Builder, and Operator layers. A later policy revision
  may enable it only after read-only credentials and server-side negative DML/DDL checks prove the
  effective boundary. Prompt annotations alone are insufficient.
- Expose only semantic read and diagnostic operations from Serena. Exclude onboarding, dashboard
  launch, and bulk file replacement from the default MCP allowlist.
- Disable `node_repl` in the base layer. A separately selected operator profile may enable it.
- Use explicit MCP startup, tool timeout, server approval, and per-tool approval settings.

### Execution rules

- Use a default-deny inventory for unconditional `allow` rules. The initial reviewed inventory is
  empty, so wrappers such as `env`, versioned interpreters, and destructive Git subcommands cannot
  bypass a command-name denylist.
- Do not replace them with another raw command prefix. If repeated cleanup later proves necessary,
  use a reviewed, argument-free wrapper that validates one exact target internally.
- Verify both the intended command and the same command with an extra trailing operand through
  `codex execpolicy check`.

### Serena

- Require the trust list to equal `[]`; aliases such as `/**` and other broad patterns fail.
- Add `replace_in_files` to global exclusions.
- Require the default mode list to equal `['interactive']`.
- Disable the per-process dashboard, raise logging to warning, and reduce the default answer limit
  to 50,000 characters.
- Pin Serena runtime `1.6.1`, the registered project inventory, and project schema together. Project
  files use a non-empty `languages` list instead of `language_servers`.
- Keep portable project identifiers in the public policy and bind them to private absolute paths
  with `--serena-project ID=PATH` during the host audit.
- Keep reviewed projects read-only and leave `activation_command` empty.

### Reviewer agents

- Preserve the common role as the source of truth.
- Replace adapter wording that says project-local rules override the common adapter with wording
  that permits scope refinement but cannot relax the immutable read-only boundary.
- Protect every role used by the canonical reviewer routing registry, including `architect`, `dba`,
  and `qa-engineer`.
- Use one exact adapter instruction template rather than a permission-word denylist.
- Set every protected Codex reviewer adapter to `sandbox_mode = "read-only"` and explicitly disable
  DB, Node REPL, and computer-use MCP servers.
- Keep `Bash`, `Edit`, and `Write` disallowed in protected Claude reviewer adapters.
- Reject project-local `.codex/agents/` or `.claude/agents/` files that shadow protected roles.
- Treat the Claude active-agent directory as optional until use of global Claude roles is confirmed.

## Migration Model

Manifest schema v3 is the sole migration authority. It contains the detailed successful proposed-
target audit attestation, every target's pre/target state, its canonical target-inventory digest,
and an ordered exact partition of target labels. Confirmed migration requires exactly
`baseline`, `builder`, `operator`; the baseline owns every non-elevated target, while Builder and
Operator each own only their matching profile. Codex base/rules/all three profiles, Serena global
config, and every policy-listed Serena project are mandatory. A separate schema-v1 approval envelope binds one
authorized stage, its exact labels, external target-audit and snapshot evidence, private executor
bundle, policy/routing, and the post-apply audit/runtime/rollback argv. It projects the target
receipt's outer status into the manifest attestation, binds the receipt's Codex/Serena versions and
complete live input inventory to the audit argv, and lists one runtime probe for the base plus every
profile installed through that stage. The envelope SHA-256, not a
mutable path, is the operator approval ID. The normal operator path is `audit`, `prepare`, `dry-run`,
`envelope build/verify`, and copied-executor `apply --stage`; rollback remains journal-derived.

Earlier schema-v2 proposals and `validate_host_policy.py@2` receipts are intentionally rejected;
they predate target-root/input binding. No v2 host migration was applied, so proposals must be
regenerated and approved under v3 rather than upgraded in place.

### Prepare gate

1. Build a complete proposed target tree and a synthetic direct-link active-agent inventory. Compute
   the manifest target digest with `host_migration_snapshot.py target-digest`, then run the validator
   against those inputs with `--audit-scope target --stage full`, `--target-manifest`, and
   `--target-root`. The validator computes the target digest and requires every manifest artifact
   to match a consumed audit input. Copy its validator ID, scope, stage, policy/input digests, target
   root, detailed input receipts, target inventory digest, and `status: ok` into the approved manifest. This is distinct from live stage
   receipts; auditing the pre-migration live host cannot authorize proposed target bytes.
2. Run `host_migration_snapshot.py prepare --manifest ... --output-dir ...`. It derives all present
   and absent targets from the manifest and refuses symlinks, duplicate inventory, manifest
   checksum/mode/absent-state drift, or an existing output directory before publishing a receipt.
3. Confirm the snapshot directory is mode 0700, `receipt.json` is mode 0600, and `verify --scope
   both` succeeds. Store it privately because the receipt contains host paths.
4. Run `host_migration_apply.py --stage baseline --dry-run`. The dry run binds the manifest,
   snapshot, selected stage, target allowlist, attested inventory digest, and staged target bytes
   before any approved write.
5. Write the exact baseline audit, base `codex mcp list --json`, installed-profile
   `codex --profile NAME mcp list --json`, and rollback argv into a private envelope
   build spec. Run `host_migration_envelope.py build`, then verify its SHA from the approval channel.
   The copied executor root and envelope must be new paths under a current-user-owned mode-0700
   directory. Verification rechecks all referenced bytes and their manifest/live-audit bindings. A later stage gets a distinct
   envelope and approval SHA; the baseline envelope cannot authorize Builder or Operator.
   The envelope also binds canonical Python/Codex executable paths and byte digests; execution
   rechecks them before and after the live probes.

### Stage `baseline`

Apply execution rules, Codex base and Scout restrictions, Serena global/project restrictions, and
protected reviewer contracts together in one maintenance window. Then start fresh base/Scout and
Serena sessions and run the reviewer read/write-denial checks. Run the host validator with `--stage
baseline` and only the Scout `--codex-profile`; require a JSON receipt with `status: ok` and
`audit.stage: baseline`.

The copied apply executor performs this sequence as one fail-closed operation. Before the first
write it checks the approved envelope/manifest hashes, external target audit, executor bundle,
protected-reviewer DB invariant, all baseline targets, and the future Builder/Operator pre-state.
After the files are installed, the journal is durably `awaiting_stage_audit` and rollback-only. The
envelope-bound live audit and fresh base/Scout Codex MCP inventories must pass, and the
installed/future live state must still match, before the journal becomes `ready`. Runtime probes
use an allowlisted environment with the approved host `HOME`, `CODEX_HOME`, and private `TMPDIR`,
bounded output/time, process-group cleanup, and non-reflected raw stderr. Audit or runtime failure
triggers automatic rollback only for the same journaled attempt ID with durable mutation evidence; a later call that fails before
mutation leaves an already accepted stage unchanged. Process termination leaves the journal rollback-only.
Successful live-audit and fresh MCP outputs are canonicalized into mode-0600 receipt files, bound
to the journal by SHA-256, and reverified before any later stage.

Rollback trigger: parsing or startup failure, an unconditional allow rule, a forbidden Serena tool,
reviewer write authority, or unexpected MCP inventory.

### Stage `builder`

Apply the Builder profile with DB, Node REPL, and computer-use disabled. Confirm implementation and
tests work with only workspace-write plus the reviewed Serena reads.

Rollback trigger: required repository work cannot run or an unreviewed high-risk MCP is visible.

### Stage `operator`

Apply the explicitly selected Operator profile with DB still disabled. Node REPL may be enabled only
with its reviewed identity and approval boundary. Enabling DB requires a later policy revision after
server-side read-only credentials and negative DML/DDL evidence exist.

Rollback trigger: DB is enabled, an additional MCP is visible, or Node REPL identity/approval differs.

## Rollback

Rollback is file-for-file restoration from the prepared snapshot followed by a fresh process start.
Do not merge old and new configuration fragments. `rollback` derives the approved manifest and
snapshot from the single migration journal and is blocked without `--confirm-rollback`. It restores
only labels recorded as completed plus the active uncertain label, so an unapplied later-stage file
is not removed. Selected targets must match either the approved installed state or the original
pre-state; other bytes are preserved and rollback stops. Before a destructive namespace action,
the selected path is atomically moved to a unique same-directory quarantine and the moved inode is
verified; a concurrently created path is never overwritten or automatically deleted. The exact
retained quarantine path remains in the journal and requires separate identity, mode, and checksum
review before manual cleanup. The journal records durable
`rolling_back` per-label progress before the first mutation, so retries are idempotent. It
preflights the entire selected restore scope before the first write, fsyncs each changed parent
directory, and terminalizes the journal as `rolled_back`.

After restoration:

1. Run `host_migration_snapshot.py verify --scope backup` for the receipt recorded in the journal.
2. Run the copied, journal-bound executor's `host_migration_snapshot.py rollback --journal ...
   --confirm-rollback` (not the repository source script). It verifies the
   journal-bound approval-envelope path and digest plus copied core executor digests before imports,
   then verifies the manifest and receipt digests, performs same-directory atomic replacement, restores
   modes and source hashes, and moves migration-created files whose receipt state was `absent` out
   of the live namespace into a retained same-directory quarantine.
3. Compare restored checksums with the recorded pre-change checksums.
4. Run Codex config parsing and Serena project parsing.
5. Start a new session; an existing session may retain the earlier MCP inventory.
6. Record why rollback occurred and which acceptance assertion failed.
7. Run the live host-policy audit for the restored stage and record policy status separately from
   byte restoration status. A byte-perfect rollback can restore a pre-policy state and must not be
   reported as policy-safe without this audit and a fresh-session MCP inventory check.

If an apply journal reports `applying`, `partial`, or `awaiting_stage_audit`, do not continue the
stage. Treat `active_label` as uncertain where present, roll back the journal-recorded completed and
active scope, and only retry after the original manifest pre-state is recreated, re-snapshotted,
and approved under a new envelope SHA.

The snapshot contract preserves content and Unix mode. ACLs and extended attributes are outside the
current contract and must be explicitly accepted or added to the migration format before applying
to targets where their preservation is required.

## Validation Commands

Repository fixtures:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_host_policy_contracts \
  tests.test_host_migration_apply \
  tests.test_effective_agent_contracts \
  tests.test_shared_role_contracts -v
PYTHONDONTWRITEBYTECODE=1 ./scripts/validate_repo.sh
```

Manifest-bound stage preflight:

```bash
/usr/bin/python3 -I scripts/host_migration_apply.py \
  --manifest /private/approved/proposal/manifest.json \
  --target-root /private/approved/proposal/target \
  --snapshot-receipt "$snapshot_dir/receipt.json" \
  --stage baseline \
  --dry-run
```

Confirmed apply additionally requires the private copied executor, `--approval-envelope`,
`--expected-envelope-sha256`, `--expected-manifest-sha256`, `--journal-dir`, and
`--confirm-apply`. Use the exact approved interpreter with `-I`; copied sibling-module identity and
the approved Python/Codex executable ownership, ancestry permissions, and digests are fail-closed.
Do not run confirmed apply from the mutable repository script path.

Read-only host audit:

```bash
/usr/bin/python3 -I scripts/validate_host_policy.py \
  --policy policies/host-policy.json \
  --codex-config "$HOME/.codex/config.toml" \
  --codex-version 0.147.0 \
  --codex-profile scout="$HOME/.codex/scout.config.toml" \
  --codex-profile builder="$HOME/.codex/builder.config.toml" \
  --codex-profile operator="$HOME/.codex/operator.config.toml" \
  --codex-rules "$HOME/.codex/rules/default.rules" \
  --serena-config "$HOME/.serena/serena_config.yml" \
  --serena-project project-one=/absolute/path/to/project-one/.serena/project.yml \
  --serena-project project-two=/absolute/path/to/project-two/.serena/project.yml \
  --serena-version 1.6.1 \
  --shared-agents-root "$HOME/.agents" \
  --codex-agents-root "$HOME/.codex/agents" \
  --claude-agents-root "$HOME/.claude/agents" \
  --reviewer-routing skills/adversarial-review-loop/references/reviewer-routing.json \
  --home-root "$HOME" \
  --json
```

For the baseline acceptance receipt, add `--stage baseline` and pass only the Scout profile. For the
Builder receipt, use `--stage builder` with Scout and Builder. The default `full` stage continues to
require all three profiles.

The host audit must return `status: ok`. Warnings about an optional Claude installation or an
expensive default model are not policy failures; they remain explicit evaluation inputs.

## Acceptance Criteria

- No unconditional allow rule exists unless its full argument vector exactly matches the reviewed
  positive inventory.
- Base Codex runs with `workspace-write` and `on-request`; no broad parent path or symlink alias is
  trusted.
- MCP names and stdio identities exactly match policy. DB is disabled in every profile until a
  separately approved policy revision has server-side read-only evidence. Serena has an explicit
  semantic-read allowlist.
- Node REPL is disabled in the base layer.
- Serena `1.6.1` parses and activates the exact reviewed project inventory with a tool inventory that
  excludes bulk replacement and onboarding.
- Every routed reviewer is protected by the exact adapter template; every Codex reviewer runs
  read-only without DB, Node REPL, or computer-use access; no project-local shadow exists.
- Fixture tests and the full repository validation pass.
- A fresh-session smoke check confirms effective runtime behavior.

## Two-Week Profile Evaluation

Profile activation is a later, separately approved host change. Use the following candidates after
the P0 policy has passed:

| Profile | Default model and effort | Permission boundary | Intended work |
|---|---|---|---|
| Scout | `gpt-5.6-terra`, medium | read-only; Serena semantic reads; DB disabled | exploration and routine review |
| Builder | `gpt-5.6-terra`, high | workspace-write; DB disabled | implementation and tests |
| Operator | `gpt-5.6-sol`, high or xhigh | explicitly selected; DB disabled | browser, release, and unusual recovery work |

Tag each sampled task by profile and task class. Record:

- completion without escalation;
- validation result and user-visible rework;
- elapsed time to the first useful artifact and final result;
- input/output tokens or the closest available usage measure;
- MCP startup failures and tool timeouts;
- unique material reviewer findings and rejected false positives.

Pre-register the decision rule before starting:

- Start with a six-task pilot: three Scout and three Builder tasks, alternated within the same task
  class and complexity band. Expand to the pre-registered 20-task comparison only if the pilot is
  feasible and the result is close enough that more evidence could change the decision. Use Builder
  as the quality reference and blind final finding adjudication where practical.
- Define a material defect as a missed `[HIGH]`/`[MED]` finding, an incorrect completion claim, or
  user-visible rework that changes the technical result. Do not count style-only edits.
- Promote Scout for eligible read-only work only when it has zero missed `[HIGH]` findings, its
  material-defect and rework rates are no more than 10 percentage points above Builder, and median
  tokens or elapsed time improve by at least 20% without increasing MCP startup failures above one
  per ten tasks.
- Treat Operator as authority-gated, not a quality promotion. Record at least five genuinely
  authority-requiring tasks and require zero uses on tasks whose capabilities fit Builder.
- Exclude only cancelled tasks, unavailable external systems, or changed acceptance criteria; record
  every exclusion. A full comparison requires 10 tasks per profile. If that sample or a decision
  threshold is not met after two weeks, extend the observation window and report `inconclusive`.

No fixed cost reduction is assumed before measurement.

Official configuration basis:

- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Codex execution rules](https://learn.chatgpt.com/docs/agent-configuration/rules)
- [Serena configuration](https://oraios.github.io/serena/02-usage/050_configuration.html)
