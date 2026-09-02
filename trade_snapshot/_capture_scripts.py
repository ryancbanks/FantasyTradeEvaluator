"""Finite browser-side scripts, split by provider responsibility."""

from ._analyzer_script import (
    ANALYZER_BUNDLE_SOURCE_SCRIPT, ANALYZER_TAP_SCRIPT,
    FULL_ANALYSIS_ACTION_SCRIPT,
    PAGE_PROVENANCE_SCRIPT,
    TAKE_ANALYZER_BODY_SCRIPT,
)
from ._ecr_script import ECR_BOOTSTRAP_SCRIPT
from ._league_script import LEAGUE_SOURCE_SCRIPT
from ._projection_config_script import CONFIGURE_PROJECTION_SCRIPT
from ._projection_script import ADVANCE_PROJECTION_SCRIPT, PROJECTION_PAGE_SCRIPT
from ._yahoo_scoring_script import YAHOO_SCORING_SCRIPT


SINGLE_PAGE_SCRIPT = r"""
(() => {
  if (window.__tradeSnapshotSinglePageV2) return;
  Object.defineProperty(window, '__tradeSnapshotSinglePageV2', {value: true});
  const samePage = (value) => {
    if (value === undefined || value === null || String(value).trim() === '') return null;
    try {
      const destination = new URL(String(value), document.baseURI);
      if (destination.protocol !== 'https:') return null;
      window.location.assign(destination.href);
    } catch (_) {}
    return window;
  };
  Object.defineProperty(window, 'open', {
    configurable: true,
    value: function(url, _target, _features) { return samePage(url); }
  });
  document.addEventListener('click', (event) => {
    const link = event.target instanceof Element ? event.target.closest('a[target]') : null;
    if (!link || !['_blank', 'new'].includes(link.target.toLowerCase())) return;
    let destination;
    try { destination = new URL(link.href, document.baseURI); } catch (_) { return; }
    if (destination.protocol !== 'https:') return;
    event.preventDefault();
    window.location.assign(destination.href);
  }, true);
})();
"""


# Backward-compatible internal names; each now has one strict responsibility.
ECR_TABLE_SCRIPT = ECR_BOOTSTRAP_SCRIPT
PROJECTION_TABLE_SCRIPT = PROJECTION_PAGE_SCRIPT


__all__ = (
    "ADVANCE_PROJECTION_SCRIPT", "ANALYZER_BUNDLE_SOURCE_SCRIPT",
    "ANALYZER_TAP_SCRIPT", "CONFIGURE_PROJECTION_SCRIPT",
    "ECR_TABLE_SCRIPT", "LEAGUE_SOURCE_SCRIPT",
    "FULL_ANALYSIS_ACTION_SCRIPT", "PAGE_PROVENANCE_SCRIPT", "PROJECTION_TABLE_SCRIPT",
    "SINGLE_PAGE_SCRIPT", "TAKE_ANALYZER_BODY_SCRIPT", "YAHOO_SCORING_SCRIPT",
)
