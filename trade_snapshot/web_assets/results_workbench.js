"use strict";

window.ResultsWorkbench = (() => {
  const TWO_TEAM = "two_team";
  const THREE_TEAM = "three_team";
  const SORT_KEYS = Object.freeze([
    "combined_playoff_gain",
    "my_playoff_gain",
    "weakest_participant_gain",
    "combined_power_gain",
    "fewest_moved_players"
  ]);
  const SORT_KEY_SET = new Set(SORT_KEYS);
  const CONTROL_KEYS = new Set([
    "onlyAllParticipantsImprove",
    "minimumPlayoffGainPoints",
    "sortBy"
  ]);
  const COMPARISON_TOLERANCE = 1e-12;

  function requireRecord(value, name) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new TypeError(`${name} must be an object.`);
    }
    return value;
  }

  function requireText(value, name) {
    if (typeof value !== "string" || !value.trim()) {
      throw new TypeError(`${name} must be a non-empty string.`);
    }
    return value;
  }

  function requireFiniteNumber(value, name) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new TypeError(`${name} must be a finite number.`);
    }
    return value;
  }

  function requireTextArray(value, name) {
    if (!Array.isArray(value) || value.some(item => typeof item !== "string")) {
      throw new TypeError(`${name} must be an array of strings.`);
    }
    return value;
  }

  function readMinimumGainPoints(value) {
    if (value === undefined || value === null || value === "") return null;
    let parsed = value;
    if (typeof value === "string") {
      if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)) {
        throw new TypeError("minimumPlayoffGainPoints must be a non-negative number.");
      }
      parsed = Number(value);
    }
    if (typeof parsed !== "number" || !Number.isFinite(parsed) || parsed < 0) {
      throw new TypeError("minimumPlayoffGainPoints must be a non-negative number.");
    }
    return parsed;
  }

  function readControlValues(values = {}) {
    requireRecord(values, "controlValues");
    for (const key of Object.keys(values)) {
      if (!CONTROL_KEYS.has(key)) throw new TypeError(`Unknown control value: ${key}.`);
    }
    const onlyAllParticipantsImprove = values.onlyAllParticipantsImprove ?? false;
    if (typeof onlyAllParticipantsImprove !== "boolean") {
      throw new TypeError("onlyAllParticipantsImprove must be a boolean.");
    }
    const sortBy = values.sortBy ?? "combined_playoff_gain";
    if (typeof sortBy !== "string" || !SORT_KEY_SET.has(sortBy)) {
      throw new TypeError(`sortBy must be one of: ${SORT_KEYS.join(", ")}.`);
    }
    return Object.freeze({
      onlyAllParticipantsImprove,
      minimumPlayoffGainPoints: readMinimumGainPoints(
        values.minimumPlayoffGainPoints
      ),
      sortBy
    });
  }

  function twoTeamMetrics(row, index) {
    requireRecord(row, `rows[${index}]`);
    const give = requireTextArray(row.give, `rows[${index}].give`);
    const receive = requireTextArray(row.receive, `rows[${index}].receive`);
    const playoffGains = [
      requireFiniteNumber(row.your_playoff_delta, `rows[${index}].your_playoff_delta`),
      requireFiniteNumber(row.their_playoff_delta, `rows[${index}].their_playoff_delta`)
    ];
    const powerGains = [
      requireFiniteNumber(row.your_power_delta, `rows[${index}].your_power_delta`),
      requireFiniteNumber(row.their_power_delta, `rows[${index}].their_power_delta`)
    ];
    return metrics(playoffGains, powerGains, playoffGains[0], give.length + receive.length);
  }

  function threeTeamMetrics(row, index, primaryTeamId) {
    requireRecord(row, `rows[${index}]`);
    if (!Array.isArray(row.team_impacts) || row.team_impacts.length !== 3) {
      throw new TypeError(`rows[${index}].team_impacts must contain exactly three entries.`);
    }
    const teamIds = new Set();
    const playoffGains = [];
    const powerGains = [];
    let myPlayoffGain = null;
    for (const [impactIndex, impact] of row.team_impacts.entries()) {
      const name = `rows[${index}].team_impacts[${impactIndex}]`;
      requireRecord(impact, name);
      const teamId = requireText(impact.team_id, `${name}.team_id`);
      if (teamIds.has(teamId)) {
        throw new TypeError(`rows[${index}].team_impacts has a duplicate team.`);
      }
      teamIds.add(teamId);
      const playoffGain = requireFiniteNumber(impact.playoff_delta, `${name}.playoff_delta`);
      playoffGains.push(playoffGain);
      powerGains.push(requireFiniteNumber(impact.power_delta, `${name}.power_delta`));
      if (teamId === primaryTeamId) myPlayoffGain = playoffGain;
    }
    if (myPlayoffGain === null) {
      throw new TypeError(`rows[${index}].team_impacts does not include the primary team.`);
    }
    if (!Array.isArray(row.transfers) || row.transfers.length === 0) {
      throw new TypeError(`rows[${index}].transfers must be a non-empty array.`);
    }
    const movedPlayerIds = new Set();
    let movedPlayers = 0;
    for (const [transferIndex, transfer] of row.transfers.entries()) {
      const name = `rows[${index}].transfers[${transferIndex}]`;
      requireRecord(transfer, name);
      const fromTeamId = requireText(transfer.from_team_id, `${name}.from_team_id`);
      const toTeamId = requireText(transfer.to_team_id, `${name}.to_team_id`);
      if (fromTeamId === toTeamId || !teamIds.has(fromTeamId) || !teamIds.has(toTeamId)) {
        throw new TypeError(`${name} must connect two different participant teams.`);
      }
      if (!Array.isArray(transfer.players) || transfer.players.length === 0) {
        throw new TypeError(`${name}.players must be a non-empty array.`);
      }
      for (const [playerIndex, player] of transfer.players.entries()) {
        requireRecord(player, `${name}.players[${playerIndex}]`);
        const playerId = requireText(
          player.player_id,
          `${name}.players[${playerIndex}].player_id`
        );
        if (movedPlayerIds.has(playerId)) {
          throw new TypeError(`rows[${index}].transfers moves the same player more than once.`);
        }
        movedPlayerIds.add(playerId);
        movedPlayers += 1;
      }
    }
    return metrics(playoffGains, powerGains, myPlayoffGain, movedPlayers);
  }

  function metrics(playoffGains, powerGains, myPlayoffGain, movedPlayers) {
    return {
      playoffGains,
      combined_playoff_gain: playoffGains.reduce((total, value) => total + value, 0),
      my_playoff_gain: myPlayoffGain,
      weakest_participant_gain: Math.min(...playoffGains),
      combined_power_gain: powerGains.reduce((total, value) => total + value, 0),
      fewest_moved_players: movedPlayers
    };
  }

  function passesFilters(rowMetrics, controls) {
    if (
      controls.onlyAllParticipantsImprove
      && !rowMetrics.playoffGains.every(value => value > 0)
    ) return false;
    if (controls.minimumPlayoffGainPoints === null) return true;
    const minimumGain = controls.minimumPlayoffGainPoints / 100;
    return rowMetrics.playoffGains.every(
      value => value + COMPARISON_TOLERANCE >= minimumGain
    );
  }

  function filterAndSort(rows, context, controlValues = {}) {
    if (!Array.isArray(rows)) throw new TypeError("rows must be an array.");
    for (let index = 0; index < rows.length; index += 1) {
      if (!Object.hasOwn(rows, index)) {
        throw new TypeError("rows must not contain empty slots.");
      }
    }
    requireRecord(context, "context");
    const allowedContextKeys = new Set(["tradeFormat", "primaryTeamId"]);
    for (const key of Object.keys(context)) {
      if (!allowedContextKeys.has(key)) {
        throw new TypeError(`Unknown context value: ${key}.`);
      }
    }
    const tradeFormat = context.tradeFormat;
    if (tradeFormat !== TWO_TEAM && tradeFormat !== THREE_TEAM) {
      throw new TypeError(`tradeFormat must be ${TWO_TEAM} or ${THREE_TEAM}.`);
    }
    const primaryTeamId = tradeFormat === THREE_TEAM
      ? requireText(context.primaryTeamId, "primaryTeamId")
      : null;
    const controls = readControlValues(controlValues);
    const decorated = rows.map((row, index) => ({
      index,
      row,
      metrics: tradeFormat === THREE_TEAM
        ? threeTeamMetrics(row, index, primaryTeamId)
        : twoTeamMetrics(row, index)
    })).filter(item => passesFilters(item.metrics, controls));
    const direction = controls.sortBy === "fewest_moved_players" ? 1 : -1;
    decorated.sort((left, right) => {
      const difference = left.metrics[controls.sortBy] - right.metrics[controls.sortBy];
      return Math.abs(difference) <= COMPARISON_TOLERANCE
        ? left.index - right.index
        : direction * difference;
    });
    return decorated.map(item => item.row);
  }

  return Object.freeze({
    SORT_KEYS,
    filterAndSort,
    readControlValues
  });
})();
