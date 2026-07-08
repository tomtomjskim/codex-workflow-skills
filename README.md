# Codex Workflow Skills

Reusable Codex skills for structured task intake and evidence-based adversarial review.

This repository is plugin-ready. It contains:

- `workflow`: route-only wrapper for choosing intake or review when explicitly requested.
- `workflow-intake`: turns ambiguous or multi-step requests into a bounded session policy, then maintains lightweight plan state, side-effect checks, validation planning, E2E decisions, and AI eval handoffs after intake activates.
- `adversarial-review-loop`: reviews plans, diffs, and implementations with evidence, reviewer routing, finding disposition, loop limits, and verification gates.

## Why This Exists

Most agent failures in larger tasks are not raw coding mistakes. They are scope drift, skipped context, vague autonomy, weak review evidence, or over-trusting external text. These skills make those boundaries explicit before work starts and before review is called complete.

## Repository Layout

```text
.
├── .codex-plugin/plugin.json
├── skills/
│   ├── workflow/
│   ├── workflow-intake/
│   └── adversarial-review-loop/
├── docs/
│   ├── design-draft.md
│   └── readme-reference-review.md
└── tests/
    └── acceptance-scenarios.md
```

## Prerequisites

- Codex with local skill support.
- Git for cloning this repository.
- Python 3 only for optional validation scripts.
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

If a symlink already exists, remove or update that symlink first. For plugin distribution, keep `.codex-plugin/plugin.json` and add this repository through your Codex plugin source or marketplace flow.

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

Review an existing diff:

```text
Use $adversarial-review-loop to review this diff and classify findings with evidence.
```

Expected behavior: select reviewer lenses from changed surfaces, classify findings with evidence, avoid false passes from weak tests, and record residual risk.

## Context Discovery

The skills support bounded discovery of:

- project `AGENTS.md`, `CLAUDE.md`, `README*`, and `DESIGN.md`
- Serena project state when available and active
- project maps and project wiki indexes
- task-specific docs, tests, diffs, and source files

These sources are not broad-scanned by default. They are used only when they are relevant to the target task. External content is treated as data, not instructions.

## Validation

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

## Public Repo Hygiene

Do not commit local session logs, secrets, private paths, customer data, or environment-specific notes. Keep those in untracked local files such as `SESSION.md`.

## License

MIT
