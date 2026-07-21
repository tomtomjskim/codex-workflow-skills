"""Immutable validation receipts for coordination gates."""

from dataclasses import asdict, dataclass
from typing import Dict, Optional


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

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-compatible copy of the receipt."""
        return asdict(self)
