"""Deterministic injury replacements and shared waivers for one simulated league."""

from collections import Counter
from dataclasses import dataclass

from .draft_availability import AvailabilityStatus
from .lineup import LineupPlayer, optimize_lineup


ROSTER_MANAGEMENT_POLICY = (
    "Bench players with known OUT/IR reports; drop explicitly reported "
    "season-ending IR. Optional zero-point absence rules are inferred, not "
    "confirmed injuries, and are disabled by default. Fill vacancies from the shared free-agent pool in reverse "
    "standings order, one claim per team per pass (reverse draft order initially). "
    "Claims respect eligibility and position caps and use preseason projections "
    "plus completed prior weeks, never future scores. Unknown status is not a "
    "confirmed healthy designation. No FAAB, host-specific waiver rules, or real "
    "league transactions are performed."
)


@dataclass(frozen=True, slots=True)
class RosterMove:
    week: int
    action: str
    player_id: str | None
    player_name: str | None
    reason: str
    source: str


def optimize_roster(roster, config, players, weights):
    candidates = []
    for player_id in roster:
        if player_id not in weights:
            continue
        player = players[player_id]
        slot_weights = {
            slot: weights[player_id]
            for slot in set(config.starting_slots)
            if set(player.eligible_positions).intersection(config.slot_eligibility[slot])
        }
        candidates.append(LineupPlayer(player_id, slot_weights))
    return optimize_lineup(config.starting_slots, candidates)


class SeasonRosterManager:
    """Own mutable rosters and exclusive free-agent ownership inside one arena."""

    def __init__(self, rosters, players, config):
        self.rosters = {team: list(roster) for team, roster in rosters.items()}
        self.players = players
        self.config = config
        owned = {player_id for roster in rosters.values() for player_id in roster}
        self.free_agents = set(players).difference(owned)
        self.moves = {team: [] for team in rosters}

    def prepare_week(self, week, priority, weights, availability):
        if not availability and all(len(self.rosters[team]) == self.config.roster_size for team in priority):
            return
        for team in priority:
            for player_id in tuple(self.rosters[team]):
                report = availability.get(player_id)
                if report is None:
                    continue
                if report.status in {AvailabilityStatus.SEASON_ENDING_IR, AvailabilityStatus.EXTENDED_ABSENCE}:
                    self.rosters[team].remove(player_id)
                    self.free_agents.add(player_id)
                    self._record(team, week, "drop", player_id,
                                 ("Extended zero-point absence under the optional simulation rule; not a confirmed injury."
                                  if report.status.inferred
                                  else "Explicit season-ending IR; roster slot released."), report.source)
                else:
                    self._record(team, week, "bench", player_id,
                                 ("Optional zero-point absence rule; not a confirmed injury."
                                  if report.status.inferred else
                                  f"Known {report.status.value.upper()}; withheld from this week's lineup."),
                                 report.source)

        waiting = [team for team in priority if len(self.rosters[team]) < self.config.roster_size]
        if not waiting:
            return
        ranked = sorted(self.free_agents.intersection(weights),
                        key=lambda player_id: (-weights[player_id], player_id))
        while waiting:
            next_pass = []
            for team in waiting:
                candidate = self._best_claim(self.rosters[team], ranked, weights)
                if candidate is None:
                    self._record(team, week, "unfilled", None,
                                 "No eligible, available waiver replacement fits the roster limits; retry next week.",
                                 "Local shared waiver pool")
                    continue
                self.rosters[team].append(candidate)
                self.free_agents.remove(candidate)
                self._record(team, week, "add", candidate,
                             "Waiver replacement: roster need, projected lineup value, then depth value.",
                             "Preseason projections and completed prior weeks; reverse-standings priority")
                if len(self.rosters[team]) < self.config.roster_size:
                    next_pass.append(team)
            waiting = next_pass

    def _best_claim(self, roster, ranked, weights):
        counts = Counter(self.players[player_id].position for player_id in roster)
        signatures = set()
        best = None
        best_value = None
        for player_id in ranked:
            if player_id not in self.free_agents:
                continue
            player = self.players[player_id]
            if not any(set(player.eligible_positions).intersection(positions)
                       for positions in self.config.slot_eligibility.values()):
                continue
            if counts[player.position] >= self.config.position_limits.get(player.position, self.config.roster_size):
                continue
            signature = (player.position, player.eligible_positions)
            if signature in signatures:
                continue
            signatures.add(signature)
            candidate_roster = (*roster, player_id)
            structural = optimize_roster(candidate_roster, self.config, self.players,
                                         dict.fromkeys(candidate_roster, 1.0))
            weekly = optimize_roster(candidate_roster, self.config, self.players, weights)
            value = (
                sum(row.player_id is not None for row in structural.assignments),
                weekly.total_weight,
                weights[player_id],
            )
            if best_value is None or value > best_value:
                best, best_value = player_id, value
        return best

    def _record(self, team, week, action, player_id, reason, source):
        self.moves[team].append(RosterMove(
            week, action, player_id,
            None if player_id is None else self.players[player_id].display_name,
            reason, source,
        ))
