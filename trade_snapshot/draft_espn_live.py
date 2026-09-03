"""Read-only ESPN public draft observation for Draft Lab assistant sessions."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
from numbers import Real

from .draft_assistant import drafter_for_pick
from .draft_history import DraftPlayerBoard
from .espn_free_read import (
    EspnFreeReadClient,
    EspnFreeReadError,
    EspnUnauthorizedError,
)


_MAX_DRAFT_RESPONSE_BYTES = 8 * 1024 * 1024
_PUBLIC_ACCESS_DENIED = (
    "ESPN denied the public read. This league appears private or requires sign-in; "
    "Draft Lab public sync never sends cookies or credentials."
)


class EspnDraftSyncError(RuntimeError):
    """A public ESPN draft could not be reconciled without guessing."""


@dataclass(frozen=True, slots=True)
class EspnDraftObservation:
    league_id: str
    season: int
    team_order: tuple[str, ...]
    assistant_picks: tuple[tuple[int, str], ...]
    drafted: bool
    in_progress: bool

    def __post_init__(self) -> None:
        _requested_league_id(self.league_id)
        _integer("season", self.season, 2012, 9999)
        try:
            team_order = tuple(self.team_order)
            picks = tuple(self.assistant_picks)
        except TypeError:
            raise EspnDraftSyncError(
                "ESPN team order and assistant picks must be sequences"
            ) from None
        if len(team_order) < 2 or len(team_order) > 32:
            raise EspnDraftSyncError("ESPN team order has an unsupported size")
        for team_id in team_order:
            _positive_id("ESPN team order ID", team_id)
        if len(set(team_order)) != len(team_order):
            raise EspnDraftSyncError("ESPN team order contains a duplicate team")
        seen_players: set[str] = set()
        for overall, value in enumerate(picks, 1):
            if not isinstance(value, tuple) or len(value) != 2:
                raise EspnDraftSyncError("ESPN assistant picks have an invalid shape")
            drafter_number, player_id = value
            if drafter_number != drafter_for_pick(overall, len(team_order)):
                raise EspnDraftSyncError("ESPN assistant picks are not in snake order")
            if not isinstance(player_id, str) or not player_id.strip():
                raise EspnDraftSyncError("ESPN assistant pick player ID is invalid")
            if player_id in seen_players:
                raise EspnDraftSyncError("ESPN assistant picks contain a duplicate player")
            seen_players.add(player_id)
        if type(self.drafted) is not bool or type(self.in_progress) is not bool:
            raise EspnDraftSyncError("ESPN draft status must use booleans")
        if self.drafted and self.in_progress:
            raise EspnDraftSyncError("ESPN draft status is contradictory")
        object.__setattr__(self, "team_order", team_order)
        object.__setattr__(self, "assistant_picks", picks)

    def live_sync_record(self, appended_pick_count: int) -> dict[str, object]:
        if (
            type(appended_pick_count) is not int
            or not 0 <= appended_pick_count <= len(self.assistant_picks)
        ):
            raise ValueError("appended_pick_count is invalid")
        return {
            "status": "synced",
            "provider": "espn",
            "access": "public",
            "polling": "on_demand",
            "league_id": self.league_id,
            "season": self.season,
            "drafter_team_ids": list(self.team_order),
            "observed_pick_count": len(self.assistant_picks),
            "appended_pick_count": appended_pick_count,
            "drafted": self.drafted,
            "in_progress": self.in_progress,
            "message": (
                "Observed picks were reconciled from ESPN's public draft read. "
                "No cookies or credentials were sent."
            ),
        }


class EspnPublicDraftAdapter:
    """Poll exactly one public ``mDraftDetail`` resource and normalize its picks."""

    def __init__(self, *, read_draft: Callable | None = None) -> None:
        if read_draft is None:
            client = EspnFreeReadClient(maximum_bytes=_MAX_DRAFT_RESPONSE_BYTES)
            read_draft = client.read_draft
        if not callable(read_draft):
            raise ValueError("read_draft must be callable")
        self._read_draft = read_draft

    def poll(
        self,
        *,
        league_id: str,
        season: int,
        board: DraftPlayerBoard,
        team_count: int,
        roster_size: int,
    ) -> EspnDraftObservation:
        _requested_league_id(league_id)
        _integer("season", season, 2012, 9999)
        if not isinstance(board, DraftPlayerBoard):
            raise ValueError("board must be a DraftPlayerBoard")
        if season != board.season:
            raise ValueError("ESPN season must match the current draft board season")
        _integer("team_count", team_count, 2, 32)
        _integer("roster_size", roster_size, 1, 60)
        try:
            payload = self._read_draft(season, league_id, lambda: False)
        except EspnUnauthorizedError:
            raise EspnDraftSyncError(_PUBLIC_ACCESS_DENIED) from None
        except EspnFreeReadError:
            raise EspnDraftSyncError(
                "ESPN's public draft read failed. Verify the league ID, season, and "
                "that the league is publicly readable."
            ) from None
        return _observation(
            payload,
            requested_league_id=league_id,
            season=season,
            board=board,
            team_count=team_count,
            roster_size=roster_size,
        )


def _observation(
    payload: object,
    *,
    requested_league_id: str,
    season: int,
    board: DraftPlayerBoard,
    team_count: int,
    roster_size: int,
) -> EspnDraftObservation:
    root = _mapping("ESPN draft response", payload)
    if _access_denied_payload(root):
        raise EspnDraftSyncError(_PUBLIC_ACCESS_DENIED)
    actual_league_id = _positive_id("ESPN response league ID", root.get("id"))
    if actual_league_id != requested_league_id:
        raise EspnDraftSyncError("ESPN returned a different league than the one requested")
    actual_season = _required_integer(
        "ESPN response seasonId", root.get("seasonId"), 2012, 9999
    )
    if actual_season != season:
        raise EspnDraftSyncError("ESPN returned a different season than the one requested")

    settings = _mapping("ESPN settings", root.get("settings"))
    draft_settings = _mapping("ESPN draft settings", settings.get("draftSettings"))
    draft_type = draft_settings.get("type")
    if draft_type != "SNAKE":
        label = draft_type if isinstance(draft_type, str) and draft_type else "unknown"
        raise EspnDraftSyncError(
            f"ESPN draft type {label!r} is unsupported; public sync supports snake drafts only"
        )
    for key in ("keeperCount", "keeperCountFuture"):
        keeper_count = draft_settings.get(key, 0)
        if type(keeper_count) is not int or keeper_count < 0:
            raise EspnDraftSyncError("ESPN keeper settings have an unsupported shape")
        if keeper_count:
            raise EspnDraftSyncError("ESPN keeper drafts are not supported by live sync")

    pick_order_value = draft_settings.get("pickOrder")
    if not isinstance(pick_order_value, list):
        raise EspnDraftSyncError("ESPN pick order is missing or ambiguous")
    team_order = tuple(
        _positive_id(f"ESPN pickOrder[{index}]", value)
        for index, value in enumerate(pick_order_value)
    )
    if len(team_order) != team_count or len(set(team_order)) != team_count:
        raise EspnDraftSyncError(
            "ESPN pick order is ambiguous; it must name each league team exactly once"
        )

    detail = _mapping("ESPN draft detail", root.get("draftDetail"))
    drafted = _boolean("ESPN draftDetail.drafted", detail.get("drafted"))
    in_progress = _boolean(
        "ESPN draftDetail.inProgress", detail.get("inProgress")
    )
    if drafted and in_progress:
        raise EspnDraftSyncError("ESPN draft status is contradictory")
    raw_picks = detail.get("picks")
    if not isinstance(raw_picks, list):
        raise EspnDraftSyncError("ESPN draft picks have an unsupported shape")
    total_picks = team_count * roster_size
    if len(raw_picks) > total_picks:
        raise EspnDraftSyncError(
            "ESPN draft has more picks than the assistant league configuration"
        )

    picks_by_overall: dict[int, Mapping[str, object]] = {}
    for raw_pick in raw_picks:
        pick = _mapping("ESPN draft pick", raw_pick)
        overall = _required_integer(
            "ESPN overallPickNumber", pick.get("overallPickNumber"), 1, total_picks
        )
        if overall in picks_by_overall:
            raise EspnDraftSyncError("ESPN draft contains a duplicate overall pick number")
        picks_by_overall[overall] = pick
    expected_numbers = list(range(1, len(raw_picks) + 1))
    if sorted(picks_by_overall) != expected_numbers:
        raise EspnDraftSyncError(
            "ESPN observed picks are not a contiguous history beginning at pick 1"
        )
    if drafted and len(raw_picks) != total_picks:
        raise EspnDraftSyncError(
            "ESPN marks the draft complete but its pick count does not match this league"
        )

    provider_to_board = {
        provider_id: player_id
        for player_id, provider_id in board.espn_player_ids.items()
    }
    seen_players: set[str] = set()
    assistant_picks: list[tuple[int, str]] = []
    for overall in expected_numbers:
        pick = picks_by_overall[overall]
        expected_round = (overall - 1) // team_count + 1
        expected_round_pick = (overall - 1) % team_count + 1
        if (
            _required_integer(
                "ESPN roundId", pick.get("roundId"), 1, roster_size
            )
            != expected_round
            or _required_integer(
                "ESPN roundPickNumber", pick.get("roundPickNumber"), 1, team_count
            )
            != expected_round_pick
        ):
            raise EspnDraftSyncError(
                f"ESPN pick {overall} has an ambiguous round or round-pick number"
            )
        _reject_keeper_or_auction_pick(pick, overall)
        drafter_number = drafter_for_pick(overall, team_count)
        team_id = _positive_id("ESPN pick teamId", pick.get("teamId"))
        if team_id != team_order[drafter_number - 1]:
            raise EspnDraftSyncError(
                f"ESPN pick {overall} does not match the untraded snake order; "
                "traded picks and ambiguous draft order are unsupported"
            )
        provider_player_id = _player_id(
            "ESPN pick playerId", pick.get("playerId")
        )
        try:
            player_id = provider_to_board[provider_player_id]
        except KeyError:
            raise EspnDraftSyncError(
                f"ESPN player ID {provider_player_id} at pick {overall} is not mapped "
                "by the current draft board"
            ) from None
        if player_id in seen_players:
            raise EspnDraftSyncError(
                f"ESPN player ID {provider_player_id} appears in more than one pick"
            )
        seen_players.add(player_id)
        assistant_picks.append((drafter_number, player_id))

    return EspnDraftObservation(
        requested_league_id,
        season,
        team_order,
        tuple(assistant_picks),
        drafted,
        in_progress,
    )


def _reject_keeper_or_auction_pick(pick: Mapping[str, object], overall: int) -> None:
    for key in ("keeper", "reservedForKeeper"):
        value = pick.get(key, False)
        if type(value) is not bool:
            raise EspnDraftSyncError(f"ESPN pick {overall} has invalid keeper metadata")
        if value:
            raise EspnDraftSyncError("ESPN keeper drafts are not supported by live sync")
    if "bidAmount" not in pick:
        return
    bid = pick["bidAmount"]
    try:
        amount = float(bid)
    except (OverflowError, TypeError, ValueError):
        amount = math.nan
    if isinstance(bid, bool) or not isinstance(bid, Real) or not math.isfinite(amount):
        raise EspnDraftSyncError(f"ESPN pick {overall} has invalid auction metadata")
    if amount != 0.0:
        raise EspnDraftSyncError("ESPN auction picks are not supported by live sync")


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EspnDraftSyncError(f"{name} has an unsupported shape")
    return value


def _access_denied_payload(value: Mapping[str, object]) -> bool:
    messages = value.get("messages")
    if not isinstance(messages, list):
        return False
    text = " ".join(row.casefold() for row in messages if isinstance(row, str))
    return any(
        marker in text
        for marker in (
            "not authorized", "not authorised", "access denied", "private league",
            "authentication", "sign in", "login",
        )
    )


def _boolean(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise EspnDraftSyncError(f"{name} must be a boolean")
    return value


def _required_integer(name: str, value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise EspnDraftSyncError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _positive_id(name: str, value: object) -> str:
    result = _decimal_id(name, value, allow_negative=False)
    if result == "0":
        raise EspnDraftSyncError(f"{name} must be a positive decimal ID")
    return result


def _player_id(name: str, value: object) -> str:
    result = _decimal_id(name, value, allow_negative=True)
    if result == "0":
        raise EspnDraftSyncError(f"{name} must be a non-zero decimal ID")
    return result


def _decimal_id(name: str, value: object, *, allow_negative: bool) -> str:
    if type(value) is int:
        result = str(value)
    elif isinstance(value, str):
        result = value
    else:
        raise EspnDraftSyncError(f"{name} must be a decimal ID")
    digits = result[1:] if allow_negative and result.startswith("-") else result
    if (
        not digits
        or not digits.isascii()
        or not digits.isdigit()
        or (len(digits) > 1 and digits.startswith("0"))
        or len(digits) > 20
        or (result.startswith("-") and not allow_negative)
    ):
        raise EspnDraftSyncError(f"{name} must be a canonical decimal ID")
    return result


def _requested_league_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 20
    ):
        raise ValueError("league_id must be a positive decimal provider ID")


def _integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


__all__ = (
    "EspnDraftObservation",
    "EspnDraftSyncError",
    "EspnPublicDraftAdapter",
)
