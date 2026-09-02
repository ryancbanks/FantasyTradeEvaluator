"""Deterministic, bounded candidate design for analyzer calibration experiments."""

from dataclasses import dataclass
from itertools import combinations, product
from math import comb, fsum

from ._calibration_inputs import PlayerFeatureVector
from .strength import RoleDefinition
from .trade_space import TeamRoster


_SAMPLED_PACKAGES_PER_ROSTER_SIZE = 12


@dataclass(frozen=True, slots=True)
class ExperimentCandidate:
    team2_id: str
    team1_gives: tuple[str, ...]
    team2_gives: tuple[str, ...]
    signature: tuple[float, ...]

    @property
    def package_size(self) -> int:
        return len(self.team1_gives)


def atomic_candidates(
    by_team: dict[str, TeamRoster],
    primary_team_id: str,
    features: dict[str, PlayerFeatureVector],
    roles: tuple[RoleDefinition, ...],
    residual_feature_names: tuple[str, ...],
    role_feature_names: tuple[str, ...],
) -> tuple[ExperimentCandidate, ...]:
    """Return every distinct one-player perturbation used for coefficient fitting."""

    mine = by_team[primary_team_id]
    candidates = (
        _candidate(
            other_id,
            (outgoing,),
            (incoming,),
            features,
            roles,
            residual_feature_names,
            role_feature_names,
        )
        for other_id in sorted(set(by_team).difference({primary_team_id}))
        for outgoing in sorted(mine.player_ids)
        for incoming in sorted(by_team[other_id].player_ids)
    )
    return _unique_nonzero_signatures(candidates)


def balanced_holdout_candidates(
    by_team: dict[str, TeamRoster],
    primary_team_id: str,
    features: dict[str, PlayerFeatureVector],
    roles: tuple[RoleDefinition, ...],
    residual_feature_names: tuple[str, ...],
    role_feature_names: tuple[str, ...],
) -> tuple[ExperimentCandidate, ...]:
    """Sample every feasible balanced size without enumerating package products.

    At most twelve evenly spaced packages per roster and size are crossed.  With
    ordinary 14-player rosters this bounds the pool below 35,000 candidates while
    still covering sizes one through fourteen and every counterparty.
    """

    mine = by_team[primary_team_id]
    package_cache: dict[tuple[tuple[str, ...], int], tuple[tuple[str, ...], ...]] = {}
    rows = []
    for other_id in sorted(set(by_team).difference({primary_team_id})):
        other = by_team[other_id]
        maximum = min(len(mine.player_ids), len(other.player_ids))
        for size in range(1, maximum + 1):
            outgoing = _sample_packages(mine.player_ids, size, package_cache)
            incoming = _sample_packages(other.player_ids, size, package_cache)
            for left, right in product(outgoing, incoming):
                candidate = _candidate(
                    other_id,
                    left,
                    right,
                    features,
                    roles,
                    residual_feature_names,
                    role_feature_names,
                )
                if any(candidate.signature):
                    rows.append(candidate)
    return tuple(rows)


def candidate_key(row: ExperimentCandidate) -> tuple[object, ...]:
    return row.team2_id, row.team1_gives, row.team2_gives


def _candidate(team2_id, outgoing, incoming, features, roles, residual, role_features):
    return ExperimentCandidate(
        team2_id,
        tuple(sorted(outgoing)),
        tuple(sorted(incoming)),
        _package_signature(
            outgoing, incoming, features, roles, residual, role_features
        ),
    )


def _package_signature(outgoing, incoming, features, roles, residual, role_features):
    result = [
        fsum(features[player].values[name] for player in incoming)
        - fsum(features[player].values[name] for player in outgoing)
        for name in residual
    ]
    for role in roles:
        for name in role_features:
            result.append(
                fsum(
                    features[player].values[name]
                    for player in incoming
                    if features[player].eligible_positions.intersection(
                        role.eligible_positions
                    )
                )
                - fsum(
                    features[player].values[name]
                    for player in outgoing
                    if features[player].eligible_positions.intersection(
                        role.eligible_positions
                    )
                )
            )
    return tuple(0.0 if value == 0 else float(value) for value in result)


def _sample_packages(player_ids, size, cache):
    players = tuple(sorted(player_ids))
    key = players, size
    if key in cache:
        return cache[key]
    count = comb(len(players), size)
    sample_count = min(count, _SAMPLED_PACKAGES_PER_ROSTER_SIZE)
    if sample_count == count:
        result = tuple(combinations(players, size))
    elif sample_count == 1:
        result = (next(combinations(players, size)),)
    else:
        selected_indexes = {
            index * (count - 1) // (sample_count - 1)
            for index in range(sample_count)
        }
        result = tuple(
            package
            for index, package in enumerate(combinations(players, size))
            if index in selected_indexes
        )
    cache[key] = result
    return result


def _unique_nonzero_signatures(candidates):
    by_signature = {}
    for candidate in candidates:
        if not any(candidate.signature):
            continue
        incumbent = by_signature.get(candidate.signature)
        if incumbent is None or candidate_key(candidate) < candidate_key(incumbent):
            by_signature[candidate.signature] = candidate
    return tuple(by_signature.values())
