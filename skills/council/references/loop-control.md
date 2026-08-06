# Council Loop Control

Treat one loop as independent review, chair synthesis, bounded ideation, and optional artifact
revision. Re-review later loops against the delta and unresolved decisions instead of replaying the
whole packet.

## Preset Limits

| Preset | Completion ceiling per started attempt | Reviewer seats | Max attempts | Max loops |
|---|---:|---:|---:|---:|
| `quick review` | 300 seconds | 1 | 2 | 1 |
| `default` | 600 seconds | 1-2 | 3 | 1 |
| `deep refinement` | 900 seconds | 2 | 4 | 2 |

The completion ceiling begins only after a canonical reviewer target is registered. Use a stricter
host completion or stall policy when one exists. A start ceiling covers only dispatch registration;
it cannot terminate a registered running attempt. Observe progress in bounded wait slices and do
not infer a stall from a single quiet slice.

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
- Required seats, not raw attempts, determine completion. Return `complete` only when every required
  seat has independent completion evidence and every material item is dispositioned.
- Continue as `partial` when one independent reviewer completed but a required seat did not.
- Return `incomplete` with optional `provisional_main_only` analysis when no reviewer completed.
- Block Council-driven writes when a mandatory risk lens is unavailable.
- Preserve the failure receipt; do not turn a stall into an unexplained omission.
- When a thread limit blocks a fresh reviewer, use a separately permitted fresh read-only evaluator
  or remain `partial`; never label a context-contaminated retry as independent.
- Failed attempts do not consume a Council loop. Replace them only after terminal failure, within
  the preset attempt budget, with a fresh alternate role or materially corrected prompt.
- Use the host reviewer-start timeout; when none exists, cap only dispatch registration at 45
  seconds. Apply the preset completion ceiling separately after registration.

## Quality And Resource Discipline

- Reuse stable source evidence and target mappings.
- Send bounded packets for scope integrity, but retain every source needed to judge the target.
- Inline the reviewer contract; only the chair loads Council files.
- Keep independent first-pass reviewers isolated from each other's conclusions.
- Reuse a successfully completed original reviewer for a targeted delta recheck; use an independent
  cross-check for material high-risk resolutions or unresolved disagreement.
- Do not weaken evidence, drop a material lens, interrupt a valid active reviewer, or downgrade a required recheck merely to save tokens, latency, or model usage.
- Stop when the evidence-based stop conditions are met; do not consume loops ceremonially or stop a
  valid review solely for resource convenience.
- Report seats, attempts, unique target IDs, loops, useful findings, and fallbacks in the receipt.
