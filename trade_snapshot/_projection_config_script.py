"""Visible-control projection configuration before table extraction."""


CONFIGURE_PROJECTION_SCRIPT = r"""
(request) => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const upper = (value) => clean(value).toUpperCase();
  const visible = (node) => {
    if (!node || !node.isConnected) return false;
    const style = getComputedStyle(node), box = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity) !== 0 && box.width > 0 && box.height > 0;
  };
  const controls = Array.from(document.querySelectorAll('select')).filter(visible);
  if (request.provider === 'fantasypros') {
    if (!Array.isArray(request.positions) || request.positions.length !== 1 ||
        request.horizon !== 'weekly') {
      return {action: 'error', dimension: 'fantasypros projection URL'};
    }
    const expectedPath = `/nfl/projections/${request.positions[0].toLowerCase()}.php`;
    const expected = [['week', String(request.week)]];
    if (request.scoring !== 'HALF') expected.push(['scoring', request.scoring]);
    const actual = Array.from(new URLSearchParams(location.search).entries());
    const matches = location.pathname.toLowerCase() === expectedPath &&
      actual.length === expected.length && expected.every(([key, value]) =>
        actual.filter((item) => item[0] === key && item[1] === value).length === 1);
    return matches ? {action: 'ready'} :
      {action: 'error', dimension: 'fantasypros projection URL'};
  }
  if (request.provider === 'cbs') {
    return {action: 'ready'};
  }
  if (request.provider === 'fftoday') {
    const positionDetails = {QB: ['10', 'QUARTERBACK'], RB: ['20', 'RUNNING BACK'],
      WR: ['30', 'WIDE RECEIVER'], TE: ['40', 'TIGHT END'],
      DL: ['50', 'DEFENSIVE LINEMAN'], LB: ['60', 'LINEBACKER'],
      DB: ['70', 'DEFENSIVE BACK'], K: ['80', 'KICKER']};
    const scoringIds = {STD: '1', HALF: '193033', PPR: '107644'};
    const scoringLabels = {STD: 'FFTODAY STANDARD', HALF: 'FFTODAY HALF PPR',
      PPR: 'FFTODAY PPR'};
    const weekly = request.horizon === 'weekly';
    const supportedPositions = weekly
      ? ['QB', 'RB', 'WR', 'TE', 'K']
      : ['QB', 'RB', 'WR', 'TE', 'K', 'DL', 'LB', 'DB'];
    if (request.positions.length !== 1 ||
        !supportedPositions.includes(request.positions[0]) ||
        !positionDetails[request.positions[0]] ||
        !scoringIds[request.scoring]) {
      return {action: 'error', dimension: 'fftoday request'};
    }
    const expectedPath = weekly ? '/rankings/playerwkproj.php' : '/rankings/playerproj.php';
    if (location.pathname !== expectedPath) {
      return {action: 'error', dimension: 'fftoday path'};
    }
    const target = new URL(expectedPath, location.origin);
    target.searchParams.set('LeagueID', scoringIds[request.scoring]);
    target.searchParams.set('PosID', positionDetails[request.positions[0]][0]);
    target.searchParams.set('Season', String(request.season));
    if (weekly) target.searchParams.set('GameWeek', String(request.week));
    target.hash = location.hash;
    const current = new URL(location.href);
    const keys = weekly
      ? ['LeagueID', 'PosID', 'Season', 'GameWeek']
      : ['LeagueID', 'PosID', 'Season'];
    const currentEntries = Array.from(current.searchParams.entries());
    const exactUrl = current.pathname === expectedPath &&
      currentEntries.length === keys.length && keys.every((key) =>
        currentEntries.filter(([name, value]) =>
          name === key && value === target.searchParams.get(key)).length === 1);
    const normalized = (value) => upper(value).replace(/[^A-Z0-9]+/g, ' ').trim()
      .replace(/\bFF TODAY\b/g, 'FFTODAY');
    const projectionHeaders = () => Array.from(document.querySelectorAll('.pageheader'))
      .filter(visible).map((node) => normalized(node.innerText))
      .filter((text) => text.includes('PROJECTIONS'));
    const scoringControls = () => controls.filter((control) => control.name === 'LeagueID');
    const tableHeaderRows = () => Array.from(document.querySelectorAll('tr.tableclmhdr'))
      .filter(visible);
    const tableDimensionLinks = (row) => Array.from(row.querySelectorAll('a[href]'))
      .map((anchor) => { try { return new URL(anchor.href); } catch (_) { return null; } })
      .filter((url) => url && url.pathname === expectedPath &&
        url.searchParams.has('order_by') && url.searchParams.has('sort_order'));
    const tableDimensionsMatch = (row) => {
      const links = tableDimensionLinks(row);
      return links.length >= 2 && links.every((url) => keys.every((key) =>
        url.searchParams.getAll(key).length === 1 &&
        url.searchParams.get(key) === target.searchParams.get(key)));
    };
    const pageFingerprint = () => JSON.stringify([
      normalized(document.title), projectionHeaders(),
      scoringControls().map((control) => [control.value,
        normalized(control.selectedOptions[0]?.textContent)]),
      tableHeaderRows().map((row) => [
        normalized(row.previousElementSibling?.matches('tr.tablehdr')
          ? row.previousElementSibling.innerText : ''),
        normalized(row.innerText),
        tableDimensionLinks(row).slice(0, 32).map((url) =>
          `${url.pathname}?${url.searchParams.toString()}`)
      ])
    ]);
    const positionLabel = positionDetails[request.positions[0]][1];
    const expectedTitle = `${positionLabel} PROJECTIONS ${request.season} ${weekly
      ? `WEEK ${request.week}` : 'REGULAR SEASON'} FFTODAY`;
    const expectedHeader = `${positionLabel} PROJECTIONS`;
    const headers = projectionHeaders();
    const scoring = scoringControls();
    const tableRows = tableHeaderRows();
    const pageReady = normalized(document.title) === expectedTitle &&
      headers.length === 1 &&
      [expectedHeader, `${expectedHeader} ${request.season}`].includes(headers[0]) &&
      scoring.length === 1 && scoring[0].value === scoringIds[request.scoring] &&
      scoring[0].selectedOptions.length === 1 &&
      normalized(scoring[0].selectedOptions[0].textContent) === scoringLabels[request.scoring] &&
      tableRows.length === 1 && tableDimensionsMatch(tableRows[0]);
    if (!exactUrl) {
      const fingerprint = pageFingerprint().slice(0, 8192);
      location.replace(target.href);
      return {action: 'changed', dimension: 'fftoday dimensions', fingerprint,
        require_change: !pageReady};
    }
    const unavailable = Array.from(document.querySelectorAll('p'))
      .filter(visible).map((node) => upper(node.innerText))
      .some((text) => text === 'NO PLAYER FOUND!');
    if (unavailable) {
      return {action: 'error', dimension: 'fftoday availability'};
    }
    const fingerprint = pageFingerprint().slice(0, 8192);
    return pageReady ? {action: 'ready', fingerprint} :
      {action: 'waiting', dimension: 'fftoday content', fingerprint};
  }
  if (request.provider === 'fantasysharks') {
    const one = (name) => {
      const matches = controls.filter((control) => control.name === name);
      return matches.length === 1 ? matches[0] : null;
    };
    const segment = one('Segment'), position = one('Position'), scoring = one('scoring');
    const positionIds = {QB: '1', RB: '2', WR: '4', TE: '5', DST: '6', K: '7',
      DL: '8', LB: '9', DB: '10'};
    const scoringIds = {STD: '1', HALF: '18', PPR: '2'};
    if (!segment || !position || !scoring || request.positions.length !== 1 ||
        !positionIds[request.positions[0]] || !scoringIds[request.scoring]) {
      return {action: 'error', dimension: 'fantasysharks controls'};
    }
    let optionYear = null, desiredSegment = null;
    for (const option of Array.from(segment.options)) {
      const label = upper(option.textContent);
      const season = label.match(/^(20\d{2}) NFL SEASON$/);
      if (season) optionYear = Number(season[1]);
      const desired = request.horizon === 'ros'
        ? label === `${request.season} REST OF YEAR`
        : optionYear === request.season && label === `WEEK ${request.week}`;
      if (desired) desiredSegment = option.value;
    }
    if (!desiredSegment) {
      return {action: 'error', dimension: 'fantasysharks period'};
    }
    const tableFingerprint = () => {
      const tables = Array.from(document.querySelectorAll('table#toolData')).filter(visible);
      return JSON.stringify(tables.map((table) => {
        const rows = Array.from(table.rows).filter(visible);
        return [rows.length, ...rows.slice(0, 24).map((row) => [
          upper(row.innerText).slice(0, 600),
          Array.from(row.querySelectorAll('a[href]')).slice(0, 8).map((anchor) => {
            try {
              const url = new URL(anchor.href);
              return `${url.pathname}?${url.searchParams.toString()}`;
            } catch (_) { return ''; }
          })
        ])];
      })).slice(0, 8192);
    };
    const visibleProjectionTable = () => {
      const tables = Array.from(document.querySelectorAll('table#toolData')).filter(visible);
      return tables.length === 1 && Array.from(
        tables[0].querySelectorAll('a[href*="playerpage.php?id="]')
      ).some(visible);
    };
    for (const [control, value, dimension] of [
      [segment, desiredSegment, 'period'],
      [position, positionIds[request.positions[0]], 'position'],
      [scoring, scoringIds[request.scoring], 'scoring']
    ]) {
      if (control.value === value) continue;
      const fingerprint = tableFingerprint();
      control.value = value;
      const selected = Array.from(control.options).find((option) => option.value === value);
      if (!selected) return {action: 'error', dimension};
      selected.selected = true;
      control.dispatchEvent(new Event('input', {bubbles: true}));
      control.dispatchEvent(new Event('change', {bubbles: true}));
      return {action: 'changed', dimension, fingerprint, require_change: true};
    }
    const fingerprint = tableFingerprint();
    return visibleProjectionTable()
      ? {action: 'ready', fingerprint}
      : {action: 'waiting', dimension: 'fantasysharks content', fingerprint};
  }
  if (request.provider === 'yahoo') {
    const exactControl = (selector) => {
      const matches = Array.from(document.querySelectorAll(selector));
      return matches.length === 1 ? matches[0] : null;
    };
    const status = exactControl('select#statusselect[name="status"]');
    const stats = exactControl('select#statselect[name="stat1"]');
    if (!status || !stats || request.positions.length !== 1) {
      return {action: 'error', dimension: 'yahoo controls'};
    }
    const weeklyPeriod = `S_PW_${Number(request.week)}`;
    const remainingPeriod = `S_PSR_${Number(request.season)}`;
    const fullSeasonPeriod = `S_PS_${Number(request.season)}`;
    const availablePeriods = new Set(
      Array.from(stats.options).map((option) => option.value)
    );
    const periodValue = request.horizon === 'weekly'
      ? weeklyPeriod
      : availablePeriods.has(remainingPeriod)
        ? remainingPeriod
        : Number(request.week) === 1 && availablePeriods.has(fullSeasonPeriod)
          ? fullSeasonPeriod
          : remainingPeriod;
    const periodOptions = Array.from(stats.options).filter(
      (option) => option.value === periodValue
    );
    const statusOptions = Array.from(status.options).filter(
      (option) => option.value === 'ALL'
    );
    if (periodOptions.length !== 1 || statusOptions.length !== 1) {
      return {action: 'error', dimension: 'yahoo period'};
    }
    if (status.value !== 'ALL' || !statusOptions[0].selected) {
      status.value = 'ALL';
      statusOptions[0].selected = true;
      status.dispatchEvent(new Event('input', {bubbles: true}));
      status.dispatchEvent(new Event('change', {bubbles: true}));
      return {action: 'changed', dimension: 'availability'};
    }
    if (stats.value !== periodValue || !periodOptions[0].selected) {
      stats.value = periodValue;
      periodOptions[0].selected = true;
      stats.dispatchEvent(new Event('input', {bubbles: true}));
      stats.dispatchEvent(new Event('change', {bubbles: true}));
      return {action: 'changed', dimension: 'period'};
    }
    const requestedPosition = request.positions[0] === 'DST'
      ? 'DEF' : request.positions[0] === 'ALL' ? 'O' : request.positions[0];
    const radios = Array.from(document.querySelectorAll('input[name="pos"][type="radio"]'))
      .filter((control) => control.value === requestedPosition);
    if (radios.length !== 1) {
      return {action: 'error', dimension: 'yahoo position'};
    }
    if (!radios[0].checked) {
      radios[0].click();
      return {action: 'changed', dimension: 'position'};
    }
    return {action: 'ready'};
  }
  const positionNames = {
    QB: /^(?:QB|QUARTERBACKS?)$/,
    RB: /^(?:RB|RUNNING BACKS?)$/,
    WR: /^(?:WR|WIDE RECEIVERS?)$/,
    TE: /^(?:TE|TIGHT ENDS?)$/,
    K: /^(?:K|KICKERS?)$/,
    DST: /^(?:DST|D\/ST|DEFENSES?)$/,
    FLX: /^(?:FLX|FLEX|RB\/WR\/TE)$/,
    ALL: /^(?:ALL|ALL PLAYERS|OFFENSE)$/
  };
  const customControls = Array.from(document.querySelectorAll(
    '[role="combobox"], [aria-haspopup="listbox"]'
  )).filter((node) => visible(node) &&
    !node.parentElement?.closest('[role="combobox"], [aria-haspopup="listbox"]'));
  const referencedText = (node, attribute) => clean(node.getAttribute(attribute))
    .split(' ').filter(Boolean).map((id) => clean(document.getElementById(id)?.innerText))
    .filter(Boolean);
  const customLabel = (node) => {
    const direct = [clean(node.getAttribute('aria-label')), ...referencedText(node, 'aria-labelledby')];
    if (node.id) direct.push(...Array.from(document.querySelectorAll(
      `label[for="${CSS.escape(node.id)}"]`
    )).map((label) => clean(label.innerText)));
    for (let parent = node.parentElement, depth = 0; parent && depth < 3;
         parent = parent.parentElement, depth += 1) {
      direct.push(...Array.from(parent.children).filter((child) =>
        child !== node && child.matches?.('label, .form__label, [data-filter-label]')
      ).map((label) => clean(label.innerText)));
    }
    return upper(direct.filter(Boolean).join(' '));
  };
  const customValues = (node) => [
    clean(node.getAttribute('aria-valuetext')),
    clean(node.getAttribute('data-value')),
    clean(node.querySelector('[aria-selected="true"], [data-selected="true"]')?.innerText),
    clean(node.innerText),
  ].filter(Boolean).map(upper);
  const customOptions = (node) => {
    const ids = [node.getAttribute('aria-controls'), node.getAttribute('aria-owns')]
      .flatMap((value) => clean(value).split(' ').filter(Boolean));
    const roots = ids.map((id) => document.getElementById(id)).filter(Boolean);
    if (!roots.length) roots.push(...Array.from(document.querySelectorAll(
      '[role="listbox"], [role="menu"]'
    )).filter(visible));
    const options = roots.flatMap((root) => Array.from(root.querySelectorAll(
      '[role="option"], [role="menuitem"], [data-option-value]'
    ))).filter(visible);
    return [...new Set(options)];
  };
  const targets = [];
  targets.push({
    name: 'season',
    label: /\bSEASON\b/,
    proof: true,
    proofMatch: (text) => new RegExp(`\\b${request.season}\\b`).test(text),
    match: (text) => new RegExp(`^(?:NFL\\s+)?${request.season}(?:\\s+SEASON)?$`).test(text),
  });
  if (request.horizon === 'weekly') {
    const week = Number(request.week);
    targets.push({name: 'period', label: /\b(?:SCORING PERIOD|STAT(?:S| SPLIT)?|PROJECTION PERIOD)\b/,
      proof: true,
      proofMatch: (text) => new RegExp(`\\bWEEK\\s*${week}\\b`).test(text),
      match: (text) =>
      new RegExp(`^(?:WEEK\\s*)?${week}(?:\\s+(?:PROJ|PROJECTIONS?))?$`).test(text) ||
      new RegExp(`^WEEK\\s*${week}\\b`).test(text)});
  } else {
    targets.push({name: 'period', label: /\b(?:SCORING PERIOD|STAT(?:S| SPLIT)?|PROJECTION PERIOD)\b/,
      proof: true,
      proofMatch: (text) => /\bREST OF (?:THE )?SEASON\b|\bROS\b/.test(text),
      match: (text) =>
      /^(?:ROS|REST OF (?:THE )?SEASON|REMAINING SEASON)(?: PROJECTIONS?)?$/.test(text)});
  }
  const scoring = request.scoring;
  targets.push({name: 'scoring', label: /^(?:SCORING|SCORING TYPE)$/, proof: true,
    proofMatch: (text) => scoring === 'PPR'
      ? /\bPPR\b/.test(text) && !/\b(?:HALF|0\.5)\b/.test(text)
      : scoring === 'HALF' ? /\b(?:HALF|0\.5)\s+(?:POINT\s+)?PPR\b/.test(text)
      : /\b(?:STD|STANDARD|NON PPR)\b/.test(text),
    match: (text) => scoring === 'PPR'
    ? /^(?:PPR|FULL PPR|POINTS? PPR|POINT PER RECEPTION)$/.test(text)
    : scoring === 'HALF'
    ? /^(?:HALF PPR|HALF POINT PPR|POINTS? HALF PPR|0\.5 PPR)$/.test(text)
    : /^(?:STD|STANDARD|NON PPR|POINTS? NON PPR)$/.test(text)});
  if (request.positions.length === 1 && positionNames[request.positions[0]]) {
    const matcher = positionNames[request.positions[0]];
    targets.push({name: 'position', label: /\b(?:POSITION|LINEUP SLOT)\b/, proof: true,
      proofMatch: (text) => new RegExp(`\\b${request.positions[0]}\\b`).test(text),
      match: (text) => matcher.test(text)});
  }
  const proofNodes = [document.title, ...Array.from(document.querySelectorAll(
    'h1, h2, [aria-current="true"], [aria-selected="true"]'
  )).filter(visible).map((node) => node.innerText)].map(upper).filter(Boolean);
  for (const target of targets) {
    const nativeMatches = [];
    for (const control of controls) {
      const options = Array.from(control.options).filter((option) => target.match(upper(option.text)));
      if (options.length > 1) return {action: 'error', dimension: target.name};
      if (options.length === 1) nativeMatches.push([control, options[0]]);
    }
    const customMatches = customControls.filter((control) => target.label.test(customLabel(control)));
    if (nativeMatches.length + customMatches.length > 1) {
      return {action: 'error', dimension: target.name};
    }
    if (nativeMatches.length === 1) {
      const [control, option] = nativeMatches[0];
      if (control.value === option.value && option.selected) continue;
      control.value = option.value;
      option.selected = true;
      control.dispatchEvent(new Event('input', {bubbles: true}));
      control.dispatchEvent(new Event('change', {bubbles: true}));
      return {action: 'changed', dimension: target.name};
    }
    if (customMatches.length === 1) {
      const control = customMatches[0];
      if (customValues(control).some(target.match)) continue;
      if (control.getAttribute('aria-expanded') !== 'true') {
        control.click();
        return {action: 'changed', dimension: target.name};
      }
      const options = customOptions(control).filter((option) => target.match(upper(option.innerText)));
      if (options.length !== 1) return {action: 'error', dimension: target.name};
      options[0].click();
      return {action: 'changed', dimension: target.name};
    }
    if (target.proof && proofNodes.some(target.proofMatch || target.match)) continue;
    return {action: 'error', dimension: target.name};
  }
  return {action: 'ready'};
}
"""


__all__ = ("CONFIGURE_PROJECTION_SCRIPT",)
