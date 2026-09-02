(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const protocol = globalThis.FTEProtocol;
  const handlers = globalThis.FTECollectors || Object.create(null);
  const mainOperations = new Set([
    "analyzer.finish",
    "analyzer.abort",
    "ecr.capture",
    "league.capture",
    "espn.authenticated_json"
  ]);
  const isolatedOperations = new Set([
    "analyzer.bundle",
    "analyzer.activate_full",
    "page.provenance",
    "projection.configure",
    "projection.read",
    "projection.advance",
    "yahoo.scoring"
  ]);

  const requestId = () => {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  };
  const safeError = (error) => {
    const value = typeof error?.message === "string" ? error.message : "";
    return /^[a-z0-9_]{1,64}$/.test(value) ? value : "collector_failed";
  };

  function callMain(operation, payload) {
    return new Promise((resolve, reject) => {
      const id = requestId();
      const timer = setTimeout(() => {
        window.removeEventListener("message", receive);
        reject(new Error("main_operation_timeout"));
      }, 65000);
      function receive(event) {
        const message = event.data;
        if (event.source !== window || event.origin !== location.origin ||
            !message || message.channel !== protocol.MAIN_CHANNEL ||
            message.source !== "fte-extension-main" || message.request_id !== id) return;
        clearTimeout(timer);
        window.removeEventListener("message", receive);
        if (message.ok === true) resolve(message.value);
        else reject(new Error(typeof message.error === "string" ?
          message.error : "main_operation_failed"));
      }
      window.addEventListener("message", receive);
      window.postMessage({
        channel: protocol.MAIN_CHANNEL,
        source: "fte-extension-isolated",
        request_id: id,
        operation,
        payload
      }, location.origin);
    });
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || message.kind !== "fte.scan.action" ||
        sender.id !== chrome.runtime.id || typeof message.action !== "string" ||
        (!mainOperations.has(message.action) && !isolatedOperations.has(message.action))) {
      return undefined;
    }
    const action = message.action;
    const payload = protocol.isRecord(message.payload) ? message.payload : {};
    const operation = mainOperations.has(action) ?
      callMain(action, payload) :
      Promise.resolve().then(() => {
        const handler = handlers[action];
        if (typeof handler !== "function") throw new Error("collector_unavailable");
        return handler(payload);
      });
    operation.then(
      (value) => sendResponse({ok: true, value: value === undefined ? null : value}),
      (error) => sendResponse({ok: false, error: safeError(error)})
    );
    return true;
  });
})();
