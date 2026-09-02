"""Validate, deduplicate, and prove complete provider projection captures."""

from dataclasses import dataclass
import re

from .browser_capture import BrowserCaptureError
from .capture_schema import (
    CaptureProvider,
    PageCaptureTask,
    RankingHorizon,
    VisibleTable,
    VisibleTableCell,
    public_player_link,
)


_SOURCE_FIELDS = {"season", "week", "horizon", "scoring", "positions", "period_text"}
_PRIVATE = re.compile(
    r"\b(?:OWN(?:ED|ER|ERSHIP)?|ROST(?:ER(?:ED)?)?|START(?:ED|ER)?|"
    r"AVAIL(?:ABILITY)?|ACTION|ADD|DROP|WAIVER|TRANSACTION|FANTASY TEAM|"
    r"LEAGUE|SALARY|DRAFTED|WATCH)\b"
)
_PLAYER = re.compile(r"^(?:PLAYER|PLAYERS|PLAYER NAME|ATHLETE|NAME)$")
_IDENTITY = re.compile(r"^(?:TEAM|TM|POS|POSITION|OPP|OPPONENT|STATUS|BYE)$")
_STAT = re.compile(
    r"^(?:PROJ|PROJECTED|FPTS|FPPG|FAN PTS|FANTASY POINTS|PTS|POINTS|GP|CMP|ATT|YDS|YD|TD|TDS|INT|INTS|REC|TGT|TAR|FUM|FL|FGM|FGA|XPM|XPA|SACK|SACKS|TACK|ASST|SAFE|PA|YA|RET|LONG|AVG|RATE)$|"
    r"^(?:PASS|RUSH|REC|RECEIVING|KICK|DEF|DST) (?:CMP|ATT|YDS|YD|TD|TDS|INT|REC|TGT|TAR|FUM|PTS|POINTS|SACK|SAFE|FUM REC|BLK KICK)$|"
    r"^(?:RET TD|MISC 2PT|FUM LOST|PASS DEF|FUM FRC|FUM REC|DEF TD|RZ TGT|BLK KICK|KICK RET YDS|YDS ALLOWED|PTS AGN|SCORING OPPORTUNITIES|FGM MISS|XPM MISS|PASS YDS PER GAME|RUSH YDS PER GAME|XPM|FGM (?:0 19|10 19|20 29|30 39|40 49|50)|(?:0 19|10 19|20 29|30 39|40 49|50) FGM)$"
)


@dataclass(frozen=True, slots=True)
class ProjectionCaptureData:
    tables: tuple[VisibleTable, ...]
    source_period_text: str
    segments_captured: int


def projection_capture(
    segments: list[object], task: PageCaptureTask
) -> ProjectionCaptureData:
    if not isinstance(task, PageCaptureTask) or task.projection is None:
        raise BrowserCaptureError("projection capture task was invalid")
    if not segments or len(segments) > 10000:
        raise BrowserCaptureError("projection traversal produced no bounded segments")
    sources, parsed_tables = [], []
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"source", "tables"}:
            raise BrowserCaptureError("projection extraction returned an invalid shape")
        sources.append(_source(segment["source"], task))
        tables = segment["tables"]
        if not isinstance(tables, list) or len(tables) > 8:
            raise BrowserCaptureError("projection extraction returned invalid tables")
        parsed_tables.extend(_table(table, task.provider) for table in tables)
    if len(set(sources)) != 1:
        raise BrowserCaptureError("projection source evidence changed during traversal")
    merged = _merge(parsed_tables)
    if not merged:
        raise BrowserCaptureError("no complete projection/stat table was captured")
    return ProjectionCaptureData(merged, sources[0][-1], len(segments))


def _source(value: object, task: PageCaptureTask) -> tuple[object, ...]:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise BrowserCaptureError("projection source evidence was incomplete")
    spec = task.projection
    if value["season"] != task.season:
        raise BrowserCaptureError("projection page season did not match the task")
    if value["horizon"] != spec.horizon.value:
        raise BrowserCaptureError("projection page horizon did not match the task")
    if spec.horizon is RankingHorizon.WEEKLY and value["week"] != task.week:
        raise BrowserCaptureError("projection page week did not match the task")
    if spec.horizon is RankingHorizon.ROS and value["week"] is not None:
        raise BrowserCaptureError("ROS projection page claimed a weekly period")
    if value["scoring"] != spec.scoring:
        raise BrowserCaptureError("projection page scoring did not match the task")
    positions = value["positions"]
    if not isinstance(positions, list) or any(
        not isinstance(position, str) for position in positions
    ):
        raise BrowserCaptureError("projection position evidence was invalid")
    actual = {"FLX" if item == "FLEX" else item for item in positions}
    requested = set(spec.position_scope)
    if not actual or not _positions_match(actual, requested):
        raise BrowserCaptureError("projection page positions did not match the task")
    period = value["period_text"]
    if not isinstance(period, str) or not period or len(period) > 512 or "//" in period:
        raise BrowserCaptureError("projection period evidence was invalid")
    return (
        value["season"], value["week"], value["horizon"], value["scoring"],
        tuple(sorted(actual)), period,
    )


def _positions_match(actual: set[str], requested: set[str]) -> bool:
    supported = {
        "ALL", "QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB", "IDP", "FLX",
    }
    return bool(actual) and actual <= supported and actual == requested


def _table(value: object, provider: CaptureProvider) -> VisibleTable:
    if not isinstance(value, dict) or set(value) != {"rows"}:
        raise BrowserCaptureError("projection table shape was invalid")
    rows = value["rows"]
    if not isinstance(rows, list) or not 1 < len(rows) <= 5001:
        raise BrowserCaptureError("projection table row count was invalid")
    parsed = tuple(tuple(_cell(cell, provider) for cell in row) for row in rows)
    width = len(parsed[0])
    if not width or width > 64 or any(len(row) != width for row in parsed):
        raise BrowserCaptureError("projection table rows had inconsistent widths")
    headers = tuple(_header(cell.text) for cell in parsed[0])
    if any(_PRIVATE.search(header) for header in headers):
        raise BrowserCaptureError("projection table contained a private column")
    players = [index for index, header in enumerate(headers) if _PLAYER.fullmatch(header)]
    if len(players) != 1 or not any(_STAT.fullmatch(header) for header in headers):
        raise BrowserCaptureError("projection table did not match the provider stat profile")
    allowed = all(
        _PLAYER.fullmatch(header) or _IDENTITY.fullmatch(header) or _STAT.fullmatch(header)
        for header in headers
    )
    if not allowed:
        raise BrowserCaptureError("projection table contained a non-allowlisted column")
    player_index = players[0]
    if any(len(row[player_index].links) != 1 for row in parsed[1:]):
        raise BrowserCaptureError("projection player rows lacked one public identity link")
    if not any(
        re.search(r"[+-]?\d+(?:\.\d+)?", cell.text)
        for row in parsed[1:] for cell in row
    ):
        raise BrowserCaptureError("projection table had no numeric stat data")
    return VisibleTable(parsed)


def _cell(value: object, provider: CaptureProvider) -> VisibleTableCell:
    if (
        not isinstance(value, dict) or set(value) != {"text", "links"}
        or not isinstance(value["text"], str) or len(value["text"]) > 1000
        or not isinstance(value["links"], list) or len(value["links"]) > 1
    ):
        raise BrowserCaptureError("projection cell was invalid")
    links = tuple(filter(None, (public_player_link(provider, link) for link in value["links"])))
    if len(links) != len(value["links"]):
        raise BrowserCaptureError("projection cell contained a non-public link")
    return VisibleTableCell(value["text"], links)


def _merge(tables: list[VisibleTable]) -> tuple[VisibleTable, ...]:
    grouped: dict[tuple[str, ...], dict[str, tuple[VisibleTableCell, ...]]] = {}
    headers: dict[tuple[str, ...], tuple[VisibleTableCell, ...]] = {}
    for table in tables:
        key = tuple(cell.text for cell in table.rows[0])
        player_index = next(index for index, name in enumerate(key) if _PLAYER.fullmatch(name))
        headers[key] = table.rows[0]
        bucket = grouped.setdefault(key, {})
        for row in table.rows[1:]:
            player_key = row[player_index].links[0]
            if player_key in bucket and bucket[player_key] != row:
                raise BrowserCaptureError("projection traversal returned conflicting player rows")
            bucket[player_key] = row
    return tuple(
        VisibleTable((headers[key], *tuple(rows[player] for player in sorted(rows))))
        for key, rows in sorted(grouped.items()) if rows
    )


def _header(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


__all__ = ("ProjectionCaptureData", "projection_capture")
