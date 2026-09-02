(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  const captureLeague = async (options) => {
  if (location.protocol !== 'https:' ||
      !['fantasypros.com', 'www.fantasypros.com'].includes(location.hostname) ||
      location.pathname !== '/nfl/myplaybook/trade-analyzer.php') {
    return {error: 'provenance'};
  }
  const pageData = typeof data === 'object' && data && !Array.isArray(data) ? data : null;
  const tap = window.__fteAnalyzerV1;
  if (!pageData || !tap || !Array.isArray(tap.initQueue)) return {error: 'bootstrap'};
  const timeoutMs = Number(options?.timeout_ms);
  const expectedSeason = Number(options?.expected_season);
  const expectedWeek = Number(options?.expected_week);
  if (!Number.isInteger(expectedSeason) || expectedSeason < 2000 || expectedSeason > 2200 ||
      !Number.isInteger(expectedWeek) || expectedWeek < 1 || expectedWeek > 25) {
    return {error: 'task_dimensions'};
  }
  const record = (value) => value && typeof value === 'object' && !Array.isArray(value);
  const own = (value, names) => {
    if (!record(value)) return undefined;
    for (const name of names) if (Object.hasOwn(value, name)) return value[name];
    return undefined;
  };
  const identifier = (value) => {
    const text = String(value ?? '').trim();
    return /^[1-9]\d{0,19}$/.test(text) ? text : null;
  };
  const integer = (value) => {
    const number = Number(value);
    return Number.isInteger(number) ? number : null;
  };
  const finite = (value) => {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value !== 'string') return null;
    const display = value.trim();
    if (!/^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/.test(display)) return null;
    const number = Number(display);
    return Number.isFinite(number) ? number : null;
  };
  const percentage = (value) => {
    if (typeof value === 'string') {
      const display = value.trim();
      if (['<1%', '>99%'].includes(display)) return display;
      if (!/^(?:\d+(?:\.\d+)?|\.\d+)%?$/.test(display)) return null;
      value = display.endsWith('%') ? display.slice(0, -1) : display;
    } else if (typeof value !== 'number') return null;
    const number = finite(value);
    return number !== null && number >= 0 && number <= 100 ? number : null;
  };
  const text = (value) => typeof value === 'string' && value.trim() ? value.trim() : null;
  const reserveSlot = (value) => {
    const normalized = text(value)?.toUpperCase().replace(/[^A-Z0-9]+/g, '');
    return new Set([
      'IR', 'INJUREDRESERVE', 'RES', 'RESERVE', 'ROOKIERESERVE',
      'RESERVEIR', 'INJUREDLIST', 'IL', 'TAXI', 'TAXISQUAD', 'NFI', 'PUP', 'NA'
    ]).has(normalized);
  };
  const teamsRaw = Array.isArray(pageData.teams) ? pageData.teams.slice(0, 100) : [];
  const leagueRaw = record(pageData.league) ? pageData.league : null;
  const settingsRaw = record(leagueRaw?.settings) ? leagueRaw.settings : {};
  const configuredRosterSize = () => {
    const slots = own(settingsRaw, ['roster_positions', 'rosterPositions']);
    if (slots === undefined) return undefined;
    if (!Array.isArray(slots) || !slots.length || slots.length > 100) return null;
    let total = 0;
    for (const slot of slots) {
      const slotName = text(own(slot, ['type', 'position']));
      const rawCount = own(slot, ['count']);
      const count = Number.isInteger(rawCount) ? rawCount : null;
      if (!slotName || count === null || count < 0 || count > 100) return null;
      if (reserveSlot(slotName)) continue;
      total += count;
      if (total > 100) return null;
    }
    return total || null;
  };
  const projectLeague = () => {
    const rosterSizes = teamsRaw.map((team) => {
      const size = playerIds(team).length;
      return size > 0 ? size : null;
    }).filter(Number.isInteger);
    const explicitRosterSize = own(leagueRaw, ['rosterSize', 'roster_size']) ??
      own(settingsRaw, ['rosterSize', 'roster_size']);
    const configuredSize = configuredRosterSize();
    if (configuredSize === null) return null;
    const rosterSize = integer(configuredSize ?? explicitRosterSize ??
      own(settingsRaw, ['totalRounds']) ??
      (rosterSizes.length && new Set(rosterSizes).size === 1 ? rosterSizes[0] : null));
    const scoring = text(own(leagueRaw, ['scoring']) ??
      own(settingsRaw, ['scoring', 'scoringType', 'scoring_type', 'basic_scoring']));
    const result = {
      season: integer(own(leagueRaw, ['season', 'year']) ?? own(pageData, ['season', 'year'])),
      team_count: teamsRaw.length,
      playoff_teams: integer(own(leagueRaw,
        ['playoffsTeams', 'playoffTeams', 'playoff_teams']) ?? own(settingsRaw,
        ['playoffsTeams', 'playoffTeams', 'playoff_teams'])),
      roster_size: rosterSize,
      scoring
    };
    const aliases = {
      id: ['id', 'leagueId', 'league_id'], name: ['name'],
      team_id: ['teamId', 'team_id'], team_name: ['teamName', 'team_name'],
      host: ['host'], sport: ['sport'], positions: ['positions'],
      playoffs_start_week: ['playoffsStartWeek'], playoffs_end_week: ['playoffsEndWeek'],
      playoff_reseeding: ['playoffReseeding'], basic_scoring: ['basic_scoring'],
      total_rounds: ['totalRounds'], has_rosters: ['hasRosters'], is_manual: ['isManual']
    };
    for (const [name, names] of Object.entries(aliases)) {
      const raw = own(leagueRaw, names) ?? own(settingsRaw, names);
      if (raw === undefined || raw === null || raw === '') continue;
      if (['id', 'team_id'].includes(name)) {
        const normalizedId = identifier(raw);
        if (!normalizedId) return null;
        result[name] = normalizedId;
      } else {
        result[name] = raw;
      }
    }
    for (const name of ['playoffs_start_week', 'playoffs_end_week', 'total_rounds']) {
      if (result[name] !== undefined) result[name] = integer(result[name]);
    }
    if (String(result.host || '').toUpperCase() === 'ESPN') {
      try {
        const hostUrl = new URL(own(leagueRaw, ['url']));
        const hostname = hostUrl.hostname.toLowerCase();
        const currentPath = hostname === 'fantasy.espn.com' &&
          /^\/football(?:\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+){0,3})?\/?$/.test(hostUrl.pathname);
        const wwwPath = ['espn.com', 'www.espn.com'].includes(hostname) &&
          /^\/fantasy\/football(?:\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+){0,3})?\/?$/.test(hostUrl.pathname);
        const legacy = hostname === 'fantasy.espn.com' && /^\/football\/?$/.test(hostUrl.pathname) &&
          /^#?\/?[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+){0,3}(?:\?|$)/.test(hostUrl.hash);
        const pairs = Array.from(hostUrl.searchParams.entries());
        if (legacy && hostUrl.hash.includes('?')) {
          pairs.push(...Array.from(new URLSearchParams(hostUrl.hash.split('?', 2)[1]).entries()));
        }
        const values = (name) => pairs.filter(([key]) => key.toLowerCase() === name)
          .map(([, value]) => value);
        const leagueIds = values('leagueid');
        const seasons = values('seasonid');
        if ((currentPath || wwwPath || legacy) && leagueIds.length === 1 &&
            /^[1-9]\d{0,19}$/.test(leagueIds[0]) && seasons.length <= 1 &&
            (!seasons.length || Number(seasons[0]) === expectedSeason)) {
          result.host_league_id = leagueIds[0];
        }
      } catch (_) { /* The separately configured ESPN League Home link can supply the ID. */ }
    }
    return result.season && result.team_count > 1 && result.playoff_teams &&
      result.roster_size && result.scoring ? result : null;
  };
  const projectTeams = () => teamsRaw.map((row) => {
    if (!record(row)) return null;
    const team = {
      team_id: identifier(own(row, ['teamId', 'team_id', 'id'])),
      team_name: text(own(row, ['teamName', 'team_name', 'name']))
    };
    if (Array.isArray(row.needs)) team.needs = row.needs;
    return team.team_id && team.team_name ? team : null;
  }).filter(Boolean);
  const playerIds = (team) => {
    if (!record(team)) return [];
    const buckets = ['players', 'roster', 'rosterPlayers', 'reservePlayers', 'irPlayers']
      .filter((name) => Object.hasOwn(team, name))
      .map((name) => team[name]);
    const ids = [];
    for (const bucket of buckets) {
      const entries = Array.isArray(bucket)
        ? bucket.slice(0, 200).map((item) => [null, item])
        : record(bucket) ? Object.entries(bucket).slice(0, 200) : [];
      for (const [mapId, item] of entries) {
        const nested = record(item) ? own(item, ['player']) : null;
        const raw = record(item)
          ? own(item, ['playerId', 'player_id', 'fpId', 'id']) ??
            (record(nested)
              ? own(nested, ['playerId', 'player_id', 'fpId', 'id'])
              : nested)
          : item;
        const playerId = identifier(raw ?? mapId);
        if (playerId) ids.push(playerId);
      }
    }
    return [...new Set(ids)];
  };
  const projectRosters = () => teamsRaw.map((team) => {
    const team_id = identifier(own(team, ['teamId', 'team_id', 'id']));
    const player_ids = playerIds(team);
    return team_id && player_ids.length && new Set(player_ids).size === player_ids.length
      ? {team_id, player_ids} : null;
  }).filter(Boolean);
  const projectPlayers = () => {
    const info = record(pageData.playerInfo) ? pageData.playerInfo : {};
    const players = [];
    for (const [mapId, row] of Object.entries(info).slice(0, 10000)) {
      if (!record(row)) continue;
      const player = {
        player_id: identifier(own(row, ['playerId', 'player_id', 'fpId', 'id']) ?? mapId),
        name: text(own(row, ['player_name', 'name']))
      };
      if (!player.player_id || !player.name) continue;
      const aliases = {
        team_id: ['team_id', 'teamId'], position_id: ['position_id'],
        position: ['position'], positions: ['positions'], eligibility: ['eligibility'],
        eligibility_espn: ['eligibility_espn'], eligibility_yahoo: ['eligibility_yahoo'],
        espn_id: ['espn_id', 'espnId'], yahoo_id: ['yahoo_id', 'yahooId']
      };
      for (const [name, names] of Object.entries(aliases)) {
        const raw = own(row, names);
        if (raw !== undefined && raw !== null && raw !== '') player[name] = raw;
      }
      players.push(player);
    }
    return players;
  };
  const projectInit = (value) => {
    if (!record(value) || Object.hasOwn(value, 'error') || !Array.isArray(value.standings)) {
      return null;
    }
    const standings = value.standings.slice(0, 100).map((row) => {
      if (!record(row)) return null;
      const result = {teamId: identifier(row.teamId)};
      for (const name of ['wins', 'losses', 'ties']) result[name] = finite(row[name]);
      return result.teamId && ['wins', 'losses', 'ties'].every((name) => result[name] >= 0)
        ? result : null;
    }).filter(Boolean);
    const bestRaw = value.best_free_agents;
    if (!Array.isArray(bestRaw) || !bestRaw.length || bestRaw.length > 1000) return null;
    const best_free_agent_ids = bestRaw.map((row) => identifier(own(row, ['id'])));
    return standings.length === value.standings.length &&
      best_free_agent_ids.every(Boolean) &&
      new Set(best_free_agent_ids).size === best_free_agent_ids.length
      ? {standings, best_free_agent_ids} : null;
  };
  const projectProjected = (value) => {
    if (!record(value) || Object.hasOwn(value, 'error') || !Array.isArray(value.standings)) {
      return null;
    }
    const fields = ['teamId', 'teamName', 'rank_proj', 'rank_current', 'wins_current',
      'losses_current', 'wins_proj', 'losses_proj', 'playoffs_odds', 'championship_odds'];
    const standings = value.standings.slice(0, 100).map((row) => {
      if (!record(row) || !fields.every((name) => Object.hasOwn(row, name))) return null;
      const result = {teamId: identifier(row.teamId), teamName: text(row.teamName)};
      for (const name of fields.slice(2)) {
        result[name] = ['playoffs_odds', 'championship_odds'].includes(name)
          ? percentage(row[name]) : finite(row[name]);
      }
      return result.teamId && result.teamName && fields.slice(2)
        .every((name) => result[name] !== null) ? result : null;
    }).filter(Boolean);
    const playoffsTeam = integer(value.playoffsTeam);
    return standings.length === value.standings.length && playoffsTeam
      ? {playoffsTeam, standings} : null;
  };
  const boundedMs = Math.max(1000, Math.min(timeoutMs || 10000, 30000));
  const deadline = Date.now() + boundedMs;
  const takeInit = async () => {
    while (Date.now() < deadline) {
      if (tap.error) return null;
      while (tap.initQueue.length) {
        const projected = projectInit(tap.initQueue.shift());
        if (projected) return projected;
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return null;
  };
  const takeProjected = () => new Promise((resolve) => {
    if (typeof window.MPB?.getProjectedStandings !== 'function') return resolve(null);
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(projectProjected(value));
    };
    const timer = setTimeout(() => finish(null), boundedMs);
    const key = own(leagueRaw, ['key']);
    if (typeof key !== 'string' || !key) return finish(null);
    try {
      window.MPB.getProjectedStandings({sport: 'NFL', key}, finish, () => finish(null));
    } catch (_) { finish(null); }
  });
  const bootstrap = {
    current_week: expectedWeek,
    league: projectLeague(), players: projectPlayers(), teams: projectTeams(),
    rosters: projectRosters()
  };
  if (!bootstrap.league || bootstrap.league.season !== expectedSeason ||
      !bootstrap.players.length ||
      bootstrap.teams.length !== teamsRaw.length || bootstrap.rosters.length !== teamsRaw.length) {
    return {error: 'bootstrap_incomplete'};
  }
  const [initial, projected] = await Promise.all([takeInit(), takeProjected()]);
  if (!initial) return {error: 'analyzer_init_incomplete'};
  if (!projected) return {error: 'projected_standings_incomplete'};
  return {
    team_count: bootstrap.teams.length,
    sources: [
      {source: 'bootstrap', body: {payload: bootstrap}},
      {source: 'analyzer_init', body: {payload: initial}},
      {source: 'projected_standings', body: {payload: projected}}
    ]
  };
};

  handlers["league.capture"] = (options) => captureLeague(options);
})();
