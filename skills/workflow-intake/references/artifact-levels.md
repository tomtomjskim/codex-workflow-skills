# Artifact Levels

Use the smallest durable artifact set that matches risk.

| Level | Use when | Artifacts |
|---|---|---|
| A0 none | trivial answer, direct command, narrow read-only task | final response only |
| A1 task note | small implementation or docs change | task summary in final response |
| A2 task/test plan | multi-file or behavior-changing work | TASK and TEST_PLAN if repo convention expects files |
| A3 spec | public behavior, API, data model, UI flow, or migration planning changes | SPEC plus TEST_PLAN |
| A4 PRD/design | new product behavior, unclear user value, UI concept, or stakeholder decision | PRD, UX/SPEC, TEST_PLAN |

For AI/LLM work where output quality, prompt/model/tool/retrieval behavior, or non-deterministic answers affect acceptance, include an EVAL_PLAN when the repo convention provides one. If the AI behavior defines new product value or user-facing behavior, pair it with PRD at A4; otherwise keep it at the smallest level that safely captures evaluation criteria.

## Artifact Decision

Fill `artifact_decision` whenever the intake flow activates. Use `create_now: yes`, `no`, or `ask`.

```yaml
artifact_decision:
  planning_docs:
    recommended_level: A0|A1|A2|A3|A4
    proposed_docs: []
    create_now: yes|no|ask
    rationale:
  design_docs:
    needed: yes|no|ask
    proposed_docs: []
    create_now: yes|no|ask
    rationale:
```

Planning docs include PRD, SPEC, TASK, TEST_PLAN, EVAL_PLAN, or repo-equivalent artifacts. Design docs include UX_CONCEPT, IA, UI_SPEC, DESIGN, design SPEC, asset brief, wireframe, or repo-equivalent artifacts.

Set `create_now: yes` only when the user explicitly asked for the artifact, repo instructions require it, or prior approval already covers it. Set `create_now: ask` when the artifact would materially reduce ambiguity or risk but durable creation is not yet approved. Set `create_now: no` for A0/A1 work or when the final response or lightweight plan is enough.

For UI/product planning, separate planning docs from design docs. Example: a new multi-screen settings workflow may need PRD/SPEC/TEST_PLAN for product behavior and UX_CONCEPT/IA/UI_SPEC for design decisions. Brief both sets and ask one approval question before writing durable docs unless approval is already clear.

Do not create durable docs just because a template exists. Create docs only when the user asks, the repo requires them, or the artifact is needed to safely continue.

When a TEST_PLAN is created or implied, use `session-conduct.md` for validation level and E2E decisions. Do not force E2E for docs-only, copy-only, isolated unit-covered changes, or AI output-quality evaluation that belongs in EVAL_PLAN.
