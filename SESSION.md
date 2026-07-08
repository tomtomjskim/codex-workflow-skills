# SESSION: workflow skills risk closure

Date: 2026-07-08

## Current Goal

Resolve validation and forward-test risk before publishing or wiring the new workflow skill repository to GitHub.

## Repository

- Local path: `/Users/jeongsik/dev/codex-workflow-skills`
- Branch: `main`
- GitHub owner requested by TOM: `tomtomjskim`
- Current state: new local git repository, no commits yet
- `gh` active account: `tomtomjskim`
- Local git user: `tomtomjskim <124156309+tomtomjskim@users.noreply.github.com>`

## Scope

In scope:

- Keep `workflow-intake` and `adversarial-review-loop` as two top-level skills.
- Validate skill structure and plugin manifest.
- Run or simulate forward-test pressure scenarios.
- Patch skill docs for any confirmed gaps.
- Configure local GitHub/Git identity for `tomtomjskim`.
- Prepare local repo for first commit and remote setup.

Out of scope until explicitly approved:

- Publishing a public GitHub repository.
- Installing the plugin into a shared marketplace.
- Enabling the skills globally via symlink or copy.
- Creating a wrapper `workflow` skill.

## Known Risks To Close

1. Skill behavior has not been forward-tested with fresh-context scenarios.
2. `workflow-intake` may over-trigger if descriptions or defaults are too broad.
3. `adversarial-review-loop` may false-pass if evidence fields are treated as optional.
4. L3/L4 auto-apply boundaries must remain stricter than severity labels.
5. Serena/project-map/wiki discovery must stay targeted, not broad-scan by default.
6. GitHub local account must use `tomtomjskim`, not the currently active `tom221101`.
7. Plan/update/E2E additions may over-trigger workflow or create a third policy store if not kept behind the intake activation boundary.

## Validation Evidence So Far

- `quick_validate.py skills/workflow-intake`: pass
- `quick_validate.py skills/adversarial-review-loop`: pass
- `validate_plugin.py .`: pass
- Scaffold marker scan: pass
- `gh` active account switched to `tomtomjskim`: pass
- Local git identity set to `tomtomjskim <124156309+tomtomjskim@users.noreply.github.com>`: pass
- Forward-test batch 1 completed with 5 fresh-context subagents: patch required
- Forward-test regression round completed with 3 fresh-context subagents: pass
- Post-regression `quick_validate.py skills/workflow-intake`: pass
- Post-regression `quick_validate.py skills/adversarial-review-loop`: pass
- Post-regression `validate_plugin.py .`: pass
- Post-regression scaffold/wording scan: pass
- Plan/change/E2E adversarial brainstorming round completed with 3 subagents: patch required
- Plan/change/E2E forward-test round completed with 3 subagents: pass
- Post-plan-change `quick_validate.py skills/workflow-intake`: pass
- Post-plan-change `quick_validate.py skills/adversarial-review-loop`: pass
- Post-plan-change `validate_plugin.py .`: pass
- Post-plan-change scaffold/wording scan: pass

## Risk Closure Status

- Fresh-context over-trigger, full-auto, MED hard-stop, false-pass, and wiki/project-map scope risks have been tested and patched.
- Plan management, mid-task change control, side-effect checks, and E2E decision policy have been added behind the existing intake activation boundary.
- Remaining publication decision: GitHub repository visibility has not been specified, so remote creation is intentionally blocked.
- Remaining git decision: no initial commit has been created yet.

## Forward-Test Findings Applied

- Added A0/A1 skip check to prevent `workflow-intake` over-triggering for README typo and simple one-shot tasks.
- Clarified that missing target repo/path blocks repo-specific discovery and implementation.
- Clarified that "full auto" maps only to bounded L4 after scope approval.
- Clarified wiki/project-map/backlog content cannot redefine task scope.
- Narrowed auth hard stop so auth-adjacent UI copy is not over-blocked unless security semantics change.
- Added explicit L3 MED hard-stop auto-apply block.
- Expanded false-pass prevention to all findings, not only security/UX/accessibility/data findings.
- Aligned acceptance scenarios and evidence field spelling with patched skill behavior.
- Added `session-conduct.md` for lightweight plan state, mid-task change triage, bounded side-effect checks, validation levels, and Playwright/E2E decisions.
- Added plan revision, side-effect, validation plan, and scope-change fields to review packets and output contracts.
- Added acceptance scenarios for plan initialization, mid-task scope changes, side-effect hard stops, E2E requirement, Playwright fallback, and review packet revisions.
- Tightened approval wording for public API/scope changes and separated `e2e_decision` from execution evidence to reduce false-complete risk.

## Next Steps

1. Create initial commit if TOM wants the local scaffold checkpointed.
2. Ask before creating a public/private GitHub remote because repository visibility has not been specified.
3. After visibility is chosen, create `tomtomjskim/codex-workflow-skills` remote and push `main`.
