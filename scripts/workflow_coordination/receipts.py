"""Immutable validation receipts for coordination gates."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Optional


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ValidationReceipt:
    schema_version: int
    manifest_hash: str
    inventory_hash: str
    contract_core_hash: Optional[str]
    checkout_tree_hash: str
    derived_route: str
    required_sets: dict
    normalized_paths: dict
    run_id: str
    recorded_at: str
    derived_profiles: Optional[dict] = None

    def __post_init__(self) -> None:
        for field in ("required_sets", "normalized_paths", "derived_profiles"):
            object.__setattr__(self, field, _freeze(getattr(self, field)))

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-compatible copy of the receipt."""
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "inventory_hash": self.inventory_hash,
            "contract_core_hash": self.contract_core_hash,
            "checkout_tree_hash": self.checkout_tree_hash,
            "derived_route": self.derived_route,
            "required_sets": _thaw(self.required_sets),
            "normalized_paths": _thaw(self.normalized_paths),
            "run_id": self.run_id,
            "recorded_at": self.recorded_at,
            "derived_profiles": _thaw(self.derived_profiles),
        }
