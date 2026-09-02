"""Immutable domain types for sanitized FantasyPros analyzer observations."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
from math import isfinite
from numbers import Real
import re
from urllib.parse import urlsplit


class AnalyzerContractError(ValueError):
    """A trade request, provider response, or sanitized record is invalid."""


class AnalyzerPeriod(str, Enum):
    """Semantic period values sent by the current analyzer client."""

    ROS = "ros"
    PRE = "pre"
    DYN = "dyn"


_RESPONSE_PERIOD_KEYS = {
    AnalyzerPeriod.ROS: "ros",
    AnalyzerPeriod.PRE: "ros",
    AnalyzerPeriod.DYN: "dynasty",
}
_ASSET_FIELDS = (
    "team1_gets",
    "team2_gets",
    "team1_adds",
    "team2_adds",
    "team1_drops",
    "team2_drops",
)


@dataclass(frozen=True)
class AnalyzerTradeRequest:
    """One trade's meaning, with no transport, account, or session data."""

    period: AnalyzerPeriod
    team1_id: str
    team2_id: str
    team1_gets: tuple[str, ...]
    team2_gets: tuple[str, ...]
    team1_adds: tuple[str, ...] = ()
    team2_adds: tuple[str, ...] = ()
    team1_drops: tuple[str, ...] = ()
    team2_drops: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            period = AnalyzerPeriod(self.period)
        except (TypeError, ValueError):
            raise AnalyzerContractError("period must be ros, pre, or dyn") from None
        object.__setattr__(self, "period", period)

        team1_id = _canonical_id("team1_id", self.team1_id)
        team2_id = _canonical_id("team2_id", self.team2_id)
        if team1_id == team2_id:
            raise AnalyzerContractError("team1_id and team2_id must identify different teams")
        object.__setattr__(self, "team1_id", team1_id)
        object.__setattr__(self, "team2_id", team2_id)

        seen_assets: set[str] = set()
        for field_name in _ASSET_FIELDS:
            assets = _asset_ids(field_name, getattr(self, field_name))
            duplicate = next((asset for asset in assets if asset in seen_assets), None)
            if duplicate is not None:
                raise AnalyzerContractError(
                    f"asset ID {duplicate!r} appears more than once in the trade request"
                )
            seen_assets.update(assets)
            object.__setattr__(self, field_name, assets)
        if not self.team1_gets or not self.team2_gets:
            raise AnalyzerContractError("both trade packages must contain at least one asset")

    @property
    def response_period_key(self) -> str:
        return _RESPONSE_PERIOD_KEYS[self.period]


@dataclass(frozen=True)
class BundleFingerprint:
    """Public client bundle provenance, never an analyzer request URL."""

    url: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise AnalyzerContractError("bundle URL must identify a public FantasyPros bundle")
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError:
            raise AnalyzerContractError(
                "bundle URL must identify a public FantasyPros bundle"
            ) from None
        valid_url = (
            parsed.scheme == "https"
            and parsed.hostname == "cdn.fantasypros.com"
            and parsed.username is None
            and parsed.password is None
            and port is None
            and parsed.path.startswith("/assets/js/")
            and parsed.path.endswith(".js")
            and not parsed.query
            and not parsed.fragment
        )
        if not valid_url:
            raise AnalyzerContractError("bundle URL must identify a public FantasyPros bundle")
        if not isinstance(self.sha256, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", self.sha256
        ):
            raise AnalyzerContractError("bundle sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "sha256", self.sha256.casefold())


CURRENT_BUNDLE_FINGERPRINT = BundleFingerprint(
    url=(
        "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
        "trade-analyzer/bundle-95829a57d796c255512a.js"
    ),
    sha256="23f475081dadda2f352b4fb444a76b0dd8aa40a79e2be3d430c971934fb3d225",
)


@dataclass(frozen=True)
class PowerRankingChange:
    """Raw analyzer scores plus the client bundle's rounded display result."""

    team_id: str
    raw_before: float
    raw_after: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _canonical_id("team_id", self.team_id))
        object.__setattr__(self, "raw_before", _finite_number("raw_before", self.raw_before))
        object.__setattr__(self, "raw_after", _finite_number("raw_after", self.raw_after))

    @property
    def display_before_text(self) -> str:
        return _js_to_fixed_one(self.raw_before)

    @property
    def display_after_text(self) -> str:
        return _js_to_fixed_one(self.raw_after)

    @property
    def display_before(self) -> float:
        return float(self.display_before_text)

    @property
    def display_after(self) -> float:
        return float(self.display_after_text)

    @property
    def display_delta_text(self) -> str:
        return _js_to_fixed_one(self.display_after - self.display_before)

    @property
    def display_delta(self) -> float:
        return float(self.display_delta_text)


@dataclass(frozen=True)
class PlayoffOddsChange:
    """Raw percentages plus the full-analysis one-decimal presentation."""

    team_id: str
    raw_before: float
    raw_after: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _canonical_id("team_id", self.team_id))
        before = _finite_number("raw_before playoff odds", self.raw_before)
        after = _finite_number("raw_after playoff odds", self.raw_after)
        if not 0 <= before <= 100 or not 0 <= after <= 100:
            raise AnalyzerContractError("playoff odds must be percentages from 0 through 100")
        object.__setattr__(self, "raw_before", before)
        object.__setattr__(self, "raw_after", after)

    @property
    def display_before_text(self) -> str:
        return _js_to_fixed_one(self.raw_before)

    @property
    def display_after_text(self) -> str:
        return _js_to_fixed_one(self.raw_after)

    @property
    def display_before(self) -> float:
        return float(self.display_before_text)

    @property
    def display_after(self) -> float:
        return float(self.display_after_text)

    @property
    def display_delta_text(self) -> str:
        return _js_to_fixed_one(self.raw_after - self.raw_before)

    @property
    def display_delta(self) -> float:
        return float(self.display_delta_text)


@dataclass(frozen=True)
class PowerRankingObservation:
    semantic_period: AnalyzerPeriod
    response_period_key: str
    team1: PowerRankingChange
    team2: PowerRankingChange

    def __post_init__(self) -> None:
        try:
            period = AnalyzerPeriod(self.semantic_period)
        except (TypeError, ValueError):
            raise AnalyzerContractError("power observation period is invalid") from None
        if self.response_period_key != _RESPONSE_PERIOD_KEYS[period]:
            raise AnalyzerContractError("power observation has the wrong response period")
        if not isinstance(self.team1, PowerRankingChange) or not isinstance(
            self.team2, PowerRankingChange
        ):
            raise AnalyzerContractError("power observation must contain both team changes")
        object.__setattr__(self, "semantic_period", period)


@dataclass(frozen=True)
class PlayoffOddsObservation:
    team1: PlayoffOddsChange
    team2: PlayoffOddsChange

    def __post_init__(self) -> None:
        if not isinstance(self.team1, PlayoffOddsChange) or not isinstance(
            self.team2, PlayoffOddsChange
        ):
            raise AnalyzerContractError("playoff observation must contain both team changes")


@dataclass(frozen=True)
class AnalyzerObservation:
    request: AnalyzerTradeRequest
    power: PowerRankingObservation
    playoffs: PlayoffOddsObservation | None = None
    bundle: BundleFingerprint = CURRENT_BUNDLE_FINGERPRINT

    def __post_init__(self) -> None:
        if not isinstance(self.request, AnalyzerTradeRequest):
            raise AnalyzerContractError("observation request is invalid")
        if not isinstance(self.power, PowerRankingObservation):
            raise AnalyzerContractError("observation power result is invalid")
        if self.playoffs is not None and not isinstance(
            self.playoffs, PlayoffOddsObservation
        ):
            raise AnalyzerContractError("observation playoff result is invalid")
        if not isinstance(self.bundle, BundleFingerprint):
            raise AnalyzerContractError("observation bundle fingerprint is invalid")
        if self.power.semantic_period is not self.request.period:
            raise AnalyzerContractError("power result does not match the request period")
        expected_team_ids = (self.request.team1_id, self.request.team2_id)
        if (self.power.team1.team_id, self.power.team2.team_id) != expected_team_ids:
            raise AnalyzerContractError("power result teams do not match the request")
        if self.playoffs is not None and (
            self.playoffs.team1.team_id,
            self.playoffs.team2.team_id,
        ) != expected_team_ids:
            raise AnalyzerContractError("playoff result teams do not match the request")


def _js_to_fixed_one(value: float) -> str:
    """Match Number.toFixed(1) for the analyzer's finite score range."""

    if value == 0:
        return "0.0"
    if abs(value) >= 1e21:
        return repr(value).replace("e+0", "e+").replace("e-0", "e-")
    decimal = Decimal.from_float(value)
    with localcontext() as context:
        context.prec = max(32, decimal.adjusted() + 8)
        rounded = decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(rounded, ".1f")


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AnalyzerContractError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise AnalyzerContractError(f"{name} must be a finite number") from None
    if not isfinite(normalized):
        raise AnalyzerContractError(f"{name} must be a finite number")
    return normalized


def _canonical_id(name: str, value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AnalyzerContractError(f"{name} must be a non-empty string or integer ID")
    normalized = str(value)
    if not normalized or normalized != normalized.strip():
        raise AnalyzerContractError(f"{name} must be a non-empty ID without outer whitespace")
    if any(marker in normalized for marker in ("/", "\\", "?", "=", "%", ";", " ")):
        raise AnalyzerContractError(
            f"{name} cannot contain a URL, query, cookie, or transport data"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
        raise AnalyzerContractError(f"{name} must use only portable provider-ID characters")
    return normalized


def _asset_ids(name: str, values: Iterable[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise AnalyzerContractError(f"{name} must be an iterable of asset IDs")
    try:
        normalized = tuple(_canonical_id(f"{name} asset", value) for value in values)
    except TypeError:
        raise AnalyzerContractError(f"{name} must be an iterable of asset IDs") from None
    if len(set(normalized)) != len(normalized):
        raise AnalyzerContractError(f"{name} contains a duplicate asset ID")
    return normalized


def _require_request(request: object) -> None:
    if not isinstance(request, AnalyzerTradeRequest):
        raise AnalyzerContractError("request must be an AnalyzerTradeRequest")


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnalyzerContractError(f"{name} must be an object")
    return value


def _required_mapping(
    parent: Mapping[str, object],
    key: str,
    parent_name: str,
) -> Mapping[str, object]:
    if key not in parent:
        raise AnalyzerContractError(f"{parent_name} is missing {key}")
    return _mapping(f"{parent_name}.{key}", parent[key])
