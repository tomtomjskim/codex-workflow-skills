# Council Packet

Use one bounded packet per target revision. Keep it in session memory unless the user explicitly
requests a durable artifact.

```yaml
council_packet:
  target:
    kind: proposal | plan | design | decision | diff | recommendation | document
    identifier:
    revision:
    source_or_excerpt:
  objective:
  acceptance_criteria: []
  sources_of_truth: []
  constraints: []
  non_goals: []
  requested_focus: []
  detected_risks: []
  unknowns: []
  invocation:
    preset:
    mode: review | ideate | decide | refine
    depth: quick | standard | deep
    max_loops: 1 | 2 | 3
    result: feedback | redefined
    write: none | in-scope
  authority:
    main_writer: true
    reviewers: read-only
    max_reviewers: 1 | 2
    max_reviewer_attempts: 2 | 3 | 4
    reviewer_completion_ceiling_seconds: 300 | 600 | 900
  reviewer_seats:
    - seat_id:
      lens:
      required: true | false
      status: pending | in_progress | completed | failed | independence_lost
      completion_evidence:
  reviewer_attempts:
    - seat_id:
      attempt_id:
      target_id:
      role:
      lens:
      evaluator_runtime:
      context: fresh | inherited
      state: dispatch_requested | registered_started | heartbeat_observed | completed | failed | interrupted | start_timeout | completion_timeout | unavailable
      requested_at:
      registered_at:
      last_heartbeat_at:
      terminal_at:
      wait_slices:
      elapsed_seconds:
      failure_class:
      supersedes_attempt_id:
      completion_evidence:
  reviewer_failures: []
  evidence_scope: []
  scope_change_policy:
```

For conversation-only targets, include a compact verbatim excerpt or precise summary plus the
acceptance criteria. Do not pass unrelated conversation history.

The packet revision is source-bound revision evidence. Preserve an explicit revision exactly as the
user or target artifact names it. If no revision exists, assign session-local `v0`, record that it
was Council-assigned, and keep it distinct from any later refined-artifact revision. Reviewer
prompts, attempt completion evidence, delta reviews, and the final receipt must use the same source
revision unless a declared scope revision creates a new packet.

## Risk-First Target Lock

When the target proposes persistent approval or security infrastructure, lock the Council target to
the underlying risk or protected failure, not to the proposed mechanism. This applies to proposals
for manifests, evidence hashes, attestations, signatures, trust anchors, providers, registries,
CLIs, mutation APIs, and equivalent durable control planes.

Before treating such infrastructure as necessary, add these facts to the packet's `detected_risks`,
`unknowns`, or evidence scope:

- the concrete protected failure and relevant threat actor
- the trust boundary the actor could cross
- the expected frequency of the failure or control event
- existing human approval, database constraint, audit, or operational controls
- evidence that those simpler controls are insufficient

Keep the proposed mechanism as one candidate solution. Every Council comparison or refinement must
also include a viable no-new-infrastructure operational alternative, or explicitly state why none
can protect the locked risk.

Treat an unkeyed hash only as a fingerprint or checksum. It may identify a retrievable immutable
artifact, but it does not prove signer identity, authorization, or non-repudiation.

## Reviewer Prompt Contract

Give each reviewer:

- the compact packet and target revision inline
- one primary lens and optional secondary questions
- explicit non-goals
- required evidence strength
- a prohibition on writes and recursive delegation
- the output shape below

Do not make reviewers rediscover the Council contract from skill files. Do not reveal another
reviewer's conclusions before the independent pass.

```yaml
review:
  lens:
  target_revision:
  material_findings:
    - id:
      severity: HIGH | MED | LOW
      claim:
      evidence:
      failure_mode:
      impact:
      proposed_improvement:
      counter_condition:
      uncertainty: supported | likely | unknown
      verification_criteria:
  alternatives:
    - idea:
      value:
      tradeoff:
      assumption:
      infrastructure_impact: none | reuse | new
  no_material_findings:
  residual_risk:
```

Require `no_material_findings: true` when no supported finding exists. General advice is not a
finding. Treat unsupported HIGH claims as `needs-investigation`, not confirmed HIGH.

## Synthesis Contract

Deduplicate findings using `<lens>:<surface>:<failure-mode>`. Record every material item as:

```yaml
decision:
  id:
  disposition: accept | reject-with-reason | defer | ask
  evidence:
  artifact_change:
  verification_needed:
  residual_risk:
```

Only the main agent changes the artifact or workspace. A reviewer recommendation never grants write
authority.

## Failure Receipt

When a reviewer does not complete, append a receipt instead of inventing a review result:

```yaml
reviewer_failure:
  seat_id:
  attempt_id:
  failure_class: start_timeout | completion_timeout | agent_execution_stall | spawn_rpc_stall | thread_limit | unavailable | interrupted | evaluator_failure | other
  role:
  target_id:
  requested_at:
  registered_at:
  last_heartbeat_at:
  terminal_at:
  wait_slices:
  elapsed_seconds:
  last_status:
  heartbeat_evidence:
  interrupt_result:
  supersedes_attempt_id:
  fallback: partial_council | provisional_main_only | stop
  evaluator_runtime:
  residual_risk:
```

A receipt is execution evidence, not reviewer evidence. It cannot satisfy a required reviewer lens.

Use unique `attempt_id` plus `target_id` as the identity boundary. Rendered duplicate status lines
are not evidence of duplicate dispatch, completion, or interruption. Record two attempts only when
the runtime returned two distinct dispatch identities or the chair actually issued two dispatches.
Use `null` or `unknown` for unavailable targets, timestamps, or heartbeat data; never invent
provenance to make a receipt look complete.
