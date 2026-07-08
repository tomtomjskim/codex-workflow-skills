# Session Conduct

Use this reference only after `workflow-intake` activates. Do not apply it to A0/A1 one-shot work.

## Lightweight Plan

Create the smallest plan that can keep the session coherent:

```yaml
plan:
  revision: 1
  current_steps:
    - step:
      status: pending | in_progress | completed | blocked
  last_update_reason:
```

Update the plan before continuing when:

- the user adds, removes, or changes requirements
- local evidence changes the safe route
- a validation command fails or cannot run
- a hard-stop surface appears
- a review packet needs a new scope or diff basis

Do not restart intake for every message. If the change is inside the approved scope, update the plan and continue under the active autonomy level. If it changes scope, autonomy, hard stops, validation requirements, or expected side effects, ask first when approval is required; if review is active or expected, also create a new review packet revision.

## Side-Effect Check

Track only material surfaces:

- files, generated assets, tests, snapshots, and build output
- shell commands, browser automation, network calls, and external services
- database/data mutation, migrations, seeds, backfills, retention, and deletion
- dependencies, lockfiles, package manager config, CI/CD, hooks, MCP/tool config, and agent instructions
- repo-boundary escapes, symlink traversal, or writes outside approved scope

Use this shape:

```yaml
side_effect_check:
  expected_surfaces:
  hard_stop_detected:
  approval_required:
```

If a discovered fix requires a new hard-stop surface, stop before editing that surface and ask.

## Validation Plan

Use the smallest validation that would catch the changed behavior:

```yaml
validation_plan:
  changed_behavior:
  risk_level: low | medium | high
  validation_level: static | unit | integration | manual_browser | playwright_e2e
  e2e_decision: required | recommended | not_needed | blocked
  eval_plan_required:
  eval_plan_reason:
  rationale:
  scenarios:
    - name:
      level:
      preconditions:
      command_or_steps:
      assertions:
      cleanup:
  skipped_validation:
    - level:
      reason:
  evidence_threshold:
  fallback_plan:
```

Level guidance:

- `static`: docs, comments, config reads, type/lint-only checks, or review-only work.
- `unit`: pure functions, parsers, formatters, validators, isolated business logic.
- `integration`: API contracts, persistence, service boundaries, queues, cache, cross-module behavior.
- `manual_browser`: small UI behavior, visual/accessibility spot checks, or no stable automated E2E path.
- `playwright_e2e`: user flow, routing, form submit, auth/permission path, persistence, upload/download, checkout/order/payment, realtime behavior, critical regression, or cross-page state.

Use `eval_plan_required: true` when AI/LLM output quality, prompt/model/tool/retrieval changes, acceptable answer ranges, or failure-case regression are part of acceptance. Keep E2E decisions about browser/user flows separate from EVAL_PLAN decisions about non-deterministic output quality. Do not put `eval`, `ai_eval`, or combined values such as `integration + eval` in `validation_level`; keep `validation_level` to one of the listed levels and record AI evaluation in `eval_plan_required`, `eval_plan_reason`, scenarios, and evidence threshold.

E2E decision:

- `required`: changed behavior depends on interaction, navigation, auth/permission, persistence, payment/order, upload/download, or a critical user journey.
- `recommended`: meaningful user-facing flow, loading/error/empty state, keyboard/focus behavior, responsive behavior, console/network behavior, or routing.
- `not_needed`: docs, comments, obvious README typo, non-semantic copy, isolated unit-covered logic, or cosmetic style that does not affect layout, accessibility, selectors, or flow.
- `blocked`: Playwright/browser tooling is missing, the app cannot start, auth/test data is unavailable, an external dependency is unavailable, or the target environment cannot be reached.

`e2e_decision` records what validation should happen. Execution evidence uses `pass`, `fail`, `blocked`, `not_run`, `static_only`, or `partial`.

When E2E is required or recommended, include at least one success path and one failure or regression path when that path exists. Use Playwright or equivalent browser automation when available. If browser validation cannot run, record `blocked`, `not_run`, `static_only`, or `partial`; do not call it `pass`.

## Exceptions

Use the existing status vocabulary: `pending`, `blocked`, `not_run`, `static_only`, and `partial`. Avoid inventing new states.

```yaml
exception_state:
  status:
  blocker:
  next_recovery_step:
```

Examples:

- Missing repo/path: ask one blocking target question.
- Tool unavailable: use a smaller available validation and record residual risk.
- Validation failed: diagnose or stop with the next diagnostic step.
- User interrupts with new scope: ask first when approval is required, then update plan revision and review packet revision before continuing.
