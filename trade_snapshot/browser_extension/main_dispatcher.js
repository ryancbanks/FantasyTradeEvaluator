(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const channel = "fte-extension-main/v1";
  const requestSource = "fte-extension-isolated";
  const responseSource = "fte-extension-main";
  const allowed = new Set([
    "analyzer.finish",
    "analyzer.abort",
    "ecr.capture",
    "league.capture",
    "espn.authenticated_json",
    "espn.season_projections"
  ]);
  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  Object.freeze(handlers);

  const safeError = (error) => {
    const value = typeof error?.message === "string" ? error.message : "";
    return /^[a-z0-9_]{1,64}$/.test(value) ? value : "main_operation_failed";
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    const message = event.data;
    if (!message || typeof message !== "object" || Array.isArray(message) ||
        message.channel !== channel || message.source !== requestSource ||
        typeof message.request_id !== "string" ||
        !/^[a-f0-9]{32}$/.test(message.request_id) || !allowed.has(message.operation) ||
        !message.payload || typeof message.payload !== "object" ||
        Array.isArray(message.payload)) return;
    const handler = handlers[message.operation];
    if (typeof handler !== "function") return;
    Promise.resolve().then(() => handler(message.payload)).then(
      (value) => window.postMessage({
        channel,
        source: responseSource,
        request_id: message.request_id,
        ok: true,
        value: value === undefined ? null : value
      }, location.origin),
      (error) => window.postMessage({
        channel,
        source: responseSource,
        request_id: message.request_id,
        ok: false,
        error: safeError(error)
      }, location.origin)
    );
  });
})();
