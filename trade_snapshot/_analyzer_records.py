"""Strict JSON persistence for sanitized analyzer observations."""

from collections.abc import Mapping
import json
from math import isfinite

from ._analyzer_types import (
    _ASSET_FIELDS,
    AnalyzerContractError,
    AnalyzerObservation,
    AnalyzerTradeRequest,
    BundleFingerprint,
    PlayoffOddsChange,
    PlayoffOddsObservation,
    PowerRankingChange,
    PowerRankingObservation,
    _mapping,
)


def observation_to_record(observation: AnalyzerObservation) -> dict[str, object]:
    """Return a strict JSON-ready record containing no request transport data."""

    if not isinstance(observation, AnalyzerObservation):
        raise AnalyzerContractError("observation must be an AnalyzerObservation")
    request = observation.request
    record: dict[str, object] = {
        "schema_version": 1,
        "bundle": {
            "url": observation.bundle.url,
            "sha256": observation.bundle.sha256,
        },
        "request": {
            "period": request.period.value,
            "team1_id": request.team1_id,
            "team2_id": request.team2_id,
            **{field: list(getattr(request, field)) for field in _ASSET_FIELDS},
        },
        "power": {
            "semantic_period": observation.power.semantic_period.value,
            "response_period": observation.power.response_period_key,
            "team1": _change_record(observation.power.team1),
            "team2": _change_record(observation.power.team2),
        },
        "playoffs": (
            {
                "team1": _change_record(observation.playoffs.team1),
                "team2": _change_record(observation.playoffs.team2),
            }
            if observation.playoffs is not None
            else None
        ),
    }
    _require_json_safe(record)
    return record


def observation_from_record(record: Mapping[str, object]) -> AnalyzerObservation:
    """Rebuild a validated observation and reject secrets, URLs, and extra fields."""

    _require_json_safe(record)
    record = _mapping("analyzer observation record", record)
    _reject_forbidden_record_keys(record)
    _exact_fields(
        record,
        {"schema_version", "bundle", "request", "power", "playoffs"},
        "analyzer observation record",
    )
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise AnalyzerContractError("unsupported analyzer observation schema_version")

    bundle_record = _mapping("bundle record", record["bundle"])
    _exact_fields(bundle_record, {"url", "sha256"}, "bundle record")
    bundle = BundleFingerprint(bundle_record["url"], bundle_record["sha256"])

    request_record = _mapping("request record", record["request"])
    request_fields = {"period", "team1_id", "team2_id", *_ASSET_FIELDS}
    _exact_fields(request_record, request_fields, "request record")
    request = AnalyzerTradeRequest(
        period=request_record["period"],
        team1_id=request_record["team1_id"],
        team2_id=request_record["team2_id"],
        **{field: request_record[field] for field in _ASSET_FIELDS},
    )

    power_record = _mapping("power record", record["power"])
    _exact_fields(
        power_record,
        {"semantic_period", "response_period", "team1", "team2"},
        "power record",
    )
    power = PowerRankingObservation(
        semantic_period=power_record["semantic_period"],
        response_period_key=power_record["response_period"],
        team1=_change_from_record(power_record["team1"], PowerRankingChange),
        team2=_change_from_record(power_record["team2"], PowerRankingChange),
    )

    playoff_record = record["playoffs"]
    playoffs = None
    if playoff_record is not None:
        playoff_record = _mapping("playoffs record", playoff_record)
        _exact_fields(playoff_record, {"team1", "team2"}, "playoffs record")
        playoffs = PlayoffOddsObservation(
            team1=_change_from_record(playoff_record["team1"], PlayoffOddsChange),
            team2=_change_from_record(playoff_record["team2"], PlayoffOddsChange),
        )
    return AnalyzerObservation(request, power, playoffs, bundle)


def _change_record(change: PowerRankingChange | PlayoffOddsChange) -> dict[str, object]:
    return {
        "team_id": change.team_id,
        "raw_before": change.raw_before,
        "raw_after": change.raw_after,
        "display_before": change.display_before_text,
        "display_after": change.display_after_text,
        "display_delta": change.display_delta_text,
    }


def _change_from_record(record: object, change_type):
    record = _mapping("metric change record", record)
    _exact_fields(
        record,
        {
            "team_id",
            "raw_before",
            "raw_after",
            "display_before",
            "display_after",
            "display_delta",
        },
        "metric change record",
    )
    change = change_type(record["team_id"], record["raw_before"], record["raw_after"])
    derived = (
        change.display_before_text,
        change.display_after_text,
        change.display_delta_text,
    )
    stored = (record["display_before"], record["display_after"], record["display_delta"])
    if stored != derived:
        raise AnalyzerContractError("metric change record has an invalid derived display value")
    return change


def _exact_fields(record: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(record) != expected:
        raise AnalyzerContractError(f"{name} fields do not match the contract")


_SECRET_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "csrftoken",
    "espns2",
    "key",
    "password",
    "refreshtoken",
    "secret",
    "sessionid",
    "setcookie",
    "swid",
    "token",
    "xapikey",
    "xsrftoken",
}
_REQUEST_URL_KEYS = {"apiurl", "endpointurl", "requesturi", "requesturl"}


def _reject_forbidden_record_keys(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized in _SECRET_KEYS:
                raise AnalyzerContractError(
                    f"analyzer observation contains a forbidden secret-like key: {key}"
                )
            if normalized in _REQUEST_URL_KEYS or (
                normalized == "url" and path != ("bundle",)
            ):
                raise AnalyzerContractError("analyzer observation cannot contain a request URL")
            _reject_forbidden_record_keys(child, path + (str(key),))
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_record_keys(child, path)


def _require_json_safe(value: object) -> None:
    def validate_json_tree(node: object) -> None:
        if node is None or isinstance(node, (str, bool, int)):
            return
        if isinstance(node, float):
            if not isfinite(node):
                raise TypeError
            return
        if isinstance(node, list):
            for child in node:
                validate_json_tree(child)
            return
        if isinstance(node, dict) and all(isinstance(key, str) for key in node):
            for child in node.values():
                validate_json_tree(child)
            return
        raise TypeError

    try:
        validate_json_tree(value)
        json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError, OverflowError):
        raise AnalyzerContractError(
            "analyzer observation record must contain only finite JSON-safe values"
        ) from None
