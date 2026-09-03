"""Bounded Player Lab catalog and exact-player detail read models."""

from collections.abc import Mapping
from math import fsum
from types import MappingProxyType

from .ecr import EcrPeriod
from .engine_bundle import EngineBundle
from .player_outlook import (
    _PROJECTION_CATALOG_NOTICE,
    _WAIVER_SCOPE_NOTICE,
    _EvidenceIndex,
    _average,
    _catalog_player_record,
    _ecr_detail,
    _ecr_rankings,
    _outlook_header,
    _outlook_scoring_mode,
    _owners,
    _player_nfl_team,
    _profile_snapshot_record,
    _projection_groups,
    _provider_names,
    _require_strict_json,
    _week_record,
)
from .player_profile_outlook import (
    PROFILE_SCOPE_NOTICE,
    assign_player_ranks,
    profile_catalog_record,
)
from .projections import ProjectionStatus


def build_player_outlook_catalog_from_bundle(
    bundle: EngineBundle,
    *,
    context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the compact Player Lab catalog without materializing player detail."""

    context = _require_context(bundle, context)
    by_player = {
        player_id: _calculation_catalog_record(
            bundle,
            player_id,
            context["projections"][player_id],
            context["providers"],
            context["evidence"],
            context["owners"],
            context["eligibilities"],
            context["waiver_players"],
            context["ecr_by_period"],
        )
        for player_id in context["projections"]
    }
    lab_snapshot = bundle.player_lab_projections
    for player_id in context["lab_player_ids"]:
        rows = context["lab_projections"].get(player_id, ())
        by_player[player_id] = _retained_catalog_record(
            player_id,
            lab_snapshot.player_names[player_id],
            lab_snapshot.player_positions[player_id],
            lab_snapshot.player_nfl_team_ids[player_id],
            (lab_snapshot.player_positions[player_id],),
            rows,
            context["weeks"],
            context["providers"],
            context["ecr_by_period"],
        )

    profiles = bundle.player_profiles
    if profiles is not None:
        for profile in profiles.players:
            row = by_player.get(profile.canonical_player_id)
            if row is None:
                row = _profile_only_catalog_record(
                    profile,
                    context["ecr_by_period"],
                )
                by_player[profile.canonical_player_id] = row
            elif profile.canonical_player_id in context["lab_player_ids"]:
                row["name"] = profile.display_name
                row["eligible_slots"] = list(profile.fantasy_positions)
            row["profile"] = profile_catalog_record(
                profile, profiles, context["scoring_mode"]
            )
        for row in by_player.values():
            row.setdefault("profile", None)
    else:
        for row in by_player.values():
            row["profile"] = None

    players = sorted(
        by_player.values(),
        key=lambda row: (row["name"].casefold(), row["player_id"]),
    )
    assign_player_ranks(players)
    players = [_catalog_player_record(row) for row in players]
    scope_notice = (
        PROFILE_SCOPE_NOTICE
        if profiles is not None
        else _PROJECTION_CATALOG_NOTICE
        if context["lab_player_ids"]
        else _WAIVER_SCOPE_NOTICE
    )
    result = _outlook_header(
        bundle,
        context["scoring_mode"],
        context["weeks"],
        context["providers"],
        context["evidence"],
        profiles,
        context["lab_player_ids"],
        scope_notice,
        (
            _profile_snapshot_record(profiles, context["lab_player_ids"])
            if profiles is not None
            else None
        ),
    )
    result["view"] = "catalog"
    result["players"] = players
    _require_strict_json(result, "player outlook catalog")
    return result


def build_player_outlook_read_context(bundle):
    """Build one immutable, reusable index over an immutable engine bundle."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    weeks = bundle.state.remaining_regular_season_weeks
    projections = _projection_groups(bundle.projections, weeks)
    lab_snapshot = bundle.player_lab_projections
    lab_projections = _projection_groups(
        () if lab_snapshot is None else lab_snapshot.projections,
        weeks,
        require_nonempty=False,
        require_complete=False,
    )
    providers = _provider_names({**projections, **lab_projections})
    return MappingProxyType({
        "bundle_id": bundle.bundle_id,
        "weeks": weeks,
        "scoring_mode": _outlook_scoring_mode(bundle.scoring_profile.settings),
        "projections": _freeze_groups(projections),
        "lab_projections": _freeze_groups(lab_projections),
        "lab_player_ids": () if lab_snapshot is None else lab_snapshot.player_ids,
        "providers": providers,
        "evidence": _EvidenceIndex(bundle.projection_evidence),
        "owners": _freeze_owner_map(_owners(bundle)),
        "eligibilities": MappingProxyType({
            row.canonical_player_id: row.eligible_slots
            for row in bundle.eligibilities
        }),
        "waiver_players": MappingProxyType({
            row.canonical_player_id: row for row in bundle.waiver_pool.players
        }),
        "ecr_by_period": _freeze_nested_mapping(_ecr_rankings(bundle)),
    })


def _require_context(bundle, context):
    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    if context is None:
        return build_player_outlook_read_context(bundle)
    if not isinstance(context, Mapping) or context.get("bundle_id") != bundle.bundle_id:
        raise ValueError("Player Lab read context does not match the bundle")
    return context


def _freeze_groups(groups):
    return MappingProxyType(
        {
            player_id: tuple(rows)
            for player_id, rows in sorted(groups.items())
        }
    )


def _freeze_owner_map(owners):
    return MappingProxyType(
        {
            player_id: MappingProxyType(dict(owner))
            for player_id, owner in sorted(owners.items())
        }
    )


def _freeze_nested_mapping(values):
    return MappingProxyType(
        {
            key: MappingProxyType(dict(rows))
            for key, rows in values.items()
        }
    )


def _calculation_catalog_record(
    bundle,
    player_id,
    rows,
    providers,
    evidence,
    owners,
    eligibilities,
    waiver_players,
    ecr_by_period,
):
    position = rows[0].position
    eligible_slots = eligibilities.get(player_id)
    if eligible_slots is None or position not in eligible_slots:
        raise ValueError(f"player {player_id!r} position conflicts with eligibility")
    owner = owners.get(player_id)
    waiver = waiver_players.get(player_id)
    if (owner is None) == (waiver is None):
        raise ValueError("calculation player must be rostered or in the waiver pool")
    if waiver is not None and waiver.position != position:
        raise ValueError(f"player {player_id!r} has inconsistent positions")
    coverage = [
        _week_record(player_id, row, providers, evidence, rows) for row in rows
    ]
    return _projection_catalog_record(
        player_id=player_id,
        name=bundle.player_names[player_id],
        position=position,
        eligible_slots=eligible_slots,
        nfl_team_id=_player_nfl_team(player_id, rows, evidence, waiver_players),
        owner=None if owner is None else dict(owner),
        availability="rostered" if owner is not None else "waiver_pool",
        rows=rows,
        weekly_ecr=_ecr_detail(ecr_by_period, EcrPeriod.WEEKLY, player_id),
        rest_of_season_ecr=_ecr_detail(
            ecr_by_period, EcrPeriod.REST_OF_SEASON, player_id
        ),
        provider_complete_week_count=sum(
            bool(providers) and row["usable_source_count"] == len(providers)
            for row in coverage
        ),
        all_direct_week_count=sum(
            bool(providers) and row["direct_source_count"] == len(providers)
            for row in coverage
        ),
    )


def _retained_catalog_record(
    player_id,
    name,
    position,
    nfl_team_id,
    eligible_slots,
    rows,
    remaining_weeks,
    providers,
    ecr_by_period,
):
    usable = {ProjectionStatus.OBSERVED, ProjectionStatus.BYE}
    return _projection_catalog_record(
        player_id=player_id,
        name=name,
        position=position,
        eligible_slots=eligible_slots,
        nfl_team_id=nfl_team_id,
        owner=None,
        availability="outside_calculation_pool",
        rows=rows,
        weekly_ecr=_ecr_detail(ecr_by_period, EcrPeriod.WEEKLY, player_id),
        rest_of_season_ecr=_ecr_detail(
            ecr_by_period, EcrPeriod.REST_OF_SEASON, player_id
        ),
        provider_complete_week_count=sum(
            all(value.status in usable for value in row.provider_observations)
            and len(row.provider_observations) == len(providers)
            for row in rows
        ),
        all_direct_week_count=0,
        complete_projection=len(rows) == len(remaining_weeks),
        total_week_count=len(remaining_weeks),
    )


def _projection_catalog_record(
    *,
    player_id,
    name,
    position,
    eligible_slots,
    nfl_team_id,
    owner,
    availability,
    rows,
    weekly_ecr,
    rest_of_season_ecr,
    provider_complete_week_count,
    all_direct_week_count,
    complete_projection=True,
    total_week_count=None,
):
    points = [
        row.projected_fantasy_points
        for row in rows
        if row.projected_fantasy_points is not None
    ]
    disagreements = [
        row.between_provider_stddev
        for row in rows
        if row.between_provider_stddev is not None
    ]
    return {
        "player_id": player_id,
        "name": name,
        "position": position,
        "eligible_slots": list(eligible_slots),
        "nfl_team_id": nfl_team_id,
        "owner": owner,
        "availability": availability,
        "weekly_ecr": _catalog_ecr(weekly_ecr),
        "rest_of_season_ecr": _catalog_ecr(rest_of_season_ecr),
        "remaining_projected_points": fsum(points) if complete_projection else None,
        "average_weekly_points": _average(points) if complete_projection else None,
        "average_provider_disagreement": (
            _average(disagreements) if complete_projection else None
        ),
        "provider_complete_week_count": provider_complete_week_count,
        "all_direct_week_count": all_direct_week_count,
        "total_week_count": (
            len(rows) if total_week_count is None else total_week_count
        ),
    }


def _profile_only_catalog_record(profile, ecr_by_period):
    return {
        "player_id": profile.canonical_player_id,
        "name": profile.display_name,
        "position": profile.position,
        "eligible_slots": list(profile.fantasy_positions),
        "nfl_team_id": profile.nfl_team_id,
        "owner": None,
        "availability": "outside_calculation_pool",
        "weekly_ecr": _catalog_ecr(
            _ecr_detail(
                ecr_by_period, EcrPeriod.WEEKLY, profile.canonical_player_id
            )
        ),
        "rest_of_season_ecr": _catalog_ecr(
            _ecr_detail(
                ecr_by_period,
                EcrPeriod.REST_OF_SEASON,
                profile.canonical_player_id,
            )
        ),
        "remaining_projected_points": None,
        "average_weekly_points": None,
        "average_provider_disagreement": None,
        "provider_complete_week_count": 0,
        "all_direct_week_count": 0,
        "total_week_count": 0,
    }


def _catalog_ecr(value):
    if value is None:
        return None
    return {key: item for key, item in value.items() if key in {"rank", "position_rank"}}


def _catalog_player(catalog, player_id):
    if catalog is None:
        return None
    players = catalog.get("players") if isinstance(catalog, Mapping) else None
    if not isinstance(players, list):
        raise ValueError("player outlook catalog players must be a list")
    return next(
        (
            row
            for row in players
            if isinstance(row, Mapping) and row.get("player_id") == player_id
        ),
        None,
    )


__all__ = (
    "build_player_outlook_catalog_from_bundle",
    "build_player_outlook_read_context",
)
