"""League-wide orchestration over independently resumable team-pair searches."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .search import PreparedTradePair
from .roster_adjustment import PreparedRosterAdjuster
from .search_runner import (
    ResumableTradeSearch,
    TradeSearchOutcome,
    TradeSearchProgress,
    TradeSearchSettings,
)
from .search_store import QualifiedSearchResult
from .strength import StrengthModel
from .trade_impact import PreparedSeasonBaseline
from .trade_space import TeamRoster, TradeConstraints, TradeSpace


@dataclass(frozen=True, slots=True)
class LeagueQualifiedTrade:
    counterparty_team_id: str
    result: QualifiedSearchResult

    def __post_init__(self) -> None:
        if not isinstance(self.counterparty_team_id, str) or not self.counterparty_team_id:
            raise ValueError("counterparty_team_id must be a non-empty string")
        if not isinstance(self.result, QualifiedSearchResult):
            raise ValueError("result must be a QualifiedSearchResult")


@dataclass(frozen=True, slots=True)
class LeagueSearchProgress:
    pair_count: int
    completed_pair_count: int
    current_counterparty_team_id: str | None
    examined_candidate_count: int
    total_candidate_count: int
    qualified_trade_count: int
    mutual_playoff_gain_count: int
    cancelled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "pair_count",
            "completed_pair_count",
            "examined_candidate_count",
            "total_candidate_count",
            "qualified_trade_count",
            "mutual_playoff_gain_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.completed_pair_count > self.pair_count:
            raise ValueError("completed_pair_count cannot exceed pair_count")
        if self.examined_candidate_count > self.total_candidate_count:
            raise ValueError("examined candidates cannot exceed total candidates")
        if self.mutual_playoff_gain_count > self.qualified_trade_count:
            raise ValueError("mutual gain count cannot exceed qualified trade count")
        if self.current_counterparty_team_id is not None and (
            not isinstance(self.current_counterparty_team_id, str)
            or not self.current_counterparty_team_id
        ):
            raise ValueError("current_counterparty_team_id must be non-empty when present")
        if not isinstance(self.cancelled, bool):
            raise ValueError("cancelled must be a boolean")

    @property
    def completion_fraction(self) -> float:
        return (
            1.0
            if self.total_candidate_count == 0
            else self.examined_candidate_count / self.total_candidate_count
        )


@dataclass(frozen=True, slots=True)
class TeamPairSearchOutcome:
    counterparty_team_id: str
    search: TradeSearchOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.counterparty_team_id, str) or not self.counterparty_team_id:
            raise ValueError("counterparty_team_id must be a non-empty string")
        if not isinstance(self.search, TradeSearchOutcome):
            raise ValueError("search must be a TradeSearchOutcome")


@dataclass(frozen=True, slots=True)
class LeagueSearchOutcome:
    progress: LeagueSearchProgress
    pairs: tuple[TeamPairSearchOutcome, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.progress, LeagueSearchProgress):
            raise ValueError("progress must be LeagueSearchProgress")
        pairs = tuple(self.pairs)
        if any(not isinstance(row, TeamPairSearchOutcome) for row in pairs):
            raise ValueError("pairs must contain TeamPairSearchOutcome values")
        ids = tuple(row.counterparty_team_id for row in pairs)
        if len(set(ids)) != len(ids):
            raise ValueError("pairs contain a duplicate counterparty team")
        object.__setattr__(self, "pairs", pairs)

    @property
    def qualified_trades(self) -> tuple[LeagueQualifiedTrade, ...]:
        return tuple(
            LeagueQualifiedTrade(pair.counterparty_team_id, result)
            for pair in self.pairs
            for result in pair.search.results
        )

    @property
    def mutual_playoff_gains(self) -> tuple[LeagueQualifiedTrade, ...]:
        return tuple(
            LeagueQualifiedTrade(pair.counterparty_team_id, result)
            for pair in self.pairs
            for result in pair.search.mutual_playoff_gains
        )


class ResumableLeagueTradeSearch:
    """Search a primary roster against selected or every other league team."""

    def __init__(
        self,
        rosters: Iterable[TeamRoster],
        primary_team_id: str,
        model: StrengthModel,
        season_baseline: PreparedSeasonBaseline,
        constraints: TradeConstraints,
        search_settings: TradeSearchSettings | None = None,
        *,
        counterparty_team_ids: Iterable[str] | None = None,
    ) -> None:
        roster_rows = tuple(rosters)
        if not roster_rows or any(not isinstance(row, TeamRoster) for row in roster_rows):
            raise ValueError("rosters must contain TeamRoster values")
        by_team = {row.team_id: row for row in roster_rows}
        if len(by_team) != len(roster_rows):
            raise ValueError("rosters contain a duplicate team_id")
        if primary_team_id not in by_team:
            raise ValueError("primary_team_id is not present in rosters")
        if not isinstance(model, StrengthModel):
            raise ValueError("model must be a StrengthModel")
        if not isinstance(season_baseline, PreparedSeasonBaseline):
            raise ValueError("season_baseline must be a PreparedSeasonBaseline")
        if not isinstance(constraints, TradeConstraints):
            raise ValueError("constraints must be TradeConstraints")
        settings = search_settings or TradeSearchSettings()
        if not isinstance(settings, TradeSearchSettings):
            raise ValueError("search_settings must be TradeSearchSettings")
        ordered_league_ids = tuple(team.team_id for team in season_baseline.state.teams)
        if set(ordered_league_ids) != set(by_team):
            raise ValueError("rosters must exactly cover the season baseline teams")
        selected_ids = _counterparty_ids(
            counterparty_team_ids, ordered_league_ids, primary_team_id
        )
        primary = by_team[primary_team_id]
        adjuster = PreparedRosterAdjuster(
            model,
            roster_rows,
            forbid_drops=constraints.require_no_drops,
        )
        eligible_positions = {
            player_id: player.eligible_positions
            for player_id, player in model.players.items()
        }
        runners = []
        for team_id in selected_ids:
            other = by_team[team_id]
            pair = PreparedTradePair(model, primary, other, adjuster)
            space = TradeSpace(
                primary,
                other,
                constraints,
                eligible_positions_by_player=eligible_positions,
            )
            runners.append(
                (
                    team_id,
                    ResumableTradeSearch(space, pair, season_baseline, settings),
                )
            )
        self.primary_team_id = primary_team_id
        self.runners = tuple(runners)

    def run(
        self,
        database_directory: str | Path,
        *,
        on_progress: Callable[[LeagueSearchProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> LeagueSearchOutcome:
        if on_progress is not None and not callable(on_progress):
            raise ValueError("on_progress must be callable")
        if should_cancel is not None and not callable(should_cancel):
            raise ValueError("should_cancel must be callable")
        directory = Path(database_directory)
        directory.mkdir(parents=True, exist_ok=True)
        total = sum(runner.run_definition.total_candidate_count for _, runner in self.runners)
        outcomes: list[TeamPairSearchOutcome] = []
        examined_before = qualified_before = mutual_before = 0

        for pair_index, (team_id, runner) in enumerate(self.runners):
            filename_id = sha256(
                runner.run_definition.run_id.encode("utf-8")
            ).hexdigest()[:24]
            database = directory / f"pair-{pair_index:03d}-{filename_id}.sqlite3"

            def pair_progress(progress: TradeSearchProgress) -> None:
                if on_progress is not None:
                    on_progress(
                        LeagueSearchProgress(
                            len(self.runners),
                            pair_index,
                            team_id,
                            examined_before + progress.next_candidate_index,
                            total,
                            qualified_before + progress.power_qualified_count,
                            mutual_before + progress.mutual_playoff_gain_count,
                            progress.cancelled,
                        )
                    )

            outcome = runner.run(
                database,
                on_progress=pair_progress if on_progress is not None else None,
                should_cancel=should_cancel,
            )
            outcomes.append(TeamPairSearchOutcome(team_id, outcome))
            examined_before += outcome.progress.next_candidate_index
            qualified_before += outcome.progress.power_qualified_count
            mutual_before += outcome.progress.mutual_playoff_gain_count
            if outcome.progress.cancelled:
                progress = LeagueSearchProgress(
                    len(self.runners), pair_index, team_id, examined_before, total,
                    qualified_before, mutual_before, True,
                )
                return LeagueSearchOutcome(progress, tuple(outcomes))

        progress = LeagueSearchProgress(
            len(self.runners), len(self.runners), None, total, total,
            qualified_before, mutual_before, False,
        )
        if on_progress is not None:
            on_progress(progress)
        return LeagueSearchOutcome(progress, tuple(outcomes))


def _counterparty_ids(values, league_ids, primary_team_id) -> tuple[str, ...]:
    if values is None:
        return tuple(team_id for team_id in league_ids if team_id != primary_team_id)
    if isinstance(values, (str, bytes)):
        raise ValueError("counterparty_team_ids must be an iterable of team IDs")
    try:
        result = tuple(values)
    except TypeError:
        raise ValueError("counterparty_team_ids must be an iterable of team IDs") from None
    if not result or any(not isinstance(value, str) or not value for value in result):
        raise ValueError("counterparty_team_ids must contain non-empty team IDs")
    if len(set(result)) != len(result):
        raise ValueError("counterparty_team_ids contains a duplicate")
    if primary_team_id in result:
        raise ValueError("primary team cannot be its own counterparty")
    unknown = set(result).difference(league_ids)
    if unknown:
        raise ValueError(f"unknown counterparty team_id {min(unknown)!r}")
    return result
