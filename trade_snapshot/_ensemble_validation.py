"""Private immutable-value and context validation for projection ensembles."""

from collections.abc import Mapping
from dataclasses import dataclass
import math
from numbers import Real

from .projections import ProjectionStatus
from .positions import normalize_player_position


@dataclass(frozen=True, slots=True)
class FrozenFloors(Mapping[str, float]):
    _items: tuple[tuple[str, float], ...]

    def __getitem__(self, key: str) -> float:
        for position, value in self._items:
            if position == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (position for position, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, memo):
        return self


def freeze_floors(values: Mapping[str, float]) -> FrozenFloors:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("position_stddev_floors must be a non-empty mapping")
    copied: dict[str, float] = {}
    for position, value in values.items():
        key = normalize_position(position)
        if key in copied:
            raise ValueError(f"duplicate position uncertainty floor: {key}")
        number = finite_float("position uncertainty floor", value)
        if number < 0:
            raise ValueError("position uncertainty floors must be finite and nonnegative")
        copied[key] = number
    return FrozenFloors(tuple(sorted(copied.items())))


def validate_game_context(projection: object) -> None:
    for name in ("nfl_team_id", "nfl_game_id", "opponent_team_id"):
        value = getattr(projection, name)
        if value is not None:
            require_nonempty_string(name, value)
    is_home = getattr(projection, "is_home")
    if is_home is not None and not isinstance(is_home, bool):
        raise ValueError("is_home must be a boolean or None")
    game = (
        getattr(projection, "nfl_game_id"),
        getattr(projection, "opponent_team_id"),
        is_home,
    )
    team = getattr(projection, "nfl_team_id")
    if any(value is not None for value in game) and not all(
        value is not None for value in game
    ):
        raise ValueError("NFL game context requires game, opponent, and home/away")
    if any(value is not None for value in game) and team is None:
        raise ValueError("NFL game context requires nfl_team_id")
    if game[1] is not None and game[1] == team:
        raise ValueError("opponent_team_id cannot equal nfl_team_id")
    if getattr(projection, "status") is ProjectionStatus.BYE:
        if team is None or any(value is not None for value in game):
            raise ValueError("bye ensemble requires a team and no game context")


def normalize_position(value: object) -> str:
    return normalize_player_position(value)


def require_nonempty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def require_int(
    name: str,
    value: object,
    minimum: int,
    maximum: int | None,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{name} is outside the supported range")


def is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def finite_float(name: str, value: object) -> float:
    if not is_finite_number(value):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number
