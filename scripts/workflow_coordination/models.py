"""Immutable derived coordination models."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DerivedProfiles:
    shared_interface: bool


@dataclass(frozen=True)
class DerivedCoordination:
    completeness: str
    route: str
    profiles: DerivedProfiles
    affected_consumers: tuple[str, ...]
    required_handoffs: tuple[tuple[str, str], ...]
    required_acknowledgements: tuple[str, ...]
    required_reviewers: tuple[str, ...]
