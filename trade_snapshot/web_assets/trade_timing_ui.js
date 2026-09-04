"use strict";

window.TradeTimingUi = (() => {
  const $ = id => document.getElementById(id);
  const percentFormatter = new Intl.NumberFormat(undefined, {
    style: "percent", minimumFractionDigits: 0, maximumFractionDigits: 1
  });
  const numberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
  const SVG_NS = "http://www.w3.org/2000/svg";
  const HISTORY_ATTEMPT_NOTES = Object.freeze({
    activity_schema_unsupported: "ESPN's activity format was not recognized during this scan; timing uses current forward-looking data only.",
    activity_unavailable: "ESPN activity could not be read during this scan; timing uses current forward-looking data only.",
    canonicalization_failed: "Captured activity could not be matched safely to this league snapshot; behavioral timing is withheld.",
    history_processing_unavailable: "League activity could not be processed locally during this scan; forward-looking timing still runs.",
    store_unavailable: "Captured activity could not be saved to the local history database; forward-looking timing still runs.",
    not_provided: "This bundle was imported without an activity-capture attempt; forward-looking timing still runs."
  });
  let requestRevision = 0;
  let activeController = null;
  let activeBundle = null;
  let request = null;
  let primaryTeamId = null;
  let timing = null;
  let selectedPartnerId = null;

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function svgNode(tag, attributes = {}) {
    const element = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
    return element;
  }

  function finite(value) {
    const number = typeof value === "string" && value.trim() ? Number(value) : value;
    return Number.isFinite(number) ? number : null;
  }

  function metricValue(value) {
    const direct = finite(value);
    if (direct !== null) return direct;
    if (!value || typeof value !== "object") return null;
    for (const key of ["estimate", "value", "probability", "rate", "mean", "league_percentile"]) {
      const result = finite(value[key]);
      if (result !== null) return result;
    }
    return null;
  }

  function probability(value) {
    const number = metricValue(value);
    if (number === null) return "—";
    const proportion = Math.abs(number) > 1.000001 ? number / 100 : number;
    if (proportion > 0 && proportion < .001) return "<0.1%";
    return percentFormatter.format(proportion);
  }

  function percentagePoints(value, signed = true) {
    const number = metricValue(value);
    if (number === null) return "—";
    const points = Math.abs(number) <= 1.000001 ? number * 100 : number;
    const prefix = signed && points > 0 ? "+" : "";
    return `${prefix}${numberFormatter.format(points)} pp`;
  }

  function signedNumber(value, suffix = "") {
    const number = metricValue(value);
    if (number === null) return "—";
    return `${number > 0 ? "+" : ""}${numberFormatter.format(number)}${suffix}`;
  }

  function plainText(value) {
    if (typeof value === "string") return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    return "";
  }

  function humanize(value) {
    const text = plainText(value).replaceAll("_", " ").trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
  }

  function weekLabel(value) {
    const week = finite(value);
    return week === null ? "a future week" : `Week ${numberFormatter.format(week)}`;
  }

  function array(value) { return Array.isArray(value) ? value : value ? [value] : []; }

  function setState(kind, message = "") {
    $("tradeTimingLab").setAttribute("aria-busy", String(kind === "loading"));
    $("tradeTimingEmpty").classList.toggle("hidden", kind !== "empty");
    $("tradeTimingLoading").classList.toggle("hidden", kind !== "loading");
    $("tradeTimingError").classList.toggle("hidden", kind !== "error");
    $("tradeTimingContent").classList.toggle("hidden", kind !== "content");
    if (kind === "empty" && message) $("tradeTimingEmpty").textContent = message;
    if (kind === "error") $("tradeTimingError").textContent = message;
  }

  function clear() {
    for (const id of [
      "tradeTimingPartnerOverview", "tradeTimingRecommendations", "tradeTimingWindows",
      "tradeTimingTrajectory", "tradeTimingDealTiming", "tradeTimingMethodology",
      "tradeTimingPartnerBoard"
    ]) $(id)?.replaceChildren();
    $("tradeTimingSummary").textContent = "";
    $("tradeTimingTrajectoryDirection").textContent = "";
    $("tradeTimingPartnerSelect")?.replaceChildren();
  }

  function abortActiveRequest() {
    requestRevision += 1;
    if (activeController) activeController.abort();
    activeController = null;
  }

  function reset(message = "Select a ready week and your team to calculate trade windows.") {
    abortActiveRequest();
    activeBundle = null;
    request = null;
    primaryTeamId = null;
    timing = null;
    selectedPartnerId = null;
    clear();
    setState("empty", message);
  }

  function planFor(partner) {
    const recommendation = partner?.recommendation || {};
    return recommendation.default_plan || recommendation.conditional_watch_plan || null;
  }

  function pressureFor(partner) {
    const windows = array(partner?.vulnerable_windows);
    return Math.max(-1, ...windows.map(item => metricValue(item?.pressure_percentile) ?? -1));
  }

  function orderedPartners() {
    return [...array(timing?.partner_plans)].sort((left, right) => {
      const leftRank = finite(left?.timing_partner_rank);
      const rightRank = finite(right?.timing_partner_rank);
      if (leftRank !== null && rightRank !== null && leftRank !== rightRank) return leftRank - rightRank;
      const planTier = partner => partner?.recommendation?.default_plan
        ? 2
        : partner?.recommendation?.conditional_watch_plan ? 1 : 0;
      return planTier(right) - planTier(left)
        || pressureFor(right) - pressureFor(left)
        || String(left.partner_team_name || "").localeCompare(String(right.partner_team_name || ""));
    });
  }

  function selectedPartner() {
    return array(timing?.partner_plans).find(item => item.partner_team_id === selectedPartnerId)
      || orderedPartners()[0]
      || null;
  }

  async function load() {
    if (!activeBundle || !primaryTeamId || typeof request !== "function") {
      abortActiveRequest();
      timing = null;
      clear();
      setState("empty", primaryTeamId
        ? "Select a ready week to calculate trade windows."
        : "Choose your team in Trade Search to calculate trade windows.");
      return;
    }
    const revision = ++requestRevision;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    timing = null;
    clear();
    setState("loading");
    try {
      const bundleId = encodeURIComponent(activeBundle.bundle_id);
      const teamId = encodeURIComponent(primaryTeamId);
      const value = await request(
        `/api/bundles/${bundleId}/trade-timing?primaryTeamId=${teamId}`,
        {signal: controller.signal}
      );
      if (controller.signal.aborted || revision !== requestRevision) return;
      const validPartners = Array.isArray(value?.partner_plans)
        && value.partner_plans.every(item => item && plainText(item.partner_team_id) && plainText(item.partner_team_name));
      const responseTeamId = plainText(value?.primary_team_id);
      if (
        !value
        || finite(value.schema_version) !== 2
        || !validPartners
        || (responseTeamId && responseTeamId !== primaryTeamId)
      ) {
        throw new Error("Trade Timing response is invalid.");
      }
      timing = value;
      const ids = new Set(value.partner_plans.map(item => item.partner_team_id));
      selectedPartnerId = ids.has(selectedPartnerId) ? selectedPartnerId : orderedPartners()[0]?.partner_team_id || null;
      if (!value.partner_plans.length) {
        setState("empty", emptyMessage(value.status));
        return;
      }
      setState("content");
      render();
    } catch (error) {
      if (controller.signal.aborted || revision !== requestRevision) return;
      console.error("Trade Timing Lab failed to load", error);
      timing = null;
      clear();
      setState("error", "Trade Timing Lab could not calculate this week. Your other league analysis is still available.");
    } finally {
      if (revision === requestRevision) activeController = null;
    }
  }

  function emptyMessage(status) {
    if (status === "season_complete") return "The regular season is complete, so there are no remaining trade windows to simulate.";
    if (status === "unsupported_provider") return "Trade timing is not available for this league provider yet.";
    if (status === "insufficient_projection_data") return "There are not enough future weekly projections to simulate trade windows.";
    return "No opponent timing plans are available for this weekly model.";
  }

  async function setBundle(bundle, {apiRequest, request: requestAlias, primaryTeamId: ownTeamId} = {}) {
    if (!bundle) {
      reset();
      return;
    }
    const requestFunction = apiRequest || requestAlias;
    if (typeof requestFunction !== "function") throw new Error("Trade Timing request function is unavailable.");
    activeBundle = bundle;
    request = requestFunction;
    primaryTeamId = plainText(ownTeamId) || null;
    await load();
  }

  async function setPrimaryTeam(teamId) {
    const nextTeamId = plainText(teamId) || null;
    if (nextTeamId === primaryTeamId) return;
    primaryTeamId = nextTeamId;
    selectedPartnerId = null;
    await load();
  }

  function setPartnerTeam(teamId) {
    const nextPartnerId = plainText(teamId);
    if (!nextPartnerId || nextPartnerId === primaryTeamId) return;
    selectedPartnerId = nextPartnerId;
    if (timing?.partner_plans.some(item => item.partner_team_id === nextPartnerId)) renderPartner();
  }

  function statusPill(text, modifier = "") {
    return node("span", `trade-timing-pill${modifier ? ` is-${modifier}` : ""}`, text);
  }

  function renderPartnerPicker() {
    const select = $("tradeTimingPartnerSelect");
    select.replaceChildren();
    for (const partner of orderedPartners()) {
      const recommendation = partner.recommendation || {};
      const suffix = recommendation.default_plan
        ? " · current candidate"
        : recommendation.conditional_watch_plan
          ? " · conditional watch"
          : " · pressure watch";
      select.add(new Option(`${partner.partner_team_name}${suffix}`, partner.partner_team_id));
    }
    select.value = selectedPartnerId;
  }

  function renderSummary() {
    const count = timing.partner_plans.length;
    const ready = timing.partner_plans.filter(partner => planFor(partner)).length;
    const scenarioCount = metricValue(
      timing.scenario_sampling?.scenario_count
      ?? timing.scenario_sampling?.scenarios_used
      ?? timing.scenario_sampling?.count
    );
    $("tradeTimingSummary").textContent = [
      `${timing.primary_team_name || "Your team"} vs. ${count} opponent${count === 1 ? "" : "s"}`,
      `${ready} with a simulated trade plan`,
      scenarioCount === null ? "" : `${numberFormatter.format(scenarioCount)} shared season scenarios`
    ].filter(Boolean).join(" · ");
  }

  function renderPartnerBoard() {
    const board = document.createDocumentFragment();
    for (const partner of orderedPartners()) {
      const recommendation = partner.recommendation || {};
      const plan = recommendation.default_plan || recommendation.conditional_watch_plan;
      const topWindow = array(partner.vulnerable_windows)[0];
      const button = node("button", "trade-timing-partner-button");
      button.type = "button";
      button.dataset.partnerTeamId = partner.partner_team_id;
      button.setAttribute("aria-controls", "tradeTimingPartnerOverview");
      button.setAttribute("aria-pressed", String(partner.partner_team_id === selectedPartnerId));
      if (partner.partner_team_id === selectedPartnerId) button.classList.add("is-selected");
      const action = recommendation.default_plan
        ? `Candidate ${weekLabel(plan.effective_week)}`
        : recommendation.conditional_watch_plan
          ? `Watch ${weekLabel(plan.effective_week)}`
          : topWindow
            ? `Pressure ${weekLabel(topWindow.effective_week)}`
            : "No window";
      const pressure = topWindow ? probability(topWindow.pressure_percentile) : "—";
      button.append(
        node("strong", "", partner.partner_team_name),
        node("span", "", action),
        node("small", "", `Pressure percentile ${pressure}`)
      );
      board.append(button);
    }
    $("tradeTimingPartnerBoard").replaceChildren(board);
  }

  function recommendationLabel(partner) {
    const recommendation = partner.recommendation || {};
    if (recommendation.default_plan) {
      return ["Verify deadline + current health", "watch"];
    }
    if (recommendation.conditional_watch_plan) return ["Conditional watch", "watch"];
    return [humanize(recommendation.status) || "No mutual-positive trade found", "quiet"];
  }

  function renderPartnerOverview(partner) {
    const container = node("div", "trade-timing-overview-card");
    const heading = node("div", "trade-timing-overview-heading");
    const title = node("div");
    title.append(
      node("p", "trade-timing-overline", "TIMING PLAN AGAINST"),
      node("h3", "", partner.partner_team_name || "Selected opponent")
    );
    const [label, status] = recommendationLabel(partner);
    heading.append(title, statusPill(label, status));
    const trajectory = partner.record_trajectory || {};
    const direction = humanize(trajectory.direction) || "Trajectory unavailable";
    const topWindow = array(partner.vulnerable_windows)[0];
    const healthStatus = timing?.current_health_screen?.status;
    const healthText = healthStatus === "complete_and_fresh"
      ? "Complete at bundle capture — recheck before proposing"
      : `${humanize(healthStatus) || "Unavailable"} — verify before proposing`;
    const facts = node("dl", "trade-timing-overview-facts");
    for (const [term, description] of [
      ["Projected direction", direction],
      ["Highest-pressure window", topWindow ? weekLabel(topWindow.effective_week) : "No projected window"],
      ["Pressure scenario occurs", topWindow ? probability(topWindow.trigger_probability) : "—"],
      ["Current health screen", healthText],
      ["Trade deadline", "Not captured — verify"],
      ["Power method", humanize(timing?.methodology?.power_methodology_status) || "Unavailable"]
    ]) {
      const group = node("div");
      group.append(node("dt", "", term), node("dd", "", description));
      facts.append(group);
    }
    container.append(heading, facts);
    $("tradeTimingPartnerOverview").replaceChildren(container);
  }

  function playerName(player) {
    return plainText(player?.player_name) || plainText(player?.name) || plainText(player) || "Unknown player";
  }

  function playerMeta(player) {
    if (!player || typeof player !== "object") return "";
    return [player.position, player.nfl_team || player.pro_team || player.team]
      .map(plainText).filter(Boolean).join(" · ");
  }

  function playerPackage(players, label, modifier) {
    const section = node("div", `trade-timing-package is-${modifier}`);
    section.append(node("p", "trade-timing-package-label", label));
    const list = node("ul", "trade-timing-player-list");
    for (const player of array(players)) {
      const item = node("li");
      const text = node("span");
      text.append(node("strong", "", playerName(player)));
      const meta = playerMeta(player);
      if (meta) text.append(node("small", "", meta));
      item.append(text);
      list.append(item);
    }
    if (!list.children.length) list.append(node("li", "is-empty", "Package unavailable"));
    section.append(list);
    return section;
  }

  function impactFact(label, value, modifier = "") {
    const group = node("div", modifier ? `is-${modifier}` : "");
    group.append(node("dt", "", label), node("dd", "", value));
    return group;
  }

  function marketText(pattern) {
    if (!pattern) return "No projection-shape signal is available for this package.";
    if (typeof pattern === "string") return pattern;
    for (const key of ["summary", "interpretation", "plain_language", "label", "classification", "primary_perspective", "description"]) {
      const text = plainText(pattern[key]);
      if (text) return text;
    }
    return "The player projections create a high/low timing signal for this package.";
  }

  function renderPlan(plan, {label, conditional = false, compact = false} = {}) {
    const article = node("article", `trade-timing-plan${conditional ? " is-conditional" : ""}${compact ? " is-compact" : ""}`);
    const heading = node("div", "trade-timing-plan-heading");
    const titles = node("div");
    titles.append(
      node("p", "trade-timing-overline", label || "SIMULATED TRADE PLAN"),
      node("h3", "", `${conditional ? "Watch before" : "Consider before"} ${weekLabel(plan.effective_week)}`)
    );
    heading.append(titles, statusPill(
      conditional ? "Wait for trigger" : "Verification required",
      "watch"
    ));
    article.append(heading);
    const trigger = plainText(plan.trigger?.label) || plainText(plan.trigger) || (conditional
      ? "Use only if the projected pressure condition occurs."
      : "The current model supports proposing this package in this window.");
    article.append(node("p", "trade-timing-trigger", trigger));

    const trade = node("div", "trade-timing-trade");
    trade.append(
      playerPackage(plan.primary_sends, "You send", "send"),
      node("span", "trade-timing-swap-mark", "⇄"),
      playerPackage(plan.primary_receives, "You receive", "receive")
    );
    article.append(trade);

    const impacts = node("dl", "trade-timing-impact-grid");
    impacts.append(
      impactFact("Your playoff change", percentagePoints(plan.primary_playoff_probability_delta), "primary"),
      impactFact("Their playoff change", percentagePoints(plan.partner_playoff_probability_delta), "partner"),
      impactFact("Your expected wins", signedNumber(plan.primary_expected_wins_delta), "primary"),
      impactFact("Their expected wins", signedNumber(plan.partner_expected_wins_delta), "partner")
    );
    if (!compact) {
      impacts.append(
        impactFact("Your displayed power", signedNumber(plan.primary_display_power_delta ?? plan.primary_power_delta, " power")),
        impactFact("Their displayed power", signedNumber(plan.partner_display_power_delta ?? plan.partner_power_delta, " power"))
      );
    }
    article.append(impacts);

    const scenarioCount = metricValue(plan.scenario_count);
    const minimumGain = metricValue(plan.minimum_playoff_probability_gain_each_team);
    article.append(node(
      "p",
      "trade-timing-estimate-boundary",
      [
        "Paired Monte Carlo point estimate—not a confidence-certified improvement.",
        minimumGain === null ? "" : `Each team clears a ${percentagePoints(minimumGain, false)} materiality floor.`,
        scenarioCount === null ? "" : `${numberFormatter.format(scenarioCount)} scenario${scenarioCount === 1 ? "" : "s"} in this comparison.`
      ].filter(Boolean).join(" ")
    ));

    if (!compact) {
      const market = node("div", "trade-timing-market-note");
      market.append(
        node("strong", "", "Projection-shape signal"),
        node("p", "", marketText(plan.market_pattern)),
        node("small", "", "“High” and “low” describe captured weekly projections—not market price, manager intent, or a guarantee.")
      );
      article.append(market);
      const delayCost = metricValue(plan.delay_cost_primary);
      if (delayCost !== null) {
        const delayText = delayCost > 0
          ? `Waiting for this window costs your modeled playoff benefit ${percentagePoints(Math.abs(delayCost), false)} versus acting now.`
          : delayCost < 0
            ? `Waiting for this window improves your modeled playoff benefit ${percentagePoints(Math.abs(delayCost), false)} versus acting now.`
            : "The model finds no playoff-benefit difference between acting now and this window.";
        article.append(node("p", "trade-timing-delay", delayText));
      }
      const reasons = array(plan.reasons).map(plainText).filter(Boolean).slice(0, 5);
      if (reasons.length) {
        const list = node("ul", "trade-timing-reasons");
        for (const reason of reasons) list.append(node("li", "", reason));
        article.append(list);
      }
    }
    return article;
  }

  function renderRecommendations(partner) {
    const recommendation = partner.recommendation || {};
    const container = node("section", "trade-timing-recommendations");
    const defaultPlan = recommendation.default_plan;
    const watchPlan = recommendation.conditional_watch_plan;
    if (!defaultPlan && !watchPlan) {
      const unavailable = node("div", "trade-timing-no-plan");
      unavailable.append(
        node("strong", "", recommendation.shortlist_is_exhaustive
          ? "No mutually improving timed trade found in the complete 1-for-1 screen"
          : "No mutually improving trade found in the simulated shortlist"),
        node("p", "", plainText(recommendation.interpretation)
          || "The bounded timing preview did not find a package that clears both teams’ playoff-gain floor. The full trade search may still find a different or larger package.")
      );
      container.append(unavailable);
    } else {
      if (defaultPlan) container.append(renderPlan(defaultPlan, {label: "BEST CURRENT-WINDOW CANDIDATE"}));
      if (watchPlan) container.append(renderPlan(watchPlan, {label: "CONDITIONAL WATCH PLAN", conditional: true}));
    }
    const alternatives = array(recommendation.alternatives).filter(value => value && typeof value === "object").slice(0, 4);
    if (alternatives.length) {
      const details = node("details", "trade-timing-alternatives");
      details.append(node("summary", "", `${alternatives.length} alternative timing plan${alternatives.length === 1 ? "" : "s"}`));
      const list = node("div", "trade-timing-alternative-list");
      alternatives.forEach((plan, index) => list.append(renderPlan(plan, {
        label: `ALTERNATIVE ${index + 1}`,
        conditional: plan.trigger?.kind === "loss_and_downward_slope",
        compact: true
      })));
      details.append(list);
      container.append(details);
    }
    $("tradeTimingRecommendations").replaceChildren(container);
  }

  function ratioWidth(value) {
    const number = metricValue(value);
    if (number === null) return 0;
    const ratio = Math.abs(number) > 1.000001 ? number / 100 : number;
    return Math.max(0, Math.min(1, ratio)) * 100;
  }

  function probabilityBar(label, value) {
    const group = node("div", "trade-timing-probability");
    const heading = node("div");
    heading.append(node("span", "", label), node("strong", "", probability(value)));
    const track = node("div", "trade-timing-probability-track");
    const bar = node("span");
    bar.style.width = `${ratioWidth(value)}%`;
    track.append(bar);
    group.append(heading, track);
    return group;
  }

  function renderWindows(partner) {
    const windows = array(partner.vulnerable_windows).slice(0, 5);
    if (!windows.length) {
      $("tradeTimingWindows").replaceChildren(node("p", "trade-timing-unavailable", "No pressure windows could be projected from the remaining schedule."));
      return;
    }
    const list = node("div", "trade-timing-window-list");
    for (const [index, window] of windows.entries()) {
      const card = node("article", `trade-timing-window${index === 0 ? " is-top" : ""}`);
      const heading = node("div", "trade-timing-window-heading");
      const title = node("div");
      title.append(
        node("p", "trade-timing-overline", index === 0 ? "HIGHEST PROJECTED PRESSURE" : `WATCH WINDOW ${index + 1}`),
        node("h4", "", `${weekLabel(window.result_week)} result → reassess before ${weekLabel(window.effective_week)}`)
      );
      heading.append(title, statusPill(`${probability(window.trigger_probability)} trigger chance`, index === 0 ? "watch" : "quiet"));
      card.append(heading);
      const metrics = node("div", "trade-timing-window-metrics");
      metrics.append(
        probabilityBar("Projected loss", window.loss_probability),
        probabilityBar("Downward slope", window.downward_slope_probability),
        probabilityBar("Two-loss streak", window.two_loss_streak_probability),
        probabilityBar("Pressure percentile", window.pressure_percentile)
      );
      card.append(metrics);
      const outcome = node("p", "trade-timing-outcome-split");
      outcome.append(
        node("span", "", `Playoffs after win: ${probability(window.playoff_probability_if_win)}`),
        node("span", "", `after loss: ${probability(window.playoff_probability_if_loss)}`)
      );
      card.append(outcome);
      if (window.conditional_trade_simulation_status !== "ready") {
        card.append(node(
          "p",
          "trade-timing-window-gate",
          `Only ${numberFormatter.format(metricValue(window.trigger_scenario_count) ?? 0)} trigger paths were captured; ${numberFormatter.format(metricValue(window.conditional_minimum_scenario_count) ?? 0)} are required before valuing a trade in this window.`
        ));
      }
      list.append(card);
    }
    $("tradeTimingWindows").replaceChildren(list);
  }

  function trajectoryValue(point) {
    for (const key of [
      "cumulative_win_percentage", "median_cumulative_win_percentage", "win_percentage",
      "expected_win_percentage", "projected_win_percentage", "win_rate", "value"
    ]) {
      const value = metricValue(point?.[key]);
      if (value !== null) return Math.abs(value) > 1.000001 ? value / 100 : value;
    }
    const wins = metricValue(point?.wins ?? point?.expected_wins);
    const losses = metricValue(point?.losses ?? point?.expected_losses);
    const ties = metricValue(point?.ties ?? point?.expected_ties) || 0;
    if (wins !== null && losses !== null && wins + losses + ties > 0) return (wins + ties * .5) / (wins + losses + ties);
    const games = metricValue(point?.games_played ?? point?.games);
    return wins !== null && games ? wins / games : null;
  }

  function trajectoryPoints(section, projected) {
    const candidates = projected
      ? section.projected_points ?? section.projected ?? section.projection
      : section.observed_points ?? section.observed ?? section.history;
    return array(candidates).map((point, index) => ({
      week: finite(point?.week ?? point?.result_week ?? point?.period) ?? index + 1,
      value: trajectoryValue(point),
      projected
    })).filter(point => point.value !== null);
  }

  function renderTrajectory(partner) {
    const section = partner.record_trajectory || {};
    const observed = trajectoryPoints(section, false);
    const projected = trajectoryPoints(section, true);
    const points = [...observed, ...projected].sort((a, b) => a.week - b.week || Number(a.projected) - Number(b.projected));
    const direction = humanize(section.direction || section.current_direction) || "Projection unavailable";
    $("tradeTimingTrajectoryDirection").textContent = direction;
    if (points.length < 2) {
      $("tradeTimingTrajectory").replaceChildren(node("p", "trade-timing-unavailable", "Not enough completed and projected weeks to draw a record trajectory."));
      return;
    }
    const width = 420;
    const height = 158;
    const margin = {left: 35, right: 14, top: 16, bottom: 30};
    const weeks = points.map(point => point.week);
    const minWeek = Math.min(...weeks);
    const maxWeek = Math.max(...weeks);
    const x = week => margin.left + (week - minWeek) / Math.max(1, maxWeek - minWeek) * (width - margin.left - margin.right);
    const y = value => margin.top + (1 - Math.max(0, Math.min(1, value))) * (height - margin.top - margin.bottom);
    const figure = node("figure", "trade-timing-trajectory-figure");
    const hasObservedHistory = section.history_status === "complete" && observed.length > 0;
    const description = hasObservedHistory
      ? `${direction} record path. Observed through ${weekLabel(observed.at(-1)?.week)}; projected through ${weekLabel(projected.at(-1)?.week)}.`
      : `${direction} projected record path from the captured current standings. Verified week-by-week history is unavailable.`;
    const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": description});
    const title = svgNode("title");
    title.textContent = description;
    svg.append(title);
    for (const ratio of [0, .5, 1]) {
      const gridY = y(ratio);
      svg.append(svgNode("line", {x1: margin.left, y1: gridY, x2: width - margin.right, y2: gridY, class: "trade-timing-chart-grid"}));
      const label = svgNode("text", {x: margin.left - 7, y: gridY + 4, "text-anchor": "end", class: "trade-timing-chart-label"});
      label.textContent = `${Math.round(ratio * 100)}%`;
      svg.append(label);
    }
    const path = rows => rows.map((point, index) => `${index ? "L" : "M"} ${x(point.week).toFixed(1)} ${y(point.value).toFixed(1)}`).join(" ");
    if (observed.length > 1) svg.append(svgNode("path", {d: path(observed), class: "trade-timing-chart-observed"}));
    const forecast = observed.length ? [observed.at(-1), ...projected] : projected;
    if (forecast.length > 1) svg.append(svgNode("path", {d: path(forecast), class: "trade-timing-chart-projected"}));
    for (const point of points) {
      svg.append(svgNode("circle", {
        cx: x(point.week), cy: y(point.value), r: point.projected ? 3 : 3.5,
        class: point.projected ? "trade-timing-chart-point is-projected" : "trade-timing-chart-point"
      }));
    }
    for (const week of [minWeek, maxWeek]) {
      const label = svgNode("text", {x: x(week), y: height - 7, "text-anchor": week === minWeek ? "start" : "end", class: "trade-timing-chart-label"});
      label.textContent = `W${week}`;
      svg.append(label);
    }
    const legend = node("figcaption", "trade-timing-chart-caption");
    if (hasObservedHistory) legend.append(node("span", "is-observed", "Observed"));
    legend.append(node("span", "is-projected", "Aggregate projection"));
    figure.append(svg, legend);
    $("tradeTimingTrajectory").replaceChildren(figure);
  }

  function sampleDetail(value) {
    if (!value || typeof value !== "object") return "";
    const count = metricValue(value.completed_deals ?? value.deal_count ?? value.successes ?? value.numerator);
    const weeks = metricValue(value.eligible_weeks ?? value.exposure_weeks ?? value.exposures ?? value.denominator ?? value.sample_size);
    if (count !== null && weeks !== null) return `${numberFormatter.format(count)} active trade periods / ${numberFormatter.format(weeks)} elapsed scoring periods`;
    if (weeks !== null) return `${numberFormatter.format(weeks)} elapsed scoring periods`;
    return "";
  }

  function timingRate(label, value) {
    const row = node("div", "trade-timing-rate");
    const copy = node("div");
    copy.append(node("span", "", label));
    const sample = sampleDetail(value);
    const interval = value?.interval_80;
    const lower = metricValue(interval?.lower);
    const upper = metricValue(interval?.upper);
    const evidence = [
      sample,
      lower === null || upper === null
        ? ""
        : `80% Wilson interval ${probability(lower)}–${probability(upper)}`
    ].filter(Boolean).join(" · ");
    if (evidence) copy.append(node("small", "", evidence));
    row.append(copy, node("strong", "", probability(value)));
    return row;
  }

  function renderDealTiming(partner) {
    const section = partner.completed_deal_timing || {};
    const container = node("div", "trade-timing-deal-content");
    container.append(statusPill("Historical completed-deal participation", "quiet"));
    const ratesAvailable = [section.unconditional, section.after_loss, section.after_nonloss]
      .some(value => metricValue(value) !== null);
    if (section.status !== "descriptive" || !ratesAvailable) {
      const reason = humanize(section.unavailable_reason);
      container.append(node("p", "trade-timing-unavailable", reason
        || plainText(section.interpretation)
        || "There is not enough verified completed-deal history to compare post-loss and other scoring periods."));
    } else {
      const rates = node("div", "trade-timing-rate-list");
      rates.append(
        timingRate("All elapsed scoring periods", section.unconditional),
        timingRate("After a loss", section.after_loss),
        timingRate("After a non-loss", section.after_nonloss),
        timingRate("During a downward record slope", section.downward),
        timingRate("During a non-downward slope", section.non_downward)
      );
      container.append(rates);
      for (const [label, association] of [
        ["After-loss association", section.association || {}],
        ["Downward-slope association", section.slope_association || {}]
      ]) {
        const difference = metricValue(association.rate_difference ?? association.estimate ?? association);
        if (difference === null) continue;
        const callout = node("p", "trade-timing-association");
        const bound = association.heuristic_difference_bound_95
          || association.heuristic_difference_bound_80;
        const boundLevel = metricValue(bound?.level);
        const boundLower = metricValue(bound?.lower);
        const boundUpper = metricValue(bound?.upper);
        const boundText = boundLower === null || boundUpper === null
          ? "No heuristic difference bound is available."
          : `${probability(boundLevel)} heuristic bound ${percentagePoints(boundLower)} to ${percentagePoints(boundUpper)}.`;
        const sampleText = association.sample_gate_met
          ? "The comparison sample gate is met."
          : "The comparison sample gate is not met.";
        callout.textContent = `${label}: ${percentagePoints(difference)}. ${sampleText} ${boundText} Descriptive only; no causal or nominal confidence claim.`;
        container.append(callout);
      }
    }
    container.append(node(
      "p",
      "trade-timing-unavailable",
      "A personalized future participation projection is unavailable because historical health and the league trade deadline are not aligned to each scoring period."
    ));
    container.append(node(
      "p", "trade-timing-history-boundary",
      "This measures completed-deal participation by team slot. It does not measure offer acceptance, willingness, proposer identity, or causation."
    ));
    $("tradeTimingDealTiming").replaceChildren(container);
  }

  function renderMethodology() {
    const container = node("div", "trade-timing-method-content");
    const list = node("ul");
    list.append(
      node("li", "", "Observed records are joined to aggregate future score scenarios; pressure is a simulated team situation, not a psychological claim."),
      node("li", "", "Future packages are valued only inside pre-trade scenarios where the named loss-and-downturn trigger occurs, and only after the conditional sample gate is met."),
      node("li", "", "A candidate package must clear the engine’s power screen and a declared mutual playoff-gain materiality floor. The result remains a Monte Carlo point estimate."),
      node("li", "", "Buy-low and sell-high labels describe the captured weekly projection curve. They are not market prices and do not predict another manager’s beliefs."),
      node("li", "", "Completed-trade comparisons use elapsed scoring periods because the league deadline is not captured; they cannot estimate acceptance or willingness."),
      node("li", "", "Confirm that trades remain open and verify current health before using any package shown here.")
    );
    for (const limitation of array(timing.methodology?.limitations).map(plainText).filter(Boolean)) {
      list.append(node("li", "", limitation));
    }
    const readiness = timing.data_readiness || {};
    const activityStatus = plainText(
      readiness.capabilities?.completed_deal_activity?.status
    );
    const attemptNote = HISTORY_ATTEMPT_NOTES[readiness.collection_attempt?.reason_code];
    if (attemptNote) {
      list.append(node("li", "", attemptNote));
    } else if (readiness.store_status === "unavailable") {
      list.append(node("li", "", "The local history store is unavailable. Forward-looking simulation still runs, but completed-deal behavior is withheld."));
    } else if (activityStatus && activityStatus !== "ready") {
      list.append(node("li", "", `Completed-deal data is ${humanize(activityStatus)}. Forward-looking simulation remains separate from that missing history.`));
    }
    container.append(list);
    const summary = plainText(timing.methodology?.summary || timing.methodology?.interpretation);
    if (summary) container.prepend(node("p", "", summary));
    $("tradeTimingMethodology").replaceChildren(container);
  }

  function renderPartner() {
    const partner = selectedPartner();
    if (!partner) return;
    selectedPartnerId = partner.partner_team_id;
    $("tradeTimingPartnerSelect").value = selectedPartnerId;
    renderPartnerBoard();
    renderPartnerOverview(partner);
    renderRecommendations(partner);
    renderWindows(partner);
    renderTrajectory(partner);
    renderDealTiming(partner);
  }

  function render() {
    renderSummary();
    renderPartnerPicker();
    renderPartner();
    renderMethodology();
  }

  $("tradeTimingPartnerSelect")?.addEventListener("change", event => {
    selectedPartnerId = event.target.value;
    renderPartner();
  });
  $("tradeTimingPartnerBoard")?.addEventListener("click", event => {
    const button = event.target.closest(".trade-timing-partner-button");
    if (!button) return;
    const activatedPartnerId = button.dataset.partnerTeamId;
    selectedPartnerId = activatedPartnerId;
    renderPartner();
    const replacement = [...$("tradeTimingPartnerBoard").querySelectorAll(".trade-timing-partner-button")]
      .find(item => item.dataset.partnerTeamId === activatedPartnerId);
    replacement?.focus({preventScroll: true});
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("tradeTimingPartnerOverview").scrollIntoView({block: "nearest", behavior: reducedMotion ? "auto" : "smooth"});
  });

  return Object.freeze({reset, setBundle, setPrimaryTeam, setPartnerTeam});
})();
