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
  if (request.provider === 'cbs') {
    return {action: 'ready'};
  }
  if (request.provider === 'fftoday') {
    const positionIds = {QB: '10', RB: '20', WR: '30', TE: '40', DL: '50',
      LB: '60', DB: '70', K: '80'};
    const scoringIds = {STD: '1', HALF: '193033', PPR: '107644'};
    const weekly = request.horizon === 'weekly';
    const supportedPositions = weekly
      ? ['QB', 'RB', 'WR', 'TE', 'K']
      : ['QB', 'RB', 'WR', 'TE', 'K', 'DL', 'LB', 'DB'];
    if (request.positions.length !== 1 ||
        !supportedPositions.includes(request.positions[0]) ||
        !positionIds[request.positions[0]] ||
        !scoringIds[request.scoring]) {
      return {action: 'error', dimension: 'fftoday request'};
    }
    const expectedPath = weekly ? '/rankings/playerwkproj.php' : '/rankings/playerproj.php';
    if (location.pathname !== expectedPath) {
      return {action: 'error', dimension: 'fftoday path'};
    }
    const target = new URL(expectedPath, location.origin);
    target.searchParams.set('LeagueID', scoringIds[request.scoring]);
    target.searchParams.set('PosID', positionIds[request.positions[0]]);
    target.searchParams.set('Season', String(request.season));
    if (weekly) target.searchParams.set('GameWeek', String(request.week));
    const current = new URL(location.href);
    const keys = weekly
      ? ['LeagueID', 'PosID', 'Season', 'GameWeek']
      : ['LeagueID', 'PosID', 'Season'];
    if (keys.some((key) => current.searchParams.get(key) !== target.searchParams.get(key))) {
      location.replace(target.href);
      return {action: 'changed', dimension: 'fftoday dimensions'};
    }
    const unavailable = Array.from(document.querySelectorAll('p'))
      .filter(visible).map((node) => upper(node.innerText))
      .some((text) => text === 'NO PLAYER FOUND!');
    if (unavailable) {
      return {action: 'error', dimension: 'fftoday availability'};
    }
    return {action: 'ready'};
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
    for (const [control, value, dimension] of [
      [segment, desiredSegment, 'period'],
      [position, positionIds[request.positions[0]], 'position'],
      [scoring, scoringIds[request.scoring], 'scoring']
    ]) {
      if (control.value === value) continue;
      control.value = value;
      const selected = Array.from(control.options).find((option) => option.value === value);
      if (!selected) return {action: 'error', dimension};
      selected.selected = true;
      control.dispatchEvent(new Event('input', {bubbles: true}));
      control.dispatchEvent(new Event('change', {bubbles: true}));
      return {action: 'changed', dimension};
    }
    return {action: 'ready'};
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
  const targets = [];
  targets.push({name: 'season', match: (text) => text === String(request.season)});
  if (request.horizon === 'weekly') {
    const week = Number(request.week);
    targets.push({name: 'period', match: (text) =>
      new RegExp(`^(?:WEEK\\s*)?${week}(?:\\s+(?:PROJ|PROJECTIONS?))?$`).test(text) ||
      new RegExp(`^WEEK\\s*${week}\\b`).test(text)});
  } else {
    targets.push({name: 'period', match: (text) =>
      /^(?:ROS|REST OF (?:THE )?SEASON|REMAINING SEASON)(?: PROJECTIONS?)?$/.test(text)});
  }
  const scoring = request.scoring;
  targets.push({name: 'scoring', optional: true, match: (text) => scoring === 'PPR'
    ? /^(?:PPR|FULL PPR|POINT PER RECEPTION)$/.test(text)
    : scoring === 'HALF'
    ? /^(?:HALF PPR|HALF POINT PPR|0\.5 PPR)$/.test(text)
    : /^(?:STD|STANDARD|NON PPR)$/.test(text)});
  if (request.positions.length === 1 && positionNames[request.positions[0]]) {
    const matcher = positionNames[request.positions[0]];
    targets.push({name: 'position', optional: true, match: (text) => matcher.test(text)});
  }
  for (const target of targets) {
    const matches = [];
    for (const control of controls) {
      const options = Array.from(control.options).filter((option) => target.match(upper(option.text)));
      if (options.length === 1) matches.push([control, options[0]]);
    }
    if (matches.length > 1) return {action: 'error', dimension: target.name};
    if (!matches.length) continue;
    const [control, option] = matches[0];
    if (control.value === option.value && option.selected) continue;
    control.value = option.value;
    option.selected = true;
    control.dispatchEvent(new Event('input', {bubbles: true}));
    control.dispatchEvent(new Event('change', {bubbles: true}));
    return {action: 'changed', dimension: target.name};
  }
  return {action: 'ready'};
}
"""


__all__ = ("CONFIGURE_PROJECTION_SCRIPT",)
