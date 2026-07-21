# Reviewer Trigger Matrix

Select reviewer agents from exact changed-surface tags. Do not run every reviewer by default, and do not substitute display labels or locally invented role names for canonical agent names.

| Changed-surface tag | Canonical reviewer agents |
|---|---|
| `auth`, `authorization`, `roles`, `tenant-boundary`, `secrets`, `file-io`, `shell`, `network` | `security-reviewer` |
| `data-mutation` | `security-reviewer`, `qa-engineer` |
| `database`, `migration` | `security-reviewer`, `qa-engineer`, `dba` |
| `public-api`, `api`, `sdk`, `integration-contract` | `code-reviewer`, `api-reviewer` |
| `ai-llm`, `tests`, `ci` | `qa-engineer`, `test-coverage-reviewer` |
| `ui`, `user-flow` | `ux-reviewer`, `accessibility-reviewer`, `qa-engineer` |
| `performance` | `performance-reviewer` |
| `concurrency` | `performance-reviewer`, `code-reviewer` |
| `architecture` | `architect`, `code-reviewer` |

The derived `shared_interface` profile additionally requires `api-reviewer`. The CLI's canonical matrix is authoritative for coordination receipts; submitted manifests and contracts cannot replace or narrow it.

For a material domain not represented by an exact tag, record the gap and use sequential execution until the plan is revised and validated. For skipped reviewers, record the reason. A required reviewer that is unavailable blocks integration unless the user approves a defer with an owner, reason, and residual risk.
