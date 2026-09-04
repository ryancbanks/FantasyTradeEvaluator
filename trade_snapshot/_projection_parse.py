"""Parse identity and numeric rows from strict captured projection tables."""

from dataclasses import dataclass
from math import isfinite
import re
from urllib.parse import urlsplit

from .capture_schema import CaptureProvider, GenericTableArtifact
from .identity import IdentityRegistry
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_player_position


_PLAYER_HEADERS = {"PLAYER", "PLAYERS", "PLAYER NAME", "ATHLETE", "NAME"}
_POSITION_HEADERS = {"POS", "POSITION"}
_TEAM_HEADERS = {"TEAM", "TM"}
_OPPONENT_HEADERS = {"OPP", "OPPONENT"}
_DESIGNATION_HEADERS = {"STATUS"}
_BYE_HEADERS = {"BYE"}
_POINT_HEADER_TIERS = (
    {"FPTS", "FAN PTS", "FANTASY POINTS"},
    {"PROJ", "PROJECTED"},
    {"PTS", "POINTS"},
)
_POSITIONS = CANONICAL_PLAYER_POSITIONS
_MISSING = {"", "-", "--", "—", "N/A", "NA"}
_FANTASYPROS_DEFENSE_TEAMS = {
    "arizona-defense": "ARI",
    "atlanta-defense": "ATL",
    "baltimore-defense": "BAL",
    "buffalo-defense": "BUF",
    "carolina-defense": "CAR",
    "chicago-defense": "CHI",
    "cincinnati-defense": "CIN",
    "cleveland-defense": "CLE",
    "dallas-defense": "DAL",
    "denver-defense": "DEN",
    "detroit-defense": "DET",
    "green-bay-defense": "GB",
    "houston-defense": "HOU",
    "indianapolis-defense": "IND",
    "jacksonville-defense": "JAX",
    "kansas-city-defense": "KC",
    "las-vegas-defense": "LV",
    "los-angeles-chargers-defense": "LAC",
    "los-angeles-rams-defense": "LAR",
    "miami-defense": "MIA",
    "minnesota-defense": "MIN",
    "new-england-defense": "NE",
    "new-orleans-defense": "NO",
    "new-york-giants-defense": "NYG",
    "new-york-jets-defense": "NYJ",
    "philadelphia-defense": "PHI",
    "pittsburgh-defense": "PIT",
    "san-francisco-defense": "SF",
    "seattle-defense": "SEA",
    "tampa-bay-defense": "TB",
    "tennessee-defense": "TEN",
    "washington-defense": "WSH",
}


@dataclass(frozen=True, slots=True)
class ProjectionArtifactRow:
    identity_provider: str
    provider_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    projected_fantasy_points: float | None
    raw_projected_stats: tuple[tuple[str, float], ...]
    is_bye: bool
    opponent_team_id: str | None
    is_home: bool | None
    provider_status_designation: str | None


def projection_artifact_rows(
    artifact: GenericTableArtifact,
    *,
    known_registry: IdentityRegistry | None = None,
) -> tuple[ProjectionArtifactRow, ...]:
    if not isinstance(artifact, GenericTableArtifact):
        raise ValueError("artifact must be a GenericTableArtifact")
    if known_registry is not None and not isinstance(known_registry, IdentityRegistry):
        raise ValueError("known_registry must be an IdentityRegistry or None")
    rows = []
    seen = set()
    for table in artifact.tables:
        headers = tuple(_header(cell.text) for cell in table.rows[0])
        player_index = _one_index(headers, _PLAYER_HEADERS, "player")
        points_index = _points_index(headers)
        position_index = _optional_index(headers, _POSITION_HEADERS, "position")
        team_index = _optional_index(headers, _TEAM_HEADERS, "team")
        opponent_index = _optional_index(headers, _OPPONENT_HEADERS, "opponent")
        designation_index = _optional_index(
            headers, _DESIGNATION_HEADERS, "status designation"
        )
        bye_index = _optional_index(headers, _BYE_HEADERS, "bye status")
        status_indices = tuple(
            index for index in (designation_index, bye_index) if index is not None
        )
        excluded = {
            player_index,
            points_index,
            *(() if position_index is None else (position_index,)),
            *(() if team_index is None else (team_index,)),
            *(() if opponent_index is None else (opponent_index,)),
            *status_indices,
        }
        stat_indices = _stat_indices(headers, excluded)
        for cells in table.rows[1:]:
            identity_provider, link_id, is_team_link = _provider_link(
                artifact.provider, cells[player_index].links[0]
            )
            known = (
                None
                if known_registry is None or is_team_link
                else known_registry.lookup(identity_provider, link_id)
            )
            name, position, team = _player_metadata(
                cells[player_index].text,
                None if position_index is None else cells[position_index].text,
                (
                    link_id
                    if is_team_link
                    and artifact.provider in {
                        CaptureProvider.CBS,
                        CaptureProvider.FANTASYPROS,
                    }
                    else None if team_index is None else cells[team_index].text
                ),
                artifact,
                known,
            )
            if is_team_link:
                if position != "DST" or team == "FA":
                    raise ValueError(
                        "team identity links are valid only for an NFL team defense"
                    )
                provider_id = f"dst:{team}"
            elif artifact.provider is CaptureProvider.ESPN and position == "DST":
                # ESPN represents team defenses as negative player IDs (for
                # example, -16034) even though a safe synthetic public detail
                # link can encode only the unsigned numeric component.
                provider_id = f"-{link_id}"
            else:
                provider_id = link_id
            key = identity_provider, provider_id
            if key in seen:
                raise ValueError("projection artifact repeats a provider player ID")
            seen.add(key)
            statuses = " ".join(cells[index].text for index in status_indices).upper()
            opponent, is_home = (
                (None, None)
                if opponent_index is None
                else _opponent(cells[opponent_index].text)
            )
            stats = tuple(
                (headers[index].lower().replace(" ", "_"), value)
                for index in stat_indices
                if (value := _optional_number(cells[index].text)) is not None
            )
            rows.append(
                ProjectionArtifactRow(
                    identity_provider=identity_provider,
                    provider_player_id=provider_id,
                    display_name=name,
                    position=position,
                    nfl_team_id=team,
                    projected_fantasy_points=_optional_number(cells[points_index].text),
                    raw_projected_stats=stats,
                    is_bye=bool(re.search(r"\bBYE\b", statuses)),
                    opponent_team_id=opponent,
                    is_home=is_home,
                    provider_status_designation=(
                        None
                        if designation_index is None
                        else _provider_status(cells[designation_index].text)
                    ),
                )
            )
    if not rows:
        raise ValueError("projection artifact contains no player rows")
    return tuple(sorted(rows, key=lambda row: (row.identity_provider, row.provider_player_id)))


def _player_metadata(player_text, position_text, team_text, artifact, known):
    explicit_position = (
        normalize_position(position_text)
        if position_text and position_text.strip()
        else None
    )
    scope = tuple(
        value for value in artifact.position_scope if value not in {"ALL", "FLX", "IDP"}
    )
    position = explicit_position or (scope[0] if len(scope) == 1 else None)
    team = _team(team_text) if team_text and team_text.strip() else None
    parsed = _parse_player_cell(player_text, position, team)
    if parsed is not None:
        name, parsed_position, parsed_team = parsed
        position = position or parsed_position
        team = team or parsed_team
    elif known is not None:
        name = known.display_name
        position = position or normalize_position(known.position)
        team = team or _team(known.nfl_team_id)
    else:
        raise ValueError("projection player row lacks exact name/position/team metadata")
    if position is None or team is None:
        raise ValueError("projection player row lacks exact position or NFL team")
    return _display_name(name, artifact.provider), position, team


def _parse_player_cell(value, expected_position, expected_team):
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    expected_aliases = {
        "RB": ("RB", "FB"),
        "K": ("K", "PK"),
        "DST": (r"D\s*/\s*ST", "DST", "DEF"),
    }
    positions = [
        re.escape(value) if "\\" not in value else value
        for value in expected_aliases.get(expected_position, (expected_position,))
    ] if expected_position else [
        r"D\s*/\s*ST", "DST", "DEF", "QB", "RB", "FB", "WR", "TE", "K", "PK",
        "DL", "DE", "DT", "NT", "EDGE", "LB", "ILB", "OLB", "MLB", "DB", "CB",
        "S", "FS", "SS", "IDP"
    ]
    position_pattern = "(?:" + "|".join(positions) + ")"
    team_pattern = re.escape(expected_team) if expected_team else r"[A-Z]{2,3}|FA"
    patterns = (
        rf"^(?P<name>.+?)\s+(?P<team>{team_pattern})\s*(?:-|·|,)?\s*(?P<pos>{position_pattern})$",
        rf"^(?P<name>.+?)\s+(?P<pos>{position_pattern})\s*(?:-|·|,)?\s*(?P<team>{team_pattern})$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            return (
                match.group("name").strip(),
                normalize_position(match.group("pos")),
                _team(match.group("team")),
            )
    if expected_position and not expected_team:
        match = re.fullmatch(
            r"(?P<name>.+?)\s+(?P<team>[A-Z]{2,3}|FA)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return (
                match.group("name").strip(),
                expected_position,
                _team(match.group("team")),
            )
    if expected_position and expected_team:
        for suffix in (expected_team, expected_position):
            match = re.fullmatch(
                rf"(?P<name>.+?)\s+{re.escape(suffix)}", text, flags=re.IGNORECASE
            )
            if match:
                return match.group("name").strip(), expected_position, expected_team
        return text, expected_position, expected_team
    return None


def _provider_link(provider, link):
    path = urlsplit(link).path
    patterns = {
        CaptureProvider.ESPN: r"^/nfl/player/_/id/([0-9]+)(?:/[^/]*)?/?$",
        CaptureProvider.YAHOO: r"^/nfl/players/([0-9]+)/?$",
        CaptureProvider.FANTASYPROS: (
            r"^/nfl/(?:players|projections)/([a-z0-9-]+)\.php$"
        ),
        CaptureProvider.CBS: r"^/nfl/players/([0-9]+)/[a-z0-9-]+/fantasy/?$",
        CaptureProvider.FFTODAY: r"^/stats/players/([0-9]+)/[A-Za-z0-9_.'-]+/?$",
    }
    if provider is CaptureProvider.FANTASYSHARKS:
        query = urlsplit(link).query
        match = re.fullmatch(r"id=([1-9][0-9]{0,9})", query)
        if path == "/apps/bert/players/playerpage.php" and match is not None:
            return "fantasysharks", match.group(1), False
    else:
        identity_provider = projection_identity_provider(provider)
        pattern = patterns[provider]
        match = re.fullmatch(pattern, path, flags=re.IGNORECASE)
        if match is not None:
            provider_id = match.group(1).casefold()
            if provider is CaptureProvider.FANTASYPROS:
                defense_team = _FANTASYPROS_DEFENSE_TEAMS.get(provider_id)
                if defense_team is not None:
                    return identity_provider, defense_team, True
            return identity_provider, provider_id, False
    if provider is CaptureProvider.YAHOO:
        team = re.fullmatch(
            r"^/nfl/teams/([a-z0-9]+(?:-[a-z0-9]+)*)/?$",
            path,
            flags=re.IGNORECASE,
        )
        if team is not None:
            return "yahoo", team.group(1).casefold(), True
    if provider is CaptureProvider.CBS:
        team = re.fullmatch(
            r"^/nfl/teams/([a-z]{2,3})/[a-z0-9]+(?:-[a-z0-9]+)*/?$",
            path,
            flags=re.IGNORECASE,
        )
        if team is not None:
            return "cbs", _team(team.group(1)), True
    raise ValueError("projection link does not contain a supported public identity")


def projection_identity_provider(provider):
    """Return the stable identity namespace used by a projection page's links."""

    try:
        provider = CaptureProvider(provider)
    except (TypeError, ValueError):
        raise ValueError("projection provider is invalid") from None
    return {
        CaptureProvider.FANTASYPROS: "fantasypros_projection",
        CaptureProvider.ESPN: "espn",
        CaptureProvider.YAHOO: "yahoo",
        CaptureProvider.CBS: "cbs",
        CaptureProvider.FFTODAY: "fftoday",
        CaptureProvider.FANTASYSHARKS: "fantasysharks",
    }[provider]


def _points_index(headers):
    for choices in _POINT_HEADER_TIERS:
        matches = [index for index, header in enumerate(headers) if header in choices]
        if len(matches) > 1:
            raise ValueError("projection table has ambiguous fantasy-point columns")
        if matches:
            return matches[0]
    raise ValueError("projection table has no fantasy-point projection column")


def _stat_indices(headers, excluded):
    result, names = [], set()
    for index, header in enumerate(headers):
        if index in excluded:
            continue
        name = header.lower().replace(" ", "_")
        if name in names:
            raise ValueError("projection table contains duplicate stat headers")
        names.add(name)
        result.append(index)
    return tuple(result)


def _one_index(headers, choices, label):
    matches = [index for index, header in enumerate(headers) if header in choices]
    if len(matches) != 1:
        raise ValueError(f"projection table must contain exactly one {label} column")
    return matches[0]


def _optional_index(headers, choices, label):
    matches = [index for index, header in enumerate(headers) if header in choices]
    if len(matches) > 1:
        raise ValueError(f"projection table contains duplicate {label} columns")
    return matches[0] if matches else None


def _opponent(value):
    text = value.strip().upper()
    if text in _MISSING or text == "BYE":
        return None, None
    match = re.fullmatch(r"(?:(@)|(?:VS\.?\s*))?([A-Z]{2,3})", text)
    if match is None:
        return None, None
    is_home = False if match.group(1) else True if text.startswith("VS") else None
    return _team(match.group(2)), is_home


def _optional_number(value):
    text = value.strip().upper()
    if text in _MISSING:
        return None
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:,[0-9]{3})*|[0-9]*\.[0-9]+)", text):
        return None
    number = float(text.replace(",", ""))
    return number if isfinite(number) else None


def _provider_status(value):
    text = " ".join(value.split())
    if text.upper() in _MISSING or text.upper() == "BYE":
        return None
    if len(text) > 80 or re.search(r"(?:https?://|www\.)", text, flags=re.IGNORECASE):
        raise ValueError("projection status designation is not a bounded label")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("projection status designation contains a control character")
    return text


def normalize_position(value):
    return normalize_player_position(value, require_supported=True)


def _team(value):
    normalized = value.strip().upper()
    normalized = {
        "GBP": "GB", "JAC": "JAX", "KCC": "KC", "LA": "LAR",
        "LVR": "LV", "NEP": "NE", "NOS": "NO", "SFO": "SF",
        "TBB": "TB", "WAS": "WSH",
    }.get(normalized, normalized)
    if not re.fullmatch(r"[A-Z]{2,3}|FA", normalized):
        raise ValueError(f"invalid NFL team abbreviation {value!r}")
    return normalized


def _display_name(value: str, provider: CaptureProvider) -> str:
    name = re.sub(r"\s+", " ", value).strip()
    if provider is CaptureProvider.FANTASYSHARKS:
        match = re.fullmatch(r"([^,]+),\s*(.+)", name)
        if match:
            name = f"{match.group(2)} {match.group(1)}"
    return name


def _header(value):
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


__all__ = (
    "ProjectionArtifactRow",
    "normalize_position",
    "projection_artifact_rows",
    "projection_identity_provider",
)
