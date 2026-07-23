# Harness Experiment Task 6 Binding Contract

**Status:** Normative clarification for Tasks 6 and 7 of
`2026-07-23-harness-experiment-readiness-plan.md`.

This contract closes the source-authority, local-config, object-layout, Git
process, and receipt-timing gaps found by the pre-implementation security and
architecture reviews. If the implementation plan conflicts with this file,
this file takes precedence.

## 1. Scope and selected approach

Task 6 verifies one operator-attested local Git source without reading a
working tree and returns an immutable `PreparedTaskSource`. It does not load
commit or blob content, materialize a target, or issue a receipt.

Three approaches were considered:

1. trust ordinary Git discovery and configuration;
2. accept only a closed, source-shaped local-repository profile and fail
   closed on unknown controls or layouts;
3. implement a general hostile-repository sandbox.

Phase A selects approach 2. Approach 1 leaves ambient configuration and
object indirection authoritative. Approach 3 requires a containment backend
that Phase A explicitly does not implement.

Machine verification covers the physical path, ownership and mode, supported
control files and configuration, object-database topology, bounded Git
process policy, and equality of captured seals. The following remain
operator-attested assumptions:

- the repository was provisioned as an operator-owned trusted local clone
  using a remote source, no local source, or a local clone mode that cannot
  share hardlinks;
- the source is not on a network, FUSE, or otherwise externally mutable
  mount;
- another process running as the same OS user does not replace data and
  restore the original metadata between the two transaction seals.

Reports must not describe those three properties as machine verified.

## 2. Supported platform and physical path

Task snapshots support CPython 3.9 or later on POSIX Darwin and Linux only.
Before source access, require:

- `os.getuid`, `st_uid`, `st_dev`, `st_ino`, `st_mtime_ns`, and
  `st_ctime_ns`;
- `os.killpg`, `os.getpgid`, `start_new_session`, `SIGTERM`, and `SIGKILL`;
- `os.scandir`, `dir_fd`, `O_NOFOLLOW`, and `O_CLOEXEC`.

Absence of any required facility is `task_source_platform_unsupported`.

`TaskSourceSpec.repository_root` is accepted only when its original
`os.fspath()` value:

- has exact type `str`;
- is absolute;
- equals `os.path.normpath(value)`;
- equals `os.path.realpath(value)`;
- names an existing directory.

Do not repair input with `absolute()` or `resolve()`. Starting at the
filesystem anchor, `lstat()` every component through the repository root and
reject every symbolic link. On macOS, tests created below `/var` pass the
physical `/private/var/...` result explicitly.

The repository root, its in-tree `.git` directory, `.git/objects`, every
walked object directory, and every accepted control file must:

- have `st_uid == os.getuid()`;
- have `st_mode & 0o022 == 0`;
- share the repository root's `st_dev`.

Directories must be plain directories. Files must be regular files with
`st_nlink == 1`. A `.git` gitfile, linked worktree, external Git directory,
or external object directory is unsupported.

## 3. Exact public surface

The Task 6 public surface is:

```python
class TaskSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSourceSpec:
    input_digest: str
    task_id: str
    repository_root: Path = field(repr=False)
    commit_oid: str
    provisioning_class: str
    operator_attested: bool
    local_clone_policy: str


@dataclass(frozen=True)
class TaskSnapshotPolicy:
    policy_version: str = "task-object-materializer-v1"
    git_timeout_seconds: int = 15
    git_termination_grace_milliseconds: int = 250
    max_git_stdout_bytes: int = 16 * 1024 * 1024
    max_git_stderr_bytes: int = 64 * 1024
    max_config_bytes: int = 256 * 1024
    max_packed_refs_bytes: int = 4 * 1024 * 1024
    max_object_entries: int = 200000
    max_object_store_bytes: int = 2 * 1024 * 1024 * 1024
    max_object_depth: int = 3
    max_component_bytes: int = 255
    max_relative_path_bytes: int = 4096
    max_files: int = 10000
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class ObjectTopologySeal:
    object_topology_digest: str
    entry_count: int
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class PreparedTaskSource:
    source: TaskSourceSpec = field(repr=False)
    policy: TaskSnapshotPolicy = field(repr=False)
    git_dir: Path = field(repr=False)
    object_format: str
    source_identity_digest: str
    local_config_digest: str
    object_topology: ObjectTopologySeal
    git_process_policy_digest: str


def prepare_task_source(
    source: TaskSourceSpec,
    policy: TaskSnapshotPolicy,
) -> PreparedTaskSource:
    ...
```

Exact scalar rules are:

- `input_digest` is `sha256:` plus 64 lowercase hexadecimal digits;
- `task_id` follows the Task 1 task-ID contract;
- `commit_oid` is exactly 40 or 64 lowercase hexadecimal digits;
- `provisioning_class` is
  `operator_owned_trusted_git_local_clone`;
- `operator_attested` has exact type `bool` and is `True`;
- `local_clone_policy` is
  `remote_or_no_local_or_no_hardlinks`;
- every policy integer has exact type `int`, is positive, and is no greater
  than its displayed default hard ceiling;
- `max_git_stderr_bytes <= max_git_stdout_bytes`,
  `max_file_bytes <= max_total_bytes`, and all path limits are internally
  consistent.

Task 7 must reject a forged or mutated prepared value by revalidating the
exact type, fields, nested values, policy, and fresh source seals.

Public failures contain one fixed message and no path, config value, stdout,
stderr, exception text, or exception context:

```text
task_source_spec_invalid
task_snapshot_policy_invalid
task_source_platform_unsupported
task_source_root_invalid
task_source_git_dir_invalid
task_source_control_invalid
task_source_config_invalid
task_source_topology_invalid
task_source_changed
task_git_operation_invalid
task_git_spawn_failed
task_git_output_limit
task_git_timeout
task_git_failed
```

## 4. Control-file authority

The required controls are `.git/config` and `.git/objects`. The config file
is opened with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC`, then `lstat()` and
`fstat()` identities are compared before and after a bounded read. At most
`max_config_bytes` bytes are read; the limit is inclusive and one byte over
fails. Its exact bytes contribute a `sha256:` digest.

Reject the presence of:

- `.git/commondir` or `.git/config.worktree`;
- any entry in `.git/worktrees` or `.git/modules`;
- `.git/refs/replace` or a `refs/replace/` record in `.git/packed-refs`;
- `.git/objects/info/alternates` or `http-alternates`;
- any `.promisor` object-store file.

When `.git/packed-refs` exists, apply the same no-follow identity rules,
bounded read it through `max_packed_refs_bytes`, reject malformed bytes or a
replace ref, and bind its exact digest and identity. Absence is bound as the
literal state `absent`.

An ordinary `.git/hooks` directory containing only regular, single-link,
non-writable `*.sample` files is allowed but never opened or executed. Any
non-sample hook is rejected. The process policy disables hooks independently.

## 5. Exact local-config policy

Run exactly this operation after the config descriptor identity is verified:

```text
git <fixed-global-prefix>
  config --file=<verified-config> --no-includes --null --list
```

The stdout is non-empty, ends in NUL, and consists of non-empty NUL-delimited
records. Split each record at its first LF into key and value. Require exactly
one LF, valid UTF-8, NFC, no remaining control character, an ASCII config key,
and no duplicate key. Sort semantic records by `(key UTF-8 bytes, value UTF-8
bytes)` for the semantic config digest. Bind both the exact raw-config digest
and semantic digest into `local_config_digest`; retain neither raw bytes nor
values.

The closed allowlist is:

- `core.repositoryformatversion`;
- `core.filemode`;
- `core.bare`;
- `core.logallrefupdates`;
- `core.ignorecase`;
- `core.precomposeunicode`;
- `extensions.objectformat`;
- `remote.<name>.url`;
- `remote.<name>.fetch`;
- `branch.<name>.remote`;
- `branch.<name>.merge`.

Names match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. Boolean values are lowercase
`true` or `false`. Require `core.bare=false`. A remote fetch value is exactly
`+refs/heads/*:refs/remotes/<same-name>/*`. A branch remote is an allowed
remote name or `.`, and a branch merge is `refs/heads/<validated-name>`.
Remote URLs are non-empty NFC values but are never retained or reported.

For SHA-1 require `core.repositoryformatversion=0` and no
`extensions.objectformat`. For SHA-256 require
`core.repositoryformatversion=1` and
`extensions.objectformat=sha256`. The expected format is selected from the
validated commit-OID length and must equal
`rev-parse --show-object-format=storage`.

Every unknown key fails. Explicitly forbidden families include:

- `include.*`, `includeif.*`, and unapproved `extensions.*`;
- `core.worktree`, `core.fsmonitor`, `core.hookspath`,
  `core.attributesfile`, `core.excludesfile`, and
  `core.alternaterefscommand`;
- `filter.*`, `submodule.*`, `credential.*`, and `url.*`;
- `remote.*.promisor`, `remote.*.partialclonefilter`, and every partial-clone
  or lazy-fetch control.

`--no-includes` must ensure that an include target is not opened before the
include key itself is rejected.

## 6. Object-database grammar and seal

Walk `.git/objects` iteratively with `os.scandir()` and never follow links.
The root record `.` has depth zero and is included in
`max_object_entries`. The cap counts every directory and file record.
`file_count` counts files only, and `total_bytes` sums file sizes only. All
caps are inclusive; cap plus one fails.

Each component is NFC, within `max_component_bytes` UTF-8 bytes, and unique
under exact, NFC, and casefold keys in its directory. Each relative path is
within `max_relative_path_bytes`; depth cannot exceed `max_object_depth`.

For an object format with hexadecimal length `H` (40 or 64), the only allowed
layout is:

```text
objects/
  <two-lowercase-hex>/<H-2 lowercase hex>
  info/packs
  info/commit-graph
  info/commit-graphs/commit-graph-chain
  info/commit-graphs/graph-<H lowercase hex>.graph
  pack/pack-<H lowercase hex>.pack
  pack/pack-<H lowercase hex>.idx
  pack/pack-<H lowercase hex>.rev
  pack/pack-<H lowercase hex>.bitmap
  pack/pack-<H lowercase hex>.mtimes
  pack/pack-<H lowercase hex>.keep
  pack/multi-pack-index
  pack/multi-pack-index-<H lowercase hex>.bitmap
```

Only the directories implied by this grammar are accepted. Every
`pack-<oid>` requires exactly one `.pack` and one `.idx`; sidecars require
that pair. Temporary, lock, unknown, uppercase, aliased, alternate, and
promisor names fail closed. Empty allowed directories are valid.

Canonical topology records have exactly:

```text
path, kind, mode, dev, ino, uid, gid, nlink, size, mtime_ns, ctime_ns
```

Use repository-relative paths only, sort records by path UTF-8 bytes, and
digest the canonical document. The absolute root and object contents do not
enter this document.

## 7. Bounded Git process policy

The global executable ban has one Phase A exception: the literal executable
name `git` inside `task_snapshot.py`'s fixed adapter. It may not resolve,
probe, or invoke Codex, a model runner, authentication, a validator, a shell,
or another executable.

Every argv begins exactly:

```text
git
-c core.fsmonitor=false
-c core.attributesFile=/dev/null
-c core.excludesFile=/dev/null
-c core.hooksPath=/dev/null
-c submodule.recurse=false
--git-dir=<verified-git-dir>
```

Only these operation tails are permitted:

```text
config --file=<verified-config> --no-includes --null --list
rev-parse --show-object-format=storage
rev-parse --verify --end-of-options <full-oid>^{commit}
rev-parse --verify --end-of-options <full-oid>^{tree}
ls-tree -r -l -z --full-tree <full-tree-oid>
cat-file blob <full-blob-oid>
```

Task 6 calls only the first two. Task 7 may call the remaining four with
already validated in-memory full OIDs. No repository discovery, ref name,
pathspec, abbreviated OID, extra option, or caller-selected argv is accepted.

Replace the environment rather than extending it:

```python
{
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": os.defpath,
}
```

Use `cwd="/"`, `shell=False`, `stdin=DEVNULL`, stdout and stderr pipes,
`close_fds=True`, and `start_new_session=True`. Drain both pipes concurrently
with bounded reads. A stream of exactly its cap succeeds; reading one byte
beyond a cap terminates the process and fails.

Use one monotonic deadline. On timeout, cap failure, or an internal drain
failure, send `SIGTERM` to the process group, wait at most the fixed grace
period, send `SIGKILL` when needed, reap the process, and close every pipe.
Nonzero exit is `task_git_failed`. Spawn, output, timeout, and operation
failures use their fixed codes and discard raw stderr.

`git_process_policy_digest` hashes a canonical
`task-git-process-policy-v1` document containing the exact prefix, operation
templates, environment, cwd policy, timeout, grace, caps, termination rules,
and placeholders `<verified-git-dir>`, `<verified-config>`, and
`<validated-full-oid>`. It never hashes or retains an actual path.

## 8. Preparation and sole receipt issuance

`prepare_task_source()` performs, in order:

```text
validate source and policy without source access
capture filesystem/config/object seals F0/C0/O0
run bounded config and object-format metadata operations
capture filesystem/config/object seals F1/C1/O1
require F0 == F1, C0 == C1, and O0 == O1
return PreparedTaskSource
```

It performs no `rev-parse --verify`, `ls-tree`, or `cat-file`, and imports no
receipt constructor.

Task 7 capture is a separate transaction. It revalidates the prepared value,
captures fresh F0/C0/O0, performs all fixed-OID object reads, captures
F1/C1/O1 once after the final object command, and requires all three pairs to
match. It does not rescan the object database around each blob.

Only after those equalities hold does Task 7 issue exactly one
`task_source_trust` receipt using the existing Task 4 payload. The source
identity fields bind the full filesystem/config/control seal, the topology
fields bind the object seal, and the process-policy field binds the path-free
policy document. Every failure path issues no source-trust receipt.

## 9. Required TDD and security checkpoint

Use table-driven tests for:

- valid SHA-1 and SHA-256 temporary repositories;
- source scalar, platform, ancestor symlink, gitfile, ownership, permission,
  and cross-device rejection before Git spawn;
- config/control symlink, hardlink, FIFO, include, hook, fsmonitor, filter,
  submodule, replace, alternate, promisor, partial-clone, duplicate, malformed
  record, cap, and format-pairing attacks;
- every allowed object family plus unknown, alias, symlink, hardlink, special
  file, pairing, depth, component, path, entry, and byte boundary attacks;
- exact Git argv/environment, forbidden operations, simultaneous pipe
  saturation, cap and cap-plus-one, timeout, process-group termination, reap,
  pipe close, and sanitized errors;
- immutable path-private preparation, operation ordering, seal mutation,
  absence of object reads, and absence of receipts.

The Task 6 checkpoint is:

```bash
python3 -m unittest tests.test_live_eval_task_snapshot -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_live_eval_checkout tests.test_live_eval_harness -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
python3 -m py_compile scripts/live_eval/task_snapshot.py
git diff --check
```

An independent security/isolation reviewer must inspect the implemented
adapter, sentinel non-access, limits, termination, receipt absence, and
path-free public failures before Task 6 is accepted.
