---
name: verify-atomic-publish
description: TLA+ model check of the prepare-coordination output publish in scripts/workflow (`_publish_prepared_output`, `_atomic_publish_directory`, `_rename_directory_noreplace`). Proves that concurrent runs and an outside creator never get an existing --out-dir replaced, never see a half-written output, and never leave a staged sibling after a normal exit. Also runs the real function against each outside target shape. Use before merging any change to those functions or to the staging, fsync, or cleanup order, or when prepare-coordination output looks clobbered or incomplete.
---

# Verify atomic publish

## The promise

`scripts/workflow prepare-coordination --out-dir <dir>` publishes `manifest.json` and `inventory.json` as one unit and never replaces a path that already exists. If this breaks, a second run or another tool can silently overwrite a coordination directory that reviewers already read, or a reader can see a directory with only one file.

The model checks these invariants in every interleaving.

- **NoReplace.** Once the output path exists, its kind and owner never change.
- **ExternalPreserved.** A path that someone else created keeps its exact shape and contents.
- **Complete.** A directory published by a run always holds both files.
- **SingleWinner** and **WinnerOwns.** At most one run reports success, and the output is that run's directory.
- **NoLeftover.** A run that ends normally, by success or by error, leaves no `.<name>.*` staged sibling.

## Run it

From the repository root:

```bash
.claude/skills/verify-atomic-publish/scripts/check_atomic_publish.py
```

It needs Java 11 or later (confirmed on Java 17), Python 3.9 or later, and `tla2tools.jar` 1.7.4 at `~/.local/share/tlaplus/tla2tools-1.7.4.jar`. Pass `--jar <path>` or set `TLA2TOOLS_JAR` to use another location. To install the jar:

```bash
mkdir -p ~/.local/share/tlaplus
curl -fsSL -o ~/.local/share/tlaplus/tla2tools-1.7.4.jar https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
shasum -a 256 ~/.local/share/tlaplus/tla2tools-1.7.4.jar
```

The confirmed SHA-256 is `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, and the size is 2274532 bytes. The run takes about 5 seconds. Exit codes follow.

- `0` prints `RESULT: PASS within the stated bounds.`
- `1` prints `RESULT: FINDING.` Either the model of the current design breaks an invariant, and the TLC output is kept with its path printed, or the real code failed a conformance case, which is printed as a sentence.
- `2` prints `RESULT: CHECK TOO WEAK.` A broken variant was not caught, so repair the model before you trust a pass.
- `3` means the jar was not found.

`--keep` keeps every TLC output and trace in the printed temp directory. Read each TLA+ result, a violation trace or a passing model, with the pstack `/statechart` skill. It tells a violation as its shortest path in plain words and draws the state chart of a passing model.

The full model has too many states for one chart, so `model/` also holds chart inputs for `/statechart`: `chart-real.cfg` and `chart-check_then_rename.cfg` each run one preparer with the outside creator, and `chart-view.ts` is the view module that gives states and steps their plain-word labels. Work on copies in a scratch folder. The `Done` action exists so that a finished run is not reported as a deadlock. The view hides only `Done` steps, which never leave their state. At `2cad251` the real chart had 13 states and 14 arrows with every rule holding. The check_then_rename chart showed one extra arrow, from someone else's empty folder to this run's output, on a 7 step path that breaks NoReplace and ExternalPreserved.

## What runs

1. TLC checks `model/AtomicPublish.tla` with `model/AtomicPublish.cfg`, which sets `Variant = "real"` and three preparers. Deadlock checking is off, because every run ends in a terminal state.
2. TLC checks three broken variants. Each must violate the invariant it breaks.
   - `check_then_rename` checks `exists()` and then calls plain `rename(2)`. It must violate NoReplace.
   - `publish_then_write` renames the empty staged directory first and writes the files after. It must violate Complete.
   - `no_cleanup` skips `shutil.rmtree` in the except branch. It must violate NoLeftover.
3. Conformance runs the real `_publish_prepared_output` in a temp directory. For each of an empty directory, a directory with a file, a regular file, and a symlink to a directory, the call must raise `ValidationError("output directory must not already exist")`, leave the path byte for byte unchanged, and leave no staged sibling. A fresh path must end up with both files. Last, it replays the check_then_rename counterexample through the real function by passing a racing publisher as its `publish` argument. Another process creates an empty folder after the run sees the path free. A plain rename must replace that folder, which shows the broken variant's harm is real on this filesystem. The real `_rename_directory_noreplace` must refuse and leave the folder empty.

## How the model maps to the code

Each action is one indivisible step in `scripts/workflow`.

- `Mkdtemp` is `tempfile.mkdtemp(prefix=".<name>.", dir=parent)` and `os.chmod(..., 0o700)` in `_publish_prepared_output`. The staged directory is private and unique, so the model gives each run its own stage.
- `WriteStaged` is each `_write_staged_file` call (`open("xb")`, write, fsync). Byte-level progress inside one file is not modeled, because no other actor can see the staged directory.
- `WriteFails` is an exception from either write.
- `FsyncDir` is the `os.fsync` on the staged directory.
- `PublishNoReplace` is `_rename_directory_noreplace`, which calls `renamex_np(..., RENAME_EXCL)` (`0x00000004`) on macOS and `renameat2(..., RENAME_NOREPLACE)` (`1`) on Linux. The kernel does it as one atomic step that fails when the target exists. Failure maps to `FileExistsError` and then to `ValidationError` in `_atomic_publish_directory`.
- `Cleanup` is the `except Exception` branch that removes the staged directory.
- `Crash` is a kill at any point between two steps. A crashed run's staged sibling stays on disk by design, and NoLeftover does not cover it.
- `ExternalCreate` is any other process that creates the output path as one of the four shapes above, at any moment.

## Scope and bounds

Three preparers, one outside creator acting at most once, every interleaving, every write failure, and every crash point. That is 8675 distinct states.

It does not cover these.

- Power loss and what fsync makes durable. The model treats each step as durable once taken.
- An outside actor that renames, deletes, or swaps the staged directory or the parent while a run is in progress. The staged directory is mode 700 and has a random name, but the same user can still touch it.
- A parent that is itself a symlink, or that is removed mid-run, and the `parent.is_dir()` check.
- Platforms without `renamex_np` or `renameat2`. Those raise "atomic no-replace publish is unavailable", which the unit tests in `tests/test_workflow_cli.py` cover.
- More than three preparers. Runs that target different paths do not interact in this code, so three preparers on one path is the case that matters.
- Case-sensitive filesystems. Conformance was confirmed on macOS APFS only.

## Coverage record

- `scripts/workflow`: `_write_staged_file`, `_rename_directory_noreplace`, `_atomic_publish_directory`, and `_publish_prepared_output`.

**Sync commit: `5538237`.** The last change to `scripts/workflow` before it was `73998b1`. At this commit the model passed, all three broken variants were caught, and conformance passed. A scratch edit that changed the macOS flag in `_rename_directory_noreplace` from `0x00000004` to `0x00000000`, which makes it a plain rename, made conformance report that an outside empty directory was replaced. It also reported that publishing over a file or a symlink raised `NotADirectoryError`, and that the counterexample replay with the real rename replaced the other process's empty folder. The run ended with `RESULT: FINDING` and exit 1, with 5 failures. After the edit was reverted, the run passed again. `scripts/workflow` was unchanged when the chart inputs and the replay were added, so the sync commit did not move.

Before trusting a pass, run `git log --oneline 5538237..HEAD -- scripts/workflow`. If it lists commits, read their diffs for the four functions above and confirm the mapping in this file still holds. Move the sync commit forward only after that review and a passing run at the new commit.

## Reading a failure

- **A real bug.** A conformance sentence names a target shape the real code replaced or mishandled. Or the model of `real` breaks an invariant after you changed the model on purpose to match new code. Report it in the code's words, for example "a second run replaced an empty `--out-dir` that another process created after the first run checked". Do not weaken an invariant, drop a shape, or remove a broken variant to make it pass. Do not change product code unless the user asks.
- **A modeling error.** Suspect the model when the code changed its design on purpose, such as a new step between the writes and the rename or a different cleanup rule. Update the action that maps to the changed code, keep each action one atomic step in the code, rerun, and record the reason here.
- **CHECK TOO WEAK.** A broken variant now passes. Usually a model edit removed the step that variant depends on. Restore the variant so it breaks the same idea against the new model, then confirm it is caught.
