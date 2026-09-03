"use strict";

window.PlayerLabUi = (() => {
  const $ = id => document.getElementById(id);
  const numberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1, minimumFractionDigits: 1});
  const evidenceNumberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 3, minimumFractionDigits: 1});
  const integerFormatter = new Intl.NumberFormat();
  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
  });
  const DETAIL_CACHE_LIMIT = 12;
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
  const FILTER_CONTROL_EVENTS = Object.freeze({
    playerLabSearch: "input",
    playerLabOwnerFilter: "change",
    playerLabNflTeamFilter: "change",
    playerLabPositionFilter: "change",
    playerLabGroup: "change",
    playerLabTrendFilter: "change",
    playerLabProjectionMin: "input",
    playerLabProjectionMax: "input",
    playerLabSort: "change"
  });

  let requestRevision = 0;
  let activeController = null;
  let detailController = null;
  let detailPromise = null;
  let detailRevision = 0;
  let loadingDetailPlayerId = null;
  let detailError = null;
  const detailCache = new Map();
  let outlook = null;
  let visiblePlayers = [];
  let selectedPlayerId = null;
  let pendingBundle = null;
  let pendingOptions = null;
  let activeWorkspace = "trade";
  let currentPage = 1;
  let loadingBundleId = null;

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

  function array(value) {
    return Array.isArray(value) ? value : [];
  }

  function finite(value) {
    return Number.isFinite(value) ? value : null;
  }

  function overallRank(player) {
    return finite(player?.overall_rank) ?? finite(player?.projection_overall_rank);
  }

  function positionRank(player) {
    return finite(player?.projection_position_rank);
  }

  function overallRankBasis(player) {
    if (typeof player?.overall_rank_basis === "string" && player.overall_rank_basis) {
      return humanize(player.overall_rank_basis);
    }
    const value = overallRank(player);
    if (value !== null) {
      return "Local remaining projection";
    }
    return "Ranking basis unavailable";
  }

  function playerDescription(player) {
    return window.PlayerLabProfileUi.describe(player);
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
    if (provider === "cbs") return "CBS Sports";
    if (provider === "fftoday") return "FFToday";
    if (provider === "fantasysharks") return "FantasySharks";
    if (provider === "nflverse") return "NFLverse";
    if (provider === "sleeper") return "Sleeper";
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

  const catalogUi = window.PlayerLabCatalogUi.create({
    describe: playerDescription,
    humanize,
    statusLabel,
    number,
    rank,
    ecrRank,
    ecrDetail,
    finite,
    overallRank,
    overallRankBasis,
    positionRank
  });

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
      "playerLabWeeklyBody", "playerLabRosSources", "playerLabFreshness",
      "playerLabChart", "playerLabSeasonStats"
    ]) $(id).replaceChildren();
    $("playerLabCount").textContent = "";
    $("playerLabPageStatus").textContent = "Page 1 of 1";
    $("playerLabPreviousPage").disabled = true;
    $("playerLabNextPage").disabled = true;
  }

  function clearOutlook(message) {
    requestRevision += 1;
    if (activeController) activeController.abort();
    activeController = null;
    clearDetailState();
    loadingBundleId = null;
    outlook = null;
    visiblePlayers = [];
    selectedPlayerId = null;
    currentPage = 1;
    $("playerLabSearch").value = "";
    for (const id of ["playerLabProjectionMin", "playerLabProjectionMax"]) {
      if ($(id)) $(id).value = "";
    }
    clearRenderedContent();
    setState("empty", message);
  }

  function cancelDetailRequest() {
    detailRevision += 1;
    if (detailController) detailController.abort();
    detailController = null;
    detailPromise = null;
    loadingDetailPlayerId = null;
  }

  function clearDetailState() {
    cancelDetailRequest();
    detailCache.clear();
    detailError = null;
  }

  function reset(message = "Select a ready week to explore its player projections.") {
    pendingBundle = null;
    pendingOptions = null;
    clearOutlook(message);
  }

  function queueBundle(bundle, {request, onError} = {}) {
    if (!bundle) {
      reset();
      return Promise.resolve();
    }
    if (typeof request !== "function") throw new Error("Player Lab request function is unavailable.");
    pendingBundle = bundle;
    pendingOptions = {request, onError};
    if (outlook?.bundle_id === bundle.bundle_id || loadingBundleId === bundle.bundle_id) return Promise.resolve();
    clearOutlook("Open Player Lab to load this week's player profiles.");
    return activeWorkspace === "players" ? loadPendingBundle() : Promise.resolve();
  }

  async function loadPendingBundle() {
    const bundle = pendingBundle;
    const options = pendingOptions;
    if (!bundle || !options || outlook?.bundle_id === bundle.bundle_id || loadingBundleId === bundle.bundle_id) return;
    const revision = ++requestRevision;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    loadingBundleId = bundle.bundle_id;
    outlook = null;
    visiblePlayers = [];
    selectedPlayerId = null;
    setState("loading");
    try {
      const value = await options.request(
        `/api/bundles/${encodeURIComponent(bundle.bundle_id)}/player-outlook`,
        {signal: controller.signal}
      );
      if (controller.signal.aborted || revision !== requestRevision || pendingBundle?.bundle_id !== bundle.bundle_id) return;
      if (!value || !Array.isArray(value.players) || !Array.isArray(value.providers)) {
        throw new Error("Player outlook response is invalid.");
      }
      outlook = value;
      catalogUi.prepareControls(outlook);
      renderFreshness();
      render();
      setState("content");
    } catch (error) {
      if (controller.signal.aborted || revision !== requestRevision) return;
      clearRenderedContent();
      setState("empty", "Player Lab could not load this week's projection evidence.");
      if (typeof options.onError === "function") options.onError(error);
    } finally {
      if (revision === requestRevision) {
        activeController = null;
        loadingBundleId = null;
      }
    }
  }

  function cachePlayerDetail(player) {
    detailCache.delete(player.player_id);
    detailCache.set(player.player_id, player);
    while (detailCache.size > DETAIL_CACHE_LIMIT) {
      detailCache.delete(detailCache.keys().next().value);
    }
  }

  function cachedPlayerDetail(playerId) {
    const player = detailCache.get(playerId) || null;
    if (player) cachePlayerDetail(player);
    return player;
  }

  async function loadSelectedPlayerDetail() {
    const playerId = selectedPlayerId;
    const bundleId = outlook?.bundle_id;
    const options = pendingOptions;
    if (!playerId || !bundleId || !options || cachedPlayerDetail(playerId)) return;
    if (loadingDetailPlayerId === playerId && detailPromise) return detailPromise;
    cancelDetailRequest();
    const revision = ++detailRevision;
    const controller = new AbortController();
    detailController = controller;
    loadingDetailPlayerId = playerId;
    detailError = null;
    renderDetail();
    detailPromise = options.request(
      `/api/bundles/${encodeURIComponent(bundleId)}/player-outlook/players/${encodeURIComponent(playerId)}`,
      {signal: controller.signal}
    );
    try {
      const value = await detailPromise;
      if (controller.signal.aborted || revision !== detailRevision || outlook?.bundle_id !== bundleId) return;
      if (!value || value.bundle_id !== bundleId || value.player?.player_id !== playerId) {
        throw new Error("Player detail response is invalid.");
      }
      cachePlayerDetail(value.player);
      if (selectedPlayerId === playerId) renderDetail();
    } catch (error) {
      if (controller.signal.aborted || revision !== detailRevision) return;
      detailError = "Detailed weekly evidence could not be loaded. Choose the player again to retry.";
      if (selectedPlayerId === playerId) renderDetail();
      if (typeof options.onError === "function") options.onError(error);
    } finally {
      if (revision === detailRevision) {
        detailController = null;
        detailPromise = null;
        loadingDetailPlayerId = null;
      }
    }
  }

  function render() {
    visiblePlayers = catalogUi.filteredPlayers(outlook);
    currentPage = Math.min(currentPage, catalogUi.pageCount(visiblePlayers));
    const pageSelection = catalogUi.selectionForPage(
      visiblePlayers, currentPage, selectedPlayerId
    );
    if (pageSelection !== selectedPlayerId) {
      cancelDetailRequest();
      detailError = null;
      selectedPlayerId = pageSelection;
    }
    renderMasterTable();
    renderDetail();
    void loadSelectedPlayerDetail();
  }

  function ownerText(player) {
    return player.owner?.team_name || statusLabel(player.availability);
  }

  function secondaryText(parent, value) {
    parent.append(node("span", "player-lab-muted", value));
  }

  function renderMasterTable() {
    catalogUi.render(outlook, visiblePlayers, selectedPlayerId, currentPage, selectPlayer);
  }

  function selectPlayer(playerId, restoreFocus = false) {
    if (!visiblePlayers.some(player => player.player_id === playerId)) return;
    const previousId = selectedPlayerId;
    if (previousId !== playerId) {
      cancelDetailRequest();
      detailError = null;
    }
    selectedPlayerId = playerId;
    catalogUi.updateSelection(previousId, playerId);
    renderDetail();
    void loadSelectedPlayerDetail();
    if (restoreFocus) {
      const replacement = [...$("playerLabTableBody").querySelectorAll(".player-lab-player-button")]
        .find(button => button.dataset.playerId === playerId);
      replacement?.focus();
    }
  }

  function selectedCatalogPlayer() {
    return outlook?.players.find(player => player.player_id === selectedPlayerId) || null;
  }

  function selectedPlayer() {
    return cachedPlayerDetail(selectedPlayerId) || selectedCatalogPlayer();
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
    const hasDetailedEvidence = detailCache.has(selectedPlayerId);
    if (!player) {
      detail.append(node("p", "player-lab-no-results", "Choose a player to inspect weekly and source-level evidence."));
      renderProviderHeader();
      $("playerLabWeeklyBody").replaceChildren();
      $("playerLabRosSources").replaceChildren();
      $("playerLabChart").replaceChildren();
      $("playerLabSeasonStats").replaceChildren();
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
    const description = playerDescription(player);
    metrics.append(
      metric(
        "Rest-of-season points",
        number(player.remaining_projected_points),
        `${player.provider_complete_week_count}/${player.total_week_count} provider-complete · ${player.all_direct_week_count} all-direct`
      ),
      metric(
        "Average active week",
        number(player.average_weekly_points),
        `${finite(player.total_week_count) ?? 0} remaining projection rows`
      ),
      metric(
        "Overall ranking",
        rank(overallRank(player)),
        `${overallRankBasis(player)} · Local projected ${player.position || "position"} ${rank(positionRank(player))}`
      ),
      metric("Recent actual trend", description.performanceLabel, description.performanceDetail),
      metric("Depth chart", description.depthLabel, description.profile ? "Captured public metadata" : "Not retained in this legacy bundle"),
      metric("Rest-of-season ECR", ecrRank(player.rest_of_season_ecr), ecrDetail(player.rest_of_season_ecr))
    );
    const eligibility = node("p", "player-lab-eligibility", `Eligible lineup slots: ${(player.eligible_slots || []).join(", ") || "none listed"}.`);
    const notice = node("p", "player-lab-notice", outlook.waiver_scope_notice || "Waiver-wire scope is not available for this bundle.");
    detail.append(heading, metrics, eligibility);
    if (!hasDetailedEvidence) {
      const message = detailError || "Loading this player's weekly, source, and historical evidence…";
      detail.append(node("p", detailError ? "player-lab-detail-error" : "player-lab-detail-loading", message), notice);
      renderDeferredEvidence(message);
      return;
    }
    window.PlayerLabProfileUi.render(outlook, player, detail);
    detail.append(notice);
    renderProviderHeader();
    renderWeeklyEvidence(player);
    renderRemainingSeasonSources(player);
  }

  function renderDeferredEvidence(message) {
    renderProviderHeader();
    const body = $("playerLabWeeklyBody");
    const row = document.createElement("tr");
    const cell = node("td", "player-lab-no-results", message);
    cell.colSpan = 5 + (outlook?.providers.length || 0);
    row.append(cell);
    body.replaceChildren(row);
    $("playerLabRosSources").replaceChildren();
    $("playerLabChart").replaceChildren();
    $("playerLabSeasonStats").replaceChildren();
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
    const snapshot = outlook.profile_snapshot;
    if (!snapshot) {
      container.append(freshnessCard(
        "Public player profiles",
        null,
        null,
        "Not retained in this legacy bundle · projection-only Player Lab"
      ));
      return;
    }
    for (const source of array(snapshot.provenance)) {
      container.append(freshnessCard(
        `${providerLabel(source.provider)} · ${humanize(source.dataset)}`,
        source.captured_at,
        source.source_updated_at,
        `${statusLabel(source.status)} · ${Number.isFinite(source.byte_count) ? `${integerFormatter.format(source.byte_count)}-byte source response size at capture` : "Source response size unavailable"}`
      ));
    }
    if (array(snapshot.materialization_issues).length) {
      const issue = node("article", "player-lab-freshness-card player-lab-source-warning");
      issue.append(
        node("strong", "", "Identity conflicts quarantined"),
        node("span", "player-lab-muted", `${integerFormatter.format(snapshot.materialization_issues.length)} public rows were withheld instead of guessed.`)
      );
      container.append(issue);
    }
  }

  for (const [id, eventName] of Object.entries(FILTER_CONTROL_EVENTS)) {
    const control = $(id);
    if (control) control.addEventListener(eventName, () => {
      if (outlook) {
        currentPage = 1;
        render();
      }
    });
  }

  $("playerLabPreviousPage")?.addEventListener("click", () => {
    if (!outlook || currentPage <= 1) return;
    currentPage -= 1;
    render();
  });
  $("playerLabNextPage")?.addEventListener("click", () => {
    if (!outlook || currentPage >= catalogUi.pageCount(visiblePlayers)) return;
    currentPage += 1;
    render();
  });
  $("playerLabPageSize")?.addEventListener("change", () => {
    if (!outlook) return;
    currentPage = 1;
    render();
  });

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

  function activateWorkspace(name) {
    const playerActive = name === "players";
    activeWorkspace = playerActive ? "players" : "trade";
    $("tradeWorkspace")?.classList.toggle("hidden", playerActive);
    $("playerLabWorkspace")?.classList.toggle("hidden", !playerActive);
    for (const [id, active] of [["tradeTab", !playerActive], ["playerLabTab", playerActive]]) {
      const button = $(id);
      button?.classList.toggle("active", active);
      button?.setAttribute("aria-selected", String(active));
      if (button) button.tabIndex = active ? 0 : -1;
    }
    if (playerActive) {
      $("playerLabSearch")?.focus();
      if (outlook) {
        renderDetail();
        void loadSelectedPlayerDetail();
      }
      void loadPendingBundle();
    } else {
      cancelDetailRequest();
      if (activeController) {
        clearOutlook("Open Player Lab to resume loading this week's player profiles.");
      }
    }
  }

  $("tradeTab")?.addEventListener("click", () => activateWorkspace("trade"));
  $("playerLabTab")?.addEventListener("click", () => activateWorkspace("players"));
  for (const button of [$("tradeTab"), $("playerLabTab")].filter(Boolean)) {
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const showPlayers = event.key === "ArrowRight" || event.key === "End";
      activateWorkspace(showPlayers ? "players" : "trade");
      $(showPlayers ? "playerLabTab" : "tradeTab").focus();
    });
  }
  $("playerLabTab")?.setAttribute("tabindex", "-1");

  return Object.freeze({activateWorkspace, queueBundle, reset});
})();
