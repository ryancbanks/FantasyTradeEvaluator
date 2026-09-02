"""Finite visible projection/stat table extraction and traversal actions."""


PROJECTION_PAGE_SCRIPT = r"""
(request) => {
  const provider = request && request.provider;
  const profiles = {
    fantasypros: {
      hosts: ['fantasypros.com', 'www.fantasypros.com'],
      tables: '#projections-app table, table#projections, table.player-table, main table'
    },
    espn: {
      hosts: ['espn.com', 'www.espn.com'],
      tables: '.Table__Scroller table.Table, table.Table'
    },
    yahoo: {
      hosts: ['sports.yahoo.com'],
      tables: '#players-table table.Table, #players table.Table, table.Table'
    }
  };
  const pageHosts = {
    fantasypros: ['fantasypros.com', 'www.fantasypros.com'],
    espn: ['fantasy.espn.com'],
    yahoo: ['football.fantasysports.yahoo.com']
  };
  const profile = profiles[provider];
  if (!profile) return null;
  if (!pageHosts[provider].includes(location.hostname.toLowerCase())) return null;
  const visible = (node) => {
    if (!node || !node.isConnected) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
          style.contentVisibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const normalized = (value) => clean(value).toUpperCase().replace(/[^A-Z0-9]+/g, ' ').trim();
  const privateColumn = (header) => /\b(?:OWN(?:ED|ER|ERSHIP)?|ROST(?:ER(?:ED)?)?|START(?:ED|ER)?|AVAIL(?:ABILITY)?|ACTION|ADD|DROP|WAIVER|TRANSACTION|FANTASY TEAM|LEAGUE|SALARY|DRAFTED|WATCH)\b/.test(header);
  const playerColumn = (header) => /^(?:PLAYER|PLAYERS|PLAYER NAME|ATHLETE|NAME)$/.test(header) ||
    (provider === 'yahoo' && /^(?:OFFENSE|KICKERS|DEFENSE SPECIAL TEAMS)$/.test(header));
  const identityColumn = (header) => /^(?:TEAM|TM|POS|POSITION|OPP|OPPONENT|STATUS|BYE)$/.test(header);
  const statColumn = (header) => /^(?:PROJ|PROJECTED|FPTS|FAN PTS|FANTASY POINTS|PTS|POINTS|CMP|ATT|YDS|YD|TD|TDS|INT|INTS|REC|TGT|TAR|FUM|FL|FGM|FGA|XPM|XPA|SACK|SACKS|PA|YA|RET|LONG|AVG)$/.test(header) ||
    /^(?:PASS|RUSH|REC|RECEIVING|KICK|DEF|DST) (?:CMP|ATT|YDS|YD|TD|TDS|INT|REC|TGT|TAR|FUM|PTS|POINTS|SACK|SAFE|FUM REC|BLK KICK)$/.test(header) ||
    /^(?:RET TD|MISC 2PT|FUM LOST|XPM|FGM (?:0 19|20 29|30 39|40 49|50))$/.test(header);
  const publicLink = (anchor, position = null) => {
    if (!visible(anchor)) return null;
    try {
      const url = new URL(anchor.href);
      if (url.protocol !== 'https:' || url.username || url.password ||
          !profile.hosts.includes(url.hostname.toLowerCase())) return null;
      const path = provider === 'espn'
        ? /^\/nfl\/player\/_\/id\/\d+(?:\/[^/]*)?\/?$/.test(url.pathname)
        : provider === 'yahoo'
        ? (/^\/nfl\/players\/\d+\/?$/.test(url.pathname) ||
          (position === 'DST' &&
           /^\/nfl\/teams\/[a-z0-9]+(?:-[a-z0-9]+)*\/?$/i.test(url.pathname)))
        : /^\/nfl\/players\/[a-z0-9-]+\.php$/i.test(url.pathname);
      if (!path) return null;
      url.search = ''; url.hash = '';
      return url.href;
    } catch (_) { return null; }
  };
  const yahooPosition = (value) => ({
    'DEF': 'DST', 'D/ST': 'DST', 'PK': 'K', 'FB': 'RB',
    'DE': 'DL', 'DT': 'DL', 'NT': 'DL', 'EDGE': 'DL',
    'ILB': 'LB', 'OLB': 'LB', 'MLB': 'LB',
    'CB': 'DB', 'S': 'DB', 'FS': 'DB', 'SS': 'DB'
  })[value] || value;
  const yahooSource = () => {
    const statuses = Array.from(document.querySelectorAll('select#statusselect[name="status"]'));
    const stats = Array.from(document.querySelectorAll('select#statselect[name="stat1"]'));
    const checked = Array.from(document.querySelectorAll('input[name="pos"][type="radio"]'))
      .filter((control) => control.checked);
    if (statuses.length !== 1 || stats.length !== 1 || checked.length !== 1 ||
        statuses[0].value !== 'ALL') {
      return {season: null, week: null, horizon: null, scoring: null,
        positions: [], period_text: ''};
    }
    const projectedSeasons = Array.from(stats[0].options).map((option) =>
      option.value.match(/^S_PS_(20\d{2})$/)).filter(Boolean).map((match) => match[1]);
    const uniqueSeasons = [...new Set(projectedSeasons)];
    const weekly = stats[0].value.match(/^S_PW_([1-9]|1\d|2[0-5])$/);
    const ros = stats[0].value.match(/^S_PSR_(20\d{2})$/);
    const season = ros ? ros[1] : uniqueSeasons.length === 1 ? uniqueSeasons[0] : null;
    const position = yahooPosition(checked[0].value);
    const exactRequest = request.positions.length === 1 &&
      request.positions[0] === position && String(request.season) === season &&
      ((request.horizon === 'weekly' && weekly && Number(weekly[1]) === request.week) ||
       (request.horizon === 'ros' && ros));
    const period = ros ? 'Rest of Season' : weekly ? `Week ${weekly[1]}` : null;
    const positions = exactRequest ? [position] : [];
    return {
      season: season ? Number(season) : null,
      week: weekly ? Number(weekly[1]) : null,
      horizon: ros ? 'ros' : weekly ? 'weekly' : null,
      scoring: exactRequest ? request.scoring : null,
      positions,
      period_text: [season, period, exactRequest ? request.scoring : null,
        positions.length ? position : null, exactRequest ? 'Yahoo All Players' : null]
        .filter(Boolean).join(' | ')
    };
  };
  let genericDimensions = null;
  let source;
  if (provider === 'yahoo') {
    source = yahooSource();
  } else {
    const evidence = Array.from(document.querySelectorAll(
      'h1, h2, [aria-current="true"], [aria-selected="true"]'
    )).filter(visible).map((node) => clean(node.innerText));
    for (const control of Array.from(document.querySelectorAll('select')).filter(visible)) {
      for (const option of control.selectedOptions) evidence.push(clean(option.textContent));
    }
    const evidenceText = evidence.filter(Boolean).slice(0, 80).join(' | ');
    const years = [...new Set((evidenceText.match(/\b20\d{2}\b/g) || []))];
    const weekMatch = evidenceText.match(/\bWEEK\s*([1-9]|1\d|2[0-5])\b/i);
    const isRos = /\bREST OF (?:THE )?SEASON\b|\bROS\b/i.test(evidenceText);
    const scoring = /\bHALF(?:\s+POINT)?\s*PPR\b/i.test(evidenceText) ? 'HALF' :
      /\bPPR\b/i.test(evidenceText) ? 'PPR' :
      /\bSTANDARD\b|\bSTD\b/i.test(evidenceText) ? 'STD' : null;
    const positions = [...new Set((evidenceText.toUpperCase().match(
      /\b(?:ALL|QB|RB|WR|TE|K|DST|DL|LB|DB|IDP|FLEX|FLX)\b/g
    ) || []).map((item) => item === 'FLEX' ? 'FLX' : item))].sort();
    genericDimensions = {years, weekMatch, isRos, scoring};
    source = {
      season: years.length === 1 ? Number(years[0]) : null,
      week: weekMatch ? Number(weekMatch[1]) : null,
      horizon: isRos ? 'ros' : weekMatch ? 'weekly' : null,
      scoring,
      positions,
      period_text: [years.length === 1 ? years[0] : null,
        isRos ? 'Rest of Season' : weekMatch ? `Week ${weekMatch[1]}` : null,
        scoring, positions.length ? positions.join('/') : null].filter(Boolean).join(' | ')
    };
  }
  const yahooHeaders = (candidates, headerRow, rawHeaders) => {
    if (provider !== 'yahoo' || candidates.length < 2) return rawHeaders;
    const groupRow = candidates[candidates.length - 2];
    const groups = [];
    for (const cell of Array.from(groupRow.cells)) {
      for (let index = 0; index < Math.max(1, cell.colSpan || 1); index += 1) {
        groups.push(normalized(cell.innerText));
      }
    }
    if (groups.length !== rawHeaders.length) return rawHeaders;
    const defense = rawHeaders.some((header) => header === 'DEFENSE SPECIAL TEAMS');
    return rawHeaders.map((header, index) => {
      if (playerColumn(header)) return 'PLAYER';
      const group = groups[index];
      const prefixes = {PASSING: 'PASS', RUSHING: 'RUSH', RECEIVING: 'REC'};
      if (prefixes[group] && header) return `${prefixes[group]} ${header}`;
      if (group === 'RET' && header) return `RET ${header}`;
      if (group === 'MISC' && header) return defense ? `DEF ${header}` : `MISC ${header}`;
      if (group === 'FUM' && header) return `FUM ${header}`;
      if (group === 'FIELD GOALS MADE' && header) return `FGM ${header}`;
      if (group === 'PAT' && header === 'MADE') return 'XPM';
      if (defense && ['TACKLES', 'TURNOVERS', 'TD'].includes(group) && header) {
        return `DEF ${header}`;
      }
      return header;
    });
  };
  const yahooPlayer = (cell) => {
    const anchors = Array.from(cell.querySelectorAll('a.name[data-ys-playerid][href]'));
    if (anchors.length !== 1) return null;
    const detail = Array.from(cell.querySelectorAll('span')).map((node) => clean(node.innerText))
      .map((text) => text.match(/^([A-Za-z]{2,3}|FA)\s*-\s*(D\s*\/\s*ST|[A-Za-z]{1,4})$/i))
      .find(Boolean);
    if (!detail) return null;
    const position = yahooPosition(detail[2].toUpperCase().replace(/\s+/g, ''));
    const link = publicLink(anchors[0], position);
    if (!link) return null;
    return {text: `${clean(anchors[0].innerText)} ${detail[1]} - ${position}`.slice(0, 1000),
      links: [link]};
  };
  const tables = Array.from(document.querySelectorAll(profile.tables)).filter(visible)
    .slice(0, 8).map((table) => {
      const candidates = Array.from(table.querySelectorAll('thead tr')).filter(visible);
      const headerRow = candidates.length ? candidates[candidates.length - 1] :
        Array.from(table.rows).find(visible);
      if (!headerRow) return null;
      const rawHeaders = Array.from(headerRow.cells).map((cell) => normalized(cell.innerText));
      const headers = yahooHeaders(candidates, headerRow, rawHeaders);
      const allowed = headers.map((header, index) => (
        !privateColumn(header) && (playerColumn(header) || identityColumn(header) || statColumn(header))
      ) ? index : -1).filter((index) => index >= 0);
      const playerIndex = rawHeaders.findIndex(playerColumn);
      if (playerIndex < 0 || !allowed.includes(playerIndex) ||
          !allowed.some((index) => statColumn(headers[index]))) return null;
      const result = [[...allowed.map((index) => ({text: headers[index], links: []}))]];
      for (const row of Array.from(table.rows).filter(visible).slice(0, 5000)) {
        if (row === headerRow || row.closest('table') !== table) continue;
        const cells = Array.from(row.cells);
        if (cells.length <= Math.max(...allowed)) continue;
        const yahooIdentity = provider === 'yahoo' ? yahooPlayer(cells[playerIndex]) : null;
        const playerLinks = provider === 'yahoo' ?
          (yahooIdentity ? yahooIdentity.links : []) :
          Array.from(cells[playerIndex].querySelectorAll('a[href]'))
            .map((anchor) => publicLink(anchor)).filter(Boolean);
        if (playerLinks.length !== 1) continue;
        const selected = allowed.map((index) => ({
          text: provider === 'yahoo' && index === playerIndex ? yahooIdentity.text :
            clean(cells[index].innerText).slice(0, 1000),
          links: index === playerIndex ? playerLinks : []
        }));
        if (selected.some((cell, index) => statColumn(headers[allowed[index]]) &&
            /[+-]?\d+(?:\.\d+)?/.test(cell.text))) result.push(selected);
      }
      return result.length > 1 ? {rows: result} : null;
    }).filter(Boolean);
  if (provider !== 'yahoo' && !source.positions.length) {
    const found = new Set();
    for (const table of tables) {
      const positionIndex = table.rows[0].findIndex((cell) => /^(?:POS|POSITION)$/.test(cell.text));
      if (positionIndex < 0) continue;
      for (const row of table.rows.slice(1)) {
        const match = row[positionIndex].text.toUpperCase().match(/\b(?:QB|RB|WR|TE|K|DST|DL|LB|DB|IDP)\b/);
        if (match) found.add(match[0]);
      }
    }
    source.positions = [...found].sort();
    const positionText = source.positions.length ? source.positions.join('/') : null;
    source.period_text = [genericDimensions.years.length === 1 ? genericDimensions.years[0] : null,
      genericDimensions.isRos ? 'Rest of Season' : genericDimensions.weekMatch ?
        `Week ${genericDimensions.weekMatch[1]}` : null,
      genericDimensions.scoring, positionText].filter(Boolean).join(' | ');
  }
  return {source, tables};
}
"""


ADVANCE_PROJECTION_SCRIPT = r"""
(provider) => {
  const tableSelectors = {
    fantasypros: '#projections-app table, table#projections, table.player-table, main table',
    espn: '.Table__Scroller table.Table, table.Table',
    yahoo: 'table#players-table, #players table, table.Table, main table'
  };
  const selector = tableSelectors[provider];
  if (!selector) return {action: 'error'};
  const visible = (node) => {
    if (!node || !node.isConnected) return false;
    const style = getComputedStyle(node), box = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      box.width > 0 && box.height > 0;
  };
  const tables = Array.from(document.querySelectorAll(selector)).filter(visible);
  for (const table of tables) {
    for (let node = table.parentElement; node && node !== document.body; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight + 2 &&
          node.scrollTop + node.clientHeight < node.scrollHeight - 2) {
        node.scrollTop = Math.min(node.scrollHeight, node.scrollTop + node.clientHeight);
        return {action: 'scroll'};
      }
    }
  }
  const root = tables[0] ? (tables[0].closest('main') || document.body) : document.body;
  const candidates = Array.from(root.querySelectorAll(
    'a[rel="next"], button[aria-label*="next" i], a[aria-label*="next" i],' +
    '.Pagination__Button--next, .pagination-next, [data-testid*="pagination-next" i],' +
    '[id^="playerspagenav"] li.last a[href]'
  )).filter(visible);
  const enabled = candidates.filter((node) => !node.disabled &&
    node.getAttribute('aria-disabled') !== 'true' && !node.classList.contains('disabled'));
  const destinations = enabled.map((node) => {
    const link = node.closest('a[href]');
    if (!link) return null;
    try {
      const url = new URL(link.href);
      if (url.protocol !== 'https:' || url.origin !== location.origin) return false;
      if (provider === 'yahoo') {
        const current = location.pathname.match(
          /^(\/(?:20\d{2}\/)?f1\/[1-9]\d{0,19})\/(?:players|playersearch)\/?$/
        );
        const target = url.pathname.match(
          /^(\/(?:20\d{2}\/)?f1\/[1-9]\d{0,19})\/(?:players|playersearch)\/?$/
        );
        const currentCount = Number(new URL(location.href).searchParams.get('count') || '0');
        const targetCount = Number(url.searchParams.get('count') || '0');
        if (!current || !target || current[1] !== target[1] ||
            !Number.isInteger(currentCount) || !Number.isInteger(targetCount) ||
            targetCount <= currentCount || url.searchParams.get('status') !== 'ALL') return false;
        for (const key of ['stat1', 'pos']) {
          const before = new URL(location.href).searchParams.get(key);
          if (!before || url.searchParams.get(key) !== before) return false;
        }
      }
      url.hash = '';
      return url.href;
    } catch (_) { return false; }
  });
  if (destinations.some((value) => value === false)) return {action: 'error'};
  if (enabled.length > 1 &&
      (destinations.some((value) => value === null) || new Set(destinations).size !== 1)) {
    return {action: 'error'};
  }
  if (enabled.length >= 1) {
    if (destinations[0] === null && enabled.length !== 1) {
      return {action: 'error'};
    }
    enabled[0].click();
    return {action: 'next'};
  }
  return {action: 'done'};
}
"""


__all__ = ("ADVANCE_PROJECTION_SCRIPT", "PROJECTION_PAGE_SCRIPT")
