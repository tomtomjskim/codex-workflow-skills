# Reviewer Trigger Matrix

Derive reviewer lenses from exact changed-surface tags. Resolve every lens through
the canonical registry below. `reviewer-routing.json` is authoritative; this
human-readable matrix must mirror it exactly. Do not duplicate or override these
mappings in a review packet.

## Canonical Lens Registry

| Reviewer lens | Canonical agent |
|---|---|
| `accessibility` | `accessibility-reviewer` |
| `api` | `api-reviewer` |
| `architecture` | `architect` |
| `code` | `code-reviewer` |
| `database` | `dba` |
| `performance` | `performance-reviewer` |
| `qa` | `qa-engineer` |
| `security` | `security-reviewer` |
| `test-coverage` | `test-coverage-reviewer` |
| `ux` | `ux-reviewer` |

## Changed-Surface Triggers

| Changed-surface tags | Required reviewer lenses |
|---|---|
| `auth`, `authorization`, `roles`, `tenant-boundary` | `qa`, `security` |
| `secrets`, `file-io`, `shell`, `network` | `security` |
| `data-mutation` | `qa`, `security` |
| `database`, `migration` | `database`, `qa`, `security` |
| `public-api`, `api`, `sdk`, `integration-contract` | `api`, `code` |
| `ai-llm`, `tests`, `ci` | `qa`, `test-coverage` |
| `ui`, `user-flow` | `accessibility`, `qa`, `ux` |
| `performance` | `performance` |
| `concurrency` | `code`, `performance` |
| `architecture` | `architecture`, `code` |

## Profile Triggers

| Coordination profile | Required reviewer lenses |
|---|---|
| `shared_interface` | `api` |

Do not accept packet-authored lens or agent substitutions.

Record an unmapped material surface as a routing gap and execute sequentially
until the mapping is revised and validated. Record a reason for every skipped
reviewer. Block integration when a required reviewer is unavailable unless the
user approves a defer receipt with an owner, reason, and residual risk.
