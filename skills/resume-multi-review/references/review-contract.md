# Resume Multi-Review Contract

## Purpose

Keep recruiter, hiring-manager, and future-teammate reviews independent, comparable, and reusable across iterations.

## Reviewer Boundaries

### Recruiter

Evaluate whether the document can survive initial screening.

- target-role clarity in the first screen
- chronology, employers, titles, and career continuity
- job-description keyword coverage without keyword stuffing
- readability, document length, and information hierarchy
- credibility, consistency, and verification risk
- whether there is a clear reason to recommend the candidate to the hiring team

### Hiring Manager

Evaluate whether the candidate appears capable of doing the work and reducing delivery risk.

- direct evidence for the job's core responsibilities
- problem diagnosis, implementation scope, and operational judgment
- ownership level that is supported by evidence
- technical depth appropriate to career level
- legacy, production, data, integration, and incident-handling capability
- ability to reduce technical debt and operational risk

### Future Teammate

Evaluate the likely day-to-day collaboration cost and engineering contribution.

- collaboration and communication signals
- maintainability, reviewability, and handoff discipline
- troubleshooting and impact-scope analysis
- documentation, test, deployment, and operational awareness
- whether technical terms are explained through actual work context
- whether the candidate is likely to reduce or increase team burden

Reviewers must not copy one another's decision or reasons.

## Decision Rules

Each reviewer must return exactly one decision:

- `interview`
- `reject`

Each reviewer must provide exactly three reasons supporting that decision.

Do not return `hold`, `maybe`, `conditional`, or a percentage score as a substitute for the binary decision. Conditions may be recorded under remaining risks after the decision.

## One-Line Fix Rule

Each reviewer selects one existing line with the highest decision impact.

Required fields:

```yaml
one_line_fix:
  source_line:
  replacement_line:
  decision_impact:
  evidence_basis:
```

Rules:

- Replace one line, not an entire section disguised as a line.
- Do not insert multiple bullets separated by semicolons.
- Keep the replacement supportable by the locked resume and evidence packet.
- Prefer role identity, responsibility, problem-solving evidence, or credibility risk over cosmetic wording.
- If no source line can safely be strengthened, make the line more precise rather than more impressive.

## Conflict Adjudication

The adjudicator must distinguish document risk from candidate risk.

Example: a recruiter may reject because chronology is absent while a hiring manager may want an interview based on technical evidence. This means the candidate signal may be viable while the current document is not.

Classify risks:

- `fatal`: likely to cause rejection by itself or creates a serious credibility problem
- `material`: meaningfully reduces competitiveness but is fixable
- `limited`: useful polish with low direct effect on the decision

For each conflict, record:

```yaml
conflict:
  topic:
  recruiter_view:
  hiring_manager_view:
  future_teammate_view:
  reason_for_conflict:
  final_editing_direction:
```

## Rewrite Rules

A full rewrite is allowed only when the full authoritative resume is available.

The rewritten resume must:

1. preserve names, employers, dates, titles, technologies, and scope unless evidence supports correction;
2. lead with the target role and the strongest relevant operating domain;
3. structure experience as problem or context -> role and action -> result or operational impact;
4. separate confirmed work from architecture direction, study, or future plans;
5. keep AI-assisted work as a bounded development method unless the target job makes it a primary competency;
6. remove internal editing notes, claim-policy notes, redaction instructions, and reviewer commentary from the submitted document;
7. compress low-relevance experience rather than deleting chronology;
8. avoid unsupported metrics and inflated verbs.

When the full authoritative resume is missing, return a patch plan and revised sections only. Do not label them as a complete final resume.

## Re-Review Rules

After rewriting, each reviewer must independently evaluate the new version again.

For every original rejection reason, record one status:

- `resolved`
- `partially_resolved`
- `unresolved`
- `not_applicable`

Do not claim improvement merely because wording changed. Cite the replacement sentence or section that addresses the original risk.

## Exact Output Contract

```yaml
resume_multi_review:
  status: complete | source_gap | incomplete
  mode: job_specific | baseline
  target:
    company:
    role:
    job_description_basis:
  source_audit:
    selected_source:
    selected_version:
    selected_updated_at:
    authority:
    status:
    privacy:
    supplemental_sources: []
    rejected_sources: []
    unresolved_gap:
  recruiter:
    decision: interview | reject
    reasons:
      -
      -
      -
    one_line_fix:
      source_line:
      replacement_line:
      decision_impact:
      evidence_basis:
    remaining_risk:
  hiring_manager:
    decision: interview | reject
    reasons:
      -
      -
      -
    one_line_fix:
      source_line:
      replacement_line:
      decision_impact:
      evidence_basis:
    remaining_risk:
  future_teammate:
    decision: interview | reject
    reasons:
      -
      -
      -
    one_line_fix:
      source_line:
      replacement_line:
      decision_impact:
      evidence_basis:
    remaining_risk:
  adjudication:
    shared_strengths:
      -
      -
      -
    shared_risks:
      -
      -
      -
    conflicts: []
    prioritized_risks:
      - severity: fatal | material | limited
        risk:
        evidence:
        editing_direction:
    final_decision: interview | reject
    final_reasons:
      -
      -
      -
  rewrite:
    available: true | false
    basis:
    final_resume:
    patch_plan:
  re_review:
    recruiter:
      decision: interview | reject
      original_risk_status: []
      remaining_risk:
    hiring_manager:
      decision: interview | reject
      original_risk_status: []
      remaining_risk:
    future_teammate:
      decision: interview | reject
      original_risk_status: []
      remaining_risk:
  loop_summary:
    iterations_run:
    stop_reason:
    unsupported_claims_added: false
    residual_risks: []
```

## Completion Gate

Return `complete` only when:

- the selected resume source and version are explicit;
- all three reviewers made independent binary decisions with exactly three reasons;
- each one-line fix is evidence-safe;
- conflicts and prioritized risks are recorded;
- a full rewrite is included only when the full source was available;
- re-review compares the new text against original risks;
- unsupported claims were not added;
- residual risks are explicit.

Otherwise return `source_gap` or `incomplete` with the exact blocker.