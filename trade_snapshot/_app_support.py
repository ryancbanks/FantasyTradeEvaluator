"""Small validation and presentation helpers for the localhost app service."""

from collections.abc import Mapping
from pathlib import Path
import os
import re
import sys

from ._scenario_random import content_id
from .engine_bundle import EngineBundle
from .league_search import LeagueSearchOutcome
from .positions import CANONICAL_PLAYER_POSITIONS
from .projection_io import projection_to_record
from .season import SeasonProjection
from .surrogate_disclosure import SURROGATE_NOTICE, SURROGATE_QUALITY_GATE
from .workbook_model import WorkbookSource
from .workbook_model import team_outlook_rows, workbook_trade_rows


BUNDLE_ID_PATTERN = re.compile(r"^engine_[0-9a-f]{64}$")


def default_data_directory() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "FantasyTradeEvaluator"


def bundle_summary(bundle: EngineBundle) -> dict[str, object]:
    evidence = bundle.methodology_evidence
    exact = bundle.methodology_attestation is not None
    roster_by_team = {row.team_id: row for row in bundle.rosters}
    player_positions = {
        player_id: tuple(sorted(
            player.eligible_positions.intersection(CANONICAL_PLAYER_POSITIONS)
        ))
        for player_id, player in bundle.strength_model.players.items()
    }
    teams = []
    for team in bundle.state.teams:
        players = [
            {
                "player_id": player_id,
                "name": bundle.player_names[player_id],
                "positions": list(player_positions[player_id]),
            }
            for player_id in roster_by_team[team.team_id].player_ids
        ]
        players.sort(key=lambda row: (row["name"].casefold(), row["player_id"]))
        teams.append({"team_id": team.team_id, "name": team.name, "players": players})
    return {
        "bundle_id": bundle.bundle_id,
        "status": "ready",
        "season": bundle.state.season,
        "week": bundle.state.first_remaining_week,
        "team_count": len(bundle.state.teams),
        "teams": teams,
        "positions": sorted({
            position for positions in player_positions.values() for position in positions
        }),
        "calibration_status": bundle.strength_model.calibration.status.value,
        "power_engine_mode": bundle.methodology_mode,
        "power_engine_notice": (
            "Exact within the attested balanced-package scope; other shapes are extrapolated."
            if exact
            else SURROGATE_NOTICE
        ),
        "methodology": {
            "mode": bundle.methodology_mode,
            "attestation_id": (
                None if not exact else bundle.methodology_attestation.attestation_id
            ),
            "surrogate_disclosure_id": (
                None if exact else bundle.surrogate_disclosure.disclosure_id
            ),
            "formula_id": evidence.formula_id,
            "fingerprint_id": evidence.methodology_fingerprint.fingerprint_id,
            "formula_action": evidence.formula_decision.action.value,
            "current_evidence_id": evidence.current_evidence_id,
            "source_fit_id": evidence.formula_source_fit_id,
            "quality_gate": (
                "exact_attestation_v1" if exact else SURROGATE_QUALITY_GATE
            ),
            "source_fit_id_binds_full_solver_diagnostics": True,
            "current_holdout_count": evidence.current_holdout_count,
            "exact_trade_scope": (
                "balanced packages with no adds or drops" if exact else None
            ),
            "validated_balanced_package_sizes": list(
                evidence.validated_balanced_package_sizes
            ),
            "observed_balanced_package_sizes": (
                list(evidence.validated_balanced_package_sizes)
                if exact
                else list(evidence.observed_balanced_package_sizes)
            ),
            "holdout_max_absolute_score_error": (
                evidence.calibration_diagnostics.max_absolute_score_error
            ),
            "holdout_display_match_rate": (
                evidence.calibration_diagnostics.display_match_rate
            ),
        },
    }


def workbook_sources(bundle: EngineBundle) -> tuple[WorkbookSource, ...]:
    evidence = bundle.methodology_evidence
    sources = [
        WorkbookSource(
            f"FantasyPros ECR ({row.period.value})", row.ecr_id, row.captured_at
        )
        for row in bundle.ecr_snapshots
    ]
    by_provider = {}
    for row in bundle.projection_evidence:
        by_provider.setdefault(row.provider, []).append(row)
    for provider, rows in sorted(by_provider.items()):
        evidence_id = content_id(
            "projection-source", {"rows": [projection_to_record(row) for row in rows]}
        )
        sources.append(
            WorkbookSource(provider, evidence_id, max(row.captured_at for row in rows))
        )
    sources.extend(
        (
            WorkbookSource(
                (
                    "FantasyPros methodology attestation"
                    if bundle.methodology_mode == "exact"
                    else "FantasyPros SURROGATE methodology disclosure"
                ),
                (
                    bundle.methodology_attestation.attestation_id
                    if bundle.methodology_mode == "exact"
                    else bundle.surrogate_disclosure.disclosure_id
                ),
                evidence.current_evidence_at,
            ),
            WorkbookSource(
                "FantasyPros methodology current evidence",
                evidence.current_evidence_id,
                evidence.current_evidence_at,
            ),
        )
    )
    return tuple(sources)


def search_result_record(
    outcome: LeagueSearchOutcome,
    bundle: EngineBundle,
    projection: SeasonProjection,
    limit: int,
) -> dict[str, object]:
    team_names = {row.team_id: row.name for row in bundle.state.teams}
    rows = workbook_trade_rows(
        outcome,
        team_names,
        bundle.player_names,
        bundle.methodology_evidence,
    )
    outlook = team_outlook_rows(bundle.state, projection)
    return {
        "total_count": len(rows),
        "shown_count": min(limit, len(rows)),
        "power_engine_mode": bundle.methodology_mode,
        "power_engine_notice": (
            SURROGATE_NOTICE
            if bundle.methodology_mode == "surrogate"
            else "Exact only inside the attested trade scope."
        ),
        "team_outlook": [
            {
                "team_id": row.team_id,
                "team_name": row.team_name,
                "current_wins": row.current_wins,
                "current_losses": row.current_losses,
                "current_ties": row.current_ties,
                "expected_final_wins": row.expected_final_wins,
                "expected_final_losses": row.expected_final_losses,
                "expected_final_ties": row.expected_final_ties,
                "projected_finish": row.mean_rank,
                "playoff_probability": row.playoff_probability,
            }
            for row in outlook
        ],
        "rows": [
            {
                "other_team": row.counterparty_team_name,
                "give": list(row.outgoing_player_names),
                "receive": list(row.incoming_player_names),
                "your_adds": list(row.primary_added_player_names),
                "your_drops": list(row.primary_dropped_player_names),
                "their_adds": list(row.counterparty_added_player_names),
                "their_drops": list(row.counterparty_dropped_player_names),
                "your_power_delta": row.primary_power_delta,
                "their_power_delta": row.counterparty_power_delta,
                "your_playoff_delta": row.primary_playoff_delta,
                "their_playoff_delta": row.counterparty_playoff_delta,
                "combined_playoff_delta": row.combined_playoff_delta,
                "mutual_gain": row.is_mutual_gain,
                "power_methodology_status": row.power_methodology_status,
            }
            for row in rows[:limit]
        ],
    }


def string_array(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row for row in value
    ):
        raise ValueError(f"{name} must be a JSON array of non-empty strings")
    return tuple(value)


def boolean(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def strict_mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value
