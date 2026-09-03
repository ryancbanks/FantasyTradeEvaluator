"use strict";

window.DraftLab = (() => {
  const $ = id => document.getElementById(id);
  const STRATEGIES = ["none", "streaming_qb", "streaming_te", "streaming_dst", "late_round_qb"];
  let catalog = {corpora: [], boards: [], models: [], checkpoints: [], assistant_sessions: [], league_presets: []};
  let activeJob = null, jobLaunching = false, promotionBusy = false, assistantBusy = false;
  let pendingRecoveredJob = null;
  let assistantSyncBusy = false, assistantSyncTimer = null;
  let draftSurfaceActive = $("draftLabTab").getAttribute("aria-selected") === "true";
  let externalWorkBusy = Boolean(window.TradeAppBusy);
  let publishedDraftBusy = null;
  let lastTrainingResult = null;
  let assistantStatus = null;
  let assistantPlayers = [];
  let presetInitialized = false;

  function element(tag, className = "", text = "") {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== "") value.textContent = String(text);
    return value;
  }

  function cell(row, value, className = "") { const valueCell = element("td", className, value); row.append(valueCell); return valueCell; }

  function emptyRow(body, columns, message) {
    body.replaceChildren();
    const row = element("tr");
    const value = cell(row, message, "draft-empty-row");
    value.colSpan = columns;
    body.append(row);
  }

  function reportError(error) {
    const banner = $("draftErrorBanner");
    banner.textContent = error instanceof Error ? error.message : String(error);
    banner.classList.remove("hidden");
    banner.focus({preventScroll: true});
  }

  function clearDraftError() { $("draftErrorBanner").classList.add("hidden"); }
  function number(id) {
    const value = Number($(id).value);
    if (!Number.isFinite(value)) throw new Error(`${$(id).previousElementSibling?.textContent || id} must be a number.`);
    return value;
  }

  function integer(id) {
    const value = number(id);
    if (!Number.isInteger(value)) throw new Error(`${$(id).previousElementSibling?.textContent || id} must be a whole number.`);
    return value;
  }

  function jsonObject(id, label) {
    let value;
    try { value = JSON.parse($(id).value); }
    catch (_) { throw new Error(`${label} must be valid JSON.`); }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`${label} must be a JSON object.`);
    }
    return value;
  }

  function list(id, label) {
    const values = $(id).value.split(",").map(value => value.trim()).filter(Boolean);
    if (!values.length) throw new Error(`${label} cannot be empty.`);
    return values;
  }

  function weeks(id, label) {
    const result = [];
    for (const part of list(id, label)) {
      const match = part.match(/^(\d+)(?:\s*-\s*(\d+))?$/);
      if (!match) throw new Error(`${label} must use comma-separated weeks or ranges such as 1-14.`);
      const first = Number(match[1]);
      const last = Number(match[2] || match[1]);
      if (first < 1 || last < first || last > 25) throw new Error(`${label} contains an invalid week range.`);
      for (let week = first; week <= last; week += 1) result.push(week);
    }
    return [...new Set(result)].sort((left, right) => left - right);
  }

  function selectedYears() {
    const years = [...$("draftTrainingYears").querySelectorAll('input[type="checkbox"]:checked')]
      .map(input => Number(input.value)).sort((left, right) => left - right);
    if (!years.length) throw new Error("Choose at least one training year.");
    return years;
  }

  function syncTrainingYears() {
    const corpus = catalog.corpora.find(row => row.corpus_id === $("draftCorpus").value); const years = new Set(corpus ? corpus.seasons : catalog.supported_training_seasons || []);
    for (const input of $("draftTrainingYears").querySelectorAll('input[type="checkbox"]')) { const available = years.has(Number(input.value)); input.disabled = Boolean(corpus) && !available; input.checked = available; }
  }

  function strategyCounts() {
    return Object.fromEntries(STRATEGIES.map(strategy => {
      const value = Number(document.querySelector(`[data-strategy="${strategy}"]`).value);
      if (!Number.isInteger(value) || value < 0) throw new Error("Strategy seat counts must be non-negative whole numbers.");
      return [strategy, value];
    }));
  }

  function updateStrategyTotal() {
    const teamCount = Number($("draftTeamCount").value) || 0;
    const total = STRATEGIES.reduce((sum, strategy) => {
      const value = Number(document.querySelector(`[data-strategy="${strategy}"]`).value);
      return sum + (Number.isFinite(value) ? value : 0);
    }, 0);
    const output = $("draftStrategyTotal");
    output.textContent = `${total} of ${teamCount} seats assigned`;
    output.classList.toggle("invalid", total !== teamCount);
    return total === teamCount;
  }

  function leagueConfig() {
    const teamCount = integer("draftTeamCount");
    const strategies = strategyCounts();
    const playoffTeamCount = integer("draftPlayoffTeams");
    const playoffWeeks = weeks("draftPlayoffWeeks", "Playoff weeks");
    const startingSlots = list("draftStarterSlots", "Starter slots").map(value => value.toUpperCase());
    if (Object.values(strategies).reduce((sum, value) => sum + value, 0) !== teamCount) {
      throw new Error("Strategy seat counts must add up to the league team count.");
    }
    if (playoffTeamCount > teamCount || playoffWeeks.length !== Math.max(1, Math.ceil(Math.log2(playoffTeamCount)))) {
      throw new Error("Playoff teams and weeks must form a complete bracket with one week per round.");
    }
    if (startingSlots.length > 16) throw new Error("Draft Lab supports up to 16 starting slots per team.");
    return {
      name: $("draftLeagueName").value.trim(),
      team_count: teamCount,
      starting_slots: startingSlots,
      bench_slots: integer("draftBenchSlots"),
      slot_eligibility: jsonObject("draftSlotEligibility", "Slot eligibility"),
      position_limits: jsonObject("draftPositionLimits", "Position limits"),
      scoring_weights: jsonObject("draftScoringWeights", "Scoring"),
      regular_season_weeks: weeks("draftRegularWeeks", "Regular-season weeks"),
      playoff_team_count: playoffTeamCount,
      playoff_weeks: playoffWeeks,
      strategy_counts: strategies
    };
  }

  function evolutionConfig() {
    return {
      population_size: integer("draftPopulation"),
      generations: integer("draftGenerations"),
      appearances_per_generation: integer("draftAppearances"),
      elite_fraction: number("draftEliteFraction"),
      mutation_rate: number("draftMutationRate"),
      mutation_magnitude: integer("draftMutationMagnitude"),
      candidate_window: integer("draftCandidateWindow"),
      training_years: selectedYears(),
      seed: integer("draftSeed")
    };
  }

  function trainingPayload() {
    if (!$("draftCorpus").value) throw new Error("Import and choose a historical corpus first.");
    return {
      corpus_id: $("draftCorpus").value,
      league_config: leagueConfig(),
      evolution_config: evolutionConfig()
    };
  }

  function optionLabel(type, row) {
    if (type === "corpus") return `${row.seasons.join(", ")} · ${row.season_count} season${row.season_count === 1 ? "" : "s"} · ${row.player_seasons} player-seasons`;
    if (type === "board") return `${row.season} board · ${row.player_count} players · ${row.positions.join("/")}`;
    if (type === "model") return `${row.league_name} · generation ${row.generation} · ${row.trained_seasons.join(", ")}`;
    if (type === "checkpoint") return `${row.league_name} · generation ${row.generation_completed} of ${row.generation_count} · fitness ${row.champion_fitness.toFixed(2)}`;
    if (type === "session") {
      const model = catalog.models.find(item => item.model_id === row.model_id);
      const board = catalog.boards.find(item => item.board_id === row.board_id);
      const source = row.draft_binding ? ` · ESPN ${row.draft_binding.league_id}` : "";
      return `${model?.league_name || "Saved room"} · ${board?.season || "current"} board · Drafter #${row.user_drafter_number}${source} · ${row.pick_count} picks`;
    }
    const source = row.source === "synced_league"
      ? `Synced ${row.season} · Week ${row.week} · ${row.preset_id.slice(-8)}`
      : "Built-in";
    return `${source} · ${row.config.name}`;
  }

  function fillSelect(id, rows, valueKey, type, placeholder) {
    const select = $(id);
    const previous = select.value;
    select.replaceChildren(new Option(placeholder, ""));
    for (const row of rows.filter(item => item.status !== "invalid")) {
      select.add(new Option(optionLabel(type, row), row[valueKey]));
    }
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  }

  function applyPreset() {
    const preset = catalog.league_presets.find(row => row.preset_id === $("draftPreset").value);
    if (!preset) return;
    if (!preset.config) {
      $("draftPresetNotice").textContent = preset.compatibility_notice || "This synced league cannot be represented safely by Draft Lab.";
      return;
    }
    const config = preset.config;
    const selectedNotice = preset.compatibility_notice || (
      preset.source === "built_in"
        ? "The built-in preset applies every declared league and scoring rule exactly."
        : "This preset applies the structural rules available in the synced league record."
    );
    const unavailable = catalog.league_presets.filter(row => !row.config).map(row => row.compatibility_notice).filter(Boolean);
    $("draftPresetNotice").textContent = [selectedNotice, ...unavailable.map(value => `Unavailable synced preset: ${value}`)].join(" ");
    $("draftLeagueName").value = config.name;
    $("draftTeamCount").value = String(config.team_count);
    $("draftStarterSlots").value = config.starting_slots.join(",");
    $("draftBenchSlots").value = String(config.bench_slots);
    $("draftRegularWeeks").value = config.regular_season_weeks.join(",");
    $("draftPlayoffTeams").value = String(config.playoff_team_count);
    $("draftPlayoffWeeks").value = config.playoff_weeks.join(",");
    $("draftScoringWeights").value = JSON.stringify(config.scoring_weights, null, 2);
    $("draftSlotEligibility").value = JSON.stringify(config.slot_eligibility, null, 2);
    $("draftPositionLimits").value = JSON.stringify(config.position_limits, null, 2);
    for (const strategy of STRATEGIES) {
      document.querySelector(`[data-strategy="${strategy}"]`).value = String(config.strategy_counts[strategy] || 0);
    }
    $("assistantUserSlot").max = String(config.team_count);
    updateStrategyTotal();
  }

  function updateSavedControls() {
    const checkpointUnavailable = externalWorkBusy || Boolean(activeJob) || jobLaunching || promotionBusy || !$("draftCheckpoint").value;
    $("draftResumeButton").disabled = checkpointUnavailable;
    $("draftPromoteButton").disabled = checkpointUnavailable;
    $("assistantOpen").disabled = externalWorkBusy || assistantBusy || !$("assistantSession").value;
  }

  function selectCheckpoint() {
    const checkpoint = catalog.checkpoints.find(
      row => row.checkpoint_job_id === $("draftCheckpoint").value
    );
    if (checkpoint) {
      const nextTarget = Math.min(
        1000,
        Math.max(integer("draftGenerations"), checkpoint.generation_count,
          checkpoint.generation_completed + 1)
      );
      $("draftGenerations").value = String(nextTarget);
    }
    updateSavedControls();
  }

  async function refreshCatalog({selectModelId = "", selectBoardId = "", selectSessionId = "", selectCheckpointId = ""} = {}) {
    const previousCorpusId = $("draftCorpus").value; catalog = await api("/api/draft/catalog");
    catalog.checkpoints ||= [];
    catalog.assistant_sessions ||= [];
    fillSelect("draftCorpus", catalog.corpora, "corpus_id", "corpus", "Import a corpus first");
    fillSelect("draftBenchmarkModel", catalog.models, "model_id", "model", "Train or import a model first");
    fillSelect("assistantModel", catalog.models, "model_id", "model", "Train or import a model first");
    fillSelect("assistantBoard", catalog.boards, "board_id", "board", "Import a current board first");
    fillSelect("draftCheckpoint", catalog.checkpoints, "checkpoint_job_id", "checkpoint", "No saved checkpoints yet");
    fillSelect("assistantSession", catalog.assistant_sessions, "session_id", "session", "No saved draft rooms yet");

    const preset = $("draftPreset");
    const previousPreset = preset.value;
    preset.replaceChildren(new Option("Choose a built-in or synced league", ""));
    for (const row of catalog.league_presets) {
      const label = row.config ? optionLabel("preset", row) : `Unavailable synced league · ${row.compatibility_notice || "unsupported rules"}`;
      const option = new Option(label, row.preset_id);
      option.disabled = !row.config || row.status === "unsupported";
      preset.add(option);
    }
    if (previousPreset && [...preset.options].some(option => option.value === previousPreset)) preset.value = previousPreset;
    else preset.value = catalog.league_presets.find(row => row.config && row.status !== "unsupported")?.preset_id || "";
    if (!presetInitialized) {
      presetInitialized = true;
      applyPreset();
    }
    if (selectModelId) {
      $("draftBenchmarkModel").value = selectModelId;
      $("assistantModel").value = selectModelId;
    }
    if (selectBoardId) $("assistantBoard").value = selectBoardId;
    if (selectSessionId) $("assistantSession").value = selectSessionId;
    if (selectCheckpointId) $("draftCheckpoint").value = selectCheckpointId;
    updateSavedControls();
    $("draftCatalogSummary").textContent = `${catalog.corpora.length} corpora · ${catalog.models.length} models · ${catalog.boards.length} current boards · ${catalog.checkpoints.length} autosaves · ${catalog.assistant_sessions.length} rooms`;
    $("draftYearNotice").textContent = catalog.year_notice;
    if ($("draftCorpus").value !== previousCorpusId) syncTrainingYears();
    return catalog;
  }

  async function importJson(input, path, statusId, type) {
    clearDraftError();
    const file = input.files[0];
    if (!file) return;
    const maximumBytes = {corpus: 128, board: 128, model: 16}[type] * 1024 * 1024;
    if (file.size > maximumBytes) {
      const labels = {corpus: "Historical corpus", board: "Current board", model: "Draft model"};
      reportError(new Error(`${labels[type]} files must be ${maximumBytes / 1024 / 1024} MB or smaller.`));
      input.value = "";
      return;
    }
    const status = $(statusId);
    status.textContent = `Importing ${file.name}…`;
    status.classList.remove("ready");
    try {
      const record = JSON.parse(await file.text());
      const summary = await api(path, {method: "POST", body: JSON.stringify(record)});
      status.textContent = `${file.name} imported`;
      status.classList.add("ready");
      await refreshCatalog({
        selectModelId: type === "model" ? summary.model_id : "",
        selectBoardId: type === "board" ? summary.board_id : ""
      });
      if (type === "corpus") { $("draftCorpus").value = summary.corpus_id; syncTrainingYears(); }
    } catch (error) { reportError(error); status.textContent = "Import failed"; }
    finally { input.value = ""; }
  }

  function setJobRunning(running, kind = "") {
    const blocked = running || externalWorkBusy;
    $("draftEstimateButton").disabled = blocked;
    $("draftStartButton").disabled = blocked;
    $("draftBenchmarkButton").disabled = blocked;
    $("draftCancelButton").classList.toggle("hidden", !running || !activeJob);
    $("draftCancelButton").textContent = kind === "benchmark" ? "Stop benchmark" : "Stop and keep last autosave";
    updateSavedControls();
    publishDraftActivity();
  }

  function publishDraftActivity() {
    const busy = Boolean(activeJob) || Boolean(pendingRecoveredJob) || jobLaunching || promotionBusy || assistantBusy || assistantSyncBusy;
    if (publishedDraftBusy === busy) return;
    publishedDraftBusy = busy;
    window.dispatchEvent(new CustomEvent("draftactivitychange", {detail: {busy}}));
  }

  function applyExternalWorkState(busy) {
    externalWorkBusy = Boolean(busy);
    setJobRunning(Boolean(activeJob) || jobLaunching, activeJob?.kind || "");
    filterAssistantPlayers();
  }

  function renderJob(job) {
    const progress = job.progress || {};
    const training = job.kind === "training";
    const done = training ? progress.generation || 0 : progress.trial || 0;
    const total = training ? progress.generation_count || 0 : progress.trial_count || 0;
    const partialGeneration = training && progress.arena_count
      ? progress.arena / progress.arena_count : 0;
    const fraction = total ? Math.min(1, (done + partialGeneration) / total) : 0;
    $("draftProgressBar").style.width = `${(fraction * 100).toFixed(1)}%`;
    $("draftProgressText").textContent = training
      ? `${progress.current_generation ? `Generation ${progress.current_generation} · arena ${progress.arena} of ${progress.arena_count}` : `Generation ${done} of ${total}`}${progress.champion_fitness == null ? "" : ` · champion ${progress.champion_fitness.toFixed(2)}`}${progress.estimated_remaining_seconds == null ? "" : ` · about ${formatDuration(progress.estimated_remaining_seconds)} remaining`}`
      : `Paired scenario ${done} of ${total}`;
    $("draftAutosaveStatus").textContent = progress.autosaved
      ? `Autosaved generation ${done} · safe to stop or resume later`
      : training ? "Autosaved after every completed generation" : "Benchmark results are published when all paired trials finish";
  }

  async function acknowledgeJobActivity(jobId) {
    try {
      await api(`/api/draft/jobs/${jobId}/activity-ack`, {method: "POST", body: ""});
    } catch (_) {
      /* Keep the retained result recoverable if acknowledgement is interrupted. */
    }
  }

  async function monitorJob(job) {
    activeJob = {jobId: job.job_id, kind: job.kind};
    setJobRunning(true, job.kind);
    $("draftProgress").classList.remove("hidden");
    renderJob(job);
    while (activeJob?.jobId === job.job_id) {
      const current = await api(`/api/draft/jobs/${job.job_id}`);
      renderJob(current);
      if (!["queued", "running"].includes(current.status)) {
        activeJob = null;
        setJobRunning(false);
        if (current.kind === "training" && current.status !== "complete") await refreshCatalog({selectCheckpointId: current.job_id});
        if (current.status === "complete") {
          const result = await api(`/api/draft/jobs/${job.job_id}/result`);
          if (current.kind === "training") await renderTrainingResult(result);
          else renderBenchmarkResult(result);
        } else if (current.status === "failed") {
          $("draftProgressText").textContent = "Draft Lab job failed.";
          reportError(new Error(current.error || "Draft Lab job failed."));
        } else {
          $("draftProgressText").textContent = current.error || "Stopped safely.";
        }
        await acknowledgeJobActivity(job.job_id);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 400));
    }
  }

  function restoreDraftJob(job) {
    if (!job || activeJob || jobLaunching) return;
    pendingRecoveredJob = null;
    void monitorJob(job).catch(error => {
      if (activeJob?.jobId === job.job_id) {
        setJobRunning(true, job.kind);
        reportError(new Error(
          `${error.message} Draft Lab may still be running locally; refresh this page to reconnect.`
        ));
      } else {
        setJobRunning(false);
        reportError(error);
      }
    });
  }

  async function estimateTraining() {
    clearDraftError();
    try {
      const value = await api("/api/draft/trainings/estimate", {method: "POST", body: JSON.stringify(trainingPayload())});
      const seasonLabel = `${value.training_season_count.toLocaleString()} selected season${value.training_season_count === 1 ? "" : "s"}`;
      const checkpoint = formatBytes(value.estimated_checkpoint_bytes);
      const memory = formatBytes(value.estimated_population_memory_bytes);
      $("draftEstimate").textContent = `${value.total_leagues.toLocaleString()} simulated leagues across ${seasonLabel} · ${value.brain_appearances.toLocaleString()} brain appearances · about ${value.candidate_scores_estimate.toLocaleString()} neural scores · ${value.network_parameters.toLocaleString()} learned parameters per brain · about ${checkpoint} per autosave and ${memory} working memory. ${value.size_notice}`;
    } catch (error) { reportError(error); }
  }

  function formatBytes(value) {
    if (value < 1024) return `${value} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let amount = value / 1024, index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toFixed(amount >= 100 ? 0 : 1)} ${units[index]}`;
  }

  function formatDuration(value) {
    const seconds = Math.max(0, Math.round(value));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60), remainder = minutes % 60;
    return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
  }

  async function launchJob(path, payload, kind) {
    if (externalWorkBusy || jobLaunching || activeJob) return;
    jobLaunching = true; setJobRunning(true, kind);
    try {
      const job = await api(path, {method: "POST", body: JSON.stringify(payload)});
      jobLaunching = false;
      await monitorJob(job);
    } catch (error) {
      jobLaunching = false;
      if (activeJob) {
        setJobRunning(true, activeJob.kind);
        reportError(new Error(
          `${error.message} Draft Lab may still be running locally; refresh this page to reconnect.`
        ));
      } else {
        setJobRunning(false);
        reportError(error);
      }
    }
  }

  async function startTraining(event) {
    event.preventDefault();
    clearDraftError();
    try { await launchJob("/api/draft/trainings", trainingPayload(), "training"); }
    catch (error) { reportError(error); }
  }

  async function resumeTraining() {
    clearDraftError();
    try {
      if (!$("draftCheckpoint").value) throw new Error("Choose a saved training checkpoint first.");
      await launchJob("/api/draft/trainings/resume", {
        checkpoint_job_id: $("draftCheckpoint").value,
        generations: integer("draftGenerations")
      }, "training");
    } catch (error) { reportError(error); }
  }

  async function promoteCheckpoint() {
    if (externalWorkBusy || promotionBusy) return;
    clearDraftError();
    promotionBusy = true;
    updateSavedControls();
    publishDraftActivity();
    try {
      const checkpointId = $("draftCheckpoint").value;
      if (!checkpointId) throw new Error("Choose a saved training checkpoint first.");
      const model = await api(
        `/api/draft/checkpoints/${checkpointId}/promote`,
        {method: "POST", body: ""}
      );
      $("draftDownloadModel").dataset.modelId = model.model_id;
      $("draftDownloadModel").classList.remove("hidden");
      $("draftEstimate").textContent = `Generation ${model.generation} champion is ready as a portable model.`;
      await refreshCatalog({selectModelId: model.model_id, selectCheckpointId: checkpointId});
    } catch (error) { reportError(error); }
    finally { promotionBusy = false; updateSavedControls(); publishDraftActivity(); }
  }

  async function cancelJob() {
    if (!activeJob) return;
    try { await api(`/api/draft/jobs/${activeJob.jobId}/cancel`, {method: "POST", body: ""}); }
    catch (error) { reportError(error); }
  }

  function renderHistory(rows) {
    const body = $("draftHistoryBody");
    body.replaceChildren();
    for (const item of rows) {
      const row = element("tr");
      for (const value of [
        item.generation, item.champion_fitness.toFixed(2), item.mean_fitness.toFixed(2),
        `${(item.championship_rate * 100).toFixed(1)}%`, `${(item.playoff_rate * 100).toFixed(1)}%`, item.arena_count,
      ]) cell(row, value);
      body.append(row);
    }
  }

  function renderShowcaseTeam() {
    const showcase = lastTrainingResult?.showcase;
    if (!showcase) return;
    const numberValue = Number($("draftShowcaseTeam").value);
    const draftTeam = showcase.draft.teams.find(team => team.drafter_number === numberValue);
    const seasonTeam = showcase.season.teams.find(team => team.team_id === `drafter-${numberValue}`);
    if (!draftTeam || !seasonTeam) return;

    const rosterBody = $("draftRosterBody");
    rosterBody.replaceChildren();
    for (const player of draftTeam.roster) {
      const row = element("tr"); cell(row, player.player_name); rosterBody.append(row);
    }
    const weekBody = $("draftWeekBody");
    weekBody.replaceChildren();
    for (const week of seasonTeam.weekly_results) {
      const row = element("tr");
      for (const value of [week.week, week.stage, week.opponent_team_name || "Bye", week.outcome, `${week.score.toFixed(1)}${week.opponent_score == null ? "" : `–${week.opponent_score.toFixed(1)}`}`]) cell(row, value);
      weekBody.append(row);
    }
    const standingsBody = $("draftStandingsBody");
    standingsBody.replaceChildren();
    for (const standing of showcase.season.standings) {
      const row = element("tr", standing.team_id === seasonTeam.team_id ? "draft-highlight-row" : "");
      for (const value of [standing.finish_rank, standing.team_name, `${standing.wins}-${standing.losses}${standing.ties ? `-${standing.ties}` : ""}`, standing.points_for.toFixed(1), standing.made_playoffs ? "Yes" : "No"]) cell(row, value);
      standingsBody.append(row);
    }
    const bracketBody = $("draftBracketBody");
    bracketBody.replaceChildren();
    for (const game of showcase.season.bracket_games) {
      const row = element("tr");
      const matchup = game.lower_team_name ? `${game.higher_team_name} vs ${game.lower_team_name}` : `${game.higher_team_name} · bye`;
      const score = game.lower_score == null ? "Bye" : `${game.higher_score.toFixed(1)}–${game.lower_score.toFixed(1)}`;
      for (const value of [`R${game.round_number} · W${game.week}`, matchup, score, game.winner_team_id]) cell(row, value);
      bracketBody.append(row);
    }
  }

  async function renderTrainingResult(result) {
    lastTrainingResult = result;
    renderHistory(result.history);
    const select = $("draftShowcaseTeam");
    select.replaceChildren();
    for (const team of result.showcase.draft.teams) select.add(new Option(`${team.name} · ${team.strategy}`, String(team.drafter_number)));
    select.value = String(result.showcase.selected_drafter_number);
    $("draftShowcaseTitle").textContent = `${result.showcase.season.season} · champion ${result.showcase.season.champion_team_name}`;
    renderShowcaseTeam();
    $("draftLastBatch").classList.remove("hidden");
    $("draftDownloadModel").classList.remove("hidden");
    $("draftDownloadModel").dataset.modelId = result.model.model_id;
    $("draftProgressText").textContent = `Training complete · ${result.model.league_name} · generation ${result.model.generation}`;
    $("draftAutosaveStatus").textContent = "Autosaved final champion and last-batch history";
    await refreshCatalog({selectModelId: result.model.model_id});
  }

  function metric(label, value, className = "") {
    const card = element("article", `draft-metric ${className}`.trim());
    card.append(element("span", "", label), element("strong", "", value));
    return card;
  }

  function signed(value, digits = 2) { return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`; }
  function renderBenchmarkResult(result) {
    $("draftBenchmarkScope").textContent = result.scope_notice;
    const metrics = $("draftBenchmarkMetrics");
    metrics.replaceChildren(
      metric("Verdict", result.verdict, "verdict"),
      metric("Win / tie / loss", `${result.wins} / ${result.ties} / ${result.losses}`),
      metric("Mean points", signed(result.mean_points_delta)),
      metric("Finish improvement", signed(result.mean_finish_improvement)),
      metric("Playoff-rate delta", `${signed(result.playoff_rate_delta * 100, 1)} pp`),
      metric("Title-rate delta", `${signed(result.championship_rate_delta * 100, 1)} pp`),
      metric("Points percentile", signed(result.mean_points_percentile_delta * 100, 1)),
      metric("Season-clustered 95% interval", `${signed(result.percentile_delta_interval_95[0] * 100, 1)} to ${signed(result.percentile_delta_interval_95[1] * 100, 1)}`)
    );
    $("draftProgressText").textContent = `100-scenario regression check complete · ${result.verdict}`;
  }

  async function startBenchmark() {
    clearDraftError();
    try {
      if (!$("draftBenchmarkModel").value) throw new Error("Choose a trained model to benchmark.");
      await launchJob("/api/draft/benchmarks", {
        model_id: $("draftBenchmarkModel").value,
        trials: 100,
        seed: integer("draftSeed"),
        candidate_window: integer("draftCandidateWindow"),
        evaluation_years: selectedYears()
      }, "benchmark");
    } catch (error) { reportError(error); }
  }

  async function downloadModel() {
    try {
      const modelId = $("draftDownloadModel").dataset.modelId;
      const record = await api(`/api/draft/models/${modelId}/export`);
      const link = element("a");
      link.href = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], {type: "application/json"}));
      link.download = `${modelId}.draftbrain.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (error) { reportError(error); }
  }

  function filterAssistantPlayers() {
    const query = $("assistantPlayerSearch").value.trim().toLowerCase();
    const select = $("assistantPlayer");
    const previous = select.value;
    select.replaceChildren();
    for (const player of assistantPlayers) {
      if (player.drafted || (query && !`${player.name} ${player.position}`.toLowerCase().includes(query))) continue;
      select.add(new Option(`${player.name} · ${player.position}`, player.player_id));
    }
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
    $("assistantRecordPick").disabled = externalWorkBusy || assistantBusy || assistantStatus?.complete || !select.value;
  }

  function applyAssistantDraftBinding(status, {resetUnbound = false} = {}) {
    const binding = status.draft_binding;
    const league = $("assistantEspnLeague");
    const season = $("assistantEspnSeason");
    if (binding) {
      league.value = binding.league_id;
      season.value = String(binding.season);
    } else if (resetUnbound) {
      league.value = "";
      season.value = String(
        catalog.boards.find(row => row.board_id === status.board_id)?.season || new Date().getFullYear()
      );
    }
    league.readOnly = Boolean(binding);
    season.readOnly = Boolean(binding);
    const explanation = binding
      ? "This saved room is permanently bound to this ESPN public draft."
      : "";
    league.title = explanation;
    season.title = explanation;
  }

  function renderAssistant(status) {
    assistantStatus = status;
    applyAssistantDraftBinding(status);
    $("assistantRoom").classList.remove("hidden");
    $("assistantTurn").textContent = status.complete
      ? "Draft complete"
      : `Pick ${status.overall_pick} · Round ${status.round} · ${status.next_drafter_name}`;
    const live = status.live_sync || {};
    const syncDetail = live.status === "synced"
      ? `${live.appended_pick_count} new pick${live.appended_pick_count === 1 ? "" : "s"}; ${live.observed_pick_count} observed on ESPN.`
      : live.message;
    $("assistantLiveNotice").textContent = status.your_turn
      ? `Your turn — recommendations are ranked for your roster and strategy. ${syncDetail || ""}`
      : (syncDetail || "Enter picks manually or connect a public ESPN draft.");
    $("assistantPickDrafter").value = status.next_drafter_number == null ? "" : String(status.next_drafter_number);
    $("assistantPickDrafter").disabled = status.complete;
    $("assistantUndo").disabled = externalWorkBusy || assistantBusy || !status.picks.length;
    const pickBody = $("assistantPickBody");
    pickBody.replaceChildren();
    for (const pick of status.picks) {
      const row = element("tr");
      for (const value of [pick.overall_pick, `#${pick.drafter_number}`, pick.player_name, pick.position]) cell(row, value);
      pickBody.append(row);
    }
    if (!status.picks.length) emptyRow(pickBody, 4, "No picks recorded yet.");
    const recommendations = $("assistantRecommendations");
    recommendations.replaceChildren();
    if (!status.recommendations.length) {
      recommendations.append(element("p", "", status.complete ? "The draft is complete." : "Recommendations appear when your Drafter # is on the clock."));
    } else {
      for (const row of status.recommendations) {
        const button = element("button", "assistant-recommendation");
        button.type = "button";
        button.dataset.playerId = row.player_id;
        const copy = element("span");
        copy.append(element("strong", "", `${row.player_name} · ${row.position}`), element("small", "", row.reason));
        button.append(element("span", "rank", row.rank), copy, element("span", "utility", row.utility.toFixed(2)));
        recommendations.append(button);
      }
    }
    filterAssistantPlayers();
    if (status.complete) {
      $("assistantEspnAuto").checked = false;
      stopAssistantAutoSync();
    }
  }

  function stopAssistantAutoSync() {
    if (assistantSyncTimer !== null) clearInterval(assistantSyncTimer);
    assistantSyncTimer = null;
  }

  async function syncEspnDraft({quiet = false} = {}) {
    if (externalWorkBusy || assistantSyncBusy || !assistantStatus || assistantStatus.complete || document.hidden || !draftSurfaceActive) return;
    const leagueId = $("assistantEspnLeague").value.trim();
    if (!/^[0-9]+$/.test(leagueId)) {
      if (!quiet) reportError(new Error("Enter the numeric ESPN league ID."));
      return;
    }
    assistantSyncBusy = true;
    publishDraftActivity();
    $("assistantEspnSync").disabled = true;
    try {
      const status = await api(`/api/draft/assistants/${assistantStatus.session_id}/espn-sync`, {
        method: "POST",
        body: JSON.stringify({league_id: leagueId, season: integer("assistantEspnSeason")})
      });
      assistantStatus = status;
      await reloadAssistantPlayers();
      renderAssistant(status);
    } catch (error) {
      $("assistantEspnAuto").checked = false;
      stopAssistantAutoSync();
      if (!quiet) reportError(error);
      else $("assistantLiveNotice").textContent = `Automatic ESPN refresh stopped: ${error.message}`;
    } finally {
      assistantSyncBusy = false;
      $("assistantEspnSync").disabled = externalWorkBusy || Boolean(assistantStatus?.complete);
      publishDraftActivity();
    }
  }

  function updateAssistantAutoSync() {
    stopAssistantAutoSync();
    if (!$("assistantEspnAuto").checked || !draftSurfaceActive) return;
    void syncEspnDraft();
    assistantSyncTimer = setInterval(() => void syncEspnDraft({quiet: true}), 15000);
  }

  async function reloadAssistantPlayers() {
    const value = await api(`/api/draft/assistants/${assistantStatus.session_id}/players`);
    assistantPlayers = value.players;
    filterAssistantPlayers();
  }

  async function assistantAction(work) {
    if (externalWorkBusy || assistantBusy) return; assistantBusy = true;
    publishDraftActivity();
    for (const id of ["assistantCreate", "assistantOpen", "assistantRecordPick", "assistantUndo"]) $(id).disabled = true;
    try { await work(); } catch (error) { reportError(error); }
    finally { assistantBusy = false; $("assistantCreate").disabled = externalWorkBusy; updateSavedControls(); filterAssistantPlayers(); $("assistantUndo").disabled = externalWorkBusy || !assistantStatus?.picks.length; publishDraftActivity(); }
  }

  async function createAssistant(event) {
    event.preventDefault(); clearDraftError();
    await assistantAction(async () => {
      if (!$("assistantModel").value || !$("assistantBoard").value) throw new Error("Choose both a model and a current player board.");
      const status = await api("/api/draft/assistants", {method: "POST", body: JSON.stringify({
        model_id: $("assistantModel").value,
        board_id: $("assistantBoard").value,
        user_drafter_number: integer("assistantUserSlot"),
        strategy: $("assistantStrategy").value
      })});
      assistantStatus = status;
      stopAssistantAutoSync();
      $("assistantEspnAuto").checked = false;
      applyAssistantDraftBinding(status, {resetUnbound: true});
      await reloadAssistantPlayers();
      await refreshCatalog({selectModelId: status.model_id, selectBoardId: status.board_id, selectSessionId: status.session_id});
      renderAssistant(status);
    });
  }

  async function openAssistant() {
    clearDraftError();
    await assistantAction(async () => {
      const sessionId = $("assistantSession").value;
      const saved = catalog.assistant_sessions.find(row => row.session_id === sessionId);
      if (!saved) throw new Error("Choose a saved draft room first.");
      const status = await api(`/api/draft/assistants/${sessionId}`);
      $("assistantModel").value = saved.model_id;
      $("assistantBoard").value = saved.board_id;
      $("assistantUserSlot").value = String(saved.user_drafter_number);
      $("assistantStrategy").value = saved.strategy;
      assistantStatus = status;
      stopAssistantAutoSync();
      $("assistantEspnAuto").checked = false;
      applyAssistantDraftBinding(status, {resetUnbound: true});
      await reloadAssistantPlayers();
      renderAssistant(status);
    });
  }

  async function recordPick() {
    clearDraftError();
    await assistantAction(async () => {
      if (!assistantStatus || !$("assistantPlayer").value) throw new Error("Choose an available player first.");
      const status = await api(`/api/draft/assistants/${assistantStatus.session_id}/picks`, {method: "POST", body: JSON.stringify({
        player_id: $("assistantPlayer").value,
        drafter_number: integer("assistantPickDrafter")
      })});
      assistantStatus = status;
      await reloadAssistantPlayers();
      renderAssistant(status);
    });
  }

  async function undoPick() {
    if (!assistantStatus) return;
    await assistantAction(async () => {
      const status = await api(`/api/draft/assistants/${assistantStatus.session_id}/undo`, {method: "POST", body: ""});
      assistantStatus = status;
      await reloadAssistantPlayers();
      renderAssistant(status);
    });
  }

  function bind() {
    $("draftPreset").addEventListener("change", applyPreset);
    $("draftCorpus").addEventListener("change", syncTrainingYears);
    $("draftCheckpoint").addEventListener("change", selectCheckpoint);
    $("assistantSession").addEventListener("change", updateSavedControls);
    $("draftTeamCount").addEventListener("input", updateStrategyTotal);
    for (const input of document.querySelectorAll("[data-strategy]")) input.addEventListener("input", updateStrategyTotal);
    $("draftCorpusFile").addEventListener("change", event => void importJson(event.target, "/api/draft/corpora/import", "draftCorpusStatus", "corpus"));
    $("draftModelFile").addEventListener("change", event => void importJson(event.target, "/api/draft/models/import", "draftModelStatus", "model"));
    $("draftBoardFile").addEventListener("change", event => void importJson(event.target, "/api/draft/boards/import", "draftBoardStatus", "board"));
    $("draftEstimateButton").addEventListener("click", estimateTraining);
    $("draftTrainingForm").addEventListener("submit", startTraining);
    $("draftResumeButton").addEventListener("click", resumeTraining);
    $("draftPromoteButton").addEventListener("click", promoteCheckpoint);
    $("draftCancelButton").addEventListener("click", cancelJob);
    $("draftDownloadModel").addEventListener("click", downloadModel);
    $("draftShowcaseTeam").addEventListener("change", renderShowcaseTeam);
    $("draftBenchmarkButton").addEventListener("click", startBenchmark);
    $("assistantSetupForm").addEventListener("submit", createAssistant);
    $("assistantOpen").addEventListener("click", openAssistant);
    $("assistantPlayerSearch").addEventListener("input", filterAssistantPlayers);
    $("assistantPlayer").addEventListener("change", filterAssistantPlayers);
    $("assistantRecordPick").addEventListener("click", recordPick);
    $("assistantUndo").addEventListener("click", undoPick);
    $("assistantEspnSync").addEventListener("click", () => void syncEspnDraft());
    $("assistantEspnAuto").addEventListener("change", updateAssistantAutoSync);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && $("assistantEspnAuto").checked) void syncEspnDraft({quiet: true});
    });
    $("assistantRecommendations").addEventListener("click", event => {
      const button = event.target.closest("[data-player-id]");
      if (button) { $("assistantPlayerSearch").value = ""; filterAssistantPlayers(); $("assistantPlayer").value = button.dataset.playerId; filterAssistantPlayers(); }
    });
  }

  async function init(activitySnapshot = null) {
    bind();
    const activity = activitySnapshot || await api("/api/activity");
    pendingRecoveredJob = activity.draft || null;
    applyExternalWorkState(Boolean(
      ["queued", "running"].includes(activity.search?.status) ||
        ["queued", "running"].includes(activity.weekly_collection?.status) ||
        window.TradeAppBusy
    ));
    $("assistantEspnSeason").value = String(new Date().getFullYear());
    updateStrategyTotal();
    emptyRow($("draftHistoryBody"), 6, "No completed training batch yet.");
    for (const [id, columns, message] of [
      ["draftRosterBody", 1, "Choose a completed showcase."],
      ["draftWeekBody", 5, "Choose a completed showcase."],
      ["draftStandingsBody", 5, "Choose a completed showcase."],
      ["draftBracketBody", 4, "Choose a completed showcase."],
    ]) emptyRow($(id), columns, message);
    try {
      await refreshCatalog();
    } finally {
      restoreDraftJob(pendingRecoveredJob);
    }
  }

  let initialization = null;
  function ensureInitialized(activitySnapshot = null) {
    if (!initialization) initialization = init(activitySnapshot).catch(reportError);
    return initialization;
  }

  window.addEventListener("appsurfacechange", event => {
    draftSurfaceActive = event.detail?.surface === "draft";
    if (!draftSurfaceActive) {
      stopAssistantAutoSync();
      return;
    }
    void ensureInitialized().then(() => {
      if ($("assistantEspnAuto").checked) updateAssistantAutoSync();
    });
  });
  window.addEventListener("serveractivitychange", event => {
    const activity = event.detail || {};
    if (activity.draft) pendingRecoveredJob = activity.draft;
    if (initialization) {
      applyExternalWorkState(Boolean(
        ["queued", "running"].includes(activity.search?.status) ||
          ["queued", "running"].includes(activity.weekly_collection?.status) ||
          window.TradeAppBusy
      ));
    }
    if (activity.draft) {
      void ensureInitialized(activity).then(() => restoreDraftJob(activity.draft));
    }
  });
  window.addEventListener("tradeactivitychange", event => {
    applyExternalWorkState(event.detail?.busy);
  });
  if (draftSurfaceActive) {
    void ensureInitialized();
  }

  return {ensureInitialized};
})();
