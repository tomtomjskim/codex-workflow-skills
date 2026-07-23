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
- `os.killpg`, `os.getpgid`, `SIGTERM`, and `SIGKILL`;
- `O_DIRECTORY`, `O_NOFOLLOW`, and `O_CLOEXEC`;
- `os.open` and `os.stat` membership in `os.supports_dir_fd`, and
  `os.scandir` membership in `os.supports_fd`;
- descriptor-based `os.scandir(fd)` and a `subprocess.Popen` signature that
  supports the POSIX
  `subprocess.Popen(..., start_new_session=True)` contract.

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
reject every symbolic link. Every ancestor is a plain directory, has
`st_uid` equal to zero or `os.getuid()`, and normally has
`st_mode & 0o022 == 0`. The sole writable-ancestor exception is a root-owned
sticky directory whose immediate child on the accepted path is
current-user-owned and has `st_mode & 0o022 == 0`; this permits a private
`TemporaryDirectory` below `/tmp` without trusting another user's entry.
Capture the ordered, name-free ancestor identity records in each filesystem
seal. On macOS, tests created below `/var` pass the physical
`/private/var/...` result explicitly.

The repository root, its in-tree `.git` directory, `.git/objects`, every
walked object directory, every walked object file, every traversed
administration directory, and every accepted control file must:

- have `st_uid == os.getuid()`;
- have `st_mode & 0o022 == 0`;
- share the repository root's `st_dev`.

Directories must be plain directories. Files must be regular files with
`st_nlink == 1`. A `.git` gitfile, linked worktree, external Git directory,
or external object directory is unsupported.

After validating the anchor lexically, all descent is descriptor relative.
Open a child directory from its already-open parent with
`os.open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC,
dir_fd=parent_fd)`, compare `fstat()` with the no-follow metadata observation,
and pass the verified child descriptor to `os.scandir()`. Apply the same
parent-descriptor rule to control-file opens. Recheck each directory identity
after scanning and close every descriptor on every path. A check-then-use
sequence that later reopens a child by assembled pathname is forbidden.

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
  `max_config_bytes <= max_git_stdout_bytes`,
  `max_component_bytes <= max_relative_path_bytes`, and
  `max_file_bytes <= max_total_bytes`. There are no other cross-field
  inequalities.

`TaskSourceSpec.__post_init__()` takes a one-time `raw_root =
os.fspath(repository_root)`, validates that exact string, and stores a newly
constructed `Path(raw_root)`. It never retains the caller's `PathLike`
instance. Constructors, `prepare_task_source()`, and Task 7 revalidate exact
dataclass types, exact `vars()` field sets, exact nested dataclass types, and
every scalar so `object.__new__` or mutable caller state cannot bypass the
contract.

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

Packed refs may be empty or header-only. Non-empty bytes contain no NUL or CR
and end in LF. The first line may
be one header `# pack-refs with: ` followed by one or more unique tokens selected from
`peeled`, `fully-peeled`, and `sorted`. A ref record is exactly
`<full-lowercase-oid> SP <refname>`. A peeled record is exactly
`^<full-lowercase-oid>` and may occur only once immediately after a ref
record. Ref names start with `refs/`, contain 1 through 1024 ASCII bytes, have
no empty, `.` or `..` component, control, space, backslash, consecutive dot,
`@{`, or `//`, and do not end in `/`, `.`, or `.lock`. Duplicate refs,
additional comments, and every `refs/replace/` name fail.

An ordinary `.git/hooks` directory containing only regular, single-link
`*.sample` files with `st_mode & 0o022 == 0` is allowed but never opened or
executed. Owner-write, including the normal `0755` sample mode, is allowed.
Any non-sample hook is rejected. Traverse `.git/refs`, `.git/hooks`,
`.git/worktrees`, and `.git/modules` with the same descriptor-relative
directory and identity rules; do not follow an intermediate link. The process
policy disables hooks independently.

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

## 6. Exact canonical evidence documents

For every document below, `D(value)` means `sha256:` plus lowercase SHA-256
over `canonical_bytes(value)`. Exact dictionaries reject missing or
additional keys. Exact integers reject booleans.

An identity record has exactly:

```json
{"ctime_ns":0,"dev":0,"gid":0,"ino":0,"kind":"directory","mode":493,"mtime_ns":0,"nlink":1,"size":0,"uid":0}
```

The displayed numbers are shape examples. `kind` is `directory` or `file`;
`mode` is the exact integer `stat.S_IMODE(st_mode)`; the other values are the
corresponding nonnegative exact `stat` integers.

### 6.1 Filesystem and control seal `F`

`F = D(filesystem_document)` where the document has exactly:

```json
{
  "ancestor_identities": [],
  "config": {
    "identity": {},
    "raw_digest": "sha256:<64 lowercase hex>"
  },
  "control_policy_state": {
    "commondir": "absent",
    "config_worktree": "absent",
    "hooks": "absent",
    "modules": "absent",
    "object_alternates": "absent",
    "promisor_markers": "absent",
    "replace_refs": "absent",
    "worktrees": "absent"
  },
  "document_type": "task-source-filesystem-seal-v1",
  "expected_object_format": "sha1",
  "git_dir_identity": {},
  "objects_identity": {},
  "packed_refs": {"state": "absent"},
  "repository_identity": {},
  "schema_version": 1
}
```

The identity placeholders are exact identity records.
`ancestor_identities` is the anchor-through-parent sequence in traversal
order and contains no names. `expected_object_format` is `sha1` or `sha256`.
When packed refs are present, its exact alternative is:

```json
{"identity":{},"raw_digest":"sha256:<64 lowercase hex>","state":"present"}
```

The fixed control-state strings state the verified outcome, not caller input.
`hooks` is exactly `absent` or `sample_only`; `modules` and `worktrees` are
exactly `absent` or `empty`.

### 6.2 Local configuration seal `C`

After parsing the bounded Git output, create:

```json
{
  "document_type": "task-local-config-seal-v1",
  "raw_config_digest": "sha256:<64 lowercase hex>",
  "records": [
    {"key":"core.bare","value":"false"}
  ],
  "schema_version": 1
}
```

Every record has exactly `key` and `value`. Records are sorted by key UTF-8
bytes then value UTF-8 bytes. `C = D(config_document)`. The document may exist
transiently in memory to calculate `C`; raw bytes, records, and values are
released and only `C` is stored.

### 6.3 Object topology seal `O`

Topology paths are relative to `.git/objects`, not the repository root. The
root record path is the exact string `.`. Each record has exactly:

```json
{"ctime_ns":0,"dev":0,"gid":0,"ino":0,"kind":"file","mode":292,"mtime_ns":0,"nlink":1,"path":"pack/pack-<oid>.idx","size":0,"uid":0}
```

Records are sorted by `path.encode("utf-8")`. The exact outer document is:

```json
{
  "document_type": "task-object-topology-v1",
  "entry_count": 1,
  "file_count": 0,
  "object_format": "sha1",
  "records": [
    {"ctime_ns":0,"dev":0,"gid":0,"ino":0,"kind":"directory","mode":493,"mtime_ns":0,"nlink":1,"path":".","size":0,"uid":0}
  ],
  "schema_version": 1,
  "total_bytes": 0
}
```

The numbers and empty records are shape examples and must agree with the
actual records. `O = D(topology_document)`, and
`ObjectTopologySeal.object_topology_digest == O`.

### 6.4 Source authority identity and receipt mapping

The exact source authority document is:

```json
{
  "document_type": "task-source-identity-v1",
  "filesystem_identity_digest": "sha256:<F>",
  "local_config_digest": "sha256:<C>",
  "schema_version": 1
}
```

`S = D(source_authority_document)`. A prepared source stores `S`, `C`, and
the typed topology seal containing `O`. Task 7 maps them into the existing
Task 4 receipt without adding fields:

```text
source_identity_before_digest = S0 = D(F0, C0 document)
source_identity_after_digest  = S1 = D(F1, C1 document)
object_topology_before_digest = O0
object_topology_after_digest  = O1
git_process_policy_digest     = P
inventory_file_count          = topology.file_count
inventory_total_bytes         = topology.total_bytes
object_format                 = prepared.object_format
```

The notation `D(F, C document)` means the exact
`task-source-identity-v1` document above, not concatenated strings.
The receipt's task ID, provisioning class, attestation, and local-clone
policy are copied from the revalidated `TaskSourceSpec`; its input digest
comes from the authoritative Task 1 input.

### 6.5 Git process policy document `P`

`P = D(process_policy_document)`. It has exactly:

```json
{
  "argv_prefix": ["git","-c","core.fsmonitor=false","-c","core.attributesFile=<null-device>","-c","core.excludesFile=<null-device>","-c","core.hooksPath=<null-device>","-c","submodule.recurse=false","--git-dir=<verified-git-dir>"],
  "close_fds": true,
  "cwd": "/",
  "document_type": "task-git-process-policy-v1",
  "environment": {
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "<null-device>",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "<os.defpath>"
  },
  "limits": {
    "git_termination_grace_milliseconds": 250,
    "git_timeout_seconds": 15,
    "max_git_stderr_bytes": 65536,
    "max_git_stdout_bytes": 16777216
  },
  "operation_templates": [],
  "schema_version": 1,
  "shell": false,
  "start_new_session": true,
  "stdin": "DEVNULL",
  "termination": ["concurrent-bounded-drain","monotonic-deadline","TERM","bounded-grace","KILL","reap","close-pipes","verify-group-absent"]
}
```

`environment` has exactly the keys in Section 8, using `<null-device>` for
`os.devnull` and `<os.defpath>` for `PATH`. `limits` has exactly
`git_timeout_seconds`, `git_termination_grace_milliseconds`,
`max_git_stdout_bytes`, and `max_git_stderr_bytes` with the current validated
policy integers. `operation_templates` is exactly:

```json
[
  ["config","--file=<verified-config>","--no-includes","--null","--list"],
  ["rev-parse","--show-object-format=storage"],
  ["rev-parse","--verify","--end-of-options","<validated-full-oid>^{commit}"],
  ["rev-parse","--verify","--end-of-options","<validated-full-oid>^{tree}"],
  ["ls-tree","-r","-l","-z","--full-tree","<validated-tree-oid>"],
  ["cat-file","blob","<validated-full-oid>"]
]
```

No actual path or OID enters this document.

## 7. Object-database grammar and seal

Walk `.git/objects` iteratively with the descriptor-relative open and
`os.scandir(fd)` procedure in Section 2 and never follow links. Every child
directory is opened from its verified parent fd and `fstat()`-matched before
descent; no assembled child path is reopened. Every walked directory and
file has current uid, the repository device, and `st_mode & 0o022 == 0`.
Every file is additionally regular with `st_nlink == 1`. Enforce these rules
before the first Git subprocess.

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

## 8. Bounded Git process policy

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

No operation returns until the leader is waited, both pipes reach EOF or are
closed, and the recorded process group is confirmed absent. Timeout,
output-limit, drain failure, nonzero exit, or a residual group after nominal
leader completion always runs TERM/grace/KILL, waits the leader, closes both
pipes, and verifies group absence. A zero leader status is successful only
when no group member remains. A residual group after nominal success and
every internal drain or cleanup failure are `task_git_failed`. If cleanup
cannot be confirmed, do not preserve an earlier success, timeout, or
output-limit classification.

`git_process_policy_digest` hashes a canonical
`task-git-process-policy-v1` document containing the exact prefix, operation
templates, environment, cwd policy, timeout, grace, caps, termination rules,
and placeholders `<verified-git-dir>`, `<verified-config>`, and
`<validated-full-oid>`. It never hashes or retains an actual path.

## 9. Preparation and sole receipt issuance

`prepare_task_source()` performs, in order:

```text
validate source and policy without source access
capture raw filesystem/control seal F0 and object seal O0
run bounded config and derive semantic config seal C0
run bounded object-format metadata operation
run bounded config again and derive C1
capture raw filesystem/control seal F1 and object seal O1
require F0 == F1, C0 == C1, and O0 == O1
derive S = D(task-source-identity-v1(F0, C0))
return PreparedTaskSource storing S, C0, O0, format, and P
```

It performs no `rev-parse --verify`, `ls-tree`, or `cat-file`, and imports no
receipt constructor.

Task 7 capture is a separate transaction. Before its first Git spawn, require:

- `type(prepared) is PreparedTaskSource` and exact recursive field
  revalidation succeeds;
- `prepared.git_dir == prepared.source.repository_root / ".git"`;
- the materializer's exact validated policy equals `prepared.policy`;
- a recomputed `P` equals `prepared.git_process_policy_digest`;
- a fresh raw filesystem/control seal F0 plus fresh config operation C0
  derives exactly `prepared.source_identity_digest`;
- `C0 == prepared.local_config_digest`;
- fresh `O0` equals every field of `prepared.object_topology`;
- the commit-OID-length format equals `prepared.object_format`.

It then performs all fixed-OID object reads, runs the bounded config operation
again to derive C1, and captures F1 and O1 once after the final object
command. Require F1 == F0, C1 == C0, O1 == O0, and the recomputed final source
identity to equal the prepared source identity. It does not rescan the object
database around each blob.

Only after those equalities hold does Task 7 issue exactly one
`task_source_trust` receipt using the existing Task 4 payload. The source
identity fields bind the full filesystem/config/control seal, the topology
fields bind the object seal, and the process-policy field binds the path-free
policy document. Every failure path issues no source-trust receipt.

## 10. Required TDD and security checkpoint

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
