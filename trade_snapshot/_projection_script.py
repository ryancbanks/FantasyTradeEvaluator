"""Finite visible projection/stat table extraction."""

from ._projection_advance_script import ADVANCE_PROJECTION_SCRIPT


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
    },
    cbs: {
      hosts: ['cbssports.com', 'www.cbssports.com'],
      tables: 'table.TableBase-table, main table'
    },
    fftoday: {
      hosts: ['fftoday.com', 'www.fftoday.com'],
      tables: 'table:has(tr.tableclmhdr)'
    },
    fantasysharks: {
      hosts: ['fantasysharks.com', 'www.fantasysharks.com'],
      tables: 'table#toolData'
    }
  };
  const pageHosts = {
    fantasypros: ['fantasypros.com', 'www.fantasypros.com'],
    espn: ['fantasy.espn.com'],
    yahoo: ['football.fantasysports.yahoo.com'],
    cbs: ['cbssports.com', 'www.cbssports.com'],
    fftoday: ['fftoday.com', 'www.fftoday.com'],
    fantasysharks: ['fantasysharks.com', 'www.fantasysharks.com']
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
  const providerHeader = (value) => {
    let header = normalized(value);
    if (provider === 'fantasysharks') {
      header = header.replace(/^RSH\b/, 'RUSH').replace(/^PASSING\b/, 'PASS')
        .replace(/\bYARDS?\b/g, 'YDS');
      return ({COMP: 'CMP', RUSH: 'RUSH ATT', SCK: 'SACK', SCKS: 'SACKS', 'SCK SCKS': 'SACKS',
        PASSDEF: 'PASS DEF', FUMFRC: 'FUM FRC', FUMREC: 'FUM REC',
        DEFTD: 'DEF TD', SAFTS: 'SAFE', BLKKICK: 'BLK KICK'})[header] || header;
    }
    if (provider === 'fftoday' && /^PLAYER SORT FIRST LAST$/.test(header)) {
      return 'PLAYER';
    }
    if (provider === 'fftoday') {
      return ({
        COMP: 'CMP', YARD: 'YDS', YARDS: 'YDS', FFPTS: 'FPTS',
        'FG MADE': 'FGM', 'FG MISS': 'FGM MISS',
        'XP MADE': 'XPM', 'XP MISS': 'XPM MISS', EPM: 'XPM', EPA: 'XPA',
        TACKLE: 'TACK', ASSIST: 'ASST', PD: 'PASS DEF', FF: 'FUM FRC',
        FR: 'FUM REC', DEFTD: 'DEF TD', S: 'SAFE', KICKTD: 'RET TD',
        'PAYD G': 'PASS YDS PER GAME', 'RUYD G': 'RUSH YDS PER GAME'
      })[header] || header;
    }
    return header;
  };
  const privateColumn = (header) => /\b(?:OWN(?:ED|ER|ERSHIP)?|ROST(?:ER(?:ED)?)?|START(?:ED|ER)?|AVAIL(?:ABILITY)?|ACTION|ADD|DROP|WAIVER|TRANSACTION|FANTASY TEAM|LEAGUE|SALARY|DRAFTED|WATCH)\b/.test(header);
  const playerColumn = (header) => /^(?:PLAYER|PLAYERS|PLAYER NAME|ATHLETE|NAME)$/.test(header) ||
    (provider === 'yahoo' && /^(?:OFFENSE|KICKERS|DEFENSE SPECIAL TEAMS)$/.test(header)) ||
    (provider === 'cbs' && request.positions.length === 1 &&
      request.positions[0] === 'DST' && header === 'TEAM');
  const identityColumn = (header) => /^(?:TEAM|TM|POS|POSITION|OPP|OPPONENT|STATUS|BYE)$/.test(header);
  const statColumn = (header) => /^(?:PROJ|PROJECTED|FPTS|FPPG|FAN PTS|FANTASY POINTS|PTS|POINTS|GP|CMP|ATT|YDS|YD|TD|TDS|INT|INTS|REC|TGT|TAR|FUM|FL|FGM|FGA|XPM|XPA|SACK|SACKS|TACK|ASST|SAFE|PA|YA|RET|LONG|AVG|RATE)$/.test(header) ||
    /^(?:PASS|RUSH|REC|RECEIVING|KICK|DEF|DST) (?:CMP|ATT|YDS|YD|TD|TDS|INT|REC|TGT|TAR|FUM|PTS|POINTS|SACK|SAFE|FUM REC|BLK KICK)$/.test(header) ||
    /^(?:RET TD|MISC 2PT|FUM LOST|PASS DEF|FUM FRC|FUM REC|DEF TD|RZ TGT|BLK KICK|KICK RET YDS|YDS ALLOWED|PTS AGN|SCORING OPPORTUNITIES|FGM MISS|XPM MISS|PASS YDS PER GAME|RUSH YDS PER GAME|XPM|FGM (?:0 19|10 19|20 29|30 39|40 49|50)|(?:0 19|10 19|20 29|30 39|40 49|50) FGM)$/.test(header);
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
        : provider === 'cbs'
        ? (/^\/nfl\/players\/\d+\/[a-z0-9-]+\/fantasy\/?$/i.test(url.pathname) ||
          (position === 'DST' &&
           /^\/nfl\/teams\/[a-z]{2,3}\/[a-z0-9]+(?:-[a-z0-9]+)*\/?$/i.test(url.pathname)))
        : provider === 'fftoday'
        ? /^\/stats\/players\/\d+\/[A-Za-z0-9_.'-]+\/?$/.test(url.pathname)
        : provider === 'fantasysharks'
        ? (url.pathname === '/apps/bert/players/playerpage.php' &&
          /^[1-9]\d{0,9}$/.test(url.searchParams.get('id') || '') &&
          [...url.searchParams.keys()].every((key) => key === 'id'))
        : /^\/nfl\/players\/[a-z0-9-]+\.php$/i.test(url.pathname);
      if (!path) return null;
      if (provider !== 'fantasysharks') url.search = '';
      url.hash = '';
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
    const fullSeason = stats[0].value.match(/^S_PS_(20\d{2})$/);
    const season = ros ? ros[1] : fullSeason ? fullSeason[1]
      : uniqueSeasons.length === 1 ? uniqueSeasons[0] : null;
    const position = yahooPosition(checked[0].value);
    const exactRequest = request.positions.length === 1 &&
      request.positions[0] === position && String(request.season) === season &&
      ((request.horizon === 'weekly' && weekly && Number(weekly[1]) === request.week) ||
       (request.horizon === 'ros' && (ros || (fullSeason && request.week === 1))));
    const period = ros ? 'Rest of Season' : fullSeason ? 'Full Season'
      : weekly ? `Week ${weekly[1]}` : null;
    const positions = exactRequest ? [position] : [];
    return {
      season: season ? Number(season) : null,
      week: weekly ? Number(weekly[1]) : null,
      horizon: ros || (fullSeason && request.week === 1) ? 'ros'
        : weekly ? 'weekly' : null,
      scoring: exactRequest ? request.scoring : null,
      positions,
      period_text: [season, period, exactRequest ? request.scoring : null,
        positions.length ? position : null, exactRequest ? 'Yahoo All Players' : null]
        .filter(Boolean).join(' | ')
    };
  };
  const cbsSource = () => {
    const match = location.pathname.match(
      /^\/fantasy\/football\/stats\/(QB|RB|WR|TE|K|DST)\/(20\d{2})\/season\/projections\/(ppr|nonppr)\/?$/i
    );
    const headingLabels = {QB: 'QUARTERBACK', RB: 'RUNNING BACK', WR: 'WIDE RECEIVER',
      TE: 'TIGHT END', K: 'KICKER', DST: 'DEFENSE SPECIAL TEAM'};
    const expectedHeading = request.positions.length === 1 && headingLabels[request.positions[0]]
      ? `${request.season} PROJECTIONS FANTASY FOOTBALL ${headingLabels[request.positions[0]]} STATS`
      : null;
    const headings = Array.from(document.querySelectorAll('h1')).filter(visible)
      .map((node) => normalized(node.innerText));
    const exact = match && expectedHeading && headings.length === 1 &&
      headings[0] === expectedHeading && request.horizon === 'ros' &&
      request.positions.length === 1 &&
      request.positions[0] === match[1].toUpperCase() &&
      request.season === Number(match[2]) &&
      ((request.scoring === 'STD' && match[3].toLowerCase() === 'nonppr') ||
       (request.scoring !== 'STD' && match[3].toLowerCase() === 'ppr'));
    if (!exact) {
      return {season: null, week: null, horizon: null, scoring: null,
        positions: [], period_text: ''};
    }
    const position = match[1].toUpperCase();
    const sourceScoring = match[3].toLowerCase() === 'ppr' ? 'PPR' : 'non-PPR';
    const conversion = request.scoring === 'HALF' ? ' | local Half-PPR conversion' : '';
    return {
      season: Number(match[2]), week: null, horizon: 'ros', scoring: request.scoring,
      positions: [position],
      period_text: `${match[2]} | CBS ${position} season projections | ${sourceScoring}${conversion}`
    };
  };
  const fftodaySource = () => {
    const weekly = location.pathname === '/rankings/playerwkproj.php';
    const seasonPage = location.pathname === '/rankings/playerproj.php';
    const positionById = {10: 'QB', 20: 'RB', 30: 'WR', 40: 'TE', 50: 'DL',
      60: 'LB', 70: 'DB', 80: 'K'};
    const scoringById = {1: 'STD', 193033: 'HALF', 107644: 'PPR'};
    const query = new URL(location.href).searchParams;
    const season = Number(query.get('Season'));
    const week = weekly ? Number(query.get('GameWeek')) : null;
    const position = positionById[Number(query.get('PosID'))] || null;
    const scoring = scoringById[Number(query.get('LeagueID'))] || null;
    const pageText = clean(document.title + ' ' +
      Array.from(document.querySelectorAll('h1, h2, .pagetitle, .bodycontent'))
        .filter(visible).slice(0, 20).map((node) => node.innerText).join(' '));
    const periodMatches = weekly
      ? new RegExp(`\\b${request.season}\\s+WEEK\\s+${request.week}\\b`, 'i').test(pageText)
      : new RegExp(`\\b${request.season}\\b`, 'i').test(pageText) &&
        /\bREGULAR SEASON\b/i.test(pageText);
    const supportedPositions = weekly
      ? ['QB', 'RB', 'WR', 'TE', 'K']
      : ['QB', 'RB', 'WR', 'TE', 'K', 'DL', 'LB', 'DB'];
    const exact = (weekly || seasonPage) && request.positions.length === 1 &&
      supportedPositions.includes(position) &&
      season === request.season && position === request.positions[0] &&
      scoring === request.scoring && periodMatches &&
      ((request.horizon === 'weekly' && weekly && week === request.week) ||
       (request.horizon === 'ros' && seasonPage));
    return {
      season: exact ? season : null,
      week: exact && weekly ? week : null,
      horizon: exact ? (weekly ? 'weekly' : 'ros') : null,
      scoring: exact ? scoring : null,
      positions: exact ? [position] : [],
      period_text: exact
        ? `${season} | FFToday ${position} ${weekly ? `Week ${week}` : 'full-season'} projections | ${scoring}`
        : ''
    };
  };
  const fantasySharksSource = () => {
    const one = (name) => {
      const matches = Array.from(document.querySelectorAll(`select[name="${name}"]`));
      return matches.length === 1 ? matches[0] : null;
    };
    const segment = one('Segment'), positionControl = one('Position');
    const scoringControl = one('scoring');
    if (!segment || !positionControl || !scoringControl) {
      return {season: null, week: null, horizon: null, scoring: null,
        positions: [], period_text: ''};
    }
    const positionById = {1: 'QB', 2: 'RB', 4: 'WR', 5: 'TE', 6: 'DST', 7: 'K',
      8: 'DL', 9: 'LB', 10: 'DB'};
    const scoringById = {1: 'STD', 18: 'HALF', 2: 'PPR'};
    let optionYear = null, selectedYear = null;
    for (const option of Array.from(segment.options)) {
      const label = normalized(option.textContent);
      const marker = label.match(/^(20\d{2}) NFL SEASON$/);
      if (marker) optionYear = Number(marker[1]);
      if (option.selected) {
        const explicit = label.match(/^(20\d{2}) /);
        selectedYear = explicit ? Number(explicit[1]) : optionYear;
      }
    }
    const label = normalized(segment.selectedOptions[0] && segment.selectedOptions[0].textContent);
    const weekMatch = label.match(/^WEEK ([1-9]|1\d|2[0-5])$/);
    const ros = label === `${request.season} REST OF YEAR`;
    const position = positionById[Number(positionControl.value)] || null;
    const scoring = scoringById[Number(scoringControl.value)] || null;
    const exact = request.positions.length === 1 && selectedYear === request.season &&
      position === request.positions[0] && scoring === request.scoring &&
      ((request.horizon === 'weekly' && weekMatch &&
        Number(weekMatch[1]) === request.week) ||
       (request.horizon === 'ros' && ros));
    return {
      season: exact ? selectedYear : null,
      week: exact && weekMatch ? Number(weekMatch[1]) : null,
      horizon: exact ? (ros ? 'ros' : 'weekly') : null,
      scoring: exact ? scoring : null,
      positions: exact ? [position] : [],
      period_text: exact
        ? `${selectedYear} | FantasySharks ${position} ${ros ? 'Rest of Year' : `Week ${weekMatch[1]}`} | ${scoring}`
        : ''
    };
  };
  let genericDimensions = null;
  let source;
  if (provider === 'yahoo') {
    source = yahooSource();
  } else if (provider === 'cbs') {
    source = cbsSource();
  } else if (provider === 'fftoday') {
    source = fftodaySource();
  } else if (provider === 'fantasysharks') {
    source = fantasySharksSource();
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
  const groupedHeaders = (candidates, headerRow, rawHeaders) => {
    if (!['yahoo', 'cbs', 'fftoday'].includes(provider) || candidates.length < 2) return rawHeaders;
    const groupRow = candidates[candidates.length - 2];
    const groups = [];
    for (const cell of Array.from(groupRow.cells)) {
      for (let index = 0; index < Math.max(1, cell.colSpan || 1); index += 1) {
        groups.push(normalized(cell.innerText));
      }
    }
    if (groups.length !== rawHeaders.length) return rawHeaders;
    const defense = rawHeaders.some((header) =>
      header === 'DEFENSE SPECIAL TEAMS' ||
      (provider === 'cbs' && header === 'TEAM' && request.positions[0] === 'DST'));
    const counts = rawHeaders.reduce((result, header) => {
      result[header] = (result[header] || 0) + 1;
      return result;
    }, Object.create(null));
    return rawHeaders.map((header, index) => {
      if (playerColumn(header)) return 'PLAYER';
      const group = groups[index];
      const prefixes = {PASSING: 'PASS', RUSHING: 'RUSH', RECEIVING: 'REC'};
      if (prefixes[group] && header && (provider === 'yahoo' || counts[header] > 1)) {
        return `${prefixes[group]} ${header}`;
      }
      if (provider === 'yahoo' && group === 'RET' && header) return `RET ${header}`;
      if (provider === 'yahoo' && group === 'MISC' && header) {
        return defense ? `DEF ${header}` : `MISC ${header}`;
      }
      if (provider === 'yahoo' && group === 'FUM' && header) return `FUM ${header}`;
      if (provider === 'yahoo' && group === 'FIELD GOALS MADE' && header) return `FGM ${header}`;
      if (provider === 'yahoo' && group === 'PAT' && header === 'MADE') return 'XPM';
      if (provider === 'yahoo' && defense &&
          ['TACKLES', 'TURNOVERS', 'TD'].includes(group) && header) {
        return `DEF ${header}`;
      }
      return header;
    });
  };
  const fftodayOccurrenceHeaders = (headers) => {
    if (provider !== 'fftoday' || request.positions.length !== 1) return headers;
    const position = request.positions[0];
    if (position === 'QB') {
      let afterInterception = false;
      return headers.map((header) => {
        if (header === 'INT') {
          afterInterception = true;
          return header;
        }
        if (!['ATT', 'YDS', 'TD'].includes(header)) return header;
        return `${afterInterception ? 'RUSH' : 'PASS'} ${header}`;
      });
    }
    if (['RB', 'WR', 'TE'].includes(position)) {
      let group = null;
      return headers.map((header) => {
        if (header === 'ATT') {
          group = 'RUSH';
          return 'RUSH ATT';
        }
        if (header === 'REC') {
          group = 'REC';
          return header;
        }
        return group && ['YDS', 'TD'].includes(header)
          ? `${group} ${header}` : header;
      });
    }
    return headers;
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
      let candidates = Array.from(table.querySelectorAll('thead tr')).filter(visible);
      if (provider === 'fftoday') {
        candidates = Array.from(table.querySelectorAll('tr.tablehdr, tr.tableclmhdr'))
          .filter(visible);
      }
      const headerRow = candidates.length ? candidates[candidates.length - 1] :
        Array.from(table.rows).filter(visible).find((row) => {
          const headers = Array.from(row.cells).map((cell) => providerHeader(cell.innerText));
          return headers.some(playerColumn) && headers.some(statColumn);
        });
      if (!headerRow) return null;
      const rawHeaders = Array.from(headerRow.cells).map((cell) => providerHeader(cell.innerText));
      let headers = fftodayOccurrenceHeaders(
        groupedHeaders(candidates, headerRow, rawHeaders)
      );
      if (provider === 'fantasysharks') {
        let opponentCount = 0;
        headers = headers.map((header) => {
          if (header !== 'OPP') return header;
          opponentCount += 1;
          return request.horizon === 'weekly' && opponentCount === 1
            ? 'OPP' : 'SCORING OPPORTUNITIES';
        });
      }
      let allowed = headers.map((header, index) => (
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
          [...new Set(Array.from(cells[playerIndex].querySelectorAll('a[href]'))
            .map((anchor) => publicLink(
              anchor,
              provider === 'cbs' && request.positions[0] === 'DST' ? 'DST' : null
            )).filter(Boolean))];
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
  if (!['yahoo', 'cbs', 'fftoday', 'fantasysharks'].includes(provider) && !source.positions.length) {
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


__all__ = ("ADVANCE_PROJECTION_SCRIPT", "PROJECTION_PAGE_SCRIPT")
