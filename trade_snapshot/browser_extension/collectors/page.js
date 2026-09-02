(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.FTECollectors ||
    (globalThis.FTECollectors = Object.create(null));
  const readBundle = () => {
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
};
  const activateFull = () => {
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
};
  const provenance = () => ({
  protocol: location.protocol,
  hostname: location.hostname.toLowerCase(),
  port: location.port,
  pathname: location.pathname
});

  handlers["page.provenance"] = () => provenance();
  handlers["analyzer.bundle"] = () => {
    const url = readBundle();
    if (!url) throw new Error("analyzer_bundle_not_unique");
    return {url};
  };
  handlers["analyzer.activate_full"] = () => {
    const result = activateFull();
    if (!result || result.clicked !== true) {
      throw new Error(result?.reason || "analyzer_full_action_failed");
    }
    return result;
  };
})();
