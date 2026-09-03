"""Leak-free historical fantasy seasons and real single-elimination playoffs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import math
from types import MappingProxyType

from .draft_config import DraftLeagueConfig, score_raw_stats
from .draft_features import resolve_preseason_projection
from .draft_history import ActualWeekStatus, HistoricalSeason, PreseasonPlayer
from .lineup import LineupPlayer, optimize_lineup

class SeasonStage(str, Enum):
    REGULAR = "regular"
    PLAYOFF = "playoff"


class GameOutcome(str, Enum):
    WIN = "win"
    LOSS = "loss"
    TIE = "tie"
    BYE = "bye"


@dataclass(frozen=True, slots=True)
class LineupSlotResult:
    slot_index: int
    slot: str
    player_id: str | None
    player_name: str | None
    selection_score: float
    actual_score: float


@dataclass(frozen=True, slots=True)
class TeamWeekResult:
    team_id: str
    team_name: str
    week: int
    stage: SeasonStage
    opponent_team_id: str | None
    opponent_team_name: str | None
    outcome: GameOutcome
    score: float
    opponent_score: float | None
    lineup: tuple[LineupSlotResult, ...]


@dataclass(frozen=True, slots=True)
class FinalStanding:
    team_id: str
    team_name: str
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float
    regular_season_rank: int
    made_playoffs: bool
    finish_rank: int


@dataclass(frozen=True, slots=True)
class BracketGame:
    round_number: int
    week: int
    game_number: int
    higher_seed: int
    higher_team_id: str
    higher_team_name: str
    lower_seed: int | None
    lower_team_id: str | None
    lower_team_name: str | None
    higher_score: float | None
    lower_score: float | None
    winner_team_id: str
    decided_by_seed: bool

    @property
    def is_bye(self) -> bool:
        return self.lower_team_id is None


@dataclass(frozen=True, slots=True)
class TeamSeasonTrace:
    team_id: str
    team_name: str
    roster_player_ids: tuple[str, ...]
    roster_player_names: tuple[str, ...]
    weekly_results: tuple[TeamWeekResult, ...]
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float
    regular_season_rank: int
    made_playoffs: bool
    finish_rank: int


@dataclass(frozen=True, slots=True)
class HistoricalSeasonTrace:
    season: int
    config_id: str
    teams: tuple[TeamSeasonTrace, ...]
    standings: tuple[FinalStanding, ...]
    bracket_games: tuple[BracketGame, ...]
    champion_team_id: str
    champion_team_name: str

    def to_record(self) -> dict[str, object]:
        """Return a detached JSON-safe review record."""
        return _json_value(self)


@dataclass(slots=True)
class _Record:
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0


@dataclass(frozen=True, slots=True)
class _PreparedScoringContext:
    """Immutable season/config scoring facts shared by repeated simulations."""

    season: HistoricalSeason
    config_id: str
    players: Mapping[str, PreseasonPlayer]
    actual_scores: Mapping[tuple[str, int], float]
    preseason_scores: Mapping[str, float]
    prior_played_averages: Mapping[tuple[str, int], float]


def simulate_historical_season(
    rosters: Sequence[Sequence[str]],
    season: HistoricalSeason,
    config: DraftLeagueConfig,
    *,
    _prepared: _PreparedScoringContext | None = None,
) -> HistoricalSeasonTrace:
    """Score one draft without using current or future outcomes to choose lineups."""

    context = (
        _prepare_scoring_context(season, config)
        if _prepared is None
        else _validated_scoring_context(_prepared, season, config)
    )
    players = context.players
    normalized = _validate_rosters(rosters, config, players)
    team_ids = tuple(f"drafter-{index}" for index in range(1, config.team_count + 1))
    names = {team_id: f"Drafter #{index}" for index, team_id in enumerate(team_ids, 1)}
    roster_by_team = dict(zip(team_ids, normalized))

    records = {team_id: _Record() for team_id in team_ids}
    week_rows: dict[str, list[TeamWeekResult]] = {team_id: [] for team_id in team_ids}
    lineup_cache: dict[tuple[str, int], tuple[tuple[LineupSlotResult, ...], float]] = {}

    def lineup(team_id: str, week: int):
        key = (team_id, week)
        if key not in lineup_cache:
            lineup_cache[key] = _select_lineup(
                roster_by_team[team_id], week, config, context,
            )
        return lineup_cache[key]

    schedule = _round_robin_schedule(team_ids, config.regular_season_weeks)
    for week, pairings in schedule:
        for left, right in pairings:
            if left is None or right is None:
                team_id = right if left is None else left
                assert team_id is not None
                selected, score = lineup(team_id, week)
                records[team_id].points_for += score
                week_rows[team_id].append(_week_result(
                    team_id, names, week, SeasonStage.REGULAR, None,
                    GameOutcome.BYE, score, None, selected,
                ))
                continue
            left_lineup, left_score = lineup(left, week)
            right_lineup, right_score = lineup(right, week)
            left_outcome, right_outcome = _outcomes(left_score, right_score)
            _apply_regular(records[left], records[right], left_score, right_score)
            week_rows[left].append(_week_result(
                left, names, week, SeasonStage.REGULAR, right,
                left_outcome, left_score, right_score, left_lineup,
            ))
            week_rows[right].append(_week_result(
                right, names, week, SeasonStage.REGULAR, left,
                right_outcome, right_score, left_score, right_lineup,
            ))

    regular_order = tuple(sorted(
        team_ids,
        key=lambda team_id: (
            -_winning_percentage(records[team_id]),
            -records[team_id].points_for,
            team_ids.index(team_id),
        ),
    ))
    regular_rank = {team_id: index for index, team_id in enumerate(regular_order, 1)}
    playoff_ids = regular_order[:config.playoff_team_count]
    bracket, champion, eliminated_round = _playoffs(
        playoff_ids, config.playoff_weeks, names, lineup, week_rows,
    )
    finish_order = [champion]
    finish_order.extend(sorted(
        (team_id for team_id in playoff_ids if team_id != champion),
        key=lambda team_id: (-eliminated_round[team_id], regular_rank[team_id]),
    ))
    finish_order.extend(team_id for team_id in regular_order if team_id not in playoff_ids)
    finish_rank = {team_id: index for index, team_id in enumerate(finish_order, 1)}

    standings = tuple(
        FinalStanding(
            team_id, names[team_id], records[team_id].wins, records[team_id].losses,
            records[team_id].ties, records[team_id].points_for,
            records[team_id].points_against, regular_rank[team_id],
            team_id in playoff_ids, finish_rank[team_id],
        )
        for team_id in regular_order
    )
    teams = tuple(
        TeamSeasonTrace(
            team_id, names[team_id], roster_by_team[team_id],
            tuple(players[player_id].display_name for player_id in roster_by_team[team_id]),
            tuple(sorted(week_rows[team_id], key=lambda row: (row.week, row.stage.value))),
            records[team_id].wins, records[team_id].losses, records[team_id].ties,
            records[team_id].points_for, records[team_id].points_against,
            regular_rank[team_id], team_id in playoff_ids, finish_rank[team_id],
        )
        for team_id in team_ids
    )
    return HistoricalSeasonTrace(
        season.season, config.config_id, teams, standings, tuple(bracket),
        champion, names[champion],
    )


def _prepare_scoring_context(
    season: HistoricalSeason,
    config: DraftLeagueConfig,
) -> _PreparedScoringContext:
    """Score immutable historical facts once for every repeated roster arena."""

    _validate_scoring_inputs(season, config)
    players = {row.player_id: row for row in season.players}
    actual_scores: dict[tuple[str, int], float] = {}
    preseason_scores: dict[str, float] = {}
    prior_averages: dict[tuple[str, int], float] = {}
    required_weeks = tuple(sorted({*config.regular_season_weeks, *config.playoff_weeks}))

    for player in season.players:
        preseason_scores[player.player_id] = _preseason_weekly_score(player, config)
        played_scores: list[tuple[int, float]] = []
        for outcome in player.actual_weeks:
            score = (
                score_raw_stats(outcome.stats, config.scoring_weights)
                if outcome.status is ActualWeekStatus.PLAYED
                else 0.0
            )
            actual_scores[player.player_id, outcome.week] = score
            if outcome.status is ActualWeekStatus.PLAYED:
                played_scores.append((outcome.week, score))
        prior: list[float] = []
        played_index = 0
        for week in required_weeks:
            # A lineup lock may see completed prior weeks, never this week.
            while (
                played_index < len(played_scores)
                and played_scores[played_index][0] < week
            ):
                prior.append(played_scores[played_index][1])
                played_index += 1
            if prior:
                prior_averages[player.player_id, week] = math.fsum(prior) / len(prior)

    return _PreparedScoringContext(
        season,
        config.config_id,
        MappingProxyType(players),
        MappingProxyType(actual_scores),
        MappingProxyType(preseason_scores),
        MappingProxyType(prior_averages),
    )


def _validate_scoring_inputs(season, config):
    if not isinstance(season, HistoricalSeason) or not isinstance(config, DraftLeagueConfig):
        raise ValueError("season and config must use draft domain types")
    playoff_rounds = max(1, math.ceil(math.log2(config.playoff_team_count)))
    if len(config.playoff_weeks) != playoff_rounds:
        raise ValueError("season simulation requires exactly one playoff week per round")
    required_weeks = set(config.regular_season_weeks) | set(config.playoff_weeks)
    if not required_weeks.issubset(season.available_weeks):
        raise ValueError("historical season does not have the configured week coverage")
    if any(
        row.week in required_weeks and row.status is ActualWeekStatus.MISSING
        for player in season.players
        for row in player.actual_weeks
    ):
        raise ValueError("historical season has missing outcomes in configured weeks")


def _validated_scoring_context(context, season, config):
    if not isinstance(context, _PreparedScoringContext):
        raise ValueError("prepared scoring context is invalid")
    if context.config_id != config.config_id or not (
        context.season is season or context.season == season
    ):
        raise ValueError("prepared scoring context does not match season and config")
    return context


def _validate_rosters(rosters, config, players):
    if isinstance(rosters, (str, bytes)):
        raise ValueError("rosters must contain every configured team")
    try:
        result = tuple(tuple(roster) for roster in rosters)
    except TypeError:
        raise ValueError("rosters must contain player-ID sequences") from None
    if len(result) != config.team_count:
        raise ValueError("rosters must contain every configured team")
    seen: set[str] = set()
    for roster in result:
        if len(roster) != config.roster_size:
            raise ValueError("every completed roster must have the configured roster size")
        if any(not isinstance(player_id, str) or not player_id for player_id in roster):
            raise ValueError("rosters must contain non-empty player IDs")
        if len(set(roster)) != len(roster) or seen.intersection(roster):
            raise ValueError("a player cannot appear on more than one roster")
        seen.update(roster)
        if not set(roster).issubset(players):
            raise ValueError("every rostered player must exist in the historical season")
        counts: dict[str, int] = {}
        for player_id in roster:
            position = players[player_id].position
            counts[position] = counts.get(position, 0) + 1
        if any(counts.get(position, 0) > maximum for position, maximum in config.position_limits.items()):
            raise ValueError("a completed roster exceeds its configured position limit")
        structural = _optimize(roster, config, players, {player_id: 1.0 for player_id in roster})
        if any(row.player_id is None for row in structural.assignments):
            raise ValueError("a completed roster cannot fill every configured starting slot")
    return result


def _select_lineup(roster, week, config, context):
    weights = {}
    for player_id in roster:
        player = context.players[player_id]
        # Bye weeks are part of the preseason snapshot. Same-week inactive or
        # played status is an outcome and must not influence a historical lock.
        if player.bye_week == week:
            continue
        prior_average = context.prior_played_averages.get((player_id, week))
        weights[player_id] = (
            context.preseason_scores[player_id]
            if prior_average is None
            else (context.preseason_scores[player_id] + prior_average) / 2
        )
    optimized = _optimize(roster, config, context.players, weights)
    slots = []
    actual_scores = []
    for assignment in optimized.assignments:
        player_id = assignment.player_id
        actual = 0.0 if player_id is None else context.actual_scores[player_id, week]
        slots.append(LineupSlotResult(
            assignment.slot_index, assignment.slot, player_id,
            None if player_id is None else context.players[player_id].display_name,
            assignment.weight, actual,
        ))
        actual_scores.append(actual)
    return tuple(slots), math.fsum(actual_scores)


def _optimize(roster, config, players, weights):
    candidates = []
    for player_id in roster:
        if player_id not in weights:
            continue
        player = players[player_id]
        slot_weights = {
            slot: weights[player_id]
            for slot in set(config.starting_slots)
            if set(player.eligible_positions).intersection(config.slot_eligibility[slot])
        }
        candidates.append(LineupPlayer(player_id, slot_weights))
    return optimize_lineup(config.starting_slots, candidates)


def _preseason_weekly_score(player: PreseasonPlayer, config: DraftLeagueConfig) -> float:
    projection_horizon = _projected_game_horizon(player, config)
    for name in ("projected_fantasy_points", "projected_points"):
        value = resolve_preseason_projection(player, name)
        if value is not None:
            return value / projection_horizon
    usable = {}
    for stat_name in config.scoring_weights:
        value = resolve_preseason_projection(player, f"projected_stat.{stat_name}")
        if value is not None:
            usable[stat_name] = value
    if usable:
        return score_raw_stats(usable, config.scoring_weights) / projection_horizon
    for name in ("ecr_rank", "overall_rank", "rank", "adp"):
        value = resolve_preseason_projection(player, name)
        if value is not None and value >= 0:
            return 1 / (1 + value)
    return 0.0


def _projected_game_horizon(
    player: PreseasonPlayer,
    config: DraftLeagueConfig,
) -> float:
    for name in ("projected_games", "projected_games_played"):
        value = resolve_preseason_projection(player, name)
        if value is not None and math.isfinite(value) and value > 0:
            return value
    return float(len(config.regular_season_weeks))


def _round_robin_schedule(team_ids, weeks):
    rotation: list[str | None] = list(team_ids)
    if len(rotation) % 2:
        rotation.append(None)
    rounds = []
    for _ in range(len(rotation) - 1):
        rounds.append(tuple(
            (rotation[index], rotation[-1 - index])
            for index in range(len(rotation) // 2)
        ))
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]
    return tuple((week, rounds[index % len(rounds)]) for index, week in enumerate(weeks))


def _playoffs(playoff_ids, weeks, names, lineup, week_rows):
    seeds = {team_id: index for index, team_id in enumerate(playoff_ids, 1)}
    size = 1 << math.ceil(math.log2(len(playoff_ids)))
    byes = size - len(playoff_ids)
    active = [(seeds[team_id], team_id) for team_id in playoff_ids]
    eliminated: dict[str, int] = {}
    games = []
    game_number = 0
    for round_index, week in enumerate(weeks[:int(math.log2(size))], 1):
        active.sort()
        if round_index == 1 and byes:
            advancing = active[:byes]
            playing = active[byes:]
            for seed, team_id in advancing:
                selected, score = lineup(team_id, week)
                game_number += 1
                games.append(BracketGame(
                    round_index, week, game_number, seed, team_id, names[team_id],
                    None, None, None, score, None, team_id, False,
                ))
                week_rows[team_id].append(_week_result(
                    team_id, names, week, SeasonStage.PLAYOFF, None,
                    GameOutcome.BYE, score, None, selected,
                ))
        else:
            advancing, playing = [], active
        pairs = tuple(zip(playing[:len(playing) // 2], reversed(playing[len(playing) // 2:])))
        for (high_seed, high), (low_seed, low) in pairs:
            high_lineup, high_score = lineup(high, week)
            low_lineup, low_score = lineup(low, week)
            high_wins = high_score >= low_score
            winner, loser = (high, low) if high_wins else (low, high)
            winner_seed = high_seed if high_wins else low_seed
            eliminated[loser] = round_index
            advancing.append((winner_seed, winner))
            game_number += 1
            games.append(BracketGame(
                round_index, week, game_number, high_seed, high, names[high],
                low_seed, low, names[low], high_score, low_score, winner,
                high_score == low_score,
            ))
            high_outcome = GameOutcome.WIN if high_wins else GameOutcome.LOSS
            low_outcome = GameOutcome.LOSS if high_wins else GameOutcome.WIN
            week_rows[high].append(_week_result(
                high, names, week, SeasonStage.PLAYOFF, low,
                high_outcome, high_score, low_score, high_lineup,
            ))
            week_rows[low].append(_week_result(
                low, names, week, SeasonStage.PLAYOFF, high,
                low_outcome, low_score, high_score, low_lineup,
            ))
        active = advancing
    if len(active) != 1:
        raise ValueError("configured playoff weeks did not produce one champion")
    return games, active[0][1], eliminated


def _winning_percentage(record):
    games = record.wins + record.losses + record.ties
    return 0.0 if not games else (record.wins + 0.5 * record.ties) / games


def _apply_regular(left, right, left_score, right_score):
    left.points_for += left_score
    left.points_against += right_score
    right.points_for += right_score
    right.points_against += left_score
    if left_score > right_score:
        left.wins += 1; right.losses += 1
    elif right_score > left_score:
        right.wins += 1; left.losses += 1
    else:
        left.ties += 1; right.ties += 1


def _outcomes(left, right):
    if left > right:
        return GameOutcome.WIN, GameOutcome.LOSS
    if right > left:
        return GameOutcome.LOSS, GameOutcome.WIN
    return GameOutcome.TIE, GameOutcome.TIE


def _week_result(team_id, names, week, stage, opponent, outcome, score, opponent_score, lineup):
    return TeamWeekResult(
        team_id, names[team_id], week, stage, opponent,
        None if opponent is None else names[opponent], outcome,
        score, opponent_score, tuple(lineup),
    )

def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_json_value(row) for row in value]
    return value

__all__ = (
    "BracketGame", "FinalStanding", "GameOutcome", "HistoricalSeasonTrace", "LineupSlotResult",
    "SeasonStage", "TeamSeasonTrace", "TeamWeekResult", "simulate_historical_season",
)
