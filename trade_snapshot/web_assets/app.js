"use strict";

const token = document.querySelector('meta[name="app-token"]').content;
const browserClientId = crypto.randomUUID();
const $ = id => document.getElementById(id);
let bundleRows = [];
let activeBundle = null;
let bundles = [];
let activeJob = null;
let activeCollection = null;
let activeCollectionClock = null;
let activeSearchClock = null;
let collectionLaunching = false;
let collectionAvailable = false;
let extensionConnected = false;
let extensionPairing = false;
let extensionPairAcknowledged = false;
let extensionPairFailure = null;
let extensionPairHint = null;
let extensionDisconnectReason = null;
let extensionStatusGeneration = 0;
let threeTeamEstimateSignature = null;
let searchRunning = false;
let activeSearchFormat = "two_team";
let activeSearchTeamIds = [];
let loadedResults = null;
let loadedResultTeamIds = [];
let loadedResultJobId = null;
let loadedResultBundle = null;
let bundleLoadGeneration = 0;
let bundleCatalogGeneration = 0;
let resultLoadGeneration = 0;
let activeInsight = null;
let dashboardBundleId = null;
let gmInsightsBundleId = null;
let tradeTimingBundleId = null;
let playerLabBundleId = null;
let exportBusy = false;
let draftWorkBusy = false;
let publishedTradeBusy = null;
let recoveredSearchScopeOnly = false;
let sourceDebugTimer = null;
const heartbeatInterval = 20000;
const extensionProtocolVersion = 1;
const minimumHistoryExtensionVersion = [0, 2, 1, 0];

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-FTE-Token", token);
  headers.set("X-FTE-Client", browserClientId);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {...options, headers});
  const type = response.headers.get("Content-Type") || "";
  const value = type.includes("application/json") ? await response.json() : await response.blob();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}

async function pingLifecycle() {
  try { await api("/api/session/ping", {method: "POST", body: ""}); }
  catch (_) { /* The regular health and job requests own visible error reporting. */ }
}

window.addEventListener("pagehide", event => {
  if (event.persisted) return;
  fetch("/api/session/close", {
    method: "POST",
    headers: {"X-FTE-Token": token, "X-FTE-Client": browserClientId},
    body: "",
    keepalive: true
  }).catch(() => {});
});
setInterval(pingLifecycle, heartbeatInterval);

window.addEventListener("message", event => {
  const value = event.data;
  if (event.source !== window || event.origin !== window.location.origin ||
      !value || typeof value !== "object" || Array.isArray(value) ||
      value.source !== "fantasy-trade-evaluator-extension" ||
      value.protocol_version !== extensionProtocolVersion ||
      typeof value.type !== "string") return;
  if (value.type === "bridge.ready") return;
  if (value.type === "pair.pending") {
    extensionPairAcknowledged = true;
    return;
  }
  if (value.type === "pair.accepted") {
    extensionPairAcknowledged = true;
    extensionDisconnectReason = null;
    void refreshExtensionStatus();
    return;
  }
  if (value.type === "pair.rejected" || value.type === "pair.expired") {
    extensionPairFailure = "The extension did not accept this connection. Try Connect extension again.";
    return;
  }
  if (value.type === "session.closed") {
    extensionDisconnectReason = typeof value.reason === "string" &&
      /^[a-z0-9_]{1,64}$/.test(value.reason) ? value.reason : "unknown_disconnect";
    void refreshExtensionStatus();
  }
});

function showError(error) {
  const banner = $("errorBanner");
  banner.textContent = error instanceof Error ? error.message : String(error);
  banner.classList.remove("hidden");
  window.scrollTo({top: 0, behavior: "smooth"});
}

function clearError() { $("errorBanner").classList.add("hidden"); }
function selectedValues(element) { return [...element.selectedOptions].map(option => option.value); }
function numberValue(id, nullable = false) {
  const value = $(id).value.trim();
  return nullable && value === "" ? null : Number(value);
}
function compactNumber(value) {
  if (typeof value === "string" && /^\d+$/.test(value)) {
    return new Intl.NumberFormat(undefined, {maximumFractionDigits: 0}).format(BigInt(value));
  }
  return new Intl.NumberFormat(undefined, {maximumFractionDigits: 0}).format(value);
}

const powerEvidenceLabels = Object.freeze({
  holdout_validated: "Holdout-validated shape",
  extrapolated: "Extrapolated",
  surrogate: "Surrogate",
  surrogate_extrapolated: "Surrogate extrapolation"
});

function powerEvidenceLabel(value) {
  return powerEvidenceLabels[value] || String(value).replaceAll("_", " ");
}
function percent(value) { return new Intl.NumberFormat(undefined, {style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1}).format(value); }
function signed(value, asPercent = false) {
  const text = asPercent ? percent(Math.abs(value)) : Math.abs(value).toFixed(1);
  return `${value >= 0 ? "+" : "−"}${text}`;
}

function sourceStatusLabel(status) {
  return {
    required: "REQUIRED FOR THIS MODE",
    best_effort: "AUTO · USED IF VALID",
    reference: "REFERENCE ONLY",
    off: "OFF"
  }[status] || status;
}

function renderSourceCatalog(catalog) {
  const container = $("sourceDebugList");
  container.replaceChildren();
  const preview = catalog.weekly_projection_preview || {};
  const weeks = Array.isArray(preview.weeks) ? preview.weeks : [];
  const weekScope = preview.scope === "remaining_nfl_weeks"
    ? ` Weekly page routes are previewed for weeks ${weeks.join(", ")}; collection stops at your league's regular-season endpoint after ESPN supplies it.`
    : weeks.length
      ? ` Weekly pages are collected for week ${weeks[0]} only.`
      : "";
  $("sourceDebugMode").textContent = `${catalog.mode === "independent"
    ? "Independent power mode: FantasyPros is neither opened nor required."
    : "FantasyPros power mode: blind-holdout-validated or explicitly accepted surrogate replication is attempted."} ${
      catalog.projection_mode === "broad_consensus"
        ? "Forecasts equal-average independent publishers; composite products are excluded from that arithmetic."
        : "Broad consensus is off; the core FantasyPros, ESPN, and Yahoo ensemble is used."
    }${weekScope}`;
  const groups = [
    ["Projection and league sources", catalog.calculation_sources],
    ["Player profile sources", catalog.profile_sources || []],
    ["Method and ranking references", catalog.reference_sources]
  ];
  for (const [title, group] of groups) {
    if (!group.length) continue;
    const groupHeading = document.createElement("h3");
    groupHeading.className = "source-debug-group-heading";
    groupHeading.textContent = title;
    container.append(groupHeading);
    for (const source of group) {
      const card = document.createElement("section");
      card.className = `source-debug-card source-${source.status}`;
      const heading = document.createElement("div");
      heading.className = "source-debug-heading";
      const name = document.createElement("strong");
      name.textContent = source.provider;
      const badge = document.createElement("span");
      badge.className = "source-debug-badge";
      badge.textContent = sourceStatusLabel(source.status);
      heading.append(name, badge);
      const note = document.createElement("p");
      note.textContent = source.note;
      card.append(heading, note);
      const links = document.createElement("div");
      links.className = "source-debug-links";
      source.urls.forEach((url, index) => {
        const link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = source.urls.length === 1
          ? url
          : `${index + 1}. ${url}`;
        links.append(link);
      });
      if (source.urls.length > 4) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = `Show ${source.urls.length} selected URLs`;
        details.append(summary, links);
        card.append(details);
      } else {
        card.append(links);
      }
      container.append(card);
    }
  }
}

async function refreshSourceDebug() {
  try {
    const catalog = await api("/api/weekly-sources", {
      method: "POST",
      body: JSON.stringify(collectionPayload())
    });
    renderSourceCatalog(catalog);
  } catch (error) {
    $("sourceDebugMode").textContent = error.message;
    $("sourceDebugList").replaceChildren();
  }
}

function scheduleSourceDebugRefresh() {
  clearTimeout(sourceDebugTimer);
  sourceDebugTimer = setTimeout(refreshSourceDebug, 250);
}

function syncCollectionMode() {
  const useFantasyPros = $("useFantasyPros").checked;
  $("hostLeagueUrl").required = !useFantasyPros;
  $("espnLinkRequirement").textContent = useFantasyPros ? "(recommended)" : "(required)";
  $("allowSurrogatePower").disabled = !useFantasyPros;
  if (!useFantasyPros) $("allowSurrogatePower").checked = false;
  const broad = $("useBroadConsensus").checked;
  const projectionText = broad
    ? "Forecasts equal-average ESPN, Yahoo, and every accepted public publisher once. CBS, FFToday, and FantasySharks are attempted automatically from the built-in source catalog."
    : useFantasyPros
      ? "Forecasts use the established FantasyPros, ESPN, and Yahoo core ensemble."
      : "Forecasts use the independent ESPN and Yahoo core ensemble.";
  $("collectionModeHelp").textContent = useFantasyPros
    ? `FantasyPros supplies ECR and calibration evidence; the saved ESPN connection supplies league rules, rosters, standings, and schedules; the saved Yahoo connection supplies league-scored projections. ${projectionText} Only numeric league identifiers are retained.`
    : `FantasyPros will not be opened or required. ESPN supplies the league, custom lineup/IR rules, standings, and schedules; Yahoo supplies league-scored projections. ${projectionText} Power is produced by the transparent independent model.`;
  updateActivityControls();
  scheduleSourceDebugRefresh();
}

function currentBundle() {
  return activeBundle?.bundle_id === $("bundleSelect").value ? activeBundle : null;
}

function tradeFormat() {
  return document.querySelector('input[name="tradeFormat"]:checked')?.value || "two_team";
}

function isThreeTeam() { return ThreeWayUi.isSelected(); }

function selectedCounterpartyIds() {
  if (!isThreeTeam()) return selectedValues($("counterparties"));
  return ThreeWayUi.selectedCounterpartyIds(currentBundle());
}

function activeCounterpartyTeams(bundle) {
  const selected = new Set(selectedCounterpartyIds());
  return bundle.teams.filter(team =>
    team.team_id !== $("primaryTeam").value &&
      (isThreeTeam() ? selected.has(team.team_id) : (!selected.size || selected.has(team.team_id)))
  );
}

function populatePackageFilters() {
  const bundle = currentBundle();
  TradeFilterUi.populate({
    bundle,
    primaryTeamId: $("primaryTeam").value,
    incomingTeams: bundle ? activeCounterpartyTeams(bundle) : []
  });
}

function extensionVersionAtLeast(value, minimum) {
  if (typeof value !== "string") return false;
  const rawParts = value.split(".");
  if (rawParts.length < 1 || rawParts.length > 4) return false;
  if (rawParts.some(part =>
    !/^\d+$/.test(part) ||
    (part.length > 1 && part.startsWith("0")) ||
    Number(part) > 65535
  )) return false;
  const parts = rawParts.map(Number);
  while (parts.length < minimum.length) parts.push(0);
  for (let index = 0; index < minimum.length; index += 1) {
    if (parts[index] !== minimum[index]) return parts[index] > minimum[index];
  }
  return true;
}

function renderExtensionStatus(status) {
  const paired = Boolean(status && status.state === "paired");
  const updateRequired = paired && !extensionVersionAtLeast(
    status.extension_version,
    minimumHistoryExtensionVersion
  );
  extensionConnected = paired && !updateRequired;
  const pairing = extensionPairing;
  let statusText = "Browser extension not connected";
  let helpText = "Install or reload the downloaded extension, refresh this page once, then click Connect extension before collecting weekly data.";
  let buttonText = "Connect extension";
  if (updateRequired) {
    statusText = `Browser extension update required${status.extension_version ? ` · ${status.extension_version}` : ""}`;
    helpText = "Install the newly downloaded extension, click Reload for it on the browser’s Extensions page, refresh this app, then reconnect. Version 0.2.1 or newer is required for reliable collection and verified league history.";
    buttonText = "Reconnect updated extension";
  } else if (extensionConnected) {
    statusText = `Browser extension connected${status.extension_version ? ` · ${status.extension_version}` : ""}`;
    helpText = "Ready to scan through one temporary tab in this signed-in browser. Cookies never leave the browser.";
    buttonText = "Connected";
  } else if (pairing) {
    statusText = "Approve pairing in the extension…";
    helpText = "Open Chrome’s Extensions menu, choose Fantasy Trade Evaluator Browser Bridge, and click Pair with app.";
    buttonText = "Waiting for approval";
  } else if (extensionDisconnectReason) {
    const explanations = {
      complete: "The previous scan finished and released its browser connection.",
      cancelled: "The previous scan was cancelled.",
      scan_tab_closed: "The temporary scan tab was closed before the scan finished.",
      app_tab_closed: "The app tab used for pairing was closed.",
      app_tab_origin_changed: "The app tab used for pairing navigated away from this app.",
      invalid_command_response: "The installed extension rejected an app command. Reload the current extension before reconnecting.",
      bridge_request_rejected: "The app rejected the extension session. This can happen after the app restarts.",
      bridge_unreachable: "The extension could not reach the local app after retrying.",
      result_delivery_failed: "The extension could not deliver the scan result to the local app."
    };
    helpText = `${explanations[extensionDisconnectReason] || "The browser connection ended."} ` +
      `Connect extension to retry. Connection detail: ${extensionDisconnectReason}.`;
  }
  $("extensionDot").classList.toggle("connected", extensionConnected);
  $("extensionStatus").textContent = statusText;
  $("extensionHelp").textContent = helpText;
  $("connectExtensionButton").disabled = ProgressUi.isBusy() || extensionConnected || pairing;
  $("connectExtensionButton").textContent = buttonText;
  const pairCode = $("extensionPairCode");
  const showPairCode = pairing && typeof extensionPairHint === "string";
  pairCode.textContent = showPairCode
    ? `Pairing code ends in ${extensionPairHint} — confirm the same code in the extension.`
    : "";
  pairCode.classList.toggle("hidden", !showPairCode);
  updateActivityControls();
}

async function refreshExtensionStatus() {
  const generation = ++extensionStatusGeneration;
  try {
    const status = await api("/api/browser-extension/status");
    if (generation !== extensionStatusGeneration) return null;
    renderExtensionStatus(status);
    return status;
  } catch (_) {
    if (generation === extensionStatusGeneration) renderExtensionStatus(null);
    return null;
  }
}

async function connectExtension() {
  clearError();
  const button = $("connectExtensionButton");
  button.disabled = true;
  extensionPairAcknowledged = false;
  extensionPairFailure = null;
  extensionPairHint = null;
  try {
    const offer = await api("/api/browser-extension/pairing", {method: "POST", body: ""});
    extensionPairHint = offer.pair_code.slice(-4);
    window.postMessage({
      source: "fantasy-trade-evaluator-app",
      protocol_version: extensionProtocolVersion,
      type: "pair.request",
      app_origin: window.location.origin,
      pair_code: offer.pair_code
    }, window.location.origin);
    extensionPairing = true;
    renderExtensionStatus({state: "pairing"});
    const deadline = Date.now() + Math.min(120000, offer.expires_in_seconds * 1000);
    const detectionDeadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 250));
      if (extensionPairFailure) throw new Error(extensionPairFailure);
      const status = await refreshExtensionStatus();
      if (status && status.state === "paired") {
        extensionPairing = false;
        extensionPairHint = null;
        renderExtensionStatus(status);
        return;
      }
      if (!extensionPairAcknowledged && Date.now() >= detectionDeadline) {
        throw new Error(
          "The extension is not active on this page yet. If you just installed or reloaded it, refresh this Trade Evaluator page (Ctrl+R on Windows/Linux or Cmd+R on Mac), then click Connect extension again."
        );
      }
    }
    throw new Error("The pairing request expired. Try Connect extension again.");
  } catch (error) {
    extensionPairing = false;
    extensionPairAcknowledged = false;
    extensionPairFailure = null;
    extensionPairHint = null;
    button.disabled = false;
    await refreshExtensionStatus();
    showError(error);
  }
}

async function refreshBundles(selectId = null) {
  const generation = ++bundleCatalogGeneration;
  const path = LeagueUi.bundleCatalogPath();
  const select = $("bundleSelect");
  const previous = select.value;
  ++bundleLoadGeneration;
  ++resultLoadGeneration;
  bundleRows = [];
  activeBundle = null;
  bundles = [];
  activeJob = null;
  loadedResults = null;
  loadedResultTeamIds = [];
  loadedResultJobId = null;
  loadedResultBundle = null;
  collectionAvailable = false;
  select.replaceChildren(new Option(
    path ? "Loading weekly history…" : "No league selected",
    ""
  ));
  select.disabled = true;
  $("collectButton").disabled = true;
  $("progressPanel").classList.add("hidden");
  $("resultsPanel").classList.add("hidden");
  renderBundle();
  const response = path
    ? await ProgressUi.run(
        "bundle-catalog",
        "Checking this league's weekly history",
        () => api(path)
      )
    : {
        bundles: [],
        readiness: {
          ready: false,
          collection_available: false,
          message: "Add or choose a league workspace to begin."
        }
      };
  if (generation !== bundleCatalogGeneration) return;
  bundleRows = response.bundles.filter(item => item.status === "ready");
  collectionAvailable = response.readiness.collection_available;
  renderReadiness(response.readiness);
  select.replaceChildren(new Option(
    bundleRows.length
      ? "Choose a ready week"
      : path
        ? "No weekly bundle yet"
        : "No league selected",
    ""
  ));
  for (const bundle of bundleRows) {
    const mode = bundle.power_engine_mode === "surrogate"
      ? " · SURROGATE"
      : bundle.power_engine_mode === "independent"
        ? " · independent"
        : " · blind-holdout validated";
    const league = bundle.league_label ? `${bundle.league_label} · ` : "";
    const option = new Option(
      `${league}${bundle.season} · Week ${bundle.week} · ${bundle.team_count} teams${mode}`,
      bundle.bundle_id
    );
    select.add(option);
  }
  select.disabled = bundleRows.length === 0;
  select.value = selectId || previous;
  if (
    !select.value
    && bundleRows.length === 1
    && bundleRows[0].power_engine_mode !== "surrogate"
  ) {
    select.value = bundleRows[0].bundle_id;
  }
  const profile = LeagueUi.selectedProfile();
  $("collectButton").title = collectionAvailable
    ? ""
    : profile?.archived
      ? "Restore this league before collecting a new week."
      : profile && !profile.yahoo_league_id
        ? "Add the Yahoo connection in League settings."
        : profile && !profile.espn_league_id
          ? "Add the ESPN connection in League settings for independent mode."
          : "Choose an active league workspace to collect a new week.";
  renderLeagueCollectionSummary();
  await changeBundle();
}

function renderReadiness(readiness) {
  const row = $("readiness");
  row.textContent = readiness.message;
  row.className = `readiness ${readiness.ready ? "ready" : "not-ready"}`;
}

function renderLeagueCollectionSummary() {
  const profile = LeagueUi.selectedProfile();
  if (profile) {
    $("collectionLeagueName").textContent = profile.name;
    const connections = [
      profile.espn_league_id ? "ESPN saved" : "ESPN not connected",
      profile.yahoo_league_id ? "Yahoo saved" : "Yahoo not connected"
    ].join(" · ");
    $("collectionLeagueDetails").textContent = `${profile.season} · ${profile.scoring} scoring · ${connections}${profile.archived ? " · archived, view only" : ""}`;
  } else if (LeagueUi.isUnassigned()) {
    $("collectionLeagueName").textContent = "Unassigned imports";
    $("collectionLeagueDetails").textContent = "Choose or add a league to scan a new week.";
  } else {
    $("collectionLeagueName").textContent = "Choose a league workspace";
    $("collectionLeagueDetails").textContent = "Its saved season, scoring, ESPN, and Yahoo connections will be used.";
  }
}

const capabilityLabels = {
  fantasypros_style_power: "Trade power",
  trade_search: "Trade search",
  expected_standings: "Expected standings",
  playoff_model_estimates: "Playoff model estimates",
  player_lab: "Player lab",
  team_outlook_and_exports: "Team outlook and exports",
  fantasypros_comparison_benchmark: "FantasyPros comparison benchmark",
  exact_championship_simulation: "Exact championship simulation"
};

const capabilityStatusLabels = {
  ready_with_holdout_validated_scope: "Ready in holdout-validated scope",
  ready_with_limitations: "Ready with limitations",
  model_estimate_with_limitations: "Model estimate with limitations",
  surrogate: "Approximate",
  comparison_only: "Comparison only",
  not_ready: "Not available"
};

function appendTextElement(parent, tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = value;
  parent.append(element);
  return element;
}

function renderCurrentPlayerProjectionAudit(content, audit) {
  if (!audit || typeof audit !== "object") return;
  const card = appendTextElement(content, "article", "bundle-capability", "");
  const heading = appendTextElement(card, "div", "bundle-capability-heading", "");
  appendTextElement(heading, "h3", "", "Current player projection audit");
  const complete = audit.status === "complete";
  appendTextElement(
    heading,
    "span",
    `bundle-capability-status status-${complete ? "ready-with-attested-scope" : "not-ready"}`,
    complete ? "Complete for declared scope" : audit.status === "unavailable" ? "Reference unavailable" : "Incomplete"
  );
  const counts = audit.counts || {};
  const positions = Array.isArray(audit.configured_positions)
    ? audit.configured_positions.join(", ")
    : "unknown";
  appendTextElement(
    card,
    "p",
    "bundle-readiness-overview",
    `Sleeper active-player reference · scope ${positions} · ${Number(counts.reference_count || 0)} ` +
      `in-scope identities, ${Number(counts.projected_count || 0)} with complete numeric ` +
      `remaining-season projections, ${Number(counts.missing_count || 0)} missing, ` +
      `${Number(counts.unmatched_count || 0)} unmatched, and ` +
      `${Number(counts.unsupported_count || 0)} active rows outside the declared scope.`
  );
  appendTextElement(
    card,
    "p",
    "bundle-capability-fallback",
    audit.counting_policy || "Fetched reference rows and usable projections are counted separately."
  );
  const rows = Array.isArray(audit.coverage_rows)
    ? audit.coverage_rows.filter(row => row && row.position === "ALL")
    : [];
  if (rows.length) {
    const list = appendTextElement(card, "ul", "", "");
    for (const row of rows) {
      const horizon = row.horizon === "current_week" ? `week ${audit.current_week}` : "remaining season";
      appendTextElement(
        list,
        "li",
        "",
        `${String(row.provider)} ${horizon}: ${Number(row.covered_count || 0)} of ` +
          `${Number(row.reference_count || 0)} covered, ${Number(row.projected_count || 0)} ` +
          `numeric, ${Number(row.missing_count || 0)} missing, ${Number(row.unmatched_count || 0)} unmatched.`
      );
    }
  }
  const rosterMissing = Array.isArray(audit.rostered_players_missing_current_projection)
    ? audit.rostered_players_missing_current_projection
    : [];
  const providerMissing = Array.isArray(audit.rostered_players_missing_provider_current_projection)
    ? audit.rostered_players_missing_provider_current_projection
    : [];
  appendTextElement(
    card,
    "p",
    "bundle-readiness-overview",
    rosterMissing.length
      ? `Rostered players missing an ensemble projection for week ${audit.current_week}: ${rosterMissing.map(row => row.display_name).join(", ")}.`
      : `All ${Number(audit.rostered_player_count || 0)} rostered players have an ensemble projection or verified bye for week ${audit.current_week}.`
  );
  if (providerMissing.length) {
    appendTextElement(
      card,
      "p",
      "bundle-capability-fallback",
      `Rostered provider gaps: ${providerMissing.map(row => `${row.display_name} (${row.missing_providers.join(", ")})`).join("; ")}.`
    );
  }
  if (Array.isArray(audit.limitations) && audit.limitations.length) {
    const limitations = appendTextElement(card, "ul", "", "");
    for (const note of audit.limitations) appendTextElement(limitations, "li", "", note);
  }
}

function renderBundleDataReadiness(bundle) {
  const panel = $("bundleDataReadiness");
  const content = $("bundleDataReadinessContent");
  content.replaceChildren();
  const report = bundle && bundle.data_readiness;
  if (!report || typeof report !== "object") {
    panel.classList.add("hidden");
    panel.open = false;
    return;
  }

  const coverage = report.coverage || {};
  const available = Number(coverage.available_full_horizon_provider_cells || 0);
  const total = Number(coverage.full_horizon_provider_cells || 0);
  const direct = Number(coverage.direct_provider_cells || 0);
  const derived = Number(coverage.ros_derived_provider_cells || 0);
  const unavailable = Number(coverage.unavailable_provider_cells || 0);
  const captureRange = coverage.earliest_capture_at === coverage.latest_capture_at
    ? coverage.earliest_capture_at
    : `${coverage.earliest_capture_at} to ${coverage.latest_capture_at}`;
  appendTextElement(
    content,
    "p",
    "bundle-readiness-overview",
    `${bundle.league_label} · ${available} of ${total} provider/player remaining-season projections are complete. ` +
      `Weekly values include ${direct} source-published, ${derived} derived from captured ` +
      `rest-of-season totals, and ${unavailable} unavailable cells. Captures span ${captureRange}.`
  );
  const projectionSources = coverage.projection_sources || {};
  const sourceFormats = Array.isArray(projectionSources.source_scoring_formats)
    ? projectionSources.source_scoring_formats.join(", ")
    : "unknown";
  appendTextElement(
    content,
    "p",
    "bundle-readiness-overview",
    `${Number(projectionSources.source_count || 0)} retained projection source artifacts ` +
      `(${Number(projectionSources.captured_attempts || 0)} captured attempts, ` +
      `${Number(projectionSources.not_published_attempts || 0)} not published, ` +
      `${Number(projectionSources.unavailable_attempts || 0)} unavailable). ` +
      `Source scoring: ${sourceFormats}; ${Number(projectionSources.provider_total_sources || 0)} ` +
      `provider-total/base-format sources and ` +
      `${Number(projectionSources.locally_recomputed_sources || 0)} locally recomputed sources.`
  );
  renderCurrentPlayerProjectionAudit(
    content,
    coverage.current_player_projection_audit
  );

  const grid = appendTextElement(content, "div", "bundle-capability-grid", "");
  for (const [key, capability] of Object.entries(report.capabilities || {})) {
    const item = appendTextElement(grid, "article", "bundle-capability", "");
    const heading = appendTextElement(item, "div", "bundle-capability-heading", "");
    appendTextElement(heading, "h3", "", capabilityLabels[key] || key.replaceAll("_", " "));
    appendTextElement(
      heading,
      "span",
      `bundle-capability-status status-${String(capability.status || "unknown").replaceAll("_", "-")}`,
      capabilityStatusLabels[capability.status] || String(capability.status || "Unknown")
    );
    const notes = [...(capability.limitations || []), ...(capability.missing || [])];
    if (notes.length) {
      const list = appendTextElement(item, "ul", "", "");
      for (const note of notes) appendTextElement(list, "li", "", note);
    }
    if (capability.available_fallback) {
      appendTextElement(
        item,
        "p",
        "bundle-capability-fallback",
        `Available fallback: ${String(capability.available_fallback).replaceAll("_", " ")}.`
      );
    }
  }
  panel.classList.remove("hidden");
}

function bundleCapability(bundle, key) {
  const capability = bundle?.data_readiness?.capabilities?.[key];
  return capability && typeof capability === "object" ? capability : null;
}

function bundleCapabilityIsUsable(bundle, key) {
  const capability = bundleCapability(bundle, key);
  return Boolean(capability) && capability.status !== "not_ready";
}

function bundleCapabilityBlockMessage(bundle, key, fallback) {
  const capability = bundleCapability(bundle, key);
  const missing = Array.isArray(capability?.missing) ? capability.missing.filter(Boolean) : [];
  return missing.length ? `${fallback}: ${missing.join(" ")}` : fallback;
}

function renderBundle() {
  const bundle = currentBundle();
  renderBundleDataReadiness(bundle);
  const primary = $("primaryTeam");
  const others = $("counterparties");
  primary.replaceChildren();
  others.replaceChildren();
  if (!bundle) {
    $("bundleSummary").textContent = "Import or collect a weekly bundle to begin.";
    $("bundleSummary").classList.remove("surrogate-warning");
    $("bundleSummary").classList.remove("independent-notice");
    $("estimateButton").disabled = true;
    $("surrogateSearchConsentRow").classList.add("hidden");
    $("acceptSurrogateSearch").checked = false;
    threeTeamEstimateSignature = null;
    ThreeWayUi.syncPartnerOptions(null, "");
    ThreeWayUi.syncFormatControls(null);
    $("assignBundleControls").classList.add("hidden");
    $("skipSmall").disabled = false;
    $("skipSmall").title = "";
    populatePackageFilters();
    updateSearchStartButton();
    return;
  }
  for (const team of bundle.teams) {
    primary.add(new Option(team.name, team.team_id));
    others.add(new Option(team.name, team.team_id));
  }
  const savedTeam = LeagueUi.selectedProfile()?.my_team_id;
  if (savedTeam && [...primary.options].some(option => option.value === savedTeam)) {
    primary.value = savedTeam;
  }
  const surrogate = bundle.power_engine_mode === "surrogate";
  const independent = bundle.power_engine_mode === "independent";
  const forecast = bundle.forecast_provider_names.join(" + ").toUpperCase();
  const forecastLabel = bundle.forecast_mode === "broad_consensus"
    ? `forecast consensus: ${forecast}`
    : bundle.forecast_mode === "core_ensemble"
      ? `core forecast ensemble: ${forecast}`
      : `single-source forecast: ${forecast}`;
  const summary = $("bundleSummary");
  if (surrogate) {
    const error = bundle.methodology.holdout_max_absolute_score_error;
    const match = percent(bundle.methodology.holdout_display_match_rate);
    summary.textContent = `${bundle.season} week ${bundle.week} · ${bundle.team_count} teams · SURROGATE / APPROXIMATE POWER · ${forecastLabel} · Blind max score error ${error}; display match ${match}. ${bundle.power_engine_notice}`;
    summary.classList.add("surrogate-warning");
    summary.classList.remove("independent-notice");
  } else if (independent) {
    summary.textContent = `${bundle.season} week ${bundle.week} · ${bundle.team_count} teams · INDEPENDENT LOCAL POWER · ${forecastLabel}. ${bundle.power_engine_notice}`;
    summary.classList.remove("surrogate-warning");
    summary.classList.add("independent-notice");
  } else {
    const sizes = bundle.methodology.validated_balanced_package_sizes.join(", ");
    summary.textContent = `${bundle.season} week ${bundle.week} · ${bundle.team_count} teams · ${forecastLabel} · FantasyPros-power method passed representative blind holdouts for balanced ${sizes}-player package shapes without adds/drops; this is not exhaustive proof, and other shapes are labeled extrapolated`;
    summary.classList.remove("surrogate-warning");
    summary.classList.remove("independent-notice");
  }
  $("surrogateSearchConsentRow").classList.toggle("hidden", !surrogate);
  $("acceptSurrogateSearch").checked = false;
  $("skipSmall").disabled = independent;
  if (independent) $("skipSmall").checked = false;
  $("skipSmall").title = independent
    ? "The independent engine has no FantasyPros small-trade exclusion."
    : "";
  $("estimateButton").disabled = Boolean(activeCollection);
  const canAssign = LeagueUi.isUnassigned()
    && $("assignLeagueSelect").options.length > 1;
  $("assignBundleControls").classList.toggle("hidden", !canAssign);
  const tradeSearchReady = bundleCapabilityIsUsable(bundle, "trade_search");
  $("estimateButton").disabled = Boolean(activeCollection);
  $("estimateButton").title = "Count the candidate space without running player-value or playoff calculations.";
  if (!tradeSearchReady) {
    $("estimate").textContent = bundleCapabilityBlockMessage(
      bundle,
      "trade_search",
      "Trade search is unavailable for this weekly bundle"
    );
  }
  syncCounterparties();
  ThreeWayUi.syncFormatControls(bundle);
  updateSearchStartButton();
}

function changeBundle() {
  return loadSelectedBundle().catch(showError);
}

async function loadSelectedBundle() {
  clearError();
  const generation = ++bundleLoadGeneration;
  const bundleId = $("bundleSelect").value;
  recoveredSearchScopeOnly = false;
  activeBundle = null;
  bundles = [];
  activeJob = null;
  loadedResults = null;
  loadedResultTeamIds = [];
  loadedResultJobId = null;
  loadedResultBundle = null;
  ++resultLoadGeneration;
  $("progressPanel").classList.add("hidden");
  $("resultsPanel").classList.add("hidden");
  $("progressBar").style.width = "0%";
  $("progressStats").textContent = "";
  $("estimate").textContent = "Choose a ready week, then count the combinations.";
  threeTeamEstimateSignature = null;
  renderBundle();
  if (bundleId) {
    const loaded = await ProgressUi.run(
      "bundle-load",
      "Loading the selected weekly engine",
      () => api(`/api/bundles/${encodeURIComponent(bundleId)}`)
    );
    if (
      generation !== bundleLoadGeneration
      || $("bundleSelect").value !== bundleId
    ) return;
    activeBundle = loaded;
    bundles = [loaded];
  }
  renderBundle();
  dashboardBundleId = null;
  gmInsightsBundleId = null;
  tradeTimingBundleId = null;
  playerLabBundleId = null;
  DashboardUi.reset(
    currentBundle()
      ? "Calculate this week's league outlook when you are ready."
      : undefined
  );
  GmInsightsUi.reset(
    currentBundle()
      ? "Open GM Insights when you want current roster fit and verified league history."
      : "Select a ready week, then open GM Insights when you want current roster fit and verified league history."
  );
  TradeTimingUi.reset(
    currentBundle()
      ? "Calculate trade timing when you want simulated proposal windows."
      : "Select a ready week, then calculate trade timing when you are ready."
  );
  PlayerLabUi.activateWorkspace("trade");
  void PlayerLabUi.queueBundle(currentBundle(), {request: api, onError: showError});
  updateActivityControls();
}

function updateActivityControls() {
  const bundle = currentBundle();
  const tradeBusy = searchRunning || collectionLaunching || Boolean(activeCollection) || Boolean(activeInsight) || exportBusy;
  const busy = tradeBusy || draftWorkBusy;
  $("bundleFile").disabled = busy;
  $("bundleSelect").disabled = bundleRows.length === 0 || busy;
  $("dashboardLoadButton").disabled = !bundle || busy;
  $("gmInsightsLoadButton").disabled = !bundle || busy;
  $("tradeTimingLoadButton").disabled = !bundle || busy;
  $("playerLabLoadButton").disabled = !bundle || busy;
  $("dashboardLoadButton").textContent =
    bundle && dashboardBundleId === bundle.bundle_id ? "Refresh league outlook" : "Calculate league outlook";
  $("gmInsightsLoadButton").textContent =
    bundle && gmInsightsBundleId === bundle.bundle_id ? "Refresh GM Insights" : "Open GM Insights";
  $("tradeTimingLoadButton").textContent =
    bundle && tradeTimingBundleId === bundle.bundle_id ? "Refresh trade timing" : "Calculate trade timing";
  $("playerLabLoadButton").textContent =
    bundle && playerLabBundleId === bundle.bundle_id ? "Refresh Player Lab" : "Open Player Lab";
  $("collectButton").disabled =
    !collectionAvailable || !extensionConnected || !LeagueUi.canCollect() || busy;
  LeagueUi.setBusy(tradeBusy);
  window.TradeAppBusy = tradeBusy;
  if (publishedTradeBusy !== tradeBusy) {
    publishedTradeBusy = tradeBusy;
    window.dispatchEvent(new CustomEvent("tradeactivitychange", {detail: {busy: tradeBusy}}));
  }
}

function setSearchRunning(running) {
  searchRunning = running;
  for (const element of $("searchForm").elements) {
    if (element.id === "cancelButton") continue;
    if (running && element.dataset.disabledBeforeSearch === undefined) {
      element.dataset.disabledBeforeSearch = element.disabled ? "true" : "false";
      element.disabled = true;
    } else if (!running && element.dataset.disabledBeforeSearch !== undefined) {
      element.disabled = element.dataset.disabledBeforeSearch === "true";
      delete element.dataset.disabledBeforeSearch;
    }
  }
  $("cancelButton").disabled = !running;
  updateSearchStartButton();
}

function jobIsActive(job) {
  return Boolean(job && ["queued", "running"].includes(job.status));
}

function restoreSearchScope(search) {
  recoveredSearchScopeOnly = true;
  const request = search.request || {};
  activeSearchFormat = search.trade_format === "three_team" ? "three_team" : "two_team";
  activeSearchTeamIds = [
    request.primary_team_id,
    ...(Array.isArray(request.counterparty_team_ids) ? request.counterparty_team_ids : [])
  ].filter(Boolean);

  const bundle = currentBundle();
  if (!bundle || bundle.bundle_id !== request.bundle_id) {
    renderBundle();
    $("estimate").textContent = "Recovered a saved search, but its league workspace is not selected. Its status and retained results remain attached.";
    return;
  }

  const teamIds = new Set(bundle.teams.map(team => team.team_id));
  if (teamIds.has(request.primary_team_id)) {
    $("primaryTeam").value = request.primary_team_id;
  }
  $(activeSearchFormat === "three_team" ? "threeTeamFormat" : "twoTeamFormat").checked = true;
  syncCounterparties();
  ThreeWayUi.syncFormatControls(bundle);

  const counterparties = activeSearchTeamIds.slice(1).filter(teamId =>
    teamIds.has(teamId) && teamId !== $("primaryTeam").value
  );
  if (activeSearchFormat === "three_team" && counterparties.length === 2) {
    $("partnerTeamA").value = counterparties[0];
    ThreeWayUi.syncPartnerOptions(bundle, $("primaryTeam").value, "a");
    $("partnerTeamB").value = counterparties[1];
    ThreeWayUi.syncPartnerOptions(bundle, $("primaryTeam").value, "b");
  } else if (activeSearchFormat === "two_team") {
    const selected = new Set(counterparties);
    for (const option of $("counterparties").options) {
      option.selected = selected.has(option.value);
    }
  }
  populatePackageFilters();
  $("estimate").textContent = "Reattached to this saved search. Only its week, teams, and format are restored above. Every other original filter and setting remains fixed in the saved job and is not reconstructed here. Count the displayed form before starting a different search.";
}

async function restoreActiveWork(activity) {
  const collection = activity.weekly_collection;
  const search = activity.search;
  draftWorkBusy = jobIsActive(activity.draft);
  const restoreCollection = collection && !collectionLaunching && !activeCollection;
  const restoreSearch = search && !searchRunning && !activeJob;
  if (restoreSearch && jobIsActive(search)) {
    setSearchRunning(true);
  }

  if (restoreCollection) {
    activeCollection = collection.job_id;
    const collectionRunning = jobIsActive(collection);
    setCollectionRunning(collectionRunning);
    renderCollectionProgress(collection);
    if (collectionRunning) void pollCollection().catch(showError);
    else await finishCollection(collection).catch(showError);
  }
  if (restoreSearch) {
    restoreSearchScope(search);
    activeJob = search.job_id;
    setSearchRunning(jobIsActive(search));
    $("progressPanel").classList.remove("hidden");
    $("resultsPanel").classList.add("hidden");
    $("cancelButton").classList.toggle("hidden", !searchRunning);
    renderProgress(search);
    updateSearchStartButton();
    const context = {
      jobId: search.job_id,
      teamIds: [...activeSearchTeamIds],
      bundle: currentBundle()
    };
    if (searchRunning) void pollJob(context).catch(showError);
    else void finishSearch(search, context).catch(showError);
  }

  updateSearchStartButton();
  window.dispatchEvent(new CustomEvent("serveractivitychange", {detail: activity}));
}

async function loadInsight(kind) {
  const bundle = currentBundle();
  if (!bundle || activeInsight || searchRunning || activeCollection || exportBusy || draftWorkBusy) return;
  clearError();
  activeInsight = kind;
  updateActivityControls();
  let failed = false;
  const onError = error => {
    failed = true;
    showError(error);
  };
  try {
    if (kind === "dashboard") {
      await DashboardUi.setBundle(bundle, {request: api, onError});
      if (!failed) dashboardBundleId = bundle.bundle_id;
    } else if (kind === "gm") {
      await GmInsightsUi.setBundle(bundle, {
        request: api,
        onError,
        primaryTeamId: $("primaryTeam").value || null,
        onUseTradePartner: chooseTradePartnerFromInsights
      });
      if (!failed) gmInsightsBundleId = bundle.bundle_id;
    } else if (kind === "timing") {
      await TradeTimingUi.setBundle(bundle, {
        apiRequest: api,
        primaryTeamId: $("primaryTeam").value || null
      });
      tradeTimingBundleId = bundle.bundle_id;
    } else {
      PlayerLabUi.reset("Preparing this week's player profiles…");
      await PlayerLabUi.queueBundle(bundle, {request: api, onError});
      PlayerLabUi.activateWorkspace("players");
      if (!failed) playerLabBundleId = bundle.bundle_id;
    }
  } finally {
    activeInsight = null;
    updateSearchStartButton();
  }
}

function chooseTradePartnerFromInsights(teamId) {
  const target = [...$("counterparties").options].find(option => option.value === teamId);
  if (!target || target.disabled) return;
  $("twoTeamFormat").checked = true;
  changeTradeFormat();
  for (const option of $("counterparties").options) option.selected = option === target;
  populatePackageFilters();
  invalidateSearchEstimate();
  $("counterparties").focus({preventScroll: true});
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("searchForm").scrollIntoView({behavior: reducedMotion ? "auto" : "smooth", block: "start"});
}

function syncCounterparties() {
  const own = $("primaryTeam").value;
  for (const option of $("counterparties").options) {
    option.disabled = option.value === own;
    if (option.disabled) option.selected = false;
  }
  GmInsightsUi.setPrimaryTeam(own || null);
  ThreeWayUi.syncPartnerOptions(currentBundle(), own);
  populatePackageFilters();
}

function requestPayload({requireTradeSearchReady = true} = {}) {
  const bundle = currentBundle();
  if (!bundle) throw new Error("Choose a ready weekly bundle first.");
  if (requireTradeSearchReady && !bundleCapabilityIsUsable(bundle, "trade_search")) {
    throw new Error(bundleCapabilityBlockMessage(
      bundle,
      "trade_search",
      "Trade search is unavailable for this weekly bundle"
    ));
  }
  const format = tradeFormat();
  const partnerSlots = ThreeWayUi.selectedPartnerSlots();
  if (format === "three_team" && (partnerSlots.length !== 2 || new Set(partnerSlots).size !== 2)) {
    throw new Error("Choose two different partner teams for the three-team trade.");
  }
  const packageFilters = TradeFilterUi.requestFields(format === "three_team");
  return {
    trade_format: format,
    bundle_id: $("bundleSelect").value,
    primary_team_id: $("primaryTeam").value,
    counterparty_team_ids: selectedCounterpartyIds(),
    min_outgoing: numberValue("minOutgoing"),
    max_outgoing: numberValue("maxOutgoing"),
    min_incoming: numberValue("minIncoming"),
    max_incoming: numberValue("maxIncoming"),
    max_total_players: numberValue("maxTotal", true),
    max_imbalance: numberValue("maxImbalance", true),
    balanced_only: $("balancedOnly").checked,
    skip_fantasypros_small_trades: format === "two_team" && $("skipSmall").checked,
    locked_player_ids: [],
    require_no_drops: $("noDrops").checked,
    ...packageFilters,
    minimum_power_delta: numberValue("powerFloor"),
    checkpoint_interval: 1000,
    scenario_count: numberValue("scenarioCount"),
    seed: 20260901,
    allow_surrogate_power: $("acceptSurrogateSearch").checked
  };
}

function searchConfigurationSignature(payload = null) {
  try { return JSON.stringify(payload || requestPayload({requireTradeSearchReady: false})); }
  catch (_) { return null; }
}

function updateSearchStartButton() {
  const bundleReady = bundleCapabilityIsUsable(currentBundle(), "trade_search");
  const estimateCurrent = !isThreeTeam() || (
    threeTeamEstimateSignature !== null &&
    threeTeamEstimateSignature === searchConfigurationSignature()
  );
  const busy = Boolean(activeCollection) || Boolean(activeInsight) || searchRunning || exportBusy || draftWorkBusy;
  $("startButton").disabled = !bundleReady || busy || !estimateCurrent || recoveredSearchScopeOnly;
  $("estimateButton").disabled = !bundleReady || busy;
  updateActivityControls();
}

function invalidateSearchEstimate() {
  threeTeamEstimateSignature = null;
  if (!bundleCapabilityIsUsable(currentBundle(), "trade_search")) {
    const bundle = currentBundle();
    if (bundle) {
      $("estimate").textContent = bundleCapabilityBlockMessage(
        bundle,
        "trade_search",
        "Trade search is unavailable for this weekly bundle"
      );
    }
  } else if (isThreeTeam() && currentBundle()) {
    $("estimate").textContent = "Count this specific three-team search before starting it.";
  }
  updateSearchStartButton();
}

function collectionPayload() {
  const profile = LeagueUi.selectedProfile();
  if (!profile) throw new Error("Choose a league workspace first.");
  const hostLeagueUrl = profile.espn_league_id
    ? `https://fantasy.espn.com/football/league?leagueId=${profile.espn_league_id}&seasonId=${profile.season}`
    : null;
  const yahooProjectionUrl = profile.yahoo_league_id
    ? `https://football.fantasysports.yahoo.com/f1/${profile.yahoo_league_id}/players?status=ALL`
    : null;
  return {
    season: profile.season,
    week: numberValue("collectionWeek"),
    scoring: profile.scoring,
    host_league_url: hostLeagueUrl,
    yahoo_projection_league_url: yahooProjectionUrl,
    include_future_weekly: $("includeFutureWeekly").checked,
    allow_surrogate_power: $("allowSurrogatePower").checked,
    use_fantasypros: $("useFantasyPros").checked,
    use_broad_consensus: $("useBroadConsensus").checked,
    refresh_public_player_data: $("refreshPublicPlayerData").checked
  };
}

async function startCollection(event) {
  event.preventDefault();
  if (collectionLaunching || activeCollection || draftWorkBusy) return;
  clearError();
  try {
    if (!extensionConnected) throw new Error("Connect the browser extension before scanning the league.");
    const payload = LeagueUi.collectionPayload();
    activeCollectionClock = ProgressUi.startHistory("weekly-collection");
    collectionLaunching = true;
    setCollectionRunning(true);
    $("collectionProgress").classList.remove("hidden");
    ProgressUi.setBar(
      $("collectionProgress").querySelector(".progress-track"),
      $("collectionProgressBar"),
      null,
      "Validating the selected league workspace"
    );
    $("collectionProgressText").textContent = "Validating the selected league workspace…";
    $("collectionTiming").textContent = ProgressUi.describeTiming(
      null,
      activeCollectionClock
    );
    const job = await api("/api/weekly-collections", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    activeCollection = job.job_id;
    collectionLaunching = false;
    setCollectionRunning(true);
    renderCollectionProgress(job);
    await pollCollection();
  } catch (error) {
    ProgressUi.finishHistory(activeCollectionClock, false);
    activeCollectionClock = null;
    collectionLaunching = false;
    if (!activeCollection) setCollectionRunning(false);
    showError(error);
  }
}

async function pollCollection() {
  while (activeCollection) {
    let job;
    try { job = await api(`/api/weekly-collections/${activeCollection}`); }
    catch (error) {
      showError(new Error(
        `${error.message} Collection may still be running locally; refresh this page to reconnect.`
      ));
      return;
    }
    const terminal = !jobIsActive(job);
    if (terminal) {
      ProgressUi.finishHistory(activeCollectionClock, job.status === "complete");
    }
    renderCollectionProgress(job);
    if (terminal) {
      await finishCollection(job);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  ProgressUi.finishHistory(activeCollectionClock, false);
  activeCollectionClock = null;
  activeCollection = null;
  setCollectionRunning(false);
}

async function finishCollection(job) {
  activeCollectionClock = null;
  activeCollection = null;
  setCollectionRunning(false);
  if (job.status === "complete") {
    $("refreshPublicPlayerData").checked = false;
    await refreshBundles(job.bundle_id);
    const historyNote = historyCollectionNote(job.history_attempt);
    if (historyNote) {
      $("collectionProgressText").textContent = `Weekly model ready. ${historyNote}`;
    }
  } else if (job.status === "failed") {
    $("collectionProgressText").textContent = "Collection failed. No new weekly bundle was published.";
    showError(job.error || "No new weekly bundle was published.");
  } else {
    $("collectionProgressText").textContent = "Collection stopped safely. No incomplete week was published.";
  }
  try {
    await api(`/api/weekly-collections/${job.job_id}/activity-ack`, {method: "POST", body: ""});
  } catch (_) {
    /* Keep the terminal status recoverable if acknowledgement is interrupted. */
  }
}

function historyCollectionNote(attempt) {
  if (!attempt || typeof attempt !== "object") return "";
  if (attempt.status === "captured") {
    return "League activity was saved for GM and timing history.";
  }
  const messages = {
    activity_schema_unsupported: "League activity needs a collector update; every core trade feature remains available.",
    activity_unavailable: "League activity was temporarily unavailable; every core trade feature remains available.",
    canonicalization_failed: "League activity could not be matched safely; every core trade feature remains available.",
    history_processing_unavailable: "League activity could not be processed locally; every core trade feature remains available.",
    store_unavailable: "League activity could not be saved locally; every core trade feature remains available.",
    not_provided: "No league-activity capture was included; every core trade feature remains available."
  };
  return messages[attempt.reason_code] || "League activity is unavailable; every core trade feature remains available.";
}

function renderCollectionProgress(job) {
  $("collectionProgress").classList.remove("hidden");
  const progress = job.progress;
  const fraction = job.operation?.progress?.determinate
    ? job.operation.progress.fraction
    : progress?.fraction ?? null;
  ProgressUi.setBar(
    $("collectionProgress").querySelector(".progress-track"),
    $("collectionProgressBar"),
    fraction,
    progress?.message || "Weekly collection is waiting to start"
  );
  $("collectionProgressText").textContent = progress
    ? progress.message
    : "Weekly collection is waiting to start…";
  const pending = job.sign_in && job.sign_in.pending_provider;
  ProgressUi.pauseHistory(activeCollectionClock, Boolean(pending));
  $("collectionTiming").textContent = ProgressUi.describeTiming(
    job.operation,
    activeCollectionClock
  );
  const labels = {
    fantasypros: "FantasyPros",
    espn: "ESPN",
    yahoo: "Yahoo",
    cbs: "CBS",
    fftoday: "FFToday",
    fantasysharks: "FantasySharks"
  };
  $("signInPrompt").classList.toggle("hidden", !pending);
  if (pending) {
    $("signInPromptText").textContent = `If needed, finish signing in to ${labels[pending] || pending} on the extension’s scan tab, then continue.`;
    $("confirmSignInButton").disabled = false;
  }
}

function setCollectionRunning(running) {
  $("cancelCollectionButton").classList.toggle(
    "hidden",
    !running || !activeCollection
  );
  for (const element of $("collectionForm").elements) {
    if (element.id !== "cancelCollectionButton") element.disabled = running;
  }
  $("collectButton").disabled = running
    || !collectionAvailable
    || !extensionConnected
    || !LeagueUi.canCollect();
  $("estimateButton").disabled = running || !$("bundleSelect").value;
  updateSearchStartButton();
  if (!running) {
    $("signInPrompt").classList.add("hidden");
    syncCollectionMode();
  }
}

async function confirmCollectionSignIn() {
  if (!activeCollection) return;
  const button = $("confirmSignInButton");
  button.disabled = true;
  try {
    await api(`/api/weekly-collections/${activeCollection}/sign-in`, {
      method: "POST",
      body: ""
    });
    $("signInPrompt").classList.add("hidden");
  } catch (error) {
    button.disabled = false;
    showError(error);
  }
}

async function cancelCollection() {
  if (!activeCollection) return;
  $("collectionProgressText").textContent = "Stopping safely after the current collection step…";
  try {
    await api(`/api/weekly-collections/${activeCollection}/cancel`, {method: "POST", body: ""});
  } catch (error) { showError(error); }
}

async function estimate() {
  clearError();
  $("estimateButton").disabled = true;
  try {
    const payload = requestPayload({requireTradeSearchReady: false});
    const signature = searchConfigurationSignature(payload);
    const value = await ProgressUi.run(
      "candidate-count",
      "Counting valid trade combinations",
      () => api("/api/searches/estimate", {
        method: "POST",
        body: JSON.stringify(payload)
      })
    );
    const candidateCount = ThreeWayUi.exactCandidateCount(value);
    if (signature !== searchConfigurationSignature()) {
      updateSearchStartButton();
      return;
    }
    const caution = candidateCount > 10000000n
      ? " This is a very large run; tighten a size or imbalance filter first."
      : "";
    const searchReadiness = value?.data_readiness;
    const blocked = searchReadiness?.status === "not_ready";
    const blockedNote = blocked
      ? ` Search cannot start: ${Array.isArray(searchReadiness.missing) && searchReadiness.missing.length
        ? searchReadiness.missing.join(" ")
        : "required bundle evidence is missing."}`
      : "";
    if (payload.trade_format === "three_team") {
      const adjustmentPolicy = value.free_agent_allocation_policy
        ? ` Automatic roster adjustment policy: ${value.free_agent_allocation_policy}`
        : "";
      $("estimate").textContent = `${compactNumber(candidateCount)} combinations counted exactly for this three-team agreement.${caution}${adjustmentPolicy}${blockedNote}`;
      threeTeamEstimateSignature = signature;
    } else {
      $("estimate").textContent = `${compactNumber(candidateCount)} combinations counted exactly across ${value.pair_count} team matchups.${caution}${blockedNote}`;
    }
    recoveredSearchScopeOnly = false;
    updateSearchStartButton();
  } catch (error) {
    threeTeamEstimateSignature = null;
    updateSearchStartButton();
    showError(error);
  } finally {
    updateSearchStartButton();
  }
}

async function startSearch(event) {
  event.preventDefault();
  if (searchRunning || draftWorkBusy) return;
  clearError();
  let jobAccepted = false;
  try {
    const payload = requestPayload();
    if (payload.trade_format === "three_team" && threeTeamEstimateSignature !== searchConfigurationSignature(payload)) {
      throw new Error("Count this specific three-team search before starting it.");
    }
    activeSearchClock = ProgressUi.startHistory("trade-search");
    activeSearchFormat = payload.trade_format;
    activeSearchTeamIds = [payload.primary_team_id, ...payload.counterparty_team_ids];
    const searchBundle = currentBundle();
    setSearchRunning(true);
    $("progressPanel").classList.remove("hidden");
    $("resultsPanel").classList.add("hidden");
    $("cancelButton").classList.add("hidden");
    ProgressUi.setBar(
      $("progressPanel").querySelector(".progress-track"),
      $("progressBar"),
      null,
      "Validating the search and preparing the season simulation"
    );
    $("progressText").textContent = "Validating the search and preparing the shared season simulation…";
    $("progressStats").textContent = "Exact combination progress will appear when the search phase begins.";
    $("searchTiming").textContent = ProgressUi.describeTiming(null, activeSearchClock);
    const job = await api("/api/searches", {method: "POST", body: JSON.stringify(payload)});
    activeJob = job.job_id;
    jobAccepted = true;
    $("cancelButton").classList.remove("hidden");
    await pollJob({
      jobId: job.job_id,
      teamIds: [...activeSearchTeamIds],
      bundle: searchBundle
    });
  } catch (error) {
    ProgressUi.finishHistory(activeSearchClock, false);
    activeSearchClock = null;
    setSearchRunning(false);
    if (!jobAccepted) {
      $("progressPanel").classList.add("hidden");
      $("resultsPanel").classList.toggle("hidden", !loadedResults);
    }
    showError(error);
  }
}

async function pollJob(context) {
  while (activeJob === context.jobId) {
    let job;
    try { job = await api(`/api/searches/${context.jobId}`); }
    catch (error) {
      $("cancelButton").classList.add("hidden");
      showError(new Error(
        `${error.message} The search may still be running locally; refresh this page to reconnect.`
      ));
      return;
    }
    if (activeJob !== context.jobId) return;
    const terminal = !jobIsActive(job);
    if (terminal) {
      ProgressUi.finishHistory(activeSearchClock, job.status === "complete");
    }
    renderProgress(job);
    if (terminal) {
      await finishSearch(job, context);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  if (activeJob === null && searchRunning) {
    ProgressUi.finishHistory(activeSearchClock, false);
    activeSearchClock = null;
    setSearchRunning(false);
    $("cancelButton").classList.add("hidden");
  }
}

async function finishSearch(job, context) {
  $("cancelButton").classList.add("hidden");
  try {
    if (job.status === "complete") {
      await loadResults(context);
    } else if (job.status === "failed") {
      $("progressText").textContent = "Search failed.";
      showError(job.error || "The search failed.");
    } else {
      $("progressText").textContent = "Stopped safely. Start again later to resume from this checkpoint.";
    }
  } finally {
    try {
      await api(`/api/searches/${job.job_id}/activity-ack`, {method: "POST", body: ""});
    } catch (_) {
      /* Keep the retained result recoverable if acknowledgement is interrupted. */
    }
    activeSearchClock = null;
    if (activeJob === context.jobId) activeJob = null;
    setSearchRunning(false);
  }
}

function renderProgress(job) {
  const progress = job.progress;
  const operationFraction = job.operation?.progress?.determinate
    ? job.operation.progress.fraction
    : progress?.completion_fraction ?? null;
  ProgressUi.setBar(
    $("progressPanel").querySelector(".progress-track"),
    $("progressBar"),
    operationFraction,
    job.operation?.phase === "preparing_season_simulation"
      ? "Preparing the shared season simulation"
      : "Searching trade combinations"
  );
  $("searchTiming").textContent = ProgressUi.describeTiming(
    job.operation,
    activeSearchClock
  );
  if (!progress) {
    $("progressText").textContent = job.operation?.cancel_requested
      ? "Stopping safely after the current simulation step…"
      : "Preparing the shared season simulation…";
    $("progressStats").textContent = "The search will show exact combination progress as soon as preparation finishes.";
    return;
  }
  const pct = Math.min(100, progress.completion_fraction * 100);
  const threeTeam = activeSearchFormat === "three_team";
  const current = threeTeam
    ? " · selected three-team agreement"
    : progress.current_counterparty_team_id
      ? ` · current team ${progress.current_counterparty_team_id}`
      : "";
  const examined = progress.examined_candidate_count_text ?? progress.examined_candidate_count;
  const total = progress.total_candidate_count_text ?? progress.total_candidate_count;
  const qualified = progress.qualified_trade_count_text ?? progress.qualified_trade_count;
  const mutual = progress.mutual_playoff_gain_count_text ?? progress.mutual_playoff_gain_count;
  const gainLabel = threeTeam ? "improve all three playoff chances" : "improve both playoff chances";
  $("progressText").textContent = `${pct.toFixed(1)}% complete${current}`;
  $("progressStats").textContent = `${compactNumber(examined)} of ${compactNumber(total)} combinations · ${compactNumber(qualified)} passed power · ${compactNumber(mutual)} ${gainLabel}`;
}

function setResultHeaders(labels) {
  const row = $("resultsHeaderRow");
  row.replaceChildren();
  for (const label of labels) {
    const heading = document.createElement("th");
    heading.textContent = label;
    row.append(heading);
  }
}

function renderTwoTeamTradeRows(rows) {
  setResultHeaders([
    "Other team", "You give", "You receive", "Automatic roster moves",
    "Your power", "Their power", "Your playoff", "Their playoff", "Power evidence"
  ]);
  const body = $("resultsBody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    if (row.mutual_gain) tr.className = "mutual-row";
    const moves = [
      row.your_adds.length ? `You add: ${row.your_adds.join("; ")}` : "",
      row.your_drops.length ? `You drop: ${row.your_drops.join("; ")}` : "",
      row.their_adds.length ? `They add: ${row.their_adds.join("; ")}` : "",
      row.their_drops.length ? `They drop: ${row.their_drops.join("; ")}` : ""
    ].filter(Boolean).join(" · ") || "None";
    const cells = [
      row.other_team,
      row.give.join("; "),
      row.receive.join("; "),
      moves,
      signed(row.your_power_delta),
      signed(row.their_power_delta),
      signed(row.your_playoff_delta, true),
      signed(row.their_playoff_delta, true),
      powerEvidenceLabel(row.power_methodology_status)
    ];
    cells.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index >= 4 && index <= 7) cell.className = String(value).startsWith("+") ? "gain" : "loss";
      tr.append(cell);
    });
    body.append(tr);
  }
}

async function loadResults(context) {
  const generation = ++resultLoadGeneration;
  const value = await ProgressUi.run(
    "result-preview",
    "Preparing the result preview",
    () => api(`/api/searches/${context.jobId}/results`)
  );
  if (generation !== resultLoadGeneration || activeJob !== context.jobId) return;
  loadedResults = value;
  loadedResultTeamIds = [...context.teamIds];
  loadedResultJobId = context.jobId;
  loadedResultBundle = context.bundle;
  const outlookBody = $("outlookBody");
  outlookBody.replaceChildren();
  for (const row of value.team_outlook) {
    const tr = document.createElement("tr");
    const currentRecord = `${row.current_wins}-${row.current_losses}${row.current_ties ? `-${row.current_ties}` : ""}`;
    const finalRecord = `${row.expected_final_wins.toFixed(1)}-${row.expected_final_losses.toFixed(1)}${row.expected_final_ties ? `-${row.expected_final_ties.toFixed(1)}` : ""}`;
    const cells = [
      row.projected_finish.toFixed(1),
      row.team_name,
      currentRecord,
      finalRecord,
      `${(row.playoff_probability * 100).toFixed(1)}%`
    ];
    cells.forEach((cell, index) => {
      const td = document.createElement("td");
      td.textContent = cell;
      if (index === 4) td.className = "probability";
      tr.append(td);
    });
    outlookBody.append(tr);
  }
  renderLoadedResults();
  $("resultsPanel").classList.remove("hidden");
}

function resultWorkbenchControls() {
  return ResultsWorkbench.readControlValues({
    onlyAllParticipantsImprove: $("onlyMutualResults").checked,
    minimumPlayoffGainPoints: $("minimumResultGain").value.trim(),
    sortBy: $("resultSort").value
  });
}

function renderLoadedResults() {
  if (!loadedResults) return;
  const value = loadedResults;
  const threeTeam = value.trade_format === "three_team";
  const rows = ResultsWorkbench.filterAndSort(
    value.rows,
    {
      tradeFormat: threeTeam ? "three_team" : "two_team",
      primaryTeamId: loadedResultTeamIds[0]
    },
    resultWorkbenchControls()
  );
  const body = $("resultsBody");
  body.replaceChildren();
  if (threeTeam) {
    ThreeWayUi.renderTradeRows(
      rows,
      loadedResultTeamIds,
      loadedResultBundle,
      {signed, percent, powerEvidenceLabel}
    );
  }
  else renderTwoTeamTradeRows(rows);
  const powerNotice = ["exact", "holdout_validated"].includes(value.power_engine_mode)
    ? (threeTeam ? ` POWER NOTICE: ${value.power_engine_notice}` : "")
    : ` ${value.power_engine_mode.toUpperCase()} POWER: ${value.power_engine_notice}`;
  const adjustmentPolicy = value.free_agent_allocation_policy
    ? ` Automatic roster adjustment policy: ${value.free_agent_allocation_policy}`
    : "";
  $("workbenchCount").textContent = `${compactNumber(rows.length)} of ${compactNumber(value.rows.length)} loaded results shown`;
  $("resultsNote").textContent = `The workbench is filtering the ${compactNumber(value.shown_count)} loaded preview rows from ${compactNumber(value.total_count_text ?? value.total_count)} qualified trades. The Excel workbook still contains the full qualified result set.${powerNotice}${adjustmentPolicy}`;
}

async function cancelSearch() {
  if (!activeJob) return;
  try { await api(`/api/searches/${activeJob}/cancel`, {method: "POST", body: ""}); }
  catch (error) { showError(error); }
}

async function exportWorkbook() {
  if (exportBusy) return;
  clearError();
  exportBusy = true;
  $("exportButton").disabled = true;
  updateSearchStartButton();
  try {
    const jobId = loadedResultJobId;
    if (!jobId) throw new Error("Run a trade search before exporting its results.");
    const [result, blob] = await ProgressUi.run(
      "excel-export",
      "Building and downloading the Excel workbook",
      async () => {
        const created = await api(`/api/searches/${jobId}/export`, {
          method: "POST",
          body: ""
        });
        return [
          created,
          await api(`/api/exports/${encodeURIComponent(created.filename)}`)
        ];
      }
    );
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = result.filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) {
    showError(error);
  } finally {
    exportBusy = false;
    $("exportButton").disabled = false;
    updateSearchStartButton();
  }
}

function changeTradeFormat() {
  ThreeWayUi.syncFormatControls(currentBundle());
  ThreeWayUi.syncPartnerOptions(currentBundle(), $("primaryTeam").value);
  populatePackageFilters();
}

function changeThreeTeamPartner(side) {
  ThreeWayUi.syncPartnerOptions(currentBundle(), $("primaryTeam").value, side);
  populatePackageFilters();
}

function searchConfigurationChanged(event) {
  if (event.target.dataset.filterRole === "player-search") return;
  invalidateSearchEstimate();
}

window.addEventListener("draftactivitychange", event => {
  draftWorkBusy = Boolean(event.detail?.busy);
  updateSearchStartButton();
});

$("bundleFile").addEventListener("change", async event => {
  clearError();
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (searchRunning || activeCollection || activeInsight || exportBusy) {
      throw new Error("Wait for the current local calculation before changing weekly data.");
    }
    if (file.size > 256 * 1024 * 1024) {
      throw new Error("Weekly bundle files must be 256 MB or smaller.");
    }
    const hadProfile = Boolean(LeagueUi.selectedProfile());
    await ProgressUi.run(
      "bundle-import",
      "Validating and importing weekly data",
      async () => {
        const recordText = await file.text();
        JSON.parse(recordText);
        const summary = await api(LeagueUi.importPath(), {
          method: "POST",
          body: recordText
        });
        if (!hadProfile) {
          await LeagueUi.refresh(LeagueUi.UNASSIGNED, {notify: false});
        }
        await refreshBundles(summary.bundle_id);
      }
    );
  } catch (error) { showError(error); }
  finally { event.target.value = ""; }
});
$("bundleSelect").addEventListener("change", changeBundle);
$("primaryTeam").addEventListener("change", () => {
  syncCounterparties();
  LeagueUi.saveMyTeam(
    $("primaryTeam").value,
    $("bundleSelect").value
  ).catch(showError);
  GmInsightsUi.setPrimaryTeam($("primaryTeam").value);
  if (tradeTimingBundleId !== null) {
    tradeTimingBundleId = null;
    TradeTimingUi.reset("Your team changed. Calculate trade timing again when you are ready.");
  }
  void TradeTimingUi.setPrimaryTeam($("primaryTeam").value);
  updateActivityControls();
});
$("counterparties").addEventListener("change", populatePackageFilters);
for (const option of document.querySelectorAll('input[name="tradeFormat"]')) {
  option.addEventListener("change", changeTradeFormat);
}
$("partnerTeamA").addEventListener("change", () => changeThreeTeamPartner("a"));
$("partnerTeamB").addEventListener("change", () => changeThreeTeamPartner("b"));
$("noDrops").addEventListener("change", () => ThreeWayUi.syncFormatControls(currentBundle()));
TradeFilterUi.bind(invalidateSearchEstimate);
$("connectExtensionButton").addEventListener("click", connectExtension);
$("useFantasyPros").addEventListener("change", syncCollectionMode);
$("useBroadConsensus").addEventListener("change", syncCollectionMode);
for (const id of ["collectionSeason", "collectionWeek", "collectionScoring", "hostLeagueUrl", "yahooProjectionUrl"]) {
  $(id).addEventListener(["hostLeagueUrl", "yahooProjectionUrl"].includes(id) ? "input" : "change", scheduleSourceDebugRefresh);
}
$("collectionForm").addEventListener("submit", startCollection);
$("cancelCollectionButton").addEventListener("click", cancelCollection);
$("confirmSignInButton").addEventListener("click", confirmCollectionSignIn);
$("dashboardLoadButton").addEventListener("click", () => void loadInsight("dashboard"));
$("gmInsightsLoadButton").addEventListener("click", () => void loadInsight("gm"));
$("tradeTimingLoadButton").addEventListener("click", () => void loadInsight("timing"));
$("playerLabLoadButton").addEventListener("click", () => void loadInsight("player"));
$("estimateButton").addEventListener("click", estimate);
$("searchForm").addEventListener("input", searchConfigurationChanged);
$("searchForm").addEventListener("change", searchConfigurationChanged);
$("searchForm").addEventListener("submit", startSearch);
$("cancelButton").addEventListener("click", cancelSearch);
$("exportButton").addEventListener("click", exportWorkbook);
$("assignBundleButton").addEventListener("click", () => {
  const bundle = currentBundle();
  if (bundle) LeagueUi.assignBundle(bundle.bundle_id).catch(showError);
});
for (const id of ["onlyMutualResults", "minimumResultGain", "resultSort"]) {
  $(id).addEventListener("input", () => {
    try {
      clearError();
      renderLoadedResults();
    } catch (error) {
      showError(error);
    }
  });
}

LeagueUi.bind({
  api,
  onSelection: async () => {
    clearError();
    await refreshBundles();
    scheduleSourceDebugRefresh();
  },
  onError: showError
});

(async () => {
  try {
    $("collectionSeason").value = String(new Date().getFullYear());
    syncCollectionMode();
    await pingLifecycle();
    await api("/api/health");
    $("health").textContent = "App running locally";
    const activity = await api("/api/activity");
    draftWorkBusy = jobIsActive(activity.draft);
    await refreshExtensionStatus();
    await LeagueUi.refresh(null, {notify: false});
    await refreshBundles(activity.search?.request?.bundle_id || null);
    await restoreActiveWork(activity);
    setInterval(refreshExtensionStatus, 5000);
  } catch (error) {
    $("health").textContent = "Needs attention";
    showError(error);
  }
})();
