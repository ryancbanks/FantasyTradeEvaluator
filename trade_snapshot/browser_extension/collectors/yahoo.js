(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.FTECollectors ||
    (globalThis.FTECollectors = Object.create(null));
  const readScoring = () => ((() => {
  const visible = (element) => {
    if (!element || !element.isConnected) return false;
    for (let current = element; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
          style.contentVisibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    const box = element.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const text = (element) => String(element?.innerText || element?.textContent || '')
    .replace(/\s+/g, ' ').trim();
  const tables = [...document.querySelectorAll('table#settings-stat-mod-table')]
    .filter(visible);
  if (!tables.length) return null;
  if (tables.length !== 1) return {error: 'settings_table_ambiguous'};
  const matches = [...tables[0].querySelectorAll('tr')].filter((row) => {
    if (!visible(row)) return false;
    const cells = [...row.querySelectorAll(':scope > th, :scope > td')];
    return cells.length >= 2 && visible(cells[0]) && visible(cells[1]) &&
      text(cells[0]).toUpperCase() === 'RECEPTIONS';
  });
  if (!matches.length) return null;
  if (matches.length !== 1) return {error: 'receptions_ambiguous'};
  const cells = [...matches[0].querySelectorAll(':scope > th, :scope > td')];
  if (cells.length < 2 || cells.length > 3) return {error: 'receptions_shape'};
  const modifier = text(cells[1]);
  const scoring = {
    '0': 'STD', '0.0': 'STD', '0.00': 'STD',
    '.5': 'HALF', '0.5': 'HALF', '0.50': 'HALF',
    '1': 'PPR', '1.0': 'PPR', '1.00': 'PPR'
  }[modifier];
  return scoring ? {scoring} : {error: 'unsupported_receptions'};
})());

  handlers["yahoo.scoring"] = () => readScoring();
})();
