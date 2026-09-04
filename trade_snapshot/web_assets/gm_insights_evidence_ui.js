"use strict";

window.GmInsightsEvidenceUi = (() => {
  const F = window.GmInsightsFormat;

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function statusBadge(text) {
    return node("span", "gm-insights-badge", text);
  }

  function valuationFacts(value) {
    if (!value) return [];
    return [
      ["Power change", F.signedMetric(value.power_delta)],
      ["Relative power edge", F.signedMetric(value.relative_power_edge)],
      ["Playoff chance change", F.signedMetric(value.playoff_probability_delta, "proportion")]
    ].filter(([, result]) => result);
  }

  function valuationPanel(title, value, emptyMessage) {
    const panel = node("section", "gm-insights-valuation-panel");
    panel.append(node("h5", "", title));
    if (!value) {
      panel.append(node("p", "gm-insights-unavailable", emptyMessage));
      return panel;
    }
    const method = F.plainText(value.status);
    if (method) panel.append(statusBadge(method === "holdout_validated" ? "Holdout-validated method" : `${method.replaceAll("_", " ")} method`));
    const facts = node("dl", "gm-insights-valuation-facts");
    for (const [label, result] of valuationFacts(value)) {
      const wrapper = node("div");
      wrapper.append(node("dt", "", label), node("dd", "", result));
      facts.append(wrapper);
    }
    panel.append(facts);
    const playoffReason = typeof value.playoff_probability_unavailable_reason === "string"
      ? F.humanizeReasons([value.playoff_probability_unavailable_reason])[0]
      : null;
    if (playoffReason) {
      panel.append(node(
        "p",
        "gm-insights-unavailable",
        `Playoff change unavailable: ${playoffReason}.`
      ));
    }
    const captured = F.formatDate(value.source_bundle_captured_at || value.selected_bundle_captured_at);
    if (captured) panel.append(node("p", "gm-insights-valuation-date", `Model captured ${captured}`));
    return panel;
  }

  function eventCard(item) {
    const card = node("article", "gm-insights-evidence-item");
    const counterparties = F.plainText(item.counterparties);
    card.append(node("h5", "", counterparties ? `Completed trade with ${counterparties}` : "Completed trade"));
    const sent = item.sent?.length ? item.sent.join(", ") : "no resolved players";
    const received = item.received?.length ? item.received.join(", ") : "no resolved players";
    card.append(node("p", "gm-insights-evidence-assets", `Sent ${sent}; received ${received}.`));
    const when = F.formatDate(F.pick(item, "source_event_at", "completed_at", "occurred_at", "date", "captured_at"));
    const firstObserved = F.formatDate(item.first_observed_completed_at);
    const week = F.pick(item, "scoring_period", "week");
    const whenLabel = !when
      ? null
      : item.timestamp_basis === "espn_proposed_date"
        ? `ESPN proposal date ${when}`
        : `Completed ${when}`;
    const trace = [
      week ? `Scoring period ${week}` : null,
      whenLabel,
      firstObserved && item.timestamp_basis === "espn_proposed_date"
        ? `First seen completed ${firstObserved}`
        : null
    ].filter(Boolean).join(" · ");
    if (trace) card.append(node("p", "gm-insights-evidence-trace", trace));
    const valuation = item.valuation || {};
    const atTime = valuation.at_time || (Number.isFinite(valuation.power_delta) ? valuation : null);
    const current = valuation.current_revaluation || null;
    const comparison = valuation.comparison || {};
    const reasons = F.humanizeReasons(comparison.foresight_ineligibility_reasons);
    const atTimeUnavailable = reasons.length
      ? `Not valued: ${reasons.join("; ")}.`
      : "No strictly prior compatible weekly model was available, so the deal remains unvalued.";
    const compare = node("div", "gm-insights-valuation-comparison");
    compare.append(
      valuationPanel("At the time", atTime, atTimeUnavailable),
      valuationPanel(
        "Now, same package and pre-trade roster",
        current,
        "This exact historical package and roster context could not be revalued with the current model."
      )
    );
    card.append(compare);
    if (comparison.foresight_eligible === true) {
      const drift = F.signedMetric(comparison.relative_power_edge_drift);
      card.append(node(
        "p",
        "gm-insights-comparison-status is-eligible",
        `Eligible for the injury-screened aggregate${drift ? ` · relative edge drift ${drift}` : ""}.`
      ));
    } else {
      card.append(node(
        "p",
        "gm-insights-comparison-status is-excluded",
        `Not eligible for a foresight label: ${reasons.length ? reasons.join("; ") : "a complete, comparable history is unavailable"}.`
      ));
    }
    return card;
  }

  function render({container, items, visibleCount, pageSize, emptyMessage, onShowMore}) {
    if (!items.length) {
      container.replaceChildren(node("p", "gm-insights-unavailable", emptyMessage));
      return;
    }
    const shown = Math.min(visibleCount, items.length);
    const count = node("p", "gm-insights-evidence-limit", `Showing ${shown} of ${items.length} completed trades.`);
    count.id = "gmInsightsEvidenceCount";
    count.tabIndex = -1;
    count.setAttribute("role", "status");
    count.setAttribute("aria-live", "polite");
    const list = node("div", "gm-insights-evidence-list");
    list.id = "gmInsightsEvidenceList";
    for (const item of items.slice(0, shown)) list.append(eventCard(item));
    const fragment = document.createDocumentFragment();
    fragment.append(count, list);
    if (shown < items.length) {
      const increment = Math.min(pageSize, items.length - shown);
      const more = node("button", "gm-insights-show-more", `Show ${increment} more completed trades`);
      more.id = "gmInsightsShowMoreEvidence";
      more.type = "button";
      more.setAttribute("aria-controls", list.id);
      more.addEventListener("click", onShowMore);
      fragment.append(more);
    }
    container.replaceChildren(fragment);
  }

  return Object.freeze({render});
})();
