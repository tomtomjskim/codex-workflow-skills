from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import (
    FrozenInstanceError,
    dataclass,
    fields,
    is_dataclass,
    replace,
)
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
import unittest

from scripts.live_eval import experiment_telemetry
from scripts.live_eval.experiment_telemetry import (
    TelemetryError,
    TelemetrySummary,
    UsageSummary,
    telemetry_summary_digest,
    telemetry_summary_document,
    telemetry_summary_from_document,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


_SUMMARY_KEYS = {
    "classification",
    "response_digest",
    "usage",
    "event_count",
    "raw_retention",
}
_USAGE_KEYS = {
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_reported_tokens",
    "estimated_cost_microunits",
}
_PRICE_KEYS = {
    "model_id",
    "currency",
    "input_microunits_per_million",
    "cached_input_microunits_per_million",
    "output_microunits_per_million",
    "effective_at",
    "source_label",
}


def _price_snapshot():
    return {
        "model_id": "gpt-5.6-sol",
        "currency": "USD",
        "input_microunits_per_million": 2_000_000,
        "cached_input_microunits_per_million": 500_000,
        "output_microunits_per_million": 3_000_000,
        "effective_at": "2026-07-23T00:00:00Z",
        "source_label": "operator-price-snapshot",
    }


def _summary_document():
    return {
        "classification": "completed",
        "response_digest": "sha256:" + "a" * 64,
        "usage": {
            "input_tokens": 11,
            "cached_input_tokens": 3,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
            "total_reported_tokens": 16,
            "estimated_cost_microunits": 33,
        },
        "event_count": 4,
        "raw_retention": "discard",
    }


def _typed_summary():
    usage = UsageSummary(
        input_tokens=11,
        cached_input_tokens=3,
        output_tokens=5,
        reasoning_output_tokens=2,
        total_reported_tokens=16,
        estimated_cost_microunits=33,
    )
    return TelemetrySummary(
        classification="completed",
        response_digest="sha256:" + "a" * 64,
        usage=usage,
        event_count=4,
        raw_retention="discard",
    )


def _telemetry_limits(**changes):
    values = {
        "max_total_bytes": 100_000,
        "max_line_bytes": 20_000,
        "max_events": 100,
        "max_token_value": 1_000_000,
    }
    values.update(changes)
    return experiment_telemetry.TelemetryLimits(**values)


def _terminal_usage(**changes):
    usage = {
        "input_tokens": 11,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
    }
    usage.update(changes)
    return usage


def _terminal_events(response_text='{"answer":"ok"}', usage=None):
    if usage is None:
        usage = _terminal_usage()
    return [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "message-1",
                "type": "agent_message",
                "text": response_text,
            },
        },
        {"type": "turn.completed", "usage": usage},
    ]


def _jsonl(events, *, separators=(",", ":")):
    return (
        "\n".join(
            json.dumps(
                event,
                ensure_ascii=False,
                separators=separators,
            )
            for event in events
        )
        + "\n"
    ).encode("utf-8")


def _agent_item(item_id="message-1", text='{"answer":"ok"}'):
    return {
        "id": item_id,
        "type": "agent_message",
        "text": text,
    }


def _reasoning_item(item_id="reasoning-1", text="reasoning"):
    return {"id": item_id, "type": "reasoning", "text": text}


def _command_item(
    item_id="command-1",
    *,
    command="printf ok",
    output="ok",
    exit_code=0,
    status="completed",
):
    return {
        "id": item_id,
        "type": "command_execution",
        "command": command,
        "aggregated_output": output,
        "exit_code": exit_code,
        "status": status,
    }


def _file_item(
    item_id="file-1",
    *,
    changes=None,
    status="completed",
):
    if changes is None:
        changes = [{"path": "README.md", "kind": "update"}]
    return {
        "id": item_id,
        "type": "file_change",
        "changes": changes,
        "status": status,
    }


def _todo_item(item_id="todo-1", *, items=None):
    if items is None:
        items = [{"text": "verify", "completed": False}]
    return {"id": item_id, "type": "todo_list", "items": items}


def _error_item(item_id="item-error-1", message=""):
    return {"id": item_id, "type": "error", "message": message}


def _item_event(lifecycle, item):
    return {"type": "item." + lifecycle, "item": item}


def _successful_events_with_items(items):
    return [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        *items,
        _item_event("completed", _agent_item()),
        {"type": "turn.completed", "usage": _terminal_usage()},
    ]


@dataclass(frozen=True)
class _UsageSummarySubclass(UsageSummary):
    extra: int = 0


@dataclass(frozen=True)
class _TelemetrySummarySubclass(TelemetrySummary):
    extra: int = 0


class _StringSubclass(str):
    pass


class _IntSubclass(int):
    pass


class _WrongTelemetryErrorMapping(dict):
    def keys(self):
        raise TelemetryError("wrong_error")


class _ChangingValueMapping(Mapping):
    def __init__(self, initial, changed):
        self._initial = dict(initial)
        self._changed = dict(changed)
        self._read_counts = {}

    def __getitem__(self, key):
        read_count = self._read_counts.get(key, 0)
        self._read_counts[key] = read_count + 1
        if read_count == 0:
            return self._initial[key]
        return self._changed.get(key, self._initial[key])

    def __iter__(self):
        return iter(self._initial)

    def __len__(self):
        return len(self._initial)

    @property
    def read_counts(self):
        return dict(self._read_counts)


class _TrackingPriceMapping(Mapping):
    def __init__(self, iteration_keys, values=None):
        self._iteration_keys = tuple(iteration_keys)
        self._values = dict(_price_snapshot() if values is None else values)
        self.iteration_count = 0
        self.read_order = []

    def __getitem__(self, key):
        self.read_order.append(key)
        return self._values[key]

    def __iter__(self):
        for key in self._iteration_keys:
            self.iteration_count += 1
            yield key

    def __len__(self):
        return len(self._iteration_keys)


class _ExplodingPriceMapping(Mapping):
    def __getitem__(self, _key):
        raise ValueError("SENTINEL-PRICE-CONTEXT")

    def __iter__(self):
        raise ValueError("SENTINEL-PRICE-CONTEXT")

    def __len__(self):
        return len(_PRICE_KEYS)


class _ArithmeticSentinel:
    def __init__(self):
        self.touched = False

    def __rmul__(self, _other):
        self.touched = True
        raise AssertionError("SENTINEL-PRICE-ARITHMETIC")


class TelemetrySummaryCodecTests(unittest.TestCase):
    def assertTelemetryInvalid(self, function, *args):
        with self.assertRaises(TelemetryError) as raised:
            function(*args)
        self.assertIs(type(raised.exception), TelemetryError)
        self.assertEqual(str(raised.exception), "telemetry_summary_invalid")
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_typed_values_have_exact_fields_and_are_frozen(self):
        self.assertEqual(
            tuple(field.name for field in fields(UsageSummary)),
            (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_reported_tokens",
                "estimated_cost_microunits",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(TelemetrySummary)),
            (
                "classification",
                "response_digest",
                "usage",
                "event_count",
                "raw_retention",
            ),
        )

        summary = _typed_summary()
        with self.assertRaises(FrozenInstanceError):
            summary.event_count = 5
        with self.assertRaises(FrozenInstanceError):
            summary.usage.input_tokens = 12

    def test_type_annotations_dataclass_metadata_and_error_base_are_exact(self):
        self.assertEqual(
            tuple(UsageSummary.__annotations__.items()),
            (
                ("input_tokens", int),
                ("cached_input_tokens", int),
                ("output_tokens", int),
                ("reasoning_output_tokens", int),
                ("total_reported_tokens", int),
                ("estimated_cost_microunits", int),
            ),
        )
        self.assertEqual(
            tuple(TelemetrySummary.__annotations__.items()),
            (
                ("classification", str),
                ("response_digest", str),
                ("usage", UsageSummary),
                ("event_count", int),
                ("raw_retention", str),
            ),
        )
        for value_type in (UsageSummary, TelemetrySummary):
            with self.subTest(value_type=value_type.__name__):
                self.assertTrue(is_dataclass(value_type))
                self.assertIs(value_type.__dataclass_params__.frozen, True)
        self.assertEqual(TelemetryError.__bases__, (ValueError,))

    def test_from_document_round_trips_to_deeply_immutable_typed_value(self):
        source_document = _summary_document()
        source_price = _price_snapshot()

        summary = telemetry_summary_from_document(
            MappingProxyType(
                {
                    **source_document,
                    "usage": MappingProxyType(source_document["usage"]),
                }
            ),
            MappingProxyType(source_price),
        )

        self.assertIs(type(summary), TelemetrySummary)
        self.assertIs(type(summary.usage), UsageSummary)
        self.assertEqual(summary, _typed_summary())
        self.assertEqual(telemetry_summary_document(summary), source_document)
        self.assertEqual(
            telemetry_summary_digest(summary),
            "sha256:"
            + hashlib.sha256(canonical_bytes(source_document)).hexdigest(),
        )
        with self.assertRaises(FrozenInstanceError):
            summary.response_digest = "sha256:" + "b" * 64
        with self.assertRaises(FrozenInstanceError):
            summary.usage.output_tokens = 6

        source_document["event_count"] = 999
        source_document["usage"]["input_tokens"] = 999
        source_price["input_microunits_per_million"] = 999
        self.assertEqual(summary, _typed_summary())

    def test_from_document_snapshots_stateful_top_level_mapping_once(self):
        document = _ChangingValueMapping(
            _summary_document(), {"classification": "failed"}
        )

        summary = telemetry_summary_from_document(
            document, _price_snapshot()
        )

        self.assertEqual(summary, _typed_summary())
        self.assertEqual(
            document.read_counts,
            {key: 1 for key in _SUMMARY_KEYS},
        )

    def test_from_document_snapshots_stateful_nested_usage_mapping_once(self):
        document = _summary_document()
        usage = _ChangingValueMapping(
            document["usage"], {"estimated_cost_microunits": 34}
        )
        document["usage"] = usage

        summary = telemetry_summary_from_document(
            document, _price_snapshot()
        )

        self.assertEqual(summary, _typed_summary())
        self.assertEqual(
            usage.read_counts,
            {key: 1 for key in _USAGE_KEYS},
        )

    def test_from_document_snapshots_stateful_price_mapping_once(self):
        price = _ChangingValueMapping(
            _price_snapshot(),
            {"input_microunits_per_million": 3_000_000},
        )

        summary = telemetry_summary_from_document(
            _summary_document(), price
        )

        self.assertEqual(summary, _typed_summary())
        self.assertEqual(
            price.read_counts,
            {key: 1 for key in _PRICE_KEYS},
        )

    def test_document_has_only_exact_typed_allowlist_keys(self):
        document = telemetry_summary_document(_typed_summary())

        self.assertEqual(set(document), _SUMMARY_KEYS)
        self.assertEqual(set(document["usage"]), _USAGE_KEYS)
        self.assertEqual(document, _summary_document())
        encoded = canonical_bytes(document)
        for forbidden in (
            b"raw_jsonl",
            b"event_payload",
            b"arbitrary-provider-value",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_document_rejects_unknown_missing_and_non_mapping_boundaries(self):
        for path in ("summary", "usage"):
            expected_keys = _SUMMARY_KEYS if path == "summary" else _USAGE_KEYS
            for key in sorted(expected_keys):
                with self.subTest(path=path, mutation="missing", key=key):
                    document = _summary_document()
                    target = document if path == "summary" else document["usage"]
                    del target[key]
                    self.assertTelemetryInvalid(
                        telemetry_summary_from_document,
                        document,
                        _price_snapshot(),
                    )

            with self.subTest(path=path, mutation="unknown"):
                document = _summary_document()
                target = document if path == "summary" else document["usage"]
                target["event_payload"] = "arbitrary-provider-value"
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    document,
                    _price_snapshot(),
                )

        for document in (None, [], "document"):
            with self.subTest(document=document):
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    document,
                    _price_snapshot(),
                )

        document = _summary_document()
        document["usage"] = []
        self.assertTelemetryInvalid(
            telemetry_summary_from_document,
            document,
            _price_snapshot(),
        )

    def test_usage_requires_exact_non_boolean_non_negative_integers(self):
        for field_name in sorted(_USAGE_KEYS):
            for invalid in (True, -1, 1.5, "1"):
                with self.subTest(field=field_name, invalid=invalid):
                    document = _summary_document()
                    document["usage"][field_name] = invalid
                    self.assertTelemetryInvalid(
                        telemetry_summary_from_document,
                        document,
                        _price_snapshot(),
                    )

    def test_integer_subclasses_are_rejected_at_every_integer_boundary(self):
        typed = _typed_summary()
        for field_name in sorted(_USAGE_KEYS):
            with self.subTest(boundary="document_usage", field=field_name):
                document = _summary_document()
                document["usage"][field_name] = _IntSubclass(
                    document["usage"][field_name]
                )
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    document,
                    _price_snapshot(),
                )

            with self.subTest(boundary="typed_usage", field=field_name):
                invalid_usage = replace(
                    typed.usage,
                    **{
                        field_name: _IntSubclass(
                            getattr(typed.usage, field_name)
                        )
                    }
                )
                invalid_summary = replace(typed, usage=invalid_usage)
                for serializer in (
                    telemetry_summary_document,
                    telemetry_summary_digest,
                ):
                    self.assertTelemetryInvalid(
                        serializer, invalid_summary
                    )

        document = _summary_document()
        document["event_count"] = _IntSubclass(document["event_count"])
        self.assertTelemetryInvalid(
            telemetry_summary_from_document,
            document,
            _price_snapshot(),
        )
        invalid_summary = replace(
            typed, event_count=_IntSubclass(typed.event_count)
        )
        for serializer in (
            telemetry_summary_document,
            telemetry_summary_digest,
        ):
            self.assertTelemetryInvalid(serializer, invalid_summary)

        for rate_name in (
            "input_microunits_per_million",
            "cached_input_microunits_per_million",
            "output_microunits_per_million",
        ):
            with self.subTest(boundary="price", field=rate_name):
                price = _price_snapshot()
                price[rate_name] = _IntSubclass(price[rate_name])
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    _summary_document(),
                    price,
                )

    def test_usage_enforces_subset_and_total_invariants(self):
        mutations = (
            {"cached_input_tokens": 12},
            {"reasoning_output_tokens": 6},
            {"total_reported_tokens": 15},
            {"total_reported_tokens": 17},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                document = _summary_document()
                document["usage"].update(mutation)
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    document,
                    _price_snapshot(),
                )

        zero_document = _summary_document()
        zero_document["usage"] = {
            key: 0 for key in _USAGE_KEYS
        }
        summary = telemetry_summary_from_document(
            zero_document, _price_snapshot()
        )
        self.assertEqual(summary.usage.total_reported_tokens, 0)
        self.assertEqual(summary.usage.estimated_cost_microunits, 0)

    def test_estimated_cost_uses_each_rate_once_and_rounds_up(self):
        summary = telemetry_summary_from_document(
            _summary_document(), _price_snapshot()
        )

        # (8 * 2_000_000 + 3 * 500_000 + 5 * 3_000_000) / 1_000_000
        # is 32.5 microunits and therefore rounds upward to 33.
        self.assertEqual(summary.usage.estimated_cost_microunits, 33)

        cases = (
            (
                {
                    "input_tokens": 2,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_reported_tokens": 2,
                    "estimated_cost_microunits": 2,
                },
                "input_microunits_per_million",
            ),
            (
                {
                    "input_tokens": 2,
                    "cached_input_tokens": 2,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_reported_tokens": 2,
                    "estimated_cost_microunits": 2,
                },
                "cached_input_microunits_per_million",
            ),
            (
                {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 2,
                    "total_reported_tokens": 2,
                    "estimated_cost_microunits": 2,
                },
                "output_microunits_per_million",
            ),
        )
        for usage, active_rate in cases:
            with self.subTest(active_rate=active_rate):
                document = _summary_document()
                document["usage"] = usage
                price = _price_snapshot()
                for rate_name in (
                    "input_microunits_per_million",
                    "cached_input_microunits_per_million",
                    "output_microunits_per_million",
                ):
                    price[rate_name] = 750_000 if rate_name == active_rate else 0
                parsed = telemetry_summary_from_document(document, price)
                self.assertEqual(
                    parsed.usage.estimated_cost_microunits, 2
                )

    def test_reasoning_tokens_are_not_costed_separately(self):
        for reasoning_tokens in (0, 2, 5):
            with self.subTest(reasoning_tokens=reasoning_tokens):
                document = _summary_document()
                document["usage"][
                    "reasoning_output_tokens"
                ] = reasoning_tokens
                summary = telemetry_summary_from_document(
                    document, _price_snapshot()
                )
                self.assertEqual(
                    summary.usage.estimated_cost_microunits, 33
                )

    def test_from_document_rejects_mismatched_retained_estimate(self):
        for estimate in (32, 34):
            with self.subTest(estimate=estimate):
                document = _summary_document()
                document["usage"][
                    "estimated_cost_microunits"
                ] = estimate
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    document,
                    _price_snapshot(),
                )

    def test_price_snapshot_requires_exact_schema_and_valid_scalars(self):
        for key in sorted(_PRICE_KEYS):
            with self.subTest(mutation="missing", key=key):
                price = _price_snapshot()
                del price[key]
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    _summary_document(),
                    price,
                )

        price = _price_snapshot()
        price["unknown"] = 0
        self.assertTelemetryInvalid(
            telemetry_summary_from_document, _summary_document(), price
        )
        for price in (None, [], "price"):
            with self.subTest(price=price):
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    _summary_document(),
                    price,
                )

        for rate_name in (
            "input_microunits_per_million",
            "cached_input_microunits_per_million",
            "output_microunits_per_million",
        ):
            for invalid in (True, -1, 1.5, "1"):
                with self.subTest(rate=rate_name, invalid=invalid):
                    price = _price_snapshot()
                    price[rate_name] = invalid
                    self.assertTelemetryInvalid(
                        telemetry_summary_from_document,
                        _summary_document(),
                        price,
                    )

        for text_name in ("model_id", "effective_at", "source_label"):
            for invalid in (
                "",
                None,
                "e\u0301",
                "\ud800",
                _StringSubclass("valid"),
            ):
                with self.subTest(text=text_name, invalid=repr(invalid)):
                    price = _price_snapshot()
                    price[text_name] = invalid
                    self.assertTelemetryInvalid(
                        telemetry_summary_from_document,
                        _summary_document(),
                        price,
                    )

        for currency in (
            "usd",
            "US",
            "USDD",
            "ÜSD",
            None,
            _StringSubclass("USD"),
        ):
            with self.subTest(currency=currency):
                price = _price_snapshot()
                price["currency"] = currency
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document,
                    _summary_document(),
                    price,
                )

    def test_terminal_summary_scalars_are_strictly_validated(self):
        invalid_values = {
            "classification": (
                "failed",
                "",
                None,
                _StringSubclass("completed"),
            ),
            "response_digest": (
                None,
                "sha256:" + "A" * 64,
                "sha256:" + "a" * 63,
                "sha256:" + "g" * 64,
                "sha512:" + "a" * 64,
                _StringSubclass("sha256:" + "a" * 64),
            ),
            "event_count": (True, 0, -1, 1.0, "1"),
            "raw_retention": (
                "retain",
                "",
                None,
                _StringSubclass("discard"),
            ),
        }
        for field_name, values in invalid_values.items():
            for invalid in values:
                with self.subTest(field=field_name, invalid=invalid):
                    document = _summary_document()
                    document[field_name] = invalid
                    self.assertTelemetryInvalid(
                        telemetry_summary_from_document,
                        document,
                        _price_snapshot(),
                    )

    def test_serializer_and_digest_revalidate_exact_typed_instances(self):
        valid = _typed_summary()
        invalid_summaries = (
            None,
            _summary_document(),
            _TelemetrySummarySubclass(**vars(valid)),
            replace(valid, usage=vars(valid.usage)),
            replace(
                valid,
                usage=_UsageSummarySubclass(**vars(valid.usage)),
            ),
            replace(valid, classification="failed"),
            replace(valid, response_digest="sha256:" + "A" * 64),
            replace(valid, event_count=0),
            replace(valid, raw_retention="retain"),
            replace(valid, usage=replace(valid.usage, input_tokens=True)),
            replace(valid, usage=replace(valid.usage, input_tokens=-1)),
            replace(
                valid,
                usage=replace(valid.usage, cached_input_tokens=12),
            ),
            replace(
                valid,
                usage=replace(valid.usage, reasoning_output_tokens=6),
            ),
            replace(
                valid,
                usage=replace(valid.usage, total_reported_tokens=15),
            ),
            replace(
                valid,
                usage=replace(
                    valid.usage, estimated_cost_microunits=-1
                ),
            ),
        )
        for boundary in (
            telemetry_summary_document,
            telemetry_summary_digest,
        ):
            for invalid in invalid_summaries:
                with self.subTest(
                    boundary=boundary.__name__, invalid=repr(invalid)
                ):
                    self.assertTelemetryInvalid(boundary, invalid)

    def test_serializer_retains_nonnegative_estimate_without_price_context(self):
        summary = _typed_summary()
        changed = replace(
            summary,
            usage=replace(
                summary.usage, estimated_cost_microunits=9_999
            ),
        )

        document = telemetry_summary_document(changed)

        self.assertEqual(
            document["usage"]["estimated_cost_microunits"], 9_999
        )
        self.assertRegex(
            telemetry_summary_digest(changed), r"^sha256:[0-9a-f]{64}$"
        )

    def test_semantically_equal_values_have_identical_documents_and_digests(self):
        original = _summary_document()
        reordered = OrderedDict(reversed(tuple(original.items())))
        reordered["usage"] = OrderedDict(
            reversed(tuple(original["usage"].items()))
        )
        price = _price_snapshot()
        reordered_price = OrderedDict(reversed(tuple(price.items())))

        left = telemetry_summary_from_document(original, price)
        right = telemetry_summary_from_document(
            reordered, reordered_price
        )

        self.assertEqual(
            canonical_bytes(telemetry_summary_document(left)),
            canonical_bytes(telemetry_summary_document(right)),
        )
        self.assertEqual(
            telemetry_summary_digest(left),
            telemetry_summary_digest(right),
        )

    def test_every_variable_typed_field_is_digest_bound(self):
        summary = _typed_summary()
        variants = {
            "response_digest": replace(
                summary, response_digest="sha256:" + "b" * 64
            ),
            "event_count": replace(summary, event_count=5),
            "input_tokens": replace(
                summary,
                usage=replace(
                    summary.usage,
                    input_tokens=12,
                    total_reported_tokens=17,
                ),
            ),
            "cached_input_tokens": replace(
                summary,
                usage=replace(summary.usage, cached_input_tokens=2),
            ),
            "output_tokens": replace(
                summary,
                usage=replace(
                    summary.usage,
                    output_tokens=6,
                    total_reported_tokens=17,
                ),
            ),
            "reasoning_output_tokens": replace(
                summary,
                usage=replace(
                    summary.usage, reasoning_output_tokens=1
                ),
            ),
            "estimated_cost_microunits": replace(
                summary,
                usage=replace(
                    summary.usage, estimated_cost_microunits=34
                ),
            ),
        }
        baseline = telemetry_summary_digest(summary)
        for field_name, variant in variants.items():
            with self.subTest(field=field_name):
                self.assertNotEqual(
                    telemetry_summary_digest(variant), baseline
                )

    def test_digest_normalizes_all_validation_failures(self):
        summary = _typed_summary()
        for invalid_usage_value in (1.5, "1", True):
            with self.subTest(invalid_usage_value=invalid_usage_value):
                invalid = replace(
                    summary,
                    usage=replace(
                        summary.usage,
                        estimated_cost_microunits=invalid_usage_value,
                    ),
                )
                self.assertTelemetryInvalid(
                    telemetry_summary_digest, invalid
                )

    def test_document_and_price_failures_are_always_normalized(self):
        for document, price in (
            (_WrongTelemetryErrorMapping(), _price_snapshot()),
            (_summary_document(), _WrongTelemetryErrorMapping()),
        ):
            with self.subTest(document=document, price=price):
                self.assertTelemetryInvalid(
                    telemetry_summary_from_document, document, price
                )


class _TelemetryParserTestCase(unittest.TestCase):
    def assertTelemetryCode(self, expected, function, *args, **kwargs):
        with self.assertRaises(TelemetryError) as raised:
            function(*args, **kwargs)
        self.assertIs(type(raised.exception), TelemetryError)
        self.assertEqual(str(raised.exception), expected)
        self.assertEqual(raised.exception.args, (expected,))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        return raised.exception

    def parse_data(self, data, *, limits=None, price=None):
        return experiment_telemetry.parse_terminal_telemetry(
            data,
            _telemetry_limits() if limits is None else limits,
            _price_snapshot() if price is None else price,
        )


class TelemetryParserLimitsAndFramingTests(_TelemetryParserTestCase):
    def parse(self, data, *, limits=None, price=None):
        return self.parse_data(data, limits=limits, price=price)

    def test_limits_api_has_exact_fields_constants_and_frozen_values(self):
        self.assertIsNotNone(
            getattr(experiment_telemetry, "TelemetryLimits", None)
        )
        self.assertEqual(
            tuple(
                field.name
                for field in fields(experiment_telemetry.TelemetryLimits)
            ),
            (
                "max_total_bytes",
                "max_line_bytes",
                "max_events",
                "max_token_value",
            ),
        )
        self.assertEqual(
            (
                experiment_telemetry.MAX_TOTAL_BYTES,
                experiment_telemetry.MAX_LINE_BYTES,
                experiment_telemetry.MAX_EVENTS,
                experiment_telemetry.MAX_TOKEN_VALUE,
                experiment_telemetry.MAX_JSON_DEPTH,
                experiment_telemetry.MIN_JSON_INTEGER,
                experiment_telemetry.MAX_JSON_INTEGER,
            ),
            (
                8_388_608,
                1_048_576,
                4_096,
                1_000_000_000,
                32,
                -9_223_372_036_854_775_808,
                9_223_372_036_854_775_807,
            ),
        )
        self.assertEqual(
            experiment_telemetry.PRICE_SNAPSHOT_READ_ORDER,
            (
                "model_id",
                "currency",
                "input_microunits_per_million",
                "cached_input_microunits_per_million",
                "output_microunits_per_million",
                "effective_at",
                "source_label",
            ),
        )
        limits = _telemetry_limits()
        with self.assertRaises(FrozenInstanceError):
            limits.max_events = 2

    def test_limits_constructor_rejects_wrong_types_bounds_and_relationships(self):
        ceilings = {
            "max_total_bytes": experiment_telemetry.MAX_TOTAL_BYTES,
            "max_line_bytes": experiment_telemetry.MAX_LINE_BYTES,
            "max_events": experiment_telemetry.MAX_EVENTS,
            "max_token_value": experiment_telemetry.MAX_TOKEN_VALUE,
        }
        experiment_telemetry.TelemetryLimits(**ceilings)

        for field_name, ceiling in ceilings.items():
            for invalid in (
                0,
                -1,
                True,
                1.0,
                "1",
                _IntSubclass(1),
                ceiling + 1,
            ):
                with self.subTest(field=field_name, invalid=invalid):
                    values = dict(ceilings)
                    values[field_name] = invalid
                    if (
                        field_name == "max_total_bytes"
                        and type(invalid) is int
                        and invalid > 0
                    ):
                        values["max_line_bytes"] = 1
                    self.assertTelemetryCode(
                        "telemetry_limits_invalid",
                        experiment_telemetry.TelemetryLimits,
                        **values,
                    )

        self.assertTelemetryCode(
            "telemetry_limits_invalid",
            experiment_telemetry.TelemetryLimits,
            max_total_bytes=10,
            max_line_bytes=11,
            max_events=1,
            max_token_value=1,
        )
        subclass = type(
            "_ForgedTelemetryLimitsSubclass",
            (experiment_telemetry.TelemetryLimits,),
            {},
        )
        self.assertTelemetryCode(
            "telemetry_limits_invalid",
            subclass,
            10,
            10,
            1,
            1,
        )

    def test_parse_revalidates_forged_limit_instances_and_exact_field_set(self):
        limit_type = experiment_telemetry.TelemetryLimits
        missing = object.__new__(limit_type)
        for name, value in (
            ("max_total_bytes", 100_000),
            ("max_line_bytes", 20_000),
            ("max_events", 100),
        ):
            object.__setattr__(missing, name, value)

        extra = _telemetry_limits()
        object.__setattr__(extra, "unexpected", 1)

        invalid_value = object.__new__(limit_type)
        for name, value in (
            ("max_total_bytes", 100_000),
            ("max_line_bytes", 20_000),
            ("max_events", 100),
            ("max_token_value", True),
        ):
            object.__setattr__(invalid_value, name, value)

        data = _jsonl(_terminal_events())
        for forged in (missing, extra, invalid_value):
            with self.subTest(forged=vars(forged)):
                self.assertTelemetryCode(
                    "telemetry_limits_invalid",
                    experiment_telemetry.parse_terminal_telemetry,
                    data,
                    forged,
                    _price_snapshot(),
                )

    def test_bytes_type_check_precedes_forged_limits_and_other_work(self):
        forged = object.__new__(experiment_telemetry.TelemetryLimits)
        for data in (bytearray(b"x"), memoryview(b"x"), "x", None):
            with self.subTest(data_type=type(data).__name__):
                self.assertTelemetryCode(
                    "telemetry_not_bytes",
                    experiment_telemetry.parse_terminal_telemetry,
                    data,
                    forged,
                    _price_snapshot(),
                )

    def test_total_size_and_truncation_checks_follow_the_fixed_order(self):
        data = _jsonl(_terminal_events())
        largest = max(len(line) for line in data[:-1].split(b"\n"))
        exact = _telemetry_limits(
            max_total_bytes=len(data), max_line_bytes=largest
        )
        self.assertEqual(self.parse(data, limits=exact).event_count, 4)

        self.assertTelemetryCode(
            "telemetry_size_invalid",
            self.parse,
            b"",
        )
        self.assertTelemetryCode(
            "telemetry_size_invalid",
            self.parse,
            data,
            limits=_telemetry_limits(
                max_total_bytes=len(data) - 1,
                max_line_bytes=largest,
            ),
        )
        self.assertTelemetryCode(
            "telemetry_truncated",
            self.parse,
            data[:-1],
        )

    def test_lf_only_and_blank_record_rules_are_exact(self):
        data = _jsonl(_terminal_events())
        crlf = data.replace(b"\n", b"\r\n")
        bare_cr = data.replace(b"thread-1", b"thread\r1")
        blank_variants = (
            b"\n" + data,
            data.replace(b"\n", b"\n\n", 1),
            data + b"\n",
            b"\n",
        )
        for invalid in (crlf, bare_cr):
            with self.subTest(kind="carriage-return"):
                self.assertTelemetryCode(
                    "telemetry_line_invalid", self.parse, invalid
                )
        for invalid in blank_variants:
            with self.subTest(kind="blank-record", data=invalid[:20]):
                self.assertTelemetryCode(
                    "telemetry_line_invalid", self.parse, invalid
                )

    def test_line_total_and_event_caps_accept_boundary_and_reject_one_over(self):
        data = _jsonl(_terminal_events())
        line_lengths = [len(line) for line in data[:-1].split(b"\n")]
        largest = max(line_lengths)
        boundary = _telemetry_limits(
            max_total_bytes=len(data),
            max_line_bytes=largest,
            max_events=4,
        )
        self.assertEqual(self.parse(data, limits=boundary).event_count, 4)

        self.assertTelemetryCode(
            "telemetry_line_invalid",
            self.parse,
            data,
            limits=_telemetry_limits(max_line_bytes=largest - 1),
        )
        self.assertTelemetryCode(
            "telemetry_event_count_invalid",
            self.parse,
            data,
            limits=_telemetry_limits(max_events=3),
        )

    def test_json_depth_scan_is_quote_escape_aware_and_enforces_boundary(self):
        noisy = {
            "brackets": "[" * 80 + "]" * 80,
            "escaped": '\\"{' * 40,
        }
        accepted_value = 0
        for _ in range(experiment_telemetry.MAX_JSON_DEPTH - 1):
            accepted_value = [accepted_value]
        accepted_text = json.dumps(
            {"value": accepted_value},
            separators=(",", ":"),
        )
        self.assertEqual(
            self.parse(_jsonl(_terminal_events(accepted_text))).event_count,
            4,
        )
        self.assertEqual(
            self.parse(
                _jsonl(
                    _terminal_events(
                        json.dumps(noisy, separators=(",", ":"))
                    )
                )
            ).event_count,
            4,
        )

        rejected_value = [accepted_value]
        rejected_text = json.dumps(
            {"value": rejected_value}, separators=(",", ":")
        )
        self.assertTelemetryCode(
            "telemetry_stream_invalid",
            self.parse,
            _jsonl(_terminal_events(rejected_text)),
        )

    def test_json_integer_scan_enforces_signed_64_bit_before_projection(self):
        for accepted in (
            experiment_telemetry.MIN_JSON_INTEGER,
            experiment_telemetry.MAX_JSON_INTEGER,
        ):
            with self.subTest(boundary=accepted):
                response = json.dumps(
                    {"boundary": accepted}, separators=(",", ":")
                )
                self.assertEqual(
                    self.parse(_jsonl(_terminal_events(response))).event_count,
                    4,
                )

        for rejected in (
            "-9223372036854775809",
            "9223372036854775808",
            "999999999999999999999999999999999999999999999999",
        ):
            with self.subTest(rejected=rejected):
                response = '{"boundary":' + rejected + "}"
                self.assertTelemetryCode(
                    "telemetry_stream_invalid",
                    self.parse,
                    _jsonl(_terminal_events(response)),
                )


class TelemetryParserSchemaAndStateTests(_TelemetryParserTestCase):
    def parse(self, events):
        return self.parse_data(_jsonl(events))

    def assertStreamInvalid(self, events):
        self.assertTelemetryCode(
            "telemetry_stream_invalid", self.parse, events
        )

    def _stream_for_variant(self, variant_name, item):
        prefix = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        ]
        if variant_name == "agent_message":
            return prefix + [
                _item_event("completed", item),
                {"type": "turn.completed", "usage": _terminal_usage()},
            ]
        if variant_name == "todo_list":
            return prefix + [
                _item_event("started", _todo_item(item_id=item.get("id", "todo-1"))),
                _item_event("completed", item),
                _item_event("completed", _agent_item()),
                {"type": "turn.completed", "usage": _terminal_usage()},
            ]
        return prefix + [
            _item_event("completed", item),
            _item_event("completed", _agent_item()),
            {"type": "turn.completed", "usage": _terminal_usage()},
        ]

    def test_source_shaped_item_subset_and_supported_lifecycles_succeed(self):
        item_events = [
            _item_event(
                "started",
                _command_item(status="in_progress", exit_code=None),
            ),
            _item_event(
                "completed",
                _command_item(status="failed", exit_code=-1),
            ),
            _item_event(
                "started",
                _file_item(status="in_progress"),
            ),
            _item_event(
                "completed",
                _file_item(status="failed"),
            ),
            _item_event("started", _todo_item()),
            _item_event(
                "updated",
                _todo_item(
                    items=[{"text": "verify", "completed": True}]
                ),
            ),
            _item_event(
                "completed",
                _todo_item(
                    items=[{"text": "verify", "completed": True}]
                ),
            ),
            _item_event("completed", _reasoning_item()),
            _item_event(
                "completed",
                _command_item(
                    item_id="command-direct",
                    status="declined",
                    exit_code=None,
                ),
            ),
            _item_event(
                "completed",
                _file_item(item_id="file-direct"),
            ),
            _item_event("completed", _error_item()),
        ]
        events = _successful_events_with_items(item_events)

        summary = self.parse(events)

        self.assertEqual(summary.event_count, len(events))
        self.assertEqual(summary.classification, "completed")

    def test_top_level_event_envelopes_are_exact(self):
        baseline = _terminal_events()
        expected_keys = (
            {"type", "thread_id"},
            {"type"},
            {"type", "item"},
            {"type", "usage"},
        )
        for index, keys in enumerate(expected_keys):
            for key in keys:
                with self.subTest(index=index, mutation="missing", key=key):
                    events = _terminal_events()
                    del events[index][key]
                    self.assertStreamInvalid(events)
            with self.subTest(index=index, mutation="unknown"):
                events = _terminal_events()
                events[index]["unknown"] = "sentinel-envelope"
                self.assertStreamInvalid(events)
            with self.subTest(index=index, mutation="wrong_type"):
                events = _terminal_events()
                events[index]["type"] = 1
                self.assertStreamInvalid(events)

        for top_level in ([], "event", 1, True, None):
            with self.subTest(top_level=top_level):
                events = list(baseline)
                events[2] = top_level
                self.assertStreamInvalid(events)

    def test_failure_envelopes_are_rejection_markers_only(self):
        prefix = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        ]
        failure_events = (
            {"type": "error", "message": ""},
            {"type": "turn.failed", "error": {"message": "failed"}},
        )
        for failure in failure_events:
            with self.subTest(failure=failure["type"]):
                self.assertStreamInvalid(prefix + [failure])
                self.assertStreamInvalid(
                    prefix
                    + [failure, {"type": "turn.completed", "usage": _terminal_usage()}]
                )

        malformed = (
            {"type": "error"},
            {"type": "error", "message": "", "unknown": 1},
            {"type": "error", "message": 1},
            {"type": "turn.failed", "error": {}},
            {
                "type": "turn.failed",
                "error": {"message": "", "unknown": 1},
            },
            {"type": "turn.failed", "error": {"message": 1}},
        )
        for failure in malformed:
            with self.subTest(failure=failure):
                self.assertStreamInvalid(prefix + [failure])

    def test_each_item_variant_requires_its_exact_schema(self):
        samples = {
            "agent_message": _agent_item(),
            "reasoning": _reasoning_item(),
            "command_execution": _command_item(),
            "file_change": _file_item(),
            "todo_list": _todo_item(),
            "error": _error_item(),
        }
        for variant_name, sample in samples.items():
            with self.subTest(variant=variant_name, mutation="valid"):
                self.assertEqual(
                    self.parse(
                        self._stream_for_variant(variant_name, dict(sample))
                    ).classification,
                    "completed",
                )
            for key in sample:
                with self.subTest(
                    variant=variant_name, mutation="missing", key=key
                ):
                    item = dict(sample)
                    del item[key]
                    self.assertStreamInvalid(
                        self._stream_for_variant(variant_name, item)
                    )
            with self.subTest(variant=variant_name, mutation="unknown"):
                item = dict(sample)
                item["unknown"] = "sentinel-item"
                self.assertStreamInvalid(
                    self._stream_for_variant(variant_name, item)
                )

        for disabled in (
            "mcp_tool_call",
            "collab_tool_call",
            "web_search",
            "unknown",
        ):
            with self.subTest(disabled=disabled):
                item = {"id": "disabled-1", "type": disabled}
                self.assertStreamInvalid(
                    [
                        {"type": "thread.started", "thread_id": "thread-1"},
                        {"type": "turn.started"},
                        _item_event("completed", item),
                    ]
                )

    def test_item_identifiers_and_strings_require_exact_nfc_text(self):
        for invalid_id in ("", 1, True, None, "e\u0301"):
            with self.subTest(field="id", invalid=repr(invalid_id)):
                self.assertStreamInvalid(
                    self._stream_for_variant(
                        "reasoning",
                        _reasoning_item(item_id=invalid_id),
                    )
                )

        for item, field in (
            (_reasoning_item(), "text"),
            (_command_item(), "command"),
            (_command_item(), "aggregated_output"),
            (_error_item(), "message"),
        ):
            for invalid in (1, True, None, "e\u0301"):
                with self.subTest(
                    item=item["type"], field=field, invalid=repr(invalid)
                ):
                    changed = dict(item)
                    changed[field] = invalid
                    self.assertStreamInvalid(
                        self._stream_for_variant(item["type"], changed)
                    )

        empty_strings = (
            _reasoning_item(text=""),
            _command_item(command="", output=""),
            _error_item(message=""),
        )
        for item in empty_strings:
            with self.subTest(empty_allowed=item["type"]):
                self.assertEqual(
                    self.parse(
                        self._stream_for_variant(item["type"], item)
                    ).classification,
                    "completed",
                )

    def test_command_schema_status_exit_code_and_started_rules_are_exact(self):
        prefix = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        ]
        for status in ("in_progress", "completed", "failed", "declined"):
            for exit_code in (None, -2_147_483_648, 0, 2_147_483_647):
                with self.subTest(status=status, exit_code=exit_code):
                    events = _successful_events_with_items(
                        [
                            _item_event(
                                "completed",
                                _command_item(
                                    item_id="direct",
                                    status=status,
                                    exit_code=exit_code,
                                ),
                            )
                        ]
                    )
                    self.assertEqual(
                        self.parse(events).classification, "completed"
                    )

        for exit_code in (
            True,
            1.0,
            "0",
            -2_147_483_649,
            2_147_483_648,
        ):
            with self.subTest(exit_code=exit_code):
                self.assertStreamInvalid(
                    _successful_events_with_items(
                        [
                            _item_event(
                                "completed",
                                _command_item(exit_code=exit_code),
                            )
                        ]
                    )
                )

        for status in ("running", "", 1, None):
            with self.subTest(status=status):
                self.assertStreamInvalid(
                    _successful_events_with_items(
                        [
                            _item_event(
                                "completed",
                                _command_item(status=status),
                            )
                        ]
                    )
                )

        invalid_starts = (
            _command_item(status="completed", exit_code=None),
            _command_item(status="in_progress", exit_code=0),
        )
        for item in invalid_starts:
            with self.subTest(item=item):
                self.assertStreamInvalid(prefix + [_item_event("started", item)])

    def test_file_change_and_todo_nested_shapes_are_exact(self):
        valid_changes = [
            {"path": "", "kind": "add"},
            {"path": "a", "kind": "delete"},
            {"path": "b", "kind": "update"},
        ]
        self.assertEqual(
            self.parse(
                _successful_events_with_items(
                    [
                        _item_event(
                            "completed",
                            _file_item(changes=valid_changes),
                        )
                    ]
                )
            ).classification,
            "completed",
        )
        invalid_changes = (
            [{"path": "a"}],
            [{"path": "a", "kind": "update", "unknown": 1}],
            [{"path": 1, "kind": "update"}],
            [{"path": "e\u0301", "kind": "update"}],
            [{"path": "a", "kind": "move"}],
            {"path": "a", "kind": "update"},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                self.assertStreamInvalid(
                    _successful_events_with_items(
                        [
                            _item_event(
                                "completed",
                                _file_item(changes=changes),
                            )
                        ]
                    )
                )

        valid_todos = [
            {"text": "", "completed": False},
            {"text": "done", "completed": True},
        ]
        todo_events = [
            _item_event("started", _todo_item(items=valid_todos)),
            _item_event("completed", _todo_item(items=valid_todos)),
        ]
        self.assertEqual(
            self.parse(_successful_events_with_items(todo_events)).classification,
            "completed",
        )
        invalid_todos = (
            [{"text": "a"}],
            [{"text": "a", "completed": False, "unknown": 1}],
            [{"text": 1, "completed": False}],
            [{"text": "e\u0301", "completed": False}],
            [{"text": "a", "completed": 1}],
            {"text": "a", "completed": False},
        )
        for items in invalid_todos:
            with self.subTest(items=items):
                self.assertStreamInvalid(
                    [
                        {"type": "thread.started", "thread_id": "thread-1"},
                        {"type": "turn.started"},
                        _item_event("started", _todo_item(items=items)),
                    ]
                )

        for status in ("in_progress", "completed", "failed"):
            with self.subTest(file_status=status):
                self.assertEqual(
                    self.parse(
                        _successful_events_with_items(
                            [
                                _item_event(
                                    "completed",
                                    _file_item(status=status),
                                )
                            ]
                        )
                    ).classification,
                    "completed",
                )
        for status in ("unknown", "", 1):
            with self.subTest(file_status=status):
                self.assertStreamInvalid(
                    _successful_events_with_items(
                        [
                            _item_event(
                                "completed",
                                _file_item(status=status),
                            )
                        ]
                    )
                )

    def test_dfa_rejects_missing_duplicate_and_out_of_order_turn_events(self):
        valid = _terminal_events()
        mutations = (
            valid[1:],
            [valid[0], valid[0], *valid[1:]],
            [valid[0], *valid[2:]],
            [valid[0], valid[1], valid[1], *valid[2:]],
            [valid[1], valid[0], *valid[2:]],
            [valid[0], valid[2], valid[1], valid[3]],
            [valid[0]],
            valid[:2],
            valid[:3],
        )
        for index, events in enumerate(mutations):
            with self.subTest(index=index):
                self.assertStreamInvalid(events)

    def test_lifecycle_matrix_and_item_identity_transitions_are_exact(self):
        prefix = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        ]
        terminal = [
            _item_event("completed", _agent_item()),
            {"type": "turn.completed", "usage": _terminal_usage()},
        ]

        for item in (
            _agent_item(),
            _reasoning_item(),
            _error_item(),
        ):
            with self.subTest(disallowed_start=item["type"]):
                self.assertStreamInvalid(
                    prefix + [_item_event("started", item)] + terminal
                )

        for item in (
            _agent_item(),
            _reasoning_item(),
            _command_item(),
            _file_item(),
            _error_item(),
        ):
            with self.subTest(disallowed_update=item["type"]):
                self.assertStreamInvalid(
                    prefix + [_item_event("updated", item)] + terminal
                )

        self.assertStreamInvalid(
            prefix
            + [_item_event("completed", _todo_item())]
            + terminal
        )
        self.assertStreamInvalid(
            prefix
            + [
                _item_event("started", _todo_item()),
                _item_event(
                    "updated",
                    _todo_item(
                        item_id="different",
                    ),
                ),
            ]
            + terminal
        )
        self.assertStreamInvalid(
            prefix
            + [
                _item_event(
                    "started",
                    _command_item(status="in_progress", exit_code=None),
                ),
                _item_event("completed", _file_item(item_id="command-1")),
            ]
            + terminal
        )
        self.assertStreamInvalid(
            prefix
            + [
                _item_event("completed", _reasoning_item()),
                _item_event("completed", _reasoning_item()),
            ]
            + terminal
        )

    def test_terminal_requires_closed_items_one_response_and_final_position(self):
        prefix = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        ]
        terminal = {"type": "turn.completed", "usage": _terminal_usage()}
        self.assertStreamInvalid(prefix + [terminal])
        self.assertStreamInvalid(
            prefix
            + [
                _item_event(
                    "started",
                    _command_item(status="in_progress", exit_code=None),
                ),
                _item_event("completed", _agent_item()),
                terminal,
            ]
        )
        self.assertStreamInvalid(
            prefix
            + [
                _item_event("completed", _agent_item()),
                _item_event(
                    "completed",
                    _agent_item(item_id="message-2", text='{"second":true}'),
                ),
                terminal,
            ]
        )
        for trailing in (
            {"type": "turn.completed", "usage": _terminal_usage()},
            {"type": "error", "message": "after"},
            _item_event("completed", _reasoning_item()),
        ):
            with self.subTest(trailing=trailing["type"]):
                self.assertStreamInvalid(_terminal_events() + [trailing])


class TelemetryParserProjectionAndPriceTests(_TelemetryParserTestCase):
    def parse(self, events, *, limits=None, price=None):
        return self.parse_data(
            _jsonl(events), limits=limits, price=price
        )

    def assertStreamInvalidData(self, data, *, limits=None, price=None):
        self.assertTelemetryCode(
            "telemetry_stream_invalid",
            self.parse_data,
            data,
            limits=limits,
            price=price,
        )

    def test_price_mapping_is_read_once_in_exact_fixed_order(self):
        reversed_keys = tuple(
            reversed(experiment_telemetry.PRICE_SNAPSHOT_READ_ORDER)
        )
        price = _TrackingPriceMapping(reversed_keys)

        summary = self.parse(_terminal_events(), price=price)

        self.assertEqual(summary.usage.estimated_cost_microunits, 33)
        self.assertEqual(
            price.read_order,
            list(experiment_telemetry.PRICE_SNAPSHOT_READ_ORDER),
        )
        self.assertEqual(
            price.read_order.count("model_id"),
            1,
        )

    def test_price_iterator_is_bounded_and_duplicate_keys_fail_closed(self):
        expected = experiment_telemetry.PRICE_SNAPSHOT_READ_ORDER
        duplicate = _TrackingPriceMapping(
            (
                expected[0],
                expected[0],
                *expected[1:-1],
            )
        )
        self.assertTelemetryCode(
            "telemetry_summary_invalid",
            self.parse,
            _terminal_events(),
            price=duplicate,
        )
        self.assertLessEqual(duplicate.iteration_count, len(expected) + 1)
        self.assertEqual(duplicate.read_order, [])

        extra_keys = tuple("extra-{}".format(index) for index in range(20))
        values = _price_snapshot()
        values.update({key: index for index, key in enumerate(extra_keys)})
        overlong = _TrackingPriceMapping(expected + extra_keys, values)
        self.assertTelemetryCode(
            "telemetry_summary_invalid",
            self.parse,
            _terminal_events(),
            price=overlong,
        )
        self.assertEqual(overlong.iteration_count, len(expected) + 1)
        self.assertEqual(overlong.read_order, [])

    def test_invalid_stream_never_reads_the_price_mapping(self):
        price = _TrackingPriceMapping(
            experiment_telemetry.PRICE_SNAPSHOT_READ_ORDER
        )
        events = _terminal_events()
        events[-1]["usage"]["cache_write_input_tokens"] = 1

        self.assertTelemetryCode(
            "telemetry_stream_invalid",
            self.parse,
            events,
            price=price,
        )

        self.assertEqual(price.iteration_count, 0)
        self.assertEqual(price.read_order, [])

    def test_detached_price_snapshot_prevents_second_original_reads(self):
        changed = _price_snapshot()
        changed["input_microunits_per_million"] = 9_000_000
        price = _ChangingValueMapping(_price_snapshot(), changed)

        summary = self.parse(_terminal_events(), price=price)

        self.assertEqual(summary.usage.estimated_cost_microunits, 33)
        self.assertEqual(
            price.read_counts,
            {
                key: 1
                for key in experiment_telemetry.PRICE_SNAPSHOT_READ_ORDER
            },
        )

    def test_structured_response_requires_exactly_one_json_object(self):
        invalid_responses = (
            "",
            "null",
            "true",
            "1",
            "[]",
            '{"a":1} {"b":2}',
            '{"a":1,"a":2}',
            '{"value":1.5}',
            '{"value":1e2}',
            '{"value":NaN}',
            '{"value":"e\\u0301"}',
            '{"\\u0061":1,"a":2}',
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                self.assertStreamInvalidData(
                    _jsonl(_terminal_events(response))
                )

    def test_semantic_response_and_wire_format_equivalence_is_canonical(self):
        left = _terminal_events('{"b":2,"a":{"y":true,"x":null}}')
        right = [
            OrderedDict(
                (
                    ("thread_id", "thread-1"),
                    ("type", "thread.started"),
                )
            ),
            OrderedDict((("type", "turn.started"),)),
            OrderedDict(
                (
                    (
                        "item",
                        OrderedDict(
                            (
                                (
                                    "text",
                                    '{ "a": { "x": null, "y": true }, "b": 2 }',
                                ),
                                ("type", "agent_message"),
                                ("id", "message-1"),
                            )
                        ),
                    ),
                    ("type", "item.completed"),
                )
            ),
            OrderedDict(
                (
                    (
                        "usage",
                        OrderedDict(
                            reversed(tuple(_terminal_usage().items()))
                        ),
                    ),
                    ("type", "turn.completed"),
                )
            ),
        ]

        left_summary = self.parse_data(_jsonl(left))
        right_summary = self.parse_data(
            _jsonl(right, separators=(", ", ": "))
        )

        self.assertEqual(left_summary, right_summary)
        self.assertEqual(
            canonical_bytes(telemetry_summary_document(left_summary)),
            canonical_bytes(telemetry_summary_document(right_summary)),
        )
        self.assertEqual(
            telemetry_summary_digest(left_summary),
            telemetry_summary_digest(right_summary),
        )
        expected_response = {"a": {"x": None, "y": True}, "b": 2}
        self.assertEqual(
            left_summary.response_digest,
            "sha256:"
            + hashlib.sha256(
                canonical_bytes(expected_response)
            ).hexdigest(),
        )

    def test_wire_usage_requires_exact_five_field_zero_write_shape(self):
        baseline = _terminal_usage()
        for key in baseline:
            with self.subTest(mutation="missing", key=key):
                usage = dict(baseline)
                del usage[key]
                self.assertStreamInvalidData(
                    _jsonl(_terminal_events(usage=usage))
                )

        usage = dict(baseline)
        usage["unknown"] = 0
        self.assertStreamInvalidData(
            _jsonl(_terminal_events(usage=usage))
        )

        four_field = dict(baseline)
        del four_field["cache_write_input_tokens"]
        self.assertStreamInvalidData(
            _jsonl(_terminal_events(usage=four_field))
        )

        data = _jsonl(_terminal_events())
        duplicate = data.replace(
            b'"input_tokens":11',
            b'"input_tokens":11,"input_tokens":11',
        )
        self.assertNotEqual(duplicate, data)
        self.assertStreamInvalidData(duplicate)

    def test_each_usage_value_enforces_exact_type_sign_and_configured_cap(self):
        for field_name in _terminal_usage():
            for invalid in (True, -1, 1.5, "1"):
                with self.subTest(field=field_name, invalid=invalid):
                    usage = _terminal_usage()
                    usage[field_name] = invalid
                    self.assertStreamInvalidData(
                        _jsonl(_terminal_events(usage=usage))
                    )

            with self.subTest(field=field_name, invalid="over-cap"):
                usage = _terminal_usage()
                usage[field_name] = 101
                self.assertStreamInvalidData(
                    _jsonl(_terminal_events(usage=usage)),
                    limits=_telemetry_limits(max_token_value=100),
                )

    def test_usage_subset_total_and_zero_cache_write_rules_are_exact(self):
        invalid_usages = (
            _terminal_usage(cached_input_tokens=12),
            _terminal_usage(reasoning_output_tokens=6),
            _terminal_usage(cache_write_input_tokens=1),
            _terminal_usage(input_tokens=60, output_tokens=41),
        )
        for usage in invalid_usages:
            with self.subTest(usage=usage):
                self.assertStreamInvalidData(
                    _jsonl(_terminal_events(usage=usage)),
                    limits=_telemetry_limits(max_token_value=100),
                )

        boundary = _terminal_usage(
            input_tokens=60,
            cached_input_tokens=60,
            output_tokens=40,
            reasoning_output_tokens=40,
        )
        summary = self.parse(
            _terminal_events(usage=boundary),
            limits=_telemetry_limits(max_token_value=100),
        )
        self.assertEqual(summary.usage.total_reported_tokens, 100)

    def test_typed_projection_cost_total_and_digest_binding_are_exact(self):
        summary = self.parse(_terminal_events())

        self.assertIs(type(summary), TelemetrySummary)
        self.assertIs(type(summary.usage), UsageSummary)
        self.assertEqual(summary.classification, "completed")
        self.assertEqual(summary.raw_retention, "discard")
        self.assertEqual(summary.event_count, 4)
        self.assertEqual(summary.usage.input_tokens, 11)
        self.assertEqual(summary.usage.cached_input_tokens, 3)
        self.assertEqual(summary.usage.output_tokens, 5)
        self.assertEqual(summary.usage.reasoning_output_tokens, 2)
        self.assertEqual(summary.usage.total_reported_tokens, 16)
        self.assertEqual(summary.usage.estimated_cost_microunits, 33)

        reasoning_changed = self.parse(
            _terminal_events(
                usage=_terminal_usage(reasoning_output_tokens=0)
            )
        )
        self.assertEqual(
            reasoning_changed.usage.estimated_cost_microunits, 33
        )
        self.assertNotEqual(
            telemetry_summary_digest(reasoning_changed),
            telemetry_summary_digest(summary),
        )

        usage_changed = self.parse(
            _terminal_events(
                usage=_terminal_usage(
                    input_tokens=12,
                    cached_input_tokens=3,
                )
            )
        )
        self.assertNotEqual(
            telemetry_summary_digest(usage_changed),
            telemetry_summary_digest(summary),
        )

    def test_raw_provider_values_and_response_text_never_escape_projection(self):
        sentinels = (
            "SENTINEL-REASONING",
            "SENTINEL-COMMAND",
            "SENTINEL-OUTPUT",
            "SENTINEL-ERROR",
            "SENTINEL-RESPONSE",
        )
        events = _successful_events_with_items(
            [
                _item_event(
                    "completed",
                    _reasoning_item(text=sentinels[0]),
                ),
                _item_event(
                    "completed",
                    _command_item(
                        command=sentinels[1],
                        output=sentinels[2],
                    ),
                ),
                _item_event(
                    "completed",
                    _error_item(message=sentinels[3]),
                ),
            ]
        )
        events[-2] = _item_event(
            "completed",
            _agent_item(text='{"private":"' + sentinels[4] + '"}'),
        )
        data = _jsonl(events)

        summary = self.parse_data(data)
        public = (
            repr(summary)
            + repr(telemetry_summary_document(summary))
            + telemetry_summary_digest(summary)
        )

        self.assertNotIn(repr(data), public)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, public)

        invalid = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "error", "message": "SENTINEL-FAILURE"},
        ]
        with self.assertRaises(TelemetryError) as raised:
            self.parse(invalid)
        exposed = (
            str(raised.exception)
            + repr(raised.exception)
            + repr(raised.exception.args)
        )
        self.assertNotIn("SENTINEL-FAILURE", exposed)


class TelemetryParserFixtureIntegrationTests(_TelemetryParserTestCase):
    fixtures = (
        Path(__file__).parent / "fixtures" / "harness_experiment"
    )

    def fixture_bytes(self, name):
        return (self.fixtures / name).read_bytes()

    def test_public_failures_detach_internal_exception_contexts(self):
        valid = self.fixture_bytes("valid-terminal.jsonl")
        malformed = (
            b'{"type":"thread.started","SENTINEL-JSON-CONTEXT":\n'
            + b"".join(valid.splitlines(keepends=True)[1:])
        )
        stream_error = self.assertTelemetryCode(
            "telemetry_stream_invalid",
            self.parse_data,
            malformed,
        )
        price_error = self.assertTelemetryCode(
            "telemetry_summary_invalid",
            self.parse_data,
            valid,
            price=_ExplodingPriceMapping(),
        )

        for error in (stream_error, price_error):
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            exposed = str(error) + repr(error) + repr(error.args)
            self.assertNotIn("SENTINEL", exposed)

    def test_invalid_price_scalars_are_rejected_before_any_arithmetic(self):
        sentinel = _ArithmeticSentinel()
        price = _price_snapshot()
        price["input_microunits_per_million"] = sentinel

        self.assertTelemetryCode(
            "telemetry_summary_invalid",
            self.parse_data,
            _jsonl(_terminal_events()),
            price=price,
        )

        self.assertIs(sentinel.touched, False)

    def test_tracked_fixture_set_and_successful_semantic_pair_are_exact(self):
        expected_names = {
            "valid-terminal.jsonl",
            "duplicate-usage.jsonl",
            "usage-after-error.jsonl",
            "truncated-terminal.jsonl",
            "semantic-order-spacing.jsonl",
        }
        actual_names = {
            path.name for path in self.fixtures.glob("*.jsonl")
        }
        self.assertEqual(actual_names, expected_names)

        valid_data = self.fixture_bytes("valid-terminal.jsonl")
        semantic_data = self.fixture_bytes(
            "semantic-order-spacing.jsonl"
        )
        valid = self.parse_data(valid_data)
        semantic = self.parse_data(semantic_data)

        self.assertEqual(valid, semantic)
        self.assertEqual(
            telemetry_summary_digest(valid),
            telemetry_summary_digest(semantic),
        )
        self.assertEqual(valid.event_count, 4)
        for data in (valid_data, semantic_data):
            self.assertTrue(data.endswith(b"\n"))
            self.assertNotIn(b"sk-", data)
            self.assertNotIn(b"BEGIN " + b"PRIVATE", data)

    def test_tracked_failure_fixtures_fail_with_fixed_public_codes(self):
        cases = (
            ("duplicate-usage.jsonl", "telemetry_stream_invalid"),
            ("usage-after-error.jsonl", "telemetry_stream_invalid"),
            ("truncated-terminal.jsonl", "telemetry_stream_invalid"),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                data = self.fixture_bytes(name)
                error = self.assertTelemetryCode(
                    expected, self.parse_data, data
                )
                exposed = (
                    str(error) + repr(error) + repr(error.args)
                )
                self.assertNotIn("SENTINEL", exposed)

        valid = self.fixture_bytes("valid-terminal.jsonl")
        self.assertTelemetryCode(
            "telemetry_truncated",
            self.parse_data,
            valid[:-1],
        )

    def test_semantic_json_mutations_fail_without_decoder_or_payload_leakage(self):
        valid = self.fixture_bytes("valid-terminal.jsonl")
        lines = valid.splitlines(keepends=True)
        mutations = {
            "invalid_utf8": (
                b'{"type":"thread.started","thread_id":"\xff"}\n'
                + b"".join(lines[1:])
            ),
            "bom": b"\xef\xbb\xbf" + valid,
            "lone_surrogate": (
                b'{"type":"thread.started","thread_id":"\\ud800"}\n'
                + b"".join(lines[1:])
            ),
            "duplicate_top_key": valid.replace(
                b'{"type":"thread.started"',
                b'{"type":"thread.started","type":"thread.started"',
                1,
            ),
            "non_nfc_key": valid.replace(
                b'"thread_id"',
                '"thre\u0301ad_id"'.encode("utf-8"),
                1,
            ),
            "non_nfc_value": valid.replace(
                b'"thread-fixture"',
                '"thre\u0301ad-fixture"'.encode("utf-8"),
                1,
            ),
            "top_array": (
                b"[]\n" + b"".join(lines[1:])
            ),
            "decoder_message_sentinel": (
                b'{"type":"thread.started","SENTINEL-JSON":\n'
                + b"".join(lines[1:])
            ),
        }
        for name, data in mutations.items():
            with self.subTest(name=name):
                error = self.assertTelemetryCode(
                    "telemetry_stream_invalid",
                    self.parse_data,
                    data,
                )
                exposed = (
                    str(error) + repr(error) + repr(error.args)
                )
                self.assertNotIn("SENTINEL", exposed)
                self.assertNotIn("line", exposed.lower())
                self.assertNotIn("column", exposed.lower())

    def test_event_and_nested_container_type_mutations_fail_closed(self):
        mutations = []
        for field_name, invalid in (
            ("thread_id", ""),
            ("thread_id", 1),
        ):
            events = _terminal_events()
            events[0][field_name] = invalid
            mutations.append(events)

        for invalid_item in (None, [], "item", 1):
            events = _terminal_events()
            events[2]["item"] = invalid_item
            mutations.append(events)

        for invalid_usage in (None, [], "usage", 1):
            events = _terminal_events()
            events[-1]["usage"] = invalid_usage
            mutations.append(events)

        for events in mutations:
            with self.subTest(events=events):
                self.assertTelemetryCode(
                    "telemetry_stream_invalid",
                    self.parse_data,
                    _jsonl(events),
                )


if __name__ == "__main__":
    unittest.main()
