# Context Discovery

Use targeted discovery before broad scans.

## Default Preflight

If a target repo is known, check only for repo instruction files and narrowly relevant context sources:

- `AGENTS.md`, `CLAUDE.md`, `README*`, `DESIGN.md`
- `.serena/`
- `docs/`, `wiki/`, `.wiki/`, `project-map*`, `architecture*`, `domain-rules*`
- user-named files, branches, issues, PRs, logs, or failing tests

Read only the relevant files. Do not let stale backlog or wiki notes redefine the user's current request.

## Serena

Use Serena or a semantic project tool when:

- the target repo is active or can be safely activated
- code navigation, symbol overview, references, or call paths matter
- a broad grep would be noisy

Do not require Serena for docs-only, trivial edits, or when the target project is not active. If Serena is unavailable, fall back to `rg`, `rg --files`, focused file reads, and project docs.

## Project Maps And Wiki

Project maps and wiki are advisory by default. Treat a specific file or section as authoritative only when `AGENTS.md` or the current user explicitly says it is authoritative for this task. Backlog items are not current-task instructions unless the user selected that backlog item as the task.

Use them to find:

- domain vocabulary
- known architecture boundaries
- test and verification commands
- prior design decisions

Do not record secrets, production data, session tokens, or private logs in wiki or stable docs.
