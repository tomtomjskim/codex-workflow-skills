# Validate handoff

A user checks one workstream's changes against the current receipt before handing off. The CLI accepts changes inside the workstream's owned paths and rejects any change outside them, whether declared or found in Git status.

## Sub-features

- `handoff-inside` accepts a declared change inside the workstream's owned paths.
- `handoff-outside-declared` rejects a declared change outside the owned paths.
- `handoff-outside-untracked` rejects an untracked file outside the owned paths even when no `--changed-path` names it.
- `handoff-stale-receipt` rejects a receipt older than five minutes.

## How to get to it (user POV)

- Run `scripts/workflow validate-handoff --repo-root <repo> --manifest <coord>/manifest.json --inventory <coord>/inventory.json --contract <coord>/contract.json --receipt <coord>/receipt.json --workstream-id frontend --changed-path <path> --json`.

## Driving it with verify_workflow_cli.py

Preconditions:

- Baseline launch is done for `<id>`, prepare and validate succeeded, and `<coord>/receipt.json` holds the receipt.

- **Inside.** The journey declares `src/ui/settings.py` for `frontend`. Evidence `handoff_inside.json` has `exit_code` 0 and `"status": "valid"`.
- **Outside, declared.** The journey declares `src/api/settings.py` for `frontend`. Evidence `handoff_outside.json` has a nonzero `exit_code` and the message `changed paths outside owned paths for frontend: src/api/settings.py`.
- **Outside, untracked.** Not driven by the journey. Create `<repo>/src/api/outside.py`, then run the handoff with no `--changed-path`. It must fail with a ValidationError. Report it as unverified until you run it.
- **Stale receipt.** Not driven by the journey. It needs a receipt older than five minutes. Report it as unverified unless you wait or use a receipt you saved earlier.

## Gotchas

- Git status is the authority. A `--changed-path` only adds to what Git reports, so an omitted declaration cannot hide a write.
- Git-ignored files are not reported, and writes through a symlink to outside the repository are not seen.
- The check reads the repository at one moment. A writer that runs during the check can change the result.
