# Sample Resume Multi-Review

This sample demonstrates the expected behavior of `$resume-multi-review` when the available repository content is a public sanitized draft rather than a full authoritative resume.

## Request

```text
Use $resume-multi-review to assess this public resume draft for a PHP/MySQL operations-backend role. No company-specific job description is available. The private master resume cannot be read in this session.
```

## Expected Review

```yaml
resume_multi_review:
  status: source_gap
  mode: baseline
  target:
    company: not specified
    role: PHP/MySQL operations backend
    job_description_basis: role-family baseline only
  source_audit:
    selected_source: public sanitized backend resume draft
    selected_version: example-public-v1
    selected_updated_at: 2026-07-13
    authority: variant
    status: draft
    privacy: public-sanitized
    supplemental_sources:
      - public claim bank
      - sanitized project descriptions
    rejected_sources:
      - inferred private master resume
    unresolved_gap: full reviewed master resume and exact chronology are unavailable
  recruiter:
    decision: reject
    reasons:
      - The document does not show a complete employer, title, and date chronology.
      - The target identity is diluted by broad backend and full-stack wording.
      - Internal claim-governance language makes the document look unfinished.
    one_line_fix:
      source_line: "PHP 기반 업무시스템 경험을 가진 백엔드/풀스택 개발자"
      replacement_line: "PHP/MySQL 기반 운영 시스템의 유지보수·장애 대응·기능 개선 경험을 가진 백엔드 개발자"
      decision_impact: clarifies the target role and removes unnecessary role breadth
      evidence_basis: supported by the supplied public resume and claim bank
    remaining_risk: chronology and evidence depth remain unavailable
  hiring_manager:
    decision: interview
    reasons:
      - The resume shows experience with business-critical PHP/MySQL systems.
      - It signals awareness of database, integration, batch, and operational failure modes.
      - The candidate appears comfortable with gradual legacy improvement rather than rewrite-only work.
    one_line_fix:
      source_line: "기능 고도화와 운영 안정성 개선을 중심으로 업무를 수행했습니다."
      replacement_line: "상태값·권한·DB·관리자 화면·배치 작업·외부 API의 영향 범위를 확인해 기존 PHP 기능을 개선하고 장애 원인을 분석했습니다."
      decision_impact: converts an abstract strength into observable operating behavior
      evidence_basis: supported by the supplied role evidence
    remaining_risk: implementation ownership and production scope need confirmation
  future_teammate:
    decision: interview
    reasons:
      - The candidate describes impact-scope analysis before changing legacy code.
      - Documentation and verification are treated as part of implementation work.
      - The operational-domain emphasis suggests lower onboarding and communication cost.
    one_line_fix:
      source_line: "테스트 기준과 변경 이력을 관리했습니다."
      replacement_line: "요구사항과 오류를 기능·권한·DB·성능·외부 연동 기준으로 분류하고 수정 범위와 검수 항목을 문서화했습니다."
      decision_impact: shows how the candidate makes work reviewable and transferable
      evidence_basis: supported by the supplied workflow evidence
    remaining_risk: no concrete team handoff example is visible
  adjudication:
    shared_strengths:
      - relevant PHP/MySQL business-system experience
      - operational and legacy-maintenance orientation
      - evidence of structured diagnosis and verification
    shared_risks:
      - the public draft is not a complete submitted resume
      - several claims remain abstract or require role confirmation
      - chronology and responsibility boundaries are missing
    conflicts:
      - topic: document readiness versus candidate signal
        recruiter_view: reject because the submitted artifact is incomplete
        hiring_manager_view: interview because the operating-backend signal is relevant
        future_teammate_view: interview because the work style appears collaborative
        reason_for_conflict: the document contains useful technical evidence but lacks standard resume completeness
        final_editing_direction: restore chronology and confirmed responsibility before polishing specialist terminology
    prioritized_risks:
      - severity: fatal
        risk: the selected source is a public draft rather than the authoritative resume
        evidence: source audit
        editing_direction: obtain and lock the reviewed master or submitted variant
      - severity: material
        risk: responsibility and result language is too abstract
        evidence: repeated use of generic experience wording
        editing_direction: rewrite confirmed experience as context, action, and operational impact
      - severity: limited
        risk: mixed Korean and English domain terms reduce scan speed
        evidence: public draft wording
        editing_direction: retain only terms that improve role matching
    final_decision: reject
    final_reasons:
      - The current document is not a complete authoritative resume.
      - Chronology and responsibility boundaries cannot be verified.
      - A safe full rewrite is impossible without the missing source.
  rewrite:
    available: false
    basis: full authoritative resume is unavailable
    final_resume: null
    patch_plan:
      - obtain the reviewed master resume and exact target job description
      - replace the top headline with the recruiter one-line fix
      - lead experience bullets with commerce and operations work
      - keep manufacturing experience as supporting business-system evidence
      - hold role-confirm architecture claims until evidence is reviewed
  re_review:
    recruiter:
      decision: reject
      original_risk_status:
        - unresolved: chronology is still unavailable
      remaining_risk: document completeness
    hiring_manager:
      decision: interview
      original_risk_status:
        - partially_resolved: role identity is clearer, but ownership remains unverified
      remaining_risk: responsibility depth
    future_teammate:
      decision: interview
      original_risk_status:
        - partially_resolved: collaboration method is clearer, but no handoff case is shown
      remaining_risk: concrete collaboration evidence
  loop_summary:
    iterations_run: 1
    stop_reason: authoritative source gap blocks a safe full rewrite
    unsupported_claims_added: false
    residual_risks:
      - missing full resume
      - missing company-specific job description
      - unconfirmed strong claims
```

## Expected Behavior

- The skill distinguishes candidate signal from document readiness.
- It does not fabricate the missing private master resume.
- It runs three independent reviewer lenses.
- It gives one evidence-safe line replacement per reviewer.
- It stops after one cycle because further iteration would only rewrite an incomplete source.