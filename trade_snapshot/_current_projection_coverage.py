"""Reconcile the active Sleeper catalog with retained current projections."""

from collections import Counter
from collections.abc import Iterable

from .engine_bundle import EngineBundle
from .positions import normalize_player_position
from .projections import ProjectionStatus


DEFAULT_CURRENT_PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")
_SLEEPER_DATASET = "sleeper_active_players"
_SYNTHETIC_MISSING_PREFIX = "not-published:"


def build_current_player_projection_coverage(
    bundle: EngineBundle,
    *,
    positions: Iterable[str] | None = None,
) -> dict[str, object]:
    """Audit projection coverage against an independently captured player list.

    A Sleeper catalog row, a canonical identity match, and an observed numeric
    projection are deliberately separate states.  ``positions`` makes the
    fantasy-player scope explicit and defaults to offensive players, kickers,
    and team defenses rather than every person employed by an NFL club.
    """

    if not isinstance(bundle, EngineBundle):
        raise ValueError("current projection coverage requires an EngineBundle")
    configured_positions = _configured_positions(positions)
    profiles = bundle.player_profiles
    sleeper_sources = tuple(
        row
        for row in (() if profiles is None else profiles.provenance)
        if row.provider == "sleeper" and row.dataset == _SLEEPER_DATASET
    )
    source_status = (
        sleeper_sources[0].status if len(sleeper_sources) == 1 else "unavailable"
    )
    sleeper_profiles = tuple(
        row
        for row in (() if profiles is None else profiles.players)
        if any(reference.provider == "sleeper" for reference in row.provider_references)
    )
    active_profiles = tuple(
        (row, _profile_position(row))
        for row in sleeper_profiles
        if row.active is True
    )
    reference_profiles = tuple(
        row
        for row, position in active_profiles
        if position in configured_positions
    )
    positions_by_player = {
        row.canonical_player_id: position for row, position in active_profiles
    }
    unsupported = Counter(
        position
        for _, position in active_profiles
        if position not in configured_positions
    )

    projection_rows = [*bundle.projections]
    if bundle.player_lab_projections is not None:
        projection_rows.extend(bundle.player_lab_projections.projections)
    rows_by_player_week = {
        (row.canonical_player_id, row.week): row for row in projection_rows
    }
    providers = (
        tuple(row.provider for row in bundle.ensemble_config.provider_weights)
        if bundle.ensemble_config is not None
        else tuple(
            sorted(
                {
                    observation.provider
                    for row in projection_rows
                    for observation in row.provider_observations
                }
            )
        )
    )
    weeks = tuple(bundle.state.remaining_regular_season_weeks)
    horizons = (
        ("current_week", (bundle.state.first_remaining_week,)),
        ("remaining_season", weeks),
    )
    coverage_rows = []
    position_groups = (
        ("ALL", reference_profiles),
        *(
            (
                position,
                tuple(
                    row
                    for row in reference_profiles
                    if positions_by_player[row.canonical_player_id] == position
                ),
            )
            for position in configured_positions
        ),
    )
    for position, selected in position_groups:
        for provider in (*providers, "ensemble"):
            for horizon, horizon_weeks in horizons:
                coverage_rows.append(
                    _coverage_row(
                        selected,
                        rows_by_player_week,
                        position=position,
                        provider=provider,
                        horizon=horizon,
                        weeks=horizon_weeks,
                    )
                )

    all_remaining = _find_row(
        coverage_rows, position="ALL", provider="ensemble", horizon="remaining_season"
    )
    provider_rows = tuple(
        row
        for row in coverage_rows
        if row["position"] == "ALL" and row["provider"] != "ensemble"
    )
    identity_issues = (
        () if profiles is None else tuple(profiles.materialization_issues)
    )
    identity_complete = (
        source_status == "observed"
        and bool(reference_profiles)
        and not identity_issues
    )
    ensemble_complete = (
        all_remaining["missing_count"] == 0
        and all_remaining["unmatched_count"] == 0
    )
    providers_complete = bool(provider_rows) and all(
        row["missing_count"] == 0 and row["unmatched_count"] == 0
        for row in provider_rows
    )
    complete = identity_complete and ensemble_complete and providers_complete
    rostered = {
        player_id for roster in bundle.rosters for player_id in roster.player_ids
    }
    roster_audit = _rostered_current_audit(
        bundle,
        rostered,
        rows_by_player_week,
        providers,
        frozenset(row.canonical_player_id for row in reference_profiles),
        {} if profiles is None else profiles.players_by_id,
    )
    limitations = []
    if source_status != "observed":
        limitations.append("The Sleeper active-player reference was not observed.")
    if not reference_profiles:
        limitations.append("The declared fantasy-position scope has no reference identities.")
    if identity_issues:
        limitations.append(
            "Some public identity rows could not be reconciled safely; their activity and "
            "position cannot be inferred."
        )
    if all_remaining["missing_count"]:
        limitations.append(
            "At least one in-scope active reference identity lacks a complete retained "
            "remaining-season ensemble projection."
        )
    if not providers_complete:
        limitations.append(
            "At least one configured provider/horizon has missing or unmatched reference identities."
        )

    return {
        "schema_version": 1,
        "status": (
            "complete"
            if complete
            else "unavailable"
            if source_status != "observed"
            else "incomplete"
        ),
        "completeness_claim": (
            "complete_for_declared_scope" if complete else "not_complete"
        ),
        "configured_positions": list(configured_positions),
        "reference": {
            "provider": "sleeper",
            "dataset": _SLEEPER_DATASET,
            "status": source_status,
            "captured_at": (
                sleeper_sources[0].captured_at.isoformat(timespec="microseconds")
                if len(sleeper_sources) == 1
                else None
            ),
            "reconciled_active_rows": len(active_profiles),
            "identity_issue_count": len(identity_issues),
            "identity_issues_by_provider": dict(
                sorted(Counter(row.provider for row in identity_issues).items())
            ),
            "identity_reconciliation_complete": identity_complete,
        },
        "counts": {
            "reference_count": len(reference_profiles),
            "matched_count": all_remaining["matched_count"],
            "projected_count": all_remaining["projected_count"],
            "covered_count": all_remaining["covered_count"],
            "partial_count": all_remaining["partial_count"],
            "missing_count": all_remaining["missing_count"],
            "unmatched_count": all_remaining["unmatched_count"],
            "unsupported_count": sum(unsupported.values()),
        },
        "unsupported_by_position": dict(sorted(unsupported.items())),
        "coverage_rows": coverage_rows,
        **roster_audit,
        "counting_policy": (
            "Sleeper reference identities and projection states are counted separately. "
            "A fetched, matched, or not-published row is never counted as a numeric "
            "projection; a verified bye is covered but not projected."
        ),
        "limitations": limitations,
    }


def _coverage_row(
    profiles,
    rows_by_player_week,
    *,
    position,
    provider,
    horizon,
    weeks,
):
    matched = projected = covered = partial = bye_only = 0
    for profile in profiles:
        cells = tuple(
            _cell(rows_by_player_week.get((profile.canonical_player_id, week)), provider)
            for week in weeks
        )
        matched += any(cell[0] for cell in cells)
        complete = bool(cells) and all(cell[1] for cell in cells)
        observed = any(cell[2] for cell in cells)
        covered += complete
        projected += complete and observed
        bye_only += complete and not observed
        partial += not complete and any(cell[1] for cell in cells)
    reference_count = len(profiles)
    return {
        "position": position,
        "provider": provider,
        "horizon": horizon,
        "reference_count": reference_count,
        "matched_count": matched,
        "projected_count": projected,
        "covered_count": covered,
        "bye_only_count": bye_only,
        "partial_count": partial,
        "missing_count": reference_count - covered,
        "unmatched_count": reference_count - matched,
    }


def _cell(row, provider):
    if row is None:
        return False, False, False
    if provider == "ensemble":
        observed = row.status is ProjectionStatus.OBSERVED
        return True, observed or row.status is ProjectionStatus.BYE, observed
    observation = next(
        (item for item in row.provider_observations if item.provider == provider),
        None,
    )
    if observation is None:
        return False, False, False
    matched = not observation.provider_player_id.startswith(
        _SYNTHETIC_MISSING_PREFIX
    )
    observed = observation.status is ProjectionStatus.OBSERVED
    return matched, observed or observation.status is ProjectionStatus.BYE, observed


def _rostered_current_audit(
    bundle,
    rostered,
    rows_by_player_week,
    providers,
    reference_ids,
    profiles,
):
    current_week = bundle.state.first_remaining_week
    missing_ensemble = []
    missing_provider = []
    for player_id in sorted(rostered):
        row = rows_by_player_week.get((player_id, current_week))
        record = _roster_player_record(
            bundle, profiles.get(player_id), player_id, row
        )
        if row is None or row.status not in {
            ProjectionStatus.OBSERVED,
            ProjectionStatus.BYE,
        }:
            missing_ensemble.append(record)
        missing = tuple(
            provider
            for provider in providers
            if not _cell(row, provider)[1]
        )
        if missing:
            missing_provider.append({**record, "missing_providers": list(missing)})
    return {
        "current_week": current_week,
        "rostered_player_count": len(rostered),
        "rostered_missing_current_projection_count": len(missing_ensemble),
        "rostered_players_missing_current_projection": missing_ensemble,
        "rostered_missing_provider_current_projection_count": len(missing_provider),
        "rostered_players_missing_provider_current_projection": missing_provider,
        "rostered_outside_reference_scope_count": len(rostered - reference_ids),
    }


def _roster_player_record(bundle, profile, player_id, projection):
    return {
        "canonical_player_id": player_id,
        "display_name": bundle.player_names.get(
            player_id,
            profile.display_name if profile is not None else player_id,
        ),
        "position": (
            _profile_position(profile)
            if profile is not None
            else projection.position
            if projection is not None
            else "UNKNOWN"
        ),
    }


def _configured_positions(values):
    source = DEFAULT_CURRENT_PROJECTION_POSITIONS if values is None else values
    if isinstance(source, (str, bytes)):
        raise ValueError("projection coverage positions must be an iterable")
    try:
        normalized = tuple(
            normalize_player_position(value, require_supported=True)
            for value in source
        )
    except TypeError:
        raise ValueError("projection coverage positions must be an iterable") from None
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("projection coverage positions must be non-empty and unique")
    return tuple(sorted(normalized))


def _profile_position(profile):
    if profile.position is None:
        return "UNKNOWN"
    try:
        return normalize_player_position(profile.position)
    except ValueError:
        return "UNKNOWN"


def _find_row(rows, *, position, provider, horizon):
    return next(
        row
        for row in rows
        if (row["position"], row["provider"], row["horizon"])
        == (position, provider, horizon)
    )


__all__ = (
    "DEFAULT_CURRENT_PROJECTION_POSITIONS",
    "build_current_player_projection_coverage",
)
