# Native Canary Readiness Design

**Date:** 2026-07-29
**Status:** approved for implementation
**Scope:** Phase B0, zero-model-call native readiness only

## 1. Context

Phase A produces static experiment evidence and intentionally exposes no live
backend. Its future Phase B boundary calls for two marker canaries only after
containment proof. A subsequent adversarial review found that making a Lima VM
mandatory for those two output-only calls would add a larger lifecycle and
supply-chain surface than the canary itself.

The review also found that a native-first decision cannot rest on configuration
claims alone:

- the current canary template binds the older `--sandbox read-only` mode rather
  than a named permission profile;
- Codex permission profiles and the older sandbox settings do not compose;
- disabled feature flags do not prove the actual live tool inventory;
- the Phase A runtime containment receipt validates opaque digests but does not
  itself prove that subprocess probes ran;
- synthetic receipt replay does not prove filesystem fsync operations, lock
  exclusion, or OS sandbox enforcement.

This design introduces a smaller Phase B0 readiness slice. It records only the
native primitive observations that can be collected without authentication or
a model call. It does not authorize or implement either marker canary.

## 2. Decision

Use a **native-first, evidence-before-authorization** boundary:

1. Pin one exact Codex CLI executable and the Phase B0 policy.
2. Run positive and negative controls through one native permission-profile
   candidate for the future canary tool boundary.
3. Exercise a reusable private append-only JSONL storage primitive with real
   filesystem locking and fsync operations.
4. Emit private canonical evidence classified as
   `native_primitive_observations_only`.
5. Stop without resolving authentication, running `codex exec`, reserving a
   paid call, or creating a runtime containment receipt.

Lima remains a fallback candidate. It is considered only after a native
capability is unsupported or fails for a known policy reason. Phase B0 does not
install, provision, or benchmark a VM.

## 3. Alternatives considered

### 3.1 Recommended: Phase B0 native primitive acceptance

This isolates the new uncertainty—permission enforcement and local physical
state—without joining it to profile acquisition, model telemetry, marker
validation, or paid-call accounting. It produces reusable code for the later
runner while retaining a zero-call safety boundary.

### 3.2 Rejected for this slice: complete live canary runner

A complete runner would require a prepared-experiment lifecycle, live
authentication, exact prompt assembly, response/marker binding, reservation
durability, terminal telemetry, and a corrected Phase C approval transition.
Building those parts before the native boundary is proven would recreate the
over-engineering problem identified by the review.

### 3.3 Rejected as default: Lima appliance

A VM adds image provenance, guest bootstrap, mount policy, network policy,
credential transfer, instance identity, teardown, and host/guest version drift.
Those controls become proportionate only when the native boundary cannot prove
a required capability or when the threat model expands beyond trusted Codex
client code and low/medium synthetic tasks.

## 4. Trust and threat boundaries

### 4.1 Trusted computing base

Phase B0 trusts:

- the pinned Codex CLI binary as the orchestration client;
- the operating-system kernel and macOS Seatbelt implementation;
- the pinned Python interpreter used by the deterministic probe child;
- repository-owned canonical JSON, hashing, and ledger code;
- the human operator controlling the private roots.

The Codex client process itself is not enclosed by the permission profile.
Permission profiles govern sandboxed local command execution. Protecting the
host from the trusted Codex client itself is a stronger threat model and
requires a secret-free execution account, an outer sandbox, or a disposable
VM.

### 4.2 Untrusted or fail-closed inputs

Treat CLI output, subprocess output, filesystem state, and probe child JSON as
untrusted data. Reject unknown fields, duplicate keys,
noncanonical JSON, unexpected exit states, output overflow, path aliases,
symlinks, hardlinks, stale identities, and partial ledger records.

### 4.3 Out of scope

Phase B0 does not:

- read `OPENAI_API_KEY`, OAuth state, or any other authentication material;
- invoke `codex exec`, a model provider, an external network destination, MCP,
  plugins, hooks, skills, browser, computer use, or an evaluator;
- materialize current or lean harness homes;
- generate or validate a canary marker;
- create a `RuntimeContainmentReceipt`;
- reserve a canary or pilot call;
- change the Phase A plan or runtime receipt schemas;
- prove that a future live invocation exposes zero tools;
- prove that the Codex parent process cannot read arbitrary host files;
- implement Phase C pilot approval or execution;
- install or configure Lima.

## 5. Components

### 5.1 Native readiness policy

`scripts/live_eval/native_canary_readiness.py` owns one immutable versioned
policy document. For the first accepted run it binds:

- platform family `darwin`;
- Codex CLI version `0.145.0`;
- Codex CLI SHA-256
  `6db9193ce2c9a8cef2b5482612cde24202a4329dfc34f4687a036d5d7da619af`;
- Python `3.9.6` SHA-256
  `6b24be8ba00c4abc8b3e7e2620bd710e2cf7a4312d00c147f8f05fbb6f3d297a`;
- permission-profile name `phase-b0-native-readonly`;
- the exact `$CODEX_HOME/config.toml` bytes below and their digest;
- the exact deterministic child program bytes and digest;
- bounded command timeout and stdout/stderr byte limits;
- ledger file names, mode requirements, record and byte caps;
- the evidence schema version.

```toml
default_permissions = "phase-b0-native-readonly"

[permissions.phase-b0-native-readonly.filesystem]
":root" = "deny"
":minimal" = "read"

[permissions.phase-b0-native-readonly.filesystem.":workspace_roots"]
"." = "read"

[permissions.phase-b0-native-readonly.network]
enabled = false
```

The caller supplies canonical absolute executable paths, but identity is
accepted only when the observed content matches these pre-registered digests.
Each executable is opened without following a final symlink. The implementation
keeps the descriptor identity, requires a regular single-link file, and
rechecks descriptor and path identity plus content before and after every
invocation. For the first accepted run, the Codex binary is owned by the
operator UID with mode `0755`, and Python is owned by UID `0` with mode `0755`.
Any mismatch is `executable_identity_invalid`.

The probe creates a fresh mode-0700 `CODEX_HOME` containing only the exact
`config.toml`; it creates no named config overlay, auth file, plugin, skill,
rule, or project `.codex` directory. The exact sandbox argv is
`codex sandbox -P phase-b0-native-readonly -C <allowed-root> -- <child-argv>`.
The environment binds that `CODEX_HOME`, and the allowed root contains no
ambient configuration file. The evidence digest covers the exact config bytes,
argv template, environment-key allowlist, cwd class, and executable identities.
The production-candidate strong supervisor environment contains only
`CODEX_HOME`, `HOME`, `PATH`, `TMPDIR`, `LANG`, and `LC_ALL`. `CODEX_HOME` and
`HOME` equal the owned `codex-home`, `TMPDIR` equals the owned `tmp`, `PATH` is
`/usr/bin:/bin`, and both locale values are `C`. The weak control uses that
same sanitized map plus exactly one `PHASE_B0_SYNTHETIC_SECRET` entry; neither
attempt inherits the caller's ambient environment. The child executable is
passed by canonical absolute path and does not depend on `PATH` lookup.

Phase B0 intentionally does not run or parse `codex features list`. A finite
feature list is neither a live tool inventory nor part of Seatbelt enforcement.
The later Phase B invocation plan must bind the resolved disabled-feature set
and reject any observed tool event.

### 5.2 Permission-profile probe

Phase B0 creates one owned private temporary hierarchy:

```text
phase-b0/
  codex-home/
  tmp/
  allowed/
    readable-sentinel
  forbidden/
    host-sentinel
  ledger-probe/
```

The exact named profile is a candidate policy for the later Phase B runner. It:

- denies filesystem reads from `:root`;
- grants `:minimal` read access;
- grants read-only access to the effective workspace root;
- denies direct network access;
- grants no writable filesystem root.

The same deterministic child program runs twice with identical filesystem and
network sentinel inputs:

1. **Weak control:** directly under a mutation-sensitive supervisor without the
   permission profile and with a synthetic environment sentinel.
2. **Strong candidate:** through `codex sandbox -P <exact-name>` with the
   sealed profile and the production-candidate supervisor environment allowlist.

The weak control must prove that every sentinel is observable when policy is
absent:

- allowed-file read succeeds;
- forbidden-file read succeeds;
- owned allowed-root write succeeds;
- owned forbidden-root write succeeds;
- connection to the owned loopback listener succeeds;
- the synthetic environment secret is visible.

After the weak control, the supervisor removes only the weak control's
token-matched output files, verifies the original sentinel identities again,
and recreates the listener in the same policy class. A cleanup or identity
failure blocks the strong candidate instead of letting weak-control mutations
contaminate it.

The strong candidate must prove:

- allowed-file read succeeds;
- forbidden-file read fails;
- both writes fail;
- connection to the same ready loopback listener fails with the exact
  policy-denial error class.

The listener remains ready for the full strong attempt, records a
token-matched connection if one arrives, and is checked again after the child
exits. `ECONNREFUSED`, timeout, reset, listener exit, endpoint mismatch, or an
unclassified socket error is `native_permission_unproven`; it is not accepted
as a network denial. Only `EACCES` or `EPERM` from the strong child is the
accepted network-denial class.

Environment handling is separate evidence. The strong supervisor omits the
synthetic value using the exact future-runner candidate allowlist, and the child
must report it absent. This produces
`supervisor_environment_sanitization_only`; it is not permission-profile
enforcement and does not prove that a future authenticated Codex process cannot
pass credentials to a child. Phase B must reuse and revalidate the allowlist at
the actual launch boundary.

The probe classifies an operation only from the child’s canonical structured
result. “Command missing,” malformed output, timeout, signal termination,
wrong executable, wrong cwd, or a failed positive control is
`native_permission_unproven`, not a successful denial.

The loopback listener and all sentinel files are owned temporary test data.
Phase B0 makes no external network request.

### 5.3 Private canonical JSONL storage

`scripts/live_eval/private_jsonl_ledger.py` provides one narrow reusable
primitive for future runtime receipts.

It:

- opens an operator-owned canonical absolute directory;
- requires the current owner UID and directory mode `0700`;
- opens and retains one verified directory descriptor using
  `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`;
- creates fixed regular, non-symlink, single-link ledger and lock files with
  mode `0600`, using descriptor-relative
  `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`;
- reopens an existing ledger descriptor-relative with
  `O_APPEND|O_NOFOLLOW|O_CLOEXEC`;
- takes a nonblocking exclusive advisory lock and holds it for the object
  lifetime;
- accepts canonical JSON record bytes only;
- requires each record’s `previous_record_hash` to equal the configured
  genesis digest or the SHA-256 digest of the preceding canonical line;
- appends the record and LF with bounded complete writes;
- fsyncs every accepted append;
- fsyncs the directory when files are created;
- uses the locked open file descriptor as the write authority and rechecks its
  `fstat` identity before and after every transition;
- rejects unterminated, noncanonical, oversized, over-count, partial, or
  hash-invalid state and detects replacement during one open object lifetime;
- provides no repair, truncation, migration, database, daemon, encryption, or
  concurrent-writer merge behavior.

The storage is a cooperative single-writer, corruption-detecting primitive
under a trusted same-UID operator. Its advisory lock and unkeyed hash chain are
not tamper-proof against another same-UID process that ignores the lock and
rewrites the complete history. Extended ACLs and a malicious same-UID operator
are outside this Phase B0 threat model.

The primitive verifies that `fsync` operations return successfully. It does not
claim power-loss durability, crash recovery, or reservation safety. A valid
canonical history replaced between close and reopen is indistinguishable
without an external expected head or instance authority; those checks belong to
the existing runtime validator and future Phase B plan.

The storage primitive does not decide whether a
runtime receipt is semantically valid. A later Phase B runner must first call
the existing runtime transition validator and only then append the exact
accepted `CanonicalReceipt.canonical_bytes`.

Phase B0 exercises the primitive with owned synthetic records. It verifies:

- create, append, close, and reopen;
- exclusive lock contention;
- replacement during one open object lifetime;
- partial-tail rejection;
- wrong previous hash rejection;
- mode, link, and symlink rejection;
- cleanup that removes only paths created and still owned by the probe.

Passing this sentinel records the declared filesystem observations for the
current filesystem. It does not prove crash recovery, power-loss durability, or
that a future live runner orders reservation, invocation, and terminal receipts
correctly.

### 5.4 Evidence and CLI

`scripts/run_harness_canary_readiness.py` accepts:

- `--codex-executable` as an exact canonical absolute regular file;
- `--python-executable` as an exact canonical absolute regular file;
- `--temp-parent` as an existing canonical absolute empty private directory;
- `--private-root` as the existing canonical absolute operator-owned mode-0700
  evidence directory.

It emits one bounded JSON object and exits nonzero on a blocked result. The
public result contains only:

- `status`: `native_primitive_observations_only` or `blocked`;
- `model_calls`: always `0`;
- policy, executable, permission-profile, supervisor-environment,
  ledger-probe, and complete evidence digests when available;
- `cleanup_state`: `not_started`, `removed`, or `cleanup_required`;
- `reason_code`: one fixed reason code.

Private canonical evidence is staged as nonterminal mode `0600` data under one
fresh run directory below `--private-root`. It contains no synthetic secret
value, raw host path, command output, or file content. Raw subprocess buffers
are bounded and discarded after their parsed projections and digests are
produced.

After the temporary hierarchy cleanup finishes, the evidence is published with
the final terminal status and cleanup state in the same canonical document. If
cleanup fails, only a `blocked`/`cleanup_required` terminal artifact may be
published. No retained artifact may claim
`native_primitive_observations_only` before cleanup succeeds.

Fixed terminal reason codes are:

- `native_primitive_observations_recorded`
- `request_invalid`
- `unsupported_platform`
- `executable_identity_invalid`
- `cli_version_mismatch`
- `weak_control_invalid`
- `native_permission_unproven`
- `ledger_primitives_unproven`
- `evidence_retention_failed`
- `cleanup_required`

## 6. Data flow

```text
validate public request
  -> seal executable and policy identities
  -> create owned temporary hierarchy
  -> run weak positive/mutation control
  -> run strong named-permission-profile candidate
  -> exercise private JSONL storage
  -> stage path-free nonterminal private evidence
  -> remove owned temporary hierarchy
  -> publish terminal private evidence
  -> emit redacted public result
```

Any failure before private evidence publication returns `blocked`. A cleanup
failure returns `cleanup_required` even when all capability checks passed. No
failed or partial result is described as native containment proof.

## 7. Phase boundaries after Phase B0

A passing Phase B0 result is necessary but insufficient for a paid canary.
Before Phase B may expose a two-call command, a separate implementation design
must add:

- a `PreparedExperiment` lifecycle that preserves Phase A plan, preflight,
  derived homes, and cleanup authority without duplicating acquisition;
- a versioned canary invocation template that uses exactly one permission
  system and binds the resolved disabled-feature set;
- a runtime containment evidence contract tied to actual typed probe results,
  executable identity, and policy identity rather than opaque caller-chosen
  digests;
- raw structured response and full marker-occurrence binding;
- durable reservation-before-invocation, retry `0`, and stop-after-first-failure
  behavior;
- exactly two sequential calls with bounded request, output, time, and
  provider-side cost evidence.

Phase C remains a different approval and implementation boundary. Before a
pilot command exists, the runtime state machine must require an append-only
`pilot_approval` record that binds both canary receipts and the operator review.

Native-to-VM fallback is allowed only before the first reservation. It requires
a new backend identity, complete probe rerun, new static preflight when
plan-bound inputs change, and separate approval. Native and VM evidence or
measurements are never combined into one cohort.

## 8. Testing strategy

### 8.1 TDD unit and integration tests

Add focused tests for:

- exact immutable policy, exact config resolution, and path-free result schemas;
- CLI version, pre-registered executable digest, and invocation-time
  replacement rejection;
- weak-control sensitivity and strong-candidate acceptance;
- wrong denial causes such as missing command, bad sentinel, listener race,
  refused/timeout/reset socket errors, or malformed child output;
- separately classified environment allowlist behavior;
- command timeout, output caps, signals, and sanitized failures;
- ledger canonicality, hash chain, real process lock contention, fsync call
  order, reopen, partial tail, object-lifetime identity, link, mode, and
  byte/record caps;
- CLI rejection before any subprocess or write;
- poison pills proving no auth lookup, `codex exec`, model call, external
  network, profile materialization, or runtime receipt creation;
- public/private evidence redaction and cleanup ownership.

Do not copy the Phase A receipt mutation matrix. Reuse canonical JSON helpers
and add only the new native-boundary and physical-ledger cases.

### 8.2 Real local acceptance

After unit tests pass, run one explicit macOS acceptance using Codex CLI
`0.145.0`. It must:

- perform zero model calls;
- need no authentication;
- make no external network request;
- show a sensitive weak control;
- show the strong profile blocks the declared negative operations;
- record the separate supervisor environment observation;
- pass the real ledger lock and fsync-operation sentinel;
- retain only canonical redacted private evidence.

The real acceptance is host-specific evidence, not a portable unit test.
Linux CI runs deterministic fake-runner and filesystem-ledger tests but does
not claim macOS Seatbelt verification.

### 8.3 Repository completion gate

Run focused tests first, then:

```bash
./scripts/validate_repo.sh
```

No new production dependency is permitted.

## 9. Over-engineering guardrails

Stop this implementation at Phase B0. Specifically:

- do not implement a paid-call command;
- do not create a second experiment plan or receipt state machine;
- do not refactor task acquisition merely to speed up one canary;
- do not add a database, service, daemon, encryption layer, VM abstraction, or
  generic policy engine;
- do not add a feature-list parser or claim an empty live tool inventory;
- do not broaden probes to Phase C validator, resource-control, or
  workspace-write requirements;
- do not install Lima unless native acceptance fails and a separate decision
  approves the fallback.

The implementation should add two focused library modules, one CLI, focused
tests, and the minimum README/design cross-reference needed to describe the
zero-call command.

## 10. Acceptance criteria

Phase B0 is complete only when:

1. all new behavior was developed test-first;
2. the command cannot resolve auth or invoke `codex exec`;
3. the public result always reports `model_calls=0`;
4. weak controls prove the probes are capable of observing every sentinel;
5. the exact named permission-profile candidate proves the declared read,
   write, and loopback observations or fails closed;
6. supervisor environment sanitization is reported separately and is not
   attributed to the permission profile;
7. the private ledger passes real lock, append, fsync-operation, reopen, and
   corruption rejection checks;
8. evidence and failure output contain no paths, sentinel values, credentials,
   or raw command output;
9. a real local macOS run produces `native_primitive_observations_only` or an
   evidence-based blocked reason;
10. full repository validation passes;
11. no VM runtime or production dependency is added.
