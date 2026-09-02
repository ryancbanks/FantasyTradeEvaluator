"""Conservative cross-provider identity reconciliation with explicit unresolved rows."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import re
import unicodedata

from .identity import (
    IdentityRegistry,
    PlayerIdentity,
    ProviderReference,
    UnresolvedProviderRecord,
)
from .positions import normalize_player_position


__all__ = ("ProviderPlayerRecord", "reconcile_player_identities")


_TEAM_ALIASES = {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"}


@dataclass(frozen=True, slots=True)
class ProviderPlayerRecord:
    provider: str
    provider_player_id: str
    display_name: str
    position: str
    nfl_team_id: str

    def __post_init__(self) -> None:
        for name in ("provider", "provider_player_id", "display_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "position", _position(self.position))
        object.__setattr__(self, "nfl_team_id", _team(self.nfl_team_id))

    @property
    def reference(self) -> ProviderReference:
        return ProviderReference(self.provider, self.provider_player_id)

    @property
    def match_key(self) -> tuple[str, str, str]:
        return _match_key(self.display_name, self.position, self.nfl_team_id)


def reconcile_player_identities(
    records: Iterable[ProviderPlayerRecord],
    previous: IdentityRegistry | None = None,
    *,
    anchor_provider: str = "fantasypros",
) -> IdentityRegistry:
    """Reuse stable IDs, then accept only unique exact metadata matches."""

    rows = tuple(records)
    if not rows or any(not isinstance(row, ProviderPlayerRecord) for row in rows):
        raise ValueError("records must contain ProviderPlayerRecord values")
    keys = tuple((row.provider, row.provider_player_id) for row in rows)
    if len(set(keys)) != len(keys):
        raise ValueError("records contain a duplicate provider player ID")
    if previous is not None and not isinstance(previous, IdentityRegistry):
        raise ValueError("previous must be an IdentityRegistry or None")
    if not isinstance(anchor_provider, str) or not anchor_provider.strip():
        raise ValueError("anchor_provider must be a non-empty string")

    previous = previous or IdentityRegistry(())
    players = {row.canonical_player_id: row for row in previous.players}
    assigned: dict[tuple[str, str], str] = {}
    references = {
        row.canonical_player_id: {ref.key: ref for ref in row.provider_references}
        for row in previous.players
    }
    metadata = {
        row.canonical_player_id: (row.display_name, row.position, row.nfl_team_id)
        for row in previous.players
    }
    for row in rows:
        known = previous.lookup(row.provider, row.provider_player_id)
        if known is not None:
            assigned[(row.provider, row.provider_player_id)] = known.canonical_player_id
            references[known.canonical_player_id][row.reference.key] = row.reference

    for row in rows:
        key = (row.provider, row.provider_player_id)
        if row.provider != anchor_provider or key in assigned:
            continue
        canonical = f"{anchor_provider}:{row.provider_player_id}"
        if canonical in players:
            raise ValueError("new anchor ID collides with an existing canonical player")
        assigned[key] = canonical
        references[canonical] = {row.reference.key: row.reference}
        metadata[canonical] = (row.display_name, row.position, row.nfl_team_id)

    current_by_canonical = defaultdict(list)
    for row in rows:
        canonical = assigned.get((row.provider, row.provider_player_id))
        if canonical is not None:
            current_by_canonical[canonical].append(row)
    for canonical, current in current_by_canonical.items():
        anchor = next((row for row in current if row.provider == anchor_provider), None)
        if anchor is not None:
            metadata[canonical] = (
                anchor.display_name,
                anchor.position,
                anchor.nfl_team_id,
            )

    candidates = defaultdict(list)
    for canonical, values in metadata.items():
        candidates[_match_key(*values)].append(canonical)

    pending_by_provider_key = defaultdict(list)
    for row in rows:
        key = (row.provider, row.provider_player_id)
        if key not in assigned:
            pending_by_provider_key[(row.provider, row.match_key)].append(row)

    assigned_providers = {
        canonical: {reference.provider for reference in provider_references.values()}
        for canonical, provider_references in references.items()
    }
    unresolved = []
    for row in rows:
        key = (row.provider, row.provider_player_id)
        if key in assigned:
            continue
        matches = candidates.get(row.match_key, ())
        provider_rows = pending_by_provider_key[(row.provider, row.match_key)]
        provider_is_unused = (
            len(matches) == 1
            and row.provider not in assigned_providers[matches[0]]
        )
        if len(matches) == 1 and len(provider_rows) == 1 and provider_is_unused:
            canonical = matches[0]
            assigned[key] = canonical
            references[canonical][row.reference.key] = row.reference
            assigned_providers[canonical].add(row.provider)
        else:
            if not matches:
                reason = "no exact name/position/team anchor match"
            elif len(matches) != 1 or len(provider_rows) != 1:
                reason = "exact name/position/team match is ambiguous"
            else:
                reason = "canonical player already has an ID for this provider"
            unresolved.append(
                UnresolvedProviderRecord(
                    row.reference,
                    row.display_name,
                    row.position,
                    row.nfl_team_id,
                    reason,
                )
            )

    resolved = tuple(
        PlayerIdentity(
            canonical,
            metadata[canonical][0],
            metadata[canonical][1],
            metadata[canonical][2],
            tuple(sorted(references[canonical].values(), key=lambda ref: ref.key)),
        )
        for canonical in sorted(metadata)
    )
    return IdentityRegistry(resolved, tuple(sorted(unresolved, key=lambda row: row.provider_reference.key)))


def _name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _match_key(
    display_name: str, position: object, team: object
) -> tuple[str, str, str]:
    normalized_position = _position(position)
    normalized_team = _team(team)
    name = "<team-defense>" if normalized_position == "DST" else _name(display_name)
    return name, normalized_position, normalized_team


def _position(value: object) -> str:
    return normalize_player_position(value)


def _team(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nfl_team_id must be a non-empty string")
    normalized = value.strip().upper()
    return _TEAM_ALIASES.get(normalized, normalized)
