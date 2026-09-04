"""Serialize only the source identities actually retained by an engine bundle."""

from ._data_readiness_time import timestamp_text as _timestamp
from .engine_bundle import EngineBundle


def bound_inputs(bundle: EngineBundle) -> dict[str, object]:
    source_manifest = bundle.source_manifest
    benchmark = bundle.fantasypros_benchmark
    projection_manifest = bundle.projection_source_manifest
    schedule = bundle.nfl_schedule
    disclosure = bundle.independent_power_disclosure
    return {
        "league_binding": {
            "league_binding_id": (
                None if source_manifest is None else source_manifest.league_binding_id
            ),
            "league_binding_scope": (
                None
                if source_manifest is None
                else source_manifest.league_binding_scope.value
            ),
            "host_provider": (
                None if source_manifest is None else source_manifest.host_provider
            ),
            "host_snapshot_id": (
                bundle.state.snapshot_id
                if source_manifest is None
                else source_manifest.host_snapshot_id
            ),
            "host_captured_at": (
                None
                if source_manifest is None
                else _timestamp(source_manifest.host_captured_at)
            ),
            "fantasypros_league_artifact_id": (
                None
                if source_manifest is None
                else source_manifest.fantasypros_league_artifact_id
            ),
            "fantasypros_captured_at": (
                None
                if source_manifest is None
                else _timestamp(source_manifest.fantasypros_captured_at)
            ),
            "completed_history_available": (
                None
                if source_manifest is None
                else source_manifest.completed_history_available
            ),
        },
        "fantasypros_comparison_benchmark": {
            "benchmark_id": None if benchmark is None else benchmark.benchmark_id,
            "source_artifact_id": (
                None if benchmark is None else benchmark.source_artifact_id
            ),
            "captured_at": (
                None if benchmark is None else _timestamp(benchmark.captured_at)
            ),
        },
        "projection_source_manifest": _projection_manifest_record(
            projection_manifest
        ),
        "independent_power_disclosure": (
            None
            if disclosure is None
            else {
                "captured_at": _timestamp(disclosure.captured_at),
                "disclosure_id": disclosure.disclosure_id,
                "policy_id": disclosure.policy_id,
                "provider_names": list(disclosure.provider_names),
            }
        ),
        "scoring_profile_id": bundle.scoring_profile.scoring_profile_id,
        "nfl_schedule_id": None if schedule is None else schedule.schedule_id,
        "nfl_schedule_source_provider": (
            None if schedule is None else schedule.source_provider
        ),
        "nfl_schedule_captured_at": (
            None if schedule is None else _timestamp(schedule.captured_at)
        ),
        "ensemble_config_id": (
            None if bundle.ensemble_config is None else bundle.ensemble_config.config_id
        ),
        "strength_formula_id": (
            None if bundle.strength_formula is None else bundle.strength_formula.formula_id
        ),
        "strength_model_id": bundle.strength_model.model_id,
        "scenario_player_score_floor": bundle.scenario_config.player_score_floor,
        "ecr_snapshot_ids": {
            row.period.value: row.ecr_id for row in bundle.ecr_snapshots
        },
    }


def _projection_manifest_record(manifest) -> dict[str, object]:
    if manifest is None:
        return {
            "manifest_id": None,
            "evaluation_scoring_profile_id": None,
            "sources": [],
            "attempts": [],
        }
    return {
        "manifest_id": manifest.manifest_id,
        "evaluation_scoring_profile_id": manifest.evaluation_scoring_profile_id,
        "sources": [
            {
                "artifact_id": row.artifact_id,
                "provider": row.provider.value,
                "captured_at": _timestamp(row.captured_at),
                "season": row.season,
                "week": row.week,
                "horizon": row.horizon.value,
                "source_scoring_format": row.source_scoring_format,
                "point_basis": row.point_basis.value,
                "host_scoring_compatibility": row.host_scoring_compatibility.value,
                "position_scope": list(row.position_scope),
                "source_period_text": row.source_period_text,
                "normalized_input_count": len(row.inputs),
            }
            for row in manifest.sources
        ],
        "attempts": [
            {
                "task_id": row.task_id,
                "artifact_id": row.artifact_id,
                "provider": row.provider.value,
                "attempted_at": _timestamp(row.attempted_at),
                "season": row.season,
                "week": row.week,
                "horizon": row.horizon.value,
                "source_scoring_format": row.scoring,
                "position_scope": list(row.position_scope),
                "status": row.status.value,
                "reason_code": row.reason_code.value,
            }
            for row in manifest.attempts
        ],
    }
