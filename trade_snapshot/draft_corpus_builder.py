"""Deterministically assemble the public Draft Lab starter corpus."""

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .draft_corpus_sources import (
    STARTER_CORPUS_YEARS,
    STARTER_TRANSFORM_VERSION,
    FfcAdpPlayer,
    RosterPlayer,
    ScheduleSeason,
    load_ffc_adp,
    load_player_week_stats,
    load_previous_roster_teams,
    load_schedules,
    load_team_week_stats,
    load_week_one_roster,
    normalized_player_name,
)
from .draft_history import (
    ActualPlayerWeek,
    ActualWeekStatus,
    DataProvenance,
    HistoricalCorpus,
    HistoricalSeason,
    PreseasonPlayer,
)

_FEATURE_NAMES = (
    "adp",
    "adp_standard_deviation",
    "best_rank",
    "position_rank",
    "worst_rank",
)
_MAX_CORPUS_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceStamp:
    url: str
    sha256: str
    size: int
    source_updated_at: str | None


@dataclass(frozen=True, slots=True)
class StarterCorpusFiles:
    schedule: Path
    ffc_adp: Mapping[int, Path]
    player_stats: Mapping[int, Path]
    team_stats: Mapping[int, Path]
    rosters: Mapping[int, Path]
    source_stamps: Mapping[str, SourceStamp]


@dataclass(frozen=True, slots=True)
class StarterCorpusBuild:
    corpus: HistoricalCorpus
    status: str
    coverage: Mapping[str, object]
    serialized_bytes: int


def build_starter_corpus(
    files: StarterCorpusFiles,
    *,
    years: tuple[int, ...] = STARTER_CORPUS_YEARS,
    should_cancel=lambda: False,
    on_season=lambda season, completed, total: None,
) -> StarterCorpusBuild:
    """Build and revalidate a leak-free corpus from already verified downloads."""

    years = _years(years)
    _validate_file_map("FFC ADP", files.ffc_adp, years)
    _validate_file_map("player stats", files.player_stats, years)
    _validate_file_map("team stats", files.team_stats, years)
    _validate_file_map(
        "rosters", files.rosters, (*years, *(year - 1 for year in years))
    )
    schedules = load_schedules(files.schedule, years)
    seasons = []
    coverage_rows = []
    source_dates = {}
    for completed, season in enumerate(years, 1):
        if should_cancel():
            raise InterruptedError("starter corpus build stopped between seasons")
        schedule = schedules[season]
        ffc = load_ffc_adp(files.ffc_adp[season], season, schedule.kickoff_at)
        roster = load_week_one_roster(files.rosters[season], season)
        prior_teams = load_previous_roster_teams(files.rosters[season - 1], season - 1)
        player_stats = load_player_week_stats(files.player_stats[season], season)
        team_stats = load_team_week_stats(files.team_stats[season], season)
        built, row = _build_season(
            season,
            schedule,
            ffc.players,
            roster.players,
            prior_teams,
            player_stats,
            team_stats,
            roster.rejected_row_count,
            roster.excluded_status_counts,
        )
        seasons.append(
            HistoricalSeason(
                season,
                ffc.source_as_of,
                schedule.kickoff_at,
                schedule.available_weeks,
                built,
            )
        )
        coverage_rows.append(row)
        source_dates[season] = ffc.source_as_of
        on_season(season, completed, len(years))
    nflverse_updated_at = _latest_source_timestamp(files.source_stamps)
    provenance = (
        DataProvenance(
            source="Fantasy Football Calculator",
            captured_at=max(source_dates.values(), key=_parse_timestamp),
            scope=(
                "12-team PPR preseason ADP snapshots. The starter transform uses "
                "ADP, ADP deviation, high/low draft ranks, and derived position rank."
            ),
            license=None,
            source_url="https://fantasyfootballcalculator.com/adp",
            preseason_feature_names=_FEATURE_NAMES,
            preseason_source_as_of=source_dates,
        ),
        DataProvenance(
            source="nflverse-data",
            captured_at=nflverse_updated_at,
            scope=(
                "Historical regular-season schedules, weekly rosters, player stats, "
                "and team stats used only for metadata and realized outcomes."
            ),
            license="CC-BY-4.0",
            source_url="https://github.com/nflverse/nflverse-data",
        ),
    )
    corpus = HistoricalCorpus(tuple(seasons), provenance)
    # Exercise the same strict import boundary that a user-supplied corpus crosses.
    corpus = HistoricalCorpus.from_record(corpus.to_record())
    serialized_bytes = _serialized_size(corpus.to_record())
    if serialized_bytes > _MAX_CORPUS_BYTES:
        raise ValueError(
            "built starter corpus exceeds Draft Lab's 128 MB validated import limit"
        )
    gaps = sum(int(row["gap_count"]) for row in coverage_rows)
    status = "ready" if gaps == 0 else "ready_with_gaps"
    return StarterCorpusBuild(
        corpus,
        status,
        {
            "kind": "draft_starter_corpus_coverage",
            "schema_version": 1,
            "transform_version": STARTER_TRANSFORM_VERSION,
            "status": status,
            "seasons": coverage_rows,
            "season_count": len(coverage_rows),
            "player_seasons": sum(
                int(row["installed_players"]) for row in coverage_rows
            ),
            "gap_count": gaps,
            "fixed_bye_policy": (
                "A player-season is excluded if nflverse records actual play during "
                "the preseason team's scheduled bye. Draft Lab v1 uses one fixed bye."
            ),
            "identity_policy": (
                "Unique normalized name + position + team matches only; no fuzzy matches."
            ),
            "first_year_on_team_policy": (
                "Week 1 team is compared with the player's final rostered team in the "
                "prior season. Missing veteran history is retained as false and disclosed."
            ),
        },
        serialized_bytes,
    )


def _build_season(
    season: int,
    schedule: ScheduleSeason,
    ffc_players: tuple[FfcAdpPlayer, ...],
    roster_players: tuple[RosterPlayer, ...],
    prior_teams: Mapping[str, str],
    player_stats: Mapping[str, Mapping[int, Mapping[str, float]]],
    team_stats: Mapping[tuple[str, int], Mapping[str, float]],
    rejected_roster_rows: int,
    excluded_status_counts: Mapping[str, int],
):
    ffc_by_key: defaultdict[tuple[str, str, str], list[FfcAdpPlayer]] = defaultdict(
        list
    )
    ffc_dst_by_team: defaultdict[str, list[FfcAdpPlayer]] = defaultdict(list)
    for player in ffc_players:
        if player.position == "DST":
            if player.team is not None:
                ffc_dst_by_team[player.team].append(player)
        elif player.team is not None:
            ffc_by_key[
                normalized_player_name(player.display_name),
                player.position,
                player.team,
            ].append(player)
    roster_keys: Counter[tuple[str, str, str]] = Counter(
        (normalized_player_name(player.display_name), player.position, player.team)
        for player in roster_players
    )
    built = []
    matched_source_ids = set()
    gaps = Counter()
    gap_samples: defaultdict[str, list[str]] = defaultdict(list)
    finite_features = Counter()
    for roster in roster_players:
        key = normalized_player_name(roster.display_name), roster.position, roster.team
        matches = ffc_by_key.get(key, ())
        adp = matches[0] if len(matches) == 1 and roster_keys[key] == 1 else None
        if adp is None:
            _gap(gaps, gap_samples, "roster_player_without_adp", roster.display_name)
        else:
            matched_source_ids.add(adp.source_player_id)
            if adp.bye_week is not None and adp.bye_week != schedule.bye_by_team.get(
                roster.team
            ):
                _gap(
                    gaps,
                    gap_samples,
                    "ffc_schedule_bye_disagreement",
                    roster.display_name,
                )
        bye = schedule.bye_by_team.get(roster.team)
        if bye is None:
            _gap(gaps, gap_samples, "roster_team_missing_schedule", roster.display_name)
            continue
        actual = player_stats.get(roster.player_id, {})
        if bye in actual:
            _gap(
                gaps,
                gap_samples,
                "played_during_fixed_preseason_bye",
                roster.display_name,
            )
            continue
        features = _features(adp)
        finite_features.update(
            name for name, value in features.items() if value is not None
        )
        previous_team = prior_teams.get(roster.player_id)
        if previous_team is None and not roster.rookie:
            _gap(
                gaps, gap_samples, "veteran_prior_team_unavailable", roster.display_name
            )
        weeks = tuple(
            _player_week(week, bye, actual.get(week))
            for week in schedule.available_weeks
        )
        built.append(
            PreseasonPlayer(
                player_id=roster.player_id,
                display_name=roster.display_name,
                position=roster.position,
                eligible_positions=(roster.position,),
                nfl_team_id=roster.team,
                bye_week=bye,
                nfl_experience_years=roster.nfl_experience_years,
                rookie=roster.rookie,
                first_year_on_team=(
                    roster.rookie
                    if previous_team is None
                    else previous_team != roster.team
                ),
                preseason_features=features,
                actual_weeks=weeks,
            )
        )
    for team, bye in sorted(schedule.bye_by_team.items()):
        matches = ffc_dst_by_team.get(team, ())
        adp = matches[0] if len(matches) == 1 else None
        if adp is None:
            _gap(gaps, gap_samples, "defense_without_adp", f"{team} DST")
        else:
            matched_source_ids.add(adp.source_player_id)
        features = _features(adp)
        finite_features.update(
            name for name, value in features.items() if value is not None
        )
        weeks = tuple(
            _dst_player_week(team, week, bye, team_stats, schedule)
            for week in schedule.available_weeks
        )
        built.append(
            PreseasonPlayer(
                player_id=f"nflverse-{season}-{team}-dst",
                display_name=adp.display_name if adp is not None else f"{team} Defense",
                position="DST",
                eligible_positions=("DST",),
                nfl_team_id=team,
                bye_week=bye,
                nfl_experience_years=40,
                rookie=False,
                first_year_on_team=False,
                preseason_features=features,
                actual_weeks=weeks,
            )
        )
    for source in ffc_players:
        if source.source_player_id not in matched_source_ids:
            _gap(
                gaps, gap_samples, "adp_player_not_exactly_matched", source.display_name
            )
    installed_by_position = Counter(player.position for player in built)
    for position in ("QB", "RB", "WR", "TE", "K", "DST"):
        if installed_by_position[position] < 12:
            raise ValueError(
                f"{season} starter corpus has fewer than 12 {position} players"
            )
    if rejected_roster_rows:
        gaps["rejected_week_one_roster_rows"] += rejected_roster_rows
    gap_count = sum(gaps.values())
    return tuple(built), {
        "season": season,
        "status": "ready" if gap_count == 0 else "ready_with_gaps",
        "installed_players": len(built),
        "installed_by_position": dict(sorted(installed_by_position.items())),
        "ffc_players": len(ffc_players),
        "ffc_exact_matches": len(matched_source_ids),
        "finite_feature_counts": dict(sorted(finite_features.items())),
        "gap_count": gap_count,
        "gaps": dict(sorted(gaps.items())),
        "gap_samples": {key: values for key, values in sorted(gap_samples.items())},
        "excluded_week_one_status_rows": dict(excluded_status_counts),
    }


def _features(player: FfcAdpPlayer | None) -> dict[str, float | None]:
    return {
        "adp": None if player is None else player.adp,
        "adp_standard_deviation": None
        if player is None
        else player.adp_standard_deviation,
        "best_rank": None if player is None else player.best_rank,
        "position_rank": None if player is None else float(player.position_rank),
        "worst_rank": None if player is None else player.worst_rank,
    }


def _player_week(week: int, bye: int, stats: Mapping[str, float] | None):
    if week == bye:
        return ActualPlayerWeek(week, ActualWeekStatus.BYE, {})
    if stats is None:
        return ActualPlayerWeek(week, ActualWeekStatus.INACTIVE, {})
    return ActualPlayerWeek(week, ActualWeekStatus.PLAYED, stats)


def _dst_week(
    team: str,
    week: int,
    team_stats: Mapping[tuple[str, int], Mapping[str, float]],
    schedule: ScheduleSeason,
) -> dict[str, float]:
    raw = dict(team_stats.get((team, week), {}))
    allowed = schedule.points_allowed.get((team, week))
    if allowed is None:
        raise ValueError(f"schedule is missing {team} points allowed in week {week}")
    raw.update(
        {
            "dst_sacks": raw.get("def_sacks", 0.0),
            "dst_interceptions": raw.get("def_interceptions", 0.0),
            "dst_fumble_recoveries": raw.get(
                "def_fumbles", raw.get("fumble_recovery_opp", 0.0)
            ),
            "dst_touchdowns": math.fsum(
                (
                    raw.get("def_tds", 0.0),
                    raw.get("fumble_recovery_tds", 0.0),
                    raw.get("special_teams_tds", 0.0),
                )
            ),
            "dst_safeties": raw.get("def_safeties", 0.0),
            "dst_points_allowed": allowed,
            "dst_points_allowed_0": float(allowed == 0),
            "dst_points_allowed_1_6": float(1 <= allowed <= 6),
            "dst_points_allowed_7_13": float(7 <= allowed <= 13),
            "dst_points_allowed_14_20": float(14 <= allowed <= 20),
            "dst_points_allowed_21_27": float(21 <= allowed <= 27),
            "dst_points_allowed_28_34": float(28 <= allowed <= 34),
            "dst_points_allowed_35_plus": float(allowed >= 35),
        }
    )
    return dict(sorted(raw.items()))


def _dst_player_week(team, week, bye, team_stats, schedule):
    if week == bye:
        return ActualPlayerWeek(week, ActualWeekStatus.BYE, {})
    # A schedule gap outside the bye is a verified non-game (notably the
    # cancelled BUF-CIN Week 17 game in 2022), so it is inactive rather than
    # fabricated as a played zero.
    if (team, week) not in schedule.points_allowed:
        return ActualPlayerWeek(week, ActualWeekStatus.INACTIVE, {})
    return ActualPlayerWeek(
        week,
        ActualWeekStatus.PLAYED,
        _dst_week(team, week, team_stats, schedule),
    )


def _gap(gaps: Counter, samples: defaultdict[str, list[str]], name: str, value: str):
    gaps[name] += 1
    if len(samples[name]) < 12:
        samples[name].append(value)


def _years(value) -> tuple[int, ...]:
    years = tuple(value)
    if not years or tuple(sorted(set(years))) != years:
        raise ValueError("starter corpus years must be unique and increasing")
    if not set(years).issubset(STARTER_CORPUS_YEARS):
        raise ValueError("starter corpus includes an unsupported year")
    return years


def _validate_file_map(name: str, files: Mapping[int, Path], years) -> None:
    absent = set(years).difference(files)
    if absent:
        raise ValueError(f"{name} is missing season {min(absent)}")
    for year in years:
        path = Path(files[year])
        if not path.is_file():
            raise ValueError(f"{name} file for {year} is missing")


def _latest_source_timestamp(stamps: Mapping[str, SourceStamp]) -> str:
    values = [
        stamp.source_updated_at for stamp in stamps.values() if stamp.source_updated_at
    ]
    if not values:
        raise ValueError("nflverse release metadata has no source update timestamps")
    for value in values:
        _parse_timestamp(value)
    return max(values, key=_parse_timestamp)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source timestamp must include a timezone")
    return parsed


def _serialized_size(record: Mapping[str, object]) -> int:
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    total = 0
    for fragment in encoder.iterencode(record):
        total += len(fragment.encode("utf-8"))
        if total > _MAX_CORPUS_BYTES:
            break
    return total


__all__ = (
    "SourceStamp",
    "StarterCorpusBuild",
    "StarterCorpusFiles",
    "build_starter_corpus",
)
