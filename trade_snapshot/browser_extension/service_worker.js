"use strict";

importScripts("protocol.js");

const protocol = globalThis.FTEProtocol;
const STORAGE_KEY = "fte_bridge_session_v1";
const PENDING_KEY = "fte_pending_pair_v1";
const DEFAULT_ACTION_DELAY_MS = 500;
const CONTROL_RESPONSE_LIMIT = 256 * 1024;
const PAIR_LIFETIME_MS = 120000;
const WAIT_KEEPALIVE_MS = 10000;
const COLLECTOR_RECOVERY_GRACE_MS = 3000;
// Keep this equal to _RESULT_DELIVERY_GRACE_MS in _extension_capture.py.
const RESULT_DELIVERY_GRACE_MS = 10000;
const RESULT_LIMIT = protocol.CAPABILITIES.maximum_result_bytes;

class BridgeError extends Error {
  constructor(code, retryable = false) {
    super(code);
    this.name = "BridgeError";
    this.code = code;
    this.retryable = retryable;
  }
}

let pendingPair = null;
let session = null;
let scanTabId = null;
let pollGeneration = 0;
let phase = "idle";
let lastError = null;
const activeRequests = new Set();

const startup = restoreSession();

function statusSnapshot() {
  return {
    phase,
    app_origin: session?.appOrigin || pendingPair?.appOrigin || null,
    pair_hint: pendingPair ? pendingPair.pairCode.slice(-4) : null,
    scan_tab_open: Number.isInteger(scanTabId),
    last_error: lastError,
    capabilities: [...protocol.OPERATIONS]
  };
}

function publishStatus(nextPhase = phase, error = lastError) {
  phase = nextPhase;
  lastError = error;
  chrome.runtime.sendMessage({kind: "fte.worker.status", status: statusSnapshot()})
    .catch(() => {});
}

async function restoreSession() {
  try {
    await chrome.storage.session.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
    const storedValues = await chrome.storage.session.get([STORAGE_KEY, PENDING_KEY]);
    const stored = storedValues[STORAGE_KEY];
    if (!validStoredSession(stored)) {
      await chrome.storage.session.remove(STORAGE_KEY);
      const storedPending = storedValues[PENDING_KEY];
      if (!validStoredPendingShape(storedPending)) {
        await chrome.storage.session.remove(PENDING_KEY);
        return;
      }
      const appTab = await chrome.tabs.get(storedPending.app_tab_id).catch(() => null);
      if (!appTab || typeof appTab.url !== "string" ||
          new URL(appTab.url).origin !== storedPending.app_origin) {
        await chrome.storage.session.remove(PENDING_KEY);
        return;
      }
      if (storedPending.expires_at <= Date.now()) {
        await chrome.storage.session.remove(PENDING_KEY);
        publishStatus("idle", "pair_expired");
        await sendLocalEvent(storedPending.app_tab_id, "pair.expired");
        return;
      }
      pendingPair = {
        appOrigin: storedPending.app_origin,
        appTabId: storedPending.app_tab_id,
        pairCode: storedPending.pair_code,
        expiresAt: storedPending.expires_at
      };
      publishStatus("pair_pending", null);
      schedulePairExpiry(pendingPair);
      return;
    }
    await chrome.storage.session.remove(PENDING_KEY);
    const interruptedCommandId = stored.inflight_command_id || null;
    session = {
      appOrigin: stored.app_origin,
      appTabId: stored.app_tab_id,
      token: stored.session_token,
      pollWaitSeconds: stored.poll_wait_seconds,
      actionDelayMs: stored.action_delay_ms,
      analyzerPhase: stored.analyzer_phase,
      inflightCommandId: interruptedCommandId
    };
    scanTabId = stored.scan_tab_id;
    const appTab = await chrome.tabs.get(session.appTabId);
    if (typeof appTab.url !== "string" || new URL(appTab.url).origin !== session.appOrigin) {
      throw new BridgeError("app_tab_origin_changed");
    }
    if (Number.isInteger(scanTabId)) await chrome.tabs.get(scanTabId);
    if (interruptedCommandId) {
      await postCompletion(session, {
        command_id: interruptedCommandId,
        error: "worker_restarted_during_command"
      }, interruptedCommandId, Date.now() + RESULT_DELIVERY_GRACE_MS);
      session.inflightCommandId = null;
      await persistSession();
    }
    publishStatus("paired", null);
    void pollLoop(++pollGeneration, session);
  } catch (_) {
    const closing = session;
    const orphanedScanTab = scanTabId;
    session = null;
    scanTabId = null;
    await chrome.storage.session.remove([STORAGE_KEY, PENDING_KEY]).catch(() => {});
    if (closing) {
      await localRequest(closing.appOrigin, protocol.ENDPOINTS.disconnect, {},
        closing.token, 5000).catch(() => {});
    }
    if (Number.isInteger(orphanedScanTab)) {
      await chrome.tabs.remove(orphanedScanTab).catch(() => {});
    }
    publishStatus("error", "session_restore_failed");
  }
}

function validStoredPendingShape(value) {
  const now = Date.now();
  return protocol.isRecord(value) && protocol.parseLoopbackOrigin(value.app_origin) &&
    Number.isInteger(value.app_tab_id) && protocol.isIdentifier(value.pair_code) &&
    Number.isFinite(value.expires_at) && value.expires_at > 0 &&
    value.expires_at <= now + PAIR_LIFETIME_MS + 5000;
}

function validStoredSession(value) {
  return protocol.isRecord(value) && protocol.parseLoopbackOrigin(value.app_origin) &&
    Number.isInteger(value.app_tab_id) && protocol.isSessionToken(value.session_token) &&
    Number.isFinite(value.poll_wait_seconds) && value.poll_wait_seconds >= 1 &&
    value.poll_wait_seconds <= 30 && Number.isInteger(value.action_delay_ms) &&
    value.action_delay_ms >= 50 && value.action_delay_ms <= 5000 &&
    (value.analyzer_phase === null ||
     ["ordinary_power", "full_playoffs"].includes(value.analyzer_phase)) &&
    (value.scan_tab_id === null || Number.isInteger(value.scan_tab_id)) &&
    (value.inflight_command_id === null || protocol.isIdentifier(value.inflight_command_id));
}

async function persistSession() {
  if (!session) {
    await chrome.storage.session.remove(STORAGE_KEY);
    return;
  }
  await chrome.storage.session.set({
    [STORAGE_KEY]: {
      app_origin: session.appOrigin,
      app_tab_id: session.appTabId,
      session_token: session.token,
      poll_wait_seconds: session.pollWaitSeconds,
      action_delay_ms: session.actionDelayMs,
      analyzer_phase: session.analyzerPhase,
      scan_tab_id: scanTabId,
      inflight_command_id: session.inflightCommandId
    }
  });
}

async function persistPendingPair(pair) {
  await chrome.storage.session.set({
    [PENDING_KEY]: {
      app_origin: pair.appOrigin,
      app_tab_id: pair.appTabId,
      pair_code: pair.pairCode,
      expires_at: pair.expiresAt
    }
  });
}

async function clearPendingPair() {
  pendingPair = null;
  await chrome.storage.session.remove(PENDING_KEY).catch(() => {});
}

function schedulePairExpiry(expectedPair) {
  const remaining = Math.max(0, expectedPair.expiresAt - Date.now());
  setTimeout(() => void expirePendingPair(expectedPair), remaining + 100);
}

async function expirePendingPair(expectedPair) {
  if (pendingPair !== expectedPair) return;
  if (Date.now() < expectedPair.expiresAt) {
    schedulePairExpiry(expectedPair);
    return;
  }
  const appTabId = expectedPair.appTabId;
  await clearPendingPair();
  publishStatus("idle", "pair_expired");
  await sendLocalEvent(appTabId, "pair.expired");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message.kind !== "string") return undefined;
  void handleMessage(message, sender).then(
    (value) => sendResponse({ok: true, value}),
    (error) => sendResponse({ok: false, error: safeError(error)})
  );
  return true;
});

async function handleMessage(message, sender) {
  await startup;
  if (message.kind === "fte.popup.status") return statusSnapshot();
  if (message.kind === "fte.popup.accept") return acceptPendingPair();
  if (message.kind === "fte.popup.reject") {
    const rejectedPair = pendingPair;
    await clearPendingPair();
    publishStatus(session ? "paired" : "idle", null);
    if (rejectedPair) {
      await sendLocalEvent(rejectedPair.appTabId, "pair.rejected");
    }
    return null;
  }
  if (message.kind === "fte.popup.disconnect") {
    await stopSession("user_disconnect", true);
    return null;
  }
  if (message.kind === "fte.local.pair") return receivePair(message.value, sender);
  if (message.kind === "fte.local.disconnect") {
    assertLocalSender(message.value, sender);
    if (session && session.appOrigin === message.value.appOrigin &&
        session.appTabId === sender.tab.id) await stopSession("app_disconnect", true);
    return null;
  }
  throw new BridgeError("unsupported_extension_message");
}

function assertLocalSender(value, sender) {
  if (!protocol.isRecord(value) || !sender.tab || !Number.isInteger(sender.tab.id)) {
    throw new BridgeError("invalid_local_sender");
  }
  let senderOrigin = null;
  try {
    senderOrigin = new URL(sender.url).origin;
  } catch (_) {}
  if (senderOrigin !== value.appOrigin || !protocol.parseLoopbackOrigin(senderOrigin)) {
    throw new BridgeError("invalid_local_sender");
  }
}

async function receivePair(value, sender) {
  assertLocalSender(value, sender);
  if (session) throw new BridgeError("already_paired");
  if (pendingPair) {
    if (pendingPair.expiresAt <= Date.now()) {
      await expirePendingPair(pendingPair);
      throw new BridgeError("pair_expired");
    }
    if (pendingPair.appOrigin !== value.appOrigin ||
        pendingPair.appTabId !== sender.tab.id || pendingPair.pairCode !== value.pairCode) {
      throw new BridgeError("pair_already_pending");
    }
    return null;
  }
  pendingPair = {
    appOrigin: value.appOrigin,
    appTabId: sender.tab.id,
    pairCode: value.pairCode,
    expiresAt: Date.now() + PAIR_LIFETIME_MS
  };
  await persistPendingPair(pendingPair);
  publishStatus("pair_pending", null);
  schedulePairExpiry(pendingPair);
  return null;
}

async function acceptPendingPair() {
  if (!pendingPair || pendingPair.expiresAt <= Date.now()) {
    await clearPendingPair();
    publishStatus("idle", "pair_expired");
    throw new BridgeError("pair_expired");
  }
  const pair = pendingPair;
  await clearPendingPair();
  publishStatus("pairing", null);
  const pairCode = pair.pairCode;
  pair.pairCode = null;
  let response;
  try {
    response = await localRequest(pair.appOrigin, protocol.ENDPOINTS.pair, {
      pair_code: pairCode,
      protocol_version: protocol.VERSION,
      capabilities: [...protocol.OPERATIONS],
      extension_version: chrome.runtime.getManifest().version
    }, null, 10000);
  } catch (error) {
    publishStatus("error", safeError(error));
    await sendLocalEvent(pair.appTabId, "pair.rejected", {error: safeError(error)});
    throw error;
  }
  if (!validPairResponse(response)) {
    publishStatus("error", "invalid_pair_response");
    throw new BridgeError("invalid_pair_response");
  }
  session = {
    appOrigin: pair.appOrigin,
    appTabId: pair.appTabId,
    token: response.session_token,
    pollWaitSeconds: Math.max(1, Math.min(25, response.poll_wait_max_seconds)),
    actionDelayMs: DEFAULT_ACTION_DELAY_MS,
    analyzerPhase: null,
    inflightCommandId: null
  };
  await persistSession();
  publishStatus("paired", null);
  await sendLocalEvent(session.appTabId, "pair.accepted", {
    capabilities: [...protocol.OPERATIONS]
  });
  void pollLoop(++pollGeneration, session);
  return null;
}

function validPairResponse(value) {
  return protocol.isRecord(value) &&
    Object.keys(value).length === 5 && value.protocol_version === protocol.VERSION &&
    value.state === "paired" && protocol.isSessionToken(value.session_token) &&
    Array.isArray(value.capabilities) && arraysEqual(value.capabilities, protocol.OPERATIONS) &&
    Number.isFinite(value.poll_wait_max_seconds) && value.poll_wait_max_seconds >= 1 &&
    value.poll_wait_max_seconds <= 60;
}

function arraysEqual(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

async function pollLoop(generation, owner) {
  let failures = 0;
  while (session === owner && generation === pollGeneration) {
    try {
      publishStatus("waiting", null);
      const response = await localRequest(owner.appOrigin, protocol.ENDPOINTS.poll, {
        wait_seconds: owner.pollWaitSeconds
      }, owner.token, (owner.pollWaitSeconds + 5) * 1000);
      if (isIdleResponse(response)) {
        failures = 0;
        continue;
      }
      const command = protocol.validateOperationEnvelope(response);
      if (!command) throw new BridgeError("invalid_command_response");
      failures = 0;
      await runCommand(owner, command, Date.now() + command.expiresInMs);
    } catch (error) {
      if (session !== owner || generation !== pollGeneration) return;
      if (!(error instanceof BridgeError) || !error.retryable) {
        await stopSession(safeError(error), true);
        return;
      }
      failures += 1;
      if (failures >= 3) {
        await stopSession("bridge_unreachable", true);
        return;
      }
      await delay(500 * (2 ** (failures - 1)));
    }
  }
}

function isIdleResponse(value) {
  return protocol.isRecord(value) && Object.keys(value).length === 2 &&
    value.protocol_version === protocol.VERSION && value.state === "idle";
}

async function runCommand(owner, command, commandDeadline) {
  publishStatus("running", null);
  let result;
  let error = null;
  owner.inflightCommandId = command.commandId;
  await persistSession();
  try {
    const operationDeadline = commandDeadline - RESULT_DELIVERY_GRACE_MS;
    if (operationDeadline <= Date.now()) throw new BridgeError("operation_timeout");
    result = await dispatchOperation(
      command.operation, command.payload, owner, operationDeadline);
    assertResultSize(result);
  } catch (caught) {
    error = safeError(caught);
  }
  const body = error === null ?
    {command_id: command.commandId, result} :
    {command_id: command.commandId, error};
  await postCompletion(owner, body, command.commandId, commandDeadline);
  owner.inflightCommandId = null;
  await persistSession();
  if (command.operation === "session.close" && error === null) {
    await stopSession(command.payload.reason || "complete", true);
  }
}

async function postCompletion(owner, body, commandId, deadline) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) return "expired";
    try {
      const accepted = await localRequest(owner.appOrigin, protocol.ENDPOINTS.result,
        body, owner.token, Math.min(5000, remaining));
      if (!validResultAck(accepted, commandId)) throw new BridgeError("invalid_result_ack");
      return "accepted";
    } catch (error) {
      if (error instanceof BridgeError && error.code === "command_completion_stale") {
        return "stale";
      }
      if (!(error instanceof BridgeError) || !error.retryable || attempt === 2) {
        throw new BridgeError("result_delivery_failed");
      }
      const retryDelay = Math.min(250 * (2 ** attempt), deadline - Date.now());
      if (retryDelay <= 0) return "expired";
      await delay(retryDelay);
    }
  }
  return "expired";
}

function validResultAck(value, commandId) {
  return protocol.isRecord(value) && Object.keys(value).length === 3 &&
    value.protocol_version === protocol.VERSION && value.state === "accepted" &&
    value.command_id === commandId;
}

async function dispatchOperation(operation, payload, owner, deadline) {
  operationTimeRemaining(deadline);
  switch (operation) {
    case "session.open":
      owner.actionDelayMs = payload.action_delay_ms || owner.actionDelayMs;
      await ensureScanTab();
      await persistSession();
      return {opened: true};
    case "session.navigate":
      await navigateScanTab(payload.url,
        Math.min(payload.timeout_ms, operationTimeRemaining(deadline)));
      return {loaded: true};
    case "analyzer.begin":
      owner.analyzerPhase = payload.phase;
      await persistSession();
      return {ok: true};
    case "analyzer.finish":
      return finishAnalyzer(owner, deadline);
    case "analyzer.abort":
      if (Number.isInteger(scanTabId)) {
        try {
          await retryScanAction(operation, payload, deadline, true);
        } catch (error) {
          if (!(error instanceof BridgeError) || !error.retryable) throw error;
        }
      }
      owner.analyzerPhase = null;
      await persistSession();
      return {ok: true};
    case "analyzer.bundle":
    case "analyzer.activate_full":
    case "page.provenance":
    case "ecr.capture":
    case "league.capture":
    case "espn.authenticated_json":
    case "yahoo.scoring":
      return retryScanAction(operation, payload, deadline, true);
    case "projection.capture":
      return captureProjection(payload, owner.actionDelayMs, deadline);
    case "session.wait":
      await waitWithKeepAlive(
        Math.min(payload.timeout_ms, operationTimeRemaining(deadline)));
      return {ok: true};
    case "session.close":
      return {ok: true};
    default:
      throw new BridgeError("unsupported_operation");
  }
}

async function ensureScanTab() {
  if (Number.isInteger(scanTabId)) {
    try {
      await chrome.tabs.get(scanTabId);
      return scanTabId;
    } catch (_) {
      scanTabId = null;
    }
  }
  const tab = await chrome.tabs.create({url: "about:blank", active: true});
  if (!tab || !Number.isInteger(tab.id)) throw new BridgeError("scan_tab_create_failed");
  scanTabId = tab.id;
  await persistSession();
  publishStatus(phase, lastError);
  return scanTabId;
}

async function navigateScanTab(rawUrl, timeoutMs) {
  const parsed = protocol.parseNavigationUrl(rawUrl);
  if (!parsed) throw new BridgeError("navigation_not_allowlisted");
  parsed.hash = protocol.SCAN_MARKER;
  const tabId = await ensureScanTab();
  await new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(updated);
      chrome.tabs.onRemoved.removeListener(removed);
      callback();
    };
    const updated = (id, change) => {
      if (id === tabId && change.status === "complete") finish(resolve);
    };
    const removed = (id) => {
      if (id === tabId) finish(() => reject(new BridgeError("scan_tab_closed")));
    };
    const timer = setTimeout(() =>
      finish(() => reject(new BridgeError("navigation_timeout"))), timeoutMs);
    chrome.tabs.onUpdated.addListener(updated);
    chrome.tabs.onRemoved.addListener(removed);
    chrome.tabs.update(tabId, {url: parsed.href, active: true}).catch((error) =>
      finish(() => reject(new BridgeError(error ? "navigation_failed" : "navigation_failed"))));
  });
}

async function sendScanAction(action, payload, timeoutMs) {
  if (!Number.isInteger(scanTabId)) throw new BridgeError("scan_tab_not_open");
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new BridgeError("collector_timeout")), timeoutMs);
  });
  try {
    const response = await Promise.race([
      chrome.tabs.sendMessage(scanTabId, {
        kind: "fte.scan.action", action, payload, timeout_ms: timeoutMs
      }),
      timeout
    ]);
    if (!protocol.isRecord(response) || response.ok !== true) {
      throw new BridgeError(response?.error || "collector_unavailable", true);
    }
    return response.value;
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    throw new BridgeError("collector_unavailable", true);
  } finally {
    clearTimeout(timer);
  }
}

async function captureProjection(payload, sessionActionDelayMs, deadline) {
  if (payload.request.provider === "espn" && payload.request.horizon === "ros") {
    const segment = await retryScanAction(
      "espn.season_projections",
      {...payload.request, timeout_ms: Math.min(30000, operationTimeRemaining(deadline))},
      deadline,
      true
    );
    const result = {segments: [segment]};
    assertResultSize(result);
    return result;
  }
  const actionDelayMs = payload.action_delay_ms || sessionActionDelayMs;
  let changes = 0;
  let priorContentFingerprint = null;
  const contentChangeProviders = new Set(["fftoday", "fantasysharks"]);
  while (true) {
    const configured = await retryScanAction("projection.configure", payload.request, deadline);
    if (!protocol.isRecord(configured) ||
        !["ready", "changed", "waiting", "error"].includes(configured.action)) {
      throw new BridgeError("projection_configuration_invalid");
    }
    const fingerprint = configured.fingerprint;
    if (contentChangeProviders.has(payload.request.provider) && configured.action !== "error" &&
        (typeof fingerprint !== "string" || !fingerprint.length || fingerprint.length > 8192)) {
      throw new BridgeError("projection_configuration_invalid");
    }
    if (contentChangeProviders.has(payload.request.provider) && configured.action === "changed" &&
        typeof configured.require_change !== "boolean") {
      throw new BridgeError("projection_configuration_invalid");
    }
    if (configured.action === "ready" && priorContentFingerprint !== fingerprint) break;
    if (configured.action === "error") throw new BridgeError("projection_configuration_failed");
    if (configured.action === "changed") {
      if (contentChangeProviders.has(payload.request.provider)) {
        priorContentFingerprint = configured.require_change ? fingerprint : null;
      }
      changes += 1;
      if (changes > 12) throw new BridgeError("projection_configuration_unstable");
    }
    await boundedDelay(actionDelayMs, deadline);
  }

  const segments = [];
  let previous = null;
  let requireChange = null;
  let actions = 0;
  while (true) {
    const stable = await stableProjection(payload.request, deadline, requireChange);
    if (stable.availability === "not_published") {
      return {status: "not_published"};
    }
    const serialized = JSON.stringify(stable);
    if (serialized !== previous) {
      segments.push(stable);
      previous = serialized;
      assertResultSize({segments});
    }
    requireChange = null;
    const advance = await retryScanAction("projection.advance",
      {provider: payload.request.provider}, deadline);
    if (!protocol.isRecord(advance) ||
        !["done", "scroll", "next"].includes(advance.action)) {
      throw new BridgeError("projection_traversal_failed");
    }
    if (advance.action === "done") return {segments};
    actions += 1;
    if (actions > 10000) throw new BridgeError("projection_action_limit");
    if (advance.action === "next") requireChange = previous;
    await boundedDelay(actionDelayMs, deadline);
  }
}

async function finishAnalyzer(owner, deadline) {
  if (!["ordinary_power", "full_playoffs"].includes(owner.analyzerPhase)) {
    throw new BridgeError("analyzer_phase_missing");
  }
  while (Date.now() < deadline) {
    const body = await retryScanAction("analyzer.finish", {}, deadline, true);
    if (body !== null && analyzerBodyMatchesPhase(body, owner.analyzerPhase)) {
      await retryScanAction("analyzer.abort", {}, deadline, true);
      owner.analyzerPhase = null;
      await persistSession();
      return body;
    }
    await boundedDelay(50, deadline);
  }
  owner.analyzerPhase = null;
  await persistSession();
  throw new BridgeError("analyzer_response_timeout");
}

function analyzerBodyMatchesPhase(body, phase) {
  if (!protocol.isRecord(body) || containsAnalyzerError(body)) return false;
  if (phase === "ordinary_power") {
    const periods = ["ros", "dynasty"].filter((name) => Object.hasOwn(body, name));
    if (periods.length !== 1 || Object.hasOwn(body, "playoffs")) return false;
    const rankings = body[periods[0]]?.powerRankings;
    if (!protocol.isRecord(rankings)) return false;
    const before = analyzerTeamIds(rankings.before);
    const after = analyzerTeamIds(rankings.after);
    return before !== null && after !== null && before.length === after.length &&
      before.every((teamId, index) => teamId === after[index]);
  }
  if (Object.hasOwn(body, "ros") || Object.hasOwn(body, "dynasty") ||
      !protocol.isRecord(body.playoffs)) return false;
  return ["oddsBefore_team1", "oddsAfter_team1", "oddsBefore_team2", "oddsAfter_team2"]
    .every((name) => typeof body.playoffs[name] === "number" &&
      Number.isFinite(body.playoffs[name]) && body.playoffs[name] >= 0 &&
      body.playoffs[name] <= 100);
}

function analyzerTeamIds(rows) {
  if (!Array.isArray(rows) || !rows.length) return null;
  const ids = [];
  for (const row of rows) {
    if (!protocol.isRecord(row) || !Object.hasOwn(row, "teamId") ||
        typeof row.score_decimal !== "number" || !Number.isFinite(row.score_decimal)) return null;
    const rawTeamId = row.teamId;
    if (!((Number.isInteger(rawTeamId) && rawTeamId >= 0) ||
          (typeof rawTeamId === "string" && rawTeamId.trim()))) return null;
    const teamId = String(rawTeamId).trim();
    if (!teamId || ids.includes(teamId)) return null;
    ids.push(teamId);
  }
  return ids.sort();
}

function containsAnalyzerError(value) {
  if (Array.isArray(value)) return value.some(containsAnalyzerError);
  if (!protocol.isRecord(value)) return false;
  if (Object.hasOwn(value, "error") || Object.hasOwn(value, "errors")) return true;
  return Object.values(value).some(containsAnalyzerError);
}

async function stableProjection(request, deadline, rejectSerialized) {
  let previous = null;
  let samples = 0;
  while (Date.now() < deadline) {
    const value = await retryScanAction("projection.read", request, deadline);
    const ready = protocol.isRecord(value) && protocol.isRecord(value.source) &&
      Array.isArray(value.tables) && (
        (value.availability === "available" && value.tables.length > 0) ||
        (value.availability === "not_published" && value.tables.length === 0)
      );
    const serialized = ready ? JSON.stringify(value) : null;
    if (ready && serialized !== rejectSerialized) {
      samples = serialized === previous ? samples + 1 : 1;
      previous = serialized;
      if (samples >= 3) return value;
    } else {
      samples = 0;
      previous = null;
    }
    await boundedDelay(200, deadline);
  }
  throw new BridgeError("projection_capture_timeout");
}

async function retryScanAction(
  action, payload, deadline, onlyCollectorUnavailable = false
) {
  let collectorUnavailableSince = null;
  while (Date.now() < deadline) {
    try {
      return await sendScanAction(action, payload, Math.min(30000, deadline - Date.now()));
    } catch (error) {
      if (!(error instanceof BridgeError) || !error.retryable) throw error;
      if (onlyCollectorUnavailable && error.code !== "collector_unavailable") {
        throw error;
      }
      if (error.code === "collector_unavailable") {
        collectorUnavailableSince ??= Date.now();
        if (Date.now() - collectorUnavailableSince >= COLLECTOR_RECOVERY_GRACE_MS) {
          throw new BridgeError("collector_unavailable");
        }
      } else {
        collectorUnavailableSince = null;
      }
      await boundedDelay(200, deadline);
    }
  }
  throw new BridgeError("collector_timeout");
}

async function boundedDelay(milliseconds, deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) throw new BridgeError("operation_timeout");
  await delay(Math.min(milliseconds, remaining));
}

function operationTimeRemaining(deadline) {
  const remaining = Math.floor(deadline - Date.now());
  if (remaining <= 0) throw new BridgeError("operation_timeout");
  return remaining;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitWithKeepAlive(milliseconds) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) {
    await delay(Math.min(WAIT_KEEPALIVE_MS, deadline - Date.now()));
    await chrome.runtime.getPlatformInfo();
  }
}

function assertResultSize(value) {
  let serialized;
  try {
    serialized = JSON.stringify(value);
  } catch (_) {
    throw new BridgeError("result_not_json");
  }
  if (new TextEncoder().encode(serialized).length > RESULT_LIMIT) {
    throw new BridgeError("result_too_large");
  }
}

async function localRequest(origin, path, body, token, timeoutMs) {
  const controller = new AbortController();
  activeRequests.add(controller);
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const headers = {"Accept": "application/json", "Content-Type": "application/json"};
  if (token) headers["X-FTE-Extension-Token"] = token;
  try {
    const response = await fetch(origin + path, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: controller.signal
    });
    const mediaType = (response.headers.get("Content-Type") || "").split(";", 1)[0]
      .trim().toLowerCase();
    if (mediaType !== "application/json") throw new BridgeError("bridge_response_type");
    const contentLength = response.headers.get("Content-Length");
    const declared = contentLength === null ? null : Number(contentLength);
    if (declared !== null && Number.isFinite(declared) &&
        declared > CONTROL_RESPONSE_LIMIT) {
      throw new BridgeError("bridge_response_too_large");
    }
    const text = await readBoundedResponseText(response, CONTROL_RESPONSE_LIMIT);
    if (!text) throw new BridgeError("bridge_response_json");
    try {
      const value = JSON.parse(text);
      if (!response.ok) {
        if (path === protocol.ENDPOINTS.result && response.status === 409 &&
            isStaleCompletionResponse(value)) {
          throw new BridgeError("command_completion_stale");
        }
        throw new BridgeError(response.status >= 500 ? "bridge_server_error" :
          "bridge_request_rejected", response.status >= 500);
      }
      return value;
    } catch (_) {
      if (_ instanceof BridgeError) throw _;
      throw new BridgeError("bridge_response_json");
    }
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    throw new BridgeError("bridge_request_failed", true);
  } finally {
    clearTimeout(timer);
    activeRequests.delete(controller);
  }
}

function isStaleCompletionResponse(value) {
  return protocol.isRecord(value) && Object.keys(value).length === 1 &&
    value.error === "command completion is stale";
}

async function readBoundedResponseText(response, maximumBytes) {
  if (!response.body || typeof response.body.getReader !== "function") {
    throw new BridgeError("bridge_response_body");
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw new BridgeError("bridge_response_body");
      total += value.byteLength;
      if (total > maximumBytes) {
        await reader.cancel().catch(() => {});
        throw new BridgeError("bridge_response_too_large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", {fatal: true}).decode(bytes);
  } catch (_) {
    throw new BridgeError("bridge_response_json");
  }
}

async function stopSession(reason, notifyServer) {
  const closing = session;
  session = null;
  pendingPair = null;
  pollGeneration += 1;
  for (const controller of activeRequests) controller.abort();
  activeRequests.clear();
  await chrome.storage.session.remove([STORAGE_KEY, PENDING_KEY]).catch(() => {});
  if (notifyServer && closing) {
    await localRequest(closing.appOrigin, protocol.ENDPOINTS.disconnect, {},
      closing.token, 5000).catch(() => {});
  }
  const localTab = closing?.appTabId;
  const tabToClose = scanTabId;
  scanTabId = null;
  if (Number.isInteger(tabToClose)) await chrome.tabs.remove(tabToClose).catch(() => {});
  if (Number.isInteger(localTab)) await sendLocalEvent(localTab, "session.closed", {reason});
  publishStatus("idle", reason === "complete" || reason === "cancelled" ||
    reason.endsWith("disconnect") ? null : reason);
}

async function sendLocalEvent(tabId, type, detail = {}) {
  if (!Number.isInteger(tabId)) return;
  await chrome.tabs.sendMessage(tabId, {
    kind: "fte.worker.event",
    event: {type, ...detail}
  }).catch(() => {});
}

function safeError(error) {
  if (error instanceof BridgeError && /^[a-z0-9_]{1,64}$/.test(error.code)) {
    return error.code;
  }
  return "extension_operation_failed";
}

function handleRemovedTab(tabId) {
  if (scanTabId === tabId) {
    scanTabId = null;
    if (session) void stopSession("scan_tab_closed", true);
  } else if (session?.appTabId === tabId) {
    void stopSession("app_tab_closed", true);
  } else if (pendingPair?.appTabId === tabId) {
    void clearPendingPair();
    publishStatus("idle", null);
  }
}

function handleCreatedTab(tab) {
  if (Number.isInteger(scanTabId) && tab.openerTabId === scanTabId &&
      Number.isInteger(tab.id)) {
    void chrome.tabs.remove(tab.id).catch(() => {});
  }
}

function handleUpdatedTab(tabId, change) {
  if (session?.appTabId === tabId && typeof change.url === "string") {
    let origin = null;
    try {
      origin = new URL(change.url).origin;
    } catch (_) {}
    if (origin !== session.appOrigin) void stopSession("app_tab_origin_changed", true);
    return;
  }
  if (pendingPair?.appTabId === tabId && typeof change.url === "string") {
    let origin = null;
    try {
      origin = new URL(change.url).origin;
    } catch (_) {}
    if (origin !== pendingPair.appOrigin) {
      void clearPendingPair().then(() => publishStatus("idle", null));
    }
    return;
  }
  if (tabId !== scanTabId || typeof change.url !== "string") return;
  const parsed = protocol.parseNavigationUrl(change.url);
  if (parsed && parsed.hash !== `#${protocol.SCAN_MARKER}`) {
    parsed.hash = protocol.SCAN_MARKER;
    void chrome.tabs.update(tabId, {url: parsed.href}).catch(() => {});
  }
}

chrome.tabs.onRemoved.addListener((tabId) => {
  void startup.then(() => handleRemovedTab(tabId));
});

chrome.tabs.onCreated.addListener((tab) => {
  void startup.then(() => handleCreatedTab(tab));
});

chrome.tabs.onUpdated.addListener((tabId, change) => {
  void startup.then(() => handleUpdatedTab(tabId, change));
});

chrome.runtime.onSuspend.addListener(() => {
  for (const controller of activeRequests) controller.abort();
});
