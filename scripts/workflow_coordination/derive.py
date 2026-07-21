"""Derive coordination requirements from prepared artifacts."""

from typing import Iterable, Set

from .models import DerivedCoordination, DerivedProfiles


def _is_complete(manifest: dict, inventory: dict) -> bool:
    return (
        manifest.get("plan_hash") == inventory.get("plan_hash")
        and manifest.get("changed_surfaces") == inventory.get("changed_surfaces")
        and manifest.get("workstreams") == inventory.get("expected_workstreams")
    )


def _reviewers_for(trigger_matrix: dict, trigger: str) -> Iterable[str]:
    rule = trigger_matrix.get(trigger, ())
    if isinstance(rule, dict):
        rule = rule.get("reviewers", ())
    if isinstance(rule, list):
        return (reviewer for reviewer in rule if isinstance(reviewer, str))
    return ()


def derive_coordination(
    manifest: dict, inventory: dict, trigger_matrix: dict
) -> DerivedCoordination:
    """Derive all gate inputs; never accept submitted route or required sets."""
    complete = _is_complete(manifest, inventory)
    workstreams = manifest.get("workstreams", []) if complete else []

    producers = {}
    for workstream in workstreams:
        producer_id = workstream.get("id")
        for interface_id in workstream.get("produces", []):
            producers.setdefault(interface_id, set()).add(producer_id)

    consumers: Set[str] = set()
    handoffs = set()
    for workstream in workstreams:
        consumer_id = workstream.get("id")
        for interface_id in workstream.get("consumes", []):
            for producer_id in producers.get(interface_id, ()):
                if producer_id != consumer_id:
                    consumers.add(consumer_id)
                    handoffs.add((producer_id, consumer_id))

    shared_interface = bool(handoffs)
    reviewers = set()
    if shared_interface:
        reviewers.update(_reviewers_for(trigger_matrix, "shared_interface"))

    completeness = "verified" if complete else "mismatch"
    if not complete:
        route = "blocked"
    elif shared_interface:
        route = "contracted"
    else:
        route = "independent"

    sorted_consumers = tuple(sorted(consumers))
    return DerivedCoordination(
        completeness=completeness,
        route=route,
        profiles=DerivedProfiles(shared_interface=shared_interface),
        affected_consumers=sorted_consumers,
        required_handoffs=tuple(sorted(handoffs)),
        required_acknowledgements=sorted_consumers,
        required_reviewers=tuple(sorted(reviewers)),
    )
