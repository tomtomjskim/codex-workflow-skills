# Forward-Test Report

Date: 2026-07-08

This report records the latest validation evidence for the public `codex-workflow-skills` repository. It is intentionally scoped to repeatable checks and known limits, not private session notes.

## Scope

- `workflow-intake` guided-intake behavior for planning, design artifact decisions, validation level selection, and E2E recommendations.
- `adversarial-review-loop` behavior for read-only review routing, evidence requirements, finding severity, disposition, and residual-risk reporting.
- Repository release hygiene for README links, changelog coverage, plugin manifest version alignment, and public-content scans.

## Fresh-Context Forward Tests

### Workflow Intake Artifact Decisions

Synthetic task: plan a multi-step UI/product workflow where the user had not decided whether planning or design documents should be generated.

Result:

- The first pass exposed an output-contract issue: `artifact_decision.create_now` could incorrectly resolve to `no` even when durable docs were useful but unapproved.
- The skill was updated to require `create_now: ask` for read-only, blocked, or approval-gated intake when durable docs are useful.
- A follow-up pass exposed an enum-shape issue: generated values could combine multiple supported enum values into one invalid value.
- The skill was updated with an output-contract guard requiring exact enum values for autonomy, validation, and E2E decisions.
- Final pass matched the intended contract: planning/design artifact recommendations were separated, approval was requested before durable docs, and E2E was recommended rather than silently skipped.

### Adversarial Review Loop

Synthetic task: review a UI-oriented diff packet with a form submission flow and no full repository context.

Result:

- The reviewer stayed read-only, selected relevant UX/accessibility/QA lenses, and marked unrun checks as `static_only/not_run`.
- The output correctly avoided a false pass when the full diff and executable app were unavailable.
- A material finding was identified: a `button` inside a form without an explicit `type="button"` can submit the form when the command is meant to save a draft.
- The sample adversarial review output was updated to include this HIGH finding and concrete remediation guidance.

## Clean-Install Smoke Test

Expected command sequence:

```bash
git clone https://github.com/tomtomjskim/codex-workflow-skills.git
cd codex-workflow-skills
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/workflow" ~/.codex/skills/workflow
ln -s "$PWD/skills/workflow-intake" ~/.codex/skills/workflow-intake
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
test -f ~/.codex/skills/workflow/SKILL.md
test -f ~/.codex/skills/workflow-intake/SKILL.md
test -f ~/.codex/skills/adversarial-review-loop/SKILL.md
./scripts/validate_repo.sh
```

The local smoke test for this release uses an isolated temporary clone and isolated symlink target instead of mutating the user's real `~/.codex/skills` directory.

Latest local result: passed on 2026-07-08 with an isolated clone, isolated skill symlinks, and `./scripts/validate_repo.sh`.

## Known Limits

- Forward tests used synthetic prompts and artifacts rather than a real production repository.
- The clean-install smoke test verifies clone, file visibility, symlink shape, and repository validation. It does not programmatically launch a brand-new Codex UI session and inspect skill-trigger behavior.
- Browser or Playwright E2E remains task-dependent. `workflow-intake` should recommend it by default for real UI work, but these skills themselves do not include a browser app to exercise.

## Release Gate

### Deterministic live-eval runner validation

- Deterministic scenario, isolation, checkout, budget, artifact, and runner tests are part of the non-network repository release gate.
- The runner dry-run verifies planning preflight only and reports `preflight_only` with zero model calls.
- live model execution: not_run
- No model-quality or production-network conclusion is inferred from deterministic tests or dry-run output.

Before public release, run:

```bash
./scripts/validate_repo.sh
```

For higher-risk skill edits, add at least one fresh-context forward test against `tests/acceptance-scenarios.md` and update this report with the result and limits.
