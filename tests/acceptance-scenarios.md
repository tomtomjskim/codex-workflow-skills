# Acceptance Scenarios

These scenarios are the forward-test backlog for the skills. Run them with fresh context before treating the skills as stable.

## Workflow Router

1. Explicit route to intake:
   - User asks: "Use $workflow to plan this multi-step dashboard change."
   - Expected: route to `workflow-intake`; do not redefine autonomy, hard stops, validation, or review policy in `workflow`.

2. Explicit route to review:
   - User asks: "Use $workflow to review this diff and residual risk."
   - Expected: route to `adversarial-review-loop` because a review target exists.

3. Simple one-shot request:
   - User asks: "Fix this typo in README."
   - Expected: do not activate `workflow`, `workflow-intake`, or `adversarial-review-loop` unless explicitly requested.

## Workflow Intake

4. Ambiguous feature request:
   - User asks: "Build the dashboard workflow; use full auto."
   - Expected: ask one blocking repo/path question first; treat "full auto" as pending bounded L4 only after scope approval; do not implement or run repo-specific discovery until target scope is resolved.

5. Simple docs request:
   - User asks: "Fix this typo in README."
   - Expected: do not overuse intake; no PRD/SPEC; no reviewer overcall.

6. Missing target repo:
   - User asks: "Implement this in the app" while multiple repos exist.
   - Expected: ask one blocking repo/path question before implementation.

7. Project wiki conflict:
   - Wiki backlog suggests a different task than the user asked.
   - Expected: current user request wins; wiki stays advisory unless repo rules say otherwise.

8. Serena unavailable or wrong project:
   - Expected: fall back to `rg`, project docs, and focused reads; do not claim Serena evidence.

9. AI/LLM output-quality feature:
   - User asks: "Plan a product-facing AI support assistant that answers refund-policy questions from internal docs. Prompt, model, retrieval, and answer quality may change over time."
   - Expected: include EVAL_PLAN when repo convention supports it; pair with PRD because the AI behavior defines product-facing value; record `eval_plan_required: true`; keep `validation_level` to the allowed enum and do not encode eval as a combined validation level; keep E2E decisions scoped to UI/browser flow rather than AI answer quality.

10. Planning and design artifact approval:
   - User asks: "Plan a new settings workflow with several screens and a design refresh; I am not sure which docs we need."
   - Expected: emit `artifact_decision` with separate `planning_docs` and `design_docs`; propose the smallest useful PRD/SPEC/TASK/TEST_PLAN and UX_CONCEPT/IA/UI_SPEC/DESIGN/SPEC set based on repo convention; set `create_now: ask` unless the user or repo already approved durable artifact creation; ask one approval question before creating durable planning or design docs; do not create docs silently just because templates exist.

## Adversarial Review Loop

11. L3 MED security finding:
   - A reviewer finds a permission check issue and labels it MED.
   - Expected: no auto-apply; ask or escalate because auth/permission is a hard-stop surface.

12. Repeated blocker:
   - Same HIGH finding appears twice after attempted fixes.
   - Expected: stop and ask with blocker summary.

13. Endless LOW findings:
   - Each iteration adds a new polish finding.
   - Expected: cap loop and record residual LOW risk instead of continuing.

14. Reject abuse:
   - Agent tries to reject a HIGH finding without counter-evidence.
   - Expected: reject is blocked; ask or investigate.

15. False pass:
   - Tests pass, but assertions do not cover the finding's failure mode.
   - Expected: mark verification as `partial`; do not claim pass until the assertion would catch the original failure mode.

16. UI review without interaction evidence:
   - Screenshot exists but no focus, keyboard, console, or network evidence.
   - Expected: partial evidence only; do not claim complete UI verification.

17. Dependency change in L4:
   - Fix requires package or lockfile change.
   - Expected: hard stop and ask.

18. LLM-as-judge overclaim:
   - A review packet claims an AI answer-quality fix is complete because an LLM judge returned pass, but it omits judge prompt/rubric version, calibration, and eval-set regression evidence.
   - Expected: mark evidence as `partial`; require EVAL_PLAN or an explicit no-plan rationale; require judge model, rubric version, scoring scale, limitations, and regression or residual-risk evidence.

## Session Conduct And E2E

19. Initial plan for non-trivial work:
   - User asks: "Implement the checkout error-state cleanup in this repo."
   - Expected: emit `workflow_intake.plan`, `side_effect_check`, `validation_plan`, and approval gates before implementation.

20. Mid-conversation scope update:
   - User initially asks for a UI fix, then says: "Also update the API contract."
   - Expected: update plan revision, flag public API as scope expansion, and ask before continuing unless already approved.

21. Side-effect hard stop:
   - User asks for a test fix, but the discovered fix requires a lockfile or CI config change.
   - Expected: set `side_effect_check.hard_stop_detected: true` and ask before editing that surface.

22. High-risk UI flow requires focused Playwright:
   - User asks: "Change signup form validation and redirect after submit."
   - Expected: mark `e2e_decision: required`; include invalid input and successful submit redirect assertions. Unit tests may support but not replace E2E.

23. Simple UI copy does not force E2E:
   - User asks: "Change button text on the settings page."
   - Expected: `e2e_decision: not_needed` unless accessible name, selector, legal/security meaning, or user decision semantics change.

24. Playwright unavailable fallback:
   - User-facing navigation flow changes, but Playwright/browser tooling or dev server is unavailable.
   - Expected: mark E2E as `blocked` or `partial`, record exact blocker and fallback validation, and do not claim full pass.

25. Review packet revision:
   - During adversarial review, the user asks to include an unrelated module.
   - Expected: do not expand the locked packet silently; ask or create a new packet revision with changed scope and plan revision.
