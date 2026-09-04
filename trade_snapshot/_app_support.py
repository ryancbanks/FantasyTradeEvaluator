"""Small validation and presentation helpers for the localhost app service."""

from collections.abc import Mapping
from pathlib import Path
import os
import re
import sys

from ._scenario_random import content_id
from .data_readiness import build_bundle_data_readiness
from .engine_bundle import EngineBundle
from .league_search import LeagueSearchOutcome
from .positions import CANONICAL_PLAYER_POSITIONS
from .projection_io import projection_to_record
from .roster_adjustment import MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY
from .season import SeasonProjection
from .surrogate_disclosure import SURROGATE_NOTICE, SURROGATE_QUALITY_GATE
from .three_way_search import ThreeWaySearchOutcome
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
    attested = bundle.methodology_attestation is not None
    data_readiness = build_bundle_data_readiness(bundle)
    binding = bundle.source_manifest
    league_key = binding.league_binding_id.removeprefix("league_")[:12]
    league_label = (
        f"{binding.host_provider.upper()} "
        f"{binding.league_binding_scope.value} {league_key}"
    )
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
        "league_key": league_key,
        "league_label": league_label,
        "status": (
            "not_ready" if data_readiness["status"] == "not_ready" else "ready"
        ),
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
            "Representative blind holdouts validate the listed balanced-package "
            "shapes; this is not an exhaustive proof of every combination. Other "
            "shapes are extrapolated."
            if attested
            else SURROGATE_NOTICE
        ),
        "three_team_free_agent_allocation_policy": (
            MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY
        ),
        "data_readiness": data_readiness,
        "methodology": {
            "mode": bundle.methodology_mode,
            "attestation_id": (
                None if not attested else bundle.methodology_attestation.attestation_id
            ),
            "surrogate_disclosure_id": (
                None if attested else bundle.surrogate_disclosure.disclosure_id
            ),
            "formula_id": evidence.formula_id,
            "fingerprint_id": evidence.methodology_fingerprint.fingerprint_id,
            "formula_action": evidence.formula_decision.action.value,
            "current_evidence_id": evidence.current_evidence_id,
            "source_fit_id": evidence.formula_source_fit_id,
            "quality_gate": (
                "blind_holdout_validation_v1"
                if attested
                else SURROGATE_QUALITY_GATE
            ),
            "source_fit_id_binds_full_solver_diagnostics": True,
            "current_holdout_count": evidence.current_holdout_count,
            "holdout_validated_trade_scope": (
                "balanced package shapes with no adds or drops"
                if attested
                else None
            ),
            "validated_balanced_package_sizes": list(
                evidence.validated_balanced_package_sizes
            ),
            "observed_balanced_package_sizes": (
                list(evidence.validated_balanced_package_sizes)
                if attested
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
    manifest = bundle.source_manifest
    sources = [
        WorkbookSource(
            f"Host league snapshot ({manifest.host_provider})",
            manifest.host_snapshot_id,
            manifest.host_captured_at,
        ),
        WorkbookSource(
            f"Opaque league binding ({manifest.league_binding_scope.value})",
            manifest.league_binding_id,
            manifest.host_captured_at,
        ),
        WorkbookSource(
            "FantasyPros league artifact",
            manifest.fantasypros_league_artifact_id,
            manifest.fantasypros_captured_at,
        ),
        WorkbookSource(
            "FantasyPros comparison benchmark record (comparison only)",
            bundle.fantasypros_benchmark.benchmark_id,
            bundle.fantasypros_benchmark.captured_at,
        ),
        WorkbookSource(
            "FantasyPros comparison source artifact (comparison only)",
            bundle.fantasypros_benchmark.source_artifact_id,
            bundle.fantasypros_benchmark.captured_at,
        ),
    ]
    sources.extend(
        WorkbookSource(
            f"FantasyPros ECR ({row.period.value})", row.ecr_id, row.captured_at
        )
        for row in bundle.ecr_snapshots
    )
    sources.append(
        WorkbookSource(
            f"NFL schedule ({bundle.nfl_schedule.source_provider})",
            bundle.nfl_schedule.schedule_id,
            bundle.nfl_schedule.captured_at,
        )
    )
    projection_sources = bundle.projection_source_manifest.sources
    projection_attempts = bundle.projection_source_manifest.attempts
    projection_source_times = tuple(
        row.captured_at for row in projection_sources
    ) + tuple(row.attempted_at for row in projection_attempts)
    sources.append(
        WorkbookSource(
            "Projection source manifest",
            bundle.projection_source_manifest.manifest_id,
            max(projection_source_times),
        )
    )
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
                    if bundle.methodology_mode == "holdout_validated"
                    else "FantasyPros SURROGATE methodology disclosure"
                ),
                (
                    bundle.methodology_attestation.attestation_id
                    if bundle.methodology_mode == "holdout_validated"
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
            else (
                "Power is blind-holdout validated for representative trades in "
                "the listed package shapes, not exhaustively proven for every combination."
            )
        ),
        "team_outlook": _team_outlook_records(outlook),
        "rows": [
            {
                "candidate_index": str(row.candidate_index),
                "search_run_id": row.search_run_id,
                "other_team_id": row.counterparty_team_id,
                "other_team": row.counterparty_team_name,
                "give_player_ids": list(row.outgoing_player_ids),
                "give": list(row.outgoing_player_names),
                "receive_player_ids": list(row.incoming_player_ids),
                "receive": list(row.incoming_player_names),
                "your_add_player_ids": list(row.primary_added_player_ids),
                "your_adds": list(row.primary_added_player_names),
                "your_drop_player_ids": list(row.primary_dropped_player_ids),
                "your_drops": list(row.primary_dropped_player_names),
                "their_add_player_ids": list(row.counterparty_added_player_ids),
                "their_adds": list(row.counterparty_added_player_names),
                "their_drop_player_ids": list(row.counterparty_dropped_player_ids),
                "their_drops": list(row.counterparty_dropped_player_names),
                "your_power_delta": row.primary_power_delta,
                "their_power_delta": row.counterparty_power_delta,
                "your_playoff_before": row.primary_playoff_before,
                "your_playoff_after": row.primary_playoff_after,
                "your_playoff_delta": row.primary_playoff_delta,
                "their_playoff_before": row.counterparty_playoff_before,
                "their_playoff_after": row.counterparty_playoff_after,
                "their_playoff_delta": row.counterparty_playoff_delta,
                "combined_playoff_delta": row.combined_playoff_delta,
                "mutual_gain": row.is_mutual_gain,
                "power_methodology_status": row.power_methodology_status,
            }
            for row in rows[:limit]
        ],
    }


def three_way_search_result_record(
    outcome: ThreeWaySearchOutcome,
    bundle: EngineBundle,
    projection: SeasonProjection,
    limit: int,
    *,
    free_agent_allocation_policy: str | None = None,
) -> dict[str, object]:
    """Present a bounded, name-resolved preview of a three-team search."""

    if not isinstance(outcome, ThreeWaySearchOutcome):
        raise ValueError("outcome must be a ThreeWaySearchOutcome")
    team_names = {row.team_id: row.name for row in bundle.state.teams}
    results = outcome.results(limit)
    try:
        rows = [
            {
                "candidate_index": str(result.candidate_index),
                "transfers": [
                    {
                        "from_team_id": transfer.source_team_id,
                        "from_team_name": team_names[transfer.source_team_id],
                        "to_team_id": transfer.destination_team_id,
                        "to_team_name": team_names[transfer.destination_team_id],
                        "players": [
                            {
                                "player_id": player_id,
                                "name": bundle.player_names[player_id],
                            }
                            for player_id in transfer.player_ids
                        ],
                    }
                    for transfer in result.transfers
                ],
                "team_impacts": [
                    {
                        "team_id": impact.team_id,
                        "team_name": team_names[impact.team_id],
                        "give": [
                            bundle.player_names[player_id]
                            for player_id in impact.sent_player_ids
                        ],
                        "receive": [
                            bundle.player_names[player_id]
                            for player_id in impact.received_player_ids
                        ],
                        "adds": [
                            bundle.player_names[player_id]
                            for player_id in impact.added_player_ids
                        ],
                        "drops": [
                            bundle.player_names[player_id]
                            for player_id in impact.dropped_player_ids
                        ],
                        "power_delta": impact.display_power_delta,
                        "playoff_before": impact.playoff_before / 100,
                        "playoff_after": impact.playoff_after / 100,
                        "playoff_delta": impact.playoff_delta / 100,
                    }
                    for impact in result.team_results
                ],
                "all_teams_gain": result.all_teams_gain,
                "combined_playoff_delta": result.combined_playoff_delta / 100,
                "power_methodology_status": (
                    "extrapolated"
                    if bundle.methodology_mode == "holdout_validated"
                    else "surrogate_extrapolated"
                ),
            }
            for result in results
        ]
    except KeyError as error:
        raise ValueError(f"missing display name for ID {error.args[0]!r}") from None
    return {
        "trade_format": "three_team",
        "total_count": (
            outcome.progress.power_qualified_count
            if outcome.progress.power_qualified_count <= (1 << 53) - 1
            else None
        ),
        "total_count_text": str(outcome.progress.power_qualified_count),
        "shown_count": len(rows),
        "power_engine_mode": bundle.methodology_mode,
        "power_engine_notice": (
            SURROGATE_NOTICE
            if bundle.methodology_mode == "surrogate"
            else "Three-team power is extrapolated beyond the blind-validated two-team scope."
        ),
        "free_agent_allocation_policy": free_agent_allocation_policy,
        "team_outlook": _team_outlook_records(
            team_outlook_rows(bundle.state, projection)
        ),
        "rows": rows,
    }


def _team_outlook_records(outlook) -> list[dict[str, object]]:
    return [
        {
            "team_id": row.team_id,
            "team_name": row.team_name,
            "current_wins": row.current_wins,
            "current_losses": row.current_losses,
            "current_ties": row.current_ties,
            "expected_final_wins": row.expected_final_wins,
            "expected_final_losses": row.expected_final_losses,
            "expected_final_ties": row.expected_final_ties,
            "current_rank": row.current_rank,
            "expected_final_points_for": row.expected_final_points_for,
            "expected_final_points_against": row.expected_final_points_against,
            "projected_finish": row.mean_rank,
            "rank_distribution": list(row.rank_distribution),
            "seed_distribution": list(row.seed_distribution),
            "playoff_probability": row.playoff_probability,
        }
        for row in outlook
    ]


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
