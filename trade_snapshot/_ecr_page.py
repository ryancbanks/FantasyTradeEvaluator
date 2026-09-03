"""Validate the allowlisted FantasyPros ``ecrData`` bootstrap."""

from collections.abc import Mapping
from collections import Counter
from datetime import datetime, timezone
from math import isfinite
import re
from urllib.parse import urlsplit

from .browser_capture import BrowserCaptureError, ECRCaptureData
from .capture_schema import ECRRankingRow, FantasyProsECRTask, RankingHorizon
from .ecr_source import (
    EcrHorizonEvidence,
    EcrSourceDetails,
    FANTASYPROS_LATEST_ECR_POLICY,
)
from .positions import normalize_player_position


_SOURCE_FIELDS = {
    "sport", "ranking_type", "type_text", "year", "week", "position", "scoring",
    "expert_ids", "expert_count", "last_updated", "player_count", "position_counts",
    "expert_policy", "page_evidence",
}
_OPTIONAL_SOURCE_FIELDS = {"last_updated_ts"}
_PAGE_EVIDENCE_FIELDS = {
    "protocol", "hostname", "port", "pathname", "canonical_protocol",
    "canonical_hostname", "canonical_port", "canonical_pathname",
    "canonical_link_count", "document_title", "settings_ranking_type",
    "settings_position", "settings_page_heading", "settings_fallback_note",
    "visible_page_heading", "visible_page_heading_count", "visible_ranking_period",
    "visible_ranking_period_count", "visible_fallback_note",
    "visible_fallback_note_count",
}
_ROW_FIELDS = {
    "player_id", "player_name", "team", "position", "rank_ecr", "rank_min",
    "rank_max", "rank_avg", "rank_std", "position_rank",
}
_EXPERT_POLICY_FIELDS = {
    "policy_id", "group_id", "title", "description", "expert_ids",
}


def ecr_capture_data(value: object, task: FantasyProsECRTask) -> ECRCaptureData:
    """Prove source dimensions and convert explicitly selected ranking fields."""

    if not isinstance(value, Mapping) or set(value) != {"source", "rankings"}:
        raise BrowserCaptureError("FantasyPros ECR bootstrap returned an invalid shape")
    source, raw_rows = value["source"], value["rankings"]
    if (
        not isinstance(source, Mapping)
        or not _SOURCE_FIELDS <= set(source) <= _SOURCE_FIELDS | _OPTIONAL_SOURCE_FIELDS
    ):
        raise BrowserCaptureError("FantasyPros ECR source provenance was incomplete")
    if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > 5000:
        raise BrowserCaptureError("FantasyPros ECR rankings were empty or exceeded limits")
    rows = tuple(_ranking_row(row) for row in raw_rows)
    if len({row.provider_player_id for row in rows}) != len(rows):
        raise BrowserCaptureError("FantasyPros ECR rankings repeated a player ID")
    updated_at = _source_update_time(source.get("last_updated_ts"))
    experts = _experts(source, task)
    expert_policy = _expert_policy(source["expert_policy"], task, experts)
    source_scoring, source_details = _validate_source_dimensions(
        source, task, rows, updated_at, expert_policy
    )
    allowed = _allowed_positions(source["position"], task.position_scope)
    if len(allowed) == 1 and allowed <= {"DL", "LB", "DB"}:
        page_position = next(iter(allowed))
        permitted = {
            "DL": {"DL", "LB"},
            "LB": {"LB", "DL", "DB"},
            "DB": {"DB", "LB"},
        }[page_position]
        if any(row.position not in permitted for row in rows):
            raise BrowserCaptureError(
                "FantasyPros ECR IDP page contained an impossible position"
            )
        if not any(row.position == page_position for row in rows):
            raise BrowserCaptureError(
                "FantasyPros ECR rankings had no rows for the selected IDP position"
            )
    elif any(row.position not in allowed for row in rows):
        raise BrowserCaptureError("FantasyPros ECR rows did not match the selected position")
    update = _plain_text("last_updated", source["last_updated"], maximum=128)
    try:
        return ECRCaptureData(
            experts,
            len(experts),
            source_scoring,
            update,
            updated_at,
            source_details,
            rows,
        )
    except ValueError:
        raise BrowserCaptureError("FantasyPros ECR extraction failed strict validation") from None


def _validate_source_dimensions(source, task, rows, updated_at, expert_policy):
    if _plain_text("sport", source["sport"], maximum=8).upper() != "NFL":
        raise BrowserCaptureError("FantasyPros ECR source sport was not NFL")
    year = _integer(source["year"], "year", minimum=2000, maximum=2200)
    if year != task.season:
        raise BrowserCaptureError("FantasyPros ECR source season did not match the task")
    ranking_type = _plain_text("ranking_type", source["ranking_type"], maximum=32)
    normalized_type = re.sub(r"[^a-z]", "", ranking_type.casefold())
    expected = "weekly" if task.horizon is RankingHorizon.WEEKLY else "ros"
    aliases = {"weekly": "weekly", "ros": "ros", "restofseason": "ros"}
    source_week = _integer(source["week"], "week", minimum=0, maximum=25)
    scoring = _plain_text("scoring", source["scoring"], maximum=32).upper()
    scoring = {"STANDARD": "STD", "HALF PPR": "HALF"}.get(scoring, scoring)
    if scoring != task.source_scoring:
        raise BrowserCaptureError("FantasyPros ECR scoring did not match the task")
    type_text = _plain_text("type_text", source["type_text"], maximum=64)
    type_key = type_text.upper()
    page = _page_evidence(source["page_evidence"], task)
    direct = aliases.get(normalized_type) == expected
    preseason_ros = _is_preseason_ros_equivalent(
        task,
        normalized_type,
        type_key,
        source_week,
        source["position"],
        page,
        updated_at,
    )
    if not direct and not preseason_ros:
        raise BrowserCaptureError("FantasyPros ECR ranking horizon did not match the task")
    if task.horizon is RankingHorizon.WEEKLY:
        if source_week != task.week:
            raise BrowserCaptureError("FantasyPros ECR source week did not match the task")
        if "WEEK" not in type_key:
            raise BrowserCaptureError("FantasyPros ECR page type did not prove weekly rankings")
    elif direct and not ("REST" in type_key or "ROS" in type_key):
        raise BrowserCaptureError("FantasyPros ECR page type did not prove ROS rankings")
    if direct:
        _validate_direct_page_evidence(page, task, source["position"])
    count = _integer(source["player_count"], "player_count", minimum=1, maximum=5000)
    if count != len(rows):
        raise BrowserCaptureError("FantasyPros ECR bootstrap was not complete")
    page_position = _position(source["position"])
    _allowed_positions(page_position, task.position_scope)
    position_counts = _position_counts(source["position_counts"])
    observed_counts = dict(sorted(Counter(row.source_position for row in rows).items()))
    if position_counts != observed_counts:
        raise BrowserCaptureError("FantasyPros ECR source position counts were inconsistent")
    evidence = (
        EcrHorizonEvidence.PRESEASON_REST_OF_SEASON_PAGE
        if preseason_ros
        else EcrHorizonEvidence.DIRECT_METADATA
    )
    try:
        return scoring, EcrSourceDetails(
            ranking_type=ranking_type,
            type_text=type_text,
            source_week=source_week,
            page_position=page_position,
            source_player_count=count,
            source_position_counts=position_counts,
            expert_selection_policy=expert_policy["policy_id"],
            expert_group_id=expert_policy["group_id"],
            expert_group_title=expert_policy["title"],
            expert_group_description=expert_policy["description"],
            page_protocol=page["protocol"],
            page_hostname=page["hostname"],
            page_port=page["port"],
            page_path=page["pathname"],
            canonical_protocol=page["canonical_protocol"],
            canonical_hostname=page["canonical_hostname"],
            canonical_port=page["canonical_port"],
            canonical_path=page["canonical_pathname"],
            canonical_link_count=page["canonical_link_count"],
            document_title=page["document_title"],
            settings_ranking_type=page["settings_ranking_type"],
            settings_position=page["settings_position"],
            settings_page_heading=page["settings_page_heading"],
            settings_fallback_note=page["settings_fallback_note"],
            visible_page_heading=page["visible_page_heading"],
            visible_page_heading_count=page["visible_page_heading_count"],
            visible_ranking_period=page["visible_ranking_period"],
            visible_ranking_period_count=page["visible_ranking_period_count"],
            visible_fallback_note=page["visible_fallback_note"],
            visible_fallback_note_count=page["visible_fallback_note_count"],
            horizon_evidence=evidence,
        )
    except ValueError:
        raise BrowserCaptureError("FantasyPros ECR page evidence was invalid") from None


def _is_preseason_ros_equivalent(
    task,
    normalized_type,
    type_text,
    source_week,
    source_position,
    page,
    updated_at,
) -> bool:
    if (
        task.capture_method.value != "visible_page"
        or task.horizon is not RankingHorizon.ROS
        or task.week != 1
    ):
        return False
    page_position = _position(source_position)
    expected_type = {
        "STD": "DRAFT", "HALF": "DRAFT HALF PPR", "PPR": "DRAFT PPR",
    }[task.source_scoring]
    if (
        normalized_type != "draft"
        or type_text != expected_type
        or source_week != 0
        or len(task.position_scope) != 1
        or updated_at is None
    ):
        return False
    prefix = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}[
        task.source_scoring
    ]
    expected_path = f"/nfl/rankings/ros-{prefix}{page_position.casefold()}.php"
    expected_heading = f"FANTASY FOOTBALL ROS RANKINGS ({task.season})"
    expected_note = (
        f"WE ARE CURRENTLY DISPLAYING {task.season} DRAFT RANKINGS. "
        "UPDATED ROS RANKINGS WILL BE AVAILABLE AFTER THE FIRST WEEK."
    )
    title = _optional_plain_text(
        "document_title", page["document_title"], maximum=512
    )
    parsed_task = urlsplit(task.url)
    return bool(
        not parsed_task.query
        and not parsed_task.fragment
        and parsed_task.path == expected_path
        and page["pathname"] == expected_path
        and page["canonical_pathname"] == expected_path
        and page["canonical_link_count"] == 1
        and page["settings_ranking_type"] == "ros"
        and page["settings_position"] == page_position
        and str(page["settings_page_heading"] or "").upper() == expected_heading
        and str(page["visible_page_heading"] or "").upper() == expected_heading
        and page["visible_page_heading_count"] == 1
        and page["visible_ranking_period"] == "Rest of Season"
        and page["visible_ranking_period_count"] == 1
        and str(page["settings_fallback_note"] or "").upper() == expected_note
        and str(page["visible_fallback_note"] or "").upper() == expected_note
        and page["visible_fallback_note_count"] == 1
        and title is not None
        and title.startswith("Rest of Season ")
        and " Rankings" in title
        and title.endswith(" | FantasyPros")
    )


def _validate_direct_page_evidence(page, task, source_position) -> None:
    expected_settings = (
        "weekly" if task.horizon is RankingHorizon.WEEKLY else "ros"
    )
    period = str(page["visible_ranking_period"] or "").casefold()
    if (
        page["canonical_link_count"] != 1
        or page["visible_page_heading_count"] != 1
        or page["visible_ranking_period_count"] != 1
        or page["settings_ranking_type"] != expected_settings
        or page["settings_position"] != _position(source_position)
        or page["settings_page_heading"] != page["visible_page_heading"]
        or (
            task.horizon is RankingHorizon.WEEKLY
            and "week" not in period
        )
        or (
            task.horizon is RankingHorizon.ROS
            and period != "rest of season"
        )
    ):
        raise BrowserCaptureError(
            "FantasyPros ECR visible page did not prove the requested rankings"
        )


def _page_evidence(value, task) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _PAGE_EVIDENCE_FIELDS:
        raise BrowserCaptureError("FantasyPros ECR page evidence was incomplete")
    expected = urlsplit(task.url)
    protocol = value["protocol"]
    hostname = value["hostname"]
    port = value["port"]
    canonical_protocol = value["canonical_protocol"]
    canonical_hostname = value["canonical_hostname"]
    canonical_port = value["canonical_port"]
    if (
        protocol != "https:"
        or canonical_protocol != "https:"
        or port not in {"", "443"}
        or canonical_port not in {"", "443"}
        or not isinstance(hostname, str)
        or not isinstance(canonical_hostname, str)
        or hostname.casefold().rstrip(".")
        != (expected.hostname or "").casefold().rstrip(".")
        or canonical_hostname.casefold().rstrip(".")
        != (expected.hostname or "").casefold().rstrip(".")
        or value["pathname"] != expected.path
        or value["canonical_pathname"] != expected.path
    ):
        raise BrowserCaptureError("FantasyPros ECR page identity did not match the task")
    result = dict(value)
    for name in (
        "document_title",
        "settings_ranking_type",
        "settings_position",
        "settings_page_heading",
        "settings_fallback_note",
        "visible_page_heading",
        "visible_ranking_period",
        "visible_fallback_note",
    ):
        result[name] = _optional_plain_text(name, value[name], maximum=1000)
    if result["document_title"] is None:
        raise BrowserCaptureError("FantasyPros ECR document title was missing")
    for name in (
        "canonical_link_count",
        "visible_page_heading_count",
        "visible_ranking_period_count",
        "visible_fallback_note_count",
    ):
        result[name] = _integer(value[name], name, minimum=0, maximum=100)
    return result


def _position_counts(value) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value or len(value) > 100:
        raise BrowserCaptureError("FantasyPros ECR position counts were invalid")
    result = {}
    for raw_position, raw_count in value.items():
        position = _position(raw_position)
        if position in result:
            raise BrowserCaptureError("FantasyPros ECR position counts repeated a position")
        result[position] = _integer(
            raw_count, "position count", minimum=1, maximum=5000
        )
    return dict(sorted(result.items()))


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


def _expert_policy(value, task, selected_experts) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _EXPERT_POLICY_FIELDS:
        raise BrowserCaptureError("FantasyPros Latest ECR policy proof was incomplete")
    policy_id = _plain_text("expert policy", value["policy_id"], maximum=64)
    group_id = _plain_text("expert group ID", value["group_id"], maximum=64)
    title = _plain_text("expert group title", value["title"], maximum=128)
    description = _plain_text(
        "expert group description", value["description"], maximum=256
    )
    if (
        policy_id != task.expert_selection_policy
        or policy_id != FANTASYPROS_LATEST_ECR_POLICY
        or group_id != "default"
        or title != "Latest ECR"
        or not all(
            token in description.casefold()
            for token in ("accurat", "expert", "recent", "updat")
        )
    ):
        raise BrowserCaptureError(
            "FantasyPros expert group did not prove the Latest ECR policy"
        )
    raw_experts = value["expert_ids"]
    if not isinstance(raw_experts, list):
        raise BrowserCaptureError("FantasyPros Latest ECR expert IDs were invalid")
    group_experts = tuple(sorted((_decimal_id(item) for item in raw_experts), key=int))
    if (
        not group_experts
        or len(set(group_experts)) != len(group_experts)
        or group_experts != selected_experts
    ):
        raise BrowserCaptureError(
            "FantasyPros selected experts did not match the Latest ECR group"
        )
    return {
        "policy_id": policy_id,
        "group_id": group_id,
        "title": title,
        "description": description,
    }


def _ranking_row(value: object) -> ECRRankingRow:
    if not isinstance(value, Mapping) or set(value) != _ROW_FIELDS:
        raise BrowserCaptureError("FantasyPros ECR ranking row shape was invalid")
    player_id = _decimal_id(value["player_id"])
    name = _plain_text("player_name", value["player_name"], maximum=200)
    team = value["team"]
    if team is not None:
        team = _plain_text("team", team, maximum=8).upper()
    source_position = _position(value["position"])
    position = _canonical_position(source_position)
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
            visible_values=visible, source_position=source_position, **ranks,
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


def _source_update_time(value: object) -> str | None:
    if value is None:
        return None
    seconds = _integer(
        value,
        "last_updated_ts",
        minimum=946_684_800,
        maximum=7_289_654_399,
    )
    return (
        datetime.fromtimestamp(seconds, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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


def _canonical_position(value: object) -> str:
    raw = _position(value)
    try:
        return normalize_player_position(raw, require_supported=True)
    except ValueError:
        raise BrowserCaptureError("FantasyPros ECR position was unsupported") from None


def _plain_text(name: str, value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BrowserCaptureError(f"FantasyPros ECR {name} was invalid")
    if re.search(r"(?:https?|data|javascript):|//|[\x00-\x1f]", value, re.IGNORECASE):
        raise BrowserCaptureError(f"FantasyPros ECR {name} contained unsafe text")
    return value.strip()


def _optional_plain_text(name: str, value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _plain_text(name, value, maximum=maximum)


__all__ = ("ecr_capture_data",)
