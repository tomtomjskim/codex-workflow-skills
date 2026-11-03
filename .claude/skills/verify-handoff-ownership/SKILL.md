---
name: verify-handoff-ownership
description: Bounded exhaustive check that `validate_handoff` in scripts/workflow_coordination/validate.py never accepts a changed path whose real filesystem location is outside the workstream's owned paths, and that adding a path never turns a rejection into an acceptance. Use before merging any change to `validate_handoff`, `_normalize_path`, or `ValidationReceipt.normalized_paths`, or when a handoff result looks wrong.
---

# Verify handoff ownership

## The promise

When `scripts/workflow validate-handoff` says `"status": "valid"`, every changed path lies inside the workstream's owned paths on disk. If this breaks, a parallel writer's edit to another workstream's files passes the gate, and the documented sequential fallback never triggers.

Two properties are checked.

- **P1, no false accept.** If `validate_handoff` accepts a list of changed paths, `os.path.realpath` of each path lies inside `os.path.realpath` of one owned path.
- **P2, no hiding.** `validate_handoff` accepts a list exactly when it accepts each path in it alone. So an extra declared path, or an extra path from Git status, can never make a rejected handoff pass.

## Run it

From the repository root:

```bash
.claude/skills/verify-handoff-ownership/scripts/check_handoff_ownership.py
```

It needs Python 3.9 or later and nothing outside the standard library. It takes about 6 seconds. Exit codes follow.

- `0` prints `RESULT: PASS within the stated bounds.`
- `1` prints `RESULT: FINDING.` with each counterexample as a sentence.
- `2` prints `RESULT: CHECK TOO WEAK.` A known-broken variant was not caught, so the check itself needs repair before you trust a pass.

`--mutant <name>` checks one broken variant alone and prints its counterexamples. Use it to see what a failure looks like.

## Scope and bounds

The script builds one temporary repository with `src/ui/a.py`, `src/ui/sub/`, `src/api/`, `src/uix/`, and `docs/`. It adds three symlinks: `src/ui/link` to `../api`, `src/ui/esc` to a directory outside the repository, and `src/ui/self` to `.`. It calls the real `validate_handoff` for each of 5 owned sets (`OWNED_SETS`), each of 30 changed paths (`ALPHABET`), and every unordered pair of those paths. That makes 2325 cases. The alphabet covers sibling prefixes, `./` and `//` spellings, trailing slashes, symlinks into and out of owned paths, case aliases, `..`, absolute and Windows paths, backslashes, glob characters, NFD and NFC names, `.`, and the empty string.

The oracle is `os.path.realpath`, not anything in validate.py.

It does not cover these.

- The union of Git status and declared paths in `handle_validate_handoff` (`scripts/workflow`, the lines that build `validated_paths`). The `verify-workflow-cli` skill drives that path end to end.
- Git status parsing in `scripts/workflow_coordination/git_changes.py`.
- The cross-workstream overlap check in `_validate_paths`.
- Lists longer than two paths, owned sets outside `OWNED_SETS`, and paths outside `ALPHABET`.
- Hard links, mount points, and files that change while the check runs.
- Case-sensitive filesystems. It was confirmed on macOS APFS, which is case-insensitive. On Linux, `SRC/ui` is a different directory, and the case-alias cases test something else.

## Coverage record

- `scripts/workflow_coordination/validate.py`: `validate_handoff` and `_normalize_path`.
- `scripts/workflow_coordination/receipts.py`: `ValidationReceipt` (only `normalized_paths` matters here).
- Mutants in `MUTANTS` replace exact text in validate.py: `normalized == owned or owned in normalized.parents` and `    return canonical`.

**Sync commit: `cea7bf3`.** At this commit the check passed, both mutants were caught, and a scratch edit to the real validate.py that removed symlink resolution (`resolved = root / normalized`) produced 96 P1 violations.

Before trusting a pass, run `git log --oneline cea7bf3..HEAD -- scripts/workflow_coordination/validate.py scripts/workflow_coordination/receipts.py`. If it lists commits, read their diffs and confirm the check still describes the code. Move the sync commit forward in this file only after that review and a passing run at the new commit.

## Reading a failure

- **A real bug.** The counterexample names an owned set and a changed path, and the file really lives outside it. Confirm with `realpath` on the path shown. Report it in those words. Do not narrow `ALPHABET` or `OWNED_SETS` to make it pass, and do not change product code unless the user asks.
- **A modeling error.** Suspect the check when the intended behavior changed on purpose. Two examples follow. A deliberate policy such as "an owned symlink owns its target" changes what P1 should mean. A new `ValidationReceipt` field makes `receipt_for` fail to construct. Update the script, rerun, and record the reason in this file.
- **`mutant ... no longer applies`.** validate.py changed the text a mutant replaces. Rewrite that mutant against the new code so it still breaks the same idea, then confirm it is caught.

## Observed behavior worth knowing

These are not violations.

- With owned `src/ui/link`, a symlink to `src/api`, the code accepts `src/api/a.py`. Ownership follows the symlink target. The overlap check in `_validate_paths` is what keeps two workstreams from owning the same real directory, and this check does not cover it.
- With owned `src/ui`, the code rejects a change to the symlink entry `src/ui/link` itself, because it resolves to `src/api`. That is a fail-closed reject. A workstream cannot hand off an edit to a symlink that points outside its paths.
