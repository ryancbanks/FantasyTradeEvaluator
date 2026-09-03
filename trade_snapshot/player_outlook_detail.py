"""Lazy exact-player detail view for the local Player Lab."""

from collections.abc import Mapping

from .ecr import EcrPeriod
from .engine_bundle import EngineBundle
from .player_outlook import (
    _SCHEMA_VERSION,
    _ecr_detail,
    _outside_projected_record,
    _player_record,
    _require_strict_json,
)
from .player_outlook_lazy import (
    _catalog_player,
    _require_context,
    build_player_outlook_catalog_from_bundle,
)
from .player_profile_outlook import outside_calculation_record, profile_record


def build_player_outlook_detail_from_bundle(
    bundle: EngineBundle,
    player_id: str,
    *,
    catalog: Mapping[str, object] | None = None,
    context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Materialize detail for one exact canonical player ID only."""

    if not isinstance(player_id, str) or not player_id:
        raise ValueError("player_id must be a non-empty string")
    context = _require_context(bundle, context)
    profile_snapshot = bundle.player_profiles
    profile = (
        None
        if profile_snapshot is None
        else profile_snapshot.players_by_id.get(player_id)
    )
    rows = context["projections"].get(player_id)
    if rows is not None:
        player = _player_record(
            bundle,
            player_id,
            rows,
            context["providers"],
            context["evidence"],
            context["owners"],
            context["eligibilities"],
            context["waiver_players"],
            context["ecr_by_period"],
        )
    elif (
        (lab_snapshot := bundle.player_lab_projections) is not None
        and player_id in lab_snapshot.player_names
    ):
        rows = context["lab_projections"].get(player_id, ())
        player = _outside_projected_record(
            player_id,
            (
                profile.display_name
                if profile is not None
                else lab_snapshot.player_names[player_id]
            ),
            lab_snapshot.player_positions[player_id],
            lab_snapshot.player_nfl_team_ids[player_id],
            (
                profile.fantasy_positions
                if profile is not None
                else (lab_snapshot.player_positions[player_id],)
            ),
            rows,
            context["weeks"],
            context["providers"],
            lab_snapshot.provider_provenance_by_name,
            _ecr_detail(context["ecr_by_period"], EcrPeriod.WEEKLY, player_id),
            _ecr_detail(
                context["ecr_by_period"], EcrPeriod.REST_OF_SEASON, player_id
            ),
        )
    elif profile is not None:
        player = outside_calculation_record(
            profile,
            _ecr_detail(context["ecr_by_period"], EcrPeriod.WEEKLY, player_id),
            _ecr_detail(
                context["ecr_by_period"], EcrPeriod.REST_OF_SEASON, player_id
            ),
        )
    else:
        raise KeyError(player_id)
    player["profile"] = (
        None
        if profile is None
        else profile_record(profile, profile_snapshot, context["scoring_mode"])
    )
    summary = _catalog_player(catalog, player_id)
    if summary is None:
        calculated_catalog = build_player_outlook_catalog_from_bundle(
            bundle, context=context
        )
        summary = _catalog_player(calculated_catalog, player_id)
    assert summary is not None
    for field in (
        "projection_overall_rank",
        "projection_position_rank",
        "overall_rank",
        "overall_rank_basis",
    ):
        player[field] = summary.get(field)
    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "snapshot_id": bundle.state.snapshot_id,
        "scoring_mode": context["scoring_mode"],
        "view": "player_detail",
        "player": player,
    }
    _require_strict_json(result, "player outlook detail")
    return result


__all__ = ("build_player_outlook_detail_from_bundle",)
