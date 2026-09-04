"use strict";

window.GmInsightsUi = (() => {
  const $ = id => document.getElementById(id);
  const F = window.GmInsightsFormat;
  const VALID_STATUSES = new Set([
    "not_collected", "unsupported_provider", "insufficient_sample", "partial", "ready"
  ]);
  const EVIDENCE_PAGE_SIZE = 10;
  const HISTORY_EMPTY = "Historical league activity has not been collected for this weekly model.";
  const HISTORY_ATTEMPT_NOTES = Object.freeze({
    activity_schema_unsupported: "ESPN's activity format was not recognized during this scan. Current roster analysis is still available; scan again after the collector is updated.",
    activity_unavailable: "ESPN activity could not be read during this scan. Current roster analysis is still available; try the weekly scan again.",
    canonicalization_failed: "The activity rows could not be matched safely to this league snapshot, so history-based conclusions are withheld.",
    history_processing_unavailable: "League activity could not be processed locally during this scan. Current roster analysis remains available.",
    store_unavailable: "Activity was captured, but the local history database could not save it. Current roster analysis remains available.",
    not_provided: "This bundle was imported without an activity-capture attempt, so history-based conclusions are unavailable."
  });
  let requestRevision = 0;
  let activeController = null;
  let activeBundleId = null;
  let insights = null;
  let selectedTeamId = null;
  let primaryTeamId = null;
  let useTradePartner = null;
  let evidenceVisibleCount = EVIDENCE_PAGE_SIZE;

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function confidenceBadge(value) {
    const status = F.confidenceStatus(value);
    return node("span", `gm-insights-badge is-${status.replaceAll("_", "-")}`, F.confidenceLabels[status]);
  }

  function statusBadge(text, modifier = "") {
    return node("span", `gm-insights-badge${modifier ? ` ${modifier}` : ""}`, text);
  }

  function setState(kind, message = "") {
    $("gmInsightsEmpty").classList.toggle("hidden", kind !== "empty");
    $("gmInsightsLoading").classList.toggle("hidden", kind !== "loading");
    $("gmInsightsContent").classList.toggle("hidden", kind !== "content");
    if (kind === "empty" && message) $("gmInsightsEmpty").textContent = message;
  }

  function clearRenderedContent() {
    for (const id of [
      "gmInsightsCoverage", "gmInsightsTableBody", "gmInsightsProfile",
      "gmInsightsDecisionSignals", "gmInsightsCompatibility", "gmInsightsTradeBehavior",
      "gmInsightsAcquisitionHabits", "gmInsightsRosterHabits", "gmInsightsLineupHabits",
      "gmInsightsHindsight", "gmInsightsTradeApproach", "gmInsightsEvidence"
    ]) $(id)?.replaceChildren();
    $("gmInsightsCoverageSummary").textContent = "";
    $("gmInsightsMethodNote").textContent = "";
  }

  function reset(message = "Select a ready week to inspect current roster fit and verified league history.") {
    requestRevision += 1;
    if (activeController) activeController.abort();
    activeController = null;
    activeBundleId = null;
    insights = null;
    selectedTeamId = null;
    primaryTeamId = null;
    useTradePartner = null;
    evidenceVisibleCount = EVIDENCE_PAGE_SIZE;
    clearRenderedContent();
    setState("empty", message);
  }

  function unavailableMessage(status) {
    if (status === "unsupported_provider") {
      return "Current roster fit or historical activity is not available for this league provider yet.";
    }
    return "This weekly model does not contain teams that can be compared.";
  }

  async function setBundle(bundle, {request, onError, primaryTeamId: ownTeamId, onUseTradePartner} = {}) {
    if (!bundle) {
      reset();
      return;
    }
    if (typeof request !== "function") throw new Error("General Manager Insights request function is unavailable.");
    const sameBundle = activeBundleId === bundle.bundle_id;
    const preferredTeamId = sameBundle
      ? selectedTeamId || ownTeamId || null
      : ownTeamId || null;
    const revision = ++requestRevision;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    activeBundleId = bundle.bundle_id;
    insights = null;
    evidenceVisibleCount = EVIDENCE_PAGE_SIZE;
    primaryTeamId = ownTeamId || null;
    useTradePartner = typeof onUseTradePartner === "function" ? onUseTradePartner : null;
    clearRenderedContent();
    setState("loading");
    try {
      const value = await request(
        `/api/bundles/${encodeURIComponent(bundle.bundle_id)}/gm-insights`,
        {signal: controller.signal}
      );
      if (controller.signal.aborted || revision !== requestRevision) return;
      if (!value || !VALID_STATUSES.has(value.status) || !Array.isArray(value.teams)) {
        throw new Error("General Manager Insights response is invalid.");
      }
      if (!value.teams.length) {
        setState("empty", unavailableMessage(value.status));
        return;
      }
      insights = value;
      selectedTeamId = value.teams.some(team => team.team_id === preferredTeamId)
        ? preferredTeamId
        : value.teams[0].team_id;
      render();
      setState("content");
    } catch (error) {
      if (controller.signal.aborted || revision !== requestRevision) return;
      setState("empty", "General Manager Insights could not load for this week.");
      if (typeof onError === "function") onError(error);
    } finally {
      if (revision === requestRevision) activeController = null;
    }
  }

  function selectedTeam() {
    return insights?.teams.find(team => team.team_id === selectedTeamId) || insights?.teams[0] || null;
  }

  function primaryTeam() {
    return insights?.teams.find(team => team.team_id === primaryTeamId) || null;
  }

  function historyAvailable(team) {
    return !["not_collected", "unsupported_provider"].includes(insights?.status)
      && team?.history_insights?.status !== "not_collected";
  }

  function compatibilityWithPrimary(team) {
    return F.compatibilityWithPrimary(insights?.teams, primaryTeamId, team?.team_id);
  }

  function compareMetric(left, right, getter) {
    const leftValue = F.estimate(getter(left));
    const rightValue = F.estimate(getter(right));
    if (!Number.isFinite(leftValue)) {
      return Number.isFinite(rightValue) ? 1 : F.collator.compare(left.team_name, right.team_name);
    }
    if (!Number.isFinite(rightValue)) return -1;
    return rightValue - leftValue || F.collator.compare(left.team_name, right.team_name);
  }

  function compareCurrentFit(left, right) {
    if (left.team_id === primaryTeamId) return -1;
    if (right.team_id === primaryTeamId) return 1;
    const leftFit = compatibilityWithPrimary(left);
    const rightFit = compatibilityWithPrimary(right);
    const leftRank = Number.isFinite(leftFit?.partner_rank) ? leftFit.partner_rank : Number.POSITIVE_INFINITY;
    const rightRank = Number.isFinite(rightFit?.partner_rank) ? rightFit.partner_rank : Number.POSITIVE_INFINITY;
    return leftRank - rightRank || F.collator.compare(left.team_name, right.team_name);
  }

  function orderedTeams() {
    const rows = [...insights.teams];
    const sort = $("gmInsightsSort").value;
    if (sort === "team_name") return rows.sort((a, b) => F.collator.compare(a.team_name, b.team_name));
    if (sort === "current_fit") return rows.sort(compareCurrentFit);
    const getters = {
      trade_accessibility: team => F.teamMetrics(team).tradeLikelihood,
      counterparty_value: team => F.teamMetrics(team).opportunityMetric,
      roster_activity: team => F.teamMetrics(team).rosterActivity,
      lineup_consistency: team => F.teamMetrics(team).lineupSignal
    };
    return rows.sort((left, right) => compareMetric(left, right, getters[sort] || (() => null)));
  }

  function renderTeamPicker() {
    const select = $("gmInsightsTeamSelect");
    select.replaceChildren();
    const teams = [...insights.teams].sort((a, b) => F.collator.compare(a.team_name, b.team_name));
    for (const team of teams) select.add(new Option(team.team_name, team.team_id));
    select.value = selectedTeamId;
  }

  function appendCell(row, main, detail = null, className = "") {
    const cell = node("td", className, main ?? "—");
    if (detail) cell.append(node("span", "gm-insights-muted", detail));
    row.append(cell);
    return cell;
  }

  function fitDetail(fit) {
    if (!fit || fit.is_primary_team) return fit?.is_primary_team ? "Comparison baseline" : "Choose your team above";
    const parts = [];
    if (Number.isFinite(fit.partner_rank)) parts.push(`ranked #${F.integerFormatter.format(fit.partner_rank)}`);
    if (Number.isFinite(fit.mutually_positive_swap_count)) {
      parts.push(`${F.integerFormatter.format(fit.mutually_positive_swap_count)} mutual-positive 1-for-1s`);
    }
    parts.push(fit.power_methodology_status === "holdout_validated" ? "holdout-validated power method" : "modeled power method");
    return parts.join(" · ");
  }

  function renderTable() {
    const fragment = document.createDocumentFragment();
    for (const team of orderedTeams()) {
      const metrics = F.teamMetrics(team);
      const fit = compatibilityWithPrimary(team);
      const selected = team.team_id === selectedTeamId;
      const row = document.createElement("tr");
      row.dataset.teamId = team.team_id;
      if (selected) row.classList.add("is-selected");
      const heading = document.createElement("th");
      heading.scope = "row";
      const button = node("button", "gm-insights-team-button", team.team_name);
      button.type = "button";
      button.dataset.teamId = team.team_id;
      button.setAttribute("aria-pressed", String(selected));
      heading.append(button);
      row.append(heading);
      appendCell(row, F.fitLabel(fit), fitDetail(fit));
      appendCell(
        row,
        F.plainText(metrics.accessibility.label) || "Historical activity unavailable",
        F.formatMetric(metrics.tradeLikelihood, "probability") || "Completed deals only; not offer acceptance"
      );
      appendCell(row, F.count(metrics.completedTrades), null, "gm-insights-number");
      appendCell(
        row,
        F.plainText(metrics.opportunity.label) || "Historical value unavailable",
        F.opportunityText(metrics.opportunityMetric) || "At-time value evidence required"
      );
      appendCell(
        row,
        F.formatMetric(metrics.rosterActivity, "per_10_weeks"),
        F.percentile(metrics.rosterActivity),
        "gm-insights-number"
      );
      appendCell(
        row,
        F.formatMetric(metrics.lineupSignal, "proportion"),
        lineupDetail(team),
        "gm-insights-number"
      );
      const evidence = appendCell(row, null);
      if (historyAvailable(team)) {
        evidence.replaceChildren(confidenceBadge(team.summary || metrics.tradeLikelihood));
        const sample = profileSampleText(team);
        if (sample) evidence.append(node("span", "gm-insights-muted", sample));
      } else {
        evidence.replaceChildren(statusBadge("History not collected", "is-unavailable"));
      }
      fragment.append(row);
    }
    $("gmInsightsTableBody").replaceChildren(fragment);
  }

  function coverageCounts(coverage) {
    const parts = [];
    const observed = insights.scope?.observed_scoring_periods;
    if (Number.isFinite(observed) && observed > 0) parts.push(quantityLabel(observed, "observed scoring period"));
    const trades = coverage.transactions?.completed_trades ?? F.pick(coverage?.counts, "completed_trades");
    if (Number.isFinite(trades)) parts.push(quantityLabel(trades, "completed trade"));
    const events = coverage.transactions?.completed_events;
    if (Number.isFinite(events)) parts.push(quantityLabel(events, "total roster event"));
    const valued = coverage.valuations?.valued_trades;
    if (Number.isFinite(valued)) parts.push(quantityLabel(valued, "valued trade"));
    return parts;
  }

  function quantityLabel(value, singular, plural = `${singular}s`) {
    return `${F.integerFormatter.format(value)} ${value === 1 ? singular : plural}`;
  }

  function renderCoverage() {
    const coverage = insights.coverage || {};
    const noHistory = insights.status === "not_collected";
    const unsupported = insights.status === "unsupported_provider";
    const limited = noHistory || unsupported || insights.status !== "ready" || [
      coverage.transactions, coverage.rosters, coverage.lineups
    ].some(section => typeof section?.status === "string" && section.status.includes("partial"));
    const container = $("gmInsightsCoverage");
    container.classList.toggle("is-limited", limited);
    let title = "Verified history is ready";
    let detail = "Sample sizes and confidence are shown with every historical estimate.";
    if (noHistory) {
      title = "Current roster compatibility is ready";
      detail = "Historical GM metrics are unavailable until a weekly scan captures completed trades, moves, rosters, and lineups.";
    } else if (unsupported) {
      title = "Provider history is unavailable";
      detail = "Current roster compatibility may still be used; no behavior or offer-acceptance claim is inferred.";
    } else if (limited) {
      title = "History coverage limits some conclusions";
    }
    const parts = coverageCounts(coverage);
    const through = F.formatDate(F.pick(coverage, "coverage_end", "captured_through") || insights.as_of);
    if (through) parts.push(`through ${through}`);
    container.replaceChildren(
      node("strong", "", title),
      node("span", "gm-insights-coverage-detail", parts.length ? `${detail} ${parts.join(" · ")}.` : detail)
    );
    const season = insights.scope?.season ? `${insights.scope.season} season` : "Current-season team profiles";
    $("gmInsightsCoverageSummary").textContent = `${season} · ${noHistory ? "current roster fit available; history not collected" : parts.join(" · ") || "verified local activity"}`;
  }

  function profileSampleText(team) {
    const metrics = F.teamMetrics(team);
    const completed = F.count(metrics.completedTrades);
    const valued = F.count(F.pick(metrics.value, "valued_trades", "valued_trade_count"));
    if (completed && valued) return `${valued} valued of ${completed} ${completed === "1" ? "trade" : "trades"}`;
    if (completed) return `${completed} completed ${completed === "1" ? "trade" : "trades"}`;
    return null;
  }

  function lineupDetail(team) {
    const {lineup} = F.teamSections(team);
    const snapshots = F.count(lineup.captured_lineup_snapshots);
    return snapshots ? `${snapshots} captured ${snapshots === "1" ? "snapshot" : "snapshots"}` : "Needs multiple lineup snapshots";
  }

  function renderProfileHeading(team) {
    const heading = node("div", "gm-insights-profile-heading");
    const text = document.createElement("div");
    text.append(node("p", "gm-insights-kicker", "TEAM / GM-UNIT PROFILE"), node("h3", "", team.team_name));
    text.append(node("p", "gm-insights-profile-scope", "This is a current-season team-slot profile; ownership continuity is not assumed."));
    const badges = node("div", "gm-insights-profile-badges");
    const method = team.roster_compatibility?.power_methodology_status;
    if (method) badges.append(statusBadge(method === "holdout_validated" ? "Holdout-validated current power method" : "Modeled current power method"));
    if (historyAvailable(team)) badges.append(confidenceBadge(team.summary || F.teamMetrics(team).tradeLikelihood));
    else badges.append(statusBadge("History not collected", "is-unavailable"));
    const sample = profileSampleText(team);
    if (sample) badges.append(statusBadge(sample));
    heading.append(text, badges);
    $("gmInsightsProfile").replaceChildren(heading);
  }

  function signalCard(key, number, title, answer, detail, boundary, badge = null) {
    const card = node("article", "gm-insights-decision-card");
    card.dataset.decisionSignal = key;
    card.append(
      node("span", "gm-insights-question-number", number),
      node("h4", "", title),
      node("strong", "gm-insights-decision-answer", answer),
      node("p", "gm-insights-decision-detail", detail),
      node("p", "gm-insights-decision-boundary", boundary)
    );
    if (badge) card.append(badge);
    return card;
  }

  function selectedFitSummary(team) {
    const fit = compatibilityWithPrimary(team);
    if (!primaryTeamId) return ["Choose your team", "Select your team above to compare current rosters."];
    if (fit?.is_primary_team) {
      const partners = team.roster_compatibility?.partners || [];
      const top = partners[0];
      return [
        `${F.integerFormatter.format(partners.length)} current partners ranked`,
        top ? `Top current match: ${top.partner_team_name} · ${F.fitLabel(top)}.` : "No eligible 1-for-1 comparison is available."
      ];
    }
    return [
      F.fitLabel(fit),
      fit ? `${fitDetail(fit)} with ${primaryTeam()?.team_name || "your team"}.` : "No current pair comparison is available."
    ];
  }

  function renderDecisionSignals(team) {
    const metrics = F.teamMetrics(team);
    const [fitAnswer, fitDetailText] = selectedFitSummary(team);
    const accessibilityAnswer = F.plainText(metrics.accessibility.label) || "Historical activity unavailable";
    const accessibilityValue = F.formatMetric(metrics.tradeLikelihood, "probability");
    const opportunityAnswer = F.plainText(metrics.opportunity.label) || "Historical value unavailable";
    const opportunityValue = F.opportunityText(metrics.opportunityMetric);
    $("gmInsightsDecisionSignals").replaceChildren(
      signalCard(
        "roster_compatibility", "1", "Current roster compatibility", fitAnswer, fitDetailText,
        "Uses current roster shape, position fit, and 1-for-1 power screens. It uses no manager behavior."
      ),
      signalCard(
        "deal_accessibility", "2", "Completed-deal accessibility", accessibilityAnswer,
        accessibilityValue
          ? `${accessibilityValue} modeled chance of another completed-trade week in the next two weeks.`
          : historyAvailable(team)
            ? "The raw completed-trade count is available, but no fully completed scoring periods are available to normalize activity."
            : HISTORY_EMPTY,
        "Uses completed-deal activity only. Rejected offers are unseen, so this is never an acceptance probability.",
        historyAvailable(team) ? confidenceBadge(metrics.tradeLikelihood) : statusBadge("Unavailable", "is-unavailable")
      ),
      signalCard(
        "counterparty_value_opportunity", "3", "Counterparty value opportunity", opportunityAnswer,
        opportunityValue
          ? `${opportunityValue} across valued completed deals.`
          : "This measure requires a complete player-only package and a strictly prior compatible weekly model.",
        "Uses only the negative of this team’s contemporaneous relative power edge. It does not use activity or current fit.",
        historyAvailable(team) ? confidenceBadge(metrics.opportunityMetric) : statusBadge("Unavailable", "is-unavailable")
      )
    );
  }

  function positionSummary(fit, teamName, partnerName) {
    const needs = F.positionNames(fit?.positional_fit?.team_needs_met_by_partner_surplus);
    const offers = F.positionNames(fit?.positional_fit?.partner_needs_met_by_team_surplus);
    return [
      needs.length
        ? `${teamName} could target ${needs.join(", ")} help from this roster.`
        : `${teamName} has no relative-need position matched by this roster’s surpluses.`,
      offers.length
        ? `${partnerName} could target ${offers.join(", ")} help from ${teamName}.`
        : `${partnerName} has no relative-need position matched by ${teamName}’s surpluses.`
    ];
  }

  function renderCompatibilityExample(container, example, teamName, partnerName) {
    if (!example) {
      container.append(node("p", "gm-insights-partner-example", "No mutual-positive 1-for-1 example passed the current power screen."));
      return;
    }
    const sent = F.plainText(example.team_sends?.player_name) || "unresolved player";
    const received = F.plainText(example.team_receives?.player_name) || "unresolved player";
    const teamDelta = F.signedMetric(example.team_power_delta) || "—";
    const partnerDelta = F.signedMetric(example.partner_power_delta) || "—";
    container.append(node(
      "p", "gm-insights-partner-example is-positive",
      `Best current 1-for-1 example: ${teamName} sends ${sent} and receives ${received}. Power change: ${teamDelta} for ${teamName}; ${partnerDelta} for ${partnerName}.`
    ));
  }

  function renderCompatibility(team) {
    const container = $("gmInsightsCompatibility");
    const compatibility = team.roster_compatibility;
    const partners = Array.isArray(compatibility?.partners)
      ? [...compatibility.partners].sort((a, b) => (a.partner_rank ?? 9999) - (b.partner_rank ?? 9999))
      : [];
    const intro = node("div", "gm-insights-compatibility-intro");
    intro.append(
      node("p", "", "Partners are ranked from current roster and position evidence only—past manager behavior is not an input."),
      statusBadge(compatibility?.power_methodology_status === "holdout_validated" ? "Holdout-validated current power screen" : "Modeled current power screen")
    );
    container.append(intro);
    if (!partners.length) {
      container.append(node("p", "gm-insights-unavailable", "No eligible current partner comparison is available."));
      return;
    }
    const list = node("ol", "gm-insights-partner-list");
    for (const fit of partners) {
      const item = node("li", "gm-insights-partner-card");
      item.dataset.partnerTeamId = fit.partner_team_id;
      if (fit.partner_team_id === primaryTeamId) item.classList.add("is-primary-match");
      const header = node("div", "gm-insights-partner-heading");
      const title = node("div");
      title.append(
        node("span", "gm-insights-partner-rank", `#${F.integerFormatter.format(fit.partner_rank || 0)}`),
        node("strong", "", fit.partner_team_name || "Unknown team")
      );
      const badges = node("div", "gm-insights-profile-badges");
      badges.append(statusBadge(F.fitLabel(fit)));
      badges.append(statusBadge(fit.power_methodology_status === "holdout_validated" ? "Holdout validated" : "Modeled"));
      if (fit.partner_team_id === primaryTeamId) badges.append(statusBadge("Your team", "is-moderate"));
      header.append(title, badges);
      const [receiving, offering] = positionSummary(fit, team.team_name, fit.partner_team_name);
      const counts = node("dl", "gm-insights-partner-counts");
      for (const [label, value] of [
        ["Mutual-positive 1-for-1s", fit.mutually_positive_swap_count],
        ["Mutual non-decreases", fit.mutually_nondecreasing_swap_count],
        ["Swaps screened", fit.evaluated_swap_count]
      ]) {
        const wrapper = node("div");
        wrapper.append(node("dt", "", label), node("dd", "", F.count(value) || "0"));
        counts.append(wrapper);
      }
      item.append(header, node("p", "gm-insights-position-match", receiving), node("p", "gm-insights-position-match", offering), counts);
      renderCompatibilityExample(item, fit.best_mutually_positive_example, team.team_name, fit.partner_team_name);
      list.append(item);
    }
    container.append(
      list,
      node(
        "p", "gm-insights-compatibility-caveat",
        compatibility.scope?.limitation || "This screen covers 1-for-1 trades only. A missing result does not mean a larger or differently shaped package cannot work."
      )
    );
  }

  function fact(label, value, source = null) {
    if (value === null || value === undefined || value === "") return null;
    return {label, value, detail: source ? F.metricDetail(source, "") : ""};
  }

  function renderFactsInto(container, rows) {
    const available = rows.filter(Boolean);
    if (!available.length) return;
    const list = node("dl", "gm-insights-facts");
    for (const row of available) {
      const wrapper = node("div", "gm-insights-fact");
      const description = node("dd", "", row.value);
      if (row.detail) description.append(node("span", "gm-insights-fact-note", row.detail));
      wrapper.append(node("dt", "", row.label), description);
      list.append(wrapper);
    }
    container.append(list);
  }

  function renderFacts(containerId, rows, emptyMessage) {
    const container = $(containerId);
    const available = rows.filter(Boolean);
    if (!available.length) {
      container.replaceChildren(node("p", "gm-insights-unavailable", emptyMessage));
      return;
    }
    container.replaceChildren();
    renderFactsInto(container, available);
  }

  function renderTradeFacts(team) {
    if (!historyAvailable(team)) {
      $("gmInsightsTradeBehavior").replaceChildren(node(
        "p", "gm-insights-unavailable",
        `${HISTORY_EMPTY} Completed trades never reveal rejected offers or an acceptance rate.`
      ));
      return;
    }
    const metrics = F.teamMetrics(team);
    const {style} = F.teamSections(team);
    renderFacts("gmInsightsTradeBehavior", [
      fact("Completed trades", F.count(metrics.completedTrades)),
      fact("Completed deals per 10 weeks", F.formatMetric(metrics.tradeRate, "per_10_weeks"), metrics.tradeRate),
      fact("Completed-trade week in next 2 weeks", F.formatMetric(metrics.tradeLikelihood, "probability"), metrics.tradeLikelihood),
      fact("Unique completed partners", F.count(F.pick(metrics.activity, "unique_partners", "partner_count"))),
      fact("Most recent completed trade", F.formatDate(F.pick(metrics.activity, "latest_trade_at", "last_trade_at"))),
      fact("Completed-deal value style", F.valueStyle(team), metrics.valueEdge),
      fact("At-time relative power edge", F.valueEdgeText(metrics.valueEdge), metrics.valueEdge),
      fact("Counterparty value opportunity", F.opportunityText(metrics.opportunityMetric), metrics.opportunityMetric),
      fact("Both sides gained power", F.formatMetric(F.pick(metrics.value, "all_participants_benefit_rate", "win_win_rate"), "proportion")),
      fact("Most common package shape", F.plainText(F.pick(style, "package_shape", "preferred_package_shape"))),
      fact("Average package sent / received", Number.isFinite(style.average_sent) && Number.isFinite(style.average_received)
        ? `${F.numberFormatter.format(style.average_sent)} / ${F.numberFormatter.format(style.average_received)} players`
        : null),
      fact("Positions most often received", F.rankedRowsText(style.positions_received, "position")),
      fact("Frequent completed partners", F.rankedRowsText(style.frequent_partners, "team_name", "completed_trades"))
    ], `${HISTORY_EMPTY} Completed trades never reveal rejected offers or an acceptance rate.`);
  }

  function renderAcquisitionFacts(team) {
    const {acquisition: section} = F.teamSections(team);
    renderFacts("gmInsightsAcquisitionHabits", [
      fact("Waiver awards", F.count(F.pick(section, "waiver_awards", "waiver_claims_won"))),
      fact("Free-agent additions", F.count(F.pick(section, "free_agent_additions", "free_agent_adds"))),
      fact("Drops", F.count(section.drops)),
      fact("Acquisitions per 10 weeks", F.formatMetric(F.pick(section, "acquisitions_per_10_weeks", "adds_per_10_weeks"), "per_10_weeks"), F.pick(section, "acquisitions_per_10_weeks", "adds_per_10_weeks")),
      fact("Share acquired through waivers", F.formatMetric(section.waiver_share, "proportion")),
      fact("Roster turnover per player-week", F.formatMetric(section.roster_turnover, "proportion")),
      fact("Retained at next snapshot", F.formatMetric(section.next_snapshot_retention?.retained_rate, "proportion")),
      fact("Verified short-term streams", F.count(section.next_snapshot_retention?.verified_streams)),
      fact("Positions most often acquired", F.rankedRowsText(section.positions_acquired, "position"))
    ], "Waiver and free-agent habits need verified transaction types and observed weeks.");
  }

  function renderRosterFacts(team) {
    const {roster: section} = F.teamSections(team);
    renderFacts("gmInsightsRosterHabits", [
      fact("Active roster fullness", F.formatMetric(section.active_fullness, "proportion")),
      fact("Active players / roster cap", Number.isFinite(section.active_players) && Number.isFinite(section.roster_cap)
        ? `${F.integerFormatter.format(section.active_players)} / ${F.integerFormatter.format(section.roster_cap)}`
        : null),
      fact("Captured starters", F.count(section.captured_starters)),
      fact("Captured bench players", F.count(section.captured_bench)),
      fact("Captured reserve players", F.count(section.captured_reserve)),
      fact("Projected-value concentration", Number.isFinite(section.projected_value_concentration_gini)
        ? `${F.formatMetric(section.projected_value_concentration_gini)} · higher means more top-heavy`
        : null),
      fact("Position counts", F.rankedRowsText(section.position_counts, "position", "players"))
    ], "Historical roster-construction habits need multiple verified weekly roster states.");
  }

  function renderLineupFacts(team) {
    const {lineup: section, roster} = F.teamSections(team);
    renderFacts("gmInsightsLineupHabits", [
      fact("Captured lineup snapshots", F.count(F.pick(section, "captured_lineup_snapshots", "verified_lineup_weeks"))),
      fact("Starter continuity", F.formatMetric(section.starter_continuity, "proportion"), section.starter_continuity),
      fact("Average starter changes", F.formatMetric(F.pick(section, "average_starter_changes", "lineup_changes_per_week"))),
      fact("Current captured starters", F.count(roster.captured_starters))
    ], "Lineup habits need multiple captured lineup snapshots; a current roster alone is not enough.");
  }

  function excludedReasonText(value) {
    const rows = value && typeof value === "object" ? Object.entries(value) : [];
    return rows.length
      ? rows.map(([reason, amount]) => `${F.humanizeReason(reason)} (${F.integerFormatter.format(amount)})`).join("; ")
      : null;
  }

  function renderHindsight(team) {
    const section = team.hindsight_value_drift || {};
    const metric = section.relative_power_edge_drift;
    if (!historyAvailable(team) || !metric) {
      $("gmInsightsHindsight").replaceChildren(node("p", "gm-insights-unavailable", "Injury-screened hindsight needs valued trades plus complete health history."));
      return;
    }
    const eligible = Number.isFinite(section.foresight_eligible_trades) ? section.foresight_eligible_trades : 0;
    const available = section.status === "available" && Number.isFinite(F.estimate(metric));
    const lead = available
      ? F.plainText(section.label) || "Injury-screened hindsight drift"
      : "Foresight classification withheld";
    const container = node("div", "gm-insights-hindsight-content");
    container.append(node("p", "gm-insights-guidance-lead", lead));
    if (available && F.plainText(section.plain_language_alias)) {
      container.append(statusBadge(F.plainText(section.plain_language_alias), "is-moderate"));
    }
    renderFactsInto(container, [
      fact("Relative edge drift", F.formatMetric(metric), metric),
      fact("At-time mean edge", F.signedMetric(section.then_relative_power_edge_mean)),
      fact("Current mean edge", F.signedMetric(section.current_relative_power_edge_mean)),
      fact("Injury-screened comparisons", F.count(eligible)),
      fact("Raw current revaluations", F.count(section.raw_current_revalued_trades)),
      fact("Excluded comparisons", excludedReasonText(section.excluded_reasons))
    ]);
    container.append(confidenceBadge(metric));
    container.append(node(
      "p", "gm-insights-caveats",
      "This compares the same package in the original pre-trade rosters using today’s values. It is hindsight drift—not proof of skill, causality, or what was knowable then."
    ));
    $("gmInsightsHindsight").replaceChildren(container);
  }

  function guidanceText(value, ...keys) {
    return F.plainText(F.pick(value, ...keys));
  }

  function renderGuidance(team) {
    const guidance = team.proposal_guidance || {};
    const content = document.createDocumentFragment();
    if (!historyAvailable(team)) {
      content.append(
        node("p", "gm-insights-guidance-lead", "Start from current roster compatibility"),
        node("p", "gm-insights-guidance-copy", "History-based proposal guidance is unavailable. Use the ranked current partners and 1-for-1 examples above, then treat the response as new evidence.")
      );
      $("gmInsightsTradeApproach").replaceChildren(content);
      return;
    }
    const headline = guidanceText(guidance, "headline", "title");
    const recommendation = guidanceText(guidance, "recommendation", "summary");
    const actions = Array.isArray(guidance.actions) ? guidance.actions.filter(item => typeof item === "string").slice(0, 6) : [];
    const support = Array.isArray(guidance.supporting_evidence) ? guidance.supporting_evidence.filter(item => typeof item === "string").slice(0, 5) : [];
    const counter = Array.isArray(guidance.counterevidence) ? guidance.counterevidence.filter(item => typeof item === "string").slice(0, 5) : [];
    const caveats = F.plainText(guidance.caveats || guidance.limitations);
    content.append(node("p", "gm-insights-guidance-lead", headline || "Start with an evidence-balanced offer"));
    if (recommendation) content.append(node("p", "gm-insights-guidance-copy", recommendation));
    if (actions.length) {
      const list = node("ol", "gm-insights-action-list");
      for (const action of actions) list.append(node("li", "", action));
      content.append(list);
    }
    for (const [label, rows] of [["Supporting evidence", support], ["Counterevidence", counter]]) {
      if (!rows.length) continue;
      const group = node("div", "gm-insights-tendency");
      group.append(node("strong", "", label));
      for (const text of rows) group.append(node("p", "", text));
      content.append(group);
    }
    content.append(confidenceBadge(guidance));
    if (caveats) content.append(node("p", "gm-insights-caveats", `Keep in mind: ${caveats}`));
    $("gmInsightsTradeApproach").replaceChildren(content);
  }

  function renderEvidence(team) {
    const explicit = team.evidence_preview || team.evidence || team.summary?.evidence_preview;
    const items = Array.isArray(explicit) ? explicit : [];
    GmInsightsEvidenceUi.render({
      container: $("gmInsightsEvidence"), items, visibleCount: evidenceVisibleCount,
      pageSize: EVIDENCE_PAGE_SIZE,
      emptyMessage: historyAvailable(team)
        ? "No completed-trade event evidence is available for this profile."
        : HISTORY_EMPTY,
      onShowMore: () => {
        evidenceVisibleCount += EVIDENCE_PAGE_SIZE;
        renderEvidence(team);
        ($("gmInsightsShowMoreEvidence") || $("gmInsightsEvidenceCount"))?.focus();
      }
    });
  }

  function renderMethodNote() {
    const limitations = Array.isArray(insights.methodology?.limitations)
      ? insights.methodology.limitations.filter(value => typeof value === "string").join(" ")
      : F.plainText(insights.methodology?.limitations);
    const readiness = insights.data_readiness || {};
    const activity = readiness.capabilities?.completed_deal_activity || {};
    const attemptReason = readiness.collection_attempt?.reason_code;
    const historyDataNote = HISTORY_ATTEMPT_NOTES[attemptReason] || (readiness.store_status === "unavailable"
      ? "The local history store could not be read; current roster compatibility remains available, while history-based conclusions are withheld."
      : activity.status === "not_ready"
        ? "No bundle-bound transaction capture is available yet; current roster compatibility remains available."
        : activity.status === "partial"
          ? "Transaction history is partial or stale, so normalized behavioral conclusions are withheld."
          : "The transaction ledger is complete and fresh for this weekly model; individual historical valuations still pass their own ordering and prior-engine gates.");
    $("gmInsightsMethodNote").textContent = [
      "The three decision questions above remain independent: current roster fit uses no behavior, completed-deal accessibility is not offer acceptance, and counterparty value opportunity is only the reversed at-time relative edge.",
      "Historical profiles describe completed activity for a team slot, not rejected offers, intent, or a private person.",
      historyDataNote,
      limitations
    ].filter(Boolean).join(" ");
  }

  function updatePartnerButton() {
    const button = $("gmInsightsUseTradePartner");
    const team = selectedTeam();
    const ownTeam = team && team.team_id === primaryTeamId;
    button.disabled = !team || ownTeam || typeof useTradePartner !== "function";
    button.textContent = ownTeam
      ? "This is your selected team"
      : team
        ? `Search trades with ${team.team_name}`
        : "Search trades with this team";
  }

  function renderProfile() {
    const team = selectedTeam();
    if (!team) return;
    renderProfileHeading(team);
    renderDecisionSignals(team);
    renderCompatibility(team);
    renderTradeFacts(team);
    renderAcquisitionFacts(team);
    renderRosterFacts(team);
    renderLineupFacts(team);
    renderHindsight(team);
    renderGuidance(team);
    renderEvidence(team);
    updatePartnerButton();
  }

  function render() {
    renderCoverage();
    renderTeamPicker();
    renderTable();
    renderProfile();
    renderMethodNote();
  }

  function selectTeam(teamId, focus = false) {
    if (!insights?.teams.some(team => team.team_id === teamId)) return;
    if (teamId !== selectedTeamId) evidenceVisibleCount = EVIDENCE_PAGE_SIZE;
    selectedTeamId = teamId;
    $("gmInsightsTeamSelect").value = teamId;
    renderTable();
    renderProfile();
    window.TradeTimingUi?.setPartnerTeam(teamId);
    if (focus) {
      const buttons = [...$("gmInsightsTableBody").querySelectorAll(".gm-insights-team-button")];
      buttons.find(button => button.dataset.teamId === teamId)?.focus();
    }
  }

  function setPrimaryTeam(teamId) {
    primaryTeamId = typeof teamId === "string" && teamId ? teamId : null;
    if (!insights) return;
    renderTable();
    renderProfile();
  }

  $("gmInsightsTeamSelect")?.addEventListener("change", event => selectTeam(event.target.value));
  $("gmInsightsSort")?.addEventListener("change", () => { if (insights) renderTable(); });
  $("gmInsightsTableBody")?.addEventListener("click", event => {
    const button = event.target.closest(".gm-insights-team-button");
    if (button) selectTeam(button.dataset.teamId);
  });
  $("gmInsightsTableBody")?.addEventListener("keydown", event => {
    if (!event.target.matches(".gm-insights-team-button")) return;
    const buttons = [...$("gmInsightsTableBody").querySelectorAll(".gm-insights-team-button")];
    const current = buttons.indexOf(event.target);
    let target = null;
    if (event.key === "ArrowDown") target = buttons[Math.min(buttons.length - 1, current + 1)];
    else if (event.key === "ArrowUp") target = buttons[Math.max(0, current - 1)];
    else if (event.key === "Home") target = buttons[0];
    else if (event.key === "End") target = buttons.at(-1);
    if (!target || target === event.target) return;
    event.preventDefault();
    selectTeam(target.dataset.teamId, true);
  });
  $("gmInsightsTableBody")?.closest(".gm-insights-table-wrap")?.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key) || event.target.matches("button, select, input")) return;
    const container = event.currentTarget;
    if (container.scrollWidth <= container.clientWidth) return;
    container.scrollLeft += (event.key === "ArrowRight" ? 1 : -1) * Math.max(48, container.clientWidth * .6);
    event.preventDefault();
  });
  $("gmInsightsUseTradePartner")?.addEventListener("click", () => {
    const team = selectedTeam();
    if (team && team.team_id !== primaryTeamId && typeof useTradePartner === "function") {
      useTradePartner(team.team_id);
    }
  });

  return Object.freeze({reset, setBundle, setPrimaryTeam});
})();
