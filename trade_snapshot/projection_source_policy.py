"""Select projection inputs without counting an aggregate and its ingredients twice."""

from collections.abc import Iterable
from dataclasses import dataclass

from .ensemble import EnsembleConfig
from .methodology import default_projection_ensemble
from .projection_provider_rules import (
    COMPOSITE_PROJECTION_PROVIDERS,
    INDEPENDENT_PROJECTION_PROVIDERS,
    RECOGNIZED_PROJECTION_PROVIDERS,
    SUPPORTED_PROJECTION_PROVIDERS,
    normalize_projection_providers,
    validate_no_composite_double_count,
    validate_selectable_projection_providers,
)


@dataclass(frozen=True, slots=True)
class ProjectionSourceSelection:
    """The provider set that is allowed to contribute to one forecast mean."""

    providers: tuple[str, ...]
    minimum_observed_sources: int
    mode: str

    def __post_init__(self) -> None:
        providers = validate_selectable_projection_providers(self.providers)
        if self.mode not in {
            "broad_consensus",
            "composite_fallback",
            "core_ensemble",
            "single_source",
        }:
            raise ValueError("projection source selection mode is invalid")
        if (
            type(self.minimum_observed_sources) is not int
            or not 1 <= self.minimum_observed_sources <= len(providers)
        ):
            raise ValueError("projection source quorum is invalid")
        validate_no_composite_double_count(providers)
        if self.mode == "broad_consensus":
            if len(providers) < 2 or self.minimum_observed_sources < 2:
                raise ValueError("broad consensus requires at least two independent sources")
            if any(provider in COMPOSITE_PROJECTION_PROVIDERS for provider in providers):
                raise ValueError("broad consensus may contain only independent publishers")
        object.__setattr__(self, "providers", providers)

    def ensemble_config(self) -> EnsembleConfig:
        return default_projection_ensemble(
            self.providers,
            minimum_observed_sources=self.minimum_observed_sources,
        )


def select_projection_sources(
    captured_providers: Iterable[str],
    *,
    broad_consensus: bool,
    fantasypros_available: bool,
) -> ProjectionSourceSelection:
    """Choose one-vote-per-publisher inputs from the sources actually captured."""

    captured = set(normalize_projection_providers(captured_providers))
    unknown = captured.difference(RECOGNIZED_PROJECTION_PROVIDERS)
    if unknown:
        raise ValueError(f"unsupported projection provider {min(unknown)!r}")
    if not isinstance(broad_consensus, bool) or not isinstance(
        fantasypros_available, bool
    ):
        raise ValueError("projection source policy flags must be booleans")
    if fantasypros_available != ("fantasypros" in captured):
        raise ValueError("FantasyPros availability does not match captured projections")

    if broad_consensus:
        providers = tuple(
            provider
            for provider in INDEPENDENT_PROJECTION_PROVIDERS
            if provider in captured
        )
        if len(providers) < 2:
            raise ValueError(
                "Broad projection consensus needs at least two independent publishers. "
                "Only one usable source was captured; retry later or turn the consensus "
                "option off."
            )
        return ProjectionSourceSelection(providers, 2, "broad_consensus")

    core_independent = {"espn", "yahoo"}
    if not core_independent <= captured:
        raise ValueError("core forecasts require ESPN and Yahoo projections")
    if fantasypros_available:
        return ProjectionSourceSelection(
            ("fantasypros", "espn", "yahoo"), 2, "core_ensemble"
        )
    return ProjectionSourceSelection(("espn", "yahoo"), 2, "core_ensemble")


__all__ = (
    "COMPOSITE_PROJECTION_PROVIDERS",
    "INDEPENDENT_PROJECTION_PROVIDERS",
    "ProjectionSourceSelection",
    "SUPPORTED_PROJECTION_PROVIDERS",
    "select_projection_sources",
    "validate_no_composite_double_count",
    "validate_selectable_projection_providers",
)
