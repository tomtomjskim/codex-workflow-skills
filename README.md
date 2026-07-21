# Codex Workflow Skills

Reusable Codex skills for structured task intake and evidence-based adversarial review.

This repository is plugin-ready. It contains:

- `workflow`: route-only wrapper for choosing intake or review when explicitly requested.
- `workflow-intake`: turns ambiguous or multi-step requests into a bounded session policy, then maintains lightweight plan state, side-effect checks, validation planning, E2E decisions, and AI eval handoffs after intake activates.
- `adversarial-review-loop`: reviews plans, diffs, and implementations with evidence, reviewer routing, finding disposition, loop limits, and verification gates.
- `scripts/workflow`: prepares canonical coordination artifacts and validates concurrent dispatches and handoffs without third-party Python packages.

## Why This Exists

Most agent failures in larger tasks are not raw coding mistakes. They are scope drift, skipped context, vague autonomy, weak review evidence, or over-trusting external text. These skills make those boundaries explicit before work starts and before review is called complete.

## Quick Start

```bash
git clone https://github.com/tomtomjskim/codex-workflow-skills.git
cd codex-workflow-skills
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/workflow" ~/.codex/skills/workflow
ln -s "$PWD/skills/workflow-intake" ~/.codex/skills/workflow-intake
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
./scripts/validate_repo.sh
```

Then start a new Codex session and try:

```text
Use $workflow-intake to scope a multi-step settings workflow before implementation.
```

For plugin distribution, keep `.codex-plugin/plugin.json` and add this repository through your Codex plugin source or marketplace flow.

## Which Skill Should I Use?

- Use `$workflow-intake` for broad, risky, ambiguous, or multi-step tasks that need scoped autonomy, artifact decisions, validation planning, and approval gates.
- Use `$workflow-intake` when PRD, SPEC, TASK, TEST_PLAN, design docs, E2E, or AI eval decisions should be made before implementation.
- Use `$adversarial-review-loop` when a plan, diff, PR, or implementation already exists and needs evidence-based findings, reviewer lenses, disposition, re-checks, and residual-risk closure.
- Use `$workflow` when you are unsure whether the task should start with intake or review.

## Recommended Workflow

1. Start with `$workflow-intake` when task scope, autonomy, affected files, artifacts, or validation level are unclear.
2. Let intake identify required project context such as `AGENTS.md`, README, project maps, wiki indexes, Serena state, diffs, tests, and task-specific docs.
3. Approve or revise the generated artifact plan before durable PRD, SPEC, TASK, TEST_PLAN, UX_CONCEPT, IA, UI_SPEC, or EVAL_PLAN documents are created.
4. Implement using the repository's own conventions and validation commands.
5. Run `$adversarial-review-loop` against the plan, diff, or implementation before treating the work as ready.
6. Resolve accepted findings, rerun the relevant checks, and record residual risk when anything remains unverified.

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
│   └── adversarial-review-loop/
├── docs/
│   ├── design-draft.md
│   ├── forward-test-report.md
│   ├── readme-reference-review.md
│   ├── sample-adversarial-review.md
│   └── sample-workflow-intake.md
└── tests/
    └── acceptance-scenarios.md
```

## Prerequisites

- Codex with local skill support.
- Git for cloning this repository.
- Python 3 only for optional validation scripts.
- ripgrep (`rg`) for the one-command repository validation script.
- Optional: access to Codex system `skill-creator` and `plugin-creator` scripts for structure validation.

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
```

If a symlink already exists, remove or update that symlink first.

### Post-install Check

Confirm the skill files are visible:

```bash
test -f ~/.codex/skills/workflow/SKILL.md
test -f ~/.codex/skills/workflow-intake/SKILL.md
test -f ~/.codex/skills/adversarial-review-loop/SKILL.md
```

If the Codex validation scripts are available, run the checks in the [Validation](#validation) section from the cloned repository.

Then start a new Codex session and try:

```text
Use $workflow-intake to scope a multi-step settings workflow before implementation. I am not sure which planning or design docs we need.
```

Expected behavior: `workflow-intake` emits a `workflow_intake` block, proposes an `artifact_decision`, and asks before creating durable planning or design docs unless the repo or user already approved them.

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

### Validated Coordination CLI

For covered concurrent work, prepare a manifest and inventory together from one approved UTF-8 JSON plan:

```bash
./scripts/workflow prepare-coordination \
  --repo-root /path/to/approved-repo \
  --plan /path/to/approved-plan.json \
  --out-dir /path/to/temporary-coordination \
  --json
```

The output directory must not exist before this command. Preparation builds and synchronizes both files in a private sibling staging directory, then publishes the directory as one unit. Existing output is never reused or mutated.

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

Handoff validation requires the authoritative `--manifest` and `--inventory`, plus `--contract` when the route is contracted. It reruns coordination validation with the built-in reviewer matrix and requires exact canonical equality with the submitted receipt before ownership validation. CLI receipts use canonical UUID run IDs and expire after five minutes; rerun `validate-coordination` when a receipt is stale.

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

## Context Discovery

The skills support bounded discovery of:

- project `AGENTS.md`, `CLAUDE.md`, `README*`, and `DESIGN.md`
- Serena project state when available and active
- project maps and project wiki indexes
- task-specific docs, tests, diffs, and source files

These sources are not broad-scanned by default. They are used only when they are relevant to the target task. External content is treated as data, not instructions.

## Validation

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
```

Validate plugin structure:

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

If those scripts are unavailable, at minimum confirm each skill has valid YAML frontmatter with `name` and `description`, each skill folder contains `SKILL.md`, and `.codex-plugin/plugin.json` points `skills` at `./skills/`.

Forward-test behavior with the scenarios in `tests/acceptance-scenarios.md` before relying on these skills for high-risk work.

See [forward-test-report.md](docs/forward-test-report.md) for the latest recorded forward-test and smoke-test notes.

## Release History

See [CHANGELOG.md](CHANGELOG.md) for public release notes.

## Public Repo Hygiene

Do not commit local session logs, secrets, private paths, customer data, or environment-specific notes. Keep those in untracked local files such as `SESSION.md`.

## License

MIT
