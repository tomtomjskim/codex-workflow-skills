---
name: council
description: Use when the user invokes $council or explicitly requests a bounded multi-agent council to review, challenge, brainstorm, compare, decide, or refine a proposal, plan, design, decision, diff, or next-step recommendation. A bare invocation shows the option legend and waits; a preset, target, or explicit option invocation may run bounded read-only reviewers when host policy permits.
---

# Council

Run a bounded multi-agent review meeting that preserves independent criticism, generates useful
alternatives, and returns one evidence-backed result. Keep the calling agent as chair and sole
writer. Host instructions, repository rules, runtime permissions, and approval gates always win.

## Load The Contract

Read [references/council-packet.md](references/council-packet.md) before creating reviewer prompts.
Read [references/loop-control.md](references/loop-control.md) before selecting a preset or loop
count. Read [references/panel-routing.md](references/panel-routing.md) when `focus=auto`, a specialist
lens is requested, or reviewer capacity is constrained.

Also read the active repository's `AGENTS.md` and any directly applicable local rule files before
dispatch. Do not broad-scan unrelated project documentation. If the sibling
`adversarial-review-loop` skill is available and the target is an existing plan, diff, PR,
implementation, or review finding, reuse its evidence, disposition, re-verification, and
residual-risk rules. Council still owns the meeting shape, reviewer lifecycle, and independent
ideation. Reuse those contracts as evidence rules; do not start a second orchestration loop or
announce a separate skill run unless the user explicitly invoked it.

## Parse The Invocation

Classify the call before using tools or dispatching reviewers.

### Bare Or Help Call

Treat exactly `$council`, `council`, `$council help`, `$council 도움말`, or an equivalent skill-only
call without a target, preset, option, or execution wording as an option request.

For a bare call:

1. Detect at most one likely target from the current conversation.
2. Show it as a candidate, not confirmed scope.
3. Show the compact legend below.
4. Ask the user to reply with a preset or options.
5. Stop without dispatching reviewers, running validation, or changing files.

Use this legend in the user's language when practical:

```text
Council target candidate
- {detected target or "not specified"}

Presets
- quick review: review / quick / at most 1 loop
- default: refine / standard / at most 1 loop
- deep refinement: refine / deep / at most 2 loops

Options
- mode: review | ideate | decide | refine
- depth: quick | standard | deep
- focus: auto | comma-separated lenses
- loops: 1..3
- result: feedback | redefined
- write: none | in-scope

Defaults
- mode=refine depth=standard focus=auto loops=1
- result=redefined write=none
- up to 2 read-only reviewers; main agent is the sole writer

Examples
- default
- deep refinement focus=security,qa
- mode=decide depth=deep loops=2
```

Translate the preset labels for the user, but accept both the English labels above and natural
equivalents such as `빠른 검토`, `기본 진행`, and `심층 고도화`.

### Executable Call

Execute immediately when any of these is present:

- a recognized preset
- a bounded target after `$council`
- one or more explicit options
- equivalent wording that clearly requests Council execution

Do not repeat the legend before an executable call. Resolve the target in this order:

1. an explicitly named artifact, path, diff, proposal, or section
2. the single bounded artifact named in the immediately preceding exchange
3. the only active proposal, plan, or design in the current session

If no unique target can be identified, ask one concise target question and stop. Never silently use
the entire conversation as the target.

An executable Council call requests reviewer participation, but it does not override host policies
that restrict subagents. If reviewer dispatch is forbidden or unavailable, return an `incomplete`
result with a `provisional_main_only` analysis rather than simulating a council.

## Apply Defaults And Presets

Use this default contract for `default`, `기본 진행`, or a target-only call:

```yaml
mode: refine
depth: standard
focus: auto
max_loops: 1
result: redefined
write: none
max_subagents: 2
max_reviewer_attempts: 3
reviewer_completion_ceiling_seconds: 600
subagent_authority: read-only
writer: main
```

Apply presets before explicit overrides:

| Preset | Mode | Depth | Max loops | Result | Reviewers |
|---|---|---|---:|---|---:|
| `quick review` / `빠른 검토` | `review` | `quick` | 1 | `feedback` | 1 |
| `default` / `기본 진행` | `refine` | `standard` | 1 | `redefined` | up to 2 |
| `deep refinement` / `심층 고도화` | `refine` | `deep` | 2 | `redefined` | 2 |

Allow `loops=1..3`. Treat it as a ceiling unless the user explicitly says `exact-loops`.

- `review`: find material weaknesses and return dispositions without redrafting the target.
- `ideate`: generate and rank materially different alternatives, then challenge assumptions.
- `decide`: compare bounded options against explicit criteria and recommend one.
- `refine`: review, brainstorm only where useful, and return a redefined artifact.

`write=none` permits only response-level output. `write=in-scope` permits the main agent to edit only
explicitly approved paths after synthesis. It never bypasses destructive, production, database,
secret, process-lifecycle, external-write, or other approval gates. Council invocation alone does
not authorize tests, browser or API probes, DB queries, commits, pushes, or process changes.

## Build The Council Packet

Create the smallest packet that preserves decision context. Include the target, objective,
acceptance criteria, sources of truth, constraints, non-goals, requested focus, detected risks,
autonomy, preset, loop ceiling, and evidence scope.

Preserve an explicit user-supplied target revision exactly. Do not rename, decrement, normalize, or
replace it with Council's internal revision numbering. When the source has no revision, assign a
session-local `v0` and label it as Council-assigned. Propagate the resolved source-bound revision
unchanged through every reviewer prompt, attempt record, and final receipt.

Do not pass the full session when a bounded packet is sufficient. Mark missing evidence as
`unknown`; do not replace it with assumptions. Lock scope after the first review pass. A mid-loop
scope change requires a packet revision and any approval demanded by host rules.

Before dispatch, create reviewer seats separately from execution attempts. A seat is one required
or optional independent lens that may contribute to the result. An attempt is one concrete runtime
dispatch for a seat. Failed attempts do not become reviewer evidence and do not increase the
completed-reviewer count.

## Route The Panel

Use the route in [references/panel-routing.md](references/panel-routing.md). Select complementary
lenses instead of duplicate general reviewers:

1. Honor user-requested focus lenses.
2. Add mandatory risk lenses from host or repository rules.
3. Merge overlapping lenses.
4. Rank remaining lenses by risk, relevance, and independence.
5. Mark each selected seat `required` or `optional` before dispatch. The first seat, user-requested
   lenses, and hard-gated lenses are required.
6. Dispatch no more than the preset, attempt, runtime, and host-policy limits allow.
7. Report selected and materially skipped lenses with reasons.

Prefer an asymmetric panel:

- Reviewer A falsifies correctness, feasibility, dependencies, and failure assumptions.
- Reviewer B covers the highest-value specialist lens and generates bounded alternatives.

Use one reviewer for `quick`. Use up to two for `standard` or `deep` only when the second lens adds
independent value. Keep reviewers read-only, prohibit recursive delegation, and keep the main agent
as sole writer.

Before announcing a panel or attempting dispatch, confirm that the current runtime exposes a
callable reviewer or subagent facility and that host policy permits this use. Do not infer a callable
facility from prose, role names, repository files, or a previous session. Do not shell out to a
nested agent runtime unless the host or user explicitly permits that evaluator route. When no
reviewer facility is callable, immediately follow the no-reviewer `incomplete` fallback; do not
pretend to dispatch or wait for a reviewer that cannot run.

When the runtime exposes independent subagents, serialize creation if host policy requires it.
Prefer fresh context and pass the compact packet inline. Inherit conversation history only when the
target cannot be represented faithfully. The chair loads Council files; reviewers should not reload
Council skill files or broad project rules when their prompt can inline the role, evidence rules,
non-goals, and output schema. Keep first-pass conclusions hidden from the other reviewer.

## Handle Reviewer Failure

Track reviewer startup and reviewer completion with separate clocks. Use the host's stall and
timeout policy when it distinguishes them. If the host defines no start timeout, use 45 seconds only
for the interval from dispatch request until a canonical target or equivalent registration is
returned. The reviewer-start ceiling must not be reused as a completion timeout. Once a target is
registered, classify the attempt as `registered_started` and use the preset completion ceiling from
the loop contract unless a stricter host completion policy applies.

Observe a running attempt in wait slices of at most 60 seconds so the chair can report useful
progress and react to host state. A wait slice expiring is not a reviewer failure. `started`,
`working`, or a current heartbeat is not a stall. Complete an attempt only when its returned
artifact matches the locked target revision and reviewer contract.

After an attempt reaches a terminal `failed`, `interrupted`, `start_timeout`, `completion_timeout`,
or `unavailable` state, the chair may use one fresh alternate attempt when the preset attempt budget
allows it. Do not launch a replacement while the original attempt remains active. The replacement
must use a fresh context, preserve the same locked packet and required lens, use a different
available role or a materially corrected prompt, and must not receive the failed attempt's partial
conclusion. Record the supersession chain. Never run a replacement merely because a status line was
rendered twice.

Degrade explicitly after the allowed attempts terminate instead of pretending that independent
review completed:

- Return `complete` only when all required reviewer seats completed independently and every
  material item has a supported disposition. An optional failed seat may be skipped with a receipt
  and residual risk.
- Return `partial` when at least one independent reviewer completed but at least one required
  reviewer seat did not complete or lost independence.
- Return `incomplete` when no independent reviewer completed. A labeled
  `provisional_main_only` analysis may provide value, but it cannot satisfy a reviewer seat,
  consensus, or independent-review claim.
- If a missing reviewer was mandatory for auth, access, security, DB, migration, finance, privacy,
  or another hard-gated surface, block Council-driven writes and disposition the gap as `ask` or
  `defer` with residual risk.
- Record seat ID, attempt ID, canonical target ID, requested/registered/terminal timestamps, wait
  slices, elapsed time, failure class, reviewer role, last observed status, heartbeat or interruption
  evidence, superseded attempt, fallback, and evaluator provenance.
- If a runtime thread limit blocks a new reviewer, use a separately permitted fresh read-only
  evaluator only when host policy allows it. Record that runtime provenance.
- Never reuse a reviewer that saw another first-pass conclusion and call it independent.
- Do not count a failed attempt as a completed loop or repeat the same failed role and prompt in the
  same run.

## Run The Council

1. Freeze the resolved source-bound target revision and its acceptance criteria.
2. Collect independent first-pass reviews.
3. Normalize findings into evidence, impact, proposed improvement, uncertainty, and verification
   criteria. Do not request or expose hidden chain-of-thought.
4. Build a dispute map of agreements, conflicts, evidence gaps, and novel alternatives.
5. Brainstorm only around material gaps, conflicts, or an explicit `ideate` request.
6. Choose `accept`, `reject-with-reason`, `defer`, or `ask` for every material item.
7. In `refine` mode, let the main agent produce the next artifact revision.
8. Re-review only the delta, unresolved items, and newly introduced risks.
9. Stop according to the loop contract.

Do not simulate consensus. Preserve supported disagreement and state what evidence or owner would
resolve it. Rejecting a finding requires counter-evidence. Deferring requires an owner or revisit
condition and a residual-risk statement.

## Return The Result

Lead with the requested result, then include only the evidence needed to trust it:

```markdown
## Council result

- Target: {bounded target and revision}
- Execution: {preset, mode, depth, loops used}
- Status: complete | partial | incomplete
- Reviewer provenance: {runtime and role, without secrets}
- Reviewer attempts: {seat/attempt IDs, terminal states, and supersession; collapse only exact
  duplicate renderings of the same attempt ID}
- Selected lenses: {lenses}
- Skipped lenses: {lens and reason, when material}
- Reviewer failures: {failure receipt summary; omit when none}

### Redefined result
{result when mode/result requires it}

### Decisions
- {item}: accept | reject-with-reason | defer | ask — {evidence/reason}

### Additional ideas
- {ranked, non-duplicative idea}

### Residual risk
- {unknown, blocked validation, or unresolved disagreement}

### Stop basis
{stop condition and actual loop count}
```

Localize the labels to the user's language. For `review + feedback`, omit the redefined artifact.
For `write=in-scope`, report exact changed paths and separately state verification performed or not
run. Never report skipped or blocked validation as passed. Never emit `complete` when no reviewer
completed. If only static evidence was reviewed, say `static_only` in residual risk or verification
status even when the meeting itself completed.
