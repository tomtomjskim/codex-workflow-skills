"""Canonical, immutable planning contracts for the Phase A experiment."""

from collections.abc import Mapping as ABCMapping
from collections.abc import Sequence as ABCSequence
from dataclasses import dataclass, field, fields
import hashlib
import re
from types import MappingProxyType
from typing import Mapping, Sequence, Tuple
import unicodedata

from scripts.workflow_coordination.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    load_canonical_input,
)


class ExperimentPlanError(ValueError):
    """Raised when an experiment input or immutable plan is invalid."""


_INPUT_ERROR = "experiment_input_invalid"
_PLAN_ERROR = "experiment_plan_invalid"
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_FORMATTING_ONLY_CATEGORIES = frozenset(
    {"formatting", "parsing", "syntax_only"}
)

_EXPERIMENT_KEYS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "selection_seed",
        "selection_rule",
        "model",
        "candidates",
        "analysis_contract_version",
        "masking_contract_version",
        "containment_policy_version",
        "retention",
        "budgets",
        "price_snapshot",
        "provider_cap_evidence",
        "external_prerequisite_receipt_digests",
        "invocation_policy",
    }
)
_MODEL_KEYS = frozenset(
    {
        "model_id",
        "reasoning_effort",
        "required_cli_version",
        "required_cli_capability_policy",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "task_id",
        "difficulty",
        "commit_oid",
        "prompt_digest",
        "validator_digest",
        "assertion_digest",
        "allowed_write_paths",
        "provenance_id",
        "inclusion_rule_ids",
        "exclusion_rule_ids",
        "offline_executable",
        "reference_result",
        "negative_controls",
        "behavior_mutants",
        "difficulty_rubric_digest",
        "qualification_evidence_classification",
        "source_provisioning_class",
        "operator_attested",
        "local_clone_policy",
        "absolute_safety_assertion_ids",
    }
)
_NEGATIVE_CONTROL_KEYS = frozenset({"control_id", "result"})
_BEHAVIOR_MUTANT_KEYS = frozenset({"category", "mutant_digest", "result"})
_RETENTION_KEYS = frozenset({"durable_summary", "raw_jsonl"})
_BUDGET_KEYS = frozenset(
    {
        "total_calls",
        "canary_calls",
        "pilot_calls",
        "retry_calls",
        "concurrency",
        "max_elapsed_seconds",
        "max_retained_bytes",
        "max_reported_tokens",
        "max_output_tokens_per_call",
        "max_estimated_cost_microunits",
        "currency",
    }
)
_PRICE_KEYS = frozenset(
    {
        "model_id",
        "currency",
        "input_microunits_per_million",
        "cached_input_microunits_per_million",
        "output_microunits_per_million",
        "effective_at",
        "source_label",
    }
)
_INVOCATION_POLICY_KEYS = frozenset(
    {
        "canary_sandbox",
        "pilot_sandbox",
        "approval_policy",
        "ignore_user_config",
        "ignore_rules",
        "provider_transport_allowed",
        "tool_network_disabled",
        "web_search_disabled",
        "mcp_disabled",
        "plugins_disabled",
        "hooks_disabled",
        "skills_disabled",
        "child_process_policy",
        "validator_policy",
        "executable_identity_policy",
    }
)
_INTEGER_BUDGET_KEYS = (
    "total_calls",
    "canary_calls",
    "pilot_calls",
    "retry_calls",
    "concurrency",
    "max_elapsed_seconds",
    "max_retained_bytes",
    "max_reported_tokens",
    "max_output_tokens_per_call",
    "max_estimated_cost_microunits",
)
_TRUE_INVOCATION_KEYS = (
    "ignore_user_config",
    "ignore_rules",
    "provider_transport_allowed",
    "tool_network_disabled",
    "web_search_disabled",
    "mcp_disabled",
    "plugins_disabled",
    "hooks_disabled",
    "skills_disabled",
)
_CAPABILITY_SET_FIELDS = (
    "tool_read_root_identity_digests",
    "tool_write_root_identity_digests",
    "validator_read_root_identity_digests",
    "validator_write_root_identity_digests",
)


def freeze_json_value(value: object) -> object:
    """Return a detached, recursively immutable JSON-compatible value."""
    if isinstance(value, ABCMapping):
        return MappingProxyType(
            {key: freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item) for item in value)
    return value


def thaw_json_value(value: object) -> object:
    """Return mutable dict/list containers for a recursively frozen value."""
    if isinstance(value, ABCMapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def sha256_bytes(value: bytes) -> str:
    """Return a lowercase sha256 identifier for exact bytes."""
    if not isinstance(value, bytes):
        raise TypeError("value must be bytes")
    return "sha256:" + hashlib.sha256(value).hexdigest()


_CANARY_OVERLAY_RECIPE_DOCUMENT = {
    "assembly_order": [
        "base_bytes",
        "prefix_utf8",
        "runtime_marker",
        "suffix_utf8",
    ],
    "base_encoding": "utf-8",
    "base_file_kind": "regular",
    "document_type": "canary_overlay_recipe",
    "marker": {
        "encoded_length": 32,
        "encoding": "lowercase_hex",
        "entropy_bits": 128,
    },
    "operation": "append_exact_utf8",
    "prefix_utf8": (
        "\n\nReturn this exact opaque canary marker in the required response field: "
    ),
    "preserve_base_bytes": True,
    "require_marker_absent_before_append": True,
    "required_derived_marker_occurrences": 1,
    "schema_version": 1,
    "suffix_utf8": "\n",
    "target_relative_path": "AGENTS.md",
}
_CANARY_RESPONSE_SCHEMA_DOCUMENT = {
    "$id": "urn:codex-workflow-skills:harness-experiment:canary-response:v1",
    "additionalProperties": False,
    "properties": {
        "marker": {
            "maxLength": 32,
            "minLength": 32,
            "pattern": "^[0-9a-f]{32}$",
            "type": "string",
        }
    },
    "required": ["marker"],
    "type": "object",
}
_PILOT_RESPONSE_SCHEMA_DOCUMENT = {
    "$id": "urn:codex-workflow-skills:harness-experiment:pilot-response:v1",
    "additionalProperties": False,
    "properties": {
        "status": {"enum": ["completed", "blocked"], "type": "string"},
        "summary": {"maxLength": 2048, "minLength": 1, "type": "string"},
    },
    "required": ["status", "summary"],
    "type": "object",
}
_CANARY_ARGV_TEMPLATE_DOCUMENT = {
    "argv_tail": [
        {"literal": "-a"},
        {"literal": "never"},
        {"literal": "exec"},
        {"literal": "--json"},
        {"literal": "--strict-config"},
        {"literal": "--ephemeral"},
        {"literal": "--ignore-user-config"},
        {"literal": "--ignore-rules"},
        {"literal": "--sandbox"},
        {"literal": "read-only"},
        {"literal": "--model"},
        {"encoding": "one_argv_token", "slot": "model_id"},
        {"literal": "--config"},
        {
            "encoding": "canonical_json_string_as_toml_basic_string",
            "prefix": "model_reasoning_effort=",
            "slot": "reasoning_effort",
        },
        {"literal": "--output-schema"},
        {"encoding": "one_argv_token", "slot": "output_schema_path"},
        {"literal": "-"},
    ],
    "document_type": "canary_argv_template",
    "ignore_rules_semantics": "execpolicy_dot_rules_only",
    "runtime_executable_binding": "future_child_receipt",
    "schema_version": 1,
    "stdin_utf8": (
        "Return the exact opaque canary marker specified by the applicable global "
        "AGENTS.md instructions as JSON matching the output schema."
    ),
}
_PILOT_ARGV_TEMPLATE_DOCUMENT = {
    "argv_tail": [
        {"literal": "-a"},
        {"literal": "never"},
        {"literal": "exec"},
        {"literal": "--json"},
        {"literal": "--strict-config"},
        {"literal": "--ephemeral"},
        {"literal": "--ignore-user-config"},
        {"literal": "--ignore-rules"},
        {"literal": "--sandbox"},
        {"literal": "workspace-write"},
        {"literal": "--model"},
        {"encoding": "one_argv_token", "slot": "model_id"},
        {"literal": "--config"},
        {
            "encoding": "canonical_json_string_as_toml_basic_string",
            "prefix": "model_reasoning_effort=",
            "slot": "reasoning_effort",
        },
        {"literal": "--output-schema"},
        {"encoding": "one_argv_token", "slot": "output_schema_path"},
        {"literal": "-"},
    ],
    "document_type": "pilot_argv_template",
    "ignore_rules_semantics": "execpolicy_dot_rules_only",
    "runtime_executable_binding": "future_child_receipt",
    "schema_version": 1,
    "stdin_binding": "plan_candidate_prompt_digest",
}
_ENVIRONMENT_POLICY_DOCUMENT = {
    "document_type": "experiment_environment_policy",
    "hooks_disabled": True,
    "mcp_disabled": True,
    "parent_environment_inherited": False,
    "plugins_disabled": True,
    "provider_transport_allowed": True,
    "schema_version": 1,
    "skills_disabled": True,
    "tool_credentials_allowed": False,
    "tool_network_disabled": True,
    "transport_credential_binding": "future_runtime_only",
    "validator_network_disabled": True,
    "web_search_disabled": True,
}
_ROOT_CAPABILITY_POLICY_DOCUMENT = {
    "canary_runtime_identities": "future_child_receipt_only",
    "document_type": "root_capability_policy",
    "implicit_roots_allowed": False,
    "required_sets": [
        "tool_read_root_identity_digests",
        "tool_write_root_identity_digests",
        "validator_read_root_identity_digests",
        "validator_write_root_identity_digests",
    ],
    "root_identity_format": "sha256_prefixed_digest",
    "schema_version": 1,
    "sets_are_utf8_sorted_unique": True,
    "tool_and_validator_write_sets_disjoint": True,
    "tool_write_must_be_readable": True,
    "unlisted_host_roots_allowed": False,
    "validator_only_roots_visible_to_tool": False,
    "validator_write_must_be_readable": True,
}
_CALL_ALLOCATION_DOCUMENT = {
    "canary_calls": 2,
    "concurrency": 1,
    "document_type": "call_allocation",
    "pilot_calls": 8,
    "retry_calls": 0,
    "schema_version": 1,
    "total_calls": 10,
}

CANARY_OVERLAY_RECIPE = freeze_json_value(_CANARY_OVERLAY_RECIPE_DOCUMENT)
CANARY_RESPONSE_SCHEMA = freeze_json_value(_CANARY_RESPONSE_SCHEMA_DOCUMENT)
PILOT_RESPONSE_SCHEMA = freeze_json_value(_PILOT_RESPONSE_SCHEMA_DOCUMENT)
CANARY_ARGV_TEMPLATE = freeze_json_value(_CANARY_ARGV_TEMPLATE_DOCUMENT)
PILOT_ARGV_TEMPLATE = freeze_json_value(_PILOT_ARGV_TEMPLATE_DOCUMENT)
ENVIRONMENT_POLICY = freeze_json_value(_ENVIRONMENT_POLICY_DOCUMENT)
ROOT_CAPABILITY_POLICY = freeze_json_value(_ROOT_CAPABILITY_POLICY_DOCUMENT)
CALL_ALLOCATION = freeze_json_value(_CALL_ALLOCATION_DOCUMENT)

CANARY_OVERLAY_RECIPE_DIGEST = sha256_bytes(
    canonical_bytes(_CANARY_OVERLAY_RECIPE_DOCUMENT)
)
CANARY_RESPONSE_SCHEMA_DIGEST = sha256_bytes(
    canonical_bytes(_CANARY_RESPONSE_SCHEMA_DOCUMENT)
)
PILOT_RESPONSE_SCHEMA_DIGEST = sha256_bytes(
    canonical_bytes(_PILOT_RESPONSE_SCHEMA_DOCUMENT)
)
CANARY_ARGV_TEMPLATE_DIGEST = sha256_bytes(
    canonical_bytes(_CANARY_ARGV_TEMPLATE_DOCUMENT)
)
PILOT_ARGV_TEMPLATE_DIGEST = sha256_bytes(
    canonical_bytes(_PILOT_ARGV_TEMPLATE_DOCUMENT)
)
ENVIRONMENT_POLICY_DIGEST = sha256_bytes(
    canonical_bytes(_ENVIRONMENT_POLICY_DOCUMENT)
)
ROOT_CAPABILITY_POLICY_DIGEST = sha256_bytes(
    canonical_bytes(_ROOT_CAPABILITY_POLICY_DOCUMENT)
)
CALL_ALLOCATION_DIGEST = sha256_bytes(
    canonical_bytes(_CALL_ALLOCATION_DOCUMENT)
)


@dataclass(frozen=True)
class CanonicalExperimentInput:
    canonical_bytes: bytes = field(repr=False)
    value: Mapping[str, object]
    input_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))
        object.__setattr__(self, "value", freeze_json_value(self.value))


@dataclass(frozen=True)
class PlannedRun:
    ordinal: int
    task_id: str
    difficulty: str
    condition: str


@dataclass(frozen=True)
class CanaryInvocationTemplate:
    ordinal: int
    profile: str
    model_id: str
    reasoning_effort: str
    sandbox: str
    approval_policy: str
    provider_transport_allowed: bool
    tool_network_disabled: bool
    base_profile_digest: str
    overlay_recipe_policy_digest: str
    root_capability_policy_digest: str
    child_process_policy: str
    validator_policy: str
    output_schema_digest: str
    environment_policy_digest: str
    argv_template_digest: str
    containment_policy_version: str


@dataclass(frozen=True)
class PilotInvocationPlan:
    ordinal: int
    run: PlannedRun
    model_id: str
    reasoning_effort: str
    sandbox: str
    approval_policy: str
    provider_transport_allowed: bool
    tool_network_disabled: bool
    codex_home_identity_digest: str
    task_root_identity_digest: str
    temp_root_identity_digest: str
    tool_read_root_identity_digests: Tuple[str, ...]
    tool_write_root_identity_digests: Tuple[str, ...]
    validator_read_root_identity_digests: Tuple[str, ...]
    validator_write_root_identity_digests: Tuple[str, ...]
    child_process_policy: str
    validator_policy: str
    output_schema_digest: str
    environment_policy_digest: str
    argv_template_digest: str
    containment_policy_version: str

    def __post_init__(self) -> None:
        for name in _CAPABILITY_SET_FIELDS:
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class ExperimentPlan:
    input_digest: str
    plan_document: Mapping[str, object]
    canonical_bytes: bytes = field(repr=False)
    plan_digest: str
    pilot_schedule: Tuple[PlannedRun, ...]
    canary_templates: Tuple[CanaryInvocationTemplate, ...]
    pilot_invocation_plans: Tuple[PilotInvocationPlan, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "plan_document", freeze_json_value(self.plan_document)
        )
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))
        object.__setattr__(self, "pilot_schedule", tuple(self.pilot_schedule))
        object.__setattr__(
            self, "canary_templates", tuple(self.canary_templates)
        )
        object.__setattr__(
            self,
            "pilot_invocation_plans",
            tuple(self.pilot_invocation_plans),
        )


def _raise_input_error() -> None:
    raise ExperimentPlanError(_INPUT_ERROR)


def _raise_plan_error() -> None:
    raise ExperimentPlanError(_PLAN_ERROR)


def _require_mapping(
    value: object, exact_keys: frozenset, label: str
) -> ABCMapping:
    del label
    if not isinstance(value, ABCMapping):
        _raise_input_error()
    if set(value.keys()) != set(exact_keys):
        _raise_input_error()
    return value


def _require_sequence(value: object, label: str) -> Tuple[object, ...]:
    del label
    if (
        not isinstance(value, ABCSequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        _raise_input_error()
    return tuple(value)


def _require_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    del label
    if not isinstance(value, str) or (not allow_empty and not value):
        _raise_input_error()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _raise_input_error()
    if not unicodedata.is_normalized("NFC", value):
        _raise_input_error()
    return value


def _require_identifier(value: object, label: str) -> str:
    text = _require_text(value, label)
    if _IDENTIFIER_PATTERN.fullmatch(text) is None:
        _raise_input_error()
    return text


def _require_opaque_identifier(value: object, label: str) -> str:
    return _require_text(value, label)


def _require_digest(value: object, label: str) -> str:
    del label
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        _raise_input_error()
    return value


def _require_full_oid(value: object, label: str) -> str:
    del label
    if not isinstance(value, str) or _FULL_OID_PATTERN.fullmatch(value) is None:
        _raise_input_error()
    return value


def _require_relative_path(value: object, label: str) -> str:
    text = _require_text(value, label)
    if (
        text.startswith("/")
        or text.endswith("/")
        or "\\" in text
        or "\x00" in text
        or ":" in text
    ):
        _raise_input_error()
    parts = text.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        _raise_input_error()
    return text


def _require_integer(
    value: object,
    label: str,
    *,
    minimum: int = None,
    exact: int = None
) -> int:
    del label
    if not isinstance(value, int) or isinstance(value, bool):
        _raise_input_error()
    if minimum is not None and value < minimum:
        _raise_input_error()
    if exact is not None and value != exact:
        _raise_input_error()
    return value


def _require_boolean(value: object, expected: bool, label: str) -> bool:
    del label
    if not isinstance(value, bool) or value is not expected:
        _raise_input_error()
    return value


def _require_exact(value: object, expected: object, label: str) -> None:
    del label
    if type(value) is not type(expected) or value != expected:
        _raise_input_error()


def _canonical_alias_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _require_sorted_unique(
    value: object,
    validator,
    label: str,
    *,
    nonempty: bool = False
) -> Tuple[str, ...]:
    items = _require_sequence(value, label)
    if nonempty and not items:
        _raise_input_error()
    checked = tuple(validator(item, label) for item in items)
    if checked != tuple(sorted(checked, key=lambda item: item.encode("utf-8"))):
        _raise_input_error()
    aliases = tuple(_canonical_alias_key(item) for item in checked)
    if len(set(aliases)) != len(aliases):
        _raise_input_error()
    return checked


def _validate_model(value: object) -> ABCMapping:
    model = _require_mapping(value, _MODEL_KEYS, "model")
    _require_text(model["model_id"], "model_id")
    _require_text(model["reasoning_effort"], "reasoning_effort")
    _require_text(model["required_cli_version"], "required_cli_version")
    _require_exact(
        model["required_cli_capability_policy"],
        "codex-experiment-cli-v1",
        "required_cli_capability_policy",
    )
    return model


def _validate_candidate(value: object) -> ABCMapping:
    candidate = _require_mapping(value, _CANDIDATE_KEYS, "candidate")
    _require_identifier(candidate["task_id"], "task_id")
    if candidate["difficulty"] not in ("low", "medium"):
        _raise_input_error()
    _require_full_oid(candidate["commit_oid"], "commit_oid")
    for name in (
        "prompt_digest",
        "validator_digest",
        "assertion_digest",
        "difficulty_rubric_digest",
    ):
        _require_digest(candidate[name], name)
    _require_sorted_unique(
        candidate["allowed_write_paths"],
        _require_relative_path,
        "allowed_write_paths",
    )
    _require_opaque_identifier(candidate["provenance_id"], "provenance_id")
    _require_sorted_unique(
        candidate["inclusion_rule_ids"],
        _require_opaque_identifier,
        "inclusion_rule_ids",
        nonempty=True,
    )
    _require_sorted_unique(
        candidate["exclusion_rule_ids"],
        _require_opaque_identifier,
        "exclusion_rule_ids",
    )
    _require_boolean(candidate["offline_executable"], True, "offline_executable")
    _require_exact(candidate["reference_result"], "pass", "reference_result")

    controls = _require_sequence(candidate["negative_controls"], "negative_controls")
    if not controls:
        _raise_input_error()
    for value in controls:
        control = _require_mapping(
            value, _NEGATIVE_CONTROL_KEYS, "negative_control"
        )
        _require_opaque_identifier(control["control_id"], "control_id")
        _require_exact(control["result"], "fail", "negative_control_result")

    mutants = _require_sequence(candidate["behavior_mutants"], "behavior_mutants")
    if not mutants:
        _raise_input_error()
    categories = []
    for value in mutants:
        mutant = _require_mapping(
            value, _BEHAVIOR_MUTANT_KEYS, "behavior_mutant"
        )
        categories.append(
            _require_opaque_identifier(mutant["category"], "category")
        )
        _require_digest(mutant["mutant_digest"], "mutant_digest")
        _require_exact(mutant["result"], "fail", "behavior_mutant_result")
    if len(set(categories)) != len(categories):
        _raise_input_error()
    if not any(
        category not in _FORMATTING_ONLY_CATEGORIES for category in categories
    ):
        _raise_input_error()

    _require_exact(
        candidate["qualification_evidence_classification"],
        "operator_attested_static",
        "qualification_evidence_classification",
    )
    _require_exact(
        candidate["source_provisioning_class"],
        "operator_owned_trusted_git_local_clone",
        "source_provisioning_class",
    )
    _require_boolean(candidate["operator_attested"], True, "operator_attested")
    _require_exact(
        candidate["local_clone_policy"],
        "remote_or_no_local_or_no_hardlinks",
        "local_clone_policy",
    )
    _require_sorted_unique(
        candidate["absolute_safety_assertion_ids"],
        _require_opaque_identifier,
        "absolute_safety_assertion_ids",
    )
    return candidate


def _validate_candidates(value: object) -> Tuple[ABCMapping, ...]:
    candidates = _require_sequence(value, "candidates")
    if len(candidates) != 4:
        _raise_input_error()
    checked = tuple(_validate_candidate(item) for item in candidates)
    _require_sorted_unique(
        tuple(candidate["task_id"] for candidate in checked),
        _require_identifier,
        "task_ids",
        nonempty=True,
    )
    difficulties = tuple(candidate["difficulty"] for candidate in checked)
    if difficulties.count("low") != 2 or difficulties.count("medium") != 2:
        _raise_input_error()
    return checked


def _validate_retention(value: object) -> None:
    retention = _require_mapping(value, _RETENTION_KEYS, "retention")
    _require_exact(
        retention["durable_summary"], "typed_allowlist", "durable_summary"
    )
    _require_exact(retention["raw_jsonl"], "discard", "raw_jsonl")


def _validate_budgets(value: object) -> ABCMapping:
    budgets = _require_mapping(value, _BUDGET_KEYS, "budgets")
    for name in _INTEGER_BUDGET_KEYS:
        _require_integer(budgets[name], name, minimum=0)
    for name, expected in (
        ("total_calls", 10),
        ("canary_calls", 2),
        ("pilot_calls", 8),
        ("retry_calls", 0),
        ("concurrency", 1),
    ):
        _require_integer(budgets[name], name, exact=expected)
    for name in (
        "max_elapsed_seconds",
        "max_reported_tokens",
        "max_output_tokens_per_call",
    ):
        _require_integer(budgets[name], name, minimum=1)
    currency = _require_text(budgets["currency"], "currency")
    if _CURRENCY_PATTERN.fullmatch(currency) is None:
        _raise_input_error()
    return budgets


def _validate_price(
    value: object, model_id: object, currency: object
) -> None:
    price = _require_mapping(value, _PRICE_KEYS, "price_snapshot")
    _require_exact(price["model_id"], model_id, "price_model_id")
    _require_exact(price["currency"], currency, "price_currency")
    for name in (
        "input_microunits_per_million",
        "cached_input_microunits_per_million",
        "output_microunits_per_million",
    ):
        _require_integer(price[name], name, minimum=0)
    _require_text(price["effective_at"], "effective_at")
    _require_text(price["source_label"], "source_label")


def _validate_invocation_policy(value: object) -> ABCMapping:
    policy = _require_mapping(
        value, _INVOCATION_POLICY_KEYS, "invocation_policy"
    )
    _require_exact(policy["canary_sandbox"], "read-only", "canary_sandbox")
    _require_exact(policy["pilot_sandbox"], "workspace-write", "pilot_sandbox")
    _require_exact(policy["approval_policy"], "never", "approval_policy")
    for name in _TRUE_INVOCATION_KEYS:
        _require_boolean(policy[name], True, name)
    for name in (
        "child_process_policy",
        "validator_policy",
        "executable_identity_policy",
    ):
        _require_opaque_identifier(policy[name], name)
    return policy


def _validate_experiment_document(value: object) -> ABCMapping:
    document = _require_mapping(value, _EXPERIMENT_KEYS, "experiment")
    _require_integer(document["schema_version"], "schema_version", exact=1)
    _require_identifier(document["experiment_id"], "experiment_id")

    seed = _require_text(document["selection_seed"], "selection_seed")
    try:
        encoded_seed = seed.encode("ascii")
    except UnicodeEncodeError:
        _raise_input_error()
    if not 16 <= len(encoded_seed) <= 128:
        _raise_input_error()

    _require_exact(
        document["selection_rule"],
        "sha256-rank-paired-v1",
        "selection_rule",
    )
    model = _validate_model(document["model"])
    _validate_candidates(document["candidates"])
    _require_exact(
        document["analysis_contract_version"],
        "four-pair-screening-v1",
        "analysis_contract_version",
    )
    _require_exact(
        document["masking_contract_version"],
        "masked-review-chain-v1",
        "masking_contract_version",
    )
    _require_opaque_identifier(
        document["containment_policy_version"],
        "containment_policy_version",
    )
    _validate_retention(document["retention"])
    budgets = _validate_budgets(document["budgets"])
    _validate_price(
        document["price_snapshot"],
        model["model_id"],
        budgets["currency"],
    )
    if document["provider_cap_evidence"] not in (
        "not_supplied",
        "operator_attested_only",
        "independently_verified",
    ):
        _raise_input_error()
    _require_sorted_unique(
        document["external_prerequisite_receipt_digests"],
        _require_digest,
        "external_prerequisite_receipt_digests",
    )
    _validate_invocation_policy(document["invocation_policy"])
    return document


def load_experiment_input(data: bytes) -> CanonicalExperimentInput:
    """Accept only exact canonical UTF-8 JSON bytes for the experiment."""
    if not isinstance(data, bytes):
        raise ExperimentPlanError("experiment_input_not_bytes")
    try:
        value = load_canonical_input(data)
    except (CanonicalJSONError, UnicodeError, ValueError, TypeError):
        raise ExperimentPlanError(_INPUT_ERROR) from None
    try:
        encoded = canonical_bytes(value)
    except (CanonicalJSONError, UnicodeError, ValueError, TypeError):
        raise ExperimentPlanError(_INPUT_ERROR) from None
    if encoded != data:
        raise ExperimentPlanError("experiment_input_not_canonical")
    _validate_experiment_document(value)
    return CanonicalExperimentInput(
        canonical_bytes=data,
        value=value,
        input_digest=sha256_bytes(data),
    )


def experiment_input_bytes(value: CanonicalExperimentInput) -> bytes:
    """Return the authoritative bytes after verifying the frozen projection."""
    if not isinstance(value, CanonicalExperimentInput):
        raise TypeError("value must be CanonicalExperimentInput")
    try:
        encoded = canonical_bytes(thaw_json_value(value.value))
    except (CanonicalJSONError, UnicodeError, ValueError, TypeError):
        raise ExperimentPlanError("experiment_input_changed") from None
    if encoded != value.canonical_bytes:
        raise ExperimentPlanError("experiment_input_changed")
    return encoded


def _rank(seed: str, domain: str, task_id: str) -> bytes:
    payload = {
        "domain": domain,
        "seed": seed,
        "task_id": task_id,
    }
    return hashlib.sha256(canonical_bytes(payload)).digest()


def build_pilot_schedule(
    seed: str, candidates: Sequence[Mapping[str, object]]
) -> Tuple[PlannedRun, ...]:
    """Build the stable, paired eight-run pilot schedule."""
    try:
        seed_text = _require_text(seed, "selection_seed")
        try:
            encoded_seed = seed_text.encode("ascii")
        except UnicodeEncodeError:
            _raise_input_error()
        if not 16 <= len(encoded_seed) <= 128:
            _raise_input_error()
        checked = _validate_candidates(candidates)
        by_difficulty = {"low": [], "medium": []}
        for candidate in checked:
            by_difficulty[candidate["difficulty"]].append(candidate)
        orientations = {}
        for difficulty, values in by_difficulty.items():
            ordered = sorted(
                values,
                key=lambda item: _rank(
                    seed_text,
                    "condition-order:" + difficulty,
                    item["task_id"],
                ),
            )
            orientations[ordered[0]["task_id"]] = ("current", "lean")
            orientations[ordered[1]["task_id"]] = ("lean", "current")
        pair_order = sorted(
            checked,
            key=lambda item: _rank(
                seed_text, "pair-order", item["task_id"]
            ),
        )
        runs = []
        for candidate in pair_order:
            for condition in orientations[candidate["task_id"]]:
                runs.append(
                    PlannedRun(
                        ordinal=len(runs) + 1,
                        task_id=candidate["task_id"],
                        difficulty=candidate["difficulty"],
                        condition=condition,
                    )
                )
        return tuple(runs)
    except ExperimentPlanError:
        raise
    except (KeyError, TypeError, ValueError, CanonicalJSONError):
        raise ExperimentPlanError(_INPUT_ERROR) from None


def _dataclass_has_exact_fields(value: object, expected_type: type) -> bool:
    if type(value) is not expected_type:
        return False
    return set(vars(value)) == {item.name for item in fields(expected_type)}


def _require_plan_digest(value: object) -> str:
    try:
        return _require_digest(value, "digest")
    except ExperimentPlanError:
        _raise_plan_error()


def _require_plan_opaque_identifier(value: object) -> str:
    try:
        return _require_opaque_identifier(value, "identifier")
    except ExperimentPlanError:
        _raise_plan_error()


def _require_plan_sorted_digests(
    value: object, *, nonempty: bool
) -> Tuple[str, ...]:
    try:
        return _require_sorted_unique(
            value,
            _require_digest,
            "digests",
            nonempty=nonempty,
        )
    except ExperimentPlanError:
        _raise_plan_error()


def _planned_run_document(run: PlannedRun) -> Mapping[str, object]:
    return {
        "condition": run.condition,
        "difficulty": run.difficulty,
        "ordinal": run.ordinal,
        "task_id": run.task_id,
    }


def _canary_template_document(
    template: CanaryInvocationTemplate,
) -> Mapping[str, object]:
    return {
        "approval_policy": template.approval_policy,
        "argv_template_digest": template.argv_template_digest,
        "base_profile_digest": template.base_profile_digest,
        "child_process_policy": template.child_process_policy,
        "containment_policy_version": template.containment_policy_version,
        "document_type": "canary_invocation_template",
        "environment_policy_digest": template.environment_policy_digest,
        "model_id": template.model_id,
        "ordinal": template.ordinal,
        "output_schema_digest": template.output_schema_digest,
        "overlay_recipe_policy_digest": template.overlay_recipe_policy_digest,
        "profile": template.profile,
        "provider_transport_allowed": template.provider_transport_allowed,
        "reasoning_effort": template.reasoning_effort,
        "root_capability_policy_digest": (
            template.root_capability_policy_digest
        ),
        "sandbox": template.sandbox,
        "schema_version": 1,
        "tool_network_disabled": template.tool_network_disabled,
        "validator_policy": template.validator_policy,
    }


def _pilot_invocation_plan_document(
    plan: PilotInvocationPlan,
) -> Mapping[str, object]:
    return {
        "approval_policy": plan.approval_policy,
        "argv_template_digest": plan.argv_template_digest,
        "child_process_policy": plan.child_process_policy,
        "codex_home_identity_digest": plan.codex_home_identity_digest,
        "containment_policy_version": plan.containment_policy_version,
        "document_type": "pilot_invocation_plan",
        "environment_policy_digest": plan.environment_policy_digest,
        "model_id": plan.model_id,
        "ordinal": plan.ordinal,
        "output_schema_digest": plan.output_schema_digest,
        "provider_transport_allowed": plan.provider_transport_allowed,
        "reasoning_effort": plan.reasoning_effort,
        "run": _planned_run_document(plan.run),
        "sandbox": plan.sandbox,
        "schema_version": 1,
        "task_root_identity_digest": plan.task_root_identity_digest,
        "temp_root_identity_digest": plan.temp_root_identity_digest,
        "tool_network_disabled": plan.tool_network_disabled,
        "tool_read_root_identity_digests": list(
            plan.tool_read_root_identity_digests
        ),
        "tool_write_root_identity_digests": list(
            plan.tool_write_root_identity_digests
        ),
        "validator_policy": plan.validator_policy,
        "validator_read_root_identity_digests": list(
            plan.validator_read_root_identity_digests
        ),
        "validator_write_root_identity_digests": list(
            plan.validator_write_root_identity_digests
        ),
    }


def _validate_canary_templates(
    values: object,
    *,
    model: ABCMapping,
    invocation_policy: ABCMapping,
    containment_policy_version: object,
    current_profile_digest: str,
    lean_profile_digest: str
) -> Tuple[CanaryInvocationTemplate, ...]:
    if (
        not isinstance(values, ABCSequence)
        or isinstance(values, (str, bytes, bytearray))
    ):
        _raise_plan_error()
    templates = tuple(values)
    if len(templates) != 2:
        _raise_plan_error()
    expected = (
        (1, "current", current_profile_digest),
        (2, "lean", lean_profile_digest),
    )
    for template, (ordinal, profile, base_digest) in zip(templates, expected):
        if not _dataclass_has_exact_fields(template, CanaryInvocationTemplate):
            _raise_plan_error()
        if (
            not isinstance(template.ordinal, int)
            or isinstance(template.ordinal, bool)
            or template.ordinal != ordinal
            or template.profile != profile
            or template.model_id != model["model_id"]
            or template.reasoning_effort != model["reasoning_effort"]
            or template.sandbox != invocation_policy["canary_sandbox"]
            or template.approval_policy != invocation_policy["approval_policy"]
            or not isinstance(template.provider_transport_allowed, bool)
            or template.provider_transport_allowed
            is not invocation_policy["provider_transport_allowed"]
            or not isinstance(template.tool_network_disabled, bool)
            or template.tool_network_disabled
            is not invocation_policy["tool_network_disabled"]
            or template.base_profile_digest != base_digest
            or template.overlay_recipe_policy_digest
            != CANARY_OVERLAY_RECIPE_DIGEST
            or template.root_capability_policy_digest
            != ROOT_CAPABILITY_POLICY_DIGEST
            or template.child_process_policy
            != invocation_policy["child_process_policy"]
            or template.validator_policy != invocation_policy["validator_policy"]
            or template.output_schema_digest != CANARY_RESPONSE_SCHEMA_DIGEST
            or template.environment_policy_digest != ENVIRONMENT_POLICY_DIGEST
            or template.argv_template_digest != CANARY_ARGV_TEMPLATE_DIGEST
            or template.containment_policy_version
            != containment_policy_version
        ):
            _raise_plan_error()
        for name in (
            "base_profile_digest",
            "overlay_recipe_policy_digest",
            "root_capability_policy_digest",
            "output_schema_digest",
            "environment_policy_digest",
            "argv_template_digest",
        ):
            _require_plan_digest(getattr(template, name))
        _require_plan_opaque_identifier(template.child_process_policy)
        _require_plan_opaque_identifier(template.validator_policy)
        _require_plan_opaque_identifier(template.containment_policy_version)
    return templates


def _validate_pilot_plans(
    values: object,
    *,
    schedule: Tuple[PlannedRun, ...],
    model: ABCMapping,
    invocation_policy: ABCMapping,
    containment_policy_version: object
) -> Tuple[PilotInvocationPlan, ...]:
    if (
        not isinstance(values, ABCSequence)
        or isinstance(values, (str, bytes, bytearray))
    ):
        _raise_plan_error()
    plans = tuple(values)
    if len(plans) != 8:
        _raise_plan_error()
    for plan, run in zip(plans, schedule):
        if not _dataclass_has_exact_fields(plan, PilotInvocationPlan):
            _raise_plan_error()
        if not _dataclass_has_exact_fields(plan.run, PlannedRun):
            _raise_plan_error()
        if (
            not isinstance(plan.ordinal, int)
            or isinstance(plan.ordinal, bool)
            or plan.ordinal != run.ordinal + 2
            or plan.run != run
            or plan.model_id != model["model_id"]
            or plan.reasoning_effort != model["reasoning_effort"]
            or plan.sandbox != invocation_policy["pilot_sandbox"]
            or plan.approval_policy != invocation_policy["approval_policy"]
            or not isinstance(plan.provider_transport_allowed, bool)
            or plan.provider_transport_allowed
            is not invocation_policy["provider_transport_allowed"]
            or not isinstance(plan.tool_network_disabled, bool)
            or plan.tool_network_disabled
            is not invocation_policy["tool_network_disabled"]
            or plan.child_process_policy
            != invocation_policy["child_process_policy"]
            or plan.validator_policy != invocation_policy["validator_policy"]
            or plan.output_schema_digest != PILOT_RESPONSE_SCHEMA_DIGEST
            or plan.environment_policy_digest != ENVIRONMENT_POLICY_DIGEST
            or plan.argv_template_digest != PILOT_ARGV_TEMPLATE_DIGEST
            or plan.containment_policy_version != containment_policy_version
        ):
            _raise_plan_error()

        identities = {
            _require_plan_digest(plan.codex_home_identity_digest),
            _require_plan_digest(plan.task_root_identity_digest),
            _require_plan_digest(plan.temp_root_identity_digest),
        }
        if len(identities) != 3:
            _raise_plan_error()
        capability_sets = {
            name: _require_plan_sorted_digests(
                getattr(plan, name), nonempty=False
            )
            for name in _CAPABILITY_SET_FIELDS
        }
        if any(
            not set(values).issubset(identities)
            for values in capability_sets.values()
        ):
            _raise_plan_error()
        tool_read = set(
            capability_sets["tool_read_root_identity_digests"]
        )
        tool_write = set(
            capability_sets["tool_write_root_identity_digests"]
        )
        validator_read = set(
            capability_sets["validator_read_root_identity_digests"]
        )
        validator_write = set(
            capability_sets["validator_write_root_identity_digests"]
        )
        if not tool_write.issubset(tool_read):
            _raise_plan_error()
        if not validator_write.issubset(validator_read):
            _raise_plan_error()
        if tool_write.intersection(validator_write):
            _raise_plan_error()
        for name in (
            "output_schema_digest",
            "environment_policy_digest",
            "argv_template_digest",
        ):
            _require_plan_digest(getattr(plan, name))
        _require_plan_opaque_identifier(plan.child_process_policy)
        _require_plan_opaque_identifier(plan.validator_policy)
        _require_plan_opaque_identifier(plan.containment_policy_version)
    return plans


def _validate_call_allocation(budgets: ABCMapping) -> None:
    for name in (
        "total_calls",
        "canary_calls",
        "pilot_calls",
        "retry_calls",
        "concurrency",
    ):
        if budgets[name] != _CALL_ALLOCATION_DOCUMENT[name]:
            _raise_plan_error()


def _build_experiment_plan(
    experiment_input: CanonicalExperimentInput,
    *,
    bundle_digest: str,
    current_profile_digest: str,
    lean_profile_digest: str,
    task_source_trust_receipt_digests: Sequence[str],
    task_selection_receipt_digest: str,
    task_corpus_receipt_digest: str,
    canary_templates: Sequence[CanaryInvocationTemplate],
    pilot_invocation_plans: Sequence[PilotInvocationPlan],
) -> ExperimentPlan:
    if not isinstance(experiment_input, CanonicalExperimentInput):
        _raise_plan_error()
    authoritative = experiment_input_bytes(experiment_input)
    if experiment_input.input_digest != sha256_bytes(authoritative):
        _raise_plan_error()
    document = thaw_json_value(experiment_input.value)
    try:
        validated = _validate_experiment_document(document)
    except ExperimentPlanError:
        _raise_plan_error()

    bundle = _require_plan_digest(bundle_digest)
    current_profile = _require_plan_digest(current_profile_digest)
    lean_profile = _require_plan_digest(lean_profile_digest)
    if current_profile == lean_profile:
        _raise_plan_error()
    source_receipts = _require_plan_sorted_digests(
        task_source_trust_receipt_digests, nonempty=False
    )
    selection_receipt = _require_plan_digest(task_selection_receipt_digest)
    corpus_receipt = _require_plan_digest(task_corpus_receipt_digest)
    _validate_call_allocation(validated["budgets"])

    schedule = build_pilot_schedule(
        validated["selection_seed"], validated["candidates"]
    )
    templates = _validate_canary_templates(
        canary_templates,
        model=validated["model"],
        invocation_policy=validated["invocation_policy"],
        containment_policy_version=validated["containment_policy_version"],
        current_profile_digest=current_profile,
        lean_profile_digest=lean_profile,
    )
    pilots = _validate_pilot_plans(
        pilot_invocation_plans,
        schedule=schedule,
        model=validated["model"],
        invocation_policy=validated["invocation_policy"],
        containment_policy_version=validated["containment_policy_version"],
    )

    template_digests = tuple(
        sha256_bytes(canonical_bytes(_canary_template_document(template)))
        for template in templates
    )
    pilot_digests = tuple(
        sha256_bytes(canonical_bytes(_pilot_invocation_plan_document(plan)))
        for plan in pilots
    )
    document.update(
        {
            "bundle_digest": bundle,
            "call_allocation_digest": CALL_ALLOCATION_DIGEST,
            "canary_template_digests": list(template_digests),
            "current_profile_digest": current_profile,
            "input_digest": experiment_input.input_digest,
            "lean_profile_digest": lean_profile,
            "pilot_invocation_plan_digests": list(pilot_digests),
            "pilot_schedule": [
                _planned_run_document(run) for run in schedule
            ],
            "task_corpus_receipt_digest": corpus_receipt,
            "task_selection_receipt_digest": selection_receipt,
            "task_source_trust_receipt_digests": list(source_receipts),
        }
    )
    encoded = canonical_bytes(document)
    return ExperimentPlan(
        input_digest=experiment_input.input_digest,
        plan_document=document,
        canonical_bytes=encoded,
        plan_digest=sha256_bytes(encoded),
        pilot_schedule=schedule,
        canary_templates=templates,
        pilot_invocation_plans=pilots,
    )


def build_experiment_plan(
    experiment_input: CanonicalExperimentInput,
    *,
    bundle_digest: str,
    current_profile_digest: str,
    lean_profile_digest: str,
    task_source_trust_receipt_digests: Sequence[str],
    task_selection_receipt_digest: str,
    task_corpus_receipt_digest: str,
    canary_templates: Sequence[CanaryInvocationTemplate],
    pilot_invocation_plans: Sequence[PilotInvocationPlan],
) -> ExperimentPlan:
    """Build a deterministic path-free plan from validated child projections."""
    try:
        return _build_experiment_plan(
            experiment_input,
            bundle_digest=bundle_digest,
            current_profile_digest=current_profile_digest,
            lean_profile_digest=lean_profile_digest,
            task_source_trust_receipt_digests=(
                task_source_trust_receipt_digests
            ),
            task_selection_receipt_digest=task_selection_receipt_digest,
            task_corpus_receipt_digest=task_corpus_receipt_digest,
            canary_templates=canary_templates,
            pilot_invocation_plans=pilot_invocation_plans,
        )
    except ExperimentPlanError:
        raise ExperimentPlanError(_PLAN_ERROR) from None
    except (
        AttributeError,
        CanonicalJSONError,
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise ExperimentPlanError(_PLAN_ERROR) from None
