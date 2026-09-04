"""Projection-shape context for trade-timing candidates."""

from datetime import timezone
from statistics import mean

from .projection_lineage import ProjectionLineageIndex
from .projection_source import projection_input_id
from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjectionOrigin,
)


class _MarketEvidenceIndex:
    """Resolve one displayed player/week value to its retained raw artifacts."""

    def __init__(self, bundle):
        self.lineage = ProjectionLineageIndex(
            bundle.projections, bundle.projection_evidence
        )
        self.has_source_manifest = bundle.projection_source_manifest is not None
        self.source_by_input = {
            binding.projection_input_id: (source, binding)
            for source in (
                ()
                if bundle.projection_source_manifest is None
                else bundle.projection_source_manifest.sources
            )
            for binding in source.inputs
        }

    def provider_record(self, projection, observation):
        lineage = self.lineage.lineage_for(projection, observation)
        pair = projection.canonical_player_id, observation.provider
        weekly = self.lineage.weekly.get((*pair, projection.week))
        remaining = self.lineage.remaining_season_for(*pair)
        raw = (
            remaining
            if lineage.origin is WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON
            else weekly or remaining
        )
        binding_record = None
        if raw is not None and self.has_source_manifest:
            input_id = projection_input_id(raw)
            source, binding = self.source_by_input[input_id]
            binding_record = {
                "projection_input_id": input_id,
                "capture_task_id": source.task_id,
                "source_artifact_id": source.artifact_id,
                "source_horizon": source.horizon.value,
                "source_scoring_format": source.source_scoring_format,
                "point_basis": source.point_basis.value,
                "host_scoring_compatibility": (
                    source.host_scoring_compatibility.value
                ),
                "input_presence": binding.presence.value,
            }
        return {
            "provider": observation.provider,
            "status": observation.status.value,
            "projected_fantasy_points": observation.projected_fantasy_points,
            "weekly_value_origin": (
                None if lineage.origin is None else lineage.origin.value
            ),
            "captured_at": _iso(lineage.captured_at),
            "source_published_at": (
                None
                if lineage.source_published_at is None
                else _iso(lineage.source_published_at)
            ),
            "source_binding": binding_record,
        }


def prepare_market_evidence(bundle):
    return _MarketEvidenceIndex(bundle)


def projection_lineage_summary(bundle):
    """Return bounded bundle-level lineage; option rows carry exact artifacts."""

    manifest = bundle.projection_source_manifest
    if manifest is None:
        rows = bundle.projection_evidence
        capture_times = tuple(row.captured_at for row in rows)
        return {
            "projection_source_manifest_id": None,
            "ensemble_config_id": None,
            "provider_names": list(
                bundle.independent_power_disclosure.provider_names
            ),
            "source_artifact_count": 0,
            "capture_attempt_count": 0,
            "capture_status_counts": {},
            "horizons": sorted(
                {
                    "rest_of_season"
                    if isinstance(row, RemainingSeasonProjection)
                    else "weekly"
                    for row in rows
                }
            ),
            "point_bases": [],
            "host_scoring_compatibility": [],
            "captured_at_start": _iso(min(capture_times)),
            "captured_at_end": _iso(max(capture_times)),
            "normalized_evidence_row_count": len(rows),
            "detail_scope": (
                "Independent timing rows show normalized provider evidence and "
                "timestamps. Source artifact and capture-task IDs are not retained."
            ),
        }
    sources = manifest.sources
    attempts = manifest.attempts
    return {
        "projection_source_manifest_id": manifest.manifest_id,
        "ensemble_config_id": bundle.ensemble_config.config_id,
        "provider_names": sorted(
            row.provider for row in bundle.ensemble_config.provider_weights
        ),
        "source_artifact_count": len(sources),
        "capture_attempt_count": len(attempts),
        "capture_status_counts": {
            status: sum(row.status.value == status for row in attempts)
            for status in sorted({row.status.value for row in attempts})
        },
        "horizons": sorted({row.horizon.value for row in sources}),
        "point_bases": sorted({row.point_basis.value for row in sources}),
        "host_scoring_compatibility": sorted(
            {row.host_scoring_compatibility.value for row in sources}
        ),
        "captured_at_start": _iso(min(row.captured_at for row in sources)),
        "captured_at_end": _iso(max(row.captured_at for row in sources)),
        "detail_scope": (
            "Exact input, task, and artifact IDs are attached only to the "
            "players in each simulated option."
        ),
    }


def market_pattern(bundle, swap, effective_week, *, evidence_index=None):
    evidence = evidence_index or prepare_market_evidence(bundle)
    incoming = _projection_position(
        bundle, swap.counterparty_player_id, effective_week, evidence
    )
    outgoing = _projection_position(
        bundle, swap.primary_player_id, effective_week, evidence
    )
    primary_actions = []
    if incoming["projection_band"] == "low":
        primary_actions.append("primary_buys_projected_low")
    if outgoing["projection_band"] == "high":
        primary_actions.append("primary_sells_projected_high")
    partner_actions = []
    if outgoing["projection_band"] == "high":
        partner_actions.append("partner_buys_projected_high")
    if incoming["projection_band"] == "low":
        partner_actions.append("partner_sells_projected_low")
    return {
        "basis": "within_player_remaining_active_week_projection_percentile",
        "not_market_price_or_future_ecr": True,
        "primary_receives": incoming,
        "primary_sends": outgoing,
        "primary_pattern": primary_actions
        or ["no_clear_projected_high_low_pattern"],
        "partner_pattern": partner_actions
        or ["no_clear_projected_high_low_pattern"],
        "summary": market_summary(primary_actions, partner_actions),
    }


def market_summary(primary_actions, partner_actions):
    primary = (
        "You buy at a projected low and sell at a projected high"
        if len(primary_actions) == 2
        else "You buy at a projected low"
        if primary_actions == ["primary_buys_projected_low"]
        else "You sell at a projected high"
        if primary_actions == ["primary_sells_projected_high"]
        else "Your side has no clear projected high/low pattern"
    )
    partner = (
        "the partner buys at a projected high and sells at a projected low"
        if len(partner_actions) == 2
        else "the partner buys at a projected high"
        if partner_actions == ["partner_buys_projected_high"]
        else "the partner sells at a projected low"
        if partner_actions == ["partner_sells_projected_low"]
        else "the partner has no complete buy-high/sell-low projection pattern"
    )
    return f"{primary}; {partner}."


def _projection_position(bundle, player_id, week, evidence):
    player_rows = [
        row
        for row in bundle.projections
        if row.canonical_player_id == player_id
        and row.week >= week
    ]
    rows = [
        row
        for row in player_rows
        if row.status is not ProjectionStatus.BYE
        and row.projected_fantasy_points is not None
    ]
    by_week = {row.week: row.projected_fantasy_points for row in rows}
    target_row = next((row for row in player_rows if row.week == week), None)
    target = by_week.get(week)
    values = sorted(by_week.values())
    percentile = None if target is None else _value_percentile(target, values)
    band = (
        "unavailable"
        if percentile is None
        else "low"
        if percentile <= 0.35
        else "high"
        if percentile >= 0.65
        else "middle"
    )
    return {
        "player_id": player_id,
        "player_name": bundle.player_names[player_id],
        "effective_week": week,
        "projected_points": target,
        "remaining_active_week_mean": mean(values) if values else None,
        "within_player_percentile": percentile,
        "projection_band": band,
        "projection_lineage": (
            []
            if target_row is None
            else [
                evidence.provider_record(target_row, observation)
                for observation in target_row.provider_observations
            ]
        ),
    }


def _value_percentile(value, values):
    if len(values) <= 1:
        return 0.5
    less = sum(candidate < value for candidate in values)
    equal = sum(candidate == value for candidate in values)
    return (less + (equal - 1) / 2) / (len(values) - 1)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


__all__ = (
    "market_pattern",
    "market_summary",
    "prepare_market_evidence",
    "projection_lineage_summary",
)
