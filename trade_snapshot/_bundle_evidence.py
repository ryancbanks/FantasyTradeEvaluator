"""Shared identities and timestamps for evidence retained in an engine bundle."""

from datetime import datetime

from .engine_bundle import EngineBundle


def retained_source_times(bundle: EngineBundle) -> tuple[datetime, ...]:
    """Return only timestamps whose source records remain in the bundle."""

    _require_bundle(bundle)
    times = [
        *(row.captured_at for row in bundle.projection_evidence),
        *(row.captured_at for row in bundle.ecr_snapshots),
        bundle.methodology_evidence.current_evidence_at,
    ]
    if bundle.nfl_schedule is not None:
        times.append(bundle.nfl_schedule.captured_at)
    if bundle.source_manifest is not None:
        times.extend(
            (
                bundle.source_manifest.host_captured_at,
                bundle.source_manifest.fantasypros_captured_at,
            )
        )
    if bundle.fantasypros_benchmark is not None:
        times.append(bundle.fantasypros_benchmark.captured_at)
    if bundle.projection_source_manifest is not None:
        times.extend(
            row.captured_at for row in bundle.projection_source_manifest.sources
        )
        times.extend(
            row.attempted_at for row in bundle.projection_source_manifest.attempts
        )
    return tuple(times)


def model_analysis_as_of(bundle: EngineBundle) -> datetime:
    """Return the newest input time used by model-facing reports.

    Configured bundles retain the original report boundary: host, FantasyPros,
    ECR, and projection-source captures. Independent bundles have no such
    manifests, so their boundary comes from their retained normalized
    projection evidence and the independent-methodology disclosure.
    """

    _require_bundle(bundle)
    if (
        bundle.source_manifest is not None
        and bundle.projection_source_manifest is not None
    ):
        return max(
            bundle.source_manifest.host_captured_at,
            bundle.source_manifest.fantasypros_captured_at,
            *(row.captured_at for row in bundle.ecr_snapshots),
            *(
                row.captured_at
                for row in bundle.projection_source_manifest.sources
            ),
        )
    return max(
        bundle.methodology_evidence.current_evidence_at,
        *(row.captured_at for row in bundle.projection_evidence),
    )


def model_evidence_ids(bundle: EngineBundle) -> dict[str, object]:
    """Return stable model IDs, leaving intentionally absent artifacts as null."""

    _require_bundle(bundle)
    methodology = bundle.methodology_evidence
    independent = bundle.methodology_mode == "independent"
    return {
        "league_binding_id": (
            None
            if bundle.source_manifest is None
            else bundle.source_manifest.league_binding_id
        ),
        "strength_formula_id": (
            methodology.policy_id
            if independent
            else bundle.strength_formula.formula_id
        ),
        "methodology_evidence_id": (
            getattr(methodology, "attestation_id", None)
            or getattr(methodology, "disclosure_id", None)
        ),
        "projection_source_manifest_id": (
            None
            if bundle.projection_source_manifest is None
            else bundle.projection_source_manifest.manifest_id
        ),
        "ensemble_config_id": (
            None
            if bundle.ensemble_config is None
            else bundle.ensemble_config.config_id
        ),
        "ecr_ids": sorted(row.ecr_id for row in bundle.ecr_snapshots),
    }


def _require_bundle(bundle) -> None:
    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")


__all__ = (
    "model_analysis_as_of",
    "model_evidence_ids",
    "retained_source_times",
)
