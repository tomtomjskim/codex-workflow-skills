# Evidence Contract

Every material finding uses this shape:

```yaml
finding:
  id:
  severity: HIGH | MED | LOW
  category:
  location:
  finding_evidence:
    observed_problem:
    failure_mode:
    source:
  impact:
  proposed_fix:
  fix_risk_class:
  disposition: apply | ask | defer | reject-with-reason
  disposition_evidence:
  approval_required:
  auto_apply_blocked_reason:
  verification_required:
  verification_evidence:
    command_or_artifact:
    assertion_strength:
    result: pass | fail | not_run | blocked | static_only | partial
```

Rules:

- Finding evidence explains why the issue exists.
- Verification evidence explains why the fix worked.
- A test pass is not enough for any finding unless the executed assertion would fail for the original failure mode or actual regression. Truthiness-only assertions, smoke tests, "does not throw" checks, and unrelated coverage must be recorded as `partial` when they would not catch that failure mode.
- If the review packet requires or recommends E2E/browser validation, evidence must include the route or entrypoint, interaction steps, assertions, and artifacts or command output. Screenshot-only evidence, page-load smoke checks, or tests that miss the original failure mode are `partial`.
- If E2E/browser validation is blocked, record the exact blocker, fallback validation, and residual risk. Do not convert blocked E2E to `pass`.
- If AI/LLM output quality is the reviewed surface, evidence must reference the applicable EVAL_PLAN or explicitly record why no durable eval plan exists. Prompt/model/tool/retrieval changes need regression evidence against the relevant eval set, or `not_run`, `partial`, or `blocked` with residual risk.
- LLM-as-judge evidence must record the judge model, judge prompt or rubric version, scoring scale, and known limitations. Judge pass alone is `partial` for high-risk behavior unless calibrated with rule-based checks, human eval, or other direct evidence that would catch the reviewed failure mode.
- Keep the validation decision separate from execution evidence: `e2e_decision` says what should run; `verification_evidence.result` says what actually happened.
- HIGH findings without evidence become `needs-investigation`, not confirmed HIGH.
- HIGH findings cannot be auto-applied or auto-rejected.
- `reject-with-reason` requires counter-evidence.
- `defer` requires why it is safe to defer and when it should be revisited.

## Persistent Control Proposals

When a finding proposes persistent approval or security infrastructure such as a manifest,
evidence hash, attestation, signature, trust anchor, provider, registry, CLI, mutation API, or an
equivalent durable control plane, require evidence for all of the following before accepting the
proposal:

- the concrete protected failure and relevant threat actor
- the trust boundary the actor could cross
- the expected frequency of the failure or control event
- existing human approval, database constraint, audit, or operational controls
- why those simpler controls are insufficient
- a no-new-infrastructure operational alternative and its tradeoffs

An unkeyed hash is a fingerprint or checksum. It may identify a retrievable immutable artifact, but
it does not prove signer identity, authorization, or non-repudiation. Do not describe it as an
attestation or signature without separate authenticated signing and trust evidence.

If a reviewer finds a defect in an agent-proposed control, reassess whether that control needs to
exist before proposing recursive hardening around it. Compare removal, reuse of an existing human
or database control, and a bounded operational procedure before adding another persistent layer.
