---
name: verify-workflow-cli
description: Drive this repository's `scripts/workflow` coordination CLI (prepare-coordination, validate-coordination, validate-handoff) the way a user does, in a disposable Git repository, and capture command, stdout, stderr, and exit code as proof. Use before claiming a change to `scripts/workflow` or `scripts/workflow_coordination/` works, or when a coordination receipt or handoff result looks wrong.
---

# Verify the workflow CLI

The user-facing surface is the `scripts/workflow` CLI documented in `README.md` under "Validated Coordination CLI". Skills in `skills/` are read by agents, not driven, so this skill does not cover them. Use `scripts/validate_repo.sh` and the unit tests for skill text.

All commands below run from the repository root. The helper is `.claude/skills/verify-workflow-cli/scripts/verify_workflow_cli.py`, called `H` below.

## Launch

The CLI has no server and no build step. Launch creates a fresh Git repository with `src/ui` and `src/api` and one empty commit, which is the state the fixture plan `tests/fixtures/coordination/approved-plan.json` expects.

```bash
H=.claude/skills/verify-workflow-cli/scripts/verify_workflow_cli.py
$H launch --run-id <id> --base <dir>
```

Ready means the line `launch: ready run=<dir>/verify-workflow-cli/<id> repo=...` printed and exit code is 0. `--base` defaults to `$VERIFY_WORKFLOW_BASE` or the system temp dir. In Claude Code, pass the session scratchpad. Each `--run-id` gets its own directory, so runs can go side by side. Launch refuses an existing run directory.

## Doctor

```bash
$H doctor
```

It checks that `scripts/workflow` is executable, the fixture plan exists, and `scripts/workflow --version` succeeds. It prints the CLI version, repository HEAD, and Python version. It writes nothing. Expect `doctor: OK`. Run it first whenever a result looks off. The system `python3` on this host is 3.9, and the CLI must stay compatible with it.

## Drive

`$H journey --run-id <id> --base <dir>` runs launch, every step below, and cleanup in one go, printing one `PASS` or `FAIL` line per check and `journey: OK` at the end. Exit code 0 means every check passed. Use it as the default proof.

The steps it drives, in user order:

1. `scripts/workflow prepare-coordination --repo-root <repo> --plan <plan> --out-dir <coord> --json` publishes `manifest.json` and `inventory.json`.
2. `scripts/workflow validate-coordination` without `--contract` must fail with `current contract required for contracted route` and fallback `sequential`.
3. The helper writes `contract.json` the way an architect would, frozen ledger and completed reviewer registry included. This step mirrors `tests/test_workflow_cli.py::_write_contract`.
4. `validate-coordination` with `--contract` exits 0 and its stdout is the receipt. The receipt must list `api-reviewer` and `ux-reviewer`.
5. `validate-handoff --workstream-id frontend --changed-path src/ui/settings.py` exits 0 with `"status": "valid"`.
6. `validate-handoff --workstream-id frontend --changed-path src/api/settings.py` exits nonzero with `changed paths outside owned paths for frontend`.

Per-feature recipes and the cases the journey does not cover live in `features/`.

## Evidence

Every CLI call writes `<base>/verify-workflow-cli-evidence/<id>/<step>.json` with `step`, `command`, `exit_code`, `stdout`, and `stderr`. The contract used is copied to `contract.json` there. A proof cites the step file and quotes the exit code and the field it asserts. Proof standards follow.

- Drive the real `scripts/workflow` entry point, not imported functions. The contract builder is the only import, and it stands in for the human architect, not the CLI.
- Prove rejection paths as well as the happy path. A handoff check that accepts everything passes step 5 too.
- Read the error message in the evidence file. A nonzero exit for the wrong reason is not proof.

## Cleanup

```bash
$H cleanup --run-id <id> --base <dir>
```

It removes only `<base>/verify-workflow-cli/<id>`, and only if it holds the `.verify-workflow-cli-run` marker that launch wrote. Evidence under `verify-workflow-cli-evidence/<id>` survives. `journey` calls cleanup itself, even after a failed check. No processes are started, so nothing needs killing.

## Helpers

`scripts/verify_workflow_cli.py` is executable and takes `doctor`, `launch`, `journey`, or `cleanup`, plus `--run-id` and `--base`. It needs only the standard library and this repository.
