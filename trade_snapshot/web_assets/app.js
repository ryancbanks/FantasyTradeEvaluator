"use strict";

const token = document.querySelector('meta[name="app-token"]').content;
const $ = id => document.getElementById(id);
let bundleRows = [];
let activeBundle = null;
let activeJob = null;
let activeCollection = null;
let activeCollectionClock = null;
let activeSearchClock = null;
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
let loadedResults = null;
let loadedResultTeamIds = [];
let loadedResultJobId = null;
let loadedResultBundle = null;
let bundleLoadGeneration = 0;
let bundleCatalogGeneration = 0;
let resultLoadGeneration = 0;
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
    : "After installing or reloading the extension, refresh this page once, then click Connect extension.";
  $("connectExtensionButton").disabled = ProgressUi.isBusy() || extensionConnected || pairing;
  $("connectExtensionButton").textContent = extensionConnected ? "Connected" : pairing ? "Waiting for approval" : "Connect extension";
  const pairCode = $("extensionPairCode");
  const showPairCode = pairing && typeof extensionPairHint === "string";
  pairCode.textContent = showPairCode
    ? `Pairing code ends in ${extensionPairHint} — confirm the same code in the extension.`
    : "";
  pairCode.classList.toggle("hidden", !showPairCode);
  $("collectButton").disabled = ProgressUi.isBusy() || Boolean(activeCollectionClock) ||
    Boolean(activeCollection) || searchRunning ||
    !collectionAvailable || !extensionConnected || !LeagueUi.canCollect();
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
  // A league selection takes effect immediately. Never leave the prior
  // league's engine or results actionable while its replacement is loading.
  ++bundleLoadGeneration;
  ++resultLoadGeneration;
  bundleRows = [];
  activeBundle = null;
  activeJob = null;
  loadedResults = null;
  loadedResultTeamIds = [];
  loadedResultJobId = null;
  loadedResultBundle = null;
  collectionAvailable = false;
  select.replaceChildren(new Option(path ? "Loading weekly history…" : "No league selected", ""));
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
  select.replaceChildren(new Option(bundleRows.length ? "Choose a ready week" : "No weekly bundle yet", ""));
  for (const bundle of bundleRows) {
    const mode = bundle.power_engine_mode === "surrogate" ? " · SURROGATE" : " · exact method";
    const option = new Option(`${bundle.season} · Week ${bundle.week} · ${bundle.team_count} teams${mode}`, bundle.bundle_id);
    select.add(option);
  }
  select.disabled = bundleRows.length === 0;
  select.value = selectId || previous;
  if (!select.value && bundleRows.length === 1 && bundleRows[0].power_engine_mode !== "surrogate") {
    select.value = bundleRows[0].bundle_id;
  }
  $("collectButton").disabled = !collectionAvailable || !extensionConnected || !LeagueUi.canCollect() || Boolean(activeCollection);
  const profile = LeagueUi.selectedProfile();
  $("collectButton").title = collectionAvailable
    ? ""
    : profile?.archived
      ? "Restore this league before collecting a new week."
      : profile && (!profile.espn_league_id || !profile.yahoo_league_id)
        ? "Finish the ESPN and Yahoo connections in League settings."
        : "Import a complete weekly bundle in this build.";
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
    const connection = profile.espn_league_id && profile.yahoo_league_id
      ? "saved ESPN and Yahoo connections"
      : "finish the ESPN and Yahoo connections in League settings";
    $("collectionLeagueDetails").textContent = `${profile.season} · ${profile.scoring} scoring · ${connection}${profile.archived ? " · archived, view only" : ""}`;
  } else if (LeagueUi.isUnassigned()) {
    $("collectionLeagueName").textContent = "Unassigned imports";
    $("collectionLeagueDetails").textContent = "Choose or add a league to scan a new week.";
  } else {
    $("collectionLeagueName").textContent = "Choose a league workspace";
    $("collectionLeagueDetails").textContent = "Its saved season, scoring, ESPN, and Yahoo connection will be used.";
  }
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
    $("assignBundleControls").classList.add("hidden");
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
  const canAssign = LeagueUi.isUnassigned() && $("assignLeagueSelect").options.length > 1;
  $("assignBundleControls").classList.toggle("hidden", !canAssign);
  syncCounterparties();
  ThreeWayUi.syncFormatControls(bundle);
  updateSearchStartButton();
}

async function changeBundle() {
  clearError();
  const generation = ++bundleLoadGeneration;
  const bundleId = $("bundleSelect").value;
  activeBundle = null;
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
    if (generation !== bundleLoadGeneration || $("bundleSelect").value !== bundleId) return;
    activeBundle = loaded;
  }
  renderBundle();
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
  $("startButton").disabled = !bundleReady || Boolean(activeCollectionClock) ||
    Boolean(activeCollection) || searchRunning || !estimateCurrent;
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
  $("collectButton").disabled = running || !collectionAvailable || !extensionConnected || !LeagueUi.canCollect();
  $("bundleSelect").disabled = running || bundleRows.length === 0;
  $("bundleFile").disabled = running || Boolean(activeCollection);
  $("estimateButton").disabled = running || Boolean(activeCollection) || !currentBundle();
  LeagueUi.setBusy(running || Boolean(activeCollection));
}

function invalidateSearchEstimate() {
  threeTeamEstimateSignature = null;
  if (isThreeTeam() && currentBundle()) {
    $("estimate").textContent = "Count this specific three-team search before starting it.";
  }
  updateSearchStartButton();
}

async function startCollection(event) {
  event.preventDefault();
  clearError();
  try {
    if (!extensionConnected) throw new Error("Connect the browser extension before scanning the league.");
    activeCollectionClock = ProgressUi.startHistory("weekly-collection");
    setCollectionRunning(true);
    $("collectionProgress").classList.remove("hidden");
    ProgressUi.setBar(
      $("collectionProgress").querySelector(".progress-track"),
      $("collectionProgressBar"),
      null,
      "Validating the selected league workspace"
    );
    $("collectionProgressText").textContent = "Validating the selected league workspace…";
    $("collectionTiming").textContent = ProgressUi.describeTiming(null, activeCollectionClock);
    const job = await api("/api/weekly-collections", {
      method: "POST",
      body: JSON.stringify(LeagueUi.collectionPayload())
    });
    activeCollection = job.job_id;
    setCollectionRunning(true);
    renderCollectionProgress(job);
    await pollCollection();
  } catch (error) {
    ProgressUi.finishHistory(activeCollectionClock, false);
    activeCollectionClock = null;
    setCollectionRunning(false);
    showError(error);
  }
}

async function pollCollection() {
  while (activeCollection) {
    let job;
    try { job = await api(`/api/weekly-collections/${activeCollection}`); }
    catch (error) { showError(error); break; }
    const terminal = !["queued", "running"].includes(job.status);
    if (terminal) {
      ProgressUi.finishHistory(activeCollectionClock, job.status === "complete");
    }
    renderCollectionProgress(job);
    if (terminal) {
      const selectedBundle = job.bundle_id;
      activeCollectionClock = null;
      activeCollection = null;
      setCollectionRunning(false);
      if (job.status === "complete") await refreshBundles(selectedBundle);
      else if (job.status === "failed") {
        if (selectedBundle) {
          await LeagueUi.refresh(LeagueUi.UNASSIGNED, {notify: false});
          await refreshBundles(selectedBundle);
        }
        showError(job.error || "No new weekly bundle was published.");
      }
      else $("collectionProgressText").textContent = "Collection stopped safely. No incomplete week was published.";
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  ProgressUi.finishHistory(activeCollectionClock, false);
  activeCollectionClock = null;
  activeCollection = null;
  setCollectionRunning(false);
}

function renderCollectionProgress(job) {
  $("collectionProgress").classList.remove("hidden");
  const progress = job.progress;
  const fraction = job.operation?.progress?.determinate
    ? job.operation.progress.fraction
    : null;
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
  $("collectionTiming").textContent = ProgressUi.describeTiming(job.operation, activeCollectionClock);
  const labels = {fantasypros: "FantasyPros", espn: "ESPN", yahoo: "Yahoo"};
  $("signInPrompt").classList.toggle("hidden", !pending);
  if (pending) {
    $("signInPromptText").textContent = `If needed, finish signing in to ${labels[pending] || pending} on the extension’s scan tab, then continue.`;
    $("confirmSignInButton").disabled = false;
  }
}

function setCollectionRunning(running) {
  $("cancelCollectionButton").classList.toggle("hidden", !running || !activeCollection);
  for (const element of $("collectionForm").elements) {
    if (element.id !== "cancelCollectionButton") element.disabled = running;
  }
  LeagueUi.setBusy(running || searchRunning);
  $("collectButton").disabled = running || !collectionAvailable || !extensionConnected || !LeagueUi.canCollect();
  $("bundleFile").disabled = running || searchRunning;
  $("bundleSelect").disabled = running || bundleRows.length === 0;
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
  $("estimateButton").disabled = true;
  try {
    const payload = requestPayload();
    const signature = searchConfigurationSignature(payload);
    const value = await ProgressUi.run(
      "candidate-count",
      "Counting valid trade combinations",
      () => api("/api/searches/estimate", {method: "POST", body: JSON.stringify(payload)})
    );
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
  } finally {
    $("estimateButton").disabled = Boolean(activeCollection) || !currentBundle();
  }
}

async function startSearch(event) {
  event.preventDefault();
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
      bundle: currentBundle()
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
      ProgressUi.finishHistory(activeSearchClock, false);
      activeSearchClock = null;
      if (activeJob === context.jobId) activeJob = null;
      setSearchRunning(false);
      $("cancelButton").classList.add("hidden");
      showError(error);
      return;
    }
    if (activeJob !== context.jobId) return;
    const terminal = !["queued", "running"].includes(job.status);
    if (terminal) {
      ProgressUi.finishHistory(activeSearchClock, job.status === "complete");
    }
    renderProgress(job);
    if (terminal) {
      $("cancelButton").classList.add("hidden");
      try {
        if (job.status === "complete") await loadResults(context);
        else if (job.status === "failed") showError(job.error || "The search failed.");
        else $("progressText").textContent = "Stopped safely. Start again later to resume from this checkpoint.";
      } finally {
        activeSearchClock = null;
        if (activeJob === context.jobId) activeJob = null;
        setSearchRunning(false);
      }
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  // A defensive cleanup for a programmatic bundle/profile change. Normal UI
  // controls are locked while searching, but an interrupted context must never
  // leave the visible job state or timer stranded.
  if (activeJob === null && searchRunning) {
    ProgressUi.finishHistory(activeSearchClock, false);
    activeSearchClock = null;
    setSearchRunning(false);
    $("cancelButton").classList.add("hidden");
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
  $("searchTiming").textContent = ProgressUi.describeTiming(job.operation, activeSearchClock);
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
      {signed, percent}
    );
  }
  else renderTwoTeamTradeRows(rows);
  const powerNotice = value.power_engine_mode === "surrogate"
    ? ` SURROGATE POWER: ${value.power_engine_notice}`
    : threeTeam
      ? ` POWER NOTICE: ${value.power_engine_notice}`
      : "";
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
  clearError();
  $("exportButton").disabled = true;
  try {
    const jobId = loadedResultJobId;
    if (!jobId) throw new Error("Run a trade search before exporting its results.");
    const [result, blob] = await ProgressUi.run(
      "excel-export",
      "Building and downloading the Excel workbook",
      async () => {
        const created = await api(`/api/searches/${jobId}/export`, {method: "POST", body: ""});
        return [created, await api(`/api/exports/${encodeURIComponent(created.filename)}`)];
      }
    );
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = result.filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) { showError(error); }
  finally { $("exportButton").disabled = false; }
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
    const importPath = LeagueUi.importPath();
    const selectedLeagueId = LeagueUi.selectedId();
    await ProgressUi.run("bundle-import", "Validating and importing weekly data", async () => {
      const record = JSON.parse(await file.text());
      const summary = await api(importPath, {method: "POST", body: JSON.stringify(record)});
      if (!selectedLeagueId) {
        await LeagueUi.refresh(LeagueUi.UNASSIGNED, {notify: false});
      }
      await refreshBundles(summary.bundle_id);
    });
  } catch (error) { showError(error); }
  finally { event.target.value = ""; }
});
$("bundleSelect").addEventListener("change", () => changeBundle().catch(showError));
$("primaryTeam").addEventListener("change", () => {
  syncCounterparties();
  LeagueUi.saveMyTeam($("primaryTeam").value, $("bundleSelect").value).catch(showError);
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
$("assignBundleButton").addEventListener("click", () => {
  const bundle = currentBundle();
  if (bundle) LeagueUi.assignBundle(bundle.bundle_id).catch(showError);
});
for (const id of ["onlyMutualResults", "minimumResultGain", "resultSort"]) {
  $(id).addEventListener("change", () => {
    try { renderLoadedResults(); }
    catch (error) { showError(error); }
  });
}

LeagueUi.bind({
  api,
  onSelection: async () => {
    clearError();
    await refreshBundles();
  },
  onError: showError
});

(async () => {
  try {
    await pingLifecycle();
    await api("/api/health");
    $("health").textContent = "App running locally";
    await refreshExtensionStatus();
    await LeagueUi.refresh();
    setInterval(refreshExtensionStatus, 5000);
  } catch (error) {
    $("health").textContent = "Needs attention";
    showError(error);
  }
})();
