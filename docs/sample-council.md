# Sample Council Result

This is an illustrative response for a static review of a proposed checkout retry plan. It shows
the result contract; it is not evidence that the plan was executed or validated in a live system.

## Council result

- Target: checkout retry plan, revision v0
- Execution: default, refine, standard, 1 loop
- Status: complete
- Reviewer provenance: two separate read-only reviewer contexts
- Reviewer attempts: `S1/A1/T1 completed`; `S2/A2/T2 completed`; no replacement attempts
- Selected lenses: falsifier; reliability and alternative design
- Skipped lenses: security — no auth, secret, or trust-boundary change was present in the packet

### Redefined result

Retry only idempotent checkout status reads. Use bounded exponential backoff with jitter, a total
deadline, and cancellation propagation. Do not retry payment creation automatically. Emit one
stable operation identifier per checkout attempt, expose a recoverable user state after the retry
budget is exhausted, and make rollout reversible behind the existing configuration boundary.

Acceptance criteria:

1. A retry cannot create a second payment or order.
2. Client cancellation stops pending retry work.
3. Exhausted retries return a user-recoverable state rather than an ambiguous success.
4. Unit tests cover budget exhaustion and cancellation; an integration test covers duplicate
   responses for the same operation identifier.

### Decisions

- Automatic payment-creation retries: reject-with-reason — the packet did not prove idempotency for
  the write path.
- Bounded read retries: accept — the narrowed operation is read-only and the plan now has a total
  deadline and cancellation rule.
- Operation identifier: accept — it gives tests and telemetry a stable correlation boundary.

### Additional ideas

- Start with one retry tier and measured telemetry before adding endpoint-specific policies.
- Add a failure-injection fixture so timeout, duplicate response, and cancellation behavior remain
  regression-testable.

### Residual risk

- Verification is `static_only`; no application test, service probe, or production evidence was run.
- The implementation must still confirm that the chosen status endpoint has no hidden write side
  effects.

### Stop basis

One loop completed. Both material findings were dispositioned, the revised plan met the bounded
acceptance criteria, and no new HIGH or MED issue appeared in the delta.
