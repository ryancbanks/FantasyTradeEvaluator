"use strict";

window.PlayerLabUi = (() => {
  const $ = id => document.getElementById(id);
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  const numberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1, minimumFractionDigits: 1});
  const evidenceNumberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 3, minimumFractionDigits: 1});
  const integerFormatter = new Intl.NumberFormat();
  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
  });
  const AVAILABLE_OWNER = "__available__";
  const STATUS_LABELS = Object.freeze({
    observed: "Observed",
    bye: "Bye",
    not_applicable: "Not applicable",
    not_published: "Not published",
    parse_error: "Parse error",
    unmatched_player: "Unmatched player",
    insufficient_sources: "Insufficient sources",
    not_retained: "Evidence not retained",
    waiver_pool: "Available"
  });
  const SORTS = Object.freeze([
    ["ros_points", "Rest-of-season points"],
    ["weekly_ecr", "Weekly ECR"],
    ["ros_ecr", "Rest-of-season ECR"],
    ["disagreement", "Provider disagreement"],
    ["name", "Player name"]
  ]);

  let requestRevision = 0;
  let activeController = null;
  let outlook = null;
  let visiblePlayers = [];
  let selectedPlayerId = null;

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function humanize(value) {
    if (typeof value !== "string" || !value) return "Unavailable";
    return value.replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
  }

  function providerKey(value) {
    return String(typeof value === "object" && value ? value.provider : value || "").toLowerCase();
  }

  function providerLabel(value) {
    if (typeof value === "object" && value?.label) return value.label;
    const provider = providerKey(value);
    if (provider === "espn") return "ESPN";
    if (provider === "yahoo") return "Yahoo";
    if (provider === "fantasypros") return "FantasyPros";
    return humanize(provider);
  }

  function statusLabel(value) {
    return STATUS_LABELS[value] || humanize(value);
  }

  function statusClass(value) {
    if (value === "observed") return "is-observed";
    if (value === "bye" || value === "not_applicable" || value === "not_retained") return "is-neutral";
    if (value === "not_published") return "is-missing";
    return "is-error";
  }

  function number(value) {
    return Number.isFinite(value) ? numberFormatter.format(value) : "—";
  }

  function evidenceNumber(value) {
    return Number.isFinite(value) ? evidenceNumberFormatter.format(value) : "—";
  }

  function evidenceValue(value) {
    const result = node("strong", "", evidenceNumber(value));
    if (Number.isFinite(value)) {
      result.title = `Exact stored value: ${String(value)}`;
      result.setAttribute("aria-label", `${String(value)} projected points`);
    }
    return result;
  }

  function rank(value) {
    return Number.isFinite(value) ? `#${integerFormatter.format(value)}` : "—";
  }

  function ecrRank(value) {
    return rank(value?.rank);
  }

  function ecrDetail(value) {
    if (!value || !Number.isFinite(value.rank)) return "Consensus rank unavailable";
    const details = [];
    if (Number.isFinite(value.position_rank)) details.push(`Position ${rank(value.position_rank)}`);
    if (Number.isFinite(value.rank_min) && Number.isFinite(value.rank_max)) {
      details.push(`Expert range ${rank(value.rank_min)}–${rank(value.rank_max)}`);
    }
    return details.join(" · ") || "Captured consensus rank";
  }

  function timeNode(value, prefix) {
    if (typeof value !== "string" || !value) return null;
    const timestamp = new Date(value);
    const time = node("time", "", Number.isNaN(timestamp.valueOf()) ? value : dateFormatter.format(timestamp));
    time.dateTime = value;
    const row = node("span", "player-lab-trace-time", `${prefix} `);
    row.append(time);
    return row;
  }

  function statusBadge(status) {
    return node("span", `player-lab-status ${statusClass(status)}`, statusLabel(status));
  }

  function originLabel(source) {
    if (source.origin === "provider_published") {
      if (source.status === "observed") return "Published directly by provider";
      if (source.status === "bye") return "Bye captured directly from provider";
      return "Captured directly from provider";
    }
    if (source.origin === "derived_rest_of_season") return "Allocated from captured ROS total";
    if (source.origin === "derived_weekly") return "Summed from complete weekly rows";
    return humanize(source.origin);
  }

  function appendSourceTrace(container, source, includeWeeks = false) {
    if (source.status === "not_retained") {
      container.append(node("span", "player-lab-muted", "No source record was retained in this bundle."));
      return;
    }
    if (source.origin) container.append(node("span", "player-lab-source-origin", originLabel(source)));
    if (Number.isFinite(source.weight)) {
      const weight = node("span", "player-lab-muted", `Model weight ${evidenceNumber(source.weight)}`);
      weight.title = `Exact stored weight: ${String(source.weight)}`;
      weight.setAttribute("aria-label", `Model weight ${String(source.weight)}`);
      container.append(weight);
    }
    if (includeWeeks && Array.isArray(source.applicable_weeks)) {
      container.append(node("span", "player-lab-muted", source.applicable_weeks.length ? `Covers weeks ${source.applicable_weeks.join(", ")}` : "No applicable weeks"));
    }
    const captured = timeNode(source.captured_at, "Captured");
    const updated = timeNode(source.source_published_at, "Source updated");
    container.append(captured || node("span", "player-lab-muted", "Capture time unavailable"));
    container.append(updated || node("span", "player-lab-muted", "Source update time unavailable"));
  }

  function setState(kind, message = "") {
    $("playerLabEmpty").classList.toggle("hidden", kind !== "empty");
    $("playerLabLoading").classList.toggle("hidden", kind !== "loading");
    $("playerLabContent").classList.toggle("hidden", kind !== "content");
    if (kind === "empty" && message) $("playerLabEmpty").textContent = message;
  }

  function clearRenderedContent() {
    for (const id of [
      "playerLabTableBody", "playerLabDetail", "playerLabProviderHead",
      "playerLabWeeklyBody", "playerLabRosSources", "playerLabFreshness"
    ]) $(id).replaceChildren();
    $("playerLabCount").textContent = "";
  }

  function reset(message = "Select a ready week to explore its player projections.") {
    requestRevision += 1;
    if (activeController) activeController.abort();
    activeController = null;
    outlook = null;
    visiblePlayers = [];
    selectedPlayerId = null;
    $("playerLabSearch").value = "";
    clearRenderedContent();
    setState("empty", message);
  }

  async function setBundle(bundle, {request, onError} = {}) {
    if (!bundle) {
      reset();
      return;
    }
    if (typeof request !== "function") throw new Error("Player Lab request function is unavailable.");
    const revision = ++requestRevision;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    outlook = null;
    visiblePlayers = [];
    selectedPlayerId = null;
    setState("loading");
    try {
      const value = await request(
        `/api/bundles/${encodeURIComponent(bundle.bundle_id)}/player-outlook`,
        {signal: controller.signal}
      );
      if (controller.signal.aborted || revision !== requestRevision) return;
      if (!value || !Array.isArray(value.players) || !Array.isArray(value.providers)) {
        throw new Error("Player outlook response is invalid.");
      }
      outlook = value;
      prepareControls();
      renderFreshness();
      render();
      setState("content");
    } catch (error) {
      if (controller.signal.aborted || revision !== requestRevision) return;
      clearRenderedContent();
      setState("empty", "Player Lab could not load this week's projection evidence.");
      if (typeof onError === "function") onError(error);
    } finally {
      if (revision === requestRevision) activeController = null;
    }
  }

  function prepareControls() {
    $("playerLabSearch").value = "";
    const owner = $("playerLabOwnerFilter");
    owner.replaceChildren(new Option("All teams and available players", ""));
    const owners = new Map();
    for (const player of outlook.players) {
      if (player.owner) owners.set(player.owner.team_id, player.owner.team_name);
    }
    for (const [teamId, teamName] of [...owners].sort((left, right) => collator.compare(left[1], right[1]))) {
      owner.add(new Option(teamName, teamId));
    }
    if (outlook.players.some(player => !player.owner)) {
      owner.add(new Option("Available · captured waiver pool", AVAILABLE_OWNER));
    }

    const position = $("playerLabPositionFilter");
    position.replaceChildren(new Option("All positions", ""));
    const positions = [...new Set(outlook.players.map(player => player.position))].sort(collator.compare);
    for (const value of positions) position.add(new Option(value, value));

    const sort = $("playerLabSort");
    sort.replaceChildren(...SORTS.map(([value, label]) => new Option(label, value)));
    sort.value = "ros_points";
  }

  function normalizedSearch(player) {
    return [
      player.name, player.position, player.nfl_team_id,
      player.owner?.team_name, ...(player.eligible_slots || [])
    ].filter(Boolean).join(" ").toLocaleLowerCase();
  }

  function metricCompare(left, right, field, descending) {
    const leftValue = field === "weekly_ecr" || field === "rest_of_season_ecr" ? left[field]?.rank : left[field];
    const rightValue = field === "weekly_ecr" || field === "rest_of_season_ecr" ? right[field]?.rank : right[field];
    const leftMissing = !Number.isFinite(leftValue);
    const rightMissing = !Number.isFinite(rightValue);
    if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
    if (!leftMissing && leftValue !== rightValue) {
      return descending ? rightValue - leftValue : leftValue - rightValue;
    }
    return collator.compare(left.name, right.name) || collator.compare(left.player_id, right.player_id);
  }

  function playerCompare(left, right) {
    switch ($("playerLabSort").value) {
      case "weekly_ecr": return metricCompare(left, right, "weekly_ecr", false);
      case "ros_ecr": return metricCompare(left, right, "rest_of_season_ecr", false);
      case "disagreement": return metricCompare(left, right, "average_provider_disagreement", true);
      case "name": return collator.compare(left.name, right.name) || collator.compare(left.player_id, right.player_id);
      default: return metricCompare(left, right, "remaining_projected_points", true);
    }
  }

  function filteredPlayers() {
    const query = $("playerLabSearch").value.trim().toLocaleLowerCase();
    const owner = $("playerLabOwnerFilter").value;
    const position = $("playerLabPositionFilter").value;
    return outlook.players.filter(player => {
      const ownerMatches = !owner || (owner === AVAILABLE_OWNER ? !player.owner : player.owner?.team_id === owner);
      return ownerMatches && (!position || player.position === position) && (!query || normalizedSearch(player).includes(query));
    }).sort(playerCompare);
  }

  function render() {
    visiblePlayers = filteredPlayers();
    if (!visiblePlayers.some(player => player.player_id === selectedPlayerId)) {
      selectedPlayerId = visiblePlayers[0]?.player_id || null;
    }
    renderMasterTable();
    renderDetail();
  }

  function ownerText(player) {
    return player.owner?.team_name || statusLabel(player.availability);
  }

  function secondaryText(parent, value) {
    parent.append(node("span", "player-lab-muted", value));
  }

  function renderMasterTable() {
    const body = $("playerLabTableBody");
    body.replaceChildren();
    $("playerLabCount").textContent = `${visiblePlayers.length.toLocaleString()} of ${outlook.players.length.toLocaleString()} players`;
    if (!visiblePlayers.length) {
      const row = document.createElement("tr");
      const cell = node("td", "player-lab-no-results", "No players match these filters.");
      cell.colSpan = 8;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const player of visiblePlayers) {
      const row = document.createElement("tr");
      const selected = player.player_id === selectedPlayerId;
      row.dataset.playerId = player.player_id;
      row.setAttribute("aria-selected", String(selected));

      const identity = document.createElement("th");
      identity.scope = "row";
      const choose = node("button", "player-lab-player-button", player.name);
      choose.type = "button";
      choose.dataset.playerId = player.player_id;
      choose.setAttribute("aria-pressed", String(selected));
      choose.addEventListener("click", () => selectPlayer(player.player_id, true));
      identity.append(choose);
      secondaryText(identity, player.nfl_team_id || "NFL team unavailable");

      const owner = node("td", "", ownerText(player));
      secondaryText(owner, humanize(player.availability));
      const position = node("td", "", player.position);
      secondaryText(position, (player.eligible_slots || []).join(" · ") || "No listed flex eligibility");
      const points = node("td", "player-lab-number", number(player.remaining_projected_points));
      const average = node("td", "player-lab-number", number(player.average_weekly_points));
      const weeklyEcr = node("td", "player-lab-number", ecrRank(player.weekly_ecr));
      const rosEcr = node("td", "player-lab-number", ecrRank(player.rest_of_season_ecr));
      const sources = node("td", "", `${player.provider_complete_week_count}/${player.total_week_count} complete`);
      secondaryText(sources, `${player.all_direct_week_count} all-direct · Provider σ ${number(player.average_provider_disagreement)}`);
      row.append(identity, owner, position, points, average, weeklyEcr, rosEcr, sources);
      body.append(row);
    }
  }

  function selectPlayer(playerId, restoreFocus = false) {
    if (!visiblePlayers.some(player => player.player_id === playerId)) return;
    selectedPlayerId = playerId;
    renderMasterTable();
    renderDetail();
    if (restoreFocus) {
      const replacement = [...$("playerLabTableBody").querySelectorAll(".player-lab-player-button")]
        .find(button => button.dataset.playerId === playerId);
      replacement?.focus();
    }
  }

  function selectedPlayer() {
    return outlook?.players.find(player => player.player_id === selectedPlayerId) || null;
  }

  function metric(label, value, detail) {
    const card = node("div", "player-lab-metric");
    card.append(node("span", "player-lab-metric-label", label), node("strong", "", value));
    if (detail) card.append(node("span", "player-lab-muted", detail));
    return card;
  }

  function renderDetail() {
    const detail = $("playerLabDetail");
    detail.replaceChildren();
    const player = selectedPlayer();
    if (!player) {
      detail.append(node("p", "player-lab-no-results", "Choose a player to inspect weekly and source-level evidence."));
      renderProviderHeader();
      $("playerLabWeeklyBody").replaceChildren();
      $("playerLabRosSources").replaceChildren();
      return;
    }

    const heading = node("div", "player-lab-detail-heading");
    const title = node("div");
    title.append(node("p", "player-lab-kicker", "PLAYER EVIDENCE"), node("h3", "", player.name));
    const tags = node("div", "player-lab-tags");
    tags.append(node("span", "player-lab-tag", player.position));
    tags.append(node("span", "player-lab-tag", player.nfl_team_id || "NFL team unavailable"));
    tags.append(node("span", "player-lab-tag", ownerText(player)));
    heading.append(title, tags);

    const metrics = node("div", "player-lab-metrics");
    metrics.append(
      metric(
        "Rest-of-season points",
        number(player.remaining_projected_points),
        `${player.provider_complete_week_count}/${player.total_week_count} provider-complete · ${player.all_direct_week_count} all-direct`
      ),
      metric(
        "Average active week",
        number(player.average_weekly_points),
        `${(player.weeks || []).filter(week => Number.isFinite(week.projected_points)).length} projected games`
      ),
      metric("Weekly ECR", ecrRank(player.weekly_ecr), ecrDetail(player.weekly_ecr)),
      metric("Rest-of-season ECR", ecrRank(player.rest_of_season_ecr), ecrDetail(player.rest_of_season_ecr)),
      metric("Provider disagreement", number(player.average_provider_disagreement), "Average between-source σ"),
      metric("Predictive uncertainty", number(player.average_predictive_uncertainty), "Average modeled σ")
    );
    const eligibility = node("p", "player-lab-eligibility", `Eligible lineup slots: ${(player.eligible_slots || []).join(", ") || "none listed"}.`);
    const notice = node("p", "player-lab-notice", outlook.waiver_scope_notice || "Waiver-wire scope is not available for this bundle.");
    detail.append(heading, metrics, eligibility, notice);

    renderProviderHeader();
    renderWeeklyEvidence(player);
    renderRemainingSeasonSources(player);
  }

  function renderProviderHeader() {
    const head = $("playerLabProviderHead");
    head.replaceChildren();
    for (const label of ["Week", "Matchup", "Ensemble", "Uncertainty", "Sources"]) {
      const cell = node("th", "", label);
      cell.scope = "col";
      head.append(cell);
    }
    for (const provider of outlook?.providers || []) {
      const cell = node("th", "", providerLabel(provider));
      cell.scope = "col";
      head.append(cell);
    }
  }

  function matchupLabel(week) {
    if (week.status === "bye") return "Bye";
    if (!week.opponent_team_id) return "—";
    const prefix = week.is_home === true ? "vs " : week.is_home === false ? "@ " : "";
    return `${prefix}${week.opponent_team_id}`;
  }

  function sourceCell(value, provider) {
    const source = value || {provider, status: "not_retained", projected_points: null};
    const cell = node("td", `player-lab-source-cell ${statusClass(source.status)}`);
    const headline = node("div", "player-lab-source-value");
    headline.append(evidenceValue(source.projected_points), statusBadge(source.status));
    cell.append(headline);
    appendSourceTrace(cell, source);
    return cell;
  }

  function renderWeeklyEvidence(player) {
    const body = $("playerLabWeeklyBody");
    body.replaceChildren();
    const weeks = [...(player.weeks || [])].sort((left, right) => left.week - right.week);
    if (!weeks.length) {
      const row = document.createElement("tr");
      const cell = node("td", "player-lab-no-results", "No remaining weekly projection rows are available.");
      cell.colSpan = 5 + outlook.providers.length;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const week of weeks) {
      const row = document.createElement("tr");
      const weekCell = node("th", "", `Week ${week.week}`);
      weekCell.scope = "row";
      weekCell.append(statusBadge(week.status));
      const matchup = node("td", "", matchupLabel(week));
      const ensemble = node("td", "player-lab-number", number(week.projected_points));
      if (!Number.isFinite(week.projected_points)) secondaryText(ensemble, statusLabel(week.status));
      const uncertainty = node("td", "player-lab-number", Number.isFinite(week.predictive_stddev) ? `±${number(week.predictive_stddev)}` : "—");
      secondaryText(uncertainty, `Provider σ ${number(week.between_provider_stddev)}`);
      const usableSourceCount = Number.isFinite(week.usable_source_count)
        ? week.usable_source_count
        : week.observed_source_count;
      const coverage = node(
        "td",
        "",
        week.status === "bye"
          ? `${usableSourceCount}/${outlook.providers.length} report bye`
          : `${usableSourceCount}/${outlook.providers.length} usable`
      );
      const provenance = [
        `${week.direct_source_count || 0} direct`,
        `${week.derived_source_count || 0} derived`,
      ];
      if (week.unattributed_source_count) provenance.push(`${week.unattributed_source_count} provenance unknown`);
      if (week.not_retained_source_count) provenance.push(`${week.not_retained_source_count} not retained`);
      if (week.status !== "bye") provenance.push(`minimum ${week.minimum_observed_sources}`);
      secondaryText(
        coverage,
        week.status === "bye"
          ? `${provenance.join(" · ")} · Verified schedule bye`
          : provenance.join(" · ")
      );
      row.append(weekCell, matchup, ensemble, uncertainty, coverage);
      const byProvider = new Map((week.provider_values || []).map(value => [providerKey(value), value]));
      for (const provider of outlook.providers) row.append(sourceCell(byProvider.get(providerKey(provider)), providerKey(provider)));
      body.append(row);
    }
  }

  function renderRemainingSeasonSources(player) {
    const container = $("playerLabRosSources");
    container.replaceChildren();
    const heading = node("div", "player-lab-grid-heading");
    heading.append(node("h4", "", "Provider rest-of-season totals"), node("p", "", "Direct and derived evidence retained in this weekly bundle"));
    container.append(heading);
    const byProvider = new Map((player.provider_remaining_season || []).map(value => [providerKey(value), value]));
    for (const provider of outlook.providers) {
      const key = providerKey(provider);
      const value = byProvider.get(key) || {provider: key, status: "not_retained", projected_points: null};
      const card = node("article", "player-lab-source-card");
      card.append(node("h4", "", providerLabel(provider)), sourceCellContent(value));
      container.append(card);
    }
  }

  function sourceCellContent(source) {
    const content = node("div", "player-lab-source-card-content");
    const headline = node("div", "player-lab-source-value");
    headline.append(evidenceValue(source.projected_points), statusBadge(source.status));
    content.append(headline);
    appendSourceTrace(content, source, true);
    return content;
  }

  function freshnessCard(title, capturedAt, sourceUpdatedAt, detail = "") {
    const card = node("article", "player-lab-freshness-card");
    card.append(node("strong", "", title));
    if (detail) card.append(node("span", "player-lab-muted", detail));
    const captured = timeNode(capturedAt, "Captured");
    const updated = timeNode(sourceUpdatedAt, "Source updated");
    card.append(captured || node("span", "player-lab-muted", "Capture time unavailable"));
    if (updated) card.append(updated);
    if (!updated) card.append(node("span", "player-lab-muted", "Source update time unavailable"));
    return card;
  }

  function renderFreshness() {
    const container = $("playerLabFreshness");
    container.replaceChildren();
    const heading = node("div", "player-lab-grid-heading");
    heading.append(node("h3", "", "Evidence freshness"), node("p", "", "Weekly collection and source-update times"));
    container.append(heading);
    for (const snapshot of outlook.ecr_snapshots || []) {
      container.append(freshnessCard(
        `FantasyPros ECR · ${humanize(snapshot.period)}`,
        snapshot.captured_at,
        snapshot.source_updated_at || snapshot.source_published_at,
        Number.isFinite(snapshot.expert_count)
          ? `${snapshot.expert_count} experts${Number.isFinite(snapshot.selected_expert_count) ? ` · ${snapshot.selected_expert_count} selected` : ""}`
          : ""
      ));
    }
    for (const provider of outlook.providers) {
      container.append(freshnessCard(
        `${providerLabel(provider)} projections`,
        provider.captured_at,
        provider.source_published_at,
        "Latest evidence retained for this weekly bundle"
      ));
    }
  }

  for (const id of ["playerLabSearch", "playerLabOwnerFilter", "playerLabPositionFilter", "playerLabSort"]) {
    const control = $(id);
    if (control) control.addEventListener(id === "playerLabSearch" ? "input" : "change", () => {
      if (outlook) render();
    });
  }

  $("playerLabTableBody")?.addEventListener("keydown", event => {
    if (!event.target.matches(".player-lab-player-button")) return;
    const buttons = [...$("playerLabTableBody").querySelectorAll(".player-lab-player-button")];
    const current = buttons.indexOf(event.target);
    let target = null;
    if (event.key === "ArrowDown") target = buttons[Math.min(buttons.length - 1, current + 1)];
    else if (event.key === "ArrowUp") target = buttons[Math.max(0, current - 1)];
    else if (event.key === "Home") target = buttons[0];
    else if (event.key === "End") target = buttons.at(-1);
    if (!target || target === event.target) return;
    event.preventDefault();
    selectPlayer(target.dataset.playerId, true);
  });

  return Object.freeze({reset, setBundle});
})();
