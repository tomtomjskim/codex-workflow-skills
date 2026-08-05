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
  reviewer_failures: []
  evidence_scope: []
  scope_change_policy:
```

For conversation-only targets, include a compact verbatim excerpt or precise summary plus the
acceptance criteria. Do not pass unrelated conversation history.

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
  failure_class: agent_execution_stall | spawn_rpc_stall | unavailable | interrupted | other
  role:
  canonical_target:
  last_status:
  heartbeat_evidence:
  interrupt_result:
  fallback: partial_council | provisional_main_only | stop
  evaluator_runtime:
  residual_risk:
```

A receipt is execution evidence, not reviewer evidence. It cannot satisfy a required reviewer lens.
