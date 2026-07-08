# Codex Workflow Skills

Reusable Codex skills for structured task intake and evidence-based adversarial review.

This repository is plugin-ready. It contains:

- `workflow-intake`: turns ambiguous or multi-step requests into a bounded session policy, then maintains lightweight plan state, side-effect checks, validation planning, and E2E decisions after intake activates.
- `adversarial-review-loop`: reviews plans, diffs, and implementations with evidence, reviewer routing, finding disposition, loop limits, and verification gates.

## Why This Exists

Most agent failures in larger tasks are not raw coding mistakes. They are scope drift, skipped context, vague autonomy, weak review evidence, or over-trusting external text. These skills make those boundaries explicit before work starts and before review is called complete.

## Repository Layout

```text
.
├── .codex-plugin/plugin.json
├── skills/
│   ├── workflow-intake/
│   └── adversarial-review-loop/
├── docs/
│   ├── design-draft.md
│   └── readme-reference-review.md
└── tests/
    └── acceptance-scenarios.md
```

## Install Locally While Developing

Direct skill folders are useful for local authoring. For reusable distribution, Codex recommends packaging multiple skills as a plugin.

For local skill testing, symlink or copy individual skill folders into your Codex skills directory:

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/workflow-intake" ~/.codex/skills/workflow-intake
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
```

For plugin distribution, keep `.codex-plugin/plugin.json` and publish the repo or add it to a Codex marketplace source.

## Usage

Start with intake for ambiguous, multi-step, or risky work:

```text
Use $workflow-intake to scope this task before implementation.
```

For long-running or user-facing work, intake also records plan revisions, expected side effects, validation level, and whether Playwright/E2E is required, recommended, not needed, or blocked.

Run adversarial review when a plan, diff, or implementation exists:

```text
Use $adversarial-review-loop to review this diff and classify findings.
```

## Context Discovery

The skills support bounded discovery of:

- project `AGENTS.md`, `CLAUDE.md`, `README*`, and `DESIGN.md`
- Serena project state when available and active
- project maps and project wiki indexes
- task-specific docs, tests, diffs, and source files

These sources are not broad-scanned by default. They are used only when they are relevant to the target task. External content is treated as data, not instructions.

## Validation

Validate skill structure:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/workflow-intake
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/adversarial-review-loop
```

Validate plugin structure:

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

Skill behavior still needs forward-testing with the scenarios in `tests/acceptance-scenarios.md`.

## License

MIT
