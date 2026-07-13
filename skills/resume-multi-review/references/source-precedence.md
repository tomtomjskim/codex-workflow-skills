# Resume Source Precedence

## Purpose

Prevent a stale public portfolio copy, generated note, or role-specific draft from being mistaken for the latest authoritative resume.

## Authority Order

Use the first available source that is both authoritative and current:

1. User-designated submitted resume for the target job
2. Reviewed private master resume or reviewed job-specific variant
3. Reviewed claim-to-evidence ledger plus reviewed resume content
4. Reviewed public-safe resume variant
5. Draft public-safe resume variant
6. Generated resume notes or session summaries
7. Portfolio project descriptions
8. Inferred profile summaries

A lower source may supplement evidence but must not silently replace a higher source.

## Source Classification

Classify every material source:

| Field | Allowed values |
|---|---|
| authority | `submitted`, `master`, `variant`, `claim-bank`, `portfolio`, `generated-note`, `inferred` |
| status | `reviewed`, `approved`, `draft`, `generated`, `stale`, `unknown` |
| privacy | `private`, `protected`, `public-sanitized`, `public` |
| claim strength | `ready`, `selective`, `role-confirm`, `needs-verification`, `excluded` |

## Freshness Decision

Freshness is not only the latest commit date. Prefer the most recent reviewed resume version over a newer generated summary.

Before evaluation, state:

```yaml
source_audit:
  selected_source:
  selected_version:
  selected_updated_at:
  authority:
  status:
  privacy:
  supplemental_sources:
  rejected_sources:
  unresolved_gap:
```

## Conflict Rules

- The user's explicit statement that a version is final overrides an older repository label.
- A public sanitized copy does not override a newer private master resume.
- A project page may support a claim but cannot establish the exact submitted wording.
- A generated note cannot upgrade a claim from `role-confirm` to `ready`.
- A commit date does not prove human review.
- If two sources disagree on dates, titles, employment periods, or ownership, mark the conflict and do not rewrite that fact.

## Missing Latest Resume

When the latest full resume cannot be read:

1. Identify what was found.
2. State why it is insufficient for an exact final rewrite.
3. Run only the portions supported by evidence.
4. Produce a patch strategy rather than a fabricated full resume.
5. Mark the result `source_gap`.

## Public And Private Boundary

Public repositories may contain sanitized claims and portfolio narratives. They should not contain private evidence maps, raw operational data, internal project identifiers, confidential customer information, credentials, production endpoints, or protected interview scripts.

The review output may use private evidence to verify a public-safe sentence, but the final public resume must contain only approved disclosure-safe wording.
