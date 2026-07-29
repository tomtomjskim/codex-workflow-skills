# Native Canary Readiness Phase B0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task-by-task.
> Each implementation task must use `superpowers:test-driven-development`;
> each completed task receives a requirements review and a code-quality review
> before the next task starts.

**Goal:** Implement the approved zero-model-call Phase B0 command that records
host-specific native permission-profile, supervisor-environment, and private
JSONL storage observations without resolving authentication, invoking a model,
or provisioning a VM.

**Architecture:** Add one narrow cooperative JSONL ledger primitive, one
native-readiness library with immutable policy and injectable bounded-command
seam, and one thin CLI. The library runs a direct weak control and a named
permission-profile strong candidate against owned sentinels, exercises the
physical ledger, cleans temporary state, and only then publishes path-free
private evidence and a bounded public result. A passing result is
`native_primitive_observations_only`; it is not containment proof or
authorization for the future two-call canary.

**Tech Stack:** Python 3.9 standard library, existing canonical JSON helpers,
`unittest`, macOS Codex CLI 0.145.0, GitHub Actions. The legacy redacting
artifact writer is a reviewed implementation pattern only; this slice does not
import it because its finalize-only lifecycle cannot express the approved
nonterminal-to-terminal evidence transition.

**Design source:**
`docs/superpowers/specs/2026-07-29-native-canary-readiness-design.md`

## Global Constraints

- Phase B0 performs exactly zero model calls and never reads authentication.
- Do not invoke or import `codex exec`, `preflight_auth()`, model/provider
  clients, MCP, plugins, hooks, skills, browser, computer use, or evaluators.
- Do not create `RuntimeContainmentReceipt`, reservation, terminal, marker, or
  pilot state.
- Do not install Lima or introduce a VM/backend abstraction.
- Do not parse `codex features list`; it is not a live tool inventory.
- Do not modify the schemas or public behavior of Phase A or the legacy live
  runner.
- Add no production dependency. Use Python 3.9-compatible syntax only.
- Keep public and retained evidence free of host paths, sentinel content,
  synthetic secret values, command output, and credentials.
- Treat CLI output, child output, path state, and ledger bytes as untrusted.
- Use immutable public dataclasses and fixed reason/status values.
- The command must fail before subprocess creation or evidence writing when the
  public request is invalid.
- A cleanup failure must override a capability success. No retained artifact may
  claim success before owned temporary state is removed.
- A passing Phase B0 observation remains necessary but insufficient for paid
  canary work.

## File and Dependency Map

| File | Responsibility | Direct repository dependency |
|---|---|---|
| `scripts/live_eval/private_jsonl_ledger.py` | Owned directory-fd-relative cooperative JSONL append primitive | `scripts.workflow_coordination.canonical_json` |
| `scripts/live_eval/native_canary_readiness.py` | Immutable policy, executable identity, bounded weak/strong probes, ledger sentinel, cleanup, canonical evidence projection | `private_jsonl_ledger`, `canonical_json` |
| `scripts/run_harness_canary_readiness.py` | Strict argument parsing, orchestration call, bounded JSON, exit code | `native_canary_readiness` |
| `tests/test_live_eval_private_jsonl_ledger.py` | Physical ledger contract and corruption tests | module under test |
| `tests/test_live_eval_native_canary_readiness.py` | Policy, runner seam, permission attribution, cleanup, redaction, poison pills | module under test |
| `tests/test_run_harness_canary_readiness.py` | CLI argument/result/exit behavior | CLI and module under test |
| `tests/test_repository_validation.py` | Durable file/dependency/poison-pill gates | repository sources |
| `scripts/validate_repo.sh` | Require the Phase B0 source and test files | none |

## Exact Public Contracts

### Private ledger

The primitive exports these exact public values:

```python
class PrivateJSONLLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrivateJSONLLedgerState:
    record_count: int
    total_bytes: int
    last_record_hash: str
```

`PrivateJSONLLedger` has the class method
`open(directory, genesis_digest, *, max_records=64, max_bytes=262144,
max_record_bytes=16384)`, the read-only `state` property, context-manager
methods, `append(record_bytes)`, and idempotent `close()`. `open()` and
`append()` return `PrivateJSONLLedger` and `PrivateJSONLLedgerState`
respectively.

`open()` creates the fixed `runtime.jsonl` and `runtime.lock` pair when both are
absent, or reopens them when both exist. A one-file-only state is invalid.
`genesis_digest`, each `previous_record_hash`, and each computed line hash use
the repository receipt form `sha256:<64 lowercase hex>`.

### Native readiness

The library exports immutable request, result, and test seam values:

```python
@dataclass(frozen=True)
class NativeCanaryReadinessRequest:
    codex_executable: Path
    python_executable: Path
    temp_parent: Path
    private_root: Path


@dataclass(frozen=True)
class BoundedCommand:
    argv: Tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    timeout_seconds: int
    stdout_limit: int
    stderr_limit: int


@dataclass(frozen=True)
class BoundedCommandResult:
    returncode: Optional[int]
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_overflow: bool


@dataclass(frozen=True)
class NativeCanaryReadinessResult:
    status: str
    model_calls: int
    policy_digest: Optional[str]
    codex_executable_identity_digest: Optional[str]
    python_executable_identity_digest: Optional[str]
    permission_profile_evidence_digest: Optional[str]
    supervisor_environment_evidence_digest: Optional[str]
    ledger_probe_evidence_digest: Optional[str]
    complete_evidence_digest: Optional[str]
    cleanup_state: str
    reason_code: str


```

The function
`run_native_canary_readiness(request, *, command_runner=None)` returns
`NativeCanaryReadinessResult`; an injected runner has the exact callable shape
`Callable[[BoundedCommand], BoundedCommandResult]`.

The public wrapper always supplies the production policy and
`platform.system().lower()`. A module-private `_run_with_policy()` core accepts
an immutable policy and observed platform so Linux CI can use generated local
executables with known hashes. This core is not test-only behavior: it is the
dependency-free evaluator used by the production wrapper. The CLI imports only
the fixed-policy public wrapper.

`NativeCanaryReadinessResult.__post_init__()` enforces fixed fields and
relationships. `status` is `native_primitive_observations_only` or `blocked`;
`model_calls` is exactly integer `0`; every non-null digest matches
`sha256:[0-9a-f]{64}`; executable digests appear as a pair; permission and
supervisor evidence digests appear as a pair; ledger evidence requires those
four prerequisite digests; and complete evidence requires a policy digest plus
a terminal cleanup state.

Digest masks use this order:

```text
policy, codex executable, Python executable, permission profile,
supervisor environment, ledger probe, complete evidence
```

The exact terminal combinations are:

| `reason_code` | `status` | `cleanup_state` | allowed digest mask |
|---|---|---|---|
| `native_primitive_observations_recorded` | `native_primitive_observations_only` | `removed` | `1111111` |
| `request_invalid` | `blocked` | `not_started` | `1000000` |
| `unsupported_platform` | `blocked` | `not_started` | `1000000` |
| `executable_identity_invalid` | `blocked` | `not_started` or `removed` | `1000000` |
| `cli_version_mismatch` | `blocked` | `removed` | `1110000` |
| `weak_control_invalid` | `blocked` | `removed` | `1110000` |
| `native_permission_unproven` | `blocked` | `removed` | `1110000` |
| `ledger_primitives_unproven` | `blocked` | `removed` | `1111100` |
| `evidence_retention_failed` | `blocked` | `removed` | `1111110` |
| `cleanup_required` | `blocked` | `cleanup_required` | `1000000`, `1110000`, `1111100`, `1111110`, or `1111111` |

No other reason, state, cleanup, or digest-mask combination is valid.

`cleanup_state` reuses the Phase A vocabulary:

- `not_started` when validation fails before temporary state exists;
- `removed` when every owned temporary path was removed;
- `cleanup_required` when identity-aware cleanup failed.

`command_runner=None` selects the real bounded subprocess implementation.
Tests inject the callable; production callers cannot alter the immutable
policy. The public result contains only the listed fields and fixed values.

The production policy binds:

```python
CODEX_VERSION = "0.145.0"
CODEX_SHA256 = "6db9193ce2c9a8cef2b5482612cde24202a4329dfc34f4687a036d5d7da619af"
PYTHON_VERSION = "3.9.6"
PYTHON_SHA256 = "6b24be8ba00c4abc8b3e7e2620bd710e2cf7a4312d00c147f8f05fbb6f3d297a"
PERMISSION_PROFILE_NAME = "phase-b0-native-readonly"
```

The canonical policy document has exactly these top-level keys and values:

```json
{
  "argv_templates": {
    "codex_version": ["{codex_executable}", "--version"],
    "python_version": ["{python_executable}", "--version"],
    "strong_probe": ["{codex_executable}", "sandbox", "-P", "phase-b0-native-readonly", "-C", "{allowed_root}", "--", "{python_executable}", "-I", "-S", "-B", "-c", "{child_source}", "{allowed_read}", "{forbidden_read}", "{allowed_write}", "{forbidden_write}", "{loopback_port}", "{token_hex}"],
    "weak_probe": ["{python_executable}", "-I", "-S", "-B", "-c", "{child_source}", "{allowed_read}", "{forbidden_read}", "{allowed_write}", "{forbidden_write}", "{loopback_port}", "{token_hex}"]
  },
  "child": {
    "bytes_length": 2489,
    "result_schema_version": 1,
    "sha256": "sha256:aa1e8614fdd267c2cd0bbf237f1ed8f3a039e6a1f9de416cef5c52f3c520e6a9",
    "synthetic_secret_key": "PHASE_B0_SYNTHETIC_SECRET"
  },
  "command_contexts": {
    "codex_version": {
      "cwd_class": "owned_allowed_root",
      "environment_template": "strong"
    },
    "python_version": {
      "cwd_class": "owned_allowed_root",
      "environment_template": "strong"
    },
    "strong_probe": {
      "cwd_class": "owned_allowed_root",
      "environment_template": "strong"
    },
    "weak_probe": {
      "cwd_class": "owned_allowed_root",
      "environment_template": "weak"
    }
  },
  "environment_templates": {
    "strong": {
      "CODEX_HOME": "{codex_home}",
      "HOME": "{codex_home}",
      "LANG": "C",
      "LC_ALL": "C",
      "PATH": "/usr/bin:/bin",
      "TMPDIR": "{tmp}"
    },
    "weak": {
      "CODEX_HOME": "{codex_home}",
      "HOME": "{codex_home}",
      "LANG": "C",
      "LC_ALL": "C",
      "PATH": "/usr/bin:/bin",
      "PHASE_B0_SYNTHETIC_SECRET": "{synthetic_secret}",
      "TMPDIR": "{tmp}"
    }
  },
  "evidence": {
    "artifact_name": "readiness.json",
    "max_bytes": 65536,
    "schema_version": 1
  },
  "executables": {
    "codex": {
      "expected_mode": "0755",
      "expected_owner": "operator_uid",
      "sha256": "sha256:6db9193ce2c9a8cef2b5482612cde24202a4329dfc34f4687a036d5d7da619af",
      "version_line": "codex-cli 0.145.0"
    },
    "python": {
      "expected_mode": "0755",
      "expected_owner": "uid:0",
      "sha256": "sha256:6b24be8ba00c4abc8b3e7e2620bd710e2cf7a4312d00c147f8f05fbb6f3d297a",
      "version_line": "Python 3.9.6"
    }
  },
  "ledger": {
    "ledger_name": "runtime.jsonl",
    "lock_name": "runtime.lock",
    "max_bytes": 262144,
    "max_record_bytes": 16384,
    "max_records": 64
  },
  "limits": {
    "command_timeout_seconds": 10,
    "executable_hash_chunk_bytes": 1048576,
    "max_executable_bytes": 536870912,
    "stderr_bytes": 8192,
    "stdout_bytes": 8192
  },
  "permission_profile": {
    "config_bytes_length": 284,
    "config_sha256": "sha256:74f108e8c6df73d7c045d5dd5453f795916a3b394b2c968dc7b63994fb41764a",
    "name": "phase-b0-native-readonly"
  },
  "platform_family": "darwin",
  "policy_schema_version": 1
}
```

`policy_digest` is `sha256_id()` of that exact document and therefore equals
`sha256:af3a5979f7799bdf27f8ec100352a458b47e01464753d9f76e8f6826639e777c`.
The implementation asserts at construction that the literal config and child
byte lengths and digests equal the document; a changed literal cannot silently
reuse the policy identity.

The exact child bytes are the UTF-8 bytes of the following source, including
the final LF:

```python
import hashlib
import json
import os
import socket
import sys

def read_file(path):
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        data = os.read(descriptor, 4097)
        if len(data) > 4096:
            return {"digest": None, "errno": None, "ok": False, "overflow": True}
        return {"digest": hashlib.sha256(data).hexdigest(), "errno": None, "ok": True, "overflow": False}
    except OSError as error:
        return {"digest": None, "errno": error.errno, "ok": False, "overflow": False}
    finally:
        if descriptor >= 0:
            os.close(descriptor)

def write_file(path, content):
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("write made no progress")
            offset += written
        os.fsync(descriptor)
        return {"errno": None, "ok": True}
    except OSError as error:
        return {"errno": error.errno, "ok": False}
    finally:
        if descriptor >= 0:
            os.close(descriptor)

def connect_loopback(port, content):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(2)
        client.connect(("127.0.0.1", port))
        client.sendall(content)
        return {"errno": None, "ok": True}
    except OSError as error:
        return {"errno": error.errno, "ok": False}
    finally:
        client.close()

if len(sys.argv) != 7:
    raise SystemExit(64)
allowed_read, forbidden_read, allowed_write, forbidden_write, raw_port, raw_token = sys.argv[1:]
token = bytes.fromhex(raw_token)
secret = os.environ.get("PHASE_B0_SYNTHETIC_SECRET")
result = {
    "allowed_read": read_file(allowed_read),
    "allowed_write": write_file(allowed_write, token),
    "environment": {
        "digest": hashlib.sha256(secret.encode("utf-8")).hexdigest() if secret is not None else None,
        "present": secret is not None,
    },
    "forbidden_read": read_file(forbidden_read),
    "forbidden_write": write_file(forbidden_write, token),
    "network": connect_loopback(int(raw_port), token),
    "schema_version": 1,
}
sys.stdout.buffer.write(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
```

The exact config bytes are the TOML block in the approved design, including its
final LF. Their fixed digest is
`sha256:74f108e8c6df73d7c045d5dd5453f795916a3b394b2c968dc7b63994fb41764a`.
Version commands accept exactly one line matching the policy's `version_line`,
with a single optional trailing LF and no other stdout/stderr bytes.

### CLI

The CLI accepts only:

```text
--codex-executable PATH
--python-executable PATH
--temp-parent PATH
--private-root PATH
```

It prints one compact sorted JSON object plus LF. Exit code is `0` only for
`native_primitive_observations_only`; all `blocked` and sanitized input
failures exit `2`; an operator interrupt exits `130`.

---

## Task 1: Cooperative Private Canonical JSONL Ledger

**Files:**

- Create: `scripts/live_eval/private_jsonl_ledger.py`
- Create: `tests/test_live_eval_private_jsonl_ledger.py`

### Step 1: Write failing lifecycle and canonicality tests

- [ ] Add tests for a fresh owned mode-0700 directory:
  - initial empty state uses the configured genesis digest;
  - canonical records append with LF and update count, bytes, and line hash;
  - close/reopen validates the complete history and appends from the prior head;
  - context-manager close is idempotent;
  - wrong `previous_record_hash`, noncanonical JSON, duplicate keys, float,
    non-object JSON, bool limits, oversized record, byte cap, and count cap fail
    without changing the ledger bytes.

Use this record shape:

```python
def record(previous_record_hash, sequence):
    return canonical_bytes(
        {
            "document_type": "phase_b0_ledger_sentinel",
            "previous_record_hash": previous_record_hash,
            "schema_version": 1,
            "sequence": sequence,
        }
    )
```

### Step 2: Run the focused test and observe the intended red state

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_private_jsonl_ledger -v
```

Expected: import failure because
`scripts.live_eval.private_jsonl_ledger` does not yet exist.

### Step 3: Implement trusted directory and fixed-file creation

- [ ] Validate a canonical absolute non-symlink directory owned by the current
  UID with exact mode `0700`.
- [ ] Retain an `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` directory fd.
- [ ] Create both fixed files descriptor-relative with
  `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, force mode `0600`, validate regular
  file/owner/mode/single-link identity, and fsync the directory.
- [ ] Reopen an existing ledger with
  `O_RDWR|O_APPEND|O_NOFOLLOW|O_CLOEXEC`; open the existing lock without
  truncation.
- [ ] Acquire `fcntl.flock(fd, LOCK_EX|LOCK_NB)` on the lock and retain it for
  object lifetime.
- [ ] Reject a mixed missing/existing pair rather than repairing it.

### Step 4: Implement bounded history validation and append

- [ ] Read through the locked ledger fd with bounded complete reads.
- [ ] Require non-empty existing lines to end in LF.
- [ ] For each raw line, require:

```python
parsed = load_canonical_input(raw_line)
canonical_bytes(parsed) == raw_line
isinstance(parsed, dict)
set(parsed).issuperset({"previous_record_hash"})
parsed["previous_record_hash"] == expected_hash
```

- [ ] Compute the next expected hash as:

```python
"sha256:" + hashlib.sha256(raw_line).hexdigest()
```

- [ ] Before and after append, verify retained fd identity and the fixed path
  identity match the originally opened `(st_dev, st_ino)` and trust metadata.
- [ ] Append `record_bytes + b"\n"` using a complete-write loop, then
  `os.fsync(ledger_fd)`. If a write or fsync fails, close and poison the object;
  do not claim rollback or repair.

### Step 5: Add physical adversarial tests

- [ ] Add tests for:
  - partial tail, wrong historical hash, wrong file/dir mode;
  - ledger or lock symlink and hardlink;
  - ledger path replacement while the original object remains open;
  - a real second Python process failing nonblocking lock acquisition;
  - mocked `os.fsync` observation proving file append sync and creation-time
    directory sync;
  - no claim that close/reopen detects a fully rewritten valid history.

### Step 6: Run focused validation

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_private_jsonl_ledger -v
python3 -m compileall -q scripts/live_eval/private_jsonl_ledger.py
```

Expected: all ledger tests pass and compilation emits no output.

### Step 7: Commit Task 1

- [ ] Commit:

```bash
git add scripts/live_eval/private_jsonl_ledger.py \
  tests/test_live_eval_private_jsonl_ledger.py
git commit -m "feat(eval): add private canonical JSONL ledger"
```

---

## Task 2: Native Permission and Environment Observation Library

**Files:**

- Create: `scripts/live_eval/native_canary_readiness.py`
- Create: `tests/test_live_eval_native_canary_readiness.py`

### Step 1: Write failing policy, request, and poison-pill tests

- [ ] Assert exact config bytes, profile name, executable hashes, versions,
  child bytes, supervisor keys, limits, schema, and policy digest stability.
- [ ] Assert the module source and every constructed command contain no
  `codex exec`, auth lookup, provider/model call, external destination,
  feature-list command, runtime receipt, marker, or VM operation.
- [ ] Assert invalid/noncanonical/aliased paths, nonempty temp parent, wrong
  ownership/mode, symlink, or overlapping roots return `request_invalid` before
  the injected runner or artifact writer is called.
- [ ] Assert unsupported platforms fail before any invocation.
- [ ] Exercise successful and blocked orchestration in Linux CI through the
  module-private evaluator using generated regular executables and an immutable
  test policy whose expected hashes match those exact bytes. Do not patch out
  identity validation.

### Step 2: Run the focused test and observe the intended red state

- [ ] Run:

```bash
python3 -m unittest tests.test_live_eval_native_canary_readiness -v
```

Expected: import failure because
`scripts.live_eval.native_canary_readiness` does not yet exist.

### Step 3: Implement immutable policy and executable sealing

- [ ] Define the exact TOML bytes from the approved design as one module
  constant ending in LF.
- [ ] Define one constant Python child program that performs the six sentinel
  operations and emits exactly one canonical JSON object with fixed keys.
- [ ] Build the policy digest from content, not host paths.
- [ ] Keep the public wrapper fixed to the production policy. Route it through
  one module-private evaluator that receives immutable policy and observed
  platform values, allowing deterministic CI without weakening the CLI.
- [ ] Open each supplied executable without following the final symlink,
  require canonical absolute paths, regular file, single link, exact owner/mode,
  expected SHA-256, and expected `--version` projection.
- [ ] Keep descriptor identities and recheck fd, path identity, and content
  immediately before and after every command.
- [ ] Classify any executable mismatch as
  `executable_identity_invalid`; classify an exact Codex version mismatch as
  `cli_version_mismatch`.

### Step 4: Implement one bounded subprocess adapter

- [ ] Start a new process group with exact argv/cwd/environment and no inherited
  stdin.
- [ ] Enforce one wall-clock deadline and independent stdout/stderr byte caps.
- [ ] On timeout or overflow, terminate then kill the process group within
  bounded grace periods and drain/close pipes.
- [ ] Return only `BoundedCommandResult`; never interpolate argv, paths, child
  bytes, stdout, or stderr into errors or public results.
- [ ] Reject malformed runner results and extra child fields fail closed.

### Step 5: Implement weak and strong observations

- [ ] Create one fresh owned mode-0700 hierarchy under the validated empty
  temp parent, with sentinel files opened using no-follow and recorded tokens.
- [ ] Keep an owned loopback listener ready for the complete weak attempt and
  require its token-matched connection.
- [ ] Run the weak child directly with a synthetic environment sentinel. Require
  allowed and forbidden reads, both writes, loopback, and environment
  visibility all to succeed.
- [ ] The weak environment is the same six-key sanitized map used by the strong
  attempt plus exactly one `PHASE_B0_SYNTHETIC_SECRET` entry. Never inherit the
  caller's ambient environment.
- [ ] Remove only token-matched weak output files, recheck original sentinels,
  and start a fresh same-class listener for the strong attempt.
- [ ] Write the exact config to the fresh owned `codex-home` and run:

```text
<codex> sandbox -P phase-b0-native-readonly -C <allowed> -- \
  <python> -I -S -B -c <constant-child-source> <sentinel-arguments>
```

- [ ] Use exactly this strong supervisor environment:

```python
{
    "CODEX_HOME": str(codex_home),
    "HOME": str(codex_home),
    "PATH": "/usr/bin:/bin",
    "TMPDIR": str(tmp),
    "LANG": "C",
    "LC_ALL": "C",
}
```

- [ ] Require strong allowed read success, forbidden read and both writes to
  fail, and the environment sentinel to be absent.
- [ ] Accept a network denial only when the child reports `EACCES` or `EPERM`
  while the listener remained ready and recorded no token-matched connection.
  Refused, timeout, reset, endpoint mismatch, listener failure, or any other
  socket result is `native_permission_unproven`.
- [ ] Record supervisor sanitization separately from permission-profile
  observations.

### Step 6: Exercise the private ledger and terminal evidence flow

- [ ] Create two canonical synthetic records using the policy digest as genesis,
  append, close, reopen, and append again.
- [ ] Run one real-process nonblocking lock-contention check.
- [ ] In separate owned probe subdirectories, require runtime rejection of:
  ledger-path replacement during one open object lifetime, an unterminated
  partial tail, a wrong `previous_record_hash`, wrong directory/file mode, a
  ledger or lock hardlink, and a ledger or lock symlink. None of these
  corruption sentinels may modify the accepted append/reopen ledger.
- [ ] Record `append_fsync_returned`, `creation_directory_fsync_returned`, and
  `normal_reopen_validated` as operation observations. These fields describe
  successful syscall returns and normal close/reopen integrity only; they may
  not be named or consumed as crash or power-loss durability.
- [ ] Reduce observations to canonical path-free documents and digest those
  documents.
- [ ] Use one module-private, purpose-built evidence publisher rather than
  extending the legacy finalization-only `RedactingWriter`. It opens the
  validated private root by directory fd, creates one fresh mode-0700 run
  directory, and uses only exclusive no-follow mode-0600 staging files.
- [ ] Before temporary cleanup, atomically publish canonical
  `readiness.json` with `status="blocked"`,
  `reason_code="cleanup_required"`, and `cleanup_state="pending"`. Fsync the
  file and run directory.
- [ ] After cleanup, atomically replace that same path with one canonical
  terminal document. Success may appear only here. A cleanup failure replaces
  it with terminal `blocked`/`cleanup_required`; it never retains a success
  document.
- [ ] Reparse retained bytes with `load_canonical_input()` and require
  `canonical_bytes(value) == retained_bytes` before reporting its digest.
- [ ] If staging or terminal replacement fails, return
  `blocked`/`evidence_retention_failed`; perform token-matched cleanup only and
  do not expose the private path.

The success result is exactly:

```python
NativeCanaryReadinessResult(
    status="native_primitive_observations_only",
    model_calls=0,
    policy_digest=policy_digest,
    codex_executable_identity_digest=codex_executable_identity_digest,
    python_executable_identity_digest=python_executable_identity_digest,
    permission_profile_evidence_digest=permission_profile_evidence_digest,
    supervisor_environment_evidence_digest=supervisor_environment_evidence_digest,
    ledger_probe_evidence_digest=ledger_probe_evidence_digest,
    complete_evidence_digest=complete_evidence_digest,
    cleanup_state="removed",
    reason_code="native_primitive_observations_recorded",
)
```

### Step 7: Add classification and redaction tests

- [ ] Test every fixed reason code with an injected runner or controlled
  filesystem fault.
- [ ] Mutate every result axis and prove `__post_init__()` rejects unknown
  reasons, bool/nonzero `model_calls`, wrong status, wrong cleanup vocabulary,
  `cleanup_required` without its matching reason, that reason without its
  matching cleanup, and every digest mask not listed in the terminal table.
- [ ] Test weak-insensitive controls, malformed JSON, wrong child keys, timeout,
  signal, output overflow, false network causes, listener races, executable
  replacement, ledger failure, cleanup failure, and evidence failure.
- [ ] Search both public JSON and retained bytes for all input paths, sentinel
  values, synthetic secret, stdout/stderr contents, and credential-like tokens.
- [ ] Assert all outcomes, including internal exceptions, report
  `model_calls == 0`.

### Step 8: Run focused validation

- [ ] Run:

```bash
python3 -m unittest \
  tests.test_live_eval_private_jsonl_ledger \
  tests.test_live_eval_native_canary_readiness -v
python3 -m compileall -q \
  scripts/live_eval/private_jsonl_ledger.py \
  scripts/live_eval/native_canary_readiness.py
```

Expected: all focused tests pass and compilation emits no output.

### Step 9: Commit Task 2

- [ ] Commit:

```bash
git add scripts/live_eval/native_canary_readiness.py \
  tests/test_live_eval_native_canary_readiness.py
git commit -m "feat(eval): observe native canary readiness"
```

---

## Task 3: Thin CLI and Operator Documentation

**Files:**

- Create: `scripts/run_harness_canary_readiness.py`
- Create: `tests/test_run_harness_canary_readiness.py`
- Modify: `README.md`
- Modify: `tests/test_repository_validation.py`
- Modify: `scripts/validate_repo.sh`

### Step 1: Write failing CLI tests

- [ ] Test exact arguments, missing/unknown argument rejection, success/blocked
  exit codes, one compact sorted JSON line, no traceback, and no path leakage.
- [ ] Patch only `run_native_canary_readiness`; do not run the real local probe
  in unit tests.

### Step 2: Run the focused test and observe the intended red state

- [ ] Run:

```bash
python3 -m unittest tests.test_run_harness_canary_readiness -v
```

Expected: import failure because `scripts.run_harness_canary_readiness` does
not yet exist.

### Step 3: Implement the thin CLI

- [ ] Follow the existing direct-script import bootstrap pattern.
- [ ] Convert arguments to `Path`, construct the immutable request, call the
  library exactly once, serialize `dataclasses.asdict(result)` with
  `sort_keys=True`, compact separators, `ensure_ascii=False`, and LF.
- [ ] Return `0` only for
  `status == "native_primitive_observations_only"`; return `2` for blocked or
  sanitized CLI failure and `130` for `KeyboardInterrupt`.
- [ ] Do not add debug, verbose, retry, live, auth, backend, or VM flags.

### Step 4: Add the minimum README operator section

- [ ] Document Phase B0 as a host-specific zero-call observation command.
- [ ] Include the exact invocation using explicit paths.
- [ ] State that it performs no authentication/model call/external network,
  that success is not live containment proof, and that VM fallback remains a
  separately approved choice.
- [ ] Link the approved design and implementation plan.

### Step 5: Add durable repository gates

- [ ] Require the three new source files and three focused test files exactly
  once in `scripts/validate_repo.sh`.
- [ ] Add a Phase B0 direct-import allowlist in
  `tests/test_repository_validation.py` matching the dependency table above.
- [ ] Add AST/source gates that reject auth/model/live-runner imports,
  `codex exec`, feature-list execution, external network destinations,
  runtime-receipt construction, and VM commands from the Phase B0 path while
  allowing the explicitly bounded `subprocess` and loopback `socket` seams.
- [ ] Test the gates with small source mutations so a future bypass is detected
  rather than merely asserting the current source happens to be clean.

### Step 6: Run focused validation

- [ ] Run:

```bash
python3 -m unittest \
  tests.test_live_eval_private_jsonl_ledger \
  tests.test_live_eval_native_canary_readiness \
  tests.test_run_harness_canary_readiness \
  tests.test_repository_validation -v
python3 scripts/run_harness_canary_readiness.py --help
```

Expected: all tests pass; help lists only the four approved path arguments.

### Step 7: Commit Task 3

- [ ] Commit:

```bash
git add scripts/run_harness_canary_readiness.py \
  scripts/validate_repo.sh \
  tests/test_run_harness_canary_readiness.py \
  tests/test_repository_validation.py README.md
git commit -m "docs(eval): expose native readiness command"
```

---

## Task 4: Real Host Acceptance and Completion Gates

**Files:**

- Modify only if a verified defect is found:
  - `scripts/live_eval/private_jsonl_ledger.py`
  - `scripts/live_eval/native_canary_readiness.py`
  - `scripts/run_harness_canary_readiness.py`
  - their focused tests

### Step 1: Run full focused discovery

- [ ] Run:

```bash
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
python3 -m unittest tests.test_run_harness_canary_readiness -v
```

Expected: all related tests pass.

### Step 2: Run one explicit local macOS acceptance

- [ ] Create a fresh private evidence root and a separate empty temp parent,
  each owned by the current UID with mode `0700`.
- [ ] Resolve the already approved canonical Codex and Python paths.
- [ ] Run the new CLI once. Do not provide credentials.
- [ ] Record the command exit code and sanitized JSON result. A successful
  observation or an evidence-based fixed blocked reason is valid diagnostic
  output; do not convert a blocked host observation into a test bypass.
- [ ] Verify retained evidence is mode `0600`, canonical/path-free, and the
  temporary parent is empty after completion.

### Step 3: Run the repository completion gate

- [ ] Run:

```bash
./scripts/validate_repo.sh
```

Expected: all repository validation passes without hidden warnings.

### Step 4: Run final static poison-pill checks

- [ ] Run:

```bash
rg -n \
  'codex exec|OPENAI_API_KEY|preflight_auth|RuntimeContainmentReceipt|features list|lima|socket\\.create_connection\\([^)]*[\"'\"']https?://' \
  scripts/live_eval/native_canary_readiness.py \
  scripts/run_harness_canary_readiness.py
```

Expected: no output. The loopback-only socket code may exist but no external
URL or destination may exist.

### Step 5: Final whole-branch review and completion commit

- [ ] Obtain a fresh whole-branch requirements and code-quality review against
  the approved design.
- [ ] Resolve every material finding and rerun the smallest affected test, then
  the repository completion gate.
- [ ] If final fixes were needed, commit them with:

```bash
git add scripts tests README.md docs/superpowers
git commit -m "fix(eval): close native readiness review findings"
```

Do not push or merge unless the user separately authorizes those external Git
actions.

## Plan Self-Review

- [x] Every approved Phase B0 acceptance criterion maps to a task and test.
- [x] The plan contains no placeholder, deferred implementation detail, or
  unspecified production dependency.
- [x] Public types and digest forms are consistent across ledger, library, CLI,
  and tests.
- [x] Weak-control sensitivity, strong-policy attribution, supervisor
  environment separation, executable identity, cleanup ordering, and retained
  evidence each have explicit negative tests.
- [x] Crash recovery, power-loss durability, live tool inventory, canary calls,
  reservations, Phase C, and VM provisioning remain outside this plan.
