"""FantasyPros analyzer interception and allowlisted UI actions."""


ANALYZER_TAP_SCRIPT = r"""
(() => {
  if (window.__tradeSnapshotAnalyzerV2) return;
  const state = {queue: [], initQueue: [], error: null};
  Object.defineProperty(window, '__tradeSnapshotAnalyzerV2', {value: state});
  const ids = (value) => [...new Set(String(value || '').split(',')
    .map((item) => item.trim()).filter((item) => /^[1-9]\d{0,19}$/.test(item)))].sort();
  const expected = (() => {
    const query = new URLSearchParams(location.search);
    const result = {
      team2id: ids(query.get('team2Id')),
      team1gets: ids(query.get('team1Gets')),
      team2gets: ids(query.get('team2Gets')),
      team1adds: ids(query.get('team1Adds')),
      team2adds: ids(query.get('team2Adds'))
    };
    return result.team2id.length === 1 && result.team1gets.length && result.team2gets.length
      ? result : null;
  })();
  const endpointKind = (value, method) => {
    try {
      const url = new URL(value, document.baseURI);
      if (url.protocol !== 'https:' || !['', '443'].includes(url.port) ||
          url.username || url.password) return null;
      const verb = String(method).toUpperCase();
      if (verb === 'GET' && url.hostname === 'mpbnfl.fantasypros.com' &&
          url.pathname === '/api/tradeAnalyzer') return 'current';
      if (verb === 'POST' && url.hostname === 'api.fantasypros.com' &&
          url.pathname === '/v2/ajax/myplaybook.php') return 'legacy';
      return null;
    } catch (_) { return null; }
  };
  const retainedEndpoint = (requestUrl, responseUrl, method) => {
    const requestKind = endpointKind(requestUrl, method);
    if (!requestKind || endpointKind(responseUrl, method) !== requestKind) return null;
    try {
      return new URL(requestUrl, document.baseURI).href ===
        new URL(responseUrl, document.baseURI).href ? requestKind : null;
    } catch (_) { return null; }
  };
  const normalizeKey = (value) => String(value).toLowerCase().replace(/[^a-z0-9]/g, '');
  const aliases = {
    team2id: ['team2id', 'team2', 'otherteamid', 'opponentteamid'],
    team1gets: ['team1gets', 'team1players', 'playersreceived', 'receiveplayers'],
    team2gets: ['team2gets', 'team2players', 'playerssent', 'sendplayers'],
    team1adds: ['team1adds', 'team1addplayers'],
    team2adds: ['team2adds', 'team2addplayers']
  };
  const parameters = (url, raw) => {
    const result = new Map();
    const add = (key, value) => {
      const normalized = normalizeKey(key);
      if (!result.has(normalized)) result.set(normalized, []);
      result.get(normalized).push(String(value));
    };
    try { new URL(url, document.baseURI).searchParams.forEach((value, key) => add(key, value)); } catch (_) {}
    try {
      if (raw instanceof URLSearchParams || raw instanceof FormData)
        raw.forEach((value, key) => add(key, value));
      else if (typeof raw === 'string' && raw.length <= 65536) {
        if (raw.trim().startsWith('{')) {
          const object = JSON.parse(raw);
          if (object && typeof object === 'object' && !Array.isArray(object))
            Object.entries(object).forEach(([key, value]) => add(key,
              Array.isArray(value) ? value.join(',') : value));
        } else new URLSearchParams(raw).forEach((value, key) => add(key, value));
      }
    } catch (_) { return null; }
    return result;
  };
  const matches = (params, endpoint) => {
    if (!expected || !params) return false;
    if (['team1drops', 'team2drops'].some((name) =>
        (params.get(name) || []).some((value) => String(value).trim()))) return false;
    const action = (params.get('action') || []).map((item) => item.toLowerCase());
    if (endpoint === 'legacy' && !action.includes('tradeanalyzer')) return false;
    if (endpoint === 'current' && !currentDimensions(params)) return false;
    return Object.entries(aliases).every(([field, names]) => {
      const candidate = names.flatMap((name) => params.get(name) || []);
      const actual = ids(candidate.join(','));
      return actual.length === expected[field].length &&
        actual.every((value, index) => value === expected[field][index]);
    });
  };
  const identifier = (value) => /^[1-9]\d{0,19}$/.test(String(value));
  const currentDimensions = (params) => {
    const key = params.get('key') || [];
    const team = params.get('team1id') || [];
    const period = (params.get('period') || []).map((item) => item.toLowerCase());
    return key.length === 1 && key[0].length > 0 && key[0].length <= 2048 &&
      team.length === 1 && identifier(team[0]) && period.length === 1 &&
      ['ros', 'dyn', 'pre'].includes(period[0]);
  };
  const matchesInit = (params, endpoint) => {
    if (!params) return false;
    const actions = (params.get('action') || []).map((item) => item.toLowerCase());
    const init = (params.get('init') || []).map((item) => item.toLowerCase());
    return (endpoint !== 'current' || currentDimensions(params)) &&
      (endpoint === 'current' || actions.includes('tradeanalyzer')) && init.includes('y');
  };
  const retain = (requestUrl, responseUrl, status, method, requestBody, redirected, body) => {
    if (redirected) return;
    const endpoint = retainedEndpoint(requestUrl, responseUrl, method);
    const params = parameters(responseUrl, requestBody);
    if (!endpoint || status !== 200) return;
    const trade = matches(params, endpoint), initial = matchesInit(params, endpoint);
    if (!trade && !initial) return;
    if (body === null || typeof body !== 'object' || Array.isArray(body)) return;
    let portable;
    try { portable = JSON.stringify(body); } catch (_) { return; }
    if (portable.length > 8 * 1024 * 1024) return void (state.error = 'response_too_large');
    const retained = JSON.parse(portable);
    if (initial) {
      if (state.initQueue.length >= 32) return void (state.error = 'queue_overflow');
      state.initQueue.push(retained);
    }
    if (trade) {
      if (state.queue.length >= 32) return void (state.error = 'queue_overflow');
      state.queue.push(JSON.parse(portable));
    }
  };
  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = async function(...args) {
      const request = args[0] instanceof Request ? args[0] : null;
      const init = args[1] || {};
      let requestBody = Promise.resolve(null);
      try {
        requestBody = init.body !== undefined ? Promise.resolve(init.body) : request
          ? request.clone().text().catch(() => null) : requestBody;
      } catch (_) {}
      const response = await originalFetch.apply(this, args);
      Promise.all([requestBody, response.clone().json()]).then(
        ([raw, body]) => retain(request ? request.url : args[0], response.url, response.status,
          init.method || (request && request.method) || 'GET', raw, response.redirected, body),
        () => {}
      );
      return response;
    };
  }
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__tradeSnapshotMethod = method;
    this.__tradeSnapshotUrl = url;
    return originalOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(body) {
    this.addEventListener('load', () => {
      try {
        const parsed = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
        retain(this.__tradeSnapshotUrl, this.responseURL, this.status,
          this.__tradeSnapshotMethod || 'GET', body, false, parsed);
      } catch (_) {}
    }, {once: true});
    return originalSend.apply(this, arguments);
  };
})();
"""


TAKE_ANALYZER_BODY_SCRIPT = r"""
() => {
  const state = window.__tradeSnapshotAnalyzerV2;
  if (!state || !Array.isArray(state.queue)) return null;
  if (state.error) return {kind: 'error', value: state.error};
  if (!state.queue.length) return null;
  return {kind: 'body', value: state.queue.shift()};
}
"""


ANALYZER_BUNDLE_SOURCE_SCRIPT = r"""
() => {
  const pattern = /^\/assets\/js\/min\/pages\/myplaybook\/trade-analyzer\/bundle-[a-f0-9]+\.js$/;
  const matches = new Set();
  for (const script of document.scripts) {
    const source = script.getAttribute('src');
    if (!source) continue;
    try {
      const url = new URL(source, document.baseURI);
      if (url.protocol === 'https:' && url.hostname === 'cdn.fantasypros.com' &&
          !url.port && !url.username && !url.password && !url.search && !url.hash &&
          pattern.test(url.pathname)) matches.add(url.href);
    } catch (_) {}
  }
  return matches.size === 1 ? [...matches][0] : null;
}
"""


FULL_ANALYSIS_ACTION_SCRIPT = r"""
() => {
  const visible = (node) => {
    if (!node || !node.isConnected || node.disabled || node.getAttribute('aria-disabled') === 'true')
      return false;
    const style = getComputedStyle(node), box = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
  };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const candidates = Array.from(document.querySelectorAll('main button, main a, button, a'))
    .filter(visible).filter((node) => ['full trade analysis', 'view full trade analysis']
      .includes(clean(node.innerText || node.getAttribute('aria-label'))));
  if (candidates.length !== 1) return {clicked: false, reason: 'not_unique'};
  const link = candidates[0].closest('a[href]');
  if (link) {
    try {
      const url = new URL(link.href);
      if (url.protocol !== 'https:' || url.hostname !== location.hostname ||
          url.pathname !== location.pathname) return {clicked: false, reason: 'unsafe_target'};
    } catch (_) { return {clicked: false, reason: 'unsafe_target'}; }
  }
  candidates[0].click();
  return {clicked: true};
}
"""


PAGE_PROVENANCE_SCRIPT = r"""
() => ({
  protocol: location.protocol,
  hostname: location.hostname.toLowerCase(),
  port: location.port,
  pathname: location.pathname
})
"""


__all__ = (
    "ANALYZER_BUNDLE_SOURCE_SCRIPT", "ANALYZER_TAP_SCRIPT",
    "FULL_ANALYSIS_ACTION_SCRIPT", "PAGE_PROVENANCE_SCRIPT",
    "TAKE_ANALYZER_BODY_SCRIPT",
)
