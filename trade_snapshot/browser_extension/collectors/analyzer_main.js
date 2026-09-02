(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  if (!Object.hasOwn(globalThis, "__FTE_MAIN_HANDLERS")) {
    Object.defineProperty(globalThis, "__FTE_MAIN_HANDLERS", {
      value: Object.create(null),
      configurable: false,
      enumerable: false,
      writable: false
    });
  }
  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  const markedAnalyzer = location.protocol === "https:" &&
    ["fantasypros.com", "www.fantasypros.com"].includes(location.hostname.toLowerCase()) &&
    location.pathname === "/nfl/myplaybook/trade-analyzer.php";
  if (markedAnalyzer) {
    (() => {
  if (window.__fteAnalyzerV1) return;
  const state = {queue: [], initQueue: [], error: null, active: true};
  Object.defineProperty(window, '__fteAnalyzerV1', {value: state});
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
  const isEndpoint = (value) => {
    try {
      const url = new URL(value, document.baseURI);
      return url.protocol === 'https:' && url.hostname === 'api.fantasypros.com' &&
        (url.port === '' || url.port === '443') &&
        url.pathname === '/v2/ajax/myplaybook.php' && !url.username && !url.password;
    } catch (_) { return false; }
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
  const matches = (params) => {
    if (!expected || !params) return false;
    const action = (params.get('action') || []).map((item) => item.toLowerCase());
    if (!action.includes('tradeanalyzer')) return false;
    return Object.entries(aliases).every(([field, names]) => {
      const candidate = names.flatMap((name) => params.get(name) || []);
      const actual = ids(candidate.join(','));
      return actual.length === expected[field].length &&
        actual.every((value, index) => value === expected[field][index]);
    });
  };
  const matchesInit = (params) => {
    if (!params) return false;
    const actions = (params.get('action') || []).map((item) => item.toLowerCase());
    const init = (params.get('init') || []).map((item) => item.toLowerCase());
    return actions.includes('tradeanalyzer') && init.includes('y');
  };
  const retain = (url, status, method, requestBody, body) => {
    if (!state.active) return;
    const params = parameters(url, requestBody);
    if (!isEndpoint(url) || status !== 200 || String(method).toUpperCase() !== 'POST') return;
    const trade = matches(params), initial = matchesInit(params);
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
        ([raw, body]) => retain(response.url, response.status,
          init.method || (request && request.method) || 'GET', raw, body), () => {}
      );
      return response;
    };
  }
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__tradeSnapshotMethod = method;
    return originalOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(body) {
    this.addEventListener('load', () => {
      try {
        const parsed = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
        retain(this.responseURL, this.status, this.__tradeSnapshotMethod || 'GET', body, parsed);
      } catch (_) {}
    }, {once: true});
    return originalSend.apply(this, arguments);
  };
})();
  }

  handlers["analyzer.finish"] = () => {
    const state = window.__fteAnalyzerV1;
    if (!markedAnalyzer || !state) throw new Error("analyzer_marker_required");
    if (state.error) throw new Error(state.error);
    if (!state.queue.length) return null;
    return state.queue.shift();
  };
  handlers["analyzer.abort"] = () => {
    const state = window.__fteAnalyzerV1;
    if (state) {
      state.active = false;
      state.queue.length = 0;
      state.initQueue.length = 0;
      state.error = null;
    }
    return {ok: true};
  };
})();
