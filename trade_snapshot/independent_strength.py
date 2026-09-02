"""Transparent, provider-neutral roster strength derived from remaining points."""

from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
import json
from math import fsum, isfinite

from ._scenario_random import content_id
from .ensemble import EnsembleProjection
from .projections import ProjectionStatus
from .scenario_config import PlayerEligibility
from .strength import StrengthModel
from .strength_calibration import (
    CalibrationMetadata,
    CalibrationStatus,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
)
from .trade_space import TeamRoster


INDEPENDENT_STRENGTH_POLICY = {
    "depth_role_weight": 0.15,
    "normalization": "strongest-current-roster-equals-100",
    "residual_roster_weight": 0.02,
    "starter_role_weight": 1.0,
    "value": "remaining-regular-season-ensemble-points",
    "version": 1,
}
INDEPENDENT_STRENGTH_POLICY_ID = content_id(
    "independent-strength-policy", INDEPENDENT_STRENGTH_POLICY
)


def build_independent_strength_model(
    *,
    snapshot_id: str,
    season: int,
    scoring_profile_id: str,
    role_definitions: Iterable[RoleDefinition],
    projections: Iterable[EnsembleProjection],
    eligibilities: Iterable[PlayerEligibility],
    rosters: Iterable[TeamRoster],
    captured_at: datetime,
) -> StrengthModel:
    """Score every player with one documented local remaining-points policy."""

    roles = tuple(role_definitions)
    rows = tuple(projections)
    eligibility_rows = tuple(eligibilities)
    roster_rows = tuple(rosters)
    if not roles or any(not isinstance(row, RoleDefinition) for row in roles):
        raise ValueError("role_definitions must contain RoleDefinition values")
    if not rows or any(not isinstance(row, EnsembleProjection) for row in rows):
        raise ValueError("projections must contain EnsembleProjection values")
    if not eligibility_rows or any(
        not isinstance(row, PlayerEligibility) for row in eligibility_rows
    ):
        raise ValueError("eligibilities must contain PlayerEligibility values")
    if len(roster_rows) < 2 or any(
        not isinstance(row, TeamRoster) for row in roster_rows
    ):
        raise ValueError("rosters must contain at least two TeamRoster values")

    eligibilities_by_player = {
        row.canonical_player_id: row for row in eligibility_rows
    }
    if len(eligibilities_by_player) != len(eligibility_rows):
        raise ValueError("eligibilities contain a duplicate player")
    projected_by_player: dict[str, list[EnsembleProjection]] = {}
    for row in rows:
        if (
            row.snapshot_id != snapshot_id
            or row.season != season
            or row.scoring_profile_id != scoring_profile_id
        ):
            raise ValueError("projection identity does not match independent strength")
        projected_by_player.setdefault(row.canonical_player_id, []).append(row)
    if set(projected_by_player) != set(eligibilities_by_player):
        raise ValueError("projection and eligibility player universes differ")

    strengths = tuple(
        _player_strength(
            player_id,
            projected_by_player[player_id],
            eligibilities_by_player[player_id],
            roles,
        )
        for player_id in sorted(projected_by_player)
    )
    calibration = _independent_metadata(captured_at, rows)
    provisional = StrengthModel(
        roles,
        strengths,
        1.0,
        snapshot_id=snapshot_id,
        season=season,
        scoring_profile_id=scoring_profile_id,
        calibration=calibration,
    )
    denominator = max(
        provisional.score_roster(row.player_ids).absolute_score
        for row in roster_rows
    )
    if not isfinite(denominator) or denominator <= 0:
        raise ValueError("independent baseline league strength must be positive")
    return StrengthModel(
        roles,
        strengths,
        denominator,
        snapshot_id=snapshot_id,
        season=season,
        scoring_profile_id=scoring_profile_id,
        calibration=calibration,
    )


def _player_strength(player_id, projections, eligibility, roles) -> PlayerStrength:
    remaining_points = fsum(
        row.projected_fantasy_points
        for row in projections
        if row.status is ProjectionStatus.OBSERVED
    )
    eligible = frozenset(eligibility.eligible_slots)
    assignments = {
        role.role_id: remaining_points * (
            INDEPENDENT_STRENGTH_POLICY["starter_role_weight"]
            if role.kind is RoleKind.STARTER
            else INDEPENDENT_STRENGTH_POLICY["depth_role_weight"]
        )
        for role in roles
        if eligible.intersection(role.eligible_positions)
    }
    return PlayerStrength(
        player_id,
        remaining_points * INDEPENDENT_STRENGTH_POLICY["residual_roster_weight"],
        eligible,
        assignments,
    )


def _independent_metadata(captured_at, projections) -> CalibrationMetadata:
    provider_names = tuple(
        sorted(
            {
                observation.provider
                for row in projections
                for observation in row.provider_observations
            }
        )
    )
    policy_bytes = json.dumps(
        INDEPENDENT_STRENGTH_POLICY,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence_bytes = json.dumps(
        {"providers": provider_names, "policy_id": INDEPENDENT_STRENGTH_POLICY_ID},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CalibrationMetadata(
        analyzer_bundle_url="https://fantasy.espn.com/football/players/projections",
        analyzer_bundle_sha256=sha256(policy_bytes).hexdigest(),
        response_schema_sha256=sha256(evidence_bytes).hexdigest(),
        captured_at=captured_at,
        status=CalibrationStatus.UNVALIDATED,
    )


__all__ = (
    "INDEPENDENT_STRENGTH_POLICY",
    "INDEPENDENT_STRENGTH_POLICY_ID",
    "build_independent_strength_model",
)
