"""Comparable, content-addressed model evidence for retrospective GM analysis."""

from dataclasses import dataclass, field

from ._scenario_random import content_id
from .capture_schema import RankingHorizon
from .engine_bundle import EngineBundle
from .projections import RemainingSeasonProjection


POWER_RESULT_STATUSES = frozenset(
    {
        "holdout_validated",
        "extrapolated",
        "surrogate",
        "surrogate_extrapolated",
        "independent",
    }
)
FORESIGHT_POWER_STATUS = "holdout_validated"


@dataclass(frozen=True, slots=True)
class GmModelEvidence:
    """The model and source semantics behind one package valuation.

    Artifact IDs deliberately remain evidence pointers rather than comparison
    criteria: two weekly captures should contain different values and artifact
    IDs.  ``source_contract_id`` instead binds the provider, scoring-basis, and
    horizon semantics that must stay stable for a then/current interpretation.
    """

    bundle_id: str
    weekly_snapshot_id: str
    strength_model_id: str
    formula_id: str
    formula_source_fit_id: str | None
    methodology_mode: str
    methodology_status: str
    methodology_evidence_id: str
    methodology_fingerprint_id: str
    scoring_profile_id: str
    power_feature_names: tuple[str, ...]
    source_providers: tuple[str, ...]
    scoring_bases: tuple[str, ...]
    horizons: tuple[str, ...]
    source_contract_id: str
    projection_source_manifest_id: str | None
    ensemble_config_id: str | None
    scenario_config_id: str
    ecr_ids: tuple[str, ...]
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "bundle_id",
            "weekly_snapshot_id",
            "strength_model_id",
            "formula_id",
            "methodology_evidence_id",
            "methodology_fingerprint_id",
            "scoring_profile_id",
            "source_contract_id",
            "scenario_config_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        for name in (
            "formula_source_fit_id",
            "projection_source_manifest_id",
            "ensemble_config_id",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be non-empty text or None")
        if self.methodology_mode not in {
            "holdout_validated",
            "surrogate",
            "independent",
        }:
            raise ValueError("methodology_mode is invalid")
        if self.methodology_status not in POWER_RESULT_STATUSES:
            raise ValueError("methodology_status is invalid")
        expected_statuses = {
            "holdout_validated": {"holdout_validated", "extrapolated"},
            "surrogate": {"surrogate", "surrogate_extrapolated"},
            "independent": {"independent"},
        }[self.methodology_mode]
        if self.methodology_status not in expected_statuses:
            raise ValueError("methodology status does not match methodology mode")
        optional_artifact_ids = (
            self.formula_source_fit_id,
            self.projection_source_manifest_id,
            self.ensemble_config_id,
        )
        if self.methodology_mode == "independent":
            if any(value is not None for value in optional_artifact_ids):
                raise ValueError(
                    "independent model evidence cannot claim unavailable audit artifacts"
                )
        elif any(value is None for value in optional_artifact_ids):
            raise ValueError(
                "FantasyPros model evidence requires complete audit artifact IDs"
            )
        for name in (
            "power_feature_names",
            "source_providers",
            "scoring_bases",
            "horizons",
        ):
            values = tuple(getattr(self, name))
            if not values or any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} cannot contain duplicates")
            object.__setattr__(self, name, tuple(sorted(values)))
        ecr_ids = tuple(self.ecr_ids)
        if any(not isinstance(value, str) or not value for value in ecr_ids):
            raise ValueError("ecr_ids must contain non-empty strings")
        if len(set(ecr_ids)) != len(ecr_ids):
            raise ValueError("ecr_ids cannot contain duplicates")
        if not ecr_ids and self.methodology_mode != "independent":
            raise ValueError("ecr_ids cannot be empty outside independent mode")
        if ecr_ids and self.methodology_mode == "independent":
            raise ValueError("independent model evidence cannot claim ECR inputs")
        object.__setattr__(self, "ecr_ids", tuple(sorted(ecr_ids)))
        object.__setattr__(
            self,
            "evidence_id",
            content_id("gm-model-evidence", self._content_record()),
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "ecr_ids": list(self.ecr_ids),
            "ensemble_config_id": self.ensemble_config_id,
            "formula_id": self.formula_id,
            "formula_source_fit_id": self.formula_source_fit_id,
            "horizons": list(self.horizons),
            "methodology_evidence_id": self.methodology_evidence_id,
            "methodology_fingerprint_id": self.methodology_fingerprint_id,
            "methodology_mode": self.methodology_mode,
            "methodology_status": self.methodology_status,
            "power_feature_names": list(self.power_feature_names),
            "projection_source_manifest_id": self.projection_source_manifest_id,
            "scenario_config_id": self.scenario_config_id,
            "scoring_bases": list(self.scoring_bases),
            "scoring_profile_id": self.scoring_profile_id,
            "source_contract_id": self.source_contract_id,
            "source_providers": list(self.source_providers),
            "strength_model_id": self.strength_model_id,
            "weekly_snapshot_id": self.weekly_snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {**self._content_record(), "evidence_id": self.evidence_id}


def build_gm_model_evidence(
    bundle: EngineBundle,
    *,
    outgoing_count: int,
    incoming_count: int,
) -> GmModelEvidence:
    """Describe the exact model/source contract used for one trade shape."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    methodology = bundle.methodology_evidence
    independent = bundle.methodology_mode == "independent"
    feature_names = (
        ("remaining_regular_season_ensemble_points",)
        if independent
        else tuple(
            sorted(
                {
                    *bundle.strength_formula.residual_weights,
                    *(
                        name
                        for weights in bundle.strength_formula.role_weights.values()
                        for name in weights
                    ),
                }
            )
        )
    )
    source_contract = _power_source_contract(bundle, feature_names)
    evidence_id = getattr(methodology, "attestation_id", None) or getattr(
        methodology, "disclosure_id", None
    )
    return GmModelEvidence(
        bundle_id=bundle.bundle_id,
        weekly_snapshot_id=bundle.state.snapshot_id,
        strength_model_id=bundle.strength_model.model_id,
        formula_id=(
            methodology.policy_id if independent else bundle.strength_formula.formula_id
        ),
        formula_source_fit_id=(
            None if independent else bundle.strength_formula.source_fit_id
        ),
        methodology_mode=bundle.methodology_mode,
        methodology_status=methodology.power_result_status(
            outgoing_count=outgoing_count,
            incoming_count=incoming_count,
            has_roster_adjustment=False,
        ),
        methodology_evidence_id=evidence_id,
        methodology_fingerprint_id=(
            methodology.policy_id
            if independent
            else methodology.methodology_fingerprint.fingerprint_id
        ),
        scoring_profile_id=bundle.state.scoring_profile_id,
        power_feature_names=feature_names,
        source_providers=source_contract["providers"],
        scoring_bases=source_contract["scoring_bases"],
        horizons=source_contract["horizons"],
        source_contract_id=source_contract["contract_id"],
        projection_source_manifest_id=(
            None
            if independent
            else bundle.projection_source_manifest.manifest_id
        ),
        ensemble_config_id=(
            None if independent else bundle.ensemble_config.config_id
        ),
        scenario_config_id=bundle.scenario_config.config_id,
        ecr_ids=tuple(row.ecr_id for row in bundle.ecr_snapshots),
    )


def model_comparability_reasons(
    source: GmModelEvidence,
    current: GmModelEvidence,
) -> tuple[str, ...]:
    """Return every reason the two value snapshots cannot support foresight."""

    if not isinstance(source, GmModelEvidence) or not isinstance(
        current, GmModelEvidence
    ):
        raise ValueError("model evidence values are required")
    reasons = []
    if source.scoring_profile_id != current.scoring_profile_id:
        reasons.append("scoring_profile_changed")
    if source.formula_id != current.formula_id:
        reasons.append("strength_formula_changed")
    if source.methodology_fingerprint_id != current.methodology_fingerprint_id:
        reasons.append("methodology_fingerprint_changed")
    if source.methodology_mode != current.methodology_mode:
        reasons.append("methodology_mode_changed")
    if (
        source.methodology_status != FORESIGHT_POWER_STATUS
        or current.methodology_status != FORESIGHT_POWER_STATUS
    ):
        reasons.append("power_shape_not_blind_holdout_validated_at_both_times")
    if source.power_feature_names != current.power_feature_names:
        reasons.append("power_feature_set_changed")
    if source.source_providers != current.source_providers:
        reasons.append("power_input_provider_set_changed")
    if source.scoring_bases != current.scoring_bases:
        reasons.append("power_input_scoring_basis_changed")
    if source.horizons != current.horizons:
        reasons.append("power_input_horizon_changed")
    if source.source_contract_id != current.source_contract_id:
        reasons.append("power_input_source_semantics_changed")
    return tuple(sorted(set(reasons)))


def _power_source_contract(bundle, feature_names):
    if bundle.methodology_mode == "independent":
        return _independent_power_source_contract(bundle, feature_names)
    ecr_periods = {
        "weekly" for name in feature_names if name.startswith("ecr_weekly_")
    } | {
        "rest_of_season" for name in feature_names if name.startswith("ecr_ros_")
    }
    projection_scope = _projection_scope(bundle, feature_names)
    ecr_semantics = []
    for snapshot in bundle.ecr_snapshots:
        if snapshot.period.value not in ecr_periods:
            continue
        for panel in snapshot.expert_panels:
            provenance = panel.provenance
            details = provenance.source_details
            ecr_semantics.append(
                (
                    snapshot.period.value,
                    panel.position,
                    provenance.league_scoring,
                    provenance.source_scoring,
                    provenance.capture_method,
                    details.expert_selection_policy,
                    details.expert_group_id,
                    details.ranking_type,
                    details.horizon_evidence.value,
                )
            )
    projection_semantics = tuple(
        sorted(
            {
                (
                    source.provider.value,
                    source.horizon.value,
                    source.source_scoring_format,
                    source.point_basis.value,
                    source.host_scoring_compatibility.value,
                    *source.position_scope,
                )
                for source in bundle.projection_source_manifest.sources
                if (source.provider.value, source.horizon) in projection_scope
            }
        )
    )
    providers = set()
    scoring_bases = set()
    horizons = set()
    for period, _, league_scoring, source_scoring, capture_method, *_ in ecr_semantics:
        providers.add("fantasypros_latest_ecr")
        scoring_bases.add(
            f"fantasypros_ecr:{league_scoring}:{source_scoring}:{capture_method}"
        )
        horizons.add(f"fantasypros_ecr:{period}")
    for provider, horizon, scoring, point_basis, compatibility, *_ in projection_semantics:
        providers.add(provider)
        scoring_bases.add(
            f"{provider}:{scoring}:{point_basis}:{compatibility}"
        )
        horizons.add(f"{provider}:{horizon}")
    if not providers:
        providers.add("formula_constant_only")
        scoring_bases.add("not_applicable")
        horizons.add("not_applicable")
    contract = {
        "ecr_semantics": sorted(ecr_semantics),
        "ensemble_config_id": (
            bundle.ensemble_config.config_id
            if any(name.startswith("projection_ensemble_") for name in feature_names)
            else None
        ),
        "feature_names": list(feature_names),
        "projection_semantics": list(projection_semantics),
    }
    return {
        "providers": tuple(sorted(providers)),
        "scoring_bases": tuple(sorted(scoring_bases)),
        "horizons": tuple(sorted(horizons)),
        "contract_id": content_id("power-input-contract", contract),
    }


def _independent_power_source_contract(bundle, feature_names):
    providers = bundle.independent_power_disclosure.provider_names
    semantics = sorted(
        {
            (
                row.provider,
                "rest_of_season"
                if isinstance(row, RemainingSeasonProjection)
                else "weekly",
                row.origin.value,
            )
            for row in bundle.projection_evidence
            if row.provider in providers
        }
    )
    contract = {
        "feature_names": list(feature_names),
        "policy_id": bundle.independent_power_disclosure.policy_id,
        "projection_semantics": semantics,
        "provider_names": list(providers),
        "scoring_profile_id": bundle.state.scoring_profile_id,
    }
    return {
        "providers": providers,
        "scoring_bases": tuple(
            f"{provider}:normalized_points:{bundle.state.scoring_profile_id}"
            for provider in providers
        ),
        "horizons": tuple(
            sorted({f"{provider}:{horizon}" for provider, horizon, _ in semantics})
        ),
        "contract_id": content_id("independent-power-input-contract", contract),
    }


def _projection_scope(bundle, feature_names):
    configured = tuple(
        row.provider for row in bundle.ensemble_config.provider_weights
    )
    scope = set()
    for name in feature_names:
        if not name.startswith("projection_"):
            continue
        providers = (
            configured
            if name.startswith("projection_ensemble_")
            else tuple(
                provider
                for provider in configured
                if name.startswith(f"projection_{provider}_")
            )
        )
        if "full_ros" in name:
            horizons = (RankingHorizon.ROS,)
        elif "current" in name or "observed_week_fraction" in name or "uncertainty" in name:
            horizons = (RankingHorizon.WEEKLY,)
        else:
            horizons = (RankingHorizon.WEEKLY, RankingHorizon.ROS)
        scope.update((provider, horizon) for provider in providers for horizon in horizons)
    return scope


__all__ = (
    "FORESIGHT_POWER_STATUS",
    "GmModelEvidence",
    "POWER_RESULT_STATUSES",
    "build_gm_model_evidence",
    "model_comparability_reasons",
)
