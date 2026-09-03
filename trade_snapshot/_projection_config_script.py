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
    const periodValue = request.horizon === 'weekly'
      ? `S_PW_${Number(request.week)}`
      : `S_PSR_${Number(request.season)}`;
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
