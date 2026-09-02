"""Strict content-addressed configuration for score scenarios."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import fsum, isclose, isfinite
from numbers import Real

from ._scenario_random import (
    DRAW_ALGORITHM,
    SAFE_INTEGER,
    content_id,
    require_json_int,
    require_text,
)


@dataclass(frozen=True, slots=True)
class PlayerEligibility:
    """One canonical player's exact set of legal fantasy-lineup slots."""

    canonical_player_id: str
    eligible_slots: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text("canonical_player_id", self.canonical_player_id)
        if isinstance(self.eligible_slots, (str, bytes)):
            raise ValueError("eligible_slots must be an iterable of slot names")
        try:
            slots = tuple(self.eligible_slots)
        except TypeError:
            raise ValueError("eligible_slots must be an iterable") from None
        if not slots or any(not isinstance(slot, str) or not slot for slot in slots):
            raise ValueError("eligible_slots must contain non-empty strings")
        if len(set(slots)) != len(slots):
            raise ValueError("eligible_slots cannot contain duplicates")
        object.__setattr__(self, "eligible_slots", tuple(sorted(slots)))


@dataclass(frozen=True, slots=True)
class FactorLoadings:
    """Nonnegative factor coefficients whose squared values sum to one."""

    league: float
    game: float
    nfl_team: float
    player: float

    def __post_init__(self) -> None:
        names = ("league", "game", "nfl_team", "player")
        values = tuple(
            _finite_nonnegative(name, getattr(self, name)) for name in names
        )
        if not isclose(
            fsum(value * value for value in values),
            1.0,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise ValueError("squared factor loadings must sum to one")
        for name, value in zip(names, values):
            object.__setattr__(self, name, value)

    def to_record(self) -> dict[str, float]:
        return {
            "game": self.game,
            "league": self.league,
            "nfl_team": self.nfl_team,
            "player": self.player,
        }


@dataclass(frozen=True, slots=True)
class CorrelatedScenarioConfig:
    """Content-addressed stochastic settings for a finite scenario stream."""

    scenario_count: int
    seed: int
    loadings: FactorLoadings
    config_id: str = field(init=False)

    def __post_init__(self) -> None:
        require_json_int("scenario_count", self.scenario_count, minimum=1)
        require_json_int("seed", self.seed, minimum=-SAFE_INTEGER)
        if not isinstance(self.loadings, FactorLoadings):
            raise ValueError("loadings must be FactorLoadings")
        object.__setattr__(self, "config_id", content_id("scfg", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        return {
            "algorithm": DRAW_ALGORITHM,
            "loadings": self.loadings.to_record(),
            "scenario_count": self.scenario_count,
            "seed": self.seed,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "correlated_scenario_config",
            "schema_version": 1,
            **self._content_record(),
            "config_id": self.config_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "CorrelatedScenarioConfig":
        keys = {
            "algorithm",
            "config_id",
            "kind",
            "loadings",
            "scenario_count",
            "schema_version",
            "seed",
        }
        if not isinstance(record, Mapping) or set(record) != keys:
            raise ValueError("scenario config record fields are invalid")
        if (
            record["kind"] != "correlated_scenario_config"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["algorithm"] != DRAW_ALGORITHM
        ):
            raise ValueError(
                "scenario config record kind, version, or algorithm is invalid"
            )
        raw = record["loadings"]
        if not isinstance(raw, Mapping) or set(raw) != {
            "league",
            "game",
            "nfl_team",
            "player",
        }:
            raise ValueError("factor loading record fields are invalid")
        config = cls(
            scenario_count=record["scenario_count"],
            seed=record["seed"],
            loadings=FactorLoadings(
                league=raw["league"],
                game=raw["game"],
                nfl_team=raw["nfl_team"],
                player=raw["player"],
            ),
        )
        if record["config_id"] != config.config_id:
            raise ValueError("scenario config_id does not match its content")
        return config


def _finite_nonnegative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} loading must be a finite nonnegative number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} loading must be a finite nonnegative number") from None
    if not isfinite(result) or result < 0:
        raise ValueError(f"{name} loading must be a finite nonnegative number")
    return 0.0 if result == 0 else result
