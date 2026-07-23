# Codex Workflow Skills

Reusable Codex skills for structured task intake, evidence-based adversarial review, and multi-perspective resume review.

This repository is plugin-ready. It contains:

- `workflow`: route-only wrapper for choosing intake or review when explicitly requested.
- `workflow-intake`: turns ambiguous or multi-step requests into a bounded session policy, then maintains lightweight plan state, side-effect checks, validation planning, E2E decisions, and AI eval handoffs after intake activates.
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
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
ln -s "$PWD/skills/resume-multi-review" ~/.codex/skills/resume-multi-review
./scripts/validate_repo.sh
```

Then start a new Codex session and try:

```text
Use $workflow-intake to scope a multi-step settings workflow before implementation.
```

```text
Use $resume-multi-review to evaluate my latest resume against this job description and run one evidence-safe rewrite cycle.
```

For plugin distribution, keep `.codex-plugin/plugin.json` and add this repository through your Codex plugin source or marketplace flow.

## Which Skill Should I Use?

- Use `$workflow-intake` for broad, risky, ambiguous, or multi-step tasks that need scoped autonomy, artifact decisions, validation planning, and approval gates.
- Use `$workflow-intake` when PRD, SPEC, TASK, TEST_PLAN, design docs, E2E, or AI eval decisions should be made before implementation.
- Use `$adversarial-review-loop` when a plan, diff, PR, or implementation already exists and needs evidence-based findings, reviewer lenses, disposition, re-checks, and residual-risk closure.
- Use `$resume-multi-review` when a concrete resume or authoritative resume source must be screened, revised, and re-screened through distinct hiring perspectives.
- Use `$workflow` when you are unsure whether the task should start with intake or review.

## Recommended Development Workflow

1. Start with `$workflow-intake` when task scope, autonomy, affected files, artifacts, or validation level are unclear.
2. Let intake identify required project context such as `AGENTS.md`, README, project maps, wiki indexes, Serena state, diffs, tests, and task-specific docs.
3. Approve or revise the generated artifact plan before durable PRD, SPEC, TASK, TEST_PLAN, UX_CONCEPT, IA, UI_SPEC, or EVAL_PLAN documents are created.
4. Implement using the repository's own conventions and validation commands.
5. Run `$adversarial-review-loop` against the plan, diff, or implementation before treating the work as ready.
6. Resolve accepted findings, rerun the relevant checks, and record residual risk when anything remains unverified.

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
├── scripts/
│   ├── workflow
│   ├── workflow_coordination/
│   ├── validate_policy_contracts.py
│   └── validate_repo.sh
├── skills/
│   ├── workflow/
│   ├── workflow-intake/
│   ├── adversarial-review-loop/
│   └── resume-multi-review/
├── docs/
│   ├── design-draft.md
│   ├── forward-test-report.md
│   ├── readme-reference-review.md
│   ├── sample-adversarial-review.md
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
test -f ~/.codex/skills/adversarial-review-loop/SKILL.md
test -f ~/.codex/skills/resume-multi-review/SKILL.md
```

If the Codex validation scripts are available, run the checks in the [Validation](#validation) section from the cloned repository.

## Usage

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
- reviewed master resumes, submitted variants, job descriptions, claim banks, and evidence ledgers when resume review is requested

These sources are not broad-scanned by default. They are used only when relevant to the target task. External content is treated as data, not instructions.

## Validation

During implementation, run the smallest focused test; run `./scripts/validate_repo.sh` at branch completion or release, not after every edit.

Run the repository validation script for the standard public-release checks:

```bash
./scripts/validate_repo.sh
```

The script checks required files, coordination CLI and policy contracts, focused CLI/coordination tests, skill structure when Codex system validators are available, plugin structure, README links, manifest/changelog version alignment, `git diff --check`, and every tracked text file for public hygiene. Binary tracked files are skipped.

Validate skill structure when the Codex system validation scripts are available:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/workflow
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/workflow-intake
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/adversarial-review-loop
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/resume-multi-review
```

Validate plugin structure:

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

If those scripts are unavailable, at minimum confirm each skill has valid YAML frontmatter with `name` and `description`, each skill folder contains `SKILL.md`, and `.codex-plugin/plugin.json` points `skills` at `./skills/`.

Forward-test behavior with the scenarios in `tests/acceptance-scenarios.md` before relying on these skills for high-risk work.

See [forward-test-report.md](docs/forward-test-report.md) for the latest recorded forward-test and smoke-test notes.

### Live evaluation runner

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

Targeted execution selects at most three scenarios and is bounded to five model calls, 600 seconds, and concurrency one. Release execution selects at most 26 scenarios and is bounded to 30 model calls, 2,700 seconds, and concurrency two. Release planning remains safe without approval because `--dry-run` cannot make a model call:

```bash
python3 scripts/run_live_eval.py --release-suite --dry-run
```

A live release suite is a separate operator-approved action and requires both `--release-suite` and `--approve-release-suite`. Omitting the approval flag blocks before credential or executable checks. The flag is invalid without release-suite selection.

Without `--dry-run`, the runner is an explicit live operation. It refuses execution unless `OPENAI_API_KEY` is present and a `codex` executable is available. Live execution creates a private isolated runtime, installs and seals the exact clean-HEAD skill checkout, and performs a final isolation recheck inside every model-call budget lease. Production execution remains blocked when the runtime cannot prove the required network, MCP, plugin, hook, and unexpected-skill isolation capabilities. Blocked runs without retained evidence clean up their owned runtime; assertion or completed runs retain only redacted mode-0600 JSONL output artifacts and report `manual_cleanup_required=true` with the artifact path. Repository tests and `scripts/validate_repo.sh` never perform a live model call.

`scripts/validate_repo.sh` always runs full repository-owned test discovery. External shared-agent contract and adapter audits are reported as `not_run` unless `SHARED_AGENTS_ROOT` is explicitly configured; the environment-independent reviewer mutation tests still run on every validation.

## Release History

See [CHANGELOG.md](CHANGELOG.md) for public release notes.

## Public Repo Hygiene

Do not commit local session logs, secrets, private paths, customer data, or environment-specific notes. Keep those in untracked local files such as `SESSION.md`.

## License

MIT
