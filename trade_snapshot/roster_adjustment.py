"""Deterministic local add/drop handling for imbalanced trade packages."""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations

from ._scenario_random import content_id
from .strength import StrengthModel
from .trade_space import TeamRoster, TradeCandidate


ROSTER_ADJUSTMENT_ALGORITHM = "post-trade-optimal-replacement-v3"
MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY = (
    "When automatic roster adjustments are allowed, each team's drops are "
    "optimized locally and scarce free-agent replacements are reserved in "
    "ascending team-ID order from the players still available."
)


@dataclass(frozen=True, slots=True)
class TeamRosterAdjustment:
    roster: TeamRoster
    added_player_ids: tuple[str, ...] = ()
    dropped_player_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.roster, TeamRoster):
            raise ValueError("roster must be a TeamRoster")
        added = _ids("added_player_ids", self.added_player_ids)
        dropped = _ids("dropped_player_ids", self.dropped_player_ids)
        if set(added).intersection(dropped):
            raise ValueError("an adjusted roster cannot add and drop the same player")
        if not set(added).issubset(self.roster.player_ids):
            raise ValueError("added players must be present on the adjusted roster")
        if set(dropped).intersection(self.roster.player_ids):
            raise ValueError("dropped players cannot remain on the adjusted roster")
        object.__setattr__(self, "added_player_ids", added)
        object.__setattr__(self, "dropped_player_ids", dropped)


@dataclass(frozen=True, slots=True)
class TradeRosterAdjustment:
    primary: TeamRosterAdjustment
    counterparty: TeamRosterAdjustment

    def __post_init__(self) -> None:
        if not isinstance(self.primary, TeamRosterAdjustment) or not isinstance(
            self.counterparty, TeamRosterAdjustment
        ):
            raise ValueError("trade adjustment requires two team adjustments")
        if self.primary.roster.team_id == self.counterparty.roster.team_id:
            raise ValueError("trade adjustment teams must be different")
        overlap = set(self.primary.roster.player_ids).intersection(
            self.counterparty.roster.player_ids
        )
        if overlap:
            raise ValueError("adjusted teams cannot share a player")


class PreparedRosterAdjuster:
    """Select exact post-trade drops and replacements from the local player pool."""

    def __init__(self, model: StrengthModel, rosters: Iterable[TeamRoster]) -> None:
        if not isinstance(model, StrengthModel):
            raise ValueError("model must be a StrengthModel")
        rows = tuple(rosters)
        if len(rows) < 2 or any(not isinstance(row, TeamRoster) for row in rows):
            raise ValueError("rosters must contain at least two TeamRoster values")
        by_team = {row.team_id: row for row in rows}
        if len(by_team) != len(rows):
            raise ValueError("rosters contain a duplicate team")
        owned = set()
        for row in rows:
            if row.current_size != len(row.player_ids):
                raise ValueError("rosters must contain complete current player lists")
            if not set(row.player_ids).issubset(model.players):
                raise ValueError("rosters contain a player absent from the strength model")
            if owned.intersection(row.player_ids):
                raise ValueError("league rosters cannot share a player")
            owned.update(row.player_ids)
        free_agents = tuple(sorted(set(model.players).difference(owned)))
        self.model = model
        self.rosters = by_team
        self.free_agent_ids = free_agents
        self.adjustment_id = content_id(
            "radj",
            {
                "algorithm": ROSTER_ADJUSTMENT_ALGORITHM,
                "free_agent_ids": list(free_agents),
                "model_id": model.model_id,
                "rosters": [
                    {
                        "active_size": row.active_size,
                        "capacity_exempt_player_ids": sorted(
                            row.capacity_exempt_player_ids
                        ),
                        "current_size": row.current_size,
                        "player_ids": list(row.player_ids),
                        "roster_cap": row.roster_cap,
                        "team_id": row.team_id,
                    }
                    for row in sorted(rows, key=lambda value: value.team_id)
                ],
            },
        )

    def adjust_trade(
        self,
        primary: TeamRoster,
        counterparty: TeamRoster,
        candidate: TradeCandidate,
    ) -> TradeRosterAdjustment:
        self._require_roster(primary)
        self._require_roster(counterparty)
        outgoing = set(candidate.outgoing_player_ids)
        incoming = set(candidate.incoming_player_ids)
        if not outgoing or not incoming:
            raise ValueError("both trade packages must contain at least one player")
        if not outgoing.issubset(primary.player_ids) or not incoming.issubset(
            counterparty.player_ids
        ):
            raise ValueError("trade packages do not belong to the selected teams")
        primary_raw = tuple(
            player for player in primary.player_ids if player not in outgoing
        ) + candidate.incoming_player_ids
        counterparty_raw = tuple(
            player for player in counterparty.player_ids if player not in incoming
        ) + candidate.outgoing_player_ids
        primary_after = self._balance(
            primary,
            primary_raw,
            capacity_exempt_player_ids=primary.capacity_exempt_player_ids.difference(
                outgoing
            ),
            protected=set(candidate.incoming_player_ids),
        )
        counterparty_after = self._balance(
            counterparty,
            counterparty_raw,
            capacity_exempt_player_ids=(
                counterparty.capacity_exempt_player_ids.difference(incoming)
            ),
            protected=set(candidate.outgoing_player_ids),
        )
        return TradeRosterAdjustment(primary_after, counterparty_after)

    def adjust_teams(
        self,
        changes: Iterable[
            tuple[TeamRoster, Iterable[str], Iterable[str]]
        ],
    ) -> tuple[TeamRosterAdjustment, ...]:
        """Balance one simultaneous transfer across two or more league teams.

        Each row contains the before roster, players sent, and players received.
        Teams reserve free agents in canonical team-ID order, then results are
        returned in caller order. This makes one transaction independent of
        which participant was selected as the primary team.
        """

        normalized = _validated_team_changes(self, changes)
        available_free_agents = self.free_agent_ids
        by_team = {}
        for roster, outgoing, incoming in sorted(
            normalized, key=lambda row: row[0].team_id
        ):
            outgoing_set = set(outgoing)
            raw_players = tuple(
                player for player in roster.player_ids if player not in outgoing_set
            ) + incoming
            adjusted = self._balance(
                roster,
                raw_players,
                capacity_exempt_player_ids=(
                    roster.capacity_exempt_player_ids.difference(outgoing_set)
                ),
                protected=set(incoming),
                free_agent_ids=available_free_agents,
            )
            additions = set(adjusted.added_player_ids)
            available_free_agents = tuple(
                player_id
                for player_id in available_free_agents
                if player_id not in additions
            )
            by_team[roster.team_id] = adjusted
        adjustments = tuple(by_team[roster.team_id] for roster, _, _ in normalized)
        owned_after = tuple(
            player_id
            for adjustment in adjustments
            for player_id in adjustment.roster.player_ids
        )
        if len(set(owned_after)) != len(owned_after):
            raise ValueError("simultaneous adjustment assigned one player twice")
        return adjustments

    def _balance(
        self,
        before,
        raw_players,
        *,
        capacity_exempt_player_ids,
        protected,
        free_agent_ids=None,
    ) -> TeamRosterAdjustment:
        players = list(raw_players)
        capacity_exempt = frozenset(capacity_exempt_player_ids)
        active_count = len(players) - len(capacity_exempt)
        excess = max(0, active_count - before.roster_cap)
        dropped = self._optimal_drops(
            players,
            protected.union(capacity_exempt),
            excess,
        )
        players = [player for player in players if player not in set(dropped)]
        active_count = len(players) - len(capacity_exempt)
        needed = max(0, before.active_size - active_count)
        additions = self._optimal_additions(
            players,
            needed,
            free_agent_ids=free_agent_ids,
        )
        players.extend(additions)
        if len(players) - len(capacity_exempt) > before.roster_cap:
            raise ValueError("adjusted roster exceeds its roster cap")
        roster = TeamRoster(
            before.team_id,
            tuple(players),
            len(players),
            before.roster_cap,
            capacity_exempt,
        )
        return TeamRosterAdjustment(roster, tuple(additions), tuple(dropped))

    def _optimal_drops(self, players, protected, count):
        if count == 0:
            return ()
        candidates = tuple(sorted(set(players).difference(protected)))
        if len(candidates) < count:
            raise ValueError("trade requires dropping a protected incoming player")
        return _best_package(
            combinations(candidates, count),
            lambda package: tuple(player for player in players if player not in package),
            self.model,
        )

    def _optimal_additions(self, players, count, *, free_agent_ids=None):
        if count == 0:
            return ()
        pool = self.free_agent_ids if free_agent_ids is None else tuple(free_agent_ids)
        candidates = tuple(
            player for player in pool if player not in players
        )
        available_count = min(count, len(candidates))
        if available_count == 0:
            return ()
        return _best_package(
            combinations(candidates, available_count),
            lambda package: (*players, *package),
            self.model,
        )

    def _require_roster(self, roster):
        expected = self.rosters.get(roster.team_id)
        if expected is None or (
            frozenset(expected.player_ids) != frozenset(roster.player_ids)
            or expected.current_size != roster.current_size
            or expected.roster_cap != roster.roster_cap
            or expected.capacity_exempt_player_ids
            != roster.capacity_exempt_player_ids
        ):
            raise ValueError("trade roster does not match the prepared weekly league")


def unchanged_trade_adjustment(
    model: StrengthModel,
    primary: TeamRoster,
    counterparty: TeamRoster,
    candidate: TradeCandidate,
) -> TradeRosterAdjustment:
    """Apply a pure trade while retaining explicit adjustment-shaped output."""

    outgoing = set(candidate.outgoing_player_ids)
    incoming = set(candidate.incoming_player_ids)
    primary_players = tuple(
        player for player in primary.player_ids if player not in outgoing
    ) + candidate.incoming_player_ids
    counterparty_players = tuple(
        player for player in counterparty.player_ids if player not in incoming
    ) + candidate.outgoing_player_ids
    if not set(primary_players).issubset(model.players) or not set(
        counterparty_players
    ).issubset(model.players):
        raise ValueError("trade contains a player absent from the strength model")
    return TradeRosterAdjustment(
        TeamRosterAdjustment(
            TeamRoster(
                primary.team_id,
                primary_players,
                len(primary_players),
                primary.roster_cap,
                primary.capacity_exempt_player_ids.difference(outgoing),
            )
        ),
        TeamRosterAdjustment(
            TeamRoster(
                counterparty.team_id,
                counterparty_players,
                len(counterparty_players),
                counterparty.roster_cap,
                counterparty.capacity_exempt_player_ids.difference(incoming),
            )
        ),
    )


def _best_package(packages, roster_for_package, model):
    best_package = None
    best_score = None
    for package in packages:
        score = model.score_roster(roster_for_package(package)).absolute_score
        if (
            best_score is None
            or score > best_score
            or (score == best_score and package < best_package)
        ):
            best_package = package
            best_score = score
    if best_package is None:
        raise AssertionError("replacement package search produced no candidates")
    return best_package


def _validated_team_changes(adjuster, values):
    rows = tuple(values)
    if len(rows) < 2:
        raise ValueError("changes must contain at least two teams")
    result = []
    seen_teams = set()
    all_outgoing = []
    all_incoming = []
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 3:
            raise ValueError("each team change must contain roster, sent, and received")
        roster, outgoing_values, incoming_values = row
        if not isinstance(roster, TeamRoster):
            raise ValueError("team changes must contain TeamRoster values")
        adjuster._require_roster(roster)
        if roster.team_id in seen_teams:
            raise ValueError("team changes contain a duplicate team")
        seen_teams.add(roster.team_id)
        outgoing = _ids("sent player IDs", outgoing_values)
        incoming = _ids("received player IDs", incoming_values)
        if not set(outgoing).issubset(roster.player_ids):
            raise ValueError("sent players must belong to their source team")
        if set(incoming).intersection(roster.player_ids):
            raise ValueError("received players cannot already belong to the team")
        result.append((roster, outgoing, incoming))
        all_outgoing.extend(outgoing)
        all_incoming.extend(incoming)
    if (
        len(set(all_outgoing)) != len(all_outgoing)
        or len(set(all_incoming)) != len(all_incoming)
        or set(all_outgoing) != set(all_incoming)
    ):
        raise ValueError("simultaneous transfers must conserve unique player ownership")
    return tuple(result)


def _ids(name, values):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    rows = tuple(values)
    if any(not isinstance(value, str) or not value for value in rows):
        raise ValueError(f"{name} must contain non-empty player IDs")
    if len(set(rows)) != len(rows):
        raise ValueError(f"{name} contains a duplicate player")
    return rows
