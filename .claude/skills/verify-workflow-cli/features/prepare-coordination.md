# Prepare coordination

A user turns one approved JSON plan into `manifest.json` and `inventory.json` in a new output directory. The CLI publishes the directory as one unit and never replaces an existing path.

## Sub-features

- `prepare-publish` writes both files and prints their hashes as JSON.
- `prepare-no-replace` refuses an output directory that already exists.

## How to get to it (user POV)

- Run `scripts/workflow prepare-coordination --repo-root <repo> --plan <plan> --out-dir <new-dir> --json`.

## Driving it with verify_workflow_cli.py

Preconditions:

- Baseline launch is done for `<id>`.

- **Publish.** Run `$H journey --run-id <id> --base <dir>`. Evidence `prepare.json` has `exit_code` 0, and its stdout has `manifest_hash`, `inventory_hash`, `manifest`, and `inventory`. The journey line `PASS manifest.json and inventory.json published` confirms both files exist.
- **No replace.** Not driven by the journey. Run the prepare command a second time with the same `--out-dir`. It must exit nonzero and leave the first files unchanged. Report it as unverified until you run it.

## Gotchas

- The output directory must not exist. A leftover directory from an earlier run fails prepare for that reason, not because of the plan.
- Preparation stages in a sibling directory and renames atomically. A platform without an atomic no-replace rename returns a structured blocked error, and the right next step is sequential execution.
