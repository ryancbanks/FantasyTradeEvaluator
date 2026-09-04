"use strict";

window.ProgressUi = (() => {
  const STORAGE_KEY = "fantasy-trade-evaluator.operation-history.v1";
  const MAX_HISTORY_KEYS = 12;
  const histories = readHistories();
  let panelGeneration = 0;
  let activeRunCount = 0;

  function readDeviceValue(key) {
    try {
      const prefix = `${encodeURIComponent(key)}=`;
      const row = document.cookie.split(";").map(value => value.trim())
        .find(value => value.startsWith(prefix));
      return row ? decodeURIComponent(row.slice(prefix.length)) : null;
    } catch (_) {
      return null;
    }
  }

  function writeDeviceValue(key, value) {
    try {
      const name = encodeURIComponent(key);
      if (value === null || value === undefined || value === "") {
        document.cookie = `${name}=; Max-Age=0; Path=/; SameSite=Strict`;
        return;
      }
      document.cookie = `${name}=${encodeURIComponent(String(value))}; Max-Age=31536000; Path=/; SameSite=Strict`;
    } catch (_) {
      // Remembering local UI preferences is optional.
    }
  }

  function setOperationBusy(busy) {
    activeRunCount = Math.max(0, activeRunCount + (busy ? 1 : -1));
    if ((busy && activeRunCount !== 1) || (!busy && activeRunCount !== 0)) return;
    const main = document.querySelector("main");
    if (!main) return;
    if (busy) main.setAttribute("aria-busy", "true");
    else main.removeAttribute("aria-busy");
    for (const element of main.children) {
      if (["errorBanner", "operationProgress"].includes(element.id)) continue;
      if (busy) {
        element.dataset.inertBeforeOperation = element.inert ? "true" : "false";
        element.inert = true;
      } else if (element.dataset.inertBeforeOperation !== undefined) {
        element.inert = element.dataset.inertBeforeOperation === "true";
        delete element.dataset.inertBeforeOperation;
      }
    }
  }

  function readHistories() {
    try {
      const value = JSON.parse(readDeviceValue(STORAGE_KEY) || "{}");
      if (!value || typeof value !== "object" || Array.isArray(value)) return {};
      return Object.fromEntries(Object.entries(value).filter(([key, row]) =>
        typeof key === "string" && key.length <= 80
        && row && typeof row === "object" && !Array.isArray(row)
        && Number.isInteger(row.count) && row.count >= 1
        && Number.isFinite(row.mean_seconds) && row.mean_seconds > 0
        && Number.isFinite(row.deviation_seconds) && row.deviation_seconds >= 0
      ));
    } catch (_) {
      return {};
    }
  }

  function saveHistories() {
    const ordered = Object.entries(histories)
      .sort((left, right) => (right[1].updated_at || 0) - (left[1].updated_at || 0));
    for (const [key] of ordered.slice(MAX_HISTORY_KEYS)) delete histories[key];
    writeDeviceValue(STORAGE_KEY, JSON.stringify(histories));
  }

  function remember(key, seconds) {
    if (typeof key !== "string" || !key || !Number.isFinite(seconds) || seconds <= 0) return;
    const previous = histories[key];
    const count = Number.isInteger(previous?.count) ? previous.count : 0;
    const mean = Number.isFinite(previous?.mean_seconds) ? previous.mean_seconds : seconds;
    const deviation = Number.isFinite(previous?.deviation_seconds)
      ? previous.deviation_seconds
      : seconds * 0.25;
    const weight = count ? 0.3 : 1;
    const nextMean = mean + weight * (seconds - mean);
    histories[key] = {
      count: Math.min(count + 1, 1_000_000),
      mean_seconds: nextMean,
      deviation_seconds: deviation + weight * (Math.abs(seconds - nextMean) - deviation),
      updated_at: Date.now()
    };
    saveHistories();
  }

  function history(key) {
    const row = histories[key];
    if (!row || !Number.isFinite(row.mean_seconds) || row.mean_seconds <= 0) return null;
    return row;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "estimating…";
    const rounded = Math.max(0, Math.round(seconds));
    if (rounded < 60) return `${rounded} sec`;
    const minutes = Math.floor(rounded / 60);
    const remainder = rounded % 60;
    if (minutes < 60) return remainder ? `${minutes} min ${remainder} sec` : `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    const minuteRemainder = minutes % 60;
    return minuteRemainder ? `${hours} hr ${minuteRemainder} min` : `${hours} hr`;
  }

  function formatEtaRange(eta) {
    if (!eta || !Number.isFinite(eta.likely_seconds)) return null;
    const low = Number.isFinite(eta.low_seconds) ? eta.low_seconds : eta.likely_seconds;
    const high = Number.isFinite(eta.high_seconds) ? eta.high_seconds : eta.likely_seconds;
    const lowText = formatDuration(low);
    const highText = formatDuration(high);
    return lowText === highText ? `About ${lowText} left` : `About ${lowText}–${highText} left`;
  }

  function startHistory(key) {
    return {
      key,
      started_at: performance.now(),
      paused_at: null,
      paused_ms: 0,
      finished_at: null,
      finished: false,
      succeeded: null
    };
  }

  function pauseHistory(clock, paused) {
    if (!clock || clock.finished) return;
    if (paused && clock.paused_at === null) clock.paused_at = performance.now();
    if (!paused && clock.paused_at !== null) {
      clock.paused_ms += performance.now() - clock.paused_at;
      clock.paused_at = null;
    }
  }

  function elapsedSeconds(clock) {
    if (!clock) return 0;
    const end = clock.finished_at ?? (
      clock.paused_at === null ? performance.now() : clock.paused_at
    );
    return Math.max(0, (end - clock.started_at - clock.paused_ms) / 1000);
  }

  function finishHistory(clock, succeeded) {
    if (!clock || clock.finished) return;
    pauseHistory(clock, false);
    clock.finished_at = performance.now();
    clock.finished = true;
    clock.succeeded = Boolean(succeeded);
    if (succeeded) remember(clock.key, elapsedSeconds(clock));
  }

  function historicalEta(clock) {
    const row = clock && history(clock.key);
    if (!row) return null;
    const remaining = Math.max(0, row.mean_seconds - elapsedSeconds(clock));
    const spread = Math.max(row.deviation_seconds || 0, row.mean_seconds * 0.2, 1);
    return {
      low_seconds: Math.max(0, remaining - spread),
      likely_seconds: remaining,
      high_seconds: remaining + spread,
      confidence: row.count >= 3 ? "medium" : "low",
      basis: "previous app runs in this browser"
    };
  }

  function describeTiming(operation, clock = null) {
    const activity = operation?.activity;
    const elapsed = Number.isFinite(operation?.elapsed_seconds)
      ? operation.elapsed_seconds
      : elapsedSeconds(clock);
    const terminalStatus = operation?.activity === "terminal"
      ? operation.status
      : clock?.finished
        ? (clock.succeeded ? "complete" : "cancelled")
        : null;
    if (terminalStatus) {
      const label = terminalStatus === "complete"
        ? "Complete"
        : terminalStatus === "failed"
          ? "Failed"
          : "Stopped";
      return `${label} · ${formatDuration(elapsed)} active`;
    }
    if (activity === "paused") return `Waiting for you · ${formatDuration(elapsed)} active`;
    if (operation?.cancel_requested && operation?.status === "running") {
      return `Stopping safely · ${formatDuration(elapsed)} elapsed`;
    }
    const measuredEta = operation?.eta;
    const priorRunEta = measuredEta ? null : historicalEta(clock);
    const etaText = formatEtaRange(measuredEta) || formatEtaRange(priorRunEta);
    const evidence = measuredEta
      ? `${measuredEta.confidence || "low"} confidence, measured this run`
      : priorRunEta
        ? "based on previous app runs in this browser"
        : null;
    return `${formatDuration(elapsed)} elapsed · ${etaText || "Estimating time remaining…"}${evidence ? ` · ${evidence}` : ""}`;
  }

  function setBar(track, bar, fraction, label) {
    const exact = Number.isFinite(fraction) && fraction >= 0 && fraction <= 1;
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute(
      "aria-valuetext",
      label || (exact ? `${(fraction * 100).toFixed(0)}%` : "Working")
    );
    track.classList.toggle("indeterminate", !exact);
    if (exact) {
      const pct = Math.min(100, Math.max(0, fraction * 100));
      track.setAttribute("aria-valuenow", pct.toFixed(1));
      bar.style.width = `${pct}%`;
    } else {
      track.removeAttribute("aria-valuenow");
      bar.style.width = "35%";
    }
  }

  async function run(key, label, task) {
    if (typeof task !== "function") throw new Error("Progress task must be callable.");
    const panel = document.getElementById("operationProgress");
    const track = document.getElementById("operationProgressTrack");
    const bar = document.getElementById("operationProgressBar");
    const text = document.getElementById("operationProgressText");
    const clock = startHistory(key);
    const generation = ++panelGeneration;
    panel.classList.remove("hidden");
    setOperationBusy(true);

    const render = () => {
      if (panelGeneration !== generation) return;
      const estimate = historicalEta(clock);
      const eta = formatEtaRange(estimate);
      const timing = `${formatDuration(elapsedSeconds(clock))} elapsed · ${eta || "Estimating time remaining…"}${eta ? " · based on previous app runs in this browser" : ""}`;
      text.textContent = `${label} · ${timing}`;
      setBar(track, bar, null, `${label}. ${timing}`);
    };
    render();
    const timer = window.setInterval(render, 250);
    try {
      const value = await task();
      window.clearInterval(timer);
      finishHistory(clock, true);
      if (panelGeneration === generation) {
        text.textContent = `${label} complete · ${formatDuration(elapsedSeconds(clock))}`;
        setBar(track, bar, 1, `${label} complete`);
      }
      setOperationBusy(false);
      window.setTimeout(() => {
        if (panelGeneration === generation) panel.classList.add("hidden");
      }, 500);
      return value;
    } catch (error) {
      window.clearInterval(timer);
      finishHistory(clock, false);
      setOperationBusy(false);
      if (panelGeneration === generation) panel.classList.add("hidden");
      throw error;
    }
  }

  return {
    describeTiming,
    finishHistory,
    formatDuration,
    isBusy: () => activeRunCount > 0,
    run,
    readDeviceValue,
    setBar,
    startHistory,
    pauseHistory,
    writeDeviceValue
  };
})();
