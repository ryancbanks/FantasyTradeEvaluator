"""Content-addressed provenance for an independent local power model."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

from ._scenario_random import content_id
from .strength import StrengthModel


INDEPENDENT_POWER_MODE = "independent"
INDEPENDENT_POWER_STATUS = "independent"
INDEPENDENT_POWER_NOTICE = (
    "Independent power model: roster power is calculated locally from the listed "
    "independent data providers and the recorded policy. It is not a FantasyPros "
    "score, and no output from this model is FantasyPros-exact."
)

_SCHEMA_VERSION = 1
_KIND = "independent_power_disclosure"
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class IndependentPowerDisclosure:
    """Bind one independent policy and its source set to a weekly strength model."""

    weekly_snapshot_id: str
    strength_model_id: str
    policy_id: str
    provider_names: tuple[str, ...]
    captured_at: datetime
    disclosure_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("weekly_snapshot_id", "strength_model_id", "policy_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        providers = _providers(self.provider_names)
        captured_at = _captured_at(self.captured_at)
        object.__setattr__(self, "provider_names", providers)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(
            self,
            "disclosure_id",
            content_id("independent-power-disclosure", self._content_record()),
        )

    @classmethod
    def from_strength_model(
        cls,
        strength_model: StrengthModel,
        *,
        policy_id: str,
        provider_names: Iterable[str],
        captured_at: datetime,
    ) -> "IndependentPowerDisclosure":
        """Create provenance already bound to the supplied weekly model."""

        if not isinstance(strength_model, StrengthModel):
            raise ValueError("strength_model must be a StrengthModel")
        return cls(
            weekly_snapshot_id=strength_model.snapshot_id,
            strength_model_id=strength_model.model_id,
            policy_id=policy_id,
            provider_names=provider_names,
            captured_at=captured_at,
        )

    @property
    def mode(self) -> str:
        return INDEPENDENT_POWER_MODE

    @property
    def status(self) -> str:
        return INDEPENDENT_POWER_STATUS

    @property
    def notice(self) -> str:
        return INDEPENDENT_POWER_NOTICE

    @property
    def current_evidence_id(self) -> str:
        return self.disclosure_id

    @property
    def current_evidence_at(self) -> datetime:
        return self.captured_at

    @property
    def current_holdout_count(self) -> int:
        return 0

    @property
    def validated_balanced_package_sizes(self) -> tuple[int, ...]:
        return ()

    def power_result_status(
        self,
        *,
        outgoing_count: int,
        incoming_count: int,
        has_roster_adjustment: bool,
    ) -> str:
        """Never imply exactness, regardless of trade shape."""

        _trade_shape(outgoing_count, incoming_count, has_roster_adjustment)
        return INDEPENDENT_POWER_STATUS

    def validate_bundle(
        self, *, snapshot_id: str, strength_model: StrengthModel
    ) -> None:
        """Fail closed if this provenance is detached from its weekly model."""

        snapshot_id = _text("snapshot_id", snapshot_id)
        if not isinstance(strength_model, StrengthModel):
            raise ValueError("strength_model must be a StrengthModel")
        if self.weekly_snapshot_id != snapshot_id:
            raise ValueError("independent disclosure does not match league snapshot")
        if strength_model.snapshot_id != snapshot_id:
            raise ValueError("strength model does not match league snapshot")
        if self.strength_model_id != strength_model.model_id:
            raise ValueError("independent disclosure does not match strength model")

    def _content_record(self) -> dict[str, object]:
        return {
            "captured_at": _iso(self.captured_at),
            "mode": INDEPENDENT_POWER_MODE,
            "notice": INDEPENDENT_POWER_NOTICE,
            "policy_id": self.policy_id,
            "provider_names": list(self.provider_names),
            "status": INDEPENDENT_POWER_STATUS,
            "strength_model_id": self.strength_model_id,
            "weekly_snapshot_id": self.weekly_snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": _KIND,
            "schema_version": _SCHEMA_VERSION,
            **self._content_record(),
            "disclosure_id": self.disclosure_id,
        }

    @classmethod
    def from_record(cls, record: object) -> "IndependentPowerDisclosure":
        fields = {
            "captured_at",
            "disclosure_id",
            "kind",
            "mode",
            "notice",
            "policy_id",
            "provider_names",
            "schema_version",
            "status",
            "strength_model_id",
            "weekly_snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("independent power disclosure fields are invalid")
        if (
            record["kind"] != _KIND
            or type(record["schema_version"]) is not int
            or record["schema_version"] != _SCHEMA_VERSION
            or record["mode"] != INDEPENDENT_POWER_MODE
            or record["status"] != INDEPENDENT_POWER_STATUS
            or record["notice"] != INDEPENDENT_POWER_NOTICE
            or not isinstance(record["provider_names"], list)
        ):
            raise ValueError("independent power disclosure schema is invalid")
        result = cls(
            weekly_snapshot_id=record["weekly_snapshot_id"],
            strength_model_id=record["strength_model_id"],
            policy_id=record["policy_id"],
            provider_names=tuple(record["provider_names"]),
            captured_at=_parse_time(record["captured_at"]),
        )
        if record["disclosure_id"] != result.disclosure_id:
            raise ValueError(
                "independent power disclosure content does not match disclosure_id"
            )
        return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _providers(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("provider_names must be a collection")
    try:
        providers = tuple(values)
    except TypeError:
        raise ValueError("provider_names must be a collection") from None
    if (
        not providers
        or any(
            not isinstance(value, str) or not _PROVIDER_NAME.fullmatch(value)
            for value in providers
        )
        or len(set(providers)) != len(providers)
    ):
        raise ValueError(
            "provider_names must contain distinct lowercase provider identifiers"
        )
    if any(value.startswith("fantasypros") for value in providers):
        raise ValueError("independent power cannot use FantasyPros as a provider")
    return tuple(sorted(providers))


def _captured_at(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("captured_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("captured_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("captured_at must be an ISO-8601 timestamp") from None
    return _captured_at(parsed)


def _trade_shape(outgoing: object, incoming: object, adjusted: object) -> None:
    if (
        type(outgoing) is not int
        or outgoing < 1
        or type(incoming) is not int
        or incoming < 1
        or not isinstance(adjusted, bool)
    ):
        raise ValueError("trade shape counts and adjustment flag are invalid")


__all__ = (
    "INDEPENDENT_POWER_MODE",
    "INDEPENDENT_POWER_NOTICE",
    "INDEPENDENT_POWER_STATUS",
    "IndependentPowerDisclosure",
)
