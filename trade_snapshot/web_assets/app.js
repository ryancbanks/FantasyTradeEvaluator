"use strict";

const token = document.querySelector('meta[name="app-token"]').content;
const $ = id => document.getElementById(id);
let bundles = [];
let activeJob = null;
let activeCollection = null;
let collectionAvailable = false;
let extensionConnected = false;
let extensionPairing = false;
let extensionPairAcknowledged = false;
let extensionPairFailure = null;
let extensionPairHint = null;
let threeTeamEstimateSignature = null;
let searchRunning = false;
let activeSearchFormat = "two_team";
let activeSearchTeamIds = [];
const heartbeatInterval = 20000;
const extensionProtocolVersion = 1;

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-FTE-Token", token);
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
    headers: {"X-FTE-Token": token},
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
    void refreshExtensionStatus();
    return;
  }
  if (value.type === "pair.rejected" || value.type === "pair.expired") {
    extensionPairFailure = "The extension did not accept this connection. Try Connect extension again.";
    return;
  }
  if (value.type === "session.closed") void refreshExtensionStatus();
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
function percent(value) { return new Intl.NumberFormat(undefined, {style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1}).format(value); }
function signed(value, asPercent = false) {
  const text = asPercent ? percent(Math.abs(value)) : Math.abs(value).toFixed(1);
  return `${value >= 0 ? "+" : "−"}${text}`;
}

function currentBundle() {
  return bundles.find(item => item.bundle_id === $("bundleSelect").value) || null;
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

function renderExtensionStatus(status) {
  extensionConnected = status && status.state === "paired";
  const pairing = extensionPairing;
  $("extensionDot").classList.toggle("connected", extensionConnected);
  $("extensionStatus").textContent = extensionConnected
    ? `Browser extension connected${status.extension_version ? ` · ${status.extension_version}` : ""}`
    : pairing
      ? "Approve pairing in the extension…"
      : "Browser extension not connected";
  $("extensionHelp").textContent = extensionConnected
    ? "Ready to scan through one temporary tab in this signed-in browser. Cookies never leave the browser."
    : pairing
      ? "Open Chrome’s Extensions menu, choose Fantasy Trade Evaluator Browser Bridge, and click Pair with app."
    : "Install the downloaded extension once, then click Connect extension before collecting weekly data.";
  $("connectExtensionButton").disabled = extensionConnected || pairing;
  $("connectExtensionButton").textContent = extensionConnected ? "Connected" : pairing ? "Waiting for approval" : "Connect extension";
  const pairCode = $("extensionPairCode");
  const showPairCode = pairing && typeof extensionPairHint === "string";
  pairCode.textContent = showPairCode
    ? `Pairing code ends in ${extensionPairHint} — confirm the same code in the extension.`
    : "";
  pairCode.classList.toggle("hidden", !showPairCode);
  if (!activeCollection) $("collectButton").disabled = !collectionAvailable || !extensionConnected;
}

async function refreshExtensionStatus() {
  try {
    const status = await api("/api/browser-extension/status");
    renderExtensionStatus(status);
    return status;
  } catch (_) {
    renderExtensionStatus(null);
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
          "The browser extension was not detected. Install or reload it, refresh this page, and try Connect extension again."
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
  const response = await api("/api/bundles");
  const previous = $("bundleSelect").value;
  bundles = response.bundles.filter(item => item.status === "ready");
  collectionAvailable = response.readiness.collection_available;
  renderReadiness(response.readiness);
  const select = $("bundleSelect");
  select.replaceChildren(new Option(bundles.length ? "Choose a ready week" : "No weekly bundle yet", ""));
  for (const bundle of bundles) {
    const mode = bundle.power_engine_mode === "surrogate" ? " · SURROGATE" : " · exact method";
    const option = new Option(`${bundle.season} · Week ${bundle.week} · ${bundle.team_count} teams${mode}`, bundle.bundle_id);
    select.add(option);
  }
  select.disabled = bundles.length === 0;
  select.value = selectId || previous;
  if (!select.value && bundles.length === 1 && bundles[0].power_engine_mode !== "surrogate") {
    select.value = bundles[0].bundle_id;
  }
  $("collectButton").disabled = !collectionAvailable || !extensionConnected || Boolean(activeCollection);
  $("collectButton").title = collectionAvailable ? "" : "Import a complete weekly bundle in this build.";
  changeBundle();
}

function renderReadiness(readiness) {
  const row = $("readiness");
  row.textContent = readiness.message;
  row.className = `readiness ${readiness.ready ? "ready" : "not-ready"}`;
}

function renderBundle() {
  const bundle = currentBundle();
  const primary = $("primaryTeam");
  const others = $("counterparties");
  primary.replaceChildren();
  others.replaceChildren();
  if (!bundle) {
    $("bundleSummary").textContent = "Import or collect a weekly bundle to begin.";
    $("bundleSummary").classList.remove("surrogate-warning");
    $("estimateButton").disabled = true;
    $("surrogateSearchConsentRow").classList.add("hidden");
    $("acceptSurrogateSearch").checked = false;
    threeTeamEstimateSignature = null;
    ThreeWayUi.syncPartnerOptions(null, "");
    ThreeWayUi.syncFormatControls(null);
    populatePackageFilters();
    updateSearchStartButton();
    return;
  }
  for (const team of bundle.teams) {
    primary.add(new Option(team.name, team.team_id));
    others.add(new Option(team.name, team.team_id));
  }
  const surrogate = bundle.power_engine_mode === "surrogate";
  const summary = $("bundleSummary");
  if (surrogate) {
    const error = bundle.methodology.holdout_max_absolute_score_error;
    const match = percent(bundle.methodology.holdout_display_match_rate);
    summary.textContent = `${bundle.season} week ${bundle.week} · ${bundle.team_count} teams · SURROGATE / APPROXIMATE POWER · Blind max score error ${error}; display match ${match}. ${bundle.power_engine_notice}`;
    summary.classList.add("surrogate-warning");
  } else {
    const sizes = bundle.methodology.validated_balanced_package_sizes.join(", ");
    summary.textContent = `${bundle.season} week ${bundle.week} · ${bundle.team_count} teams · exact FantasyPros-power evidence: balanced ${sizes}-player packages without adds/drops; other shapes are labeled extrapolated`;
    summary.classList.remove("surrogate-warning");
  }
  $("surrogateSearchConsentRow").classList.toggle("hidden", !surrogate);
  $("acceptSurrogateSearch").checked = false;
  $("estimateButton").disabled = Boolean(activeCollection);
  syncCounterparties();
  ThreeWayUi.syncFormatControls(bundle);
  updateSearchStartButton();
}

function changeBundle() {
  clearError();
  activeJob = null;
  $("progressPanel").classList.add("hidden");
  $("resultsPanel").classList.add("hidden");
  $("progressBar").style.width = "0%";
  $("progressStats").textContent = "";
  $("estimate").textContent = "Choose a ready week, then count the combinations.";
  threeTeamEstimateSignature = null;
  renderBundle();
  void DashboardUi.setBundle(currentBundle(), {request: api, onError: showError});
  void GmInsightsUi.setBundle(currentBundle(), {
    request: api,
    onError: showError,
    primaryTeamId: $("primaryTeam").value || null,
    onUseTradePartner: chooseTradePartnerFromInsights
  });
  void TradeTimingUi.setBundle(currentBundle(), {
    apiRequest: api,
    primaryTeamId: $("primaryTeam").value || null
  });
  void PlayerLabUi.setBundle(currentBundle(), {request: api, onError: showError});
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
  ThreeWayUi.syncPartnerOptions(currentBundle(), own);
  populatePackageFilters();
}

function requestPayload() {
  if (!$("bundleSelect").value) throw new Error("Choose a ready weekly bundle first.");
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
  try { return JSON.stringify(payload || requestPayload()); }
  catch (_) { return null; }
}

function updateSearchStartButton() {
  const bundleReady = Boolean(currentBundle());
  const estimateCurrent = !isThreeTeam() || (
    threeTeamEstimateSignature !== null &&
    threeTeamEstimateSignature === searchConfigurationSignature()
  );
  $("startButton").disabled = !bundleReady || Boolean(activeCollection) || searchRunning || !estimateCurrent;
}

function invalidateSearchEstimate() {
  threeTeamEstimateSignature = null;
  if (isThreeTeam() && currentBundle()) {
    $("estimate").textContent = "Count this specific three-team search before starting it.";
  }
  updateSearchStartButton();
}

function collectionPayload() {
  const hostLeagueUrl = $("hostLeagueUrl").value.trim();
  const yahooProjectionUrl = $("yahooProjectionUrl").value.trim();
  return {
    season: numberValue("collectionSeason"),
    week: numberValue("collectionWeek"),
    scoring: $("collectionScoring").value,
    host_league_url: hostLeagueUrl || null,
    yahoo_projection_league_url: yahooProjectionUrl || null,
    include_future_weekly: $("includeFutureWeekly").checked,
    allow_surrogate_power: $("allowSurrogatePower").checked
  };
}

async function startCollection(event) {
  event.preventDefault();
  clearError();
  try {
    if (!extensionConnected) throw new Error("Connect the browser extension before scanning the league.");
    const job = await api("/api/weekly-collections", {
      method: "POST",
      body: JSON.stringify(collectionPayload())
    });
    activeCollection = job.job_id;
    setCollectionRunning(true);
    renderCollectionProgress(job);
    await pollCollection();
  } catch (error) {
    setCollectionRunning(false);
    showError(error);
  }
}

async function pollCollection() {
  while (activeCollection) {
    let job;
    try { job = await api(`/api/weekly-collections/${activeCollection}`); }
    catch (error) { showError(error); break; }
    renderCollectionProgress(job);
    if (!["queued", "running"].includes(job.status)) {
      const selectedBundle = job.bundle_id;
      activeCollection = null;
      setCollectionRunning(false);
      if (job.status === "complete") await refreshBundles(selectedBundle);
      else if (job.status === "failed") showError(job.error || "No new weekly bundle was published.");
      else $("collectionProgressText").textContent = "Collection stopped safely. No incomplete week was published.";
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  activeCollection = null;
  setCollectionRunning(false);
}

function renderCollectionProgress(job) {
  $("collectionProgress").classList.remove("hidden");
  const progress = job.progress;
  const pct = progress ? Math.min(100, progress.fraction * 100) : 0;
  $("collectionProgressBar").style.width = `${pct}%`;
  $("collectionProgressText").textContent = progress
    ? `${progress.message} · ${pct.toFixed(0)}%`
    : "Weekly collection is waiting to start…";
  const pending = job.sign_in && job.sign_in.pending_provider;
  const labels = {fantasypros: "FantasyPros", espn: "ESPN", yahoo: "Yahoo"};
  $("signInPrompt").classList.toggle("hidden", !pending);
  if (pending) {
    $("signInPromptText").textContent = `If needed, finish signing in to ${labels[pending] || pending} on the extension’s scan tab, then continue.`;
    $("confirmSignInButton").disabled = false;
  }
}

function setCollectionRunning(running) {
  $("cancelCollectionButton").classList.toggle("hidden", !running);
  for (const element of $("collectionForm").elements) {
    if (element.id !== "cancelCollectionButton") element.disabled = running;
  }
  $("collectButton").disabled = running || !collectionAvailable || !extensionConnected;
  $("estimateButton").disabled = running || !$("bundleSelect").value;
  updateSearchStartButton();
  if (!running) $("signInPrompt").classList.add("hidden");
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
  try {
    const payload = requestPayload();
    const signature = searchConfigurationSignature(payload);
    const value = await api("/api/searches/estimate", {method: "POST", body: JSON.stringify(payload)});
    const candidateCount = ThreeWayUi.exactCandidateCount(value);
    if (signature !== searchConfigurationSignature()) {
      updateSearchStartButton();
      return;
    }
    const caution = candidateCount > 10000000n
      ? " This is a very large run; tighten a size or imbalance filter first."
      : "";
    if (payload.trade_format === "three_team") {
      const adjustmentPolicy = value.free_agent_allocation_policy
        ? ` Automatic roster adjustment policy: ${value.free_agent_allocation_policy}`
        : "";
      $("estimate").textContent = `${compactNumber(candidateCount)} combinations counted exactly for this three-team agreement.${caution}${adjustmentPolicy}`;
      threeTeamEstimateSignature = signature;
    } else {
      $("estimate").textContent = `${compactNumber(candidateCount)} combinations counted exactly across ${value.pair_count} team matchups.${caution}`;
    }
    updateSearchStartButton();
  } catch (error) {
    threeTeamEstimateSignature = null;
    updateSearchStartButton();
    showError(error);
  }
}

async function startSearch(event) {
  event.preventDefault();
  clearError();
  try {
    const payload = requestPayload();
    if (payload.trade_format === "three_team" && threeTeamEstimateSignature !== searchConfigurationSignature(payload)) {
      throw new Error("Count this specific three-team search before starting it.");
    }
    searchRunning = true;
    activeSearchFormat = payload.trade_format;
    activeSearchTeamIds = [payload.primary_team_id, ...payload.counterparty_team_ids];
    updateSearchStartButton();
    const job = await api("/api/searches", {method: "POST", body: JSON.stringify(payload)});
    activeJob = job.job_id;
    $("progressPanel").classList.remove("hidden");
    $("resultsPanel").classList.add("hidden");
    $("cancelButton").classList.remove("hidden");
    $("collectButton").disabled = true;
    $("bundleSelect").disabled = true;
    await pollJob();
  } catch (error) {
    searchRunning = false;
    updateSearchStartButton();
    $("collectButton").disabled = !collectionAvailable || !extensionConnected;
    $("bundleSelect").disabled = bundles.length === 0;
    showError(error);
  }
}

async function pollJob() {
  while (activeJob) {
    let job;
    try { job = await api(`/api/searches/${activeJob}`); }
    catch (error) {
      searchRunning = false;
      updateSearchStartButton();
      $("collectButton").disabled = !collectionAvailable || !extensionConnected;
      $("bundleSelect").disabled = bundles.length === 0;
      $("cancelButton").classList.add("hidden");
      showError(error);
      return;
    }
    renderProgress(job);
    if (!["queued", "running"].includes(job.status)) {
      searchRunning = false;
      updateSearchStartButton();
      $("collectButton").disabled = !collectionAvailable || !extensionConnected;
      $("bundleSelect").disabled = bundles.length === 0;
      $("cancelButton").classList.add("hidden");
      if (job.status === "complete") await loadResults();
      else if (job.status === "failed") showError(job.error || "The search failed.");
      else $("progressText").textContent = "Stopped safely. Start again later to resume from this checkpoint.";
      break;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}

function renderProgress(job) {
  const progress = job.progress;
  if (!progress) {
    $("progressText").textContent = "Preparing the shared season simulation…";
    return;
  }
  const pct = Math.min(100, progress.completion_fraction * 100);
  $("progressBar").style.width = `${pct}%`;
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
      row.power_methodology_status
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

async function loadResults() {
  const value = await api(`/api/searches/${activeJob}/results`);
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
  const body = $("resultsBody");
  body.replaceChildren();
  const threeTeam = value.trade_format === "three_team";
  if (threeTeam) {
    ThreeWayUi.renderTradeRows(
      value.rows,
      activeSearchTeamIds,
      currentBundle(),
      {signed, percent}
    );
  }
  else renderTwoTeamTradeRows(value.rows);
  const powerNotice = value.power_engine_mode === "surrogate"
    ? ` SURROGATE POWER: ${value.power_engine_notice}`
    : threeTeam
      ? ` POWER NOTICE: ${value.power_engine_notice}`
      : "";
  const gainLabel = threeTeam ? "trades improving all three teams first" : "mutual gains first";
  const adjustmentPolicy = value.free_agent_allocation_policy
    ? ` Automatic roster adjustment policy: ${value.free_agent_allocation_policy}`
    : "";
  $("resultsNote").textContent = `Showing ${compactNumber(value.shown_count)} of ${compactNumber(value.total_count_text ?? value.total_count)} qualified trades, with ${gainLabel}.${powerNotice}${adjustmentPolicy}`;
  $("resultsPanel").classList.remove("hidden");
}

async function cancelSearch() {
  if (!activeJob) return;
  try { await api(`/api/searches/${activeJob}/cancel`, {method: "POST", body: ""}); }
  catch (error) { showError(error); }
}

async function exportWorkbook() {
  clearError();
  try {
    const result = await api(`/api/searches/${activeJob}/export`, {method: "POST", body: ""});
    const blob = await api(`/api/exports/${encodeURIComponent(result.filename)}`);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = result.filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) { showError(error); }
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

$("bundleFile").addEventListener("change", async event => {
  clearError();
  const file = event.target.files[0];
  if (!file) return;
  try {
    const record = JSON.parse(await file.text());
    const summary = await api("/api/bundles/import", {method: "POST", body: JSON.stringify(record)});
    await refreshBundles(summary.bundle_id);
  } catch (error) { showError(error); }
});
$("bundleSelect").addEventListener("change", changeBundle);
$("primaryTeam").addEventListener("change", () => {
  syncCounterparties();
  GmInsightsUi.setPrimaryTeam($("primaryTeam").value);
  void TradeTimingUi.setPrimaryTeam($("primaryTeam").value);
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
$("collectionForm").addEventListener("submit", startCollection);
$("cancelCollectionButton").addEventListener("click", cancelCollection);
$("confirmSignInButton").addEventListener("click", confirmCollectionSignIn);
$("estimateButton").addEventListener("click", estimate);
$("searchForm").addEventListener("input", searchConfigurationChanged);
$("searchForm").addEventListener("change", searchConfigurationChanged);
$("searchForm").addEventListener("submit", startSearch);
$("cancelButton").addEventListener("click", cancelSearch);
$("exportButton").addEventListener("click", exportWorkbook);

(async () => {
  try {
    $("collectionSeason").value = String(new Date().getFullYear());
    await pingLifecycle();
    await api("/api/health");
    $("health").textContent = "App running locally";
    await refreshExtensionStatus();
    await refreshBundles();
    setInterval(refreshExtensionStatus, 5000);
  } catch (error) {
    $("health").textContent = "Needs attention";
    showError(error);
  }
})();
