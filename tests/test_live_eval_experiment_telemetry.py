from collections import OrderedDict
from dataclasses import FrozenInstanceError, dataclass, fields, replace
import hashlib
from types import MappingProxyType
import unittest

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


@dataclass(frozen=True)
class _UsageSummarySubclass(UsageSummary):
    extra: int = 0


@dataclass(frozen=True)
class _TelemetrySummarySubclass(TelemetrySummary):
    extra: int = 0


class _StringSubclass(str):
    pass


class _WrongTelemetryErrorMapping(dict):
    def keys(self):
        raise TelemetryError("wrong_error")


class TelemetrySummaryCodecTests(unittest.TestCase):
    def assertTelemetryInvalid(self, function, *args):
        with self.assertRaises(TelemetryError) as raised:
            function(*args)
        self.assertIs(type(raised.exception), TelemetryError)
        self.assertEqual(str(raised.exception), "telemetry_summary_invalid")

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


if __name__ == "__main__":
    unittest.main()
