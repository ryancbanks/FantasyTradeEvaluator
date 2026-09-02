"""Independent trade-delta validation for fitted strength models."""

from dataclasses import dataclass
from typing import Sequence

from ._analyzer_types import PowerRankingChange
from ._calibration_inputs import CalibrationTradeObservation, RosterPowerSample
from ._calibration_results import CalibrationFitConfig
from .strength import CalibrationStatus, StrengthModel


@dataclass(frozen=True, slots=True)
class HoldoutMetrics:
    max_absolute_score_error: float | None
    max_delta_error: float | None
    display_match_rate: float | None


def training_errors(
    model: StrengthModel, samples: Sequence[RosterPowerSample]
) -> tuple[float, ...]:
    return tuple(
        model.score_roster(row.roster_player_ids).power_score - row.raw_power_score
        for row in samples
    )


def heldout_trade_metrics(
    model: StrengthModel,
    trades: Sequence[CalibrationTradeObservation],
) -> HoldoutMetrics:
    if not trades:
        return HoldoutMetrics(None, None, None)
    score_errors = []
    delta_errors = []
    display_matches = 0
    for trade in trades:
        team_rows = (
            (
                trade.team1_id,
                trade.team1_before_player_ids,
                trade.team1_after_player_ids,
                trade.team1_raw_before,
                trade.team1_raw_after,
            ),
            (
                trade.team2_id,
                trade.team2_before_player_ids,
                trade.team2_after_player_ids,
                trade.team2_raw_before,
                trade.team2_raw_after,
            ),
        )
        for team_id, before, after, raw_before, raw_after in team_rows:
            predicted_before = model.score_roster(before).power_score
            predicted_after = model.score_roster(after).power_score
            score_errors.extend(
                (predicted_before - raw_before, predicted_after - raw_after)
            )
            delta_errors.append(
                (predicted_after - predicted_before) - (raw_after - raw_before)
            )
            expected = PowerRankingChange(team_id, raw_before, raw_after)
            predicted = PowerRankingChange(team_id, predicted_before, predicted_after)
            display_matches += expected.display_delta_text == predicted.display_delta_text
    return HoldoutMetrics(
        max(abs(value) for value in score_errors),
        max(abs(value) for value in delta_errors),
        display_matches / (2 * len(trades)),
    )


def calibration_status(
    config: CalibrationFitConfig,
    *,
    converged: bool,
    identifiable: bool,
    training_max_error: float,
    trades: Sequence[CalibrationTradeObservation],
    distinct_perturbation_count: int,
    holdout: HoldoutMetrics,
) -> CalibrationStatus:
    if (
        type(distinct_perturbation_count) is not int
        or distinct_perturbation_count < 0
        or distinct_perturbation_count > len(trades)
    ):
        raise ValueError("distinct_perturbation_count is inconsistent with held-out trades")
    if not trades:
        return CalibrationStatus.UNVALIDATED
    if (
        len(trades) >= config.minimum_exact_holdouts
        and distinct_perturbation_count >= config.minimum_exact_holdouts
        and converged
        and identifiable
        and training_max_error <= config.exact_raw_tolerance
        and holdout.max_absolute_score_error is not None
        and holdout.max_absolute_score_error <= config.exact_raw_tolerance
        and holdout.max_delta_error is not None
        and holdout.max_delta_error <= config.exact_raw_tolerance
        and holdout.display_match_rate == 1.0
    ):
        return CalibrationStatus.EXACT
    return CalibrationStatus.SURROGATE
