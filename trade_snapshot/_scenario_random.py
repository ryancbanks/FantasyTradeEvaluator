"""Canonical identities and stateless draws for score scenarios."""

from hashlib import sha256
import json
import math
from typing import Mapping, Sequence


DRAW_ALGORITHM = "sha256-box-muller-factor-floor-zero-v1"
SAFE_INTEGER = (1 << 53) - 1


def require_json_int(name: str, value: object, *, minimum: int) -> None:
    if type(value) is not int or value < minimum or value > SAFE_INTEGER:
        raise ValueError(f"{name} must be a JSON-safe integer of at least {minimum}")


def require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def content_id(prefix: str, payload: Mapping[str, object]) -> str:
    """Hash a JSON-safe payload using one stable canonical representation."""

    encoded = canonical_json(payload).encode("utf-8")
    return f"{prefix}_{sha256(encoded).hexdigest()}"


def canonical_json(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("identity payload must contain only finite JSON values") from None


def standard_normal(
    draw_space_id: str,
    scenario_index: int,
    component: str,
    parts: Sequence[str | int],
) -> float:
    """Return a deterministic standard normal without mutable random state."""

    key = canonical_json(
        {
            "algorithm": DRAW_ALGORITHM,
            "component": component,
            "draw_space_id": draw_space_id,
            "parts": list(parts),
            "scenario_index": scenario_index,
        }
    ).encode("utf-8")
    digest = sha256(key).digest()
    scale = float(1 << 64)
    first = (int.from_bytes(digest[:8], "big") + 0.5) / scale
    second = (int.from_bytes(digest[8:16], "big") + 0.5) / scale
    return math.sqrt(-2.0 * math.log(first)) * math.cos(math.tau * second)
