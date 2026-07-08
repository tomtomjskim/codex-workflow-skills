# Autonomy Levels For Review

| Level | Review behavior |
|---|---|
| L0 | Read-only findings only. |
| L1 | Read-only findings plus doc-draft suggestions. |
| L2 | Ask before applying any fix. |
| L3 | Auto-apply LOW only when in scope, local, reversible, testable, and outside hard-stop surfaces. |
| L4 | Iterate within approved scope, but hard stops and unresolved blockers still require approval. |

Hard-stop surfaces always require approval:

- secrets, credentials, sessions, private keys
- production, deploy, release, push, destructive commands
- auth, permissions, roles, tenant boundaries, crypto
- CI/CD, hooks, MCP/tool config, agent instructions
- database schema, migrations, seed, backfill, retention, deletion
- dependencies, lockfiles, package-manager config
- workspace root escape or symlink traversal
- disabling tests, lint, typecheck, security checks, or validation

MED fixes default to `ask`. A `med-safe-autofix` requires all of:

- local and reversible change
- no public API, auth, DB, dependency, CI/CD, or data impact
- clear test or manual verification path
- same reviewer lens can re-check the fix

A `med-safe-autofix` is categorically disallowed when the finding or proposed fix touches any hard-stop surface. For example, a MED admin-endpoint permission-check fix that edits auth or authorization logic must require user approval.
