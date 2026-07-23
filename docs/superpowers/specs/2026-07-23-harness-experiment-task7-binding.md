# Harness Experiment Task 7 Binding Contract

**Status:** Normative clarification for Tasks 7 and 8 of
`2026-07-23-harness-experiment-readiness-plan.md`.

This contract defines the complete fixed-object capture, paired
materialization, snapshot-receipt, and allowed-write-policy boundary. If the
implementation plan or the Task 6 binding conflicts with this file for Task
7 or its Task 8 handoff, this file takes precedence.

## 1. Scope, precedence, and selected approach

Task 7 consumes one exact `PreparedTaskSource`, captures the commit's complete
regular-file tree from Git objects, issues the sole source-trust receipt,
materializes the same capture into two distinct condition roots, verifies both
roots, and then issues one condition-independent snapshot receipt shared by
both returned snapshots. It never reads the source working tree, index, HEAD,
ref names, attributes, ignore rules, submodules, or caller-selected Git argv.

The selected capture strategy is one bounded `cat-file blob <full-blob-oid>`
operation per unique blob OID. Batch mode is not used in Phase A. This costs
more process launches than `cat-file --batch`, but preserves the already
reviewed one-operation/one-object boundary, makes each stdout cap equal the
declared object size, and avoids a persistent parser whose framing failure
could desynchronize later objects. `max_unique_blobs=256` and the 60-second
capture transaction cap bound that cost. A selected corpus above 256 unique
blobs fails closed. Phase A must not silently raise that ceiling; supporting a
larger corpus requires a separately reviewed batch protocol and a new policy
version.

The following amendments are normative:

- the Task 6 process-policy templates and operation tails are replaced by
  Section 3 below;
- the final sentence of Task 6 Section 9, “Every failure path issues no
  source-trust receipt,” means **every capture failure before successful
  source-trust receipt construction**. A later pair-materialization failure
  occurs after capture and therefore does not erase or invalidate that
  already-issued source-trust receipt;
- Task 7 has no public single-target `materialize()` operation. The only
  public write operation is `materialize_pair()`;
- “atomic pair” means one ownership-tracked all-or-rollback API transaction.
  POSIX does not provide a portable atomic two-directory publish operation.
  The method does not claim that an observer with the same UID cannot briefly
  see one in-progress root. Such an observer is outside the machine-verified
  source assumptions; detected interference fails closed.

Capture and materialization are separate trust transitions. A successful
`capture()` returns one source-trust receipt and retained immutable blob
bytes. A successful `materialize_pair()` returns two snapshots sharing one
snapshot receipt. No snapshot receipt is constructed until both roots have
been sealed and independently verified.

## 2. Exact policy and public surface

Task 7 extends the Task 6 policy with four bounded fields. The complete exact
field set and field order become:

```python
@dataclass(frozen=True)
class TaskSnapshotPolicy:
    policy_version: str = "task-object-materializer-v1"
    git_timeout_seconds: int = 15
    git_termination_grace_milliseconds: int = 250
    max_git_stdout_bytes: int = 16 * 1024 * 1024
    max_git_stderr_bytes: int = 64 * 1024
    capture_timeout_seconds: int = 60
    max_config_bytes: int = 256 * 1024
    max_packed_refs_bytes: int = 4 * 1024 * 1024
    max_object_entries: int = 200000
    max_object_store_bytes: int = 2 * 1024 * 1024 * 1024
    max_object_depth: int = 3
    max_component_bytes: int = 255
    max_relative_path_bytes: int = 4096
    max_tree_entries: int = 100000
    max_tree_depth: int = 64
    max_files: int = 10000
    max_unique_blobs: int = 256
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
```

Each integer retains the Task 6 exact-`int`, positive, inclusive hard-ceiling
rule, using its displayed default as the ceiling. The Task 6 cross-field
rules remain unchanged. There are no new cross-field inequalities: lowering
one cap intentionally makes that cap independently authoritative.

`max_tree_entries` counts trie nodes after the complete tree is parsed:
exactly one root node, every distinct non-root directory implied by a file
path, and every file node. The empty tree therefore has one tree entry and
zero files. The count is checked inclusively while constructing the trie;
cap plus one is `task_tree_limit`. This definition bounds derived directories,
not merely `ls-tree` records. `max_tree_depth` counts path components, so a
root file has depth one and the empty-tree root has depth zero.

Task 7 adds this exact public surface:

```python
@dataclass(frozen=True)
class TaskTreeEntry:
    path: str
    git_mode: str
    blob_oid: str
    size: int
    content_digest: str


@dataclass(frozen=True)
class CapturedTaskObjects:
    source_trust_receipt: CanonicalReceipt
    source: TaskSourceSpec = field(repr=False)
    policy: TaskSnapshotPolicy = field(repr=False)
    object_format: str
    commit_oid: str
    tree_oid: str
    entry_digest: str
    entries: Tuple[TaskTreeEntry, ...] = field(repr=False)
    blobs: Mapping[str, bytes] = field(repr=False)
    file_count: int
    total_bytes: int
    unique_blob_count: int
    unique_blob_bytes: int


@dataclass(frozen=True)
class MaterializedTaskSnapshot:
    snapshot_receipt: CanonicalReceipt
    target_root: Path = field(repr=False)
    target_identity_digest: str
    materialized_tree_digest: str
    file_count: int
    total_bytes: int


class TaskSnapshotMaterializer:
    def __init__(self, policy: TaskSnapshotPolicy) -> None:
        ...

    def capture(
        self, prepared: PreparedTaskSource
    ) -> CapturedTaskObjects:
        ...

    def materialize_pair(
        self,
        captured: CapturedTaskObjects,
        target_parent: Path,
        current_name: str = "current",
        lean_name: str = "lean",
    ) -> Tuple[MaterializedTaskSnapshot, MaterializedTaskSnapshot]:
        ...

    def verify(self, snapshot: MaterializedTaskSnapshot) -> None:
        ...

    def close(self) -> None:
        ...
```

All exact-type, exact-`vars()` field-set, scalar, nested dataclass, tuple, and
mapping invariants are revalidated at every public boundary. Constructors
detach tuple values, bytes values, mappings, and paths. `blobs` is a
`MappingProxyType` over a new dictionary inserted in full-OID byte-sort order;
every value has exact immutable type `bytes`. Its keys are exactly the
distinct blob OIDs referenced by `entries`. `entries` is path-UTF-8-byte
sorted and contains detached exact `TaskTreeEntry` values. Public objects
never expose a source or target absolute path through `repr`, receipt payload,
canonical document, or failure text.

The materializer stores a detached, revalidated policy. `capture()` requires
that it equal `prepared.policy` field for field. `materialize_pair()` requires
that `captured.policy` equal the materializer policy and fully revalidates the
source-trust receipt and every captured digest, count, entry, and blob.
`verify()` similarly rejects forged or mutated snapshot objects and receipts.
In particular, the receipt task/source/format fields must equal
`captured.source`; its process-policy digest must equal the value recomputed
from `captured.policy`; `commit_oid` must equal `source.commit_oid`; all OID
lengths must equal `object_format`; the exact entry document and digest must
recompute; mapping keys and unique counts must equal entry references; every
blob byte must rehash to its key and entry content digest; and all logical and
unique totals must recompute.

Semantic recomputation alone cannot distinguish a caller-constructed,
internally consistent capture from one returned by Git capture. The
materializer therefore retains one strong reference to the exact
`CapturedTaskObjects` instance returned by its successful `capture()`.
Exact object identity alone also cannot detect a coherent in-place mutation
performed with Python's low-level `object.__setattr__`. At successful capture,
the materializer therefore retains one private detached operational seal
containing the complete validated scalar, entry, OID-sorted blob-byte,
receipt-projection, and protected-source-identity values. `materialize_pair()`
requires both exact public object identity and exact equality with this seal
before any target mutation, then uses only the detached seal as its byte and
metadata authority and never rereads the caller-owned public capture. This
seal is process-local mutation defense, is never serialized or exposed,
confers no durable provenance, and is cleared after pair success, rollback,
or `close()`.
Its complete lifecycle is:

```text
new --capture success--> captured --pair success--> paired --close--> closed
new/captured --operation failure and rollback--> closed
new/captured/paired --close--> closed
```

One private `threading.Lock` may serialize the three state-mutating public
methods (`capture()`, `materialize_pair()`, and `close()`), but no
concurrency-poisoning protocol or registry is required. `capture()` is valid
only from `new`; `materialize_pair()` is valid only from `captured`.
`materialize_pair()` first requires `captured is self._captured`, then performs
the complete semantic and receipt revalidation above. A different, copied,
serialized, or merely equal object is `task_snapshot_receipt_invalid`.
The retained reference is consumed and cleared after pair success or rollback.
A failed capture or pair closes the materializer after its required cleanup.
A wrong-state or closed-state call fails as
`task_snapshot_receipt_invalid` without authorizing another transition or
closing a valid capture retained by another serialized call.

`close()` is public, exact, path-free, and idempotent. From every state it
closes any currently owned top-level descriptor, clears the captured strong
reference and private path copies, and permanently enters `closed`.
It performs no recursive filesystem cleanup; a partially written pair has
already followed Section 9 rollback before the public operation returns.
Every state-mutating public method other than another `close()` rejects after
closure.

`verify(snapshot)` is repeatable, stateless, and does not advance this
lifecycle. It revalidates the complete public object, receipt, root identity,
inventory, modes, sizes, and bytes on every call. It may be called repeatedly
in `paired` before `close()` and rejects after closure. It is available for a
future Phase B launcher, but Task 8 does not call it immediately after
`materialize_pair()` because that operation already performs two independent
full verifications. Task 8 closes the materializer after extracting path-free
content evidence. There is no capture fingerprint, snapshot registry, one-shot
verification token, or durable in-process provenance claim. In particular,
the private operational seal is not a digest-only substitute for exact object
identity and cannot be used after its owning materializer is closed.

## 3. Amended Git process policy

Task 7 replaces the Task 6 policy with the complete exact document below.
The `ls-tree --format=...` value is one argv token; placeholders are literal
host-path/OID-free policy text.

```json
{
  "argv_prefix": ["git","-c","core.fsmonitor=false","-c","core.attributesFile=<null-device>","-c","core.excludesFile=<null-device>","-c","core.hooksPath=<null-device>","-c","submodule.recurse=false","--git-dir=<verified-git-dir>"],
  "capture_deadline": {
    "capture_timeout_seconds": 60,
    "clock": "time.monotonic",
    "effective_process_deadline": "earliest-of-capture-and-operation",
    "equal_expiry_classification": "task_capture_timeout",
    "scope": "fresh-F0-through-source-trust-receipt"
  },
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
  "operation_templates": [
    ["config","--file=<verified-config>","--no-includes","--null","--list"],
    ["rev-parse","--show-object-format=storage"],
    ["rev-parse","--verify","--end-of-options","<validated-full-commit-oid>^{commit}"],
    ["rev-parse","--verify","--end-of-options","<validated-full-commit-oid>^{tree}"],
    ["ls-tree","-r","-z","--full-tree","--format=%(objectmode)%x09%(objecttype)%x09%(objectname)%x09%(objectsize)%x09%(path)","<validated-full-tree-oid>"],
    ["cat-file","blob","<validated-full-blob-oid>"]
  ],
  "output_policy": {
    "cap_is_inclusive": true,
    "cap_plus_one_action": "terminate-process-group",
    "cat_file_blob_stdout_cap": "validated-declared-blob-size",
    "generic_stdout_cap": "max_git_stdout_bytes",
    "successful_stderr": "empty"
  },
  "schema_version": 1,
  "shell": false,
  "start_new_session": true,
  "stdin": "DEVNULL",
  "termination": ["concurrent-bounded-drain","monotonic-deadline","TERM","bounded-grace","KILL","reap","close-pipes","verify-group-absent"]
}
```

The displayed integers are the default validated policy values; a
non-default accepted policy substitutes its exact current values in
`capture_timeout_seconds` and `limits`. Every key and string remains exact.
Both Task 6 preparation and Task 7 capture recompute this amended `P`. Task 6
binds the future capture/output rules even though preparation itself invokes
only config and storage-format operations.

Every successful Git operation additionally requires stderr to be exactly
empty. Any stderr byte, including a warning paired with exit status zero, is
`task_git_failed` after ordinary process-group cleanup. Raw stderr is never
reported or retained.

The generic stdout ceiling applies to config, object-format, commit, tree, and
`ls-tree` operations. Each `cat-file blob` operation instead receives the
entry's validated declared size as its exact dynamic stdout cap, including
zero. A read of declared size plus one terminates and fails
`task_blob_invalid`; generic process cleanup failure still overrides with
`task_git_failed`. The implementation must not first buffer against the
larger generic ceiling.

## 4. Capture transaction and operation order

`capture()` follows this exact state machine:

```text
validate prepared/source/policy exact shapes without source access
require materializer policy == prepared.policy
recompute amended process policy P and compare it with prepared.P
establish one monotonic capture deadline
capture fresh filesystem/control seal F0 and object-topology seal O0
run config and derive C0
run storage-format metadata and compare it with prepared.object_format
derive S0 from F0/C0 and compare S0, C0, O0, and all counts with prepared
verify the exact input commit OID as a commit
resolve that exact commit's tree OID
read the formatted recursive tree for that exact full tree OID
parse and validate the complete tree and every limit before any blob read
read each distinct full blob OID exactly once in OID byte-sort order
verify every blob size, Git object OID, and SHA-256 content digest
build and validate the canonical entry document and digest
run config and derive C1
capture fresh filesystem/control seal F1 and object-topology seal O1
derive S1 from F1/C1
require F1 == F0, C1 == C0, O1 == O0, S1 == S0
require the final S1, C1, O1, counts, object format, policy, and P still equal prepared
construct and self-validate exactly one task_source_trust receipt
return the immutable capture
```

There are exactly two config operations, one storage-format operation, one
commit verification, one tree resolution, one formatted `ls-tree`, and one
`cat-file` per distinct blob. No fixed-object operation occurs before the
fresh source matches the prepared source. No Git operation occurs after C1.
No source-trust receipt constructor is called before every comparison
succeeds.

The commit verification stdout must be exactly:

```text
<prepared.source.commit_oid>\n
```

The OID before LF must byte-for-byte equal the input commit OID. This rejects
an annotated-tag OID that peels to a commit. Tree-resolution stdout is exactly
one full lowercase OID of the selected format followed by one LF and no other
byte. Storage-format stdout retains the exact Task 6 grammar. No output is
trimmed, decoded with replacement, or accepted with extra whitespace.
An exact commit-output mismatch is `task_source_changed`; malformed
tree-resolution output is `task_tree_invalid`.

The capture deadline is established with `time.monotonic()` immediately
before F0 and expires after `capture_timeout_seconds`. It covers source seals,
all Git operations, parsing, hashing, receipt construction, and intervening
work. Check it before and after every bounded phase and during process
monitoring. Local synchronous filesystem work need not be asynchronously
preempted, but observing expiry when it returns is
`task_capture_timeout`.

Each Git operation also retains its own `git_timeout_seconds` deadline,
established immediately before `Popen` as required by Task 6. The effective
process deadline is the earlier of the per-operation and capture deadlines.
If the capture deadline is the first expired bound, terminate and classify
`task_capture_timeout`; otherwise classify `task_git_timeout`. If both are
observed expired at the same check, the transaction classification wins.
Failure to confirm process-group cleanup always overrides either result with
`task_git_failed`.

Count and byte bounds limit work requested by the adapter, but Python cannot
preempt a synchronous filesystem syscall. A slow or stuck `open`, `stat`,
`scandir`, `read`, `write`, `chmod`, `unlink`, or `rmdir` may exceed
the observed capture deadline; during capture the implementation checks that
deadline immediately after control returns and then fails, but cannot
guarantee wall-clock interruption inside the syscall. Materialization,
verification, rollback, and Task 8 final cleanup have count, byte, depth, and
descriptor bounds but no Phase A wall-clock deadline. A slow or stuck local
filesystem syscall in those phases is therefore a reported residual
limitation. The Task 6 operator attestation still excludes
network/FUSE/external-mutable source mounts; structural bounds do not convert
syscall latency into a verified property.

The source-trust payload maps the fresh equal seals exactly as defined by
Task 6 Section 6.4. It uses `input_digest=source.input_digest`,
`plan_digest=None`, and `previous_record_hash=None`.

## 5. Tree grammar, path trie, and exclusions

The formatted `ls-tree` stdout is a byte protocol. Empty stdout is the one
valid representation of an empty tree. Non-empty stdout ends in exactly one
record-terminating NUL and contains no empty record. Split each record at the
first four ASCII TAB bytes into exactly:

```text
objectmode TAB objecttype TAB objectname TAB objectsize TAB path
```

The first four fields are non-empty ASCII. `objectmode` is exactly `100644`
or `100755`; `objecttype` is exactly `blob`; `objectname` is a full lowercase
OID of the declared object format; and `objectsize` is canonical unsigned
decimal (`0` or a nonzero digit followed by digits, with no sign or leading
zero). The framing parser can unambiguously carry an ordinary TAB after the
first four structural TABs, but the later `Cc` path policy rejects it. A path
may not contain NUL because NUL terminates the record.
Unknown mode, tree, commit, gitlink, symlink, special object, missing size,
malformed record, or extra record field is `task_tree_invalid`.

Decode each path with strict UTF-8. The decoded string must already be NFC.
Reject any Unicode character whose general category is `Cc`, `Cf`, or `Cs`,
and reject backslash. Reject a leading slash, trailing slash, empty
component, `.`, `..`, or a component ending in ASCII dot or space. Each
component is non-empty NFC UTF-8 of at most `max_component_bytes`; the full
relative path is at most `max_relative_path_bytes`; path depth is at most
`max_tree_depth`. Paths are never normalized or repaired.

Build one complete trie before the first blob operation. At every sibling
set, reject collisions under all three keys:

- exact decoded text;
- `unicodedata.normalize("NFC", text)`;
- `unicodedata.normalize("NFC", text).casefold()`.

Although accepted text is already NFC, retaining all three checks makes the
alias rule explicit and testable. Reject duplicate file paths, a file that is
also a directory prefix, a later parent beneath an existing file, and any
other file/directory conflict. Count the root, derived directories, and files
against `max_tree_entries` as defined in Section 2. Sort the final file
entries by `path.encode("utf-8")`; input record order has no authority.

Exclusions use the same NFC-casefold alias key and are exact:

- a component canonically equal to `.git` is forbidden at every depth,
  whether it would be a directory or the final file;
- a file basename canonically equal to `AGENTS.md` or
  `AGENTS.override.md` is forbidden at every depth;
- a **directory component** canonically equal to `.codex`, `.agents`,
  `.claude`, or `.mcp` is forbidden at every depth;
- a **top-level directory component** canonically equal to `.git-hooks`,
  `hooks`, or `plugins` is forbidden. The same names below another ordinary
  source directory are allowed because application source commonly uses
  nested `hooks` and `plugins` packages and no Phase A consumer treats those
  nested names as repository-level configuration;
- a file basename canonically equal to `.mcp.json`, `mcp.json`, `.env`,
  `.env.local`, `.npmrc`, `.pypirc`, `credentials.json`, or `secrets.json`
  is forbidden at every depth.

“Directory component” means a non-final path component implied by a file
entry. An otherwise valid regular file whose final basename is `plugins`,
for example, is not rejected by the directory rule. These exact exclusions,
the path grammar, and their scopes are part of
`task-object-materializer-v1`; changing them requires a new policy version
and corresponding receipt-validator change. There is no free-form content
scan.

This is an exact enumerated denylist, not a complete secret detector.
Unlisted names or content such as provider-specific credential files,
`.env.production`, cloud configuration, private fixtures, or credentials
embedded in ordinary source are not machine-classified by Task 7. Their
absence is an operator-attested corpus-provisioning residual and must not be
reported as verified merely because this denylist passed.

Validate the complete set before issuing any `cat-file` operation:

- file count is at most `max_files`;
- every declared size is at most `max_file_bytes`;
- the sum of declared sizes over paths is at most `max_total_bytes`;
- distinct blob OID count is at most `max_unique_blobs`;
- repeated occurrences of one blob OID declare exactly the same size;
- the trie count, component, relative-path, and depth limits all hold.

All caps are inclusive. The implementation may fail as soon as a malformed
record or a cap-plus-one state is proven, but it must never fetch a blob from
a partially validated tree.

## 6. Blob loading, limits, and content integrity

After complete tree validation, order distinct blob OIDs by ASCII bytes and
run exactly one fixed `cat-file blob` operation for each. The declared size
for that OID is the operation's dynamic stdout cap. Success requires stdout
length to equal the declared size exactly and stderr to be empty. Missing
objects, short output, long output, or a nonzero Git exit cannot yield a
capture.

For each returned byte string, recompute the Git object OID without trusting
Git's label:

```text
object_bytes = b"blob " + ASCII_DECIMAL(len(content)) + b"\0" + content
sha1 repository:   lowerhex(SHA1(object_bytes))
sha256 repository: lowerhex(SHA256(object_bytes))
```

The recomputed full lowercase OID must equal the requested OID. Independently
compute `content_digest = "sha256:" + SHA256(content).hexdigest()`. Every
`TaskTreeEntry` carries that content digest. Repeated paths referencing one
OID must therefore have the same size and content digest and share one
retained `bytes` value in the `blobs` mapping.

`file_count` is the number of file paths. `total_bytes` is the logical sum
over paths and is what the snapshot receipt records. `unique_blob_count` is
the number of mapping keys. `unique_blob_bytes` is the sum of one validated
size per mapping key. The unique-byte total cannot exceed the logical total
and is also bounded by `max_total_bytes`. Integer accumulation occurs before
receipt construction and rejects booleans, negative values, or an
inconsistent recomputation.

Retained blob bytes live only in `CapturedTaskObjects`. Task 8 must drop its
last reference immediately after `materialize_pair()` and receipt extraction
for that task, before processing the next task. CPython does not guarantee
physical memory zeroization; reports must list that as a residual limitation,
not as a verified property.

## 7. Canonical evidence documents and receipts

For this section, `D(value)` means `sha256:` plus lowercase SHA-256 over
`canonical_bytes(value)`. Every dictionary has exactly the displayed keys,
every integer is an exact nonnegative `int`, and every list is in the stated
canonical order.

### 7.1 Captured entry document

`entry_digest` is `D(entry_document)` for this exact
`task-tree-entries-v1` document:

```json
{
  "commit_oid": "<full lowercase commit OID>",
  "directory_count": 2,
  "document_type": "task-tree-entries-v1",
  "entries": [
    {
      "blob_oid": "<full lowercase blob OID>",
      "content_digest": "sha256:<64 lowercase hex>",
      "git_mode": "100644",
      "path": "src/example.py",
      "size": 1
    }
  ],
  "file_count": 1,
  "logical_total_bytes": 1,
  "object_format": "sha1",
  "schema_version": 1,
  "tree_entry_count": 3,
  "tree_oid": "<full lowercase tree OID>",
  "unique_blob_bytes": 1,
  "unique_blob_count": 1
}
```

`entries` is sorted by path UTF-8 bytes. `directory_count` includes the root
and every derived directory; `tree_entry_count` is directory count plus file
count. The empty tree has an empty list, `directory_count=1`,
`tree_entry_count=1`, and zero file, logical-byte, unique-blob, and
unique-byte values. `commit_oid`, `tree_oid`, `object_format`, every entry,
and every aggregate are recomputed from the validated capture rather than
copied from a caller-built document.

### 7.2 Materialized tree document

The target-independent tree seal is:

```json
{
  "directory_count": 1,
  "document_type": "task-materialized-tree-v1",
  "entry_count": 1,
  "file_count": 0,
  "records": [
    {
      "content_digest": null,
      "kind": "directory",
      "mode": 365,
      "path": ".",
      "size": 0
    }
  ],
  "schema_version": 1,
  "total_bytes": 0
}
```

The root record `.` is included. Every derived directory record has the same
exact keys, `kind="directory"`, `mode=365` (`0555`),
`content_digest=null`, and logical `size=0`. Every file record has
`kind="file"`, path, final integer mode `292` (`0444`) for Git `100644` or
`365` (`0555`) for Git `100755`, exact size, and the SHA-256 digest recomputed
from bytes re-read through the sealed target. Records are sorted by
`path.encode("utf-8")`; no absolute path, inode, timestamp, source OID, or
caller-selected name enters this document.

`entry_count` is `directory_count + file_count`. `total_bytes` is the logical
file-byte sum. `materialized_tree_digest = D(materialized_tree_document)`.
Both condition roots must independently produce the same document and digest,
and that digest must equal the document predicted from the captured entries
and validated blob bytes.

### 7.3 Target root identity document

Each condition root has a distinct path-free local identity:

```json
{
  "document_type": "task-materialized-root-identity-v1",
  "materialized_tree_digest": "sha256:<64 lowercase hex>",
  "root_identity": {
    "ctime_ns": 0,
    "dev": 0,
    "gid": 0,
    "ino": 0,
    "kind": "directory",
    "mode": 365,
    "mtime_ns": 0,
    "nlink": 1,
    "size": 0,
    "uid": 0
  },
  "schema_version": 1
}
```

The displayed stat numbers are shape examples. `root_identity` is the exact
full identity record defined by Task 6, captured after final chmod and
successful inventory verification. `target_identity_digest` is the digest of
this document. It intentionally identifies this local materialization
instance and therefore differs between current and lean roots.

### 7.4 Receipt construction and public validation

The sole source-trust receipt uses the unchanged Task 4 exact payload and the
equal before/after values:

```text
source_identity_before_digest = S0
object_topology_before_digest = O0
source_identity_after_digest  = S1 == S0
object_topology_after_digest  = O1 == O0
git_process_policy_digest     = amended P
inventory_file_count          = O0.file_count
inventory_total_bytes         = O0.total_bytes
```

Public reconstruction validates these topology aggregates against the same
Task 6 policy caps before receipt canonicalization:
`inventory_file_count <= max_files` and
`inventory_total_bytes <= max_object_store_bytes`.

The pair's sole snapshot receipt uses the unchanged Task 4 payload:

```text
task_id                    = captured.source.task_id
object_format              = captured.object_format
commit_oid                 = captured.commit_oid
tree_oid                   = captured.tree_oid
entry_digest               = captured.entry_digest
materialized_tree_digest   = the equal verified pair digest
materializer_policy_version = captured.policy.policy_version
file_count                 = captured.file_count
total_bytes                = captured.total_bytes
source_trust_receipt_digest = captured.source_trust_receipt.receipt_digest
```

It uses `input_digest=captured.source.input_digest`, `plan_digest=None`, and
`previous_record_hash=None`. The target-root identity is deliberately absent
so both condition roots share one content receipt. Each returned snapshot
stores the exact same `CanonicalReceipt` instance (`is`, not only equal
digest), while storing its own distinct target identity.

Task snapshot code may import only the public `CanonicalReceipt` and
`make_receipt` names from `experiment_receipts`. It must not import the
private receipt validator. To accept any receipt at a public boundary:

1. require exact `CanonicalReceipt` type and exact dataclass field set;
2. recursively require exact envelope, payload, byte, digest, and scalar
   types;
3. call public `make_receipt()` with the receipt's envelope and a detached
   payload;
4. require the reconstructed canonical bytes, digest, envelope, and exact
   payload projection to equal the supplied object.

Receipt construction uses the same reconstruction check before a receipt
enters a returned public object. Any receipt exception or mismatch is
sanitized to `task_snapshot_receipt_invalid` with no chained context.

## 8. Trusted target parent and paired materialization

`target_parent` is caller-owned and is never removed. Its original
`os.fspath()` value has exact type `str`, is absolute, equals both
`normpath()` and `realpath()`, and names an existing plain directory.
Descriptor-relative traversal from the filesystem anchor rejects every
symlink and applies the Task 6 ancestor ownership/writability rules. The
parent itself must have current UID, exact mode `0700`, and a stable
no-follow/fstat identity. Open it with
`O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC` and retain that trusted
descriptor for the complete pair transaction.

`current_name` and `lean_name` each have exact type `str`, are NFC, encode as
UTF-8 within `max_component_bytes`, consist of exactly one non-empty
component, and are neither `.` nor `..`. They contain no slash, backslash,
NUL, `Cc`, `Cf`, or `Cs` character and do not end in ASCII dot or space. They
must be distinct under exact, NFC, and NFC-casefold keys. Neither target may
exist at the initial no-follow lookup.

At successful capture, the materializer retains physical identity keys
(`dev`, `ino`, and `kind`) for the verified source root and its exact `.git`
directory in addition to the one-time source-root string. Full source
metadata, including owner and mode, remains part of the independent source
seals; it is not part of the overlap equality key. Before any target write,
the physical anchor-to-`target_parent` descriptor traversal compares every
opened directory's physical identity key with both protected keys. Matching
`dev`, `ino`, and `kind` rejects immediately as `task_target_invalid`
regardless of any intervening owner or mode change. Mutable metadata must
never make two observations of the same filesystem object compare unequal
for overlap protection.

Component-aware `os.path.commonpath()` checks on the already physical source
root, `.git`, target parent, and two lexical `target_parent/name` planned roots
are retained only as a redundant early rejection. Identity comparison is
authoritative: string comparison alone is insufficient on case-insensitive or
Unicode-normalizing filesystems. Both gates precede `mkdir`, chmod, unlink, or
any other target mutation. Tests cover the source root, `.git`,
`.git/objects`, an arbitrary source subdirectory, and case/Unicode aliases
whose spellings differ but whose opened `(dev, ino, kind)` equals a source
ancestor.

The materialization transaction is:

1. create both top-level targets through the trusted parent descriptor with
   `mkdir(..., mode=0700, dir_fd=parent_fd)`; open each immediately with
   `O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`, fchmod it to exact `0700`,
   fstat it, and record ownership identity;
2. create all derived directories descriptor-relatively with initial mode
   `0700`, open/fstat-match them, and record their identities;
3. for every captured entry in canonical path order, create the corresponding
   file independently in both roots;
4. seal directories bottom-up, roots last, to exact `0555`;
5. independently scan and re-read both sealed trees, compare their canonical
   documents with the capture-derived expected document, and capture distinct
   root-identity documents;
6. only then construct one snapshot receipt and return the two snapshots.

Every file open is descriptor-relative and uses
`O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC` with an initial mode no
more permissive than `0600`. Immediately require a current-UID, same-device,
single-link regular `fstat()` identity. Write the complete retained bytes with
partial-write handling, `fchmod()` it to exact `0444` or `0555`, close it,
reopen it read-only through the verified parent descriptor, and re-read and
hash the complete bounded content. Require the same inode, exact final mode,
link count one, declared size, and content digest before accepting the file.

For every directory, compare its no-follow observation with its opened
descriptor identity and require current UID, the target-parent device, and no
unexpected link or special-file substitution. Final sealing performs
`fchmod(0555)` and `fstat()` for children bottom-up and the root last. The
trusted parent remains `0700`.

Phase A establishes logical state, not crash durability. It deliberately does
not call `fsync()` in the new task-snapshot file/directory materialization or
its ownership cleanup. This does not alter the already validated
`materialize_harness_home()` implementation, whose existing checkout helper
may retain internal `fsync()` calls. Those legacy calls do not make the Phase
A preflight receipt crash-durable. A successful receipt proves only the
logical cleanup state observed before return; it does not prove post-crash
absence. A host crash can expose residue under the caller-owned
`temp_parent/phase-a` even if a receipt had already been returned. After any
crash, the operator must identity-inspect `temp_parent` before reuse. If
execution is interrupted before a receipt is returned, no receipt is
observable. Durability for a future Phase B published artifact is a separate
design and acceptance boundary.

Final verification opens every directory and file through verified parent
descriptors with `O_NOFOLLOW`. It rejects a missing or extra entry, symlink,
hardlink, special file, owner/device change, wrong mode, path alias, limit
violation, size change, or content change. File bytes are bounded by their
expected size, re-read completely, and rehashed. Assembled child pathnames
must never be used to reopen a descendant.

The ledger stores component tuples and identity tokens, never persistent
descendant file descriptors. A traversal retains only the trusted parent,
the two top-level root descriptors when both are required, one current
descriptor stack, and at most one leaf file descriptor. It closes a stack
before walking the other condition. Across capture, pair materialization,
verification, rollback, and close, module-owned simultaneously open
descriptors above the caller's baseline are bounded by
`max_tree_depth + 8`. A peak-descriptor test instruments every successful
open and fault path and enforces that exact bound.

The two target-root path values, root inodes, and target-identity digests must
be distinct. Their materialized-tree documents, digests, counts, and snapshot
receipt are equal. A one-root success cannot be returned.

## 9. Verification, rollback, and cleanup ownership

The transaction maintains a private ownership ledger containing each created
entry's parent component tuple, basename, kind, and creation/acquisition
stable token with exact `dev`, `ino`, `uid`, `gid`, and kind. The ledger also
contains the expected component inventory, but no latest-mutation identity,
content copy, or descendant descriptor. Identity authority is captured at
creation/acquisition and is never invented by a cleanup-time scan. Only
entries created by this invocation enter it. The caller-owned parent and any
pre-existing or substituted entry never do.

On any failure after the first successful creation, rollback first performs
one read-only, descriptor-relative inspection of the entire owned pair:

1. reconstruct the exact expected inventory from the ledger and reject any
   missing or extra component;
2. require every observed entry's kind and stable token to match the recorded
   values, and every regular file to remain single-link;
3. if any observation, open, inventory, token, kind, or link check fails, do
   not chmod, unlink, or rmdir any entry in the pair; preserve the whole pair
   transaction and raise `task_snapshot_cleanup_required`;
4. only after the complete inspection passes, reopen directories top-down
   through verified parents, recheck each stable token, and `fchmod(0700)`;
5. delete entries bottom-up, files before directories, rechecking the stable
   token and regular-file link requirement immediately before each mutation.

The Task 8 final cleanup applies the same rule to the whole owned `phase-a`
tree. A pre-mutation mismatch or inspection failure preserves that entire
tree; it does not attempt “safe” partial cleanup of independent branches.
This conservative unit is simpler to audit and makes cleanup uncertainty
observable. No identity first observed during cleanup can authorize mutation,
and no replacement is made writable or removed.

An OS error or same-UID race after the complete inspection may still leave a
partially permission-opened or partially removed tree because POSIX provides
no atomic recursive deletion. Stop at the first such error and return
`task_snapshot_cleanup_required`; do not claim whole-tree preservation in
that case. If every owned entry is removed, raise the original sanitized
operation failure. A target that existed before the transaction is untouched.
No cleanup path calls `fsync()` or an unverified recursive deleter.

`verify(snapshot)` performs no write. It validates the exact snapshot and
publicly reconstructs its receipt, validates the target path with the physical
no-symlink rules, opens the root without following links, and requires its
current full identity document and digest to equal
`snapshot.target_identity_digest`. It then performs the complete bounded
inventory and byte re-read from Section 8, requiring the recomputed tree
digest, file count, and total bytes to equal both the snapshot fields and its
receipt payload. Success returns `None`; any mutation is
`task_target_changed`.

Successful verification does not prove that future execution will see an
unchanged tree. Task 8 binds content and policy digests, not this transient
root identity, into the stable invocation plan. A later Phase B launcher must
re-establish containment and bind the actual baseline, derived task root,
derived home, temporary root, and capability sets in versioned runtime
containment/child evidence immediately before reservation and launch.

## 10. Fixed public failures

Task 7 retains every Task 6 fixed public failure and adds exactly:

```text
task_capture_timeout
task_tree_invalid
task_tree_limit
task_blob_invalid
task_blob_limit
task_target_invalid
task_target_changed
task_snapshot_cleanup_required
task_snapshot_receipt_invalid
```

Use `task_tree_invalid` for malformed tree framing, modes, types, OIDs, sizes,
paths, aliases, conflicts, or exclusions. Use `task_tree_limit` for file,
trie-entry, depth, component-byte, and relative-path-byte caps. Use
`task_blob_limit` for declared per-file, logical-total, unique-count, or
unique-byte caps. Use `task_blob_invalid` for a blob length, recomputed Git
OID, or content projection mismatch. The existing Git error remains
authoritative for spawn, nonzero exit, generic output cap, operation-policy,
per-operation timeout, or cleanup failures unless a more specific dynamic
blob-cap or transaction-deadline rule above applies.

`task_target_invalid` covers bad caller target arguments, parent trust,
initial occupancy, creation, mode, or unsupported target operation before a
previously accepted snapshot exists. `task_target_changed` covers mutation
or mismatch of a created/returned target. Cleanup uncertainty always becomes
`task_snapshot_cleanup_required`.

Every public failure has exactly one fixed message. It contains no source or
target path, target name, config value, OID, tree record, stdout, stderr,
content byte, exception text, or receipt payload. Its `__context__` and
`__cause__` are both actually `None` when observed by the caller. This is an
outcome requirement, not a mandated control-flow pattern. An implementation
may use the existing slot-clearing `_fail` mechanism or record the fixed
classification and raise after leaving an active `except` block. `raise ...
from None` alone suppresses display but may retain `__context__`, so tests
inspect both exception attributes rather than accepting display suppression.

## 11. Task 8 handoff and allowed-write policy

Task 8 performs exactly one `prepare_task_source()`, one `capture()`, and one
`materialize_pair()` per selected task using a new single-task materializer.
It relies on the pair operation's independent full verification of both
roots, requires the two target identities to be distinct, and requires the
same snapshot receipt object and digest for the pair. It does not immediately
repeat `verify()`. A `finally` block always calls idempotent
`materializer.close()` and drops local captured/current/lean references before
starting the next task, including every blocked path.

The sealed `0444`/`0555` roots are immutable Phase A baselines. The
`workspace-write` value in a future pilot plan describes the intended Phase B
sandbox, not permission to hand one of these roots directly to a model or
chmod it writable for execution. Phase B must create a separate writable
derived root or copy-on-write overlay from a freshly verified baseline and
bind the baseline identity, derived-root identity, and exact derivation-policy
digest in a versioned child receipt before launch. It never chmods a baseline
to make it executable. Identity-aware final deletion may perform the
Section 9 verified chmod solely after baseline use has ended.

The candidate's `allowed_write_paths` are leaf capabilities, not directory
prefixes or globs. Revalidate their exact sorted/unique canonical-input shape
and the Task 7 path/component/alias/exclusion rules against the captured trie.
Each allowed path must be exactly one of:

- an existing regular file entry in the captured tree; or
- a missing final leaf whose immediate parent already exists as a captured
  directory (the root `.` counts as existing).

A directory, a missing intermediate component, a symlink/special concept, an
alias, an excluded path, or an existing/missing ambiguity fails plan
construction. The same classification must hold in both verified condition
roots.

For each task, construct this exact transient canonical document:

```json
{
  "allowed_write_paths": [
    {
      "disposition": "existing_regular_file",
      "path": "src/example.py"
    },
    {
      "disposition": "missing_leaf",
      "path": "src/new_file.py"
    }
  ],
  "document_type": "task-allowed-write-policy-v1",
  "materialized_tree_digest": "sha256:<64 lowercase hex>",
  "schema_version": 1,
  "snapshot_receipt_digest": "sha256:<64 lowercase hex>",
  "task_id": "task-id"
}
```

Records preserve the canonical input's path-UTF-8-byte order and have exactly
`path` and `disposition`. An empty list is valid. The policy document and its
derived dispositions are transient, but its repository-relative
`allowed_write_paths` remain durable authority in the authoritative
`CanonicalExperimentInput` already preserved and digest-bound by the
`ExperimentPlan`. Do not add a duplicate top-level path projection to
`plan_document`; that would create a second authority and unnecessary
consistency burden. Repository-relative
candidate paths are intentional durable policy, not host-local paths. No
absolute source, target, temporary, home, or other host-local path enters the
input, experiment plan, child plan, receipt, public result, or error.

`allowed_write_policy_digest = D(task-allowed-write-policy-v1 document)` is
the durable binding for this policy.

Task 8 replaces the transient-local fields in `PilotInvocationPlan`.
Immediately after `snapshot_receipt_digest`, the exact field order is:

```python
    allowed_write_policy_digest: str
    base_profile_digest: str
    root_capability_policy_digest: str
```

Remove `codex_home_identity_digest`, `task_root_identity_digest`,
`temp_root_identity_digest`, `tool_read_root_identity_digests`,
`tool_write_root_identity_digests`,
`validator_read_root_identity_digests`, and
`validator_write_root_identity_digests` from both the dataclass and exact
child document. `base_profile_digest` binds the condition's path-free harness
content, while `root_capability_policy_digest` binds the canonical rules for
the future runtime capability sets. The plan builder validates all three new
fields as exact SHA-256 digests, requires the two conditions for one task to
use the same allowed-write-policy digest, and requires each condition to use
its corresponding current or lean base-profile digest. It also builds a
reverse `allowed_write_policy_digest -> task_id` map and rejects reuse by any
different task. Mutation changes the child digest and therefore the final
experiment-plan digest.

The resulting Phase A plan is reproducible content/policy authority, not a
lease on a temporary inode. Equal canonical input, snapshot receipts, base
profile content, and policy documents produce the same plan digest when the
same captured task evidence is materialized into different trusted target
locations. Moving or reprovisioning a source repository may change its
source-trust receipt and is outside this stability claim. Actual baseline,
derived home, derived task root, temporary root identities, and the four
capability-root sets are bound by the future Phase B runtime
containment/child evidence immediately before reservation. That evidence must
bind the unchanged Phase A invocation plan digest and is the authority for the
local execution instance.

The existing future `pilot_reservation` payload already binds
`invocation_plan_digest`; it therefore transitively binds the content and
policy fields. Do not add an allowed-write path or a new field to the
`pilot_reservation` receipt. Phase B must first extend its versioned runtime
containment/child evidence to bind actual roots and capability sets, then
verify that evidence before reservation.

Before any future Phase B reservation, reload and revalidate the authoritative
canonical input bytes, require their recomputed digest to equal
`ExperimentPlan.input_digest`, recover the exact per-task relative paths,
rebuild `task-allowed-write-policy-v1` against the freshly verified baseline
and proposed derived namespace, require the digest to equal the selected pilot
invocation plan, and validate the Phase B runtime root/capability binding. A
stored digest without successful authoritative-input reload, fresh-tree
revalidation, and runtime containment evidence is insufficient.

### 11.1 Trusted Task 8 temporary parent

`ExperimentPreflightRequest` adds `temp_parent: Path = field(repr=False)`.
Its original `os.fspath()` result has exact type `str`, is absolute, equals
both `normpath()` and `realpath()`, and names an existing plain directory.
Validate it by the Task 6 physical no-symlink traversal, then require current
UID, exact mode `0700`, and an empty descriptor-relative scan. It is
caller-owned and is never removed or serialized.

Before creating `phase-a` or performing any other write, validate
`bundle_root`, `skill_repo`, every selected task source root, and each task
source's exact `.git` directory through physical no-symlink descriptor
traversal. Record each protected terminal's (`dev`, `ino`, `kind`) key and
the corresponding opened ancestor chain, including the terminal itself and
every opened ancestor through the filesystem anchor. Require physical ancestry
disjointness in both directions: no protected terminal may occur in the
`temp_parent` chain, and the `temp_parent` terminal may not occur in a
protected-root chain. The exact planned `temp_parent/phase-a` spelling also
passes the component-aware lexical check as redundant early rejection.
Identity is authoritative, so case-insensitive and Unicode-normalizing aliases
cannot bypass this gate. Any failure occurs before `mkdir`, harness
materialization, chmod, unlink, or another mutation.

Task 8 does not infer a parent from `TMPDIR`, `tempfile.gettempdir()`, `/tmp`,
or another ambient default. Through the retained trusted parent descriptor it
creates the exact initially absent leaf `phase-a` with mode `0700`, immediately
opens and `fstat()`-matches it, and records its creation-time identity before
creating any descendant. This owned leaf is the experiment temporary root.
Only one preflight may use a given empty parent at a time; an occupied parent
or pre-existing leaf fails closed without deletion.

Final cleanup removes the verified owned `phase-a` tree using the ledger
procedure below, `rmdir()`s that exact identity through the retained
`temp_parent` descriptor, and leaves the caller-owned empty parent in place.
The new Task 8 ownership ledger and final-cleanup layer does not `fsync()` its
temporary files, directories, or parents. Existing
`materialize_harness_home()` internals remain unchanged and may perform their
established `fsync()` calls while creating a harness home; they do not upgrade
the result to crash-durable evidence. This avoids relying on the root-owned
sticky `/tmp` entry deletion rules and behaves identically on Darwin and
Linux. A success receipt records the observed logical removal, not crash
persistence; after a host crash, the caller must inspect this parent before
reuse. Task 9 therefore requires a `--temp-parent` option and passes its exact
value into the request.

### 11.2 Preflight receipt and cleanup-failure result

There is exactly one valid preflight receipt outcome:

```text
evidence_state=static_only
live_backend_state=live_backend_not_implemented
global_agents_marker_state=global_agents_marker_not_run
pilot_state=pilot_not_run
qualification_evidence_classification=operator_attested_static
model_calls=0
materialization_result=verified
cleanup_state=removed
```

The existing `PreflightReceipt` constructor, validator, runtime replay, and
analysis projection remain success-only and continue to require
`materialization_result=verified` and `cleanup_state=removed`. Task 8 makes no
production schema change to `experiment_receipts.py`.

If final cleanup cannot safely remove every owned entry, construct no
`PreflightReceipt`. The public result retains every already completed,
path-free plan/content digest represented by its result fields and requires:

```text
status=blocked
materialization_result=blocked
cleanup_state=cleanup_required
model_calls=0
preflight_receipt_digest=None
reason_code=task_snapshot_cleanup_required
```

Qualification remains `operator_attested_static` when the final plan was
completed. A failure before that point has nullable not-yet-computed digests
and no preflight receipt as before. No blocked result can root runtime
history because it has no receipt digest.

Task 8 records the temporary root and every descendant stable token when each
entry is created or first acquired. For a subtree returned by a trusted
materializer API, acquisition is one immediate bounded descriptor scan that
records its exact expected inventory and stable tokens. Final cleanup uses the
exact Section 9 whole-tree pre-inspection, top-down permission opening, and
bottom-up deletion within the same descriptor bound.
It never uses a cleanup-time inventory as ownership authority and never
chmods or removes a replacement. Production orchestration must not use
`TemporaryDirectory.cleanup()`, its context-manager exit, a finalizer, or any
other recursive best-effort deleter; it owns an explicitly created private
root whose only deletion path is this ledger procedure.

Task 8 modifies and tests `experiment_plan.py` and creates the orchestrator.
`tests/test_live_eval_experiment_receipts.py` may require fixture updates for
the amended `PilotInvocationPlan`, but production
`experiment_receipts.py` is unchanged. Source-trust receipts remain one per
task, snapshot receipts remain one per task rather than one per condition,
and pilot invocation plans remain one per scheduled condition with stable
content/policy bindings rather than local baseline identities.

## 12. Required TDD and checkpoint

Use table-driven tests for:

- SHA-1, SHA-256, and empty-tree captures;
- exact commit equality, annotated-tag rejection, exact tree output,
  formatted-tree framing, successful empty stderr, and every operation count
  and order;
- transaction-versus-operation deadlines, spawn-inclusive timing, dynamic
  zero/exact/plus-one blob caps, exact amended process-policy document,
  empty-success-stderr binding, process-group cleanup, post-return
  slow-filesystem timeout observation, and no receipt on capture failure;
- every accepted mode plus tree, gitlink, symlink, special, malformed,
  traversal, absolute, control/format Unicode, normalization, trailing
  dot/space, duplicate, casefold alias, sibling alias, parent/file conflict,
  depth, path-byte, trie-entry, file-count, distinct-blob, per-file, logical,
  and unique-byte boundary;
- every exclusion at its exact depth scope, including aliases, plus allowed
  nested ordinary `hooks` and `plugins` source directories and explicit
  unlisted-secret residual behavior;
- one fetch per unique OID, repeated blobs, short/long bytes, missing object,
  SHA-1/SHA-256 Git object rehash mismatch, and SHA-256 content mismatch;
- exact canonical entry, materialized-tree, root-identity, source-trust, and
  snapshot documents, including the empty tree;
- receipt reconstruction through public `make_receipt`, forged dataclasses,
  string/int/bool subclasses, mutable inputs, and null exception context and
  cause;
- parent/name validation, pre-existing targets, exclusive/no-follow writes,
  source-root/`.git`/`.git/objects`/source-subdirectory overlap rejection
  before write, including case/Unicode filesystem aliases, partial writes,
  overlap rejection after mutable source mode/owner metadata changes,
  write/close/reopen/hash verification, pair equality, distinct root
  identity, same receipt object, and verification after extra, missing,
  replaced, linked, special, mode-changed, size-changed, or content-changed
  entries;
- representative create, write, chmod, reopen/read/hash, verification,
  receipt, inventory-mismatch, replacement, symlink, hardlink, special-entry,
  permission, unlink, and rmdir failures; creation/acquisition-time ownership
  authority; whole-pair or whole-`phase-a` preservation on pre-mutation
  mismatch; cleanup-error precedence; no recursive `TemporaryDirectory`
  cleanup; peak open descriptors no greater than `max_tree_depth + 8`; and no
  double close. Do not freeze every syscall position as a test contract;
- simple `new -> captured -> paired -> closed` transitions, exact captured
  object identity, coherent in-place mutation and validation-to-use race
  detection through the private detached operational seal, full semantic
  mutation detection, repeatable stateless `verify()`, no immediate Task 8
  duplicate verification, idempotent close from every state, and
  strong-reference/seal release;
- allowed-write existing-file, missing-leaf, empty-list, directory,
  missing-intermediate, exclusion, alias, pair-equality, reverse digest/task
  uniqueness, digest mutation, authoritative relative paths retained once in
  canonical input and preserved/digest-bound by the plan without a duplicate
  projection, absence of absolute or host-local paths from every durable
  projection, stable plan digests across equal rematerializations, and Phase B
  binding of actual root identities and capability sets before reservation;
- success-only `PreflightReceipt` validation and replay; cleanup failure with
  completed path-free plan/content digests, `preflight_receipt_digest=None`,
  and no possible runtime-history root;
- pre-write physical ancestry disjointness between the trusted temporary
  parent and bundle, skill, every selected task source, and every selected
  `.git`, including case/Unicode aliases and both ancestry directions;
- receipt semantics limited to observed logical cleanup, plus mandatory
  identity inspection of the caller-owned temporary parent after a host
  crash;
- sealed-baseline immutability and the requirement that future workspace
  writes use a separately identified derived root or overlay.

The Task 7 checkpoint is:

```bash
python3 -m unittest tests.test_live_eval_task_snapshot -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
python3 -m py_compile scripts/live_eval/task_snapshot.py
git diff --check
```

Task 8 additionally runs:

```bash
python3 -m unittest tests.test_live_eval_experiment_plan -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_live_eval_experiment -v
```

An independent security/filesystem reviewer and an independent
contract/efficiency reviewer must inspect the implementation, test evidence,
receipt timing, per-unique process cost, rollback semantics, and residual
same-UID, memory-zeroization, unlisted-secret, and synchronous-filesystem
latency limitations before Task 7 is accepted.
