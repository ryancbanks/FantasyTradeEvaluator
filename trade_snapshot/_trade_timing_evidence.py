"""Top-level evidence and coverage records for trade-timing outputs."""

from datetime import datetime, timezone

from ._scenario_random import content_id


def analysis_as_of(bundle, history):
    """Return the latest evidence cutoff represented by this result."""

    if history is not None:
        return history.bundle_captured_at
    return max(
        bundle.source_manifest.host_captured_at,
        bundle.source_manifest.fantasypros_captured_at,
        *(row.captured_at for row in bundle.ecr_snapshots),
        *(row.captured_at for row in bundle.projection_source_manifest.sources),
    )


def history_coverage(history, timing_profiles):
    if history is None:
        return {
            "status": "not_collected",
            "history_revision": None,
            "capture_ids": [],
            "team_coverage_statuses": {
                team_id: profile["coverage"]["status"]
                for team_id, profile in sorted(timing_profiles.items())
            },
            "incomplete_dimensions": sorted(
                {
                    dimension
                    for profile in timing_profiles.values()
                    for dimension in profile["coverage"]["incomplete_dimensions"]
                }
            ),
            "normalized_completed_deal_rates_available": False,
            "used_for_candidate_ranking_or_acceptance": False,
        }
    as_of = history.bundle_captured_at
    captures = tuple(row for row in history.captures if row.captured_at <= as_of)
    statuses = sorted(
        {profile["coverage"]["status"] for profile in timing_profiles.values()}
    )
    return {
        "status": statuses[0] if len(statuses) == 1 else "mixed",
        "history_revision": history.history_revision,
        "capture_ids": sorted(row.capture_id for row in captures),
        "team_coverage_statuses": {
            team_id: profile["coverage"]["status"]
            for team_id, profile in sorted(timing_profiles.items())
        },
        "incomplete_dimensions": sorted(
            {
                dimension
                for profile in timing_profiles.values()
                for dimension in profile["coverage"]["incomplete_dimensions"]
            }
        ),
        "normalized_completed_deal_rates_available": all(
            profile["coverage"]["normalized_rates_available"]
            for profile in timing_profiles.values()
        ),
        "used_for_candidate_ranking_or_acceptance": False,
    }


def timing_evidence(bundle, history, baseline=None):
    methodology = bundle.methodology_evidence
    methodology_evidence_id = getattr(
        methodology, "attestation_id", None
    ) or getattr(methodology, "disclosure_id", None)
    record = {
        "analysis_as_of": iso_utc(analysis_as_of(bundle, history)),
        "host_snapshot_id": bundle.state.snapshot_id,
        "league_binding_id": bundle.source_manifest.league_binding_id,
        "strength_model_id": bundle.strength_model.model_id,
        "strength_formula_id": bundle.strength_formula.formula_id,
        "methodology_evidence_id": methodology_evidence_id,
        "projection_source_manifest_id": (
            bundle.projection_source_manifest.manifest_id
        ),
        "ensemble_config_id": bundle.ensemble_config.config_id,
        "scenario_config_id": (
            bundle.scenario_config.config_id
            if baseline is None
            else baseline.scenarios.config.config_id
        ),
        "player_score_floor": (
            bundle.scenario_config.player_score_floor
            if baseline is None
            else baseline.scenarios.config.player_score_floor
        ),
        "baseline_scenario_run_id": (
            None if baseline is None else baseline.scenarios.run_id
        ),
        "draw_space_id": (
            None if baseline is None else baseline.scenarios.draw_space_id
        ),
        "ecr_ids": sorted(row.ecr_id for row in bundle.ecr_snapshots),
        "history_revision": None if history is None else history.history_revision,
    }
    return {**record, "evidence_id": content_id("trade-timing-evidence", record)}


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


__all__ = (
    "analysis_as_of",
    "history_coverage",
    "iso_utc",
    "timing_evidence",
)
