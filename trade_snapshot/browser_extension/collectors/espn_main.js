(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  const readRoot = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl";

  const record = (value) => value && typeof value === "object" &&
    !Array.isArray(value) ? value : {};
  const rows = (value) => Array.isArray(value) ? value : [];
  const pick = (value, keys) => {
    const source = record(value);
    const result = {};
    for (const key of keys) {
      if (Object.hasOwn(source, key)) result[key] = source[key];
    }
    return result;
  };
  const numericMap = (value) => Object.fromEntries(
    Object.entries(record(value)).filter(([key, item]) =>
      /^-?\d+$/.test(key) && typeof item === "number" && Number.isFinite(item))
  );

  const projectPlayer = (value) => {
    const player = pick(value, [
      "id", "fullName", "defaultPositionId", "proTeamId", "eligibleSlots",
      "injuryStatus"
    ]);
    if (Array.isArray(player.eligibleSlots)) {
      player.eligibleSlots = [...player.eligibleSlots];
    }
    return player;
  };
  const projectRosterEntry = (value) => {
    const entry = pick(value, ["playerId", "lineupSlotId"]);
    const playerPoolEntry = record(record(value).playerPoolEntry);
    entry.playerPoolEntry = {player: projectPlayer(playerPoolEntry.player)};
    return entry;
  };
  const projectTeam = (value) => {
    const team = record(value);
    const result = pick(team, ["id", "name", "abbrev", "divisionId"]);
    result.record = {
      overall: pick(record(team.record).overall, [
        "wins", "losses", "ties", "pointsFor", "pointsAgainst"
      ])
    };
    result.roster = {
      entries: rows(record(team.roster).entries).map(projectRosterEntry)
    };
    return result;
  };
  const projectMatchup = (value) => {
    const matchup = pick(value, ["matchupPeriodId"]);
    const source = record(value);
    matchup.home = pick(source.home, ["teamId", "totalPoints"]);
    matchup.away = pick(source.away, ["teamId", "totalPoints"]);
    return matchup;
  };
  const projectScoringItem = (value) => {
    const item = pick(value, ["statId", "points", "isReverseItem"]);
    if (Object.hasOwn(record(value), "pointsOverrides")) {
      item.pointsOverrides = numericMap(record(value).pointsOverrides);
    }
    return item;
  };
  const projectTransaction = (value) => {
    const source = record(value);
    const result = pick(source, [
      "acceptedDate", "bidAmount", "executionType", "expirationDate", "id",
      "isActingAsTeamOwner", "isLeagueManager", "isPending",
      "processDate", "proposedDate", "rating", "relatedTransactionId",
      "scoringPeriodId", "skipTransactionCounters", "status", "subOrder",
      "teamId", "type"
    ]);
    result.items = rows(source.items).map((entry) => pick(entry, [
      "fromLineupSlotId", "fromTeamId", "isKeeper", "overallPickNumber",
      "playerId", "toLineupSlotId", "toTeamId", "type"
    ]));
    if (Object.hasOwn(source, "teamActions")) {
      result.teamActions = Object.fromEntries(
        Object.entries(record(source.teamActions)).filter(([, action]) =>
          typeof action === "string")
      );
    }
    return result;
  };
  const projectLeague = (value) => {
    const league = record(value);
    const settings = record(league.settings);
    const rosterSettings = record(settings.rosterSettings);
    const scheduleSettings = record(settings.scheduleSettings);
    const scoringSettings = record(settings.scoringSettings);
    return {
      ...pick(league, ["id", "seasonId", "scoringPeriodId"]),
      status: pick(league.status, ["currentMatchupPeriod", "finalScoringPeriod"]),
      settings: {
        rosterSettings: {
          lineupSlotCounts: numericMap(rosterSettings.lineupSlotCounts)
        },
        scheduleSettings: {
          ...pick(scheduleSettings, [
            "matchupPeriodCount", "playoffTeamCount", "playoffReseed",
            "playoffSeedingRule"
          ]),
          divisions: rows(scheduleSettings.divisions).map((division) =>
            pick(division, ["id", "name"]))
        },
        scoringSettings: {
          ...pick(scoringSettings, [
            "allowOutOfPositionScoring", "homeTeamBonus", "matchupTieRule",
            "matchupTieRuleBy", "playerRankType", "playoffHomeTeamBonus",
            "playoffMatchupTieRule", "playoffMatchupTieRuleBy", "scoringType"
          ]),
          scoringItems: rows(scoringSettings.scoringItems).map(projectScoringItem)
        }
      },
      teams: rows(league.teams).map(projectTeam),
      schedule: rows(league.schedule).map(projectMatchup),
      transactions: rows(league.transactions).map(projectTransaction)
    };
  };
  const projectProGame = (value) => pick(value, [
    "awayProTeamId", "date", "homeProTeamId", "id", "scoringPeriodId",
    "startTimeTBD", "statsOfficial", "validForLocking"
  ]);
  const projectProTeam = (value) => {
    const source = record(value);
    const result = pick(source, [
      "abbrev", "byeWeek", "id", "location", "name", "universeId"
    ]);
    result.proGamesByScoringPeriod = Object.fromEntries(
      Object.entries(record(source.proGamesByScoringPeriod))
        .filter(([key]) => /^-?\d+$/.test(key))
        .map(([key, games]) => [key, rows(games).map(projectProGame)])
    );
    if (Object.hasOwn(source, "teamPlayersByPosition")) {
      result.teamPlayersByPosition = {};
    }
    return result;
  };
  const projectProTeams = (value) => {
    const source = record(value);
    const settings = record(source.settings);
    return {
      ...pick(source, ["display"]),
      settings: {
        ...pick(settings, [
          "defaultDraftPosition", "draftLobbyMinimumLeagueCount",
          "gameNotificationSettings", "gated", "playerOwnershipSettings",
          "readOnly", "statIdToOverridePosition", "teamActivityEnabled", "typeNames"
        ]),
        proTeams: rows(settings.proTeams).map(projectProTeam)
      }
    };
  };

  const readJson = async (expectedUrl, maximumBytes, deadline, fantasyFilter = null) => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("espn_read_timeout");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), remaining);
    try {
      const headers = {"Accept": "application/json"};
      if (fantasyFilter !== null) headers["X-Fantasy-Filter"] = fantasyFilter;
      const response = await fetch(expectedUrl, {
        method: "GET",
        headers,
        credentials: "include",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal
      });
      if (response.url !== expectedUrl || response.status !== 200) {
        throw new Error(response.status === 401 || response.status === 403 ?
          "espn_not_authorized" : "espn_unexpected_response");
      }
      const mediaType = (response.headers.get("Content-Type") || "").split(";", 1)[0]
        .trim().toLowerCase();
      const declaredHeader = response.headers.get("Content-Length");
      const declared = declaredHeader === null ? null : Number(declaredHeader);
      if (mediaType !== "application/json" ||
          (declared !== null && (!Number.isFinite(declared) || declared < 0 ||
           declared > maximumBytes))) {
        throw new Error("espn_unsupported_response");
      }
      if (!response.body) throw new Error("espn_unsupported_response");
      const reader = response.body.getReader();
      const chunks = [];
      let length = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > maximumBytes) {
          await reader.cancel().catch(() => {});
          throw new Error("espn_response_too_large");
        }
        chunks.push(value);
      }
      if (!length) throw new Error("espn_response_shape");
      const body = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        body.set(chunk, offset);
        offset += chunk.byteLength;
      }
      const text = new TextDecoder("utf-8", {fatal: true}).decode(body);
      const value = JSON.parse(text);
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("espn_response_shape");
      }
      return value;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("espn_read_timeout");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  };

  handlers["espn.authenticated_json"] = async (options) => {
    if (location.protocol !== "https:" || location.hostname.toLowerCase() !==
        "fantasy.espn.com" || !/^\/football\/players\/projections\/?$/.test(location.pathname)) {
      throw new Error("espn_provenance");
    }
    const views = [
      "mTeam", "mRoster", "mSettings", "mMatchup", "mStandings", "mTransactions2"
    ];
    const transactionFilter = JSON.stringify({transactions: {limit: 1000}});
    const query = new URLSearchParams();
    for (const view of views) query.append("view", view);
    const leagueUrl = `${readRoot}/seasons/${options.season}/segments/0/leagues/` +
      `${options.league_id}?${query.toString()}`;
    const proTeamUrl = `${readRoot}/seasons/${options.season}?view=proTeamSchedules_wl`;
    const deadline = Date.now() + options.timeout_ms;
    const league = projectLeague(
      await readJson(
        leagueUrl, options.maximum_bytes, deadline, transactionFilter
      )
    );
    const proTeams = projectProTeams(
      await readJson(proTeamUrl, options.maximum_bytes, deadline)
    );
    return {league, pro_teams: proTeams};
  };
})();
