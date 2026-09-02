(() => {
  "use strict";

  const protocol = globalThis.FTEProtocol;
  const pageOrigin = protocol.parseLoopbackOrigin(location.origin);
  if (!pageOrigin || window.top !== window) return;

  const postToApp = (type, detail = {}) => {
    window.postMessage({
      source: protocol.EXTENSION_SOURCE,
      protocol_version: protocol.VERSION,
      type,
      ...detail
    }, pageOrigin);
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== pageOrigin) return;
    const pair = protocol.validatePairRequest(event.data, event.origin);
    const disconnect = pair ? null :
      protocol.validateDisconnectRequest(event.data, event.origin);
    if (!pair && !disconnect) return;

    const kind = pair ? "fte.local.pair" : "fte.local.disconnect";
    chrome.runtime.sendMessage({kind, value: pair || disconnect}).then((response) => {
      if (!response || response.ok !== true) {
        postToApp(pair ? "pair.rejected" : "session.disconnect_failed", {
          error: response?.error || "extension_unavailable"
        });
        return;
      }
      postToApp(pair ? "pair.pending" : "session.disconnecting");
    }).catch(() => {
      postToApp(pair ? "pair.rejected" : "session.disconnect_failed", {
        error: "extension_unavailable"
      });
    });
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.kind !== "fte.worker.event" ||
        !protocol.isRecord(message.event) || typeof message.event.type !== "string") return;
    const {type, ...detail} = message.event;
    postToApp(type, detail);
  });

  postToApp("bridge.ready", {capabilities: [...protocol.OPERATIONS]});
})();
