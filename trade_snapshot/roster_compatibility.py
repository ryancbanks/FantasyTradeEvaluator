"""Present-day 1-for-1 roster compatibility without behavior inference."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
import json
from math import isfinite
from numbers import Real

from ._gm_statistics import percentile
from .engine_bundle import EngineBundle
from .search import PreparedTradePair
from .trade_space import TradeCandidate


_SCHEMA_VERSION = 1
_POWER_EPSILON = 1e-9
_NEED_PERCENTILE_MAX = 0.35
_SURPLUS_PERCENTILE_MIN = 0.65
_TIER_ORDER = {
    "verified_mutual_positive_fit": 0,
    "modeled_mutual_positive_fit": 1,
    "reciprocal_positional_fit": 2,
    "one_way_positional_fit": 3,
    "limited": 4,
}
_POSITION_ORDER = {
    position: index
    for index, position in enumerate(
        ("QB", "RB", "WR", "TE", "FLEX", "K", "DST", "DL", "LB", "DB", "IDP")
    )
}


@dataclass(frozen=True, slots=True)
class RosterSwap:
    """One oriented 1-for-1 swap with raw and displayed power changes."""

    primary_player_id: str
    counterparty_player_id: str
    primary_power_delta: float
    counterparty_power_delta: float
    primary_display_power_delta: float
    counterparty_display_power_delta: float


@dataclass(frozen=True, slots=True)
class _PairResult:
    left_team_id: str
    right_team_id: str
    evaluated_count: int
    mutually_positive_count: int
    mutually_nondecreasing_count: int
    best_mutually_positive: RosterSwap | None
    power_methodology_status: str


def build_roster_compatibility(
    bundle: EngineBundle,
    *,
    physically_injured_player_ids: Iterable[str] = (),
) -> dict[str, object]:
    """Evaluate every current, trade-eligible 1-for-1 team pairing."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    injured = _injured_ids(bundle, physically_injured_player_ids)
    rosters = {row.team_id: row for row in bundle.rosters}
    team_names = {row.team_id: row.name for row in bundle.state.teams}
    candidates, exclusions = _candidate_players(bundle, injured, team_names)
    position_profiles = _position_profiles(bundle, candidates)
    methodology_status = bundle.methodology_evidence.power_result_status(
        outgoing_count=1,
        incoming_count=1,
        has_roster_adjustment=False,
    )

    pairs = {}
    for left_id, right_id in combinations(sorted(rosters), 2):
        pairs[(left_id, right_id)] = _evaluate_pair(
            bundle,
            rosters[left_id],
            rosters[right_id],
            candidates[left_id],
            candidates[right_id],
            methodology_status,
        )

    teams = _team_records(
        pairs,
        rosters,
        candidates,
        position_profiles,
        team_names,
        bundle.player_names,
    )
    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "status": "ready",
        "scope": _scope_record(),
        "power_methodology_status": methodology_status,
        "unordered_team_pair_count": len(pairs),
        "directed_partner_record_count": sum(len(row["partners"]) for row in teams),
        "excluded_candidate_players": exclusions,
        "teams": teams,
    }
    json.dumps(result, allow_nan=False, sort_keys=True)
    return result


def screened_roster_swaps(
    bundle: EngineBundle,
    primary_team_id: str,
    counterparty_team_id: str,
    *,
    minimum_displayed_power_delta: float = -5.0,
    physically_injured_player_ids: Iterable[str] = (),
    limit: int | None = None,
) -> tuple[RosterSwap, ...]:
    """Return oriented 1-for-1 swaps that pass both teams' displayed-power floor."""

    threshold = _finite_threshold(minimum_displayed_power_delta)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer or null")
    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    rosters = {row.team_id: row for row in bundle.rosters}
    if primary_team_id == counterparty_team_id:
        raise ValueError("trade teams must be different")
    unknown = {primary_team_id, counterparty_team_id}.difference(rosters)
    if unknown:
        raise ValueError(f"team_id {min(unknown)!r} is not in the selected bundle")
    injured = _injured_ids(bundle, physically_injured_player_ids)
    team_names = {row.team_id: row.name for row in bundle.state.teams}
    candidates, _ = _candidate_players(bundle, injured, team_names)
    prepared = PreparedTradePair(
        bundle.strength_model,
        rosters[primary_team_id],
        rosters[counterparty_team_id],
    )
    swaps = []
    for left_index, left_player_id in enumerate(candidates[primary_team_id]):
        for right_index, right_player_id in enumerate(candidates[counterparty_team_id]):
            result = prepared.evaluate(
                TradeCandidate((left_player_id,), (right_player_id,)),
                candidate_index=(
                    left_index * len(candidates[counterparty_team_id]) + right_index
                ),
            )
            if (
                result.primary_display_delta >= threshold
                and result.counterparty_display_delta >= threshold
            ):
                swaps.append(_roster_swap(left_player_id, right_player_id, result))
    ordered = tuple(sorted(swaps, key=lambda row: _best_swap_key(row, bundle.player_names)))
    return ordered if limit is None else ordered[:limit]


def _team_records(pairs, rosters, candidates, profiles, team_names, player_names):
    teams = []
    for team_id in sorted(team_names, key=lambda value: _team_key(value, team_names)):
        partners = []
        for partner_id in sorted(
            (value for value in rosters if value != team_id),
            key=lambda value: _team_key(value, team_names),
        ):
            pair = pairs[tuple(sorted((team_id, partner_id)))]
            partners.append(
                _directed_partner_record(
                    pair,
                    team_id,
                    partner_id,
                    team_names,
                    player_names,
                    _positional_fit(
                        profiles[team_id],
                        profiles[partner_id],
                    ),
                )
            )
        partners.sort(key=lambda row: _partner_rank_key(row, team_names))
        for rank, row in enumerate(partners, start=1):
            row["partner_rank"] = rank
        teams.append(
            {
                "team_id": team_id,
                "team_name": team_names[team_id],
                "trade_candidate_count": len(candidates[team_id]),
                "current_position_profile": profiles[team_id],
                "partners": partners,
            }
        )
    return teams


def _scope_record():
    return {
        "current_bundle_only": True,
        "behavioral_history_used": False,
        "manager_acceptance_modeled": False,
        "trade_shape": "1_for_1",
        "positive_power_rule": f"both raw power deltas > {_POWER_EPSILON}",
        "nondecreasing_power_rule": (
            f"both raw power deltas >= -{_POWER_EPSILON} numerical tolerance"
        ),
        "candidate_exclusions": [
            "capacity-exempt roster players",
            "only physically injured player IDs explicitly supplied by the caller",
        ],
        "exclusion_handling": (
            "Excluded players cannot be sent or received and do not contribute to the "
            "positional profile; the immutable bundle's stored roster-power context is "
            "not otherwise rewritten."
        ),
        "position_fit_rule": {
            "basis": "remaining projected points among trade-eligible current players",
            "relative_need_at_or_below_league_percentile": _NEED_PERCENTILE_MAX,
            "relative_surplus_at_or_above_league_percentile": _SURPLUS_PERCENTILE_MIN,
        },
        "partner_ranking_order": [
            "mutual-positive 1-for-1 fit in a blind-holdout-validated package shape",
            "modeled mutual-positive 1-for-1 fit outside blind-holdout validation",
            "reciprocal relative positional fit",
            "one-way relative positional fit",
            "limited evidence",
            "then mutual-positive count, nondecreasing count, fit count, and team name",
        ],
        "best_example_rule": (
            "highest minimum team power delta, then highest combined delta, "
            "then player name and ID"
        ),
        "limitation": (
            "This is 1-for-1 discovery only; no result is evidence that a larger "
            "or differently shaped package cannot work."
        ),
    }


def _injured_ids(bundle, values):
    if isinstance(values, (str, bytes)):
        raise ValueError("physically_injured_player_ids must be an iterable of player IDs")
    try:
        rows = tuple(values)
        unique = set(rows)
    except TypeError:
        raise ValueError(
            "physically_injured_player_ids must be an iterable of player IDs"
        ) from None
    if any(not isinstance(value, str) or not value for value in rows):
        raise ValueError("physically_injured_player_ids must contain non-empty strings")
    if len(rows) != len(unique):
        raise ValueError("physically_injured_player_ids contains a duplicate")
    owned = {player_id for roster in bundle.rosters for player_id in roster.player_ids}
    unknown = unique.difference(owned)
    if unknown:
        raise ValueError(
            f"physically injured player {min(unknown)!r} is not on a current roster"
        )
    return frozenset(unique)


def _candidate_players(bundle, injured, team_names):
    candidates = {}
    exclusions = []
    for roster in bundle.rosters:
        excluded = roster.capacity_exempt_player_ids | injured.intersection(
            roster.player_ids
        )
        candidates[roster.team_id] = tuple(
            sorted(
                set(roster.player_ids).difference(excluded),
                key=lambda value: _player_key(value, bundle.player_names),
            )
        )
        for player_id in sorted(
            excluded,
            key=lambda value: _player_key(value, bundle.player_names),
        ):
            reasons = []
            if player_id in roster.capacity_exempt_player_ids:
                reasons.append("capacity_exempt")
            if player_id in injured:
                reasons.append("explicit_physical_injury")
            exclusions.append(
                {
                    "team_id": roster.team_id,
                    "player_id": player_id,
                    "player_name": bundle.player_names[player_id],
                    "reasons": reasons,
                }
            )
    exclusions.sort(
        key=lambda row: (
            _team_key(row["team_id"], team_names),
            row["player_name"].casefold(),
            row["player_id"],
        )
    )
    return candidates, exclusions


def _position_profiles(bundle, candidates):
    owned = {player_id for roster in bundle.rosters for player_id in roster.player_ids}
    position_by_player = {}
    points_by_player = defaultdict(float)
    for row in bundle.projections:
        if row.canonical_player_id not in owned:
            continue
        previous = position_by_player.setdefault(row.canonical_player_id, row.position)
        if previous != row.position:
            raise ValueError("one current player has conflicting projection positions")
        if row.projected_fantasy_points is not None:
            points_by_player[row.canonical_player_id] += row.projected_fantasy_points
    missing = owned.difference(position_by_player)
    if missing:
        raise ValueError(f"current player {min(missing)!r} has no projection position")
    positions = tuple(sorted(set(position_by_player.values()), key=_position_key))

    raw = {}
    for team_id, player_ids in candidates.items():
        totals = defaultdict(float)
        counts = defaultdict(int)
        for player_id in player_ids:
            position = position_by_player[player_id]
            totals[position] += points_by_player[player_id]
            counts[position] += 1
        raw[team_id] = totals, counts

    profiles = {}
    for team_id in candidates:
        records = []
        for position in positions:
            points = raw[team_id][0][position]
            league_percentile = percentile(
                points,
                (raw[other_id][0][position] for other_id in candidates),
            )
            classification = (
                "relative_need"
                if league_percentile <= _NEED_PERCENTILE_MAX
                else "relative_surplus"
                if league_percentile >= _SURPLUS_PERCENTILE_MIN
                else "middle"
            )
            records.append(
                {
                    "position": position,
                    "trade_eligible_player_count": raw[team_id][1][position],
                    "remaining_projected_points": points,
                    "league_percentile": league_percentile,
                    "classification": classification,
                }
            )
        profiles[team_id] = records
    return profiles


def _evaluate_pair(bundle, left, right, left_candidates, right_candidates, status):
    prepared = PreparedTradePair(bundle.strength_model, left, right)
    positive, nondecreasing_count = _evaluate_swaps(
        prepared, left_candidates, right_candidates
    )
    best = min(
        positive,
        key=lambda row: _best_swap_key(row, bundle.player_names),
        default=None,
    )
    return _PairResult(
        left.team_id,
        right.team_id,
        len(left_candidates) * len(right_candidates),
        len(positive),
        nondecreasing_count,
        best,
        status,
    )


def _evaluate_swaps(prepared, primary_candidates, counterparty_candidates):
    positive = []
    nondecreasing_count = 0
    for left_index, left_player_id in enumerate(primary_candidates):
        for right_index, right_player_id in enumerate(counterparty_candidates):
            result = prepared.evaluate(
                TradeCandidate((left_player_id,), (right_player_id,)),
                candidate_index=left_index * len(counterparty_candidates) + right_index,
            )
            left_delta = result.primary_raw_delta
            right_delta = result.counterparty_raw_delta
            if left_delta >= -_POWER_EPSILON and right_delta >= -_POWER_EPSILON:
                nondecreasing_count += 1
            if left_delta > _POWER_EPSILON and right_delta > _POWER_EPSILON:
                positive.append(
                    RosterSwap(
                        left_player_id,
                        right_player_id,
                        left_delta,
                        right_delta,
                        result.primary_display_delta,
                        result.counterparty_display_delta,
                    )
                )
    return tuple(positive), nondecreasing_count


def _positional_fit(team_profile, partner_profile):
    team_by_position = {row["position"]: row for row in team_profile}
    partner_by_position = {row["position"]: row for row in partner_profile}
    receiving, offering = [], []
    for position in sorted(team_by_position, key=_position_key):
        team = team_by_position[position]
        partner = partner_by_position[position]
        if (
            team["classification"] == "relative_need"
            and partner["classification"] == "relative_surplus"
        ):
            receiving.append(_position_match(position, team, partner))
        if (
            team["classification"] == "relative_surplus"
            and partner["classification"] == "relative_need"
        ):
            offering.append(_position_match(position, team, partner))
    return {
        "status": (
            "reciprocal"
            if receiving and offering
            else "one_way"
            if receiving or offering
            else "none"
        ),
        "team_needs_met_by_partner_surplus": receiving,
        "partner_needs_met_by_team_surplus": offering,
    }


def _position_match(position, need_or_surplus, counterpart):
    return {
        "position": position,
        "team_remaining_projected_points": need_or_surplus[
            "remaining_projected_points"
        ],
        "team_league_percentile": need_or_surplus["league_percentile"],
        "partner_remaining_projected_points": counterpart[
            "remaining_projected_points"
        ],
        "partner_league_percentile": counterpart["league_percentile"],
    }


def _directed_partner_record(pair, team_id, partner_id, team_names, player_names, fit):
    best = pair.best_mutually_positive
    if best is not None:
        if team_id == pair.left_team_id:
            sends_id, receives_id = best.primary_player_id, best.counterparty_player_id
            team_delta, partner_delta = best.primary_power_delta, best.counterparty_power_delta
        else:
            sends_id, receives_id = best.counterparty_player_id, best.primary_player_id
            team_delta, partner_delta = best.counterparty_power_delta, best.primary_power_delta
        example = {
            "team_sends": {"player_id": sends_id, "player_name": player_names[sends_id]},
            "team_receives": {
                "player_id": receives_id,
                "player_name": player_names[receives_id],
            },
            "team_power_delta": team_delta,
            "partner_power_delta": partner_delta,
            "minimum_team_power_delta": min(team_delta, partner_delta),
            "combined_power_delta": team_delta + partner_delta,
        }
    else:
        example = None
    tier = (
        "verified_mutual_positive_fit"
        if (
            pair.mutually_positive_count
            and pair.power_methodology_status == "holdout_validated"
        )
        else "modeled_mutual_positive_fit"
        if pair.mutually_positive_count
        else "reciprocal_positional_fit"
        if fit["status"] == "reciprocal"
        else "one_way_positional_fit"
        if fit["status"] == "one_way"
        else "limited"
    )
    return {
        "partner_team_id": partner_id,
        "partner_team_name": team_names[partner_id],
        "evidence_tier": tier,
        "evaluated_swap_count": pair.evaluated_count,
        "mutually_positive_swap_count": pair.mutually_positive_count,
        "mutually_nondecreasing_swap_count": pair.mutually_nondecreasing_count,
        "best_mutually_positive_example": example,
        "power_methodology_status": pair.power_methodology_status,
        "positional_fit": fit,
        "scope_limitation": (
            "No 1-for-1 result is proof that a larger or differently shaped package "
            "cannot work."
        ),
    }


def _partner_rank_key(row, team_names):
    fit = row["positional_fit"]
    fit_count = len(fit["team_needs_met_by_partner_surplus"]) + len(
        fit["partner_needs_met_by_team_surplus"]
    )
    return (
        _TIER_ORDER[row["evidence_tier"]],
        -row["mutually_positive_swap_count"],
        -row["mutually_nondecreasing_swap_count"],
        -fit_count,
        _team_key(row["partner_team_id"], team_names),
    )


def _best_swap_key(row, player_names):
    return (
        -min(row.primary_power_delta, row.counterparty_power_delta),
        -(row.primary_power_delta + row.counterparty_power_delta),
        *_player_key(row.primary_player_id, player_names),
        *_player_key(row.counterparty_player_id, player_names),
    )


def _team_key(team_id, names):
    return names[team_id].casefold(), team_id


def _player_key(player_id, names):
    return names[player_id].casefold(), player_id


def _position_key(position):
    return _POSITION_ORDER.get(position, len(_POSITION_ORDER)), position


def _finite_threshold(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("minimum_displayed_power_delta must be a finite number")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError("minimum_displayed_power_delta must be a finite number")
    return normalized


def _roster_swap(primary_player_id, counterparty_player_id, result):
    return RosterSwap(
        primary_player_id,
        counterparty_player_id,
        result.primary_raw_delta,
        result.counterparty_raw_delta,
        result.primary_display_delta,
        result.counterparty_display_delta,
    )


__all__ = (
    "RosterSwap",
    "build_roster_compatibility",
    "screened_roster_swaps",
)
