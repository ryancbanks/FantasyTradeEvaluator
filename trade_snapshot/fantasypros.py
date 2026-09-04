import json
from collections.abc import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .model import DatasetPayload


BASE_URL = "https://api.fantasypros.com/public/v2/json"


class FantasyProsError(RuntimeError):
    """A sanitized failure returned by the official FantasyPros API boundary."""


def fetch_datasets(
    *,
    api_key: str,
    season: int,
    week: int,
    scoring: str,
    timeout: float = 30,
    opener: Callable = urlopen,
) -> tuple[DatasetPayload, ...]:
    """Fetch ROS ECR, ROS projections, and ESPN/Yahoo player ID crosswalks."""

    if not api_key:
        raise ValueError("api_key is required")

    specifications = (
        (
            "ecr",
            f"nfl/{season}/consensus-rankings",
            {"position": "ALL", "scoring": scoring, "type": "ROS", "week": week},
        ),
        (
            "projections",
            f"nfl/{season}/projections",
            {"position": "ALL", "week": week, "ros": "true"},
        ),
        (
            "players",
            "nfl/players",
            {"ecr": "included", "external_ids": "espn:yahoo"},
        ),
    )

    datasets = []
    for name, path, query in specifications:
        endpoint = f"{BASE_URL}/{path}?{urlencode(query)}"
        request = Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "User-Agent": "fantasy-trade-evaluator/0.2.0",
                "x-api-key": api_key,
            },
        )
        try:
            with opener(request, timeout=timeout) as response:
                payload = _decode_json(response.read(), name)
                _validate_payload(name, payload, season=season, week=week)
                metadata = _source_metadata(name, payload, response.headers, endpoint)
        except FantasyProsError:
            raise
        except HTTPError as error:
            raise FantasyProsError(f"FantasyPros {name} request failed with HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise FantasyProsError(f"FantasyPros {name} request failed") from None

        datasets.append(DatasetPayload(name=name, payload=payload, source_metadata=metadata))

    return tuple(datasets)


def _decode_json(body: bytes, dataset_name: str) -> Mapping:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FantasyProsError(f"FantasyPros {dataset_name} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise FantasyProsError(f"FantasyPros {dataset_name} returned an unexpected JSON shape")
    return payload


def _validate_payload(name: str, payload: Mapping, *, season: int, week: int) -> None:
    players = payload.get("players")
    if not isinstance(players, list):
        raise FantasyProsError(f"FantasyPros {name} returned an unexpected JSON shape")

    returned_season = payload.get("season", payload.get("year"))
    if returned_season is not None and str(returned_season) != str(season):
        raise FantasyProsError(f"FantasyPros {name} returned the wrong season")

    returned_week = payload.get("week")
    if returned_week is not None and str(returned_week) != str(week):
        raise FantasyProsError(f"FantasyPros {name} returned the wrong week")


def _source_metadata(
    name: str,
    payload: Mapping,
    headers: Mapping,
    endpoint: str,
) -> dict[str, object]:
    fields: dict[str, Iterable[str]] = {
        "ecr": ("year", "week", "last_updated", "last_updated_ts", "total_experts", "count"),
        "projections": ("season", "week", "scoring", "positions", "count", "experts"),
        "players": ("sport", "season", "week", "count"),
    }
    metadata = {
        "endpoint": endpoint,
        "response": {field: payload[field] for field in fields[name] if field in payload},
    }
    http_metadata = {}
    for header in ("Date", "ETag", "Last-Modified"):
        value = headers.get(header)
        if value:
            http_metadata[header.lower().replace("-", "_")] = value
    if http_metadata:
        metadata["http"] = http_metadata
    return metadata
