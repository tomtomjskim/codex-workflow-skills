# Parallel Coordination

Use this contract only when two or more workstreams may execute concurrently. It does not authorize concurrency by itself.

## Dispatch Gate

Before every covered dispatch, resolve the installed `workflow` entrypoint from this skill or plugin and require CLI version 1. Run `prepare-coordination` from the approved plan, then run `validate-coordination` against the approved repository root. The validator supplies the canonical reviewer matrix; a plan, manifest, contract, or caller cannot replace it.

A covered dispatch requires exit code 0 and a current validation receipt. Current means that the receipt matches the active manifest, inventory, contract core when required, and checkout tree hash. Keep the returned `run_id`, derived route and profiles, required sets, and normalized paths with the session plan.

Do not dispatch concurrently when the entrypoint is missing, its CLI/schema version is incompatible, validation fails, the receipt is stale, or workstream attribution/isolation cannot be established. Record this exact fallback decision and assign one owner to execute the workstreams in dependency order:

```json
{"parallel_validation": "blocked", "execution": "sequential"}
```

This is a single-owner sequential fallback. User wording such as “parallel,” “full auto,” or “use agents” does not bypass the gate.

## Commands

Prepare artifacts together from one approved canonical JSON plan:

```bash
workflow prepare-coordination --repo-root <approved-root> --plan <plan.json> --out-dir <temporary-dir> --json
```

The output directory must not exist before preparation. The CLI encodes both canonical payloads first, writes and synchronizes them in one private sibling staging directory, then publishes that directory as a unit with an atomic no-replace rename. Existing empty/nonempty directories, files, symlinks, and concurrently created targets are never deleted or replaced; only the CLI-created staging path is cleaned after failure. If atomic no-replace publish is unavailable, preparation is blocked with a structured error. Do not use an unsafe rename fallback; record blocked parallel validation and use single-owner sequential execution.

Validate the prepared artifacts and receive the dispatch receipt:

```bash
workflow validate-coordination --repo-root <approved-root> --manifest <manifest.json> --inventory <inventory.json> --contract <contract.json> --json
```

`--contract` is required when the validator derives a contracted route. The command obtains the checkout tree hash from Git unless an explicit hash is supplied for controlled testing.

Before accepting a workstream handoff, validate its changed paths against the same current receipt:

```bash
workflow validate-handoff --repo-root <approved-root> --manifest <manifest.json> --inventory <inventory.json> --contract <contract.json> --receipt <receipt.json> --workstream-id <id> --changed-path <path> --json
```

For handoff validation, pass the authoritative `--manifest`, `--inventory`, and current optional `--contract` again. The CLI reruns coordination validation with the canonical trigger matrix and compares the complete regenerated receipt with the submitted receipt before checking owned paths. A CLI receipt expires after five minutes; rerun coordination validation instead of handing off with stale evidence.

Each command accepts only UTF-8 JSON and emits JSON. A structured error and nonzero exit blocks parallel validation.

## Scope And Ownership

- Use unique workstream IDs and owners.
- Treat `exclusive_write_paths` as repository-relative, literal POSIX paths; do not use globs, absolute paths, or parent traversal.
- Declare dependencies and typed `consumes`/`produces` interface identifiers from the approved plan.
- Do not copy a submitted route or required reviewer set into authority-bearing artifacts; the validator derives them.
- For a contracted route, freeze the current contract before dispatch and repeat validation after any contract revision.
- Use isolated worktrees or isolated patch artifacts when parallel writers are active. If changed files cannot be attributed to one workstream, use the sequential fallback.

## Receipt Lifecycle

Run validation again before each covered dispatch after the plan, manifest, inventory, contract, or checkout tree changes. Earlier receipts become stale. Handoff validation does not prove runtime write prevention; it verifies declared ownership at the handoff boundary. Runtime hooks, signed receipts, and cryptographic identity enforcement are outside this contract.
