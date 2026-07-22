"""Prepare canonical coordination artifacts from one approved plan."""

import copy
from dataclasses import dataclass
from typing import Dict, List, Optional

from .canonical_json import sha256_id


class PreparationError(ValueError):
    """Raised when an approved plan cannot be projected safely."""


@dataclass(frozen=True)
class PreparedCoordination:
    manifest: dict
    inventory: dict
    manifest_hash: str
    inventory_hash: str


_WORKSTREAM_FIELDS = (
    "id",
    "owner",
    "scope",
    "exclusive_write_paths",
    "depends_on",
    "consumes",
    "produces",
)


def _project_workstreams(plan: dict) -> List[Dict[str, object]]:
    workstreams = plan.get("workstreams")
    if not isinstance(workstreams, list):
        raise PreparationError("plan workstreams must be a list")

    projected = []
    seen_ids = set()
    for workstream in workstreams:
        if not isinstance(workstream, dict):
            raise PreparationError("each workstream must be an object")
        workstream_id = workstream.get("id")
        if not isinstance(workstream_id, str) or not workstream_id:
            raise PreparationError("workstream id must be a non-empty string")
        if workstream_id in seen_ids:
            raise PreparationError("duplicate workstream id: {}".format(workstream_id))
        seen_ids.add(workstream_id)

        missing = [field for field in _WORKSTREAM_FIELDS if field not in workstream]
        if missing:
            raise PreparationError(
                "workstream {} missing field: {}".format(workstream_id, missing[0])
            )
        projected.append(
            {field: copy.deepcopy(workstream[field]) for field in _WORKSTREAM_FIELDS}
        )
    return projected


def _known_interface_ids(catalog: Optional[dict]) -> List[str]:
    if catalog is None:
        return []
    if not isinstance(catalog, dict):
        raise PreparationError("repository interface catalog must be an object")
    interface_ids = catalog.get("known_interface_ids", [])
    if not isinstance(interface_ids, list) or any(
        not isinstance(interface_id, str) or not interface_id
        for interface_id in interface_ids
    ):
        raise PreparationError("catalog known_interface_ids must be strings")
    return sorted(set(interface_ids))


def prepare_coordination(plan: dict, catalog: Optional[dict]) -> PreparedCoordination:
    """Project one approved plan into canonical manifest and independent inventory."""
    if not isinstance(plan, dict):
        raise PreparationError("approved plan must be an object")

    changed_surfaces = plan.get("changed_surfaces")
    if not isinstance(changed_surfaces, list) or any(
        not isinstance(surface, str) or not surface for surface in changed_surfaces
    ):
        raise PreparationError("plan changed_surfaces must be strings")

    workstreams = _project_workstreams(plan)
    plan_hash = sha256_id(plan)
    inventory = {
        "schema_version": 1,
        "plan_hash": plan_hash,
        "expected_workstreams": copy.deepcopy(workstreams),
        "changed_surfaces": copy.deepcopy(changed_surfaces),
        "known_interface_ids": _known_interface_ids(catalog),
    }
    inventory_hash = sha256_id(inventory)
    manifest = {
        "schema_version": 1,
        "plan_hash": plan_hash,
        "inventory_hash": inventory_hash,
        "changed_surfaces": copy.deepcopy(changed_surfaces),
        "workstreams": workstreams,
    }
    manifest_hash = sha256_id(manifest)
    return PreparedCoordination(
        manifest=manifest,
        inventory=inventory,
        manifest_hash=manifest_hash,
        inventory_hash=inventory_hash,
    )
