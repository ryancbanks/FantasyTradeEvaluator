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


__all__ = (
    "ProviderIdentityLink",
    "ProviderPlayerRecord",
    "reconcile_player_identities",
)


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


@dataclass(frozen=True, slots=True)
class ProviderIdentityLink:
    """A source-published exact crosswalk between stable provider IDs."""

    references: tuple[ProviderReference, ...]
    evidence: str

    def __post_init__(self) -> None:
        if isinstance(self.references, (str, bytes)):
            raise ValueError("references must contain ProviderReference values")
        try:
            references = tuple(self.references)
        except TypeError:
            raise ValueError(
                "references must contain ProviderReference values"
            ) from None
        if len(references) < 2 or any(
            not isinstance(reference, ProviderReference) for reference in references
        ):
            raise ValueError(
                "references must contain at least two ProviderReference values"
            )
        keys = tuple(reference.key for reference in references)
        providers = tuple(reference.provider for reference in references)
        if len(set(keys)) != len(keys) or len(set(providers)) != len(providers):
            raise ValueError("identity link must contain one unique ID per provider")
        if not isinstance(self.evidence, str) or not self.evidence.strip():
            raise ValueError("evidence must be a non-empty string")
        object.__setattr__(
            self,
            "references",
            tuple(sorted(references, key=lambda reference: reference.key)),
        )
        object.__setattr__(self, "evidence", self.evidence.strip())


def reconcile_player_identities(
    records: Iterable[ProviderPlayerRecord],
    previous: IdentityRegistry | None = None,
    *,
    anchor_provider: str = "fantasypros",
    verified_links: Iterable[ProviderIdentityLink] = (),
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
    if isinstance(verified_links, (str, bytes)):
        raise ValueError("verified_links must contain ProviderIdentityLink values")
    try:
        links = tuple(verified_links)
    except TypeError:
        raise ValueError(
            "verified_links must contain ProviderIdentityLink values"
        ) from None
    if any(not isinstance(link, ProviderIdentityLink) for link in links):
        raise ValueError("verified_links must contain ProviderIdentityLink values")
    records_by_key = {
        (row.provider, row.provider_player_id): row for row in rows
    }
    linked_keys: dict[tuple[str, str], ProviderIdentityLink] = {}
    for link in links:
        anchors = tuple(
            reference
            for reference in link.references
            if reference.provider == anchor_provider
        )
        if len(anchors) != 1:
            raise ValueError(
                "identity link must contain exactly one anchor-provider ID"
            )
        for reference in link.references:
            if reference.key not in records_by_key:
                raise ValueError(
                    f"identity link references missing player record {reference.key!r}"
                )
            previous_link = linked_keys.get(reference.key)
            if previous_link is not None and previous_link != link:
                raise ValueError(
                    f"provider player ID appears in conflicting identity links: "
                    f"{reference.key!r}"
                )
            linked_keys[reference.key] = link

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

    for link in links:
        anchor_reference = next(
            reference
            for reference in link.references
            if reference.provider == anchor_provider
        )
        canonical = assigned[anchor_reference.key]
        linked_rows = tuple(records_by_key[reference.key] for reference in link.references)
        anchor_row = records_by_key[anchor_reference.key]
        if any(
            row.position != anchor_row.position
            or row.nfl_team_id != anchor_row.nfl_team_id
            for row in linked_rows
        ):
            raise ValueError(
                f"identity link metadata conflicts with its anchor: {link.evidence}"
            )
        assigned_canonicals = {
            assigned[reference.key]
            for reference in link.references
            if reference.key in assigned
        }
        if assigned_canonicals - {canonical}:
            raise ValueError(
                f"identity link joins conflicting canonical players: {link.evidence}"
            )
        providers = {
            reference.provider: reference.provider_player_id
            for reference in references[canonical].values()
        }
        for reference in link.references:
            known_id = providers.get(reference.provider)
            if known_id is not None and known_id != reference.provider_player_id:
                raise ValueError(
                    f"identity link conflicts with an existing provider ID: "
                    f"{link.evidence}"
                )
            assigned[reference.key] = canonical
            references[canonical][reference.key] = reference
            providers[reference.provider] = reference.provider_player_id

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
