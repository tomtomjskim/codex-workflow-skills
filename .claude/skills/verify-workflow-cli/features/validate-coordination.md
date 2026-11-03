# Validate coordination

A user checks the prepared manifest and inventory before a concurrent dispatch. A contracted route also needs the frozen contract. On success the CLI prints a receipt that names the required reviewers.

## Sub-features

- `validate-requires-contract` blocks a contracted route that has no `--contract`.
- `validate-receipt` issues a receipt with `run_id`, `cli_version` 1, and `required_sets.required_reviewers`.
- `validate-clean-tree` issues a receipt only from a clean worktree.

## How to get to it (user POV)

- Run `scripts/workflow validate-coordination --repo-root <repo> --manifest <coord>/manifest.json --inventory <coord>/inventory.json --contract <coord>/contract.json --json`.

## Driving it with verify_workflow_cli.py

Preconditions:

- Baseline launch is done for `<id>`, and prepare succeeded.

- **Contract required.** The journey runs validation without `--contract`. Evidence `validate_without_contract.json` has a nonzero `exit_code`, the message `current contract required for contracted route`, and `fallback.execution` `sequential`.
- **Receipt.** The journey writes the contract and validates again. Evidence `validate.json` has `exit_code` 0. Its stdout is the receipt, and `required_sets.required_reviewers` contains `api-reviewer` and `ux-reviewer`.
- **Clean tree.** Not driven by the journey. Add an uncommitted file to `<repo>` and rerun validation. It must refuse to issue a receipt. Report it as unverified until you run it.

## Gotchas

- The contract builder in the helper produces a valid contract for the fixture plan only. A different plan can derive other reviewers or handoffs, and the builder follows the derivation, but check the contract it wrote before you trust a failure.
- `--checkout-tree-hash`, when given, must equal the repository's actual `HEAD^{tree}`.
