(() => {
  "use strict";

  const marker = "fte-scan-v1";
  if (location.hash !== `#${marker}` || window.__fteSinglePageV1) return;
  Object.defineProperty(window, "__fteSinglePageV1", {value: true});

  const samePage = (value) => {
    if (value === undefined || value === null || String(value).trim() === "") return null;
    try {
      const destination = new URL(String(value), document.baseURI);
      if (destination.protocol !== "https:") return null;
      destination.hash = marker;
      window.location.assign(destination.href);
    } catch (_) {}
    return window;
  };

  Object.defineProperty(window, "open", {
    configurable: true,
    value: function(url, _target, _features) {
      return samePage(url);
    }
  });

  document.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[target]") : null;
    if (!link || !["_blank", "new"].includes(link.target.toLowerCase())) return;
    let destination;
    try {
      destination = new URL(link.href, document.baseURI);
    } catch (_) {
      return;
    }
    if (destination.protocol !== "https:") return;
    event.preventDefault();
    destination.hash = marker;
    window.location.assign(destination.href);
  }, true);
})();
