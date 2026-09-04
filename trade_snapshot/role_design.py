"""Generate a bounded starter/depth role architecture from one league snapshot."""

from collections import Counter
from collections.abc import Iterable, Mapping
import re

from .league_state import RosterRules
from .positions import normalize_player_position
from .scenario_config import PlayerEligibility
from .strength import RoleDefinition, RoleKind
from .trade_space import TeamRoster


__all__ = ("build_calibration_roles",)


def build_calibration_roles(
    roster_rules: RosterRules,
    rosters: Iterable[TeamRoster],
    player_positions: Mapping[str, str],
    eligibilities: Iterable[PlayerEligibility],
    *,
    maximum_depth_per_position: int = 3,
) -> tuple[RoleDefinition, ...]:
    """Represent every starter slot plus observed, bounded positional depth."""

    if not isinstance(roster_rules, RosterRules):
        raise ValueError("roster_rules must be RosterRules")
    if type(maximum_depth_per_position) is not int or not 0 <= maximum_depth_per_position <= 10:
        raise ValueError("maximum_depth_per_position must be an integer from 0 through 10")
    roster_rows = tuple(rosters)
    if not roster_rows or any(not isinstance(row, TeamRoster) for row in roster_rows):
        raise ValueError("rosters must contain TeamRoster values")
    if len({row.team_id for row in roster_rows}) != len(roster_rows):
        raise ValueError("rosters contain a duplicate team")
    positions = _positions(player_positions)
    eligibility = _eligibility(eligibilities)
    owned = set()
    for roster in roster_rows:
        if (
            roster.roster_cap != roster_rules.roster_cap
            or roster.reserve_slot_counts != roster_rules.reserve_slot_counts
        ):
            raise ValueError("roster capacities do not match league roster rules")
        if roster.current_size != len(roster.player_ids):
            raise ValueError("role design requires complete current rosters")
        overlap = owned.intersection(roster.player_ids)
        if overlap:
            raise ValueError(f"rosters share player {min(overlap)!r}")
        owned.update(roster.player_ids)
    missing = owned.difference(positions).union(owned.difference(eligibility))
    if missing:
        raise ValueError(f"role design is missing player metadata for {min(missing)!r}")
    inconsistent = tuple(
        player_id
        for player_id in sorted(owned)
        if positions[player_id] not in eligibility[player_id].eligible_slots
    )
    if inconsistent:
        raise ValueError(
            f"player {inconsistent[0]!r} is not eligible at its recorded position"
        )

    roles = []
    occurrences = Counter()
    for source_slot in roster_rules.starting_lineup_slots:
        slot = source_slot.strip()
        occurrences[slot] += 1
        roles.append(
            RoleDefinition(
                role_id=f"START__{_role_token(slot)}__{occurrences[slot]}",
                kind=RoleKind.STARTER,
                source_slot=slot,
                eligible_positions=frozenset({slot}),
            )
        )

    maximum_counts: Counter[str] = Counter()
    for roster in roster_rows:
        counts = Counter(positions[player_id] for player_id in roster.player_ids)
        for position, count in counts.items():
            maximum_counts[position] = max(maximum_counts[position], count)
    for position in sorted(maximum_counts):
        count = min(maximum_counts[position], maximum_depth_per_position)
        for index in range(1, count + 1):
            roles.append(
                RoleDefinition(
                    role_id=f"DEPTH__{_role_token(position)}__{index}",
                    kind=RoleKind.DEPTH,
                    source_slot=position,
                    eligible_positions=frozenset({position}),
                )
            )
    role_ids = tuple(role.role_id for role in roles)
    if len(set(role_ids)) != len(role_ids):
        raise ValueError("lineup slot names collapse to duplicate role identifiers")
    return tuple(roles)


def _positions(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("player_positions must be a non-empty mapping")
    result = {}
    for player_id, raw_position in value.items():
        if not isinstance(player_id, str) or not player_id.strip():
            raise ValueError("player_positions keys must be non-empty strings")
        if not isinstance(raw_position, str) or not raw_position.strip():
            raise ValueError("player_positions values must be non-empty strings")
        result[player_id.strip()] = normalize_player_position(raw_position)
    return result


def _eligibility(values: object) -> dict[str, PlayerEligibility]:
    if isinstance(values, (str, bytes)):
        raise ValueError("eligibilities must be an iterable")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("eligibilities must be an iterable") from None
    if not rows or any(not isinstance(row, PlayerEligibility) for row in rows):
        raise ValueError("eligibilities must contain PlayerEligibility values")
    result = {row.canonical_player_id: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("eligibilities contain a duplicate player")
    for player_id, row in result.items():
        if not row.eligible_slots:
            raise ValueError(f"player {player_id!r} has no eligible lineup slot")
    return result


def _role_token(value: str) -> str:
    token = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    if not token:
        raise ValueError("lineup slot cannot produce an empty role identifier")
    return token
