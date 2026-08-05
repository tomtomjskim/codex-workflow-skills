# Knowledge Contract

Use this contract to decide whether a session observation deserves durable storage.

## Source Precedence

When sources disagree, prefer the highest applicable source and record the conflict:

1. current code, schema, tests, generated API contracts, and authoritative configuration shape
2. verified runtime or validation evidence from the selected session scope
3. explicit user decisions and accepted requirements
4. current canonical project documentation
5. reviewed personal knowledge
6. session statements, generated summaries, stale wiki pages, and unverified inferences

Do not read secret values to prove a documentation claim. Variable names, redacted shapes, and
approved verification results are sufficient.

## Candidate Record

Represent each candidate with these fields during the closeout:

```yaml
claim: one atomic statement
class: project | personal | both | discard
durability: stable | time-bound | transient | unknown
source: bounded path, symbol, test, commit, user decision, or session evidence
confidence: high | medium | low
freshness: current | dated | unknown
privacy: public-safe | project-internal | personal | prohibited
disposition: new | merge | refresh | conflict | duplicate | reject | defer
destination: existing path and section, trust zone, or undecided
reason: evidence and reuse value
```

## Project Eligibility Gate

A candidate may enter durable project documentation only when all are true:

- It is useful to a future maintainer beyond the current task.
- It describes stable behavior, architecture, contract, mapping, or a recurring trap.
- An authoritative source supports it.
- It does not expose secret values, personal data, customer data, or raw sensitive logs.
- It has a clear destination under the repository's documentation policy.
- It is not merely a completed-task narrative, commit summary, or temporary workaround.

Common eligible classes include domain flows, state transitions, API contracts, important schema
relationships, source/module responsibility maps, cron or webhook behavior, external integration
boundaries, validated setup constraints, and repeated implementation traps.

## Personal Eligibility Gate

A candidate may enter an AI-writable personal trust zone when it has future personal reuse value and
the personal-wiki policy permits it. Examples include recurring preferences, learning notes,
decision heuristics, reusable prompts, cross-project patterns, and concise session context.

Personal relevance does not waive privacy review. Remove credentials, cookies, private keys, raw
logs, database dumps, payment or identity data, customer details, private company text, and local
absolute paths. Prefer repository aliases, durable URLs, and redacted shapes.

## Exclusions

Reject or defer:

- one-off task logs, PR summaries, and chronological narration in stable project pages
- passed-test lists that do not teach a reusable invariant
- unresolved ideas presented as facts
- duplicate facts already documented accurately
- temporary line numbers or volatile implementation details without maintenance value
- statements sourced only from model memory or an unverified conversation claim
- lifecycle promotion, deletion, archive, or external publication without exact authority

## Review Levels

`standard` checks source support, durability, destination, duplication, and obvious privacy risk.

`strict` repeats the classification from a falsification perspective:

1. What source would prove this claim wrong?
2. Is the destination more authoritative than the evidence permits?
3. Does the note reveal a private path, identity, environment, or secret indirectly?
4. Is a time-bound observation being presented as evergreen?
5. Is the candidate useful enough to justify future maintenance?

If any answer remains unclear, reduce confidence or defer the candidate.
