"""Capability-level readiness for optional league-history consumers."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from .engine_bundle import EngineBundle
from .league_history import (
    HISTORY_CAPTURE_BINDING_TOLERANCE,
    LeagueHistorySnapshot,
)
from .weekly_collection import WeeklyHistoryAttempt, WeeklyHistoryStatus


_SCHEMA_VERSION = 1


def build_history_data_readiness(
    bundle: EngineBundle,
    history: LeagueHistorySnapshot | None,
    *,
    store_status: str = "available",
    collection_attempt: WeeklyHistoryAttempt | None = None,
) -> dict[str, object]:
    """Describe exactly which history-backed features may consume the data.

    League history is a mutable local sidecar, so it cannot be folded into the
    immutable bundle-only readiness report.  This report binds the sidecar to
    the selected bundle and evaluates each consumer independently.
    """

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    if history is not None and not isinstance(history, LeagueHistorySnapshot):
        raise ValueError("history must be a LeagueHistorySnapshot or null")
    if collection_attempt is not None and not isinstance(
        collection_attempt, WeeklyHistoryAttempt
    ):
        raise ValueError("collection_attempt must be a WeeklyHistoryAttempt or null")
    if store_status not in {"available", "unavailable"}:
        raise ValueError("store_status must be available or unavailable")

    manifest = bundle.source_manifest
    as_of = _bundle_as_of(bundle, history)
    identity_bound = bool(
        history is not None
        and history.bundle_id == bundle.bundle_id
        and history.league_key == manifest.league_binding_id
        and history.season == bundle.state.season
    )
    if history is not None and not identity_bound:
        raise ValueError("league history does not match the selected weekly bundle")

    captures = (
        ()
        if history is None
        else tuple(row for row in history.captures if row.captured_at <= as_of)
    )
    bound_capture = _bound_capture(history)
    exact_binding = _exact_binding(bundle, history)
    fresh = bool(
        bound_capture is not None
        and bound_capture.captured_at <= as_of
        and bound_capture.coverage_end <= as_of
        and as_of - bound_capture.captured_at <= HISTORY_CAPTURE_BINDING_TOLERANCE
        and as_of - bound_capture.coverage_end <= HISTORY_CAPTURE_BINDING_TOLERANCE
    )
    transactions_complete = bool(
        exact_binding and fresh and bound_capture.transaction_history_complete
    )
    rosters_complete = bool(
        exact_binding and fresh and bound_capture.roster_complete
    )
    lineups_complete = bool(
        exact_binding and fresh and bound_capture.lineup_complete
    )
    status_rows = (
        ()
        if bound_capture is None
        else tuple(
            player for roster in bound_capture.rosters for player in roster.players
        )
    )
    health_complete = bool(
        rosters_complete
        and status_rows
        and all(row.injury_status is not None for row in status_rows)
    )
    acquisition = (
        None if bound_capture is None else bound_capture.acquisition_evidence
    )

    history_missing = _history_missing(
        store_status,
        history,
        identity_bound,
        exact_binding,
        bound_capture,
        fresh,
        collection_attempt,
    )
    activity_status = (
        "ready"
        if transactions_complete
        else "partial"
        if bound_capture is not None
        else "not_ready"
    )
    roster_power_status = bundle.methodology_evidence.power_result_status(
        outgoing_count=1,
        incoming_count=1,
        has_roster_adjustment=False,
    )
    trajectory_ready = bundle.state.completed_history_is_usable

    capabilities = {
        "current_roster_compatibility": {
            "status": (
                "ready_with_holdout_validated_scope"
                if roster_power_status == "holdout_validated"
                else "ready_with_model_limitations"
            ),
            "uses": [
                "bundle_rosters",
                "bundle_eligibility",
                "bundle_power_model",
                "bundle_projection_grid",
            ],
            "history_required": False,
            "evidence": {"power_methodology_status": roster_power_status},
            "missing": [],
            "limitations": [
                "The quick compatibility screen is exhaustive only for eligible 1-for-1 swaps.",
                "A negative result does not rule out larger or differently shaped packages.",
            ],
        },
        "completed_deal_activity": {
            "status": activity_status,
            "uses": [
                "completed_transaction_ledger",
                "capture_coverage",
                "team_identity",
                "source_timestamp_semantics",
            ],
            "history_required": True,
            "evidence": {
                "capture_count": len(captures),
                "bound_capture_id": (
                    None if bound_capture is None else bound_capture.capture_id
                ),
                "bound_capture_fresh": fresh,
                "transaction_history_complete": transactions_complete,
                "acquisition": (
                    None if acquisition is None else acquisition.to_record()
                ),
                "collection_attempt": (
                    None
                    if collection_attempt is None
                    else collection_attempt.to_record()
                ),
            },
            "missing": history_missing + (
                []
                if (
                    bound_capture is None
                    or bound_capture.transaction_history_complete
                )
                else ["complete_transaction_ledger"]
            ),
            "limitations": [
                "Completed activity does not include rejected offers or prove who initiated or accepted a trade.",
                "Activity rates remain descriptive until the league trading window is captured.",
            ],
        },
        "manager_roster_history": {
            "status": (
                "ready"
                if rosters_complete
                else "partial"
                if bound_capture is not None
                else "not_ready"
            ),
            "uses": [
                "bundle_bound_roster_snapshots",
                "canonical_player_ownership",
                "capture_coverage",
            ],
            "history_required": True,
            "evidence": {
                "bound_capture_id": (
                    None if bound_capture is None else bound_capture.capture_id
                ),
                "roster_complete": rosters_complete,
            },
            "missing": [
                *history_missing,
                *([] if rosters_complete else ["complete_roster_snapshot"]),
            ],
            "limitations": [
                "Roster snapshots describe observed ownership, not rejected moves or manager intent.",
            ],
        },
        "manager_lineup_history": {
            "status": (
                "ready_at_capture_times"
                if lineups_complete
                else "partial"
                if bound_capture is not None
                else "not_ready"
            ),
            "uses": [
                "bundle_bound_lineup_snapshots",
                "canonical_player_ownership",
                "lineup_slot_assignments",
            ],
            "history_required": True,
            "evidence": {
                "bound_capture_id": (
                    None if bound_capture is None else bound_capture.capture_id
                ),
                "lineup_complete": lineups_complete,
            },
            "missing": [
                *history_missing,
                *([] if lineups_complete else ["complete_lineup_snapshot"]),
            ],
            "limitations": [
                "Captured lineup slots are point-in-time settings, not hindsight-optimal decisions.",
            ],
        },
        "historical_trade_valuation": {
            "status": (
                "ready_with_per_transaction_gates"
                if transactions_complete
                else activity_status
            ),
            "uses": [
                "completed_deal_activity",
                "pre_event_roster_reconstruction",
                "strictly_prior_weekly_engine",
                "compatible_formula_and_source_contract",
            ],
            "history_required": True,
            "evidence": {
                "exact_bundle_binding": exact_binding,
                "transaction_history_complete": transactions_complete,
            },
            "missing": history_missing,
            "limitations": [
                "Each transaction is withheld unless event ordering, pre-trade rosters, and a strictly prior compatible engine are all provable.",
                "A current revaluation never substitutes for a missing transaction-time value.",
            ],
        },
        "historical_foresight": {
            "status": (
                "eligible_for_per_trade_screening"
                if transactions_complete and health_complete
                else "not_ready"
            ),
            "uses": [
                "historical_trade_valuation",
                "same_context_current_revaluation",
                "complete_time_aligned_health_observations",
                "compatible_methodology_and_projection_lineage",
            ],
            "history_required": True,
            "evidence": {
                "health_observation_rows": len(status_rows),
                "health_coverage_complete": health_complete,
            },
            "missing": [
                *history_missing,
                *([] if health_complete else ["complete_time_aligned_health_coverage"]),
            ],
            "limitations": [
                "Even an eligible foresight label is descriptive and non-causal.",
                "Captured weekly status cannot prove that no health change occurred between scans.",
            ],
        },
        "trade_timing_trajectory": {
            "status": "ready_with_model_limitations" if trajectory_ready else "not_ready",
            "uses": [
                "completed_matchup_ledger",
                "current_standings",
                "remaining_schedule",
                "shared_season_scenarios",
            ],
            "history_required": False,
            "evidence": {
                "completed_matchup_ledger_reconciles_to_standings": trajectory_ready,
            },
            "missing": [] if trajectory_ready else ["reconciled_completed_matchups"],
            "limitations": [
                "Pressure and playoff changes are simulated associations, not offer-acceptance probabilities.",
            ],
        },
        "trade_timing_behavior": {
            "status": activity_status,
            "uses": [
                "completed_deal_activity",
                "effective_scoring_period",
                "observed_record_trajectory",
            ],
            "history_required": True,
            "evidence": {
                "transaction_history_complete": transactions_complete,
                "completed_matchup_ledger_reconciles_to_standings": trajectory_ready,
            },
            "missing": [
                *history_missing,
                *([] if trajectory_ready else ["reconciled_completed_matchups"]),
            ],
            "limitations": [
                "Multiple completed trades in one scoring period count as one activity window.",
                "Proposal and execution timestamp meanings must remain separate strata.",
            ],
        },
        "trade_legality": {
            "status": "not_ready",
            "uses": [
                "trade_deadline",
                "locked_and_undroppable_players",
                "host_processing_rules",
            ],
            "history_required": False,
            "evidence": {
                "trade_deadline_captured": False,
                "player_tradeability_captured": False,
                "processing_rules_captured": False,
            },
            "missing": [
                "trade_deadline",
                "locked_and_undroppable_players",
                "host_trade_processing_rules",
            ],
            "limitations": [
                "Every proposed trade requires verification in the host league before submission.",
            ],
        },
    }

    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "league_binding_id": manifest.league_binding_id,
        "analysis_as_of": as_of.isoformat(timespec="microseconds"),
        "history_revision": None if history is None else history.history_revision,
        "status": (
            "ready_with_limitations"
            if transactions_complete
            else "partial"
            if bound_capture is not None
            else "history_unavailable_core_features_ready"
        ),
        "store_status": store_status,
        "collection_attempt": (
            None if collection_attempt is None else collection_attempt.to_record()
        ),
        "identity": {
            "bundle_history_identity_bound": identity_bound,
            "exact_host_capture_binding": exact_binding,
        },
        "capabilities": capabilities,
    }
    json.dumps(result, allow_nan=False, sort_keys=True)
    return result


def _bundle_as_of(
    bundle: EngineBundle, history: LeagueHistorySnapshot | None
) -> datetime:
    if history is not None:
        return history.bundle_captured_at.astimezone(timezone.utc)
    return max(
        bundle.source_manifest.host_captured_at,
        bundle.source_manifest.fantasypros_captured_at,
    ).astimezone(timezone.utc)


def _exact_binding(
    bundle: EngineBundle, history: LeagueHistorySnapshot | None
) -> bool:
    if history is None:
        return False
    binding = history.requested_binding
    manifest = bundle.source_manifest
    if (
        binding.host_snapshot_id is None
        or binding.host_snapshot_id != manifest.host_snapshot_id
        or binding.host_captured_at != manifest.host_captured_at
        or binding.history_capture_id is None
        or binding.roster_ownership_id is None
    ):
        return False
    bound = _bound_capture(history)
    if (
        bound is None
        or bound.host_snapshot_id != manifest.host_snapshot_id
        or bound.roster_ownership_id != binding.roster_ownership_id
    ):
        return False
    bundle_ownership = {
        roster.team_id: frozenset(roster.player_ids) for roster in bundle.rosters
    }
    history_ownership = {
        roster.team_id: frozenset(
            player.canonical_player_id for player in roster.players
        )
        for roster in bound.rosters
    }
    return history_ownership == bundle_ownership


def _bound_capture(history: LeagueHistorySnapshot | None):
    if history is None or history.requested_binding.history_capture_id is None:
        return None
    capture_id = history.requested_binding.history_capture_id
    return next(
        (
            capture
            for capture in history.captures
            if capture.capture_id == capture_id
        ),
        None,
    )


def _history_missing(
    store_status,
    history,
    identity_bound,
    exact_binding,
    bound_capture,
    fresh,
    collection_attempt,
):
    missing = []
    if store_status == "unavailable":
        missing.append("readable_local_history_store")
    if history is None:
        missing.append("bundle_bound_history_capture")
        if collection_attempt is None:
            missing.append("history_collection_evidence")
        elif collection_attempt.status is WeeklyHistoryStatus.NOT_PROVIDED:
            missing.append("history_collection_not_attempted")
        elif collection_attempt.status is WeeklyHistoryStatus.UNAVAILABLE:
            missing.append(
                f"history_collection_{collection_attempt.reason_code.value}"
            )
        else:
            missing.append("captured_history_sidecar")
    elif not identity_bound:
        missing.append("matching_league_season_bundle_identity")
    if history is not None and not exact_binding:
        missing.append("exact_host_snapshot_and_roster_binding")
    if bound_capture is None:
        missing.append("history_capture")
    elif not fresh:
        missing.append("fresh_history_capture_at_analysis_time")
    return missing


__all__ = ("build_history_data_readiness",)
