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

`--contract` is required when the validator derives a contracted route. The command obtains the checkout tree hash from Git and requires a clean base worktree. An explicit hash supplied for controlled testing must exactly match the actual `HEAD^{tree}`.

Before accepting a workstream handoff, validate its changed paths against the same current receipt:

```bash
workflow validate-handoff --repo-root <approved-root> --manifest <manifest.json> --inventory <inventory.json> --contract <contract.json> --receipt <receipt.json> --workstream-id <id> --changed-path <path> --json
```

For handoff validation, pass the authoritative `--manifest`, `--inventory`, and current optional `--contract` again. The CLI reruns coordination validation with the canonical trigger matrix and compares the complete regenerated receipt with the submitted receipt before checking owned paths. It authoritatively collects tracked and non-ignored untracked Git changes with a NUL-delimited status command. `--changed-path` values are additive declarations: the CLI validates their union with the Git paths, so omission or a declared subset cannot hide an actual change. A CLI receipt expires after five minutes; rerun coordination validation instead of handing off with stale evidence.

Each command accepts only UTF-8 JSON and emits JSON. A structured error and nonzero exit blocks parallel validation.

## Scope And Ownership

- Use unique workstream IDs and owners.
- Treat `exclusive_write_paths` as repository-relative, literal POSIX paths; do not use globs, absolute paths, or parent traversal.
- Version 1 cannot dispatch overlapping exclusive paths. Split their ownership or serialize the writers, revise the manifest, and rerun validation; an accepted receipt always has `path_overlap: false`.
- Declare dependencies and typed `consumes`/`produces` interface identifiers from the approved plan.
- Do not copy a submitted route or required reviewer set into authority-bearing artifacts; the validator derives them.
- For a contracted route, freeze the current contract before dispatch and repeat validation after any contract revision.
- Use isolated worktrees or isolated patch artifacts when parallel writers are active. If changed files cannot be attributed to one workstream, use the sequential fallback.
- Stop all writers before collecting a handoff. Git status is not a runtime write barrier, standard ignored files are outside the collection, and symlink targets outside the repository are not observable through repository status.

## Contract Schema And Evidence

Contracted routes use the complete schema-version-1 contract; the earlier minimal four-field core and empty frozen ledger are invalid. `contract_core` has exactly the manifest and inventory hashes, positive revision, parent core hash, contract and integration owners, all three derived profile booleans (`shared_interface`, `path_overlap`, and `integration_dependency`), and the three extension objects (`interface_contract`, `path_ownership`, and `integration`). Revision 1 has a null parent; later revisions require a SHA-256 parent core hash. `shared_interface` and `integration_dependency` require their corresponding active extension object to be non-empty. `path_overlap` and `path_ownership` are retained as diagnostic/reserved schema fields only: overlap blocks before a receipt and `path_ownership` cannot authorize dispatch. Extension contents remain domain-specific canonical JSON and are not interpreted beyond those conditions.

Every required dependency or interface edge has a completed current-core `handoff` and `checkpoint` ledger record, and every derived affected consumer has a completed `acknowledgement`. Ledger records add `record_type`, `subject_id`, and `status` to the hash-bound evidence fields. They bind to the current contract core and checkout tree, form the ordered hash chain, and must exactly cover the derived required record set. Handoff and checkpoint producers are limited to the source workstream owner or current integration owner; acknowledgement producers are limited to the subject workstream owner or contract owner. `unverified` has no authority or defer schema and is rejected. The reviewer registry must exactly cover the canonical `(lens, canonical_agent)` pairs from the loaded reviewer-routing artifact, with current-core completed entries and non-empty dispatch and completion evidence. A custom trigger matrix without an exactly matching routing artifact is incompatible and blocks. A frozen contracted route with missing, failed, stale, duplicate, unexpected, or unauthorized required evidence is blocked.

`integration_gate.status` remains `open` during dispatch validation. The current CLI does not define an integration-closure command or closure-receipt schema, so a submitted `closed` gate is rejected rather than trusted. Closure is a separate additive API/schema design, not an inferred operation of `validate-coordination` or `validate-handoff`.

## Receipt Lifecycle

Run validation again before each covered dispatch after the plan, manifest, inventory, contract, or checkout tree changes. Earlier receipts become stale. Handoff validation does not prove runtime write prevention; it verifies declared ownership at the handoff boundary. Runtime hooks, signed receipts, and cryptographic identity enforcement are outside this contract.
