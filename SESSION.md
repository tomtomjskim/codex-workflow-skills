# SESSION: workflow skills risk closure

Date: 2026-07-08

## Current Goal

Resolve validation and forward-test risk before publishing or wiring the new workflow skill repository to GitHub.

## Repository

- Local path: `/Users/jeongsik/dev/codex-workflow-skills`
- Branch: `main`
- GitHub owner requested by TOM: `tomtomjskim`
- Current state: public GitHub repository pushed and tracking `origin/main`
- GitHub URL: `https://github.com/tomtomjskim/codex-workflow-skills`
- Initial scaffold commit: `f2d0756 Initial workflow skills scaffold`
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
- Initial commit created: `f2d0756 Initial workflow skills scaffold`
- Public GitHub repository created: `tomtomjskim/codex-workflow-skills`
- `main` pushed to `origin/main`: pass

## Risk Closure Status

- Fresh-context over-trigger, full-auto, MED hard-stop, false-pass, and wiki/project-map scope risks have been tested and patched.
- Plan management, mid-task change control, side-effect checks, and E2E decision policy have been added behind the existing intake activation boundary.
- Public GitHub publication is complete.
- Local `main` tracks `origin/main`.

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

1. Optionally install the skills locally via symlink or plugin flow.
2. Optionally reflect the common workflow/review skill in `codex-project-guide`.
3. Optionally create a route-only wrapper skill later; do not duplicate policy there.
