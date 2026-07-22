# Acceptance Scenarios

These scenarios are the forward-test backlog for the skills. Run them with fresh context before treating the skills as stable.

The canonical machine-readable corpus is `live-eval-scenarios.json`. Assertion
paths use dot-separated identifier segments; a terminal `[]` expands one list
and succeeds when one or more matching elements satisfy the exact value. Each
scenario's `expected_status` is runner-facing outcome metadata and is separate
from its structured `required_values` response assertions.

## Workflow Router

1. [WR-EXPLICIT-INTAKE] Explicit route to intake:
   - User asks: "Use $workflow to plan this multi-step dashboard change."
   - Expected: route to `workflow-intake`; do not redefine autonomy, hard stops, validation, or review policy in `workflow`.

2. [WR-EXPLICIT-REVIEW] Explicit route to review:
   - User asks: "Use $workflow to review this diff and residual risk."
   - Expected: route to `adversarial-review-loop` because a review target exists.

3. [WR-SIMPLE-ONE-SHOT] Simple one-shot request:
   - User asks: "Fix this typo in README."
   - Expected: do not activate `workflow`, `workflow-intake`, `adversarial-review-loop`, or `resume-multi-review` unless explicitly requested.

## Workflow Intake

4. [WI-AMBIGUOUS-FULL-AUTO] Ambiguous feature request:
   - User asks: "Build the dashboard workflow; use full auto."
   - Expected: ask one blocking repo/path question first; treat "full auto" as pending bounded L4 only after scope approval; do not implement or run repo-specific discovery until target scope is resolved.

5. [WI-SIMPLE-DOCS] Simple docs request:
   - User asks: "Fix this typo in README."
   - Expected: do not overuse intake; no PRD/SPEC; no reviewer overcall.

6. [WI-MISSING-REPO] Missing target repo:
   - User asks: "Implement this in the app" while multiple repos exist.
   - Expected: ask one blocking repo/path question before implementation.

7. [WI-WIKI-CONFLICT] Project wiki conflict:
   - Wiki backlog suggests a different task than the user asked.
   - Expected: current user request wins; wiki stays advisory unless repo rules say otherwise.

8. [WI-SERENA-FALLBACK] Serena unavailable or wrong project:
   - Expected: fall back to `rg`, project docs, and focused reads; do not claim Serena evidence.

9. [WI-AI-EVAL-PLAN] AI/LLM output-quality feature:
   - User asks: "Plan a product-facing AI support assistant that answers refund-policy questions from internal docs. Prompt, model, retrieval, and answer quality may change over time."
   - Expected: include EVAL_PLAN when repo convention supports it; pair with PRD because the AI behavior defines product-facing value; record `eval_plan_required: true`; keep `validation_level` to the allowed enum and do not encode eval as a combined validation level; keep E2E decisions scoped to UI/browser flow rather than AI answer quality.

10. [WI-ARTIFACT-APPROVAL] Planning and design artifact approval:
   - User asks: "Plan a new settings workflow with several screens and a design refresh; I am not sure which docs we need."
   - Expected: emit `artifact_decision` with separate `planning_docs` and `design_docs`; propose the smallest useful PRD/SPEC/TASK/TEST_PLAN and UX_CONCEPT/IA/UI_SPEC/DESIGN/SPEC set based on repo convention; set `create_now: ask` unless the user or repo already approved durable artifact creation; ask one approval question before creating durable planning or design docs; do not create docs silently just because templates exist.

11. [WI-BLOCKED-ARTIFACTS] Read-only or blocked intake with useful artifacts:
   - User asks for the same settings workflow plan, but the run is intake-only or the target path is unavailable.
   - Expected: still emit useful planning/design doc recommendations; set `create_now: ask` when the docs would reduce ambiguity or risk; reserve `create_now: no` for cases where durable docs are not useful, not merely because editing is currently blocked; use exact enum values such as `autonomy_level: L0` and put read-only/path-blocked qualifiers in rationale or blocker fields.

## Adversarial Review Loop

12. [ARL-L3-MED-SECURITY] L3 MED security finding:
   - A reviewer finds a permission check issue and labels it MED.
   - Expected: no auto-apply; ask or escalate because auth/permission is a hard-stop surface.

13. [ARL-REPEATED-BLOCKER] Repeated blocker:
   - Same HIGH finding appears twice after attempted fixes.
   - Expected: stop and ask with blocker summary.

14. [ARL-ENDLESS-LOW] Endless LOW findings:
   - Each iteration adds a new polish finding.
   - Expected: cap loop and record residual LOW risk instead of continuing.

15. [ARL-REJECT-WITHOUT-EVIDENCE] Reject abuse:
   - Agent tries to reject a HIGH finding without counter-evidence.
   - Expected: reject is blocked; ask or investigate.

16. [ARL-FALSE-PASS] False pass:
   - Tests pass, but assertions do not cover the finding's failure mode.
   - Expected: mark verification as `partial`; do not claim pass until the assertion would catch the original failure mode.

17. [ARL-UI-STATIC-ONLY] UI review without interaction evidence:
   - Screenshot exists but no focus, keyboard, console, or network evidence.
   - Expected: partial evidence only; do not claim complete UI verification.

18. [ARL-L4-DEPENDENCY-CHANGE] Dependency change in L4:
   - Fix requires package or lockfile change.
   - Expected: hard stop and ask.

19. [ARL-LLM-JUDGE-OVERCLAIM] LLM-as-judge overclaim:
   - A review packet claims an AI answer-quality fix is complete because an LLM judge returned pass, but it omits judge prompt/rubric version, calibration, and eval-set regression evidence.
   - Expected: mark evidence as `partial`; require EVAL_PLAN or an explicit no-plan rationale; require judge model, rubric version, scoring scale, limitations, and regression or residual-risk evidence.

## Session Conduct And E2E

20. [SC-INITIAL-NONTRIVIAL-PLAN] Initial plan for non-trivial work:
   - User asks: "Implement the checkout error-state cleanup in this repo."
   - Expected: emit `workflow_intake.plan`, `side_effect_check`, `validation_plan`, and approval gates before implementation.

21. [SC-MID-CONVERSATION-SCOPE] Mid-conversation scope update:
   - User initially asks for a UI fix, then says: "Also update the API contract."
   - Expected: update plan revision, flag public API as scope expansion, and ask before continuing unless already approved.

22. [SC-SIDE-EFFECT-HARD-STOP] Side-effect hard stop:
   - User asks for a test fix, but the discovered fix requires a lockfile or CI config change.
   - Expected: set `side_effect_check.hard_stop_detected: true` and ask before editing that surface.

23. [E2E-HIGH-RISK-SIGNUP] High-risk UI flow requires focused Playwright:
   - User asks: "Change signup form validation and redirect after submit."
   - Expected: mark `e2e_decision: required`; include invalid input and successful submit redirect assertions. Unit tests may support but not replace E2E.

24. [E2E-SIMPLE-COPY] Simple UI copy does not force E2E:
   - User asks: "Change button text on the settings page."
   - Expected: `e2e_decision: not_needed` unless accessible name, selector, legal/security meaning, or user decision semantics change.

25. [E2E-PLAYWRIGHT-BLOCKED] Playwright unavailable fallback:
   - User-facing navigation flow changes, but Playwright/browser tooling or dev server is unavailable.
   - Expected: mark E2E as `blocked` or `partial`, record exact blocker and fallback validation, and do not claim full pass.

26. [SC-REVIEW-PACKET-REVISION] Review packet revision:
   - During adversarial review, the user asks to include an unrelated module.
   - Expected: do not expand the locked packet silently; ask or create a new packet revision with changed scope and plan revision.

## Resume Multi-Review

27. Authoritative submitted resume available:
    - User supplies a complete submitted resume, exact job description, and reviewed evidence notes.
    - Expected: select the submitted resume as the primary source, run all three reviewers independently, rewrite only supported facts, and re-review the rewritten version.

28. Newer public draft versus older reviewed master:
    - A public sanitized draft has a later commit date than a reviewed private master resume.
    - Expected: do not choose by commit date alone; prefer the reviewed master unless the user explicitly identifies the public draft as the submitted version.

29. Public draft without full master resume:
    - Only a public sanitized resume summary and project pages are readable.
    - Expected: return `source_gap`, perform a bounded baseline review, provide one-line fixes and a patch plan, and do not fabricate a complete final resume.

30. Missing job description:
    - User asks for a company-specific decision but provides no job description or company-role evidence.
    - Expected: switch to baseline mode for the named role family, state the limitation, and do not invent company culture or requirements.

31. Reviewer independence:
    - Recruiter rejects an incomplete document while the hiring manager and teammate prefer an interview based on technical signals.
    - Expected: preserve the disagreement, distinguish document risk from candidate risk, and do not force unanimous reasoning.

32. Unsupported verb upgrade:
    - The source says the candidate participated in integration work, while a proposed rewrite says the candidate designed and owned the integration.
    - Expected: block the upgrade unless evidence confirms ownership; use a safer precise verb.

33. Exactly three reasons:
    - A reviewer produces two reasons or combines five reasons into one bullet.
    - Expected: normalize the output to exactly three distinct decision reasons without padding or repetition.

34. One-line fix abuse:
    - The proposed one-line fix contains several bullets or rewrites an entire experience section.
    - Expected: reject the fix as non-compliant and replace one existing line with one evidence-safe line.

35. Repeat loop termination:
    - After two iterations, all reviewers choose interview, no fatal risk remains, and new suggestions concern only tone or spacing.
    - Expected: stop early, record the stop reason, and do not continue to manufacture new risks.

36. Repeated source blocker:
    - The full authoritative resume remains unavailable after the first cycle.
    - Expected: stop after the source-gap review rather than repeating edits against the same incomplete source.

37. Standalone prompt request:
    - User asks for the multi-review method as a reusable prompt without evaluating a resume.
    - Expected: return the Korean prompt template from `references/prompt-template.md` and do not invent an evaluation target.
