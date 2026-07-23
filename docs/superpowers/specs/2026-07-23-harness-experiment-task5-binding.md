# Harness Experiment Task 5 Binding Contract

**Status:** Normative clarification for Task 5 of
`2026-07-23-harness-experiment-readiness-plan.md`.

This contract defines the bounded semantic JSONL subset, parser state
machine, failure normalization, and typed projection that Task 5 implements.
It adds no Codex invocation, authentication, network, ledger, filesystem, or
live-compatibility behavior.

## 1. Evidence scope and compatibility claim

Task 5 implements:

```text
codex-exec-v0.145.0-zero-cache-write-projection-v1
```

The contract is based on the event shape documented by the
[official non-interactive mode guide][noninteractive] and the tagged
[`rust-v0.145.0` exec event types][exec-events] and
[JSONL event processor][event-processor]. The tagged `Usage` type has five
serialized fields, including `cache_write_input_tokens`; the four-field shape
in the implementation plan is therefore not the accepted pinned wire shape.

Task 5 verifies only source-shaped synthetic fixtures for the successful
zero-cache-write subset. Failure envelopes are recognized only to reject and
normalize a stream; their ordering is not a source-conformance claim. Task 5
does not execute Codex or prove:

- that an installed binary matches the Git tag;
- compatibility with a real CLI trace;
- support for every v0.145.0 item variant or feature flag;
- live telemetry, pricing, authentication, or transport correctness.

A sanitized real-trace conformance gate and executable continuity remain
Phase B work. Reports must describe Task 5 evidence as
`source-shaped synthetic projection verified`, never `live-compatible` or
`pinned binary verified`.

[noninteractive]: https://developers.openai.com/codex/noninteractive
[exec-events]: https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/exec/src/exec_events.rs
[event-processor]: https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs

## 2. Public API, fixed limits, and errors

Expose:

```python
@dataclass(frozen=True)
class TelemetryLimits:
    max_total_bytes: int
    max_line_bytes: int
    max_events: int
    max_token_value: int


def parse_terminal_telemetry(
    data: bytes,
    limits: TelemetryLimits,
    price_snapshot: Mapping[str, object],
) -> TelemetrySummary:
    ...
```

The hard ceilings are:

```python
MAX_TOTAL_BYTES = 8_388_608
MAX_LINE_BYTES = 1_048_576
MAX_EVENTS = 4_096
MAX_TOKEN_VALUE = 1_000_000_000
MAX_JSON_DEPTH = 32
MIN_JSON_INTEGER = -9_223_372_036_854_775_808
MAX_JSON_INTEGER = 9_223_372_036_854_775_807
PRICE_SNAPSHOT_READ_ORDER = (
    "model_id",
    "currency",
    "input_microunits_per_million",
    "cached_input_microunits_per_million",
    "output_microunits_per_million",
    "effective_at",
    "source_label",
)
```

`TelemetryLimits` is accepted only when `type(value) is TelemetryLimits`, its
field set is exact, and all four values are exact positive `int` values.
Booleans and integer subclasses fail. Each value is at most its corresponding
hard ceiling, and `max_line_bytes <= max_total_bytes`. The constructor and
`parse_terminal_telemetry()` both enforce the same rules so a forged instance
cannot bypass validation.

Public parser failures are exact `TelemetryError` values with one of these
messages and no exception chaining:

```text
telemetry_not_bytes
telemetry_limits_invalid
telemetry_size_invalid
telemetry_truncated
telemetry_event_count_invalid
telemetry_line_invalid
telemetry_stream_invalid
telemetry_summary_invalid
```

`telemetry_stream_invalid` covers UTF-8, JSON, schema, state-machine, item,
usage, response, and failure-terminal rejection. Existing Task 3 price or
typed-summary validation remains `telemetry_summary_invalid`.

No exception `args`, `str()`, `repr()`, public value, or retained parser state
contains raw JSONL, a line number, a parser message, an error message, an item
payload, or response text.

## 3. LF-only bounded framing

The parser applies checks in this order:

1. require `type(data) is bytes`;
2. require `0 < len(data) <= limits.max_total_bytes`;
3. require the final byte to be LF;
4. reject every raw carriage-return byte;
5. split only with `data[:-1].split(b"\n")`;
6. reject an empty record, a record over `max_line_bytes`, or more than
   `max_events` records.

The total-byte cap includes the final LF. The line cap excludes LF. CRLF,
bare CR, leading, middle, or additional trailing blank records all fail.
`bytes.splitlines()` is not used.

## 4. Semantic JSON hardening

Raw JSONL is semantic JSON, not canonical wire JSON. Insignificant whitespace
and object-key order may differ. Every event and the decoded structured
response still rejects:

- invalid UTF-8, a UTF-8 BOM, and lone surrogates;
- duplicate keys at any depth;
- floats, exponent notation, `NaN`, and infinities;
- non-NFC keys or string values;
- integers outside signed 64-bit range;
- nesting deeper than `MAX_JSON_DEPTH`; and
- unsupported JSON value types.

Before JSON decoding, a quote- and escape-aware byte scan rejects excessive
object or array nesting and integer literals whose sign/digit form cannot fit
the signed 64-bit bound. The JSON decoder also uses a bounded `parse_int`
callback, rejects floats and constants, and normalizes all decoder failures.
Post-decode traversal rechecks NFC strings, exact scalar types, depth, and
integer range.

Each event's top-level value is an exact JSON object. Caller-controlled
mappings or sequences are not accepted at this bytes-only boundary.

## 5. Exact top-level event envelopes

The only accepted event envelopes are:

| Event | Exact top-level keys |
|---|---|
| `thread.started` | `type`, `thread_id` |
| `turn.started` | `type` |
| `item.started` | `type`, `item` |
| `item.updated` | `type`, `item` |
| `item.completed` | `type`, `item` |
| `turn.completed` | `type`, `usage` |
| `turn.failed` | `type`, `error` |
| `error` | `type`, `message` |

`turn.failed.error` is exactly `{"message": <text>}`. Thread and item IDs are
exact, non-empty NFC strings. Error messages and non-identifier payload text
are exact NFC strings and may be empty.

Unknown or missing envelope keys and unsupported event types fail.

## 6. Source-shaped item subset

Every item is an exact object for one of these variants:

| Item type | Exact keys |
|---|---|
| `agent_message` | `id`, `type`, `text` |
| `reasoning` | `id`, `type`, `text` |
| `command_execution` | `id`, `type`, `command`, `aggregated_output`, `exit_code`, `status` |
| `file_change` | `id`, `type`, `changes`, `status` |
| `todo_list` | `id`, `type`, `items` |
| `error` | `id`, `type`, `message` |

The nested forms are:

- command status: `in_progress`, `completed`, `failed`, or `declined`;
- command `exit_code`: null or an exact signed 32-bit integer;
- file-change status: `in_progress`, `completed`, or `failed`;
- each file change: exact keys `path`, `kind`, with kind `add`, `delete`, or
  `update`;
- each todo item: exact keys `text`, `completed`, with an exact boolean.

All item strings are exact NFC strings. IDs are non-empty. Unknown item or
nested keys fail. The disabled `mcp_tool_call`, `collab_tool_call`, and
`web_search` variants fail rather than being silently discarded.

The lifecycle matrix is:

| Item type | `started` | `updated` | `completed` without start |
|---|---:|---:|---:|
| `agent_message` | no | no | yes |
| `reasoning` | no | no | yes |
| `command_execution` | yes | no | yes |
| `file_change` | yes | no | yes |
| `todo_list` | yes | yes | no |
| `error` | no | no | yes |

For a started command, status is `in_progress` and exit code is null. A
completed command may carry any tagged-source status/exit-code combination;
Task 5 validates the exact scalar and enum domains but does not infer process
success from them. A started file change has `in_progress` status. Todo
updates require the same active ID. Other started or updated forms are
rejected by the matrix even when their payload shape would otherwise
validate.

## 7. Single-turn state machine

The exact state progression is:

```text
START
  thread.started -> THREAD

THREAD
  turn.started -> TURN_ACTIVE

TURN_ACTIVE
  item.started | item.updated | item.completed -> TURN_ACTIVE
  turn.completed -> SUCCESS
  error | turn.failed -> REJECTED

SUCCESS | REJECTED
  no further records
```

Additional invariants:

- exactly one thread and one turn occur;
- item IDs are unique at first appearance;
- `item.started` requires an unseen ID and records its type as active;
- `item.updated` requires the same active ID and type;
- `item.completed` either closes the same active ID and type or is a
  lifecycle-matrix-approved direct completion;
- a completed ID cannot reappear;
- `turn.completed` requires no active items and is the final record;
- exactly one completed `agent_message` exists before success;
- any `error` or `turn.failed` marks the stream rejected and never produces a
  summary;
- EOF in any state other than `SUCCESS` fails.

Malformed candidates do not mutate a previously valid parser state.
The tagged processor can emit `error` and later `turn.failed`; this Phase A
parser intentionally rejects at the first failure marker because it projects
only successful terminal telemetry. It does not claim failure-trace
compatibility.

## 8. Structured response projection

Only a completed `agent_message` counts as the structured response. Its
`text` must encode exactly one JSON object under the same semantic JSON
hardening rules. A scalar, array, empty value, multiple JSON values, or second
completed agent message fails.

Task 5 does not validate the domain output schema of that object. It computes
and retains only:

```python
sha256_bytes(canonical_bytes(response_object))
```

The parsed object and raw text are released. Semantically equal objects with
different key order or insignificant spacing produce the same response
digest.

## 9. Five-field usage and four-field retained projection

`turn.completed.usage` has exactly:

```text
input_tokens
cached_input_tokens
cache_write_input_tokens
output_tokens
reasoning_output_tokens
```

Each value is an exact non-boolean integer satisfying:

```text
0 <= value <= limits.max_token_value
cached_input_tokens <= input_tokens
reasoning_output_tokens <= output_tokens
input_tokens + output_tokens <= limits.max_token_value
cache_write_input_tokens == 0
```

Missing, additional, duplicate, boolean, float, string, negative, over-limit,
or subset-violating values fail. The four-field documentation example fails
under this pinned profile.

`cache_write_input_tokens` is validated but not retained because Task 3 has
neither a typed field nor an authoritative cache-write price. Requiring zero
prevents accepted streams with different discarded usage from sharing a
summary digest. Supporting a non-zero value requires a versioned Task 1
price, Task 3 summary, and Task 4 receipt contract change.

The retained Task 3 usage remains:

```text
input_tokens
cached_input_tokens
output_tokens
reasoning_output_tokens
total_reported_tokens
estimated_cost_microunits
```

Total reported tokens are input plus output. Reasoning is an output subset
and is not added again.

## 10. Price snapshot and typed-summary authority

After the stream has produced a valid terminal usage and before the first
price access, snapshot the caller's price mapping with a dedicated bounded
helper. It:

1. requires a mapping;
2. consumes at most `len(_PRICE_KEYS) + 1` iterator entries;
3. requires exact string keys, no duplicate iterator keys, and exactly
   `_PRICE_KEYS`;
4. reads each expected key exactly once in
   `PRICE_SNAPSHOT_READ_ORDER`; and
5. returns a plain detached dictionary.

Do not construct this first snapshot with `dict(price_snapshot)`: a custom
mapping iterator can repeat a key and cause `dict()` to read that key more
than once. Cost calculation and `telemetry_summary_from_document()` use only
the detached plain dictionary. A stateful caller mapping is therefore read
once per key, and an invalid stream does not cause a price read.

The parser constructs the exact Task 3 summary document and finishes through
`telemetry_summary_from_document()`. Task 3 remains authoritative for:

- price schema and exact integer rates;
- non-cached, cached, and output price arithmetic;
- ceiling division;
- usage subset and total invariants; and
- typed summary shape and digest.

The parser sets:

```text
classification=completed
raw_retention=discard
event_count=<exact input record count>
```

Raw JSONL, arbitrary event values, error text, item payloads, and response
text never enter the typed summary or its representation.

## 11. Required test matrix

Tests cover, at minimum:

1. exact `TelemetryLimits`, each hard ceiling, and line/total relationships;
2. bytes type, empty/truncated input, LF-only framing, blank records, and
   total/line/event limits at the boundary and one over;
3. invalid UTF-8/BOM, duplicate keys at nested depths, floats/non-finite
   values, non-NFC strings, oversized integers, and excessive nesting;
4. every event/item missing, unknown, wrong-type, and unsupported variant;
5. missing/duplicate/out-of-order thread and turn events, item ID/type
   transitions, direct completion, active-item terminal, failure terminal,
   and post-terminal records;
6. one structured response, duplicate response, invalid response JSON, and
   semantic key-order/spacing equivalence;
7. all five usage fields under missing, extra, duplicate, type, sign, cap,
   subset, total, and zero/non-zero cache-write mutations;
8. exact cost boundaries and proof that reasoning is not charged twice;
9. one-read stateful price mappings, including exact read order and duplicate
   or overlong key iterators; and
10. sentinel absence from summary documents, digests, representations, and
    exception text.

The tracked fixtures are synthetic, contain no credentials or private policy
text, and exercise the accepted source-shaped subset only.
