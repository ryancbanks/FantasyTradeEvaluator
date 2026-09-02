"""Private matchup settlement and league tiebreak ranking policy."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
import hashlib
from itertools import combinations
from numbers import Real

from .league_state import (
    CompletedFantasyMatchup,
    HeadToHeadPolicy,
    LeagueState,
    TeamId,
    TeamStanding,
    Tiebreaker,
)


class UnsupportedTiebreakerError(ValueError):
    """Raised when projection lacks inputs required by a captured rule."""


class UnresolvedTieError(ValueError):
    """Raised when the captured rules cannot assign every team a unique rank."""


@dataclass
class _Wlt:
    wins: int
    losses: int
    ties: int


@dataclass
class SeasonRecord(_Wlt):
    points_for: Decimal
    points_against: Decimal


@dataclass(frozen=True, slots=True)
class _PlayedResult:
    team1_id: TeamId
    team2_id: TeamId
    team1_score: Decimal
    team2_score: Decimal


_GameResult = CompletedFantasyMatchup | _PlayedResult


@dataclass(frozen=True)
class _RankingContext:
    records: dict[TeamId, SeasonRecord]
    state: LeagueState
    games: tuple[_GameResult, ...]
    random_seed: int
    scenario_id: str
    indexes: dict[TeamId, int]


def validate_tiebreaker_inputs(state: LeagueState) -> bool:
    historical = {
        Tiebreaker.HEAD_TO_HEAD,
        Tiebreaker.DIVISION_RECORD,
    }.intersection(state.playoff_rules.tiebreaker_order)
    if not historical:
        return False
    if not state.completed_history_is_usable:
        names = ", ".join(sorted(rule.value for rule in historical))
        raise UnsupportedTiebreakerError(
            f"cannot project {names} without historical matchup results that are "
            "complete and standings-consistent"
        )
    if (
        Tiebreaker.HEAD_TO_HEAD in historical
        and state.playoff_rules.head_to_head_policy is None
    ):
        raise UnsupportedTiebreakerError(
            "head_to_head requires an explicitly captured head_to_head_policy"
        )
    if Tiebreaker.DIVISION_RECORD in historical and any(
        team.division_id is None for team in state.teams
    ):
        raise UnsupportedTiebreakerError(
            "division_record requires a division_id for every team"
        )
    return True


def new_records(standings: dict[TeamId, TeamStanding]) -> dict[TeamId, SeasonRecord]:
    return {
        team_id: SeasonRecord(
            row.wins,
            row.losses,
            row.ties,
            as_decimal(row.points_for),
            as_decimal(row.points_against),
        )
        for team_id, row in standings.items()
    }


def round_score(score: float, quantum: Decimal) -> Decimal:
    value = as_decimal(score)
    places = max(0, -quantum.as_tuple().exponent)
    with localcontext() as context:
        context.prec = max(28, value.adjusted() + places + 1)
        return value.quantize(quantum, rounding=ROUND_HALF_UP)


def settle_remaining_matchups(
    state: LeagueState,
    records: dict[TeamId, SeasonRecord],
    scores: dict[tuple[TeamId, int], Decimal],
    capture_results: bool,
) -> tuple[_PlayedResult, ...]:
    simulated = []
    for matchup in state.remaining_matchups:
        left_score = _add_score_adjustment(
            scores[(matchup.team1_id, matchup.week)],
            matchup.team1_score_adjustment,
        )
        right_score = scores[(matchup.team2_id, matchup.week)]
        _apply_result(records, matchup.team1_id, matchup.team2_id, left_score, right_score)
        if capture_results:
            simulated.append(
                _PlayedResult(
                    matchup.team1_id,
                    matchup.team2_id,
                    left_score,
                    right_score,
                )
            )
    return tuple(simulated)


def _add_score_adjustment(score: Decimal, adjustment: Real) -> Decimal:
    value = as_decimal(adjustment)
    if value.is_zero():
        return score
    minimum_exponent = min(score.as_tuple().exponent, value.as_tuple().exponent)
    fractional_places = max(0, -minimum_exponent)
    integer_digits = max(1, score.adjusted() + 1, value.adjusted() + 1)
    with localcontext() as context:
        context.prec = max(28, integer_digits + fractional_places + 1)
        return score + value


def rank_teams(
    records: dict[TeamId, SeasonRecord],
    state: LeagueState,
    random_seed: int,
    scenario_id: str,
    games: tuple[_GameResult, ...],
    *,
    team_ids: tuple[TeamId, ...] | None = None,
) -> tuple[TeamId, ...]:
    """Refine only still-tied groups at each captured tiebreak step."""

    league_ids = tuple(team.team_id for team in state.teams)
    selected_ids = league_ids if team_ids is None else tuple(team_ids)
    if (
        not selected_ids
        or len(set(selected_ids)) != len(selected_ids)
        or not set(selected_ids).issubset(league_ids)
    ):
        raise ValueError("ranking team_ids must be unique league teams")
    indexes = {team_id: index for index, team_id in enumerate(league_ids)}
    context = _RankingContext(
        records, state, games, random_seed, scenario_id, indexes
    )
    groups = [selected_ids]
    for rule in state.playoff_rules.tiebreaker_order:
        refined = []
        for group in groups:
            if len(group) == 1:
                refined.append(group)
                continue
            values = _criterion(rule, group, context)
            buckets: dict[object, list[TeamId]] = {}
            for team_id in group:
                buckets.setdefault(values[team_id], []).append(team_id)
            refined.extend(
                tuple(buckets[value])
                for value in sorted(buckets, reverse=True)
            )
        groups = refined
    unresolved = [group for group in groups if len(group) > 1]
    if unresolved:
        raise UnresolvedTieError(
            "captured tiebreakers leave teams tied; add RANDOM_DRAW or capture another rule"
        )
    return tuple(group[0] for group in groups)


def select_playoff_seeds(
    state: LeagueState,
    overall_order: tuple[TeamId, ...],
    records: dict[TeamId, SeasonRecord],
    random_seed: int,
    scenario_id: str,
    games: tuple[_GameResult, ...],
) -> tuple[TeamId, ...]:
    guaranteed_count = state.playoff_rules.division_winner_qualifier_count
    if guaranteed_count == 0:
        return overall_order[: state.playoff_rules.qualifier_count]
    rank = {team_id: index for index, team_id in enumerate(overall_order)}
    divisions: dict[str, list[TeamId]] = {}
    for team in state.teams:
        if team.division_id is not None:
            divisions.setdefault(team.division_id, []).append(team.team_id)
    winners = []
    for members in divisions.values():
        division_order = rank_teams(
            records,
            state,
            random_seed,
            scenario_id,
            games,
            team_ids=tuple(members),
        )
        winners.append(division_order[0])
    winners = sorted(winners, key=rank.__getitem__)[:guaranteed_count]
    wildcards = [team_id for team_id in overall_order if team_id not in winners]
    needed = state.playoff_rules.qualifier_count - len(winners)
    return tuple((*winners, *wildcards[:needed]))


def as_decimal(value: Real) -> Decimal:
    if isinstance(value, Fraction):
        return Decimal(value.numerator) / Decimal(value.denominator)
    return Decimal(str(value))


def _criterion(
    rule: Tiebreaker,
    group: tuple[TeamId, ...],
    context: _RankingContext,
) -> dict[TeamId, object]:
    if rule is Tiebreaker.WIN_PERCENTAGE:
        return {
            team_id: _record_percentage(context.records[team_id])
            for team_id in group
        }
    if rule is Tiebreaker.POINTS_FOR:
        return {team_id: context.records[team_id].points_for for team_id in group}
    if rule is Tiebreaker.POINTS_AGAINST:
        return {
            team_id: context.records[team_id].points_against
            for team_id in group
        }
    if rule is Tiebreaker.HEAD_TO_HEAD:
        return _head_to_head(
            group,
            context.games,
            context.state.playoff_rules.head_to_head_policy,
        )
    if rule is Tiebreaker.DIVISION_RECORD:
        return _division_records(group, context.games, context.state)
    if rule is Tiebreaker.RANDOM_DRAW:
        return {
            team_id: (
                -_random_draw(
                    context.random_seed,
                    context.scenario_id,
                    context.indexes[team_id],
                ),
                -context.indexes[team_id],
            )
            for team_id in group
        }
    raise AssertionError(f"unhandled tiebreaker: {rule}")


def _head_to_head(
    group: tuple[TeamId, ...],
    games: tuple[_GameResult, ...],
    policy: HeadToHeadPolicy | None,
) -> dict[TeamId, Fraction]:
    if policy is not HeadToHeadPolicy.BALANCED_GROUP_WIN_PERCENTAGE:
        raise UnsupportedTiebreakerError("unsupported or missing head_to_head_policy")
    pairs = {frozenset(pair): 0 for pair in combinations(group, 2)}
    records = {team_id: _Wlt(0, 0, 0) for team_id in group}
    for game in games:
        pair = frozenset((game.team1_id, game.team2_id))
        if pair in pairs:
            pairs[pair] += 1
            _apply_result(
                records, game.team1_id, game.team2_id,
                game.team1_score, game.team2_score,
            )
    counts = set(pairs.values())
    if len(counts) != 1 or not counts or next(iter(counts)) == 0:
        raise UnresolvedTieError(
            "balanced head-to-head policy requires every tied pair to have played "
            "the same positive number of games"
        )
    return {team_id: _record_percentage(records[team_id]) for team_id in group}


def _division_records(
    group: tuple[TeamId, ...],
    games: tuple[_GameResult, ...],
    state: LeagueState,
) -> dict[TeamId, Fraction]:
    divisions = {team.team_id: team.division_id for team in state.teams}
    records = {team_id: _Wlt(0, 0, 0) for team_id in group}
    for game in games:
        if divisions[game.team1_id] != divisions[game.team2_id]:
            continue
        if game.team1_id in records and game.team2_id in records:
            _apply_result(
                records, game.team1_id, game.team2_id,
                game.team1_score, game.team2_score,
            )
        elif game.team1_id in records:
            _apply_single(records[game.team1_id], game.team1_score, game.team2_score)
        elif game.team2_id in records:
            _apply_single(records[game.team2_id], game.team2_score, game.team1_score)
    if any(_games_played(records[team_id]) == 0 for team_id in group):
        raise UnresolvedTieError("division_record requires a division game for every tied team")
    return {team_id: _record_percentage(records[team_id]) for team_id in group}


def _apply_result(
    records: Mapping[TeamId, _Wlt],
    left_id: TeamId,
    right_id: TeamId,
    left_score: Real | Decimal,
    right_score: Real | Decimal,
) -> None:
    left = records[left_id]
    right = records[right_id]
    if isinstance(left, SeasonRecord):
        if not isinstance(right, SeasonRecord):
            raise AssertionError("mixed record types")
        left.points_for += left_score
        left.points_against += right_score
        right.points_for += right_score
        right.points_against += left_score
    if left_score > right_score:
        left.wins += 1
        right.losses += 1
    elif right_score > left_score:
        right.wins += 1
        left.losses += 1
    else:
        left.ties += 1
        right.ties += 1


def _apply_single(record: _Wlt, own_score: Real, opponent_score: Real) -> None:
    if own_score > opponent_score:
        record.wins += 1
    elif opponent_score > own_score:
        record.losses += 1
    else:
        record.ties += 1


def _record_percentage(record: _Wlt) -> Fraction:
    games = _games_played(record)
    return Fraction(2 * record.wins + record.ties, 2 * games) if games else Fraction(0)


def _games_played(record: _Wlt) -> int:
    return record.wins + record.losses + record.ties


def _random_draw(random_seed: int, scenario_id: str, team_index: int) -> int:
    payload = f"{random_seed}\0{scenario_id}\0{team_index}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")
