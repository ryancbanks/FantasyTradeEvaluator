"""Leak-free anonymous features and the regression starting draft brain."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import fsum, isfinite, sqrt

from .draft_brain import DraftBrain, FeatureSchema, RegressionBaseline
from .draft_config import DraftLeagueConfig, score_raw_stats
from .draft_feature_policy import validate_preseason_feature_name
from .draft_history import (
    ActualWeekStatus,
    HistoricalCorpus,
    HistoricalSeason,
    PreseasonPlayer,
)
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_player_position


_POSITIONS = tuple(sorted(CANONICAL_PLAYER_POSITIONS))
_MAX_REGRESSION_PASSES = 64
_RIDGE_PENALTY = 1.0
_PROJECTION_PROVIDERS = ("fantasypros", "espn", "yahoo")


@dataclass(slots=True)
class _CandidateFeatureContext:
    config: DraftLeagueConfig
    common: dict[str, float]
    roster_groups: tuple[tuple[str, ...], ...]
    roster_counts: Counter[str]
    supply: Counter[str]
    filled: int


def resolve_preseason_projection(
    player: PreseasonPlayer,
    feature_name: str,
) -> float | None:
    """Resolve one semantic projection without double-counting provider copies.

    A captured ensemble is canonical. A generic projection is the next-best
    portable representation. If only provider-specific observations exist,
    their arithmetic mean is returned; provider copies are never summed.
    """

    if not isinstance(player, PreseasonPlayer):
        raise ValueError("player must be a PreseasonPlayer")
    canonical_name = validate_preseason_feature_name(feature_name)
    if canonical_name.startswith(("ensemble.", "fantasypros.", "espn.", "yahoo.")):
        raise ValueError("feature_name must be an unnamespaced projection field")
    features = player.preseason_features
    for key in (f"ensemble.{canonical_name}", canonical_name):
        value = features.get(key)
        if value is not None:
            return float(value)
    provider_values = [
        float(value)
        for provider in _PROJECTION_PROVIDERS
        if (value := features.get(f"{provider}.{canonical_name}")) is not None
    ]
    if not provider_values:
        return None
    return fsum(provider_values) / len(provider_values)


def candidate_feature_values(
    player: PreseasonPlayer,
    *,
    config: DraftLeagueConfig,
    round_number: int,
    overall_pick: int,
    roster_player_positions: Sequence[str],
    roster_player_eligibilities: Sequence[Sequence[str]] | None = None,
    available_position_counts: Mapping[str, int],
    picks_until_next: int,
    _prepared: _CandidateFeatureContext | None = None,
) -> dict[str, float | None]:
    """Return only anonymous preseason and current-draft numeric information."""

    if not isinstance(player, PreseasonPlayer):
        raise ValueError("player must be a PreseasonPlayer")
    prepared = _prepared or _prepare_candidate_feature_context(
        config,
        round_number,
        overall_pick,
        roster_player_positions,
        roster_player_eligibilities,
        available_position_counts,
        picks_until_next,
    )
    config = prepared.config
    fields: dict[str, float | None] = {
        f"preseason.{_preseason_name(name)}": value
        for name, value in player.preseason_features.items()
    }
    for position in _POSITIONS:
        fields[f"position.{position.lower()}"] = float(player.position == position)
        fields[f"eligible.{position.lower()}"] = float(
            position in player.eligible_positions
        )
    fields.update(
        {
            "bio.bye_week": float(player.bye_week),
            "bio.experience_years": float(player.nfl_experience_years),
            "bio.rookie": float(player.rookie),
            "bio.first_year_on_team": float(player.first_year_on_team),
            **prepared.common,
            "context.starter_need": float(
                _filled_slots(
                    (*prepared.roster_groups, tuple(player.eligible_positions)),
                    config,
                )
                > prepared.filled
            ),
            "context.candidate_position_count": float(
                prepared.roster_counts[player.position]
            ),
            "context.candidate_limit_remaining": float(
                max(
                    0,
                    _position_limit(config, player.position)
                    - prepared.roster_counts[player.position],
                )
            ),
            "context.candidate_supply": float(prepared.supply[player.position]),
        }
    )
    return fields


def _prepare_candidate_feature_context(
    config,
    round_number,
    overall_pick,
    roster_player_positions,
    roster_player_eligibilities,
    available_position_counts,
    picks_until_next,
):
    if not isinstance(config, DraftLeagueConfig):
        raise ValueError("config must be a DraftLeagueConfig")
    _integer("round_number", round_number, 1, config.total_rounds)
    _integer("overall_pick", overall_pick, 1, config.team_count * config.total_rounds)
    _integer("picks_until_next", picks_until_next, 0, config.team_count * 2)
    roster = _positions("roster_player_positions", roster_player_positions)
    if len(roster) >= config.roster_size:
        raise ValueError("roster must have room for the candidate")
    supply = _supply(available_position_counts)

    roster_groups = _eligibility_groups(
        roster_player_eligibilities, roster
    )
    filled = _filled_slots(roster_groups, config)
    roster_counts = Counter(roster)
    total_picks = config.team_count * config.total_rounds
    common = {
        "context.round_number": float(round_number),
        "context.round_fraction": round_number / config.total_rounds,
        "context.overall_pick": float(overall_pick),
        "context.pick_fraction": overall_pick / total_picks,
        "context.picks_until_next": float(picks_until_next),
        "context.roster_count": float(len(roster)),
        "context.roster_fraction": len(roster) / config.roster_size,
        "context.roster_slots_left": float(config.roster_size - len(roster)),
        "context.unfilled_starters": float(len(config.starting_slots) - filled),
    }
    for position in _POSITIONS:
        current = roster_counts[position]
        generic_filled = _filled_slots((*roster_groups, (position,)), config)
        suffix = position.lower()
        common[f"context.roster.{suffix}"] = float(current)
        common[f"context.need.{suffix}"] = float(generic_filled > filled)
        common[f"context.limit_remaining.{suffix}"] = float(
            max(0, _position_limit(config, position) - current)
        )
        common[f"context.supply.{suffix}"] = float(supply[position])
        common[f"context.supply_per_team.{suffix}"] = (
            supply[position] / config.team_count
        )
    return _CandidateFeatureContext(
        config, common, roster_groups, roster_counts, supply, filled
    )


def fit_feature_schema(
    seasons: HistoricalCorpus | Iterable[HistoricalSeason],
    config: DraftLeagueConfig,
    training_years: Iterable[int] | None = None,
) -> FeatureSchema:
    """Fit feature ordering and normalization without inspecting actual outcomes."""

    if not isinstance(config, DraftLeagueConfig):
        raise ValueError("config must be a DraftLeagueConfig")
    available = _select_seasons(seasons, None)
    selected = _select_seasons(available, training_years)
    raw_names = tuple(
        sorted(
            {
                _preseason_name(name)
                for season in selected
                for player in season.players
                for name in player.preseason_features
            }
        )
    )
    ordered_names = _ordered_names(raw_names)
    rows = []
    for season in selected:
        supply = Counter(player.position for player in season.players)
        for player in season.players:
            rows.append(
                candidate_feature_values(
                    player,
                    config=config,
                    round_number=1,
                    overall_pick=1,
                    roster_player_positions=(),
                    available_position_counts=supply,
                    picks_until_next=max(0, config.team_count * 2 - 2),
                )
            )
    means = []
    scales = []
    for name in ordered_names:
        observed = tuple(
            float(row[name]) for row in rows if name in row and row[name] is not None
        )
        if not observed:
            mean, scale = 0.0, 1.0
        else:
            mean = fsum(observed) / len(observed)
            variance = fsum((value - mean) ** 2 for value in observed) / len(observed)
            scale = sqrt(variance) if variance > 0 else 1.0
        means.append(_rounded(mean))
        fitted_scale = _rounded(scale)
        scales.append(fitted_scale if fitted_scale > 0 else 1.0)
    missing = tuple(f"preseason.{name}" for name in raw_names)
    return FeatureSchema(tuple(ordered_names), tuple(means), tuple(scales), missing)


def fit_regression_baseline(
    schema: FeatureSchema,
    seasons: HistoricalCorpus | Iterable[HistoricalSeason],
    config: DraftLeagueConfig,
) -> RegressionBaseline:
    """Fit deterministic ridge coefficients to realized custom-scoring totals."""

    if not isinstance(schema, FeatureSchema):
        raise ValueError("schema must be a FeatureSchema")
    if not isinstance(config, DraftLeagueConfig):
        raise ValueError("config must be a DraftLeagueConfig")
    selected = _select_seasons(seasons, None)
    relevant_weeks = set((*config.regular_season_weeks, *config.playoff_weeks))
    samples: list[tuple[tuple[float, ...], float]] = []
    for season in selected:
        missing_weeks = relevant_weeks.difference(season.available_weeks)
        if missing_weeks:
            raise ValueError(
                f"season {season.season} lacks configured week {min(missing_weeks)}"
            )
        supply = Counter(player.position for player in season.players)
        for player in season.players:
            values = candidate_feature_values(
                player,
                config=config,
                round_number=1,
                overall_pick=1,
                roster_player_positions=(),
                available_position_counts=supply,
                picks_until_next=max(0, config.team_count * 2 - 2),
            )
            target = _actual_total(player, relevant_weeks, config)
            samples.append((schema.encode(values), target))
    coefficients, intercept = _fit_ridge(schema, samples)
    return RegressionBaseline(schema.feature_schema_id, coefficients, intercept)


def build_baseline_brain(
    corpus: HistoricalCorpus,
    config: DraftLeagueConfig,
    training_years: Iterable[int],
) -> DraftBrain:
    """Build the exact zero-residual starting brain for one league configuration."""

    if not isinstance(corpus, HistoricalCorpus):
        raise ValueError("corpus must be a HistoricalCorpus")
    selected = _select_seasons(corpus, training_years)
    selected_years = tuple(season.season for season in selected)
    schema = fit_feature_schema(corpus, config, selected_years)
    baseline = fit_regression_baseline(schema, selected, config)
    return DraftBrain.zero_residual(schema, baseline, config.config_id)


def _fit_ridge(
    schema: FeatureSchema,
    samples: Sequence[tuple[tuple[float, ...], float]],
) -> tuple[tuple[float, ...], float]:
    if not samples:
        raise ValueError("regression requires at least one player season")
    size = schema.vector_size
    active = tuple(
        index
        for index in range(size)
        if index >= len(schema.names) or not schema.names[index].startswith("context.")
    )
    targets = tuple(target for _, target in samples)
    target_mean = fsum(targets) / len(targets)
    column_means = {
        index: fsum(row[index] for row, _ in samples) / len(samples)
        for index in active
    }
    coefficients = [0.0] * size
    predictions = [0.0] * len(samples)
    for _ in range(_MAX_REGRESSION_PASSES):
        largest_change = 0.0
        for index in active:
            mean = column_means[index]
            old = coefficients[index]
            column = tuple(row[index] - mean for row, _ in samples)
            denominator = fsum(value * value for value in column) + _RIDGE_PENALTY
            numerator = fsum(
                value * (target - target_mean - prediction + old * value)
                for value, target, prediction in zip(column, targets, predictions)
            )
            new = numerator / denominator
            delta = new - old
            if delta:
                coefficients[index] = new
                for row_index, value in enumerate(column):
                    predictions[row_index] += delta * value
                largest_change = max(largest_change, abs(delta))
        if largest_change < 1e-10:
            break
    coefficients = [_rounded(value, 12) for value in coefficients]
    intercept = target_mean - fsum(
        coefficients[index] * column_means[index] for index in active
    )
    return tuple(coefficients), _rounded(intercept, 12)


def _actual_total(
    player: PreseasonPlayer,
    relevant_weeks: set[int],
    config: DraftLeagueConfig,
) -> float:
    scores = []
    for week in player.actual_weeks:
        if week.week not in relevant_weeks:
            continue
        if week.status is ActualWeekStatus.MISSING:
            raise ValueError(
                f"player outcome is missing for configured week {week.week}"
            )
        if week.status is ActualWeekStatus.PLAYED:
            scores.append(score_raw_stats(week.stats, config.scoring_weights))
    total = fsum(scores)
    if not isfinite(total) or abs(total) > 1.0e15:
        raise ValueError("actual fantasy-point target is not finite")
    return total


def _ordered_names(raw_names: tuple[str, ...]) -> tuple[str, ...]:
    static = [f"preseason.{name}" for name in raw_names]
    static.extend(f"position.{position.lower()}" for position in _POSITIONS)
    static.extend(f"eligible.{position.lower()}" for position in _POSITIONS)
    static.extend(
        ("bio.bye_week", "bio.experience_years", "bio.rookie", "bio.first_year_on_team")
    )
    context = [
        "context.round_number", "context.round_fraction", "context.overall_pick",
        "context.pick_fraction", "context.picks_until_next", "context.roster_count",
        "context.roster_fraction", "context.roster_slots_left",
        "context.unfilled_starters", "context.starter_need",
        "context.candidate_position_count", "context.candidate_limit_remaining",
        "context.candidate_supply",
    ]
    for position in _POSITIONS:
        suffix = position.lower()
        context.extend(
            (
                f"context.roster.{suffix}", f"context.need.{suffix}",
                f"context.limit_remaining.{suffix}", f"context.supply.{suffix}",
                f"context.supply_per_team.{suffix}",
            )
        )
    return tuple((*static, *context))


def _select_seasons(
    seasons: HistoricalCorpus | Iterable[HistoricalSeason],
    years: Iterable[int] | None,
) -> tuple[HistoricalSeason, ...]:
    source = seasons.seasons if isinstance(seasons, HistoricalCorpus) else seasons
    if isinstance(source, (str, bytes)):
        raise ValueError("seasons must contain HistoricalSeason values")
    try:
        available = tuple(source)
    except TypeError:
        raise ValueError("seasons must contain HistoricalSeason values") from None
    if not available or any(not isinstance(row, HistoricalSeason) for row in available):
        raise ValueError("seasons must contain HistoricalSeason values")
    by_year = {row.season: row for row in available}
    if len(by_year) != len(available):
        raise ValueError("seasons contain a duplicate year")
    if years is None:
        requested = tuple(sorted(by_year))
    else:
        if isinstance(years, (str, bytes)):
            raise ValueError("training_years must contain years")
        try:
            requested = tuple(years)
        except TypeError:
            raise ValueError("training_years must contain years") from None
        if not requested or any(type(year) is not int for year in requested):
            raise ValueError("training_years must contain integer years")
        if len(set(requested)) != len(requested):
            raise ValueError("training_years cannot contain duplicates")
        absent = set(requested).difference(by_year)
        if absent:
            raise ValueError(f"training year {min(absent)} is unavailable")
        requested = tuple(sorted(requested))
    return tuple(by_year[year] for year in requested)


def _filled_slots(
    player_eligibilities: Sequence[tuple[str, ...]], config: DraftLeagueConfig
) -> int:
    matched: dict[int, int] = {}

    def place(player_index: int, visited: set[int]) -> bool:
        eligible_positions = set(player_eligibilities[player_index])
        for slot_index, slot in enumerate(config.starting_slots):
            if slot_index in visited or not eligible_positions.intersection(
                config.slot_eligibility[slot]
            ):
                continue
            visited.add(slot_index)
            incumbent = matched.get(slot_index)
            if incumbent is None or place(incumbent, visited):
                matched[slot_index] = player_index
                return True
        return False

    for index in range(len(player_eligibilities)):
        place(index, set())
    return len(matched)


def _position_limit(config: DraftLeagueConfig, position: str) -> int:
    return config.position_limits.get(position, config.roster_size)


def _positions(name: str, values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a position sequence")
    try:
        result = tuple(
            normalize_player_position(value, require_supported=True) for value in values
        )
    except TypeError:
        raise ValueError(f"{name} must be a position sequence") from None
    return result


def _eligibility_groups(values, primary_positions):
    if values is None:
        return tuple((position,) for position in primary_positions)
    if isinstance(values, (str, bytes)):
        raise ValueError("roster_player_eligibilities must contain position sequences")
    try:
        groups = tuple(
            tuple(
                normalize_player_position(position, require_supported=True)
                for position in group
            )
            for group in values
        )
    except TypeError:
        raise ValueError(
            "roster_player_eligibilities must contain position sequences"
        ) from None
    if len(groups) != len(primary_positions) or any(
        not group or len(set(group)) != len(group) for group in groups
    ):
        raise ValueError("roster_player_eligibilities must match the current roster")
    if any(
        primary not in group
        for primary, group in zip(primary_positions, groups)
    ):
        raise ValueError("every roster primary position must be eligible")
    return groups


def _supply(values: Mapping[str, int]) -> Counter[str]:
    if not isinstance(values, Mapping):
        raise ValueError("available_position_counts must be a mapping")
    result: Counter[str] = Counter()
    for position, count in values.items():
        normalized = normalize_player_position(position, require_supported=True)
        _integer(f"available count for {normalized}", count, 0, 5_000)
        result[normalized] += count
    return result


def _preseason_name(name: str) -> str:
    return validate_preseason_feature_name(name)


def _integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


def _rounded(value: float, places: int = 15) -> float:
    result = round(float(value), places)
    if not isfinite(result):
        raise ValueError("fitted numeric value is not finite")
    return 0.0 if result == 0 else result


__all__ = (
    "fit_feature_schema", "fit_regression_baseline", "candidate_feature_values",
    "build_baseline_brain", "resolve_preseason_projection",
)
