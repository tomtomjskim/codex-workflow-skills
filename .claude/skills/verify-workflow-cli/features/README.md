# Workflow CLI verification map

This directory is the maintained source for verifying the user-facing behavior of `scripts/workflow`. Read this index before driving the CLI, then use the matching feature file as the recipe.

## Baseline preconditions

- Run from the repository root with `H=.claude/skills/verify-workflow-cli/scripts/verify_workflow_cli.py`.
- `$H doctor` prints `doctor: OK`.
- `$H launch --run-id <id> --base <dir>` created a fresh repository at `<dir>/verify-workflow-cli/<id>/repo` with `src/ui`, `src/api`, and one empty commit.
- The plan is `tests/fixtures/coordination/approved-plan.json`. It has workstreams `frontend` (owns `src/ui`) and `backend` (owns `src/api`).
- Never point `--repo-root` at this repository or another real checkout. The handoff check reads its Git status.

## Driving conventions

- Pass `--json` to every CLI command. Assert on JSON fields, not on human text.
- Treat every command as literal. Keep flags and paths unchanged.
- Successful output goes to stdout. A validation failure is a JSON object with `error.type`, `error.message`, and `fallback`, and the CLI exits nonzero. Read whichever stream holds it.
- A receipt expires five minutes after `validate-coordination` issues it. Rerun validation before a late handoff.

## Proof and skip reporting

- CLI proof is the command, stdout, stderr, and exit code, as the helper records in `<dir>/verify-workflow-cli-evidence/<id>/<step>.json`.
- Prove each rejection with its error message, not only a nonzero exit.
- Report a case the journey does not drive as unverified, with the command you would run. Do not report it as covered by a neighboring case.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with verify_workflow_cli.py`, and `Gotchas`.

## Features

- [Prepare coordination](./prepare-coordination.md) covers publishing the manifest and inventory from an approved plan.
- [Validate coordination](./validate-coordination.md) covers contract enforcement and receipt issuance.
- [Validate handoff](./validate-handoff.md) covers accepting in-scope changes and rejecting writes outside a workstream's owned paths.
