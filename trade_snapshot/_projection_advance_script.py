"""Finite traversal for a visible projection table."""


ADVANCE_PROJECTION_SCRIPT = r"""
(provider) => {
  const tableSelectors = {
    fantasypros: '#projections-app table, table#projections, table.player-table, main table',
    espn: '.Table__Scroller table.Table, table.Table',
    yahoo: 'table#players-table, #players table, table.Table, main table',
    cbs: 'table.TableBase-table, main table',
    fftoday: 'table:has(tr.tableclmhdr)',
    fantasysharks: 'table#toolData'
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
  const navigationSelector = (
    'a[rel="next"], button[aria-label*="next" i], a[aria-label*="next" i],' +
    '.Pagination__Button--next, .pagination-next, [data-testid*="pagination-next" i],' +
    '[id^="playerspagenav"] li.last a[href]' +
    (provider === 'fftoday' ? ', a[href]' : '')
  );
  const candidates = Array.from(root.querySelectorAll(navigationSelector)).filter(visible);
  const enabled = candidates.filter((node) =>
    (provider !== 'fftoday' || /^NEXT PAGE$/i.test(String(node.textContent || '').trim())) &&
    !node.disabled &&
    node.getAttribute('aria-disabled') !== 'true' && !node.classList.contains('disabled'));
  const destinations = enabled.map((node) => {
    const link = node.closest('a[href]');
    if (!link) return null;
    try {
      const url = new URL(link.href);
      if (url.protocol !== 'https:' || url.origin !== location.origin) return false;
      if (provider === 'yahoo') {
        const current = location.pathname.match(
          /^\/(?:((?:20\d{2}))\/)?f1\/([1-9]\d{0,19})\/(?:players|playersearch)\/?$/
        );
        const target = url.pathname.match(
          /^\/(?:((?:20\d{2}))\/)?f1\/([1-9]\d{0,19})\/(?:players|playersearch)\/?$/
        );
        const currentCount = Number(new URL(location.href).searchParams.get('count') || '0');
        const targetCount = Number(url.searchParams.get('count') || '0');
        if (!current || !target || current[2] !== target[2] ||
            (current[1] && target[1] && current[1] !== target[1]) ||
            !Number.isInteger(currentCount) || !Number.isInteger(targetCount) ||
            targetCount <= currentCount || url.searchParams.get('status') !== 'ALL') return false;
        for (const key of ['stat1', 'pos']) {
          const before = new URL(location.href).searchParams.get(key);
          if (!before || url.searchParams.get(key) !== before) return false;
        }
      } else if (provider === 'fftoday') {
        const current = new URL(location.href);
        if (url.pathname !== current.pathname) return false;
        const coreKeys = current.pathname === '/rankings/playerwkproj.php'
          ? ['Season', 'PosID', 'LeagueID', 'GameWeek']
          : current.pathname === '/rankings/playerproj.php'
          ? ['Season', 'PosID', 'LeagueID'] : [];
        if (!coreKeys.length) return false;
        const currentAllowed = new Set([...coreKeys, 'order_by', 'sort_order', 'cur_page']);
        const targetAllowed = new Set([...coreKeys, 'order_by', 'sort_order', 'cur_page']);
        if ([...current.searchParams.keys()].some((key) => !currentAllowed.has(key)) ||
            [...url.searchParams.keys()].some((key) => !targetAllowed.has(key))) return false;
        for (const key of coreKeys) {
          if (current.searchParams.getAll(key).length !== 1 ||
              url.searchParams.getAll(key).length !== 1 ||
              url.searchParams.get(key) !== current.searchParams.get(key)) return false;
        }
        for (const key of ['order_by', 'sort_order', 'cur_page']) {
          if (current.searchParams.getAll(key).length > 1 ||
              url.searchParams.getAll(key).length !== 1) return false;
        }
        const currentPageText = current.searchParams.get('cur_page');
        const targetPageText = url.searchParams.get('cur_page');
        if ((currentPageText !== null && !/^(?:0|[1-9]\d*)$/.test(currentPageText)) ||
            !/^[1-9]\d*$/.test(targetPageText || '')) return false;
        const currentPage = Number(currentPageText || '0');
        const targetPage = Number(targetPageText);
        const currentOrder = current.searchParams.get('order_by');
        const currentSort = current.searchParams.get('sort_order');
        if ((currentOrder === null) !== (currentSort === null) ||
            (currentOrder !== null &&
             (currentOrder !== 'FFPts' || currentSort !== 'DESC'))) return false;
        if (!Number.isInteger(currentPage) || !Number.isInteger(targetPage) ||
            targetPage !== currentPage + 1 ||
            url.searchParams.get('order_by') !== 'FFPts' ||
            url.searchParams.get('sort_order') !== 'DESC') return false;
        if (currentPage > 0 && currentOrder === null) return false;
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
    if (typeof destinations[0] === 'string') {
      const destination = new URL(destinations[0]);
      destination.hash = 'fte-scan-v1';
      window.location.assign(destination.href);
      return {action: 'next'};
    }
    enabled[0].click();
    return {action: 'next'};
  }
  return {action: 'done'};
}
"""


__all__ = ("ADVANCE_PROJECTION_SCRIPT",)
