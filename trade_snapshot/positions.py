"""Shared cross-provider fantasy-football position normalization."""


_ALIASES = {
    "D/ST": "DST",
    "DEF": "DST",
    "PK": "K",
    "FB": "RB",
    "DE": "DL",
    "DT": "DL",
    "NT": "DL",
    "EDGE": "DL",
    "ILB": "LB",
    "OLB": "LB",
    "MLB": "LB",
    "CB": "DB",
    "S": "DB",
    "FS": "DB",
    "SS": "DB",
}

CANONICAL_PLAYER_POSITIONS = frozenset(
    {"QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB", "IDP"}
)
FLEX_SLOTS = frozenset({"FLEX", "FLX", "SUPERFLEX", "SFLX", "OP", "UTIL"})


def normalize_player_position(value: object, *, require_supported: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("position must be a non-empty string")
    raw = value.strip().upper().replace(" ", "")
    normalized = _ALIASES.get(raw, raw)
    if require_supported and normalized not in CANONICAL_PLAYER_POSITIONS:
        raise ValueError(f"unsupported player position {value!r}")
    return normalized


def normalize_lineup_slot(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("lineup slot must be a non-empty string")
    raw = value.strip().upper().replace(" ", "")
    if raw in FLEX_SLOTS:
        return {"FLX": "FLEX", "SUPERFLEX": "SFLX"}.get(raw, raw)
    return normalize_player_position(raw)


def projection_position_in_scope(position: object, scope) -> bool:
    """Return whether a canonical player position belongs to a page scope."""

    normalized = normalize_player_position(position, require_supported=True)
    try:
        values = frozenset(scope)
    except TypeError:
        raise ValueError("projection position scope must be an iterable") from None
    return (
        "ALL" in values
        or normalized in values
        or ("FLX" in values and normalized in {"RB", "WR", "TE"})
        or ("IDP" in values and normalized in {"DL", "LB", "DB"})
    )


__all__ = (
    "CANONICAL_PLAYER_POSITIONS",
    "FLEX_SLOTS",
    "normalize_lineup_slot",
    "normalize_player_position",
    "projection_position_in_scope",
)
