"""Persistent, conflict-aware manual draft assistant sessions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from math import isfinite
import re
from uuid import uuid4

from .draft_config import DraftStrategy
from .draft_feasibility import validate_player_supply
from .draft_history import DraftPlayerBoard
from .draft_persistence import DraftModelArtifact
from .draft_simulation import rank_draft_candidates


_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class AssistantPick:
    overall_pick: int
    drafter_number: int
    player_id: str

    def __post_init__(self):
        if type(self.overall_pick) is not int or self.overall_pick < 1:
            raise ValueError("overall_pick must be positive")
        if type(self.drafter_number) is not int or self.drafter_number < 1:
            raise ValueError("drafter_number must be positive")
        if not isinstance(self.player_id, str) or not self.player_id:
            raise ValueError("player_id must be non-empty text")

    def to_record(self):
        return {
            "overall_pick": self.overall_pick,
            "drafter_number": self.drafter_number,
            "player_id": self.player_id,
        }


@dataclass(frozen=True, slots=True)
class AssistantDraftBinding:
    """Immutable identity of the public draft attached to an assistant room."""

    provider: str
    league_id: str
    season: int
    team_order: tuple[str, ...]

    def __post_init__(self):
        if self.provider != "espn":
            raise ValueError("assistant draft provider is unsupported")
        _positive_decimal_id("league_id", self.league_id)
        if type(self.season) is not int or not 2012 <= self.season <= 9999:
            raise ValueError("assistant draft season is invalid")
        if isinstance(self.team_order, (str, bytes)):
            raise ValueError("assistant draft team order must be a sequence of IDs")
        try:
            team_order = tuple(self.team_order)
        except TypeError:
            raise ValueError("assistant draft team order must be a sequence") from None
        if not 2 <= len(team_order) <= 32:
            raise ValueError("assistant draft team order has an unsupported size")
        for team_id in team_order:
            _positive_decimal_id("team ID", team_id)
        if len(set(team_order)) != len(team_order):
            raise ValueError("assistant draft team order contains a duplicate team")
        object.__setattr__(self, "team_order", team_order)

    def to_record(self):
        return {
            "provider": self.provider,
            "league_id": self.league_id,
            "season": self.season,
            "team_order": list(self.team_order),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]):
        keys = {"provider", "league_id", "season", "team_order"}
        if not isinstance(record, Mapping) or set(record) != keys:
            raise ValueError("assistant draft binding fields are invalid")
        if not isinstance(record["team_order"], list):
            raise ValueError("assistant draft team order must be a JSON array")
        return cls(
            record["provider"], record["league_id"], record["season"],
            tuple(record["team_order"]),
        )


@dataclass(frozen=True, slots=True)
class DraftAssistantSession:
    session_id: str
    model_id: str
    board_id: str
    user_drafter_number: int
    strategy: DraftStrategy
    picks: tuple[AssistantPick, ...] = ()
    draft_binding: AssistantDraftBinding | None = None

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not _SESSION_ID.fullmatch(self.session_id):
            raise ValueError("assistant session_id is invalid")
        if not isinstance(self.model_id, str) or not self.model_id.startswith("draft_model_"):
            raise ValueError("assistant model_id is invalid")
        if not isinstance(self.board_id, str) or not self.board_id.startswith("draft_board_"):
            raise ValueError("assistant board_id is invalid")
        if type(self.user_drafter_number) is not int or self.user_drafter_number < 1:
            raise ValueError("user_drafter_number must be positive")
        if not isinstance(self.strategy, DraftStrategy):
            raise ValueError("assistant strategy is invalid")
        picks = tuple(self.picks)
        if any(not isinstance(row, AssistantPick) for row in picks):
            raise ValueError("assistant picks are invalid")
        if tuple(row.overall_pick for row in picks) != tuple(range(1, len(picks) + 1)):
            raise ValueError("assistant picks must be contiguous")
        if len({row.player_id for row in picks}) != len(picks):
            raise ValueError("assistant picks cannot contain a player twice")
        if self.draft_binding is not None and not isinstance(
            self.draft_binding, AssistantDraftBinding
        ):
            raise ValueError("assistant draft binding is invalid")
        object.__setattr__(self, "picks", picks)

    def to_record(self):
        return {
            "kind": "draft_assistant_session", "schema_version": 2,
            "session_id": self.session_id, "model_id": self.model_id,
            "board_id": self.board_id,
            "user_drafter_number": self.user_drafter_number,
            "strategy": self.strategy.value,
            "picks": [row.to_record() for row in self.picks],
            "draft_binding": (
                None if self.draft_binding is None else self.draft_binding.to_record()
            ),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]):
        legacy_keys = {
            "kind", "schema_version", "session_id", "model_id", "board_id",
            "user_drafter_number", "strategy", "picks",
        }
        if not isinstance(record, Mapping):
            raise ValueError("assistant session fields are invalid")
        version = record.get("schema_version")
        if type(version) is not int or version not in {1, 2}:
            raise ValueError("assistant session kind or version is invalid")
        expected_keys = legacy_keys if version == 1 else legacy_keys | {"draft_binding"}
        if set(record) != expected_keys:
            raise ValueError("assistant session fields are invalid")
        if record["kind"] != "draft_assistant_session":
            raise ValueError("assistant session kind or version is invalid")
        if not isinstance(record["picks"], list):
            raise ValueError("assistant picks must be a JSON array")
        try:
            strategy = DraftStrategy(record["strategy"])
        except (TypeError, ValueError):
            raise ValueError("assistant strategy is invalid") from None
        picks = []
        for row in record["picks"]:
            if not isinstance(row, Mapping) or set(row) != {
                "overall_pick", "drafter_number", "player_id"
            }:
                raise ValueError("assistant pick fields are invalid")
            picks.append(AssistantPick(**row))
        binding_record = None if version == 1 else record["draft_binding"]
        if binding_record is not None and not isinstance(binding_record, Mapping):
            raise ValueError("assistant draft binding is invalid")
        binding = (
            None if binding_record is None
            else AssistantDraftBinding.from_record(binding_record)
        )
        return cls(
            record["session_id"], record["model_id"], record["board_id"],
            record["user_drafter_number"], strategy, tuple(picks), binding,
        )


def create_assistant_session(
    model: DraftModelArtifact,
    board: DraftPlayerBoard,
    *,
    user_drafter_number: int,
    strategy: DraftStrategy = DraftStrategy.NONE,
    session_id: str | None = None,
) -> DraftAssistantSession:
    if not isinstance(model, DraftModelArtifact) or not isinstance(board, DraftPlayerBoard):
        raise ValueError("assistant requires a saved model and current player board")
    if type(user_drafter_number) is not int or not 1 <= user_drafter_number <= model.league_config.team_count:
        raise ValueError("user_drafter_number is outside this league")
    session = DraftAssistantSession(
        session_id or uuid4().hex, model.model_id, board.board_id,
        user_drafter_number, strategy,
    )
    _session_board_coverage(session, model, board)
    return session


def bind_assistant_draft(
    session: DraftAssistantSession,
    binding: AssistantDraftBinding,
) -> DraftAssistantSession:
    """Bind a room once, allowing only idempotent reuse of the same draft."""

    if not isinstance(session, DraftAssistantSession):
        raise ValueError("session must be a DraftAssistantSession")
    if not isinstance(binding, AssistantDraftBinding):
        raise ValueError("binding must be an AssistantDraftBinding")
    if session.draft_binding is None:
        return replace(session, draft_binding=binding)
    if session.draft_binding != binding:
        raise ValueError("assistant room is already bound to a different public draft")
    return session


def record_assistant_pick(
    session: DraftAssistantSession,
    model: DraftModelArtifact,
    board: DraftPlayerBoard,
    *,
    player_id: str,
    drafter_number: int,
) -> DraftAssistantSession:
    _validate_session(session, model, board)
    _session_board_coverage(session, model, board)
    return _append_assistant_pick(
        session, model, board, player_id=player_id, drafter_number=drafter_number
    )


def _append_assistant_pick(
    session: DraftAssistantSession,
    model: DraftModelArtifact,
    board: DraftPlayerBoard,
    *,
    player_id: str,
    drafter_number: int,
) -> DraftAssistantSession:
    if len(session.picks) >= model.league_config.team_count * model.league_config.roster_size:
        raise ValueError("draft is already complete")
    overall = len(session.picks) + 1
    expected = drafter_for_pick(overall, model.league_config.team_count)
    if drafter_number != expected:
        raise ValueError(f"pick {overall} belongs to Drafter #{expected}")
    if player_id not in {row.player_id for row in board.players}:
        raise ValueError("player is not on the current draft board")
    if player_id in {row.player_id for row in session.picks}:
        raise ValueError("player has already been drafted")
    return replace(
        session,
        picks=(*session.picks, AssistantPick(overall, drafter_number, player_id)),
    )


def undo_assistant_pick(session: DraftAssistantSession) -> DraftAssistantSession:
    if not session.picks:
        raise ValueError("there is no draft pick to undo")
    return replace(session, picks=session.picks[:-1])


def reconcile_assistant_picks(
    session: DraftAssistantSession,
    model: DraftModelArtifact,
    board: DraftPlayerBoard,
    picks: Sequence[tuple[int, str]],
) -> DraftAssistantSession:
    """Idempotently append observed picks, rejecting any conflicting history."""

    _validate_session(session, model, board)
    _session_board_coverage(session, model, board)
    result = session
    for index, (drafter_number, player_id) in enumerate(picks):
        if index < len(session.picks):
            existing = session.picks[index]
            if (existing.drafter_number, existing.player_id) != (drafter_number, player_id):
                raise ValueError(f"observed draft conflicts at pick {index + 1}")
            continue
        result = _append_assistant_pick(
            result, model, board, player_id=player_id, drafter_number=drafter_number
        )
    return result


def assistant_status(
    session: DraftAssistantSession,
    model: DraftModelArtifact,
    board: DraftPlayerBoard,
    *,
    recommendation_limit: int = 10,
) -> dict[str, object]:
    _validate_session(session, model, board)
    coverage = _session_board_coverage(session, model, board)
    if type(recommendation_limit) is not int or not 1 <= recommendation_limit <= 50:
        raise ValueError("recommendation_limit must be between 1 and 50")
    config = model.league_config
    total = config.team_count * config.roster_size
    next_pick = len(session.picks) + 1
    complete = next_pick > total
    next_drafter = None if complete else drafter_for_pick(next_pick, config.team_count)
    round_number = None if complete else (next_pick - 1) // config.team_count + 1
    players = {row.player_id: row for row in board.players}
    rosters = [[] for _ in range(config.team_count)]
    for pick in session.picks:
        rosters[pick.drafter_number - 1].append(pick.player_id)
    available = tuple(row for row in board.players if row.player_id not in {
        pick.player_id for pick in session.picks
    })
    recommendations = []
    if not complete and next_drafter == session.user_drafter_number:
        ranked = rank_draft_candidates(
            board, config, model.brain, session.strategy,
            roster_player_ids=rosters[next_drafter - 1], available_players=available,
            round_number=round_number, overall_pick=next_pick,
            drafter_number=next_drafter, candidate_window=0,
            all_roster_player_ids=tuple(tuple(row) for row in rosters),
            all_strategies=tuple(
                session.strategy if index == session.user_drafter_number - 1
                else DraftStrategy.NONE
                for index in range(config.team_count)
            ),
        )
        for rank, row in enumerate(ranked[:recommendation_limit], 1):
            player = players[row.player_id]
            recommendations.append({
                "rank": rank, "player_id": row.player_id,
                "player_name": player.display_name, "position": player.position,
                "utility": row.utility, "baseline_utility": row.baseline_utility,
                "neural_adjustment": row.neural_adjustment,
                "reason": (
                    "Fills an open starter slot"
                    if row.starter_need else
                    f"Adds depth; {row.position_count} {player.position} already rostered"
                ),
            })
    binding_record = (
        None if session.draft_binding is None else session.draft_binding.to_record()
    )
    live_sync = {
        "status": "manual",
        "message": (
            "This response was not refreshed from a public draft. Enter picks "
            "manually or poll ESPN public sync."
        ),
    }
    if session.draft_binding is not None:
        live_sync = {
            "status": "bound",
            "provider": session.draft_binding.provider,
            "access": "public",
            "polling": "on_demand",
            "league_id": session.draft_binding.league_id,
            "season": session.draft_binding.season,
            "drafter_team_ids": list(session.draft_binding.team_order),
            "message": (
                "This room is bound to its saved ESPN public draft. Poll public "
                "sync to refresh observed picks; no cookies or credentials are sent."
            ),
        }
    return {
        "session_id": session.session_id,
        "model_id": session.model_id,
        "board_id": session.board_id,
        "complete": complete,
        "overall_pick": None if complete else next_pick,
        "round": round_number,
        "next_drafter_number": next_drafter,
        "next_drafter_name": None if complete else f"Drafter #{next_drafter}",
        "your_turn": next_drafter == session.user_drafter_number,
        "available_player_count": len(available),
        "board_coverage": coverage,
        "draft_binding": binding_record,
        "picks": [
            {
                **pick.to_record(),
                "player_name": players[pick.player_id].display_name,
                "position": players[pick.player_id].position,
            }
            for pick in session.picks
        ],
        "recommendations": recommendations,
        "live_sync": live_sync,
    }


def drafter_for_pick(overall_pick: int, team_count: int) -> int:
    if type(overall_pick) is not int or overall_pick < 1 or type(team_count) is not int or team_count < 2:
        raise ValueError("pick and team_count are invalid")
    round_number, offset = divmod(overall_pick - 1, team_count)
    return offset + 1 if round_number % 2 == 0 else team_count - offset


def _validate_session(session, model, board):
    if not isinstance(session, DraftAssistantSession):
        raise ValueError("session must be a DraftAssistantSession")
    if session.model_id != model.model_id or session.board_id != board.board_id:
        raise ValueError("assistant session does not match its model or player board")
    if session.user_drafter_number > model.league_config.team_count:
        raise ValueError("assistant drafter number is outside this league")
    if (
        session.draft_binding is not None
        and len(session.draft_binding.team_order) != model.league_config.team_count
    ):
        raise ValueError("assistant draft binding does not match its league size")
    if len(session.picks) > model.league_config.team_count * model.league_config.roster_size:
        raise ValueError("assistant session contains too many draft picks")
    board_ids = {player.player_id for player in board.players}
    for pick in session.picks:
        if pick.player_id not in board_ids:
            raise ValueError("assistant session contains a player outside its board")
        if pick.drafter_number > model.league_config.team_count:
            raise ValueError("assistant session contains a drafter outside its league")
        expected = drafter_for_pick(pick.overall_pick, model.league_config.team_count)
        if pick.drafter_number != expected:
            raise ValueError("assistant pick order is invalid")


def assistant_board_coverage(
    model: DraftModelArtifact,
    board: DraftPlayerBoard,
    *,
    user_drafter_number: int,
    strategy: DraftStrategy,
) -> dict[str, object]:
    """Validate and summarize usable current-board inputs for a saved model."""

    if not isinstance(model, DraftModelArtifact) or not isinstance(
        board, DraftPlayerBoard
    ):
        raise ValueError("assistant requires a saved model and current player board")
    if (
        type(user_drafter_number) is not int
        or not 1 <= user_drafter_number <= model.league_config.team_count
        or not isinstance(strategy, DraftStrategy)
    ):
        raise ValueError("assistant drafter or strategy is invalid")
    required_players = model.league_config.team_count * model.league_config.roster_size
    if len(board.players) < required_players:
        raise ValueError(f"draft board needs at least {required_players} players")
    schema_preseason = frozenset({
        name.removeprefix("preseason.")
        for name in model.brain.schema.names if name.startswith("preseason.")
    })
    declared_preseason = frozenset(
        name
        for player in board.players
        for name in player.preseason_features
    )
    omitted_preseason = sorted(schema_preseason.difference(declared_preseason))
    if omitted_preseason:
        raise ValueError(
            "draft board omits model preseason feature "
            f"{omitted_preseason[0]!r}"
        )
    usable_players = sum(
        any(
            value is not None and isfinite(value)
            for name in schema_preseason
            for value in (player.preseason_features.get(name),)
        )
        for player in board.players
    )
    if usable_players < required_players:
        raise ValueError(
            "draft board needs at least "
            f"{required_players} players with a finite model preseason feature; "
            f"found {usable_players}"
        )
    strategies = tuple(
        strategy if index == user_drafter_number - 1 else DraftStrategy.NONE
        for index in range(model.league_config.team_count)
    )
    validate_player_supply(board.players, model.league_config, strategies)
    return {
        "status": "ready",
        "board_player_count": len(board.players),
        "usable_player_count": usable_players,
        "required_usable_player_count": required_players,
        "model_preseason_feature_count": len(schema_preseason),
        "feasibility": {
            "status": "ready",
            "scope": "all_teams",
            "starting_slots_checked": (
                model.league_config.team_count
                * len(model.league_config.starting_slots)
            ),
        },
    }


def _session_board_coverage(session, model, board):
    return assistant_board_coverage(
        model,
        board,
        user_drafter_number=session.user_drafter_number,
        strategy=session.strategy,
    )


def _positive_decimal_id(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 20
    ):
        raise ValueError(f"assistant draft {name} must be a positive decimal ID")


__all__ = (
    "AssistantDraftBinding", "AssistantPick", "DraftAssistantSession",
    "assistant_board_coverage", "assistant_status", "bind_assistant_draft",
    "create_assistant_session", "drafter_for_pick", "reconcile_assistant_picks",
    "record_assistant_pick", "undo_assistant_pick",
)
