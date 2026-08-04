# Council Loop Control

Treat one loop as independent review, chair synthesis, bounded ideation, and optional artifact
revision. Re-review later loops against the delta and unresolved decisions instead of replaying the
whole packet.

## Preset Limits

| Preset | Reviewers | Max loops | Expected use |
|---|---:|---:|---|
| `quick review` | 1 | 1 | Fast material-risk check |
| `default` | 1-2 | 1 | Normal critique and refined answer |
| `deep refinement` | 2 | 2 | Conflicting constraints or valuable alternatives |

Allow an explicit maximum of three loops. `deep` does not expand scope, grant writes, run tests, or
add reviewers beyond the host cap.

## Diverge And Converge

1. Find evidence-backed gaps and disagreements.
2. Generate alternatives only for those gaps or an explicit `ideate` request.
3. Prefer no more than three materially distinct ideas.
4. Compare them against the packet's acceptance criteria.
5. Select, combine, defer, or reject them with reasons.

For generic ideation, consider the safest, simplest, fastest, and most extensible viable directions.
Do not produce cosmetic variations to inflate the option count.

## Continue When

- a new HIGH or MED issue has supporting evidence
- a core reviewer disagreement remains unresolved
- the revision introduces a new risk surface
- acceptance criteria remain unmet
- a requested alternative has not been evaluated

## Stop Early When

- no new material finding appears
- every HIGH and MED item has a disposition
- rejected findings have counter-evidence
- deferred items record residual risk and a revisit condition
- the current result satisfies the bounded acceptance criteria
- skipped lenses have reasons

## Escalate Or Close Incomplete When

- target or authority is ambiguous
- the same blocker repeats twice
- a hard-stop surface needs new approval
- required evidence or validation is unavailable
- the loop ceiling is reached with a material disagreement open
- a mid-loop request materially changes target, scope, or side effects

State `incomplete`, `blocked`, `partial`, or `static_only` instead of `complete` or `pass` when the
evidence does not support completion.

## Reviewer Failure Fallback

- Preflight reviewer capability before announcing a panel. No callable reviewer facility means an
  immediate `incomplete` fallback, not a pseudo-dispatch.
- Continue as `partial` when one independent reviewer completed and skipped lenses are not hard-gated.
- Return `incomplete` with optional `provisional_main_only` analysis when no reviewer completed.
- Block Council-driven writes when a mandatory risk lens is unavailable.
- Preserve the failure receipt; do not turn a stall into an unexplained omission.
- When a thread limit blocks a fresh reviewer, use a separately permitted fresh read-only evaluator
  or remain `partial`; never label a context-contaminated retry as independent.
- Do not consume a loop for a stalled attempt or retry the same failed role and prompt.
- Use the host reviewer-start timeout; when none exists, cap Council startup at 45 seconds.

## Performance Rules

- Reuse stable source evidence and target mappings.
- Send bounded packets instead of full conversation forks.
- Inline the reviewer contract; only the chair loads Council files.
- Keep independent first-pass reviewers isolated from each other's conclusions.
- Reuse the original reviewer for a cheap targeted recheck; cross-check only material high-risk
  resolutions with another lens.
- Stop after marginal review value reaches zero; do not consume all configured loops ceremonially.
- Report reviewers used, loops used, useful findings, and any fallback in the final route summary.
