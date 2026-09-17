# Codex Workflow Skills

Reusable Codex skills for structured task intake, bounded multi-agent councils, evidence-based
adversarial review, verified session knowledge closeout, and multi-perspective resume review.

This repository is plugin-ready. It contains:

- `workflow`: route-only wrapper for choosing intake or review when explicitly requested.
- `workflow-intake`: turns ambiguous or multi-step requests into a bounded session policy, then maintains lightweight plan state, side-effect checks, validation planning, E2E decisions, and AI eval handoffs after intake activates.
- `council`: runs a bounded, independent, read-only reviewer panel to challenge, brainstorm, compare, decide, or refine one proposal while the main agent remains the sole writer.
- `session-wiki`: closes a completed session by extracting and verifying durable knowledge, updating
  established project documentation, or capturing sanitized personal knowledge without automatic
  promotion.
- `adversarial-review-loop`: reviews plans, diffs, and implementations with evidence, reviewer routing, finding disposition, loop limits, and verification gates.
- `resume-multi-review`: evaluates a concrete resume through independent recruiter, hiring-manager, and future-teammate lenses, reconciles conflicting decisions, rewrites supported claims, and controls repeat review loops.
- `scripts/workflow`: prepares canonical coordination artifacts and validates concurrent dispatches and handoffs without third-party Python packages.

## Why This Exists

Most agent failures in larger tasks are not raw coding mistakes. They are scope drift, skipped context, vague autonomy, weak review evidence, stale source selection, or over-trusting external text. These skills make those boundaries explicit before work starts and before review is called complete.

## Quick Start

```bash
git clone https://github.com/tomtomjskim/codex-workflow-skills.git
cd codex-workflow-skills
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/workflow" ~/.codex/skills/workflow
ln -s "$PWD/skills/workflow-intake" ~/.codex/skills/workflow-intake
ln -s "$PWD/skills/council" ~/.codex/skills/council
ln -s "$PWD/skills/session-wiki" ~/.codex/skills/session-wiki
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
ln -s "$PWD/skills/resume-multi-review" ~/.codex/skills/resume-multi-review
python3 -m pip install --disable-pip-version-check -r requirements-ci.txt
./scripts/validate_repo.sh
```

Then start a new Codex session and try:

```text
Use $workflow-intake to scope a multi-step settings workflow before implementation.
```

```text
Use $council default to refine the current proposal through independent reviewer lenses.
```

```text
Use $session-wiki default to close the current session into verified project knowledge.
```

```text
Use $resume-multi-review to evaluate my latest resume against this job description and run one evidence-safe rewrite cycle.
```

For plugin distribution, keep `.codex-plugin/plugin.json` and add this repository through your Codex plugin source or marketplace flow.

## Which Skill Should I Use?

- Use `$workflow-intake` for broad, risky, ambiguous, or multi-step tasks that need scoped autonomy, artifact decisions, validation planning, and approval gates.
- Use `$workflow-intake` when PRD, SPEC, TASK, TEST_PLAN, design docs, E2E, or AI eval decisions should be made before implementation.
- Use `$council` when one bounded proposal, plan, design, decision, diff, or recommendation benefits from independent challenge, alternatives, and a synthesized redefinition.
- Use `$session-wiki` when completed work produced stable project knowledge, source mappings, or
  reusable personal learning that should be verified and routed at session closeout.
- Use `$adversarial-review-loop` when a plan, diff, PR, or implementation already exists and needs evidence-based findings, reviewer lenses, disposition, re-checks, and residual-risk closure.
- Use `$resume-multi-review` when a concrete resume or authoritative resume source must be screened, revised, and re-screened through distinct hiring perspectives.
- Use `$workflow` when you are unsure whether the task should start with intake or review.

## Recommended Development Workflow

1. Start with `$workflow-intake` when task scope, autonomy, affected files, artifacts, or validation level are unclear.
2. Let intake identify required project context such as `AGENTS.md`, README, project maps, wiki indexes, Serena state, diffs, tests, and task-specific docs.
3. Approve or revise the generated artifact plan before durable PRD, SPEC, TASK, TEST_PLAN, UX_CONCEPT, IA, UI_SPEC, or EVAL_PLAN documents are created.
4. Implement using the repository's own conventions and validation commands.
5. Use `$council default` when the plan or next-step recommendation needs independent challenge and a refined replacement.
6. Run `$adversarial-review-loop` against the plan, diff, or implementation before treating the work as ready.
7. Resolve accepted findings, rerun the relevant checks, and record residual risk when anything remains unverified.
8. Use `$session-wiki default` after implementation and review are complete to update only stable,
   source-supported project knowledge. Choose `personal capture` or `full closeout` explicitly when
   an approved personal-wiki target should receive an inbox note.

## Resume Review Workflow

1. Provide the exact submitted resume, reviewed master resume, or a repository path that identifies it.
2. Provide the target company and job description when company-specific evaluation is required.
3. Let `$resume-multi-review` classify source authority, review status, privacy level, and claim strength before scoring.
4. Review recruiter, hiring-manager, and future-teammate decisions independently.
5. Apply only evidence-supported revisions and hold claims marked draft, generated, selective, role-confirm, or needs verification.
6. Re-run the three reviewers and stop when all choose interview, no fatal risk remains, and remaining edits are stylistic.

A public sanitized portfolio copy is not automatically the latest master resume. If the authoritative full resume is unavailable, the skill returns a source-gap report and patch plan rather than fabricating a complete final resume.

## Artifact and Eval Decisions

`workflow-intake` separates planning artifacts from design artifacts. It should ask before creating durable docs unless the user or repository rules already approve them.

Common planning artifacts. These are options, not a required bundle; intake should recommend the smallest useful set for the task.

- PRD for product behavior, user goals, scope, success criteria, and release constraints.
- SPEC for technical design, data flow, APIs, state, errors, migrations, and integration points.
- TASK for implementation slices, dependencies, and ownership boundaries.
- TEST_PLAN for unit, integration, E2E, regression, and manual validation coverage.
- EVAL_PLAN for AI/LLM output quality, acceptance criteria, failure cases, regression evals, and monitoring.

Common design artifacts:

- UX_CONCEPT for interaction model and user intent.
- IA for navigation, content structure, and workflow organization.
- UI_SPEC for screen states, layout rules, responsive behavior, accessibility, and handoff details.

## Repository Layout

```text
.
├── .codex-plugin/plugin.json
├── CHANGELOG.md
├── policies/
│   └── host-policy.json
├── scripts/
│   ├── agent_contracts.py
│   ├── host_migration_apply.py
│   ├── host_migration_envelope.py
│   ├── host_migration_snapshot.py
│   ├── validate_host_policy.py
│   ├── workflow
│   ├── workflow_coordination/
│   ├── validate_policy_contracts.py
│   └── validate_repo.sh
├── skills/
│   ├── workflow/
│   ├── workflow-intake/
│   ├── council/
│   ├── session-wiki/
│   ├── adversarial-review-loop/
│   └── resume-multi-review/
├── docs/
│   ├── design-draft.md
│   ├── forward-test-report.md
│   ├── readme-reference-review.md
│   ├── sample-adversarial-review.md
│   ├── sample-council.md
│   ├── sample-session-wiki.md
│   ├── sample-resume-multi-review.md
│   └── sample-workflow-intake.md
└── tests/
    └── acceptance-scenarios.md
```

## Prerequisites

- Codex with local skill support.
- Git for cloning this repository.
- Python 3 only for optional validation scripts.
- ripgrep (`rg`) for the one-command repository validation script.
- Optional access to Codex system `skill-creator` and `plugin-creator` scripts for structure validation.

## Install Locally

Direct skill folders are useful for local authoring. For reusable distribution, Codex recommends packaging multiple skills as a plugin.

Clone the repository, then symlink or copy individual skill folders into your Codex skills directory:

```bash
git clone https://github.com/tomtomjskim/codex-workflow-skills.git
cd codex-workflow-skills
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/workflow" ~/.codex/skills/workflow
ln -s "$PWD/skills/workflow-intake" ~/.codex/skills/workflow-intake
ln -s "$PWD/skills/council" ~/.codex/skills/council
ln -s "$PWD/skills/session-wiki" ~/.codex/skills/session-wiki
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
ln -s "$PWD/skills/resume-multi-review" ~/.codex/skills/resume-multi-review
```

If a symlink already exists, remove or update that symlink first.

### Shared Agent Adapter Installer

Validate adapter installation against an isolated target before considering a global change. Use the real shared Claude adapter directory only as the source, and use a private temporary directory with a pre-created private target:

```bash
tmp_root="$(mktemp -d)"
chmod 700 "$tmp_root"
mkdir -m 700 "$tmp_root/agents"
python3 scripts/install_agent_adapters.py \
  --source-root "$HOME/.agents/adapters/claude" \
  --target-root "$tmp_root/agents" \
  --suffix .md
```

The temporary result should contain exactly 16 direct symlinks to the expected files under `$HOME/.agents/adapters/claude`; remove the temporary directory after verification.

For the real target, `$HOME/.claude/agents` must already exist as a real, non-symlink directory. If it is absent or a symlink, stop without invoking the installer or creating the directory and request a separate preparation approval. When the precondition passes, run only `--dry-run --json` first. The manifest contains exact local paths and is sensitive; keep it in a mode-0700 temporary directory and report only its hash, create/keep counts, and conflicts. A non-dry-run apply is a separate action requiring explicit approval and must not run in the same approval step.

### Post-install Check

Confirm the skill files are visible:

```bash
test -f ~/.codex/skills/workflow/SKILL.md
test -f ~/.codex/skills/workflow-intake/SKILL.md
test -f ~/.codex/skills/council/SKILL.md
test -f ~/.codex/skills/session-wiki/SKILL.md
test -f ~/.codex/skills/adversarial-review-loop/SKILL.md
test -f ~/.codex/skills/resume-multi-review/SKILL.md
```

If the Codex validation scripts are available, run the checks in the [Validation](#validation) section from the cloned repository.

## Usage

### Phase B0 native readiness

Phase B0 is a host-specific, zero-call observation command for native
filesystem, environment, loopback, and ledger primitives. Run it only with
explicit absolute paths:

```bash
python3 scripts/run_harness_canary_readiness.py \
  --codex-executable /absolute/path/to/codex \
  --python-executable /absolute/path/to/python3 \
  --temp-parent /absolute/path/to/private-temp-parent \
  --private-root /absolute/path/to/private-ledger-root
```

It performs no authentication, model call, or external network request. A
successful result is not live-containment proof and does not authorize a paid
canary. VM fallback remains a separately approved choice. See the approved
[native readiness design](docs/superpowers/specs/2026-07-29-native-canary-readiness-design.md)
and [implementation plan](docs/superpowers/plans/2026-07-29-native-canary-readiness-plan.md).

Start with intake for ambiguous, multi-step, or risky work:

```text
Use $workflow to route this task.
```

```text
Use $workflow-intake to scope this task before implementation.
```

For long-running or user-facing work, intake also records plan revisions, expected side effects, validation level, whether Playwright/E2E is required, recommended, not needed, or blocked, and whether AI/LLM output quality needs an `EVAL_PLAN`.

Run adversarial review when a plan, diff, or implementation exists:

```text
Use $adversarial-review-loop to review this diff and classify findings.
```

Run a Council when a bounded target should be challenged and redefined through independent lenses:

```text
Use $council default to refine the current rollout plan.
```

A bare `$council` call only shows the preset and option legend, then waits. `default` uses
`mode=refine`, `depth=standard`, `focus=auto`, at most one loop, response-only output, and up to two
read-only reviewers when host policy permits. `quick review` uses one reviewer; `deep refinement`
uses two reviewers and at most two loops. Explicit options can set `mode`, `depth`, `focus`, `loops`,
`result`, and `write`. The Council call never overrides repository approval gates, and only the main
agent may write. Reviewer registration and completion use separate clocks: a registered running
reviewer is observed within the preset completion ceiling, while only terminal failures may consume
a bounded fresh replacement attempt. Receipts distinguish reviewer seats, attempts, canonical
runtime targets, and duplicate-rendered status lines. An explicit source revision is preserved
unchanged end to end; Council assigns `v0` only when the source provides no revision.

Close a completed session into durable project knowledge:

```text
Use $session-wiki default for the current session-owned diff.
```

A bare `$session-wiki` call shows its target candidates, presets, and options, then waits without
scanning or writing. `default` verifies session and diff evidence before minimally updating existing
project documentation. `quick candidates` is read-only. `personal capture` writes only to an
explicitly configured personal-wiki inbox, and `full closeout` combines project updates with that
inbox capture. Neither preset authorizes promotion to reviewed or canonical knowledge, unrelated
tests, commits, pushes, or publication.

Run resume multi-review when an actual resume or resume source exists:

```text
Use $resume-multi-review to identify the authoritative resume, evaluate it against this job description, revise supported claims, and re-run the three reviewers once.
```

To receive only a reusable copy-paste prompt:

```text
Use $resume-multi-review and return the standalone prompt template without evaluating a resume.
```

### Validated Coordination CLI

For covered concurrent work, prepare a manifest and inventory together from one approved UTF-8 JSON plan:

```bash
./scripts/workflow prepare-coordination \
  --repo-root /path/to/approved-repo \
  --plan /path/to/approved-plan.json \
  --out-dir /path/to/temporary-coordination \
  --json
```

The output directory must not exist before this command. Preparation builds and synchronizes both files in a private sibling staging directory, then publishes the directory as one unit with the platform's atomic no-replace rename primitive. Existing empty/nonempty directories, files, symlinks, and concurrently created targets are never replaced or removed. If atomic no-replace publish is unavailable, preparation returns a structured blocked error; there is no unsafe fallback, so use sequential execution.

Validate the generated artifacts before every concurrent dispatch. A contracted route also requires the current frozen contract:

```bash
./scripts/workflow validate-coordination \
  --repo-root /path/to/approved-repo \
  --manifest /path/to/temporary-coordination/manifest.json \
  --inventory /path/to/temporary-coordination/inventory.json \
  --contract /path/to/temporary-coordination/contract.json \
  --json
```

Validate each workstream handoff against the current receipt and its derived write ownership:

```bash
./scripts/workflow validate-handoff \
  --repo-root /path/to/approved-repo \
  --manifest /path/to/temporary-coordination/manifest.json \
  --inventory /path/to/temporary-coordination/inventory.json \
  --contract /path/to/temporary-coordination/contract.json \
  --receipt /path/to/temporary-coordination/receipt.json \
  --workstream-id frontend \
  --changed-path src/ui/settings.py \
  --json
```

Coordination CLI v1 ends at `validate-handoff`.

In v1, `integration_gate.status` is open-only; caller-submitted `closed` is rejected.

`close-integration` and its closure receipt are a future v2 milestone and a v1 non-goal.

Until v2 exists, do not claim integration status `verified` or `closed`.

Handoff validation requires the authoritative `--manifest` and `--inventory`, plus `--contract` when the route is contracted. It reruns coordination validation with the authoritative manifest, inventory, contract, and shared reviewer routing artifact before requiring exact canonical equality with the submitted receipt. The shared reviewer routing artifact is authoritative for reviewer derivation. `validate-coordination` issues a receipt only from a clean worktree whose actual `HEAD^{tree}` matches any provided `--checkout-tree-hash`. At handoff, the CLI collects tracked, staged, unstaged, deleted, renamed, and non-ignored untracked paths from NUL-delimited Git status. A supplied `--changed-path` is an additional declaration, not the authority: validation checks the union of Git paths and declarations, so an omitted or partial declaration cannot hide a write. CLI receipts use canonical UUID run IDs and expire after five minutes; rerun `validate-coordination` when a receipt is stale.

The handoff check is a repository-state gate, not runtime write prevention. Standard Git-ignored files are not reported, and a concurrent writer can invalidate attribution after collection. Use one isolated worktree or isolated patch artifact per workstream, stop writers before handoff, and use the documented sequential fallback whenever attribution is uncertain. Symlink entries are reported without traversing their targets; writes through a symlink to an external filesystem location remain outside this Git-state claim.

All three commands emit JSON and return nonzero with a structured error when validation fails. Covered parallel dispatch requires a current CLI version 1 receipt. If the CLI is missing or incompatible, validation fails, the receipt is stale, or changes cannot be attributed to a workstream, use the single-owner sequential fallback and record `parallel_validation: blocked`; do not continue with unvalidated parallel writers.

## Examples

Route a broad task:

```text
Use $workflow to plan a multi-step checkout error-state cleanup before implementation.
```

Expected behavior: route to `workflow-intake`, ask for a missing repo/path if needed, choose an artifact level, set autonomy gates, and create a validation plan before implementation.

Scope an AI feature:

```text
Use $workflow-intake to plan a product-facing AI support assistant that answers refund-policy questions from internal docs.
```

Expected behavior: require PRD when product behavior is being defined, require `EVAL_PLAN` for AI/LLM answer quality, keep `validation_level` to the supported enum, and keep browser E2E decisions separate from AI evaluation.

Plan a UI/product workflow with artifact approval:

```text
Use $workflow-intake to plan a new settings workflow with several screens and a design refresh. I am not sure which docs we need.
```

Expected behavior: emit `artifact_decision` with separate planning and design doc recommendations, propose the smallest useful PRD/SPEC/TASK/TEST_PLAN and UX_CONCEPT/IA/UI_SPEC set for the repo, and ask before creating durable docs unless the user or repo already approved them.

See [sample-workflow-intake.md](docs/sample-workflow-intake.md) for an illustrative `workflow_intake` output.

Review an existing diff:

```text
Use $adversarial-review-loop to review this diff and classify findings with evidence.
```

Expected behavior: select reviewer lenses from changed surfaces, classify findings with evidence, avoid false passes from weak tests, and record residual risk.

See [sample-adversarial-review.md](docs/sample-adversarial-review.md) for an illustrative `adversarial_review` output.

Refine a rollout proposal through a bounded Council:

```text
Use $council default to review and redefine this rollout proposal. Keep writes disabled.
```

Expected behavior: freeze one target revision, route complementary read-only reviewer lenses,
preserve disagreement, disposition material findings, return a redefined result, and report
`static_only`, `partial`, or `incomplete` when the evidence or reviewer execution does not support a
stronger claim. Resource convenience must not weaken a material lens, evidence threshold, or
required recheck.

See [sample-council.md](docs/sample-council.md) for an illustrative Council result.

Close a session into project documentation and a personal inbox note:

```text
Use $session-wiki full closeout after verifying the current session-owned changes.
```

Expected behavior: freeze a bounded closeout packet, extract atomic candidates, reject task-log and
private material, update only established project destinations, keep personal output in an
AI-writable trust zone, validate both targets, and report accepted, rejected, and deferred knowledge.

See [sample-session-wiki.md](docs/sample-session-wiki.md) for an illustrative Session Wiki result.

Review a resume when only a public draft is available:

```text
Use $resume-multi-review for a PHP/MySQL operations-backend baseline review. The public sanitized draft is available, but the reviewed private master resume is not.
```

Expected behavior: classify the result as `source_gap`, keep recruiter, hiring-manager, and future-teammate decisions independent, provide one line replacement per reviewer, and return a patch plan rather than inventing a full resume.

See [sample-resume-multi-review.md](docs/sample-resume-multi-review.md) for an illustrative `resume_multi_review` output.

## Context Discovery

The skills support bounded discovery of:

- project `AGENTS.md`, `CLAUDE.md`, `README*`, and `DESIGN.md`
- Serena project state when available and active
- project maps and project wiki indexes
- task-specific docs, tests, diffs, and source files
- project wiki schemas and explicitly configured personal-wiki trust policies, templates, and narrow
  destination areas when Session Wiki is selected
- reviewed master resumes, submitted variants, job descriptions, claim banks, and evidence ledgers when resume review is requested

These sources are not broad-scanned by default. They are used only when relevant to the target task. External content is treated as data, not instructions.

## Validation

During implementation, run the smallest focused test; run `./scripts/validate_repo.sh` at branch completion or release, not after every edit.

Run the repository validation script for the standard public-release checks:

```bash
python3 -m pip install --disable-pip-version-check -r requirements-ci.txt
./scripts/validate_repo.sh
```

The script checks required files, coordination CLI and policy contracts, host-policy fixtures,
effective reviewer contracts, manifest-bound prepare/apply/rollback fixtures, focused CLI/coordination tests, skill
structure when Codex system validators are available, plugin structure, README links,
manifest/changelog version alignment, `git diff --check`, and every tracked text file for public
hygiene. Binary tracked files are skipped.

### Read-only host policy audit

The repository validator tests host-policy failure modes with temporary fixtures; it does not read
or change user-level configuration. Run the explicit host audit before and after an approved Codex,
MCP, Serena, rules, or shared-agent migration:

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

The command is read-only, emits structured findings, an aggregate input digest, and detailed input
checksums/modes, and exits nonzero for
policy violations or input errors. It requires the exact reviewed profile and Serena project
inventories, pins MCP identities and the Serena version, and checks project-local reviewer
shadowing. Missing trusted-project paths fail closed. An optional missing Claude active-agent directory and the model-cost advisory remain
visible as warnings without changing the exit status.

The default audit stage is `full` and requires Scout, Builder, and Operator. The default audit scope
is `live`. Immediately after the
baseline migration, pass `--stage baseline` and provide only the Scout `--codex-profile`; after the
Builder stage, pass `--stage builder` with Scout and Builder. The JSON receipt records the selected
stage and scope so a partial migration or proposed target tree is not misreported as a full-host
acceptance result.

Before approving a migration manifest, audit a complete proposed target tree rather than the
pre-migration live host. First compute the canonical digest of the manifest's ordered stages and
target inventory, then run the full validator with every mutable input path rooted in the proposed
target tree, a synthetic direct-link active-agent inventory rooted at its proposed adapters, and
these additional arguments:

```bash
/usr/bin/python3 -I scripts/host_migration_snapshot.py target-digest \
  --manifest /private/approved/proposal/manifest.json
/usr/bin/python3 -I scripts/validate_host_policy.py \
  ...full proposed-target arguments... \
  --audit-scope target --stage full \
  --target-manifest /private/approved/proposal/manifest.json \
  --target-root /private/approved/proposal/target --json
```

Copy a successful target receipt's validator ID, scope, stage, policy/input digests, target root,
target inventory digest, and detailed input receipts into the manifest attestation. The validator
sealed-reads the manifest and target artifacts, computes the digest itself, and requires every
manifest target to be consumed by exactly matching policy-audit input bytes and mode. The applier requires
`validate_host_policy.py@3`, `scope: target`, and `stage: full`, recomputes the inventory digest, and
restricts writes to the reviewed Codex, Serena, and shared-agent path families under one home root.
An arbitrary credential or DB configuration path is rejected even when its artifact checksum is
otherwise valid, and every Codex base/profile artifact is parsed again to require
`mcp_servers.db-mcp.enabled = false`. The later `baseline`, `builder`, and `operator` audits still run against the actual
live host as acceptance receipts.

Before an approved host migration, derive and verify one private checksum-bound snapshot directly
from the manifest. No target paths are re-entered at the command line:

```bash
/usr/bin/python3 -I scripts/host_migration_snapshot.py prepare \
  --manifest /private/approved/proposal/manifest.json \
  --output-dir /private/approved/new-snapshot-directory
/usr/bin/python3 -I scripts/host_migration_snapshot.py verify \
  --receipt /private/approved/new-snapshot-directory/receipt.json --scope both
```

`prepare` rejects manifest/current-state checksum, mode, and absent-state drift before creating the
snapshot directory, then binds the completed backup receipt to the manifest again.

Manifest schema v3 contains a successful full proposed-target audit attestation with detailed
input receipts, its canonical target-inventory digest, complete target inventory, and ordered stage membership. A live
or baseline-only receipt cannot authorize a multi-stage manifest. Before any write, bind the
manifest, target tree, and snapshot receipt with a dry-run:

```bash
/usr/bin/python3 -I scripts/host_migration_apply.py \
  --manifest /private/approved/proposal/manifest.json \
  --target-root /private/approved/proposal/target \
  --snapshot-receipt /private/approved/new-snapshot-directory/receipt.json \
  --stage baseline \
  --dry-run
```

Then create one stage-scoped `approval-envelope.json` from a private build spec. The flat canonical
envelope binds the exact manifest and external target audit, snapshot receipt, authorized stage and
labels, policy/routing, evidence, post-apply commands, and a private copied executor bundle. Its
SHA-256 is the approval ID. `host_migration_envelope.py build` refuses an existing output or executor
root and requires its parent to be current-user-owned mode 0700. `verify` detects byte drift and
rechecks the manifest, target-audit, snapshot, executor, and live-audit path bindings. Every Python
executor invocation must use `-I`, so inherited `PYTHONPATH`, user-site packages, and
`sitecustomize` cannot replace copied sibling modules. The Python 3.9-compatible pure-Python PyYAML
and TOMLI sources under `vendor/` are copied into and digest-bound with the executor; their upstream
license texts are retained under `vendor/licenses/`. A stdlib-only bootstrap verifies the embedded
vendor-manifest digest, exact file inventory, every file digest, and owner/write permissions before
either package is imported.
The approved Python and Codex executables must be regular non-symlink files owned by root or the
current user, with no group/other-writable file or ancestor; their digests are checked before and
after post-apply probes. The build spec contains the absolute paths and argv arrays accepted by
`build_from_spec()`:

```bash
/usr/bin/python3 -I scripts/host_migration_envelope.py build \
  --spec /private/approved/proposal/baseline-envelope-spec.json \
  --output /private/approved/proposal/approval-envelope.json \
  --executor-root /private/approved/proposal/executor
/usr/bin/python3 -I scripts/host_migration_envelope.py verify \
  --envelope /private/approved/proposal/approval-envelope.json \
  --expected-envelope-sha256 "$approved_envelope_sha256"
```

Run the confirmed stage only through the copied executor. Both the envelope and manifest approval
digests are mandatory and are compared before a journal or host target is created:

```bash
/usr/bin/python3 -I /private/approved/proposal/executor/scripts/host_migration_apply.py \
  --manifest /private/approved/proposal/manifest.json \
  --target-root /private/approved/proposal/target \
  --snapshot-receipt /private/approved/new-snapshot-directory/receipt.json \
  --approval-envelope /private/approved/proposal/approval-envelope.json \
  --expected-envelope-sha256 "$approved_envelope_sha256" \
  --expected-manifest-sha256 "$approved_manifest_sha256" \
  --journal-dir /private/approved/proposal/journal \
  --stage baseline --confirm-apply
```

The applier verifies completed labels at target state and every current/future-stage label at its
manifest pre-state before the selected write. It also parses base/profile and protected-reviewer
TOML to keep DB MCP disabled. Confirmed execution requires the exact `baseline`, `builder`,
`operator` stage layout; Codex base/rules/Scout/Builder/Operator, Serena global config, and every
policy-listed Serena project target cannot be omitted from the manifest. The envelope projects the external target receipt's outer `status`
into the manifest attestation, binds its Codex/Serena versions to the live audit argv, and requires
the full policy-defined live-audit input inventory before any write. The approved Python and Codex
executables are canonical-path and SHA-256 bound and rechecked around verification. After installation the schema-v3 journal enters
`awaiting_stage_audit` with `rollback_required: true`. The same command runs the envelope-bound live
stage audit plus fresh base and every installed-profile `codex --profile NAME mcp list --json`
probe, rechecks live file state, and only then changes the journal to `ready` or terminal `complete`.
Runtime probes force the approved host `HOME`/`CODEX_HOME` and cap execution time and captured
output; child processes receive an allowlisted environment and raw stderr is not returned. Audit,
runtime, or final-state failure automatically attempts journal rollback only when the current
invocation's attempt ID has a durable target intent or current-stage mutation evidence; a pre-write or first-intent journal error in a later invocation
cannot roll back an earlier accepted stage. A crash leaves a rollback-only journal and cannot open the next stage.
Successful live-audit and MCP JSON receipts are canonicalized into mode-0600 files beside the
journal, checksum-bound to that journal, and reverified before a later stage can write.

Apply and rollback share one per-user host-operation lock. Absent targets use atomic no-replace;
present targets use atomic exchange and retain the verified previous inode in the journal. Rollback
derives the exact manifest and snapshot from that journal and requires no repeated target inventory:

```bash
/usr/bin/python3 -I /private/approved/proposal/executor/scripts/host_migration_snapshot.py rollback \
  --journal /private/approved/proposal/journal/journal.json \
  --confirm-rollback
```

Use the copied snapshot executor recorded by the journal-bound approval envelope; the repository
source script is not interchangeable with that sealed copy. A rollback restores only completed
labels plus the active uncertain label, requires each selected
file to equal either its approved target state or original pre-state, and refuses an installed
target omitted by journal progress. Each applied stage records the canonical approval-envelope path
and digest; rollback verifies that envelope and the copied apply, envelope, and snapshot executors
before importing a copied sibling. It durably records `rolling_back` plus per-label progress before
terminal `rolled_back`, moves the pathname into an owned same-directory quarantine before verifying
and restoring the approved pre-state, and never overwrites or automatically deletes a concurrently
created path. Retained quarantine paths are recorded in the journal and require a separate identity,
mode, and checksum review before any manual cleanup. It preflights the complete
restore scope before writing, fsyncs restored parent directories, and terminalizes the journal as
`rolled_back`; that journal cannot be reused for a later stage. The contract preserves file bytes
and modes, but not ACLs or extended attributes. See the staged
[host hardening and profile evaluation plan](docs/host-hardening-plan.md) before applying or
rolling back host changes.

Validate skill structure when the Codex system validation scripts are available:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/workflow
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/workflow-intake
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/adversarial-review-loop
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/council
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/session-wiki
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/resume-multi-review
```

Validate plugin structure:

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

If those scripts are unavailable, at minimum confirm each skill has valid YAML frontmatter with `name` and `description`, each skill folder contains `SKILL.md`, and `.codex-plugin/plugin.json` points `skills` at `./skills/`.

Forward-test behavior with the scenarios in `tests/acceptance-scenarios.md` before relying on these skills for high-risk work.

See [forward-test-report.md](docs/forward-test-report.md) for the latest recorded forward-test and smoke-test notes.

### Legacy live evaluation runner

The deterministic dry-run validates the scenario corpus and selection plan only. It does not require an API key, Codex executable, temporary runtime directory, capability probe, checkout installation, subprocess, or network access:

```bash
python3 scripts/run_live_eval.py --tags workflow-intake --model gpt-5.6-sol --dry-run
```

Successful dry-run output reports `status=preflight_only` and `model_calls=0`. This is planning evidence, not model-quality evidence.

The separate harness materialization preflight composes that same scenario planning with a fixed `current` or `lean` bundle and the exact clean-HEAD skill repository. Its three harness options are all-or-none and valid only with `--dry-run`:

```bash
python3 scripts/run_live_eval.py \
  --tags workflow-intake \
  --model gpt-5.6-sol \
  --dry-run \
  --harness-profile current \
  --harness-bundle /path/to/private/harness-bundle \
  --variant-repo /path/to/clean/variant-repo
```

A successful harness preflight reports `status=harness_preflight_only`, `materialization_result=pass`, `model_conformance=not_run`, and `model_calls=0`. It proves only that the fixed files and clean-HEAD skills were materialized, hashed, sealed, and immediately reverified in a private temporary home. It does not launch or probe Codex, prove that Codex consumed those files, or provide model-quality evidence. Output contains only path-free identifiers, counts, and digests; cleanup that cannot be proven changes the result to `status=blocked_cleanup`, `reason=cleanup_unverified`.

Targeted execution selects at most three scenarios and is bounded to five model calls, 600 seconds,
and concurrency one. Release execution selects at most 36 scenarios and is bounded to 40 model
calls, 5,400 seconds, and concurrency two. The normal scenario runner's isolated checkout includes
`workflow`, `workflow-intake`, `adversarial-review-loop`, `council`, and `session-wiki`; the fixed
Phase A and harness materialization profiles retain their legacy three-skill checkout. Release
planning remains safe without approval because `--dry-run` cannot make a model call:

```bash
python3 scripts/run_live_eval.py --release-suite --dry-run
```

A live release suite is a separate operator-approved action and requires both `--release-suite` and `--approve-release-suite`. Omitting the approval flag blocks before credential or executable checks. The flag is invalid without release-suite selection.

Without `--dry-run`, the runner is an explicit live operation. It refuses execution unless `OPENAI_API_KEY` is present and a `codex` executable is available. Live execution creates a private isolated runtime, installs and seals the exact clean-HEAD skill checkout, and performs a final isolation recheck inside every model-call budget lease. Production execution remains blocked when the runtime cannot prove the required network, MCP, plugin, hook, and unexpected-skill isolation capabilities. Blocked runs without retained evidence clean up their owned runtime; assertion or completed runs retain only redacted mode-0600 JSONL output artifacts and report `manual_cleanup_required=true` with the artifact path. Repository tests and `scripts/validate_repo.sh` never perform a live model call.

### Phase A harness experiment preflight

The separate Phase A experiment CLI validates one canonical four-task experiment input, both fixed harness profiles, four operator-attested Git task sources, the static receipt graph, and the deterministic plan. It does not extend the legacy runner:

```bash
python3 scripts/run_harness_experiment.py preflight \
  --input /path/to/canonical-plan.json \
  --bundle-root /path/to/private/harness-bundle \
  --skill-repo /path/to/clean/skill-repository \
  --temp-parent /path/to/private/empty-temp-parent \
  --task-source low-alpha=/path/to/task-low-alpha \
  --task-source low-beta=/path/to/task-low-beta \
  --task-source medium-alpha=/path/to/task-medium-alpha \
  --task-source medium-beta=/path/to/task-medium-beta
```

The input must be exact canonical UTF-8 JSON no larger than 1 MiB. The four task IDs must match the selected task set exactly, each source must be an absolute operator-attested trusted local Git clone at the input's full commit OID, and the caller must provide an exclusive empty private temporary parent.

Success exits `0` with `status=static_only`, `materialization_result=verified`, `cleanup_state=removed`, `reason_code=static_preflight_verified`, and `model_calls=0`. Any blocked result exits `2`. CLI or input rejection uses a fixed all-null result; a later preflight block may retain only the typed path-free digest prefix and cleanup state reached before the block.

This is zero-model-call static evidence. It does not launch or probe Codex, prove that Codex consumed the materialized files, independently execute task qualification, validate a containment backend, compare model quality, or select a winning profile. Live canary, pilot, approval, credential, executable, network, and live-ledger commands are intentionally unavailable in Phase A.

Expected result JSON and sanitized argument-error output retain no host-local absolute paths or raw input. The required paths are still command-line arguments, so shell history and local process listings are outside this output guarantee.

`scripts/validate_repo.sh` always runs full repository-owned test discovery. External shared-agent contract and adapter audits are reported as `not_run` unless `SHARED_AGENTS_ROOT` is explicitly configured; the environment-independent reviewer mutation tests still run on every validation.

## Release History

See [CHANGELOG.md](CHANGELOG.md) for public release notes.

## Public Repo Hygiene

Do not commit local session logs, secrets, private paths, customer data, or environment-specific notes. Keep those in untracked local files such as `SESSION.md`.

## License

MIT
