"""Cycle-free rules for providers allowed to contribute numeric projections."""

from collections.abc import Iterable


INDEPENDENT_PROJECTION_PROVIDERS = (
    "espn",
    "yahoo",
    "cbs",
    "fftoday",
    "fantasysharks",
)
COMPOSITE_PROJECTION_PROVIDERS = frozenset({"fantasypros", "ffa"})
REFERENCE_ONLY_PROJECTION_PROVIDERS = frozenset({"ffa"})
SUPPORTED_PROJECTION_PROVIDERS = frozenset(
    (*INDEPENDENT_PROJECTION_PROVIDERS, "fantasypros")
)
RECOGNIZED_PROJECTION_PROVIDERS = (
    SUPPORTED_PROJECTION_PROVIDERS | REFERENCE_ONLY_PROJECTION_PROVIDERS
)

_PROVIDER_ORDER = {
    name: index
    for index, name in enumerate(
        ("fantasypros", *INDEPENDENT_PROJECTION_PROVIDERS, "ffa")
    )
}


def normalize_projection_providers(values: Iterable[str]) -> tuple[str, ...]:
    """Validate provider-name shape and return deterministic provider order."""

    if isinstance(values, (str, bytes)):
        raise ValueError("projection providers must be an iterable")
    try:
        providers = tuple(values)
    except TypeError:
        raise ValueError("projection providers must be an iterable") from None
    if not providers or any(
        not isinstance(value, str) or not value or value != value.casefold()
        for value in providers
    ):
        raise ValueError("projection providers must be lowercase names")
    if len(set(providers)) != len(providers):
        raise ValueError("projection providers contain a duplicate")
    return tuple(
        sorted(providers, key=lambda value: (_PROVIDER_ORDER.get(value, 999), value))
    )


def validate_selectable_projection_provider(provider: object) -> str:
    """Reject one unknown or non-calculation projection provider."""

    return validate_selectable_projection_providers((provider,))[0]


def validate_selectable_projection_providers(
    providers: Iterable[str],
) -> tuple[str, ...]:
    """Normalize and reject providers that may not supply calculation values."""

    normalized = normalize_projection_providers(providers)
    values = set(normalized)
    excluded = values.intersection(REFERENCE_ONLY_PROJECTION_PROVIDERS)
    if excluded:
        names = ", ".join(sorted(excluded))
        raise ValueError(
            f"reference-only projection provider(s) cannot be selected: {names}"
        )
    unknown = values.difference(SUPPORTED_PROJECTION_PROVIDERS)
    if unknown:
        raise ValueError(f"unsupported projection provider {min(unknown)!r}")
    return normalized


def validate_no_composite_double_count(providers: Iterable[str]) -> None:
    """Reject arithmetic that mixes a consensus product with likely constituents."""

    values = set(normalize_projection_providers(providers))
    # Preserve the application's established FantasyPros/ESPN/Yahoo ensemble.
    # New broad-consensus modes exclude composite FantasyPros projections, but
    # schema-v7 bundles and the explicit core mode remain valid and loadable.
    if values == {"fantasypros", "espn", "yahoo"}:
        return
    composites = values.intersection(COMPOSITE_PROJECTION_PROVIDERS)
    independent = values.difference(COMPOSITE_PROJECTION_PROVIDERS)
    if composites and independent:
        raise ValueError(
            "composite projections cannot be averaged with independent source projections"
        )
    if len(composites) > 1:
        raise ValueError("multiple composite projection products cannot be averaged")


__all__ = (
    "COMPOSITE_PROJECTION_PROVIDERS",
    "INDEPENDENT_PROJECTION_PROVIDERS",
    "RECOGNIZED_PROJECTION_PROVIDERS",
    "REFERENCE_ONLY_PROJECTION_PROVIDERS",
    "SUPPORTED_PROJECTION_PROVIDERS",
    "normalize_projection_providers",
    "validate_no_composite_double_count",
    "validate_selectable_projection_provider",
    "validate_selectable_projection_providers",
)
