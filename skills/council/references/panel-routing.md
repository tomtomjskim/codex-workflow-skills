# Council Panel Routing

Route lenses from the target's material risk, not from a fixed ceremonial panel. Host-provided roles
are capabilities, not authority; all Council reviewers remain read-only and non-recursive.

## Core Route

Always start with the falsifier lens unless the user selected a narrower one-reviewer focus:

- **Falsifier:** correctness, feasibility, hidden dependencies, failure modes, and unsupported
  assumptions.

Choose the second lens from the highest-value signal:

| Target signal | Specialist lens | Typical questions |
|---|---|---|
| auth, permissions, secrets, trust boundary | security/access | Can an unauthorized actor cross the boundary? What evidence is missing? |
| schema, migration, query, data repair | data/migration | Can data be lost, misread, duplicated, or left inconsistent? |
| public API, event, integration | API/compatibility | Does the contract remain compatible and failure-safe? |
| concurrency, cache, latency, scale | performance/reliability | What fails under load, retry, race, or partial outage? |
| UI, flow, accessibility | UX/accessibility | Can a user understand, reach, and recover from every relevant state? |
| tests, rollout, release claim | QA/verification | Would the evidence catch the original failure mode? |
| architecture, plan, ownership | architecture/delivery | Are boundaries, sequencing, dependencies, and rollback credible? |
| decision among options | alternative/challenger | Is there a materially simpler or safer option with better tradeoffs? |

When several hard-gated signals exist, obey repository routing policy. If the permitted panel cannot
cover every mandatory lens, report the skipped mandatory lens and stop Council-driven writes.

## Runtime Role Mapping

Use a native specialist role when one is available and relevant. Otherwise dispatch a generic
read-only reviewer with the lens and packet explicitly inlined. Do not invent a role's authority or
assume that a role name proves independent execution.

Reviewer provenance should identify, when available:

- runtime or evaluator type
- role/lens
- whether the context was fresh or inherited
- whether another first-pass conclusion was visible

Never include secrets, credentials, private chain-of-thought, or unnecessary personal data in the
packet or provenance.

## Independence Test

Treat first-pass reviews as independent only when:

1. neither reviewer received the other's conclusion,
2. neither reviewer is a recursive delegate of the other,
3. each got the same locked target revision and acceptance criteria, and
4. runtime provenance supports separate evaluation contexts.

If these conditions are not met, retain the analysis as a secondary critique but do not count it as
an independent vote or consensus signal.

## Replacement Route

A replacement fills an existing seat; it does not create a new vote. Dispatch one only after the
prior attempt has a terminal state and the preset attempt budget permits it. Preserve the locked
target revision, acceptance criteria, lens, evidence threshold, and non-goals. Use a fresh context
and either a different capable role or a materially corrected prompt. Do not include partial output
from the failed attempt.

Record the new attempt ID, canonical target ID, and `supersedes_attempt_id`. If the alternate cannot
preserve the required lens or independence boundary, leave the seat incomplete instead of silently
substituting a weaker review.
