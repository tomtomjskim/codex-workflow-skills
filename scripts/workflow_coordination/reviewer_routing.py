"""Load and derive canonical reviewer routing from one versioned artifact."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


DEFAULT_ROUTING_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "adversarial-review-loop"
    / "references"
    / "reviewer-routing.json"
)
_ROOT_KEYS = {
    "schema_version",
    "lens_agents",
    "profile_lenses",
    "changed_surface_lenses",
}
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_TRIGGER_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class ReviewerRoutingError(ValueError):
    """Raised when reviewer routing is missing, invalid, or incomplete."""


@dataclass(frozen=True)
class ReviewerRouting:
    schema_version: int
    lens_agents: Dict[str, str]
    profile_lenses: Dict[str, Tuple[str, ...]]
    changed_surface_lenses: Dict[str, Tuple[str, ...]]


def _reject_duplicate_keys(pairs: List[Tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewerRoutingError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def _name_mapping(name: str, value: object) -> Dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ReviewerRoutingError("{} must be a non-empty object".format(name))
    if tuple(value) != tuple(sorted(value)):
        raise ReviewerRoutingError("{} keys must be canonically sorted".format(name))
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _NAME.fullmatch(key):
            raise ReviewerRoutingError("{} contains an invalid key".format(name))
        if not isinstance(item, str) or not _NAME.fullmatch(item):
            raise ReviewerRoutingError("{} contains an invalid value".format(name))
        result[key] = item
    if len(set(result.values())) != len(result):
        raise ReviewerRoutingError("lens_agents values must be unique")
    return result


def _lens_mapping(
    name: str, value: object, known_lenses: Dict[str, str]
) -> Dict[str, Tuple[str, ...]]:
    if not isinstance(value, dict) or not value:
        raise ReviewerRoutingError("{} must be a non-empty object".format(name))
    if tuple(value) != tuple(sorted(value)):
        raise ReviewerRoutingError("{} keys must be canonically sorted".format(name))
    result = {}
    for key, lenses in value.items():
        if not isinstance(key, str) or not _TRIGGER_NAME.fullmatch(key):
            raise ReviewerRoutingError("{} contains an invalid key".format(name))
        if (
            not isinstance(lenses, list)
            or not lenses
            or any(not isinstance(lens, str) for lens in lenses)
            or lenses != sorted(set(lenses))
        ):
            raise ReviewerRoutingError(
                "{} entries must be non-empty, unique, sorted lens lists".format(name)
            )
        unknown = sorted(set(lenses).difference(known_lenses))
        if unknown:
            raise ReviewerRoutingError(
                "{} references unknown lenses: {}".format(name, ", ".join(unknown))
            )
        result[key] = tuple(lenses)
    return result


def load_reviewer_routing(path: Path = DEFAULT_ROUTING_PATH) -> ReviewerRouting:
    """Load and strictly validate the canonical reviewer routing artifact."""
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except ReviewerRoutingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReviewerRoutingError("cannot load reviewer routing: {}".format(error)) from error
    if not isinstance(payload, dict) or set(payload) != _ROOT_KEYS:
        raise ReviewerRoutingError("reviewer routing schema keys are invalid")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ReviewerRoutingError("reviewer routing schema_version must be 1")
    lens_agents = _name_mapping("lens_agents", payload.get("lens_agents"))
    return ReviewerRouting(
        schema_version=schema_version,
        lens_agents=lens_agents,
        profile_lenses=_lens_mapping(
            "profile_lenses", payload.get("profile_lenses"), lens_agents
        ),
        changed_surface_lenses=_lens_mapping(
            "changed_surface_lenses",
            payload.get("changed_surface_lenses"),
            lens_agents,
        ),
    )


def derive_lenses(
    routing: ReviewerRouting,
    changed_surfaces: Iterable[str],
    profiles: Iterable[str] = (),
) -> Tuple[str, ...]:
    """Derive a canonical lens union and reject unknown material inputs."""
    surfaces = tuple(changed_surfaces)
    selected_profiles = tuple(profiles)
    unknown_surfaces = sorted(
        {
            surface
            for surface in surfaces
            if surface not in routing.changed_surface_lenses
        }
    )
    if unknown_surfaces:
        raise ReviewerRoutingError(
            "unknown changed-surface tags: {}".format(", ".join(unknown_surfaces))
        )
    unknown_profiles = sorted(
        {profile for profile in selected_profiles if profile not in routing.profile_lenses}
    )
    if unknown_profiles:
        raise ReviewerRoutingError(
            "unknown reviewer profiles: {}".format(", ".join(unknown_profiles))
        )
    lenses = {
        lens
        for surface in surfaces
        for lens in routing.changed_surface_lenses[surface]
    }
    lenses.update(
        lens
        for profile in selected_profiles
        for lens in routing.profile_lenses[profile]
    )
    return tuple(sorted(lenses))


def derive_reviewers(
    routing: ReviewerRouting,
    changed_surfaces: Iterable[str],
    profiles: Iterable[str] = (),
) -> Tuple[str, ...]:
    """Resolve derived lenses to deduplicated, canonically sorted agent names."""
    lenses = derive_lenses(routing, changed_surfaces, profiles)
    return tuple(sorted({routing.lens_agents[lens] for lens in lenses}))


def build_trigger_matrix(routing: ReviewerRouting) -> dict:
    """Build the stable schema-version-1 matrix consumed by coordination APIs."""
    return {
        "schema_version": 1,
        "profile_reviewers": {
            profile: list(derive_reviewers(routing, (), profiles=(profile,)))
            for profile in routing.profile_lenses
        },
        "changed_surface_reviewers": {
            surface: list(derive_reviewers(routing, (surface,)))
            for surface in routing.changed_surface_lenses
        },
    }
