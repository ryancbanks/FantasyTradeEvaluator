(() => {
  "use strict";

  const VERSION = 1;
  const LOCAL_SOURCE = "fantasy-trade-evaluator-app";
  const EXTENSION_SOURCE = "fantasy-trade-evaluator-extension";
  const MAIN_CHANNEL = "fte-extension-main/v1";
  const SCAN_MARKER = "fte-scan-v1";
  const OPERATIONS = Object.freeze([
    "session.open",
    "session.navigate",
    "analyzer.begin",
    "analyzer.finish",
    "analyzer.abort",
    "analyzer.bundle",
    "analyzer.activate_full",
    "page.provenance",
    "projection.capture",
    "ecr.capture",
    "league.capture",
    "espn.authenticated_json",
    "yahoo.scoring",
    "session.wait",
    "session.close"
  ]);
  const OPERATION_SET = new Set(OPERATIONS);
  const EMPTY_PAYLOAD_OPERATIONS = new Set([
    "analyzer.finish",
    "analyzer.abort",
    "analyzer.bundle",
    "analyzer.activate_full",
    "page.provenance",
    "ecr.capture",
    "yahoo.scoring"
  ]);
  const POSITIONS = new Set([
    "ALL", "QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB", "IDP", "FLX"
  ]);
  const NAVIGATION_PATHS = Object.freeze({
    "fantasypros.com": [
      /^\/nfl\/myplaybook\/trade-analyzer\.php$/,
      /^\/nfl\/projections\/[a-z0-9-]+\.php$/i,
      /^\/nfl\/rankings\/[a-z0-9-]+\.php$/i
    ],
    "www.fantasypros.com": [
      /^\/nfl\/myplaybook\/trade-analyzer\.php$/,
      /^\/nfl\/projections\/[a-z0-9-]+\.php$/i,
      /^\/nfl\/rankings\/[a-z0-9-]+\.php$/i
    ],
    "fantasy.espn.com": [/^\/football\/players\/projections\/?$/],
    "football.fantasysports.yahoo.com": [
      /^\/f1\/players\/?$/,
      /^\/(?:20\d{2}\/)?f1\/[1-9]\d{0,19}\/(?:players|playersearch|settings)\/?$/
    ]
  });
  const ENDPOINTS = Object.freeze({
    pair: "/api/browser-extension/v1/pair",
    poll: "/api/browser-extension/v1/poll",
    result: "/api/browser-extension/v1/result",
    disconnect: "/api/browser-extension/v1/disconnect"
  });
  const CAPABILITIES = Object.freeze({
    protocol_version: VERSION,
    operations: OPERATIONS,
    providers: Object.freeze(["fantasypros", "espn", "yahoo"]),
    dedicated_scan_tab: true,
    persistent_pairing: false,
    arbitrary_code: false,
    cookie_api: false,
    browser_storage_export: false,
      maximum_result_bytes: 64 * 1024 * 1024
  });

  const isRecord = (value) => Boolean(value) && typeof value === "object" &&
    !Array.isArray(value);
  const hasOnlyKeys = (value, allowed, required = allowed) => {
    if (!isRecord(value)) return false;
    const keys = Object.keys(value);
    return keys.every((key) => allowed.includes(key)) &&
      required.every((key) => Object.hasOwn(value, key));
  };
  const isIdentifier = (value) => typeof value === "string" &&
    /^[A-Za-z0-9_-]{16,128}$/.test(value);
  const isSessionToken = (value) => typeof value === "string" &&
    /^[A-Za-z0-9._~-]{32,512}$/.test(value);
  const isInteger = (value, minimum, maximum) => Number.isInteger(value) &&
    value >= minimum && value <= maximum;

  function parseLoopbackOrigin(value) {
    if (typeof value !== "string" || value.length > 256) return null;
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase();
      if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(host) ||
          url.username || url.password || !url.port || url.pathname !== "/" ||
          url.search || url.hash || url.origin !== value) return null;
      return url.origin;
    } catch (_) {
      return null;
    }
  }

  function parseNavigationUrl(value, analyzerOnly = false) {
    if (typeof value !== "string" || value.length > 8192) return null;
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/\.$/, "");
      const patterns = NAVIGATION_PATHS[host];
      if (url.protocol !== "https:" || url.username || url.password ||
          !["", "443"].includes(url.port) || !patterns ||
          !patterns.some((pattern) => pattern.test(url.pathname))) return null;
      const isAnalyzer = ["fantasypros.com", "www.fantasypros.com"].includes(host) &&
        url.pathname === "/nfl/myplaybook/trade-analyzer.php";
      if (analyzerOnly && !isAnalyzer) return null;
      if (url.hash && url.hash !== `#${SCAN_MARKER}`) return null;
      return url;
    } catch (_) {
      return null;
    }
  }

  function validatePairRequest(value, eventOrigin) {
    if (!hasOnlyKeys(value,
      ["source", "protocol_version", "type", "app_origin", "pair_code"])) return null;
    const origin = parseLoopbackOrigin(value.app_origin);
    if (value.source !== LOCAL_SOURCE || value.protocol_version !== VERSION ||
        value.type !== "pair.request" ||
        !isIdentifier(value.pair_code) || !origin || origin !== eventOrigin) return null;
    return {
      appOrigin: origin,
      pairCode: value.pair_code
    };
  }

  function validateDisconnectRequest(value, eventOrigin) {
    if (!hasOnlyKeys(value,
      ["source", "protocol_version", "type", "app_origin"])) return null;
    const origin = parseLoopbackOrigin(value.app_origin);
    if (value.source !== LOCAL_SOURCE || value.protocol_version !== VERSION ||
        value.type !== "session.disconnect" || !origin || origin !== eventOrigin) return null;
    return {appOrigin: origin};
  }

  function projectionRequest(value) {
    const allowed = ["provider", "season", "week", "horizon", "scoring", "positions"];
    if (!hasOnlyKeys(value, allowed) ||
        !["fantasypros", "espn", "yahoo"].includes(value.provider) ||
        !isInteger(value.season, 2000, 2200) ||
        !["weekly", "ros"].includes(value.horizon) ||
        !["STD", "HALF", "PPR"].includes(value.scoring) ||
        !Array.isArray(value.positions) || !value.positions.length || value.positions.length > 12 ||
        value.positions.some((position) => !POSITIONS.has(position)) ||
        new Set(value.positions).size !== value.positions.length) return null;
    if (!isInteger(value.week, 1, 25)) return null;
    return Object.freeze({
      provider: value.provider,
      season: value.season,
      week: value.week,
      horizon: value.horizon,
      scoring: value.scoring,
      positions: Object.freeze([...value.positions])
    });
  }

  function validateOperationPayload(operation, payload) {
    if (!OPERATION_SET.has(operation) || !isRecord(payload)) return null;
    if (EMPTY_PAYLOAD_OPERATIONS.has(operation)) {
      return Object.keys(payload).length === 0 ? Object.freeze({}) : null;
    }
    if (operation === "session.open") {
      if (!hasOnlyKeys(payload, ["action_delay_ms"], [])) return null;
      if (!Object.hasOwn(payload, "action_delay_ms")) return Object.freeze({});
      return isInteger(payload.action_delay_ms, 50, 5000) ? Object.freeze({...payload}) : null;
    }
    if (operation === "session.navigate") {
      if (!hasOnlyKeys(payload, ["url", "timeout_ms"], ["url"])) return null;
      const url = parseNavigationUrl(payload.url);
      if (!url || (Object.hasOwn(payload, "timeout_ms") &&
          !isInteger(payload.timeout_ms, 1000, 120000))) return null;
      return Object.freeze({url: url.href, timeout_ms: payload.timeout_ms || 60000});
    }
    if (operation === "analyzer.begin") {
      if (!hasOnlyKeys(payload, ["phase"]) ||
          !["ordinary_power", "full_playoffs"].includes(payload.phase)) return null;
      return Object.freeze({...payload});
    }
    if (operation === "projection.capture") {
      if (!hasOnlyKeys(payload,
        ["request", "timeout_ms", "action_delay_ms"], ["request", "timeout_ms"]) ||
          !isInteger(payload.timeout_ms, 1000, 300000) ||
          (Object.hasOwn(payload, "action_delay_ms") &&
           !isInteger(payload.action_delay_ms, 50, 5000))) return null;
      const request = projectionRequest(payload.request);
      return request ? Object.freeze({
        request,
        timeout_ms: payload.timeout_ms,
        ...(Object.hasOwn(payload, "action_delay_ms") ?
          {action_delay_ms: payload.action_delay_ms} : {})
      }) : null;
    }
    if (operation === "league.capture") {
      if (!hasOnlyKeys(payload, ["expected_season", "expected_week", "timeout_ms"]) ||
          !isInteger(payload.expected_season, 2000, 2200) ||
          !isInteger(payload.expected_week, 1, 25) ||
          !isInteger(payload.timeout_ms, 1000, 60000)) return null;
      return Object.freeze({...payload});
    }
    if (operation === "espn.authenticated_json") {
      if (!hasOnlyKeys(payload, ["season", "league_id", "timeout_ms", "maximum_bytes"]) ||
          !isInteger(payload.season, 2000, 2200) ||
          typeof payload.league_id !== "string" || !/^[1-9]\d{0,19}$/.test(payload.league_id) ||
          !isInteger(payload.timeout_ms, 1000, 60000) ||
          !isInteger(payload.maximum_bytes, 1024, 64 * 1024 * 1024)) return null;
      return Object.freeze({...payload});
    }
    if (operation === "session.wait") {
      if (!hasOnlyKeys(payload, ["timeout_ms"]) ||
          !isInteger(payload.timeout_ms, 0, 30000)) return null;
      return Object.freeze({...payload});
    }
    if (operation === "session.close") {
      if (!hasOnlyKeys(payload, ["reason"], []) ||
          (Object.hasOwn(payload, "reason") &&
           !["complete", "cancelled"].includes(payload.reason))) return null;
      return Object.freeze({...payload});
    }
    return null;
  }

  function validateOperationEnvelope(value) {
    if (!hasOnlyKeys(value,
      ["protocol_version", "state", "command_id", "op", "payload"]) ||
        value.protocol_version !== VERSION || value.state !== "command" ||
        !isIdentifier(value.command_id) || !OPERATION_SET.has(value.op)) return null;
    const payload = validateOperationPayload(value.op, value.payload);
    return payload ? Object.freeze({
      commandId: value.command_id,
      operation: value.op,
      payload
    }) : null;
  }

  globalThis.FTEProtocol = Object.freeze({
    VERSION,
    LOCAL_SOURCE,
    EXTENSION_SOURCE,
    MAIN_CHANNEL,
    SCAN_MARKER,
    OPERATIONS,
    OPERATION_SET,
    ENDPOINTS,
    CAPABILITIES,
    isRecord,
    isIdentifier,
    isSessionToken,
    parseLoopbackOrigin,
    parseNavigationUrl,
    validatePairRequest,
    validateDisconnectRequest,
    validateOperationEnvelope
  });
})();
