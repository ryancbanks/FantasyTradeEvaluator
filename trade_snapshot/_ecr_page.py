"""Validate the allowlisted FantasyPros ``ecrData`` bootstrap."""

from collections.abc import Mapping
from math import isfinite
import re

from .browser_capture import BrowserCaptureError, ECRCaptureData
from .capture_schema import ECRRankingRow, FantasyProsECRTask, RankingHorizon


_SOURCE_FIELDS = {
    "sport", "ranking_type", "type_text", "year", "week", "position", "scoring",
    "expert_ids", "expert_count", "last_updated", "player_count",
}
_ROW_FIELDS = {
    "player_id", "player_name", "team", "position", "rank_ecr", "rank_min",
    "rank_max", "rank_avg", "rank_std", "position_rank",
}


def ecr_capture_data(value: object, task: FantasyProsECRTask) -> ECRCaptureData:
    """Prove source dimensions and convert explicitly selected ranking fields."""

    if not isinstance(value, Mapping) or set(value) != {"source", "rankings"}:
        raise BrowserCaptureError("FantasyPros ECR bootstrap returned an invalid shape")
    source, raw_rows = value["source"], value["rankings"]
    if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
        raise BrowserCaptureError("FantasyPros ECR source provenance was incomplete")
    if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > 5000:
        raise BrowserCaptureError("FantasyPros ECR rankings were empty or exceeded limits")
    _validate_source_dimensions(source, task, len(raw_rows))
    experts = _experts(source, task)
    rows = tuple(_ranking_row(row) for row in raw_rows)
    if len({row.provider_player_id for row in rows}) != len(rows):
        raise BrowserCaptureError("FantasyPros ECR rankings repeated a player ID")
    allowed = _allowed_positions(source["position"], task.position_scope)
    if any(_position(row.position) not in allowed for row in rows):
        raise BrowserCaptureError("FantasyPros ECR rows did not match the selected position")
    update = _plain_text("last_updated", source["last_updated"], maximum=128)
    try:
        return ECRCaptureData(experts, len(experts), update, None, rows)
    except ValueError:
        raise BrowserCaptureError("FantasyPros ECR extraction failed strict validation") from None


def _validate_source_dimensions(source, task, row_count: int) -> None:
    if _plain_text("sport", source["sport"], maximum=8).upper() != "NFL":
        raise BrowserCaptureError("FantasyPros ECR source sport was not NFL")
    year = _integer(source["year"], "year", minimum=2000, maximum=2200)
    if year != task.season:
        raise BrowserCaptureError("FantasyPros ECR source season did not match the task")
    ranking_type = _plain_text("ranking_type", source["ranking_type"], maximum=32)
    normalized_type = re.sub(r"[^a-z]", "", ranking_type.casefold())
    expected = "weekly" if task.horizon is RankingHorizon.WEEKLY else "ros"
    aliases = {"weekly": "weekly", "ros": "ros", "restofseason": "ros"}
    if aliases.get(normalized_type) != expected:
        raise BrowserCaptureError("FantasyPros ECR ranking horizon did not match the task")
    if task.horizon is RankingHorizon.WEEKLY:
        week = _integer(source["week"], "week", minimum=1, maximum=25)
        if week != task.week:
            raise BrowserCaptureError("FantasyPros ECR source week did not match the task")
    scoring = _plain_text("scoring", source["scoring"], maximum=32).upper()
    scoring = {"STANDARD": "STD", "HALF PPR": "HALF"}.get(scoring, scoring)
    if scoring != task.scoring:
        raise BrowserCaptureError("FantasyPros ECR scoring did not match the task")
    type_text = _plain_text("type_text", source["type_text"], maximum=64).upper()
    if task.horizon is RankingHorizon.WEEKLY and "WEEK" not in type_text:
        raise BrowserCaptureError("FantasyPros ECR page type did not prove weekly rankings")
    if task.horizon is RankingHorizon.ROS and not ("REST" in type_text or "ROS" in type_text):
        raise BrowserCaptureError("FantasyPros ECR page type did not prove ROS rankings")
    count = _integer(source["player_count"], "player_count", minimum=1, maximum=5000)
    if count != row_count:
        raise BrowserCaptureError("FantasyPros ECR bootstrap was not complete")
    _allowed_positions(source["position"], task.position_scope)


def _experts(source, task) -> tuple[str, ...]:
    values = source["expert_ids"]
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value)
        for value in values
    ):
        raise BrowserCaptureError("FantasyPros consensus expert IDs were invalid")
    experts = tuple(sorted(values, key=int))
    count = _integer(source["expert_count"], "expert_count", minimum=1, maximum=10000)
    if count != len(experts) or len(set(experts)) != len(experts):
        raise BrowserCaptureError("FantasyPros consensus expert selection was not verifiable")
    if task.expert_ids and tuple(sorted(task.expert_ids, key=int)) != experts:
        raise BrowserCaptureError("FantasyPros consensus experts did not match the task")
    if task.expert_count is not None and task.expert_count != count:
        raise BrowserCaptureError("FantasyPros consensus expert count did not match the task")
    return experts


def _ranking_row(value: object) -> ECRRankingRow:
    if not isinstance(value, Mapping) or set(value) != _ROW_FIELDS:
        raise BrowserCaptureError("FantasyPros ECR ranking row shape was invalid")
    player_id = _decimal_id(value["player_id"])
    name = _plain_text("player_name", value["player_name"], maximum=200)
    team = value["team"]
    if team is not None:
        team = _plain_text("team", team, maximum=8).upper()
    position = _position(value["position"])
    position_rank = _plain_text("position_rank", value["position_rank"], maximum=16).upper()
    ranks = {
        "rank_ecr": _number(value["rank_ecr"], "rank_ecr"),
        "rank_min": _number(value["rank_min"], "rank_min"),
        "rank_max": _number(value["rank_max"], "rank_max"),
        "rank_avg": _number(value["rank_avg"], "rank_avg"),
        "rank_std": _number(value["rank_std"], "rank_std", zero=True),
    }
    visible = {
        "ECR": str(value["rank_ecr"]), "BEST": str(value["rank_min"]),
        "WORST": str(value["rank_max"]), "AVG": str(value["rank_avg"]),
        "STD DEV": str(value["rank_std"]), "POS": position_rank,
    }
    try:
        return ECRRankingRow(
            player_id, name, team, position, position_rank=position_rank,
            visible_values=visible, **ranks,
        )
    except ValueError:
        raise BrowserCaptureError("FantasyPros ECR ranking row was invalid") from None


def _allowed_positions(source: object, requested: tuple[str, ...]) -> set[str]:
    selected = _position(source)
    expected = {_position(value) for value in requested}
    source_set = {"RB", "WR", "TE"} if selected == "FLX" else (
        {"QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB"}
        if selected in {"ALL", "OVERALL"} else {selected}
    )
    if expected == {"FLX"}:
        expected = {"RB", "WR", "TE"}
    if expected == {"ALL"}:
        expected = source_set
    if expected != source_set:
        raise BrowserCaptureError("FantasyPros ECR position did not match the task")
    return source_set


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise BrowserCaptureError(f"FantasyPros ECR {name} was invalid")
    return value


def _number(value: object, name: str, *, zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BrowserCaptureError(f"FantasyPros ECR {name} was not numeric") from None
    if not isfinite(number) or number < 0 or (number == 0 and not zero):
        raise BrowserCaptureError(f"FantasyPros ECR {name} was outside its range")
    return number


def _decimal_id(value: object) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise BrowserCaptureError("FantasyPros ECR player ID was invalid")
    return value


def _position(value: object) -> str:
    text = _plain_text("position", value, maximum=16).upper()
    if not re.fullmatch(r"[A-Z]{1,8}", text):
        raise BrowserCaptureError("FantasyPros ECR position was invalid")
    return text


def _plain_text(name: str, value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BrowserCaptureError(f"FantasyPros ECR {name} was invalid")
    if re.search(r"(?:https?|data|javascript):|//|[\x00-\x1f]", value, re.IGNORECASE):
        raise BrowserCaptureError(f"FantasyPros ECR {name} contained unsafe text")
    return value.strip()


__all__ = ("ecr_capture_data",)
