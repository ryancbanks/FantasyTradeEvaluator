(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  const readRoot = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl";
  const playerLimit = 5000;
  const maximumBytes = 16 * 1024 * 1024;
  const scoringFormats = Object.freeze({STD: 1, PPR: 3, HALF: 8});
  const scoringLabels = Object.freeze({STD: "Standard", PPR: "PPR", HALF: "Half PPR"});
  const positions = Object.freeze({1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"});
  const proTeams = Object.freeze({
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE",
    6: "DAL", 7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND",
    12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE",
    18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT",
    24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU"
  });
  const rawStats = Object.freeze([
    ["CMP", "1"], ["PASS ATT", "0"], ["PASS YDS", "3"], ["PASS TD", "4"],
    ["INT", "15"], ["RUSH ATT", "23"], ["RUSH YDS", "24"],
    ["RUSH TD", "25"], ["REC", "53"], ["TGT", "58"],
    ["REC YDS", "42"], ["REC TD", "43"], ["FUM", "72"], ["FUM LOST", "73"]
  ]);
  const tableHeaders = Object.freeze([
    "PLAYER", "TEAM", "POS", "GP", "FPTS", "FPPG", ...rawStats.map(([name]) => name)
  ]);

  const record = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : null;
  const finite = (value, label) => {
    if (typeof value !== "number" || !Number.isFinite(value) || Math.abs(value) > 1000000000) {
      throw new Error(label);
    }
    return value;
  };
  const optionalNumber = (value, label) => value === undefined || value === null ?
    null : finite(value, label);
  const formatted = (value) => {
    if (value === null) return "-";
    const result = value.toFixed(12).replace(/(?:\.0+|(?<=[0-9])0+)$/, "").replace(/\.$/, "");
    return result === "-0" || result === "" ? "0" : result;
  };
  const cell = (text, link = null) => ({text: String(text), links: link === null ? [] : [link]});
  const playerId = (value) => {
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value) || value === 0) throw new Error("espn_projection_player_id");
      value = String(value);
    }
    if (typeof value !== "string" || !/^-?[1-9][0-9]{0,15}$/.test(value)) {
      throw new Error("espn_projection_player_id");
    }
    return value;
  };
  const playerName = (value) => {
    if (typeof value !== "string" || /[\x00-\x1f\x7f]/.test(value) || value.includes("://")) {
      throw new Error("espn_projection_player_name");
    }
    const result = value.replace(/\s+/g, " ").trim();
    if (!result.length || result.length > 160) throw new Error("espn_projection_player_name");
    return result;
  };
  const projectionUrl = (season, scoring) =>
    `${readRoot}/seasons/${season}/segments/0/leaguedefaults/` +
    `${scoringFormats[scoring]}?view=kona_player_info`;
  const projectionFilter = (season) => JSON.stringify({
    players: {
      filterStatsForExternalIds: {value: [season]},
      filterSlotIds: {value: [
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 23, 24
      ]},
      filterStatsForSourceIds: {value: [1]},
      useFullProjectionTable: {value: true},
      sortAppliedStatTotal: {sortAsc: false, sortPriority: 3, value: `10${season}`},
      sortDraftRanks: {sortPriority: 2, sortAsc: true, value: "PPR"},
      sortPercOwned: {sortPriority: 4, sortAsc: false},
      limit: playerLimit,
      filterRanksForSlotIds: {value: [0, 2, 4, 6, 17, 16, 8, 9, 10, 12, 13, 24, 11, 14, 15]}
    }
  });

  async function readJson(expectedUrl, fantasyFilter, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(expectedUrl, {
        method: "GET",
        headers: {
          "Accept": "application/json",
          "X-Fantasy-Filter": fantasyFilter,
          "X-Fantasy-Platform": "espn-fantasy-web",
          "X-Fantasy-Source": "kona"
        },
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal
      });
      const mediaType = (response.headers.get("Content-Type") || "").split(";", 1)[0]
        .trim().toLowerCase();
      const declaredHeader = response.headers.get("Content-Length");
      const declared = declaredHeader === null ? null : Number(declaredHeader);
      if (response.url !== expectedUrl || response.status !== 200 || mediaType !== "application/json" ||
          response.headers.get("Content-Encoding") ||
          (declared !== null && (!Number.isInteger(declared) || declared < 0 || declared > maximumBytes)) ||
          !response.body) throw new Error("espn_projection_response");
      const reader = response.body.getReader();
      const chunks = [];
      let length = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > maximumBytes) {
          await reader.cancel().catch(() => {});
          throw new Error("espn_projection_size");
        }
        chunks.push(value);
      }
      if (!length) throw new Error("espn_projection_response");
      const body = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        body.set(chunk, offset);
        offset += chunk.byteLength;
      }
      const result = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(body));
      if (!record(result)) throw new Error("espn_projection_shape");
      return result;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("espn_projection_timeout");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function projectionSegment(payload, request, formatId) {
    if (!record(payload) || Object.keys(payload).length !== 1 || !Array.isArray(payload.players) ||
        !payload.players.length || payload.players.length >= playerLimit) {
      throw new Error("espn_projection_incomplete");
    }
    const selected = request.positions.length === 1 && request.positions[0] === "ALL" ?
      new Set(Object.values(positions)) : new Set(request.positions);
    const seen = new Set();
    const projected = [];
    for (const wrapper of payload.players) {
      const player = record(record(wrapper)?.player);
      if (!player) throw new Error("espn_projection_player");
      const id = playerId(player.id);
      if (seen.has(id)) throw new Error("espn_projection_duplicate");
      seen.add(id);
      if (!Array.isArray(player.stats) || player.stats.length > 512 ||
          player.stats.some((row) => !record(row))) throw new Error("espn_projection_stats");
      const matches = player.stats.filter((row) => row.seasonId === request.season &&
        row.statSourceId === 1 && row.scoringPeriodId === 0);
      if (matches.length > 1) throw new Error("espn_projection_duplicate_stat");
      if (!matches.length) continue;
      const position = positions[player.defaultPositionId];
      const team = proTeams[player.proTeamId];
      if (!position || !team || !selected.has(position)) {
        if (!position || !team) throw new Error("espn_projection_metadata");
        continue;
      }
      const projection = matches[0];
      if (projection.id !== `10${request.season}` || projection.externalId !== String(request.season) ||
          projection.statSplitTypeId !== 0 || !record(projection.stats)) {
        throw new Error("espn_projection_provenance");
      }
      const total = finite(projection.appliedTotal, "espn_projection_total");
      const average = finite(projection.appliedAverage, "espn_projection_average");
      const games = optionalNumber(projection.stats["210"], "espn_projection_games");
      if (games === null || games === 0) {
        if (Math.abs(total) > 1e-9 || Math.abs(average) > 1e-9) {
          throw new Error("espn_projection_average_consistency");
        }
      } else if (games <= 0 || games > 25 ||
          Math.abs(total / games - average) > 1e-6 * Math.max(1, Math.abs(average))) {
        throw new Error("espn_projection_average_consistency");
      }
      const linkId = id.startsWith("-") ? id.slice(1) : id;
      const playerLink = `https://www.espn.com/nfl/player/_/id/${linkId}` +
        (position === "DST" ? "/team-defense" : "");
      const row = [
        cell(playerName(player.fullName), playerLink),
        cell(team), cell(position), cell(formatted(games)), cell(formatted(total)),
        cell(formatted(average))
      ];
      for (const [, statId] of rawStats) {
        row.push(cell(formatted(optionalNumber(projection.stats[statId], "espn_projection_raw_stat"))));
      }
      projected.push({total, numericId: Number(id), row});
    }
    if (!projected.length) throw new Error("espn_projection_empty");
    projected.sort((left, right) => right.total - left.total || left.numericId - right.numericId);
    const periodText = `ESPN ${request.season} full season; ${scoringLabels[request.scoring]} ` +
      `format ${formatId}; ${projected.length} of ${payload.players.length} returned players projected`;
    return {
      availability: "available",
      source: {
        season: request.season,
        week: null,
        horizon: "ros",
        scoring: request.scoring,
        positions: [...request.positions],
        period_text: periodText
      },
      tables: [{rows: [tableHeaders.map((header) => cell(header)), ...projected.map((row) => row.row)]}]
    };
  }

  handlers["espn.season_projections"] = async (request) => {
    if (location.protocol !== "https:" || location.hostname.toLowerCase() !== "fantasy.espn.com" ||
        !/^\/football\/players\/projections\/?$/.test(location.pathname) || !record(request) ||
        request.provider !== "espn" || request.horizon !== "ros" ||
        !Number.isInteger(request.season) || request.season < 2000 || request.season > 2200 ||
        !Number.isInteger(request.week) || request.week < 1 || request.week > 25 ||
        !Object.hasOwn(scoringFormats, request.scoring) || !Array.isArray(request.positions) ||
        !request.positions.length || new Set(request.positions).size !== request.positions.length ||
        (request.positions.includes("ALL") && (request.positions.length !== 1 || request.positions[0] !== "ALL")) ||
        request.positions.some((position) => position !== "ALL" && !Object.values(positions).includes(position))) {
      throw new Error("espn_projection_request");
    }
    const formatId = scoringFormats[request.scoring];
    const url = projectionUrl(request.season, request.scoring);
    const payload = await readJson(url, projectionFilter(request.season), request.timeout_ms || 30000);
    return projectionSegment(payload, request, formatId);
  };
})();
