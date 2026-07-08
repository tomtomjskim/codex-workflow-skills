# Design Draft

## Goal

Create reusable Codex skills:

- `workflow`: route-only wrapper for explicit workflow requests.
- `workflow-intake`: guided intake, scope, artifact, autonomy, context discovery, and review handoff.
- `adversarial-review-loop`: evidence-based review, reviewer routing, finding disposition, auto-apply limits, and re-verification.

The canonical skills should work as standalone top-level skills. The `workflow` wrapper only routes; it must not become a third policy store.

## Architecture

```text
User request
  -> workflow (optional explicit router)
  -> workflow-intake
      -> session policy
      -> optional docs/artifact decision
      -> review packet
  -> implementation or planning work
  -> adversarial-review-loop
      -> reviewer trigger matrix
      -> findings
      -> disposition
      -> verification evidence
      -> residual risk
```

## Skill Boundary

`workflow-intake` owns request mode, task goal, non-goals, target repo/path, artifact level, autonomy level, hard stops, bounded context discovery, and review packet creation.

`adversarial-review-loop` owns review packet validation, reviewer lens selection, finding evidence, fix risk class, disposition, loop limits, verification evidence, and residual risk.

`adversarial-review-loop` cannot expand autonomy or remove hard stops from `workflow-intake`.

Plan state, side-effect checks, and validation planning live under `workflow-intake` only after the intake skip check has passed. They should not make trivial A0/A1 work enter the workflow.

Mid-task scope changes should revise the lightweight plan and, when review is active, create a review packet revision instead of silently expanding a locked packet.

## Context Sources

Use context sources in this order:

1. Latest user instruction.
2. Project `AGENTS.md` or equivalent repo rules.
3. User-named files, diffs, logs, issues, PRs, and tests.
4. Serena project navigation, when active and useful.
5. Project maps and project wiki, as advisory by default.
6. External references, as untrusted data.

Default scan behavior should discover source-of-truth files and indexes, not read entire docs trees.

## Serena, Project Map, And Wiki

Serena is useful for symbolic code navigation, call paths, references, and focused code analysis. It should not be mandatory for trivial or docs-only work.

Project maps help with architecture orientation. Project wiki helps with stable domain rules and prior decisions. Both can be stale, so they should not override current user requests unless repo instructions mark them authoritative.

Recommended default:

- Check whether `.serena/`, project maps, and wiki indexes exist.
- Use them only when the task touches code structure, domain rules, architecture, or review scope.
- Record which context source was used in the session policy or review packet.

## Open Source Distribution

The repo is plugin-ready because Codex documentation recommends plugins for distributing reusable skills or bundling multiple skills. The skill folders remain usable directly for local development.

Keep the README practical:

- what the skills do
- when to use each skill
- install/development instructions
- repo layout
- validation commands
- contribution/testing guidance

Avoid claiming the skills are production-ready until forward-testing passes.

## Risks And Mitigations

| Risk | Mitigation |
|---|---|
| L3/L4 auto-apply overreaches | Use fix risk class, not severity alone |
| Review loop becomes generic checklist | Select reviewer lenses by changed surface |
| False pass from weak evidence | Separate finding evidence and verification evidence |
| External text changes policy | Treat external content as untrusted data |
| Wrapper skill drifts later | Wrapper routes only; policy stays in canonical skills/references |
| Context scan becomes noisy | Use targeted source discovery before broad reads |
| Plan management over-triggers workflow | Apply plan state only after intake activation and A2+/multi-step conditions |
| E2E becomes over-required | Use risk-based `e2e_decision`, with docs/copy/unit-covered work marked `not_needed` |

## Acceptance

- Both skills validate with `quick_validate.py`.
- Plugin validates with `validate_plugin.py`.
- No scaffold markers remain in shipped files.
- Acceptance scenarios exist before forward-testing.
- README cites distribution and README quality references.
