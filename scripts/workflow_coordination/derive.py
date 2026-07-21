"""Derive coordination requirements from prepared artifacts."""

from typing import Dict, Set, Tuple

from .canonical_json import sha256_id
from .models import DerivedCoordination, DerivedProfiles


_MANIFEST_KEYS = {
    "schema_version",
    "plan_hash",
    "inventory_hash",
    "changed_surfaces",
    "workstreams",
}
_INVENTORY_KEYS = {
    "schema_version",
    "plan_hash",
    "expected_workstreams",
    "changed_surfaces",
    "known_interface_ids",
}
_WORKSTREAM_KEYS = {
    "id",
    "owner",
    "scope",
    "exclusive_write_paths",
    "depends_on",
    "consumes",
    "produces",
}
_MATRIX_KEYS = {
    "schema_version",
    "profile_reviewers",
    "changed_surface_reviewers",
}
InterfaceIdentity = Tuple[str, str]


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def _interface_ref(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "id"}
        and isinstance(value.get("kind"), str)
        and bool(value["kind"])
        and isinstance(value.get("id"), str)
        and bool(value["id"])
    )


def _workstreams(value: object) -> bool:
    if not isinstance(value, list):
        return False
    ids = set()
    for workstream in value:
        if not isinstance(workstream, dict) or not _WORKSTREAM_KEYS.issubset(workstream):
            return False
        workstream_id = workstream.get("id")
        if (
            not isinstance(workstream_id, str)
            or not workstream_id
            or workstream_id in ids
            or not isinstance(workstream.get("owner"), str)
            or not workstream["owner"]
        ):
            return False
        ids.add(workstream_id)
        if not all(
            _string_list(workstream.get(field))
            for field in ("scope", "exclusive_write_paths", "depends_on")
        ):
            return False
        for field in ("consumes", "produces"):
            refs = workstream.get(field)
            if not isinstance(refs, list) or not all(_interface_ref(ref) for ref in refs):
                return False
    return True


def _artifact_status(manifest: object, inventory: object) -> str:
    if not isinstance(manifest, dict) or not isinstance(inventory, dict):
        return "missing"
    if not _MANIFEST_KEYS.issubset(manifest) or not _INVENTORY_KEYS.issubset(inventory):
        return "missing"
    if (
        manifest.get("schema_version") != 1
        or inventory.get("schema_version") != 1
        or not isinstance(manifest.get("plan_hash"), str)
        or not manifest["plan_hash"]
        or not isinstance(inventory.get("plan_hash"), str)
        or not inventory["plan_hash"]
        or not isinstance(manifest.get("inventory_hash"), str)
        or not manifest["inventory_hash"]
        or not _string_list(manifest.get("changed_surfaces"))
        or not _string_list(inventory.get("changed_surfaces"))
        or not _workstreams(manifest.get("workstreams"))
        or not _workstreams(inventory.get("expected_workstreams"))
        or not _string_list(inventory.get("known_interface_ids"))
    ):
        return "incompatible"
    if (
        manifest["inventory_hash"] != sha256_id(inventory)
        or manifest["plan_hash"] != inventory["plan_hash"]
        or manifest["changed_surfaces"] != inventory["changed_surfaces"]
        or manifest["workstreams"] != inventory["expected_workstreams"]
    ):
        return "mismatch"
    return "verified"


def _reviewer_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(trigger, str)
        and bool(trigger)
        and _string_list(reviewers)
        for trigger, reviewers in value.items()
    )


def _valid_trigger_matrix(trigger_matrix: object) -> bool:
    return (
        isinstance(trigger_matrix, dict)
        and set(trigger_matrix) == _MATRIX_KEYS
        and trigger_matrix.get("schema_version") == 1
        and _reviewer_mapping(trigger_matrix.get("profile_reviewers"))
        and _reviewer_mapping(trigger_matrix.get("changed_surface_reviewers"))
    )


def _identity(ref: dict) -> InterfaceIdentity:
    return ref["kind"], ref["id"]


def _blocked(completeness: str) -> DerivedCoordination:
    return DerivedCoordination(
        completeness=completeness,
        route="blocked",
        profiles=DerivedProfiles(shared_interface=False),
        affected_consumers=(),
        required_handoffs=(),
        required_acknowledgements=(),
        required_reviewers=(),
    )


def derive_coordination(
    manifest: dict, inventory: dict, trigger_matrix: dict
) -> DerivedCoordination:
    """Derive all gate inputs; never accept submitted route or required sets."""
    completeness = _artifact_status(manifest, inventory)
    if completeness != "verified":
        return _blocked(completeness)
    if not _valid_trigger_matrix(trigger_matrix):
        return _blocked("incompatible")

    producers: Dict[InterfaceIdentity, Set[str]] = {}
    for workstream in manifest["workstreams"]:
        for ref in workstream["produces"]:
            producers.setdefault(_identity(ref), set()).add(workstream["id"])

    consumed_refs = {
        _identity(ref)
        for workstream in manifest["workstreams"]
        for ref in workstream["consumes"]
    }
    external_refs = consumed_refs.difference(producers)
    known_interface_ids = set(inventory["known_interface_ids"])
    if any(ref_id not in known_interface_ids for _, ref_id in external_refs):
        return _blocked("mismatch" if known_interface_ids else "unverified")

    consumers: Set[str] = set()
    handoffs = set()
    for workstream in manifest["workstreams"]:
        consumer_id = workstream["id"]
        for ref in workstream["consumes"]:
            for producer_id in producers.get(_identity(ref), ()):
                if producer_id != consumer_id:
                    consumers.add(consumer_id)
                    handoffs.add((producer_id, consumer_id))

    shared_interface = bool(handoffs)
    reviewers = set()
    if shared_interface:
        reviewers.update(
            trigger_matrix["profile_reviewers"].get("shared_interface", [])
        )
    for surface in manifest["changed_surfaces"]:
        reviewers.update(
            trigger_matrix["changed_surface_reviewers"].get(surface, [])
        )

    sorted_consumers = tuple(sorted(consumers))
    return DerivedCoordination(
        completeness="verified",
        route="contracted" if shared_interface else "independent",
        profiles=DerivedProfiles(shared_interface=shared_interface),
        affected_consumers=sorted_consumers,
        required_handoffs=tuple(sorted(handoffs)),
        required_acknowledgements=sorted_consumers,
        required_reviewers=tuple(sorted(reviewers)),
    )
