---
name: session-wiki
description: Use when the user invokes $session-wiki or asks to close a completed work session by extracting, verifying, reviewing, and routing durable knowledge into existing project documentation or an explicitly configured personal wiki. A bare invocation shows the option legend and waits; an executable invocation updates only approved documentation trust zones and never promotes personal knowledge without explicit human approval.
---

# Session Wiki

Turn completed session evidence into durable, source-linked knowledge without turning a wiki into a
task log. Keep code, schema, tests, and repository policy authoritative. Treat the conversation as a
candidate source that must be verified before durable project documentation is changed.

## Load The Contract

Read [references/knowledge-contract.md](references/knowledge-contract.md) before extracting
candidates. Read [references/project-routing.md](references/project-routing.md) before changing
project documentation. Read [references/personal-wiki-routing.md](references/personal-wiki-routing.md)
before reading or writing a personal wiki.

Also read the active target repository's `AGENTS.md` and directly applicable documentation index or
schema. Do not broad-scan the repository or the personal wiki. A target repository's stricter
instructions, trust zones, approval gates, and validation commands always win.

## Parse The Invocation

Classify the call before scanning files or changing documentation.

### Bare Or Help Call

Treat exactly `$session-wiki`, `session-wiki`, `$session-wiki help`, `$session-wiki 도움말`, or an
equivalent skill-only call without a target, preset, option, or execution wording as an option
request.

For a bare call:

1. Detect at most one likely project target and one explicitly configured personal-wiki target from
   current context.
2. Show both as candidates, not confirmed scope.
3. Show the compact legend below.
4. Ask the user to reply with a preset or explicit options.
5. Stop without repository scanning, validation, document writes, commits, pushes, or promotion.

Use this legend in the user's language when practical:

```text
Session Wiki target candidates
- project: {detected repository or "not specified"}
- personal wiki: {explicitly configured target or "not configured"}

Presets
- quick candidates: project / quick / no writes
- default: project / standard review / update verified project knowledge
- personal capture: personal / strict review / write inbox only
- full closeout: both / deep review / project + personal inbox

Options
- scope: project | personal | both
- source: session | session+diff | explicit
- depth: quick | standard | deep
- review: standard | strict
- write: none | project | personal-inbox | personal-generated | both
- result: candidates | updated

Defaults
- scope=project source=session+diff depth=standard review=standard
- write=project result=updated
- no task logs, no secret values, no personal-wiki promotion, no commit or push

Examples
- default
- personal capture source=session
- full closeout depth=deep
- scope=project write=none result=candidates
```

Accept the English preset labels and natural equivalents such as `빠른 후보`, `기본 진행`,
`개인지식화`, and `전체 마감`.

### Executable Call

Execute immediately when the invocation includes a recognized preset, explicit options, a bounded
target plus execution wording, or an equivalent request to perform the closeout. Do not repeat the
legend for an executable call.

Resolve targets in this order:

1. explicitly named repository, wiki root, path, diff, commit range, or source set
2. the single active repository and session-owned change set in current context
3. the only repository clearly associated with the completed work

Do not infer a personal-wiki path by scanning a home directory. Use only a path configured in active
instructions, explicitly provided by the user, or already established in current context. If the
selected scope has no unique writable target, ask one concise target question and stop.

## Apply Defaults And Presets

Use this contract for `default`, `기본 진행`, or a project target-only execution:

```yaml
scope: project
source: session+diff
depth: standard
review: standard
write: project
result: updated
promotion: forbidden
commit: forbidden
push: forbidden
```

Apply a preset before explicit overrides:

| Preset | Scope | Depth | Review | Write | Result |
|---|---|---|---|---|---|
| `quick candidates` / `빠른 후보` | project | quick | standard | none | candidates |
| `default` / `기본 진행` | project | standard | standard | project | updated |
| `personal capture` / `개인지식화` | personal | standard | strict | personal-inbox | updated |
| `full closeout` / `전체 마감` | both | deep | strict | both | updated |

Reject incompatible combinations instead of widening authority. Examples: `scope=project` cannot use
`write=personal-inbox`; `scope=personal` cannot use `write=project`; `result=candidates` implies
`write=none`. `personal-generated` may summarize an existing inbox source when target policy permits;
it cannot write `reviewed` or `canonical` paths.

Validate option compatibility before target discovery or file reads. On an incompatible
combination, return `blocked` with the conflicting fields and the smallest valid correction. Do not
scan for a target, reinterpret the scope, or silently reduce the requested trust boundary.

The executable invocation authorizes only the selected documentation writes and the smallest
repository-required documentation validation. It does not authorize implementation changes, tests
unrelated to documentation, browser or API probes, DB access, process-lifecycle changes, deletion,
archive moves, dependency changes, commits, pushes, PRs, external messages, or deployment.

## Freeze A Closeout Packet

Before extraction, freeze the smallest packet that represents the finished session:

- target repository and optional personal-wiki root
- session goal and completed scope
- session-owned changed paths or explicit commit range
- accepted decisions and rejected alternatives
- validation commands and observed outcomes
- unresolved risks, blocked checks, and follow-ups
- applicable source-of-truth and documentation rules

Do not silently absorb unrelated dirty files, other-session changes, or the entire conversation. If
change ownership is mixed or stale, record ownership per claim, use only explicit current-session
sources, and return `partial` for selected-scope claims that cannot be attributed safely.

## Extract And Review Candidates

Follow [references/knowledge-contract.md](references/knowledge-contract.md):

1. Extract atomic claims, one fact or reusable pattern per candidate.
2. Classify each candidate as `project`, `personal`, `both`, or `discard`.
3. Classify durability as `stable`, `time-bound`, `transient`, or `unknown`.
4. Attach source evidence, confidence, freshness, privacy, and an intended destination.
5. Compare with existing targeted documentation and record `comparison` as `new`, `merge`,
   `refresh`, `conflict`, `duplicate`, or `not_checked`.
6. Record a separate `decision` as `accept`, `reject`, or `defer`. Reject task-log material, secret
   values, raw sensitive logs, and unresolved ideas from durable project pages.
7. In strict review, run a second pass for contradiction, privacy, provenance, and lifecycle errors.

Unsupported session claims remain `decision=defer` even when they are plausible. Conflicting
authoritative sources remain `comparison=conflict` with `decision=defer` until the selected source
scope establishes which one is current. Strict review must record all four checks as
`pass`, `fail`, or `unknown` per candidate; a generic statement that review passed is not evidence.

Do not call a subagent or Council implicitly. If the user explicitly requests independent or
Council review, follow the active host and `$council` rules and keep all wiki writes with the main
agent.

## Route Project Knowledge

Use [references/project-routing.md](references/project-routing.md). Prefer updating an existing
canonical page and section over creating a new document. Project knowledge is eligible only when it
is stable, useful beyond the current task, supported by authoritative evidence, and allowed by the
repository's wiki policy.

When code and wiki conflict, trust the current authoritative source. Update the wiki only when the
source and session scope establish the correct replacement; otherwise report a stale/conflict
candidate. Never rewrite code to make it agree with a wiki during this skill.

Do not create a new project documentation hierarchy merely because a reference template exists. If
no destination convention is clear, return candidates and ask for a destination decision.

## Route Personal Knowledge

Use [references/personal-wiki-routing.md](references/personal-wiki-routing.md). Personal capture may
include reusable work preferences, learning notes, decision heuristics, cross-project patterns,
prompt candidates, and a concise session summary after private or sensitive material is removed.

Write only to the selected target's AI-writable trust zones. By default this means a dated session
note in `inbox`; an existing inbox source may be synthesized into `generated` only when explicitly
selected and target policy allows it. Never write or move content into `reviewed` or `canonical`,
never change lifecycle status to those values, and never archive or delete content without exact
user approval for those actions.

## Write Minimally And Validate

For every accepted candidate:

1. Re-read the destination section immediately before editing.
2. Merge into the smallest relevant section; preserve local schema, style, and navigation.
3. Record durable source references without local absolute paths, credentials, or raw logs.
4. Mark time-bound or uncertain personal claims with dates and reduced confidence.
5. Run only the target's smallest required documentation/schema/link validation.
6. Inspect the final diff for unrelated changes, secrets, task-log leakage, and accidental promotion.

If a required validator is unavailable or fails, preserve the documentation change only when target
policy permits and report `partial`; otherwise stop before writing or revert only the exact
skill-owned edit if repository rules require validation-before-write. Never hide the failure.

## Return The Receipt

Lead with what was updated or why no update was needed:

```markdown
## Session Wiki result

- Status: complete | partial | candidates_only | not_needed | blocked
- Scope: project | personal | both
- Sources: {bounded session, diff, commits, or explicit paths}
- Targets: {project docs and personal trust zone; redact private absolute paths}

### Accepted knowledge
- {claim} — {destination} — {evidence and confidence}

### Updated documents
- {path and section}: {reason}

### Rejected or deferred
- {candidate}: {transient, duplicate, conflict, privacy, insufficient evidence, or approval needed}

### Validation
- {command or check}: pass | fail | blocked | not_run

### Residual risk
- {unverified source, stale conflict, skipped target, or promotion boundary}
```

Use `not_needed` when no stable, reusable candidate exists; this is a successful closeout, not a
reason to manufacture documentation. Use `candidates_only` when writes were disabled. Never use
`complete` when a required destination or validation remains blocked, or when selected-scope
candidates were not dispositioned.
