"use strict";

window.DashboardUi = (() => {
  const $ = id => document.getElementById(id);
  const percentFormatter = new Intl.NumberFormat(undefined, {style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1});
  let requestRevision = 0;
  let activeController = null;
  let dashboard = null;
  let selectedTeamId = null;

  function percent(value) { return Number.isFinite(value) ? percentFormatter.format(value) : "—"; }
  function probability(value) {
    if (!Number.isFinite(value)) return "—";
    return value > 0 && value < .001 ? "<0.1%" : percent(value);
  }
  function number(value, digits = 1) {
    return Number.isFinite(value) ? value.toFixed(digits) : "—";
  }
  function rank(value) { return Number.isInteger(value) ? String(value) : number(value); }
  function record(value, projected = false) {
    if (!value) return "—";
    const format = item => projected ? number(item, 1) : String(item);
    return `${format(value.wins)}-${format(value.losses)}${value.ties ? `-${format(value.ties)}` : ""}`;
  }
  function textElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    return node;
  }
  function clamp(value, low = 0, high = 1) { return Math.max(low, Math.min(high, value)); }
  function bindHorizontalScroll(container) {
    container.addEventListener("keydown", event => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (container.scrollWidth <= container.clientWidth) return;
      const direction = event.key === "ArrowRight" ? 1 : -1;
      container.scrollLeft += direction * Math.max(48, container.clientWidth * .6);
      event.preventDefault();
    });
  }
  function teamsByProjectedFinish() {
    return [...dashboard.teams].sort((left, right) => left.projected_rank - right.projected_rank || left.team_name.localeCompare(right.team_name));
  }
  function selectedTeam() {
    return dashboard?.teams.find(team => team.team_id === selectedTeamId) || dashboard?.teams[0] || null;
  }

  function showState(kind, message = "") {
    $("dashboardEmpty").classList.toggle("hidden", kind !== "empty");
    $("dashboardLoading").classList.toggle("hidden", kind !== "loading");
    $("dashboardContent").classList.toggle("hidden", kind !== "content");
    if (kind === "empty" && message) $("dashboardEmpty").textContent = message;
  }

  function reset(message = "Select a ready week to calculate the league outlook.") {
    requestRevision += 1;
    if (activeController) activeController.abort();
    activeController = null;
    dashboard = null;
    selectedTeamId = null;
    showState("empty", message);
  }

  async function setBundle(bundle, {request, onError} = {}) {
    if (!bundle) {
      reset();
      return;
    }
    if (typeof request !== "function") throw new Error("Dashboard request function is unavailable.");
    const revision = ++requestRevision;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    dashboard = null;
    showState("loading");
    try {
      const value = await request(`/api/bundles/${encodeURIComponent(bundle.bundle_id)}/dashboard`, {signal: controller.signal});
      if (revision !== requestRevision || controller.signal.aborted) return;
      dashboard = value;
      selectedTeamId = value.teams[0]?.team_id || null;
      render();
      showState("content");
    } catch (error) {
      if (controller.signal.aborted || revision !== requestRevision) return;
      showState("empty", "The league dashboard could not be calculated for this week.");
      if (typeof onError === "function") onError(error);
    } finally {
      if (revision === requestRevision) activeController = null;
    }
  }

  function populateTeamPicker() {
    const select = $("dashboardTeamSelect");
    select.replaceChildren();
    for (const team of teamsByProjectedFinish()) select.add(new Option(team.team_name, team.team_id));
    select.value = selectedTeamId;
  }

  function renderKpis(team) {
    const fp = team.fantasypros_comparison?.source;
    const drift = team.fantasypros_comparison?.local_minus_source;
    const values = [
      ["Power ranking", `#${team.power_rank} · ${number(team.power_score)}`, "Local roster power"],
      ["Projected finish", `#${rank(team.projected_rank)}`, `${movementText(team.standings_change)} · FantasyPros mean #${rank(fp?.projected_rank)}`],
      ["Make playoffs", percent(team.playoff_probability), `${dashboard.playoff_team_count} playoff spots · FantasyPros ${percent(fp?.playoff_probability)} · local drift ${signedPercent(drift?.playoff_probability)}`],
      ["Win championship", percent(team.championship_probability), `Local proxy · FantasyPros ${percent(fp?.championship_probability)} · local drift ${signedPercent(drift?.championship_probability)}`]
    ];
    const container = $("dashboardKpis");
    container.replaceChildren();
    for (const [label, value, detail] of values) {
      const card = textElement("div", "dashboard-kpi", "");
      card.append(
        textElement("span", "dashboard-kpi-label", label),
        textElement("strong", "dashboard-kpi-value", value),
        textElement("span", "dashboard-kpi-detail", detail)
      );
      container.append(card);
    }
  }

  function signedPercent(value) {
    if (!Number.isFinite(value)) return "—";
    return `${value >= 0 ? "+" : ""}${percent(value)}`;
  }

  function movementText(value) {
    if (!Number.isFinite(value) || Math.abs(value) < .05) return "Holding steady";
    return value > 0 ? `Up ${number(value)} places` : `Down ${number(Math.abs(value))} places`;
  }

  function movementNode(value) {
    const flat = !Number.isFinite(value) || Math.abs(value) < .05;
    const className = flat ? "flat" : value > 0 ? "up" : "down";
    const symbol = flat ? "•" : value > 0 ? "↑" : "↓";
    return textElement("span", `dashboard-movement ${className}`, `${symbol} ${flat ? "steady" : number(Math.abs(value))}`);
  }

  function oddsCell(value, modeled = false) {
    const wrapper = textElement("div", `dashboard-odds${modeled ? " modeled" : ""}`, "");
    const meter = textElement("span", "mini-meter", "");
    const fill = document.createElement("span");
    fill.style.width = `${clamp(value) * 100}%`;
    meter.append(fill);
    wrapper.append(textElement("strong", "", percent(value)), meter);
    return wrapper;
  }

  function renderStandings() {
    const body = $("dashboardStandingsBody");
    body.replaceChildren();
    for (const team of teamsByProjectedFinish()) {
      const row = document.createElement("tr");
      row.setAttribute("aria-selected", String(team.team_id === selectedTeamId));
      const projected = document.createElement("td");
      projected.append(textElement("span", "dashboard-rank", `#${rank(team.projected_rank)}`), textElement("span", "dashboard-muted", `Mean finish ${number(team.mean_projected_rank)} · current #${team.current_rank} · FantasyPros #${rank(team.fantasypros_comparison?.source?.projected_rank)}`));
      const name = document.createElement("td");
      const choose = textElement("button", "dashboard-team-button", team.team_name);
      choose.type = "button";
      choose.dataset.teamId = team.team_id;
      choose.addEventListener("click", () => {
        selectTeam(team.team_id);
        const replacement = [...body.querySelectorAll(".dashboard-team-button")]
          .find(button => button.dataset.teamId === team.team_id);
        replacement?.focus();
      });
      name.append(choose);
      const power = document.createElement("td");
      power.append(textElement("strong", "", number(team.power_score)), textElement("span", "dashboard-muted", `Power rank #${team.power_rank}`));
      const current = textElement("td", "", record(team.current_record));
      current.append(textElement("span", "dashboard-muted", `${number(team.current_points_for_per_game)} PF/G · ${number(team.current_points_against_per_game)} PA/G`));
      const projectedRecord = textElement("td", "", record(team.projected_record, true));
      projectedRecord.append(textElement("span", "dashboard-muted", `${number(team.expected_final_points_for)} projected PF`));
      const movement = document.createElement("td");
      movement.append(movementNode(team.standings_change));
      const playoffs = document.createElement("td");
      playoffs.append(oddsCell(team.playoff_probability));
      playoffs.title = `FantasyPros comparison: ${percent(team.fantasypros_comparison?.source?.playoff_probability)}; local-minus-source ${signedPercent(team.fantasypros_comparison?.local_minus_source?.playoff_probability)}.`;
      const title = document.createElement("td");
      title.append(oddsCell(team.championship_probability, true));
      title.title = `FantasyPros comparison: ${percent(team.fantasypros_comparison?.source?.championship_probability)}; local-minus-source ${signedPercent(team.fantasypros_comparison?.local_minus_source?.championship_probability)}.`;
      row.append(projected, name, power, current, projectedRecord, movement, playoffs, title);
      body.append(row);
    }
  }

  function renderBarList(containerId, rows, options) {
    const container = $(containerId);
    container.replaceChildren();
    const list = textElement("div", "bar-list", "");
    for (const row of rows) {
      const item = textElement("div", `bar-row${row.team_id === selectedTeamId ? " selected" : ""}`, "");
      const track = textElement("span", "bar-track", "");
      const fill = document.createElement("span");
      fill.style.width = `${clamp(options.width(row)) * 100}%`;
      track.append(fill);
      item.append(textElement("span", "bar-label", row.team_name), track, textElement("span", "bar-value", options.value(row)));
      list.append(item);
    }
    container.append(list);
  }

  function renderTitleRace() {
    const rows = [...dashboard.teams].sort((left, right) => right.championship_probability - left.championship_probability || left.team_name.localeCompare(right.team_name));
    renderBarList("titleRaceChart", rows, {
      width: row => row.championship_probability,
      value: row => percent(row.championship_probability)
    });
  }

  function renderContenderMap() {
    const powers = dashboard.teams.map(team => team.power_score).sort((a, b) => a - b);
    const median = powers.length % 2 ? powers[(powers.length - 1) / 2] : (powers[powers.length / 2 - 1] + powers[powers.length / 2]) / 2;
    const labelIds = new Set([...dashboard.teams].sort((a, b) => b.playoff_probability - a.playoff_probability).slice(0, 3).map(team => team.team_id));
    DashboardCharts.scatter($("contenderChart"), {
      title: "Contender map",
      description: "Each team is plotted by local roster power and simulated playoff probability. Bubble size represents modeled championship probability.",
      xLabel: "Roster power",
      yLabel: "Make playoffs",
      yDomain: [0, 100],
      yFormat: value => `${value.toFixed(0)}%`,
      guides: [{axis: "x", value: median}, {axis: "y", value: 50}],
      points: dashboard.teams.map(team => ({
        x: team.power_score,
        y: team.playoff_probability * 100,
        radius: 5 + Math.sqrt(team.championship_probability) * 18,
        label: team.team_name,
        selected: team.team_id === selectedTeamId,
        showLabel: labelIds.has(team.team_id),
        detail: `${team.team_name}: ${number(team.power_score)} power, ${percent(team.playoff_probability)} playoff chance, ${percent(team.championship_probability)} modeled title chance.`
      }))
    });
  }

  function renderStandingsMovement() {
    const container = $("standingsMovementChart");
    container.replaceChildren();
    const list = textElement("div", "movement-list", "");
    const count = dashboard.teams.length;
    for (const team of teamsByProjectedFinish()) {
      const row = textElement("div", "movement-row", "");
      const track = textElement("div", "movement-track", "");
      const current = 100 * (team.current_rank - 1) / Math.max(1, count - 1);
      const projected = 100 * (team.projected_rank - 1) / Math.max(1, count - 1);
      const span = textElement("span", "movement-span", "");
      span.style.left = `${Math.min(current, projected)}%`;
      span.style.width = `${Math.abs(current - projected)}%`;
      const currentDot = textElement("span", "movement-dot current", "");
      currentDot.style.left = `${current}%`;
      currentDot.title = `Current rank ${team.current_rank}`;
      const projectedDot = textElement("span", "movement-dot projected", "");
      projectedDot.style.left = `${projected}%`;
      projectedDot.title = `Projected rank ${rank(team.projected_rank)}`;
      track.append(span, currentDot, projectedDot);
      row.append(textElement("span", "bar-label", team.team_name), track, movementNode(team.standings_change));
      list.append(row);
    }
    container.append(list);
  }

  function renderSchedule() {
    const rows = [...dashboard.teams].sort((left, right) => left.schedule_difficulty_rank - right.schedule_difficulty_rank || left.team_name.localeCompare(right.team_name));
    const values = rows.map(row => row.average_opponent_points).filter(Number.isFinite);
    const low = values.length ? Math.min(...values) : 0;
    const high = values.length ? Math.max(...values) : 1;
    renderBarList("scheduleChart", rows, {
      width: row => (row.average_opponent_points - low) / (high - low || 1),
      value: row => `${number(row.average_opponent_points)} pts · ${percent(row.expected_remaining_win_rate)} wins`
    });
  }

  function renderWeeklyScoring(team) {
    const byWeek = new Map();
    for (const row of dashboard.teams) {
      for (const week of row.weekly_outlook) {
        if (!byWeek.has(week.week)) byWeek.set(week.week, []);
        byWeek.get(week.week).push(week.projected_points);
      }
    }
    const league = [...byWeek].sort((a, b) => a[0] - b[0]).map(([week, values]) => ({x: week, y: values.reduce((sum, value) => sum + value, 0) / values.length}));
    const selected = team.weekly_outlook.map(row => ({
      x: row.week,
      y: row.projected_points,
      lower: Math.max(0, row.projected_points - row.uncertainty),
      upper: row.projected_points + row.uncertainty
    }));
    $("weeklyChartLabel").textContent = `${team.team_name} vs. league average`;
    DashboardCharts.line($("weeklyScoringChart"), {
      title: "Weekly scoring runway",
      description: `${team.team_name}'s mean-optimized weekly score forecast with one standard-deviation uncertainty band compared with the league average.`,
      xLabel: "Fantasy week",
      yLabel: "Projected points",
      xFormat: value => `W${Math.round(value)}`,
      yFormat: value => value.toFixed(1),
      series: [
        {label: team.team_name, values: selected, band: true},
        {label: "League avg", values: league, className: "league"}
      ]
    });
  }

  function heatColor(value, hue) {
    if (!Number.isFinite(value) || value <= 0) return "var(--surface)";
    return `hsla(${hue}, 55%, 25%, ${(.16 + clamp(value) * .84).toFixed(2)})`;
  }

  function renderHeatmap(containerId, headers, rows, values, {hue = 187, valueLabel = probability, showZero = false, label = "League probability table"} = {}) {
    const container = $(containerId);
    container.replaceChildren();
    const table = textElement("table", "heatmap", "");
    table.append(textElement("caption", "dashboard-sr-only", label));
    const head = document.createElement("thead");
    const headerRow = document.createElement("tr");
    headerRow.append(textElement("th", "", "Team"));
    for (const header of headers) headerRow.append(textElement("th", "", String(header)));
    head.append(headerRow);
    const body = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      const rowHeader = textElement("th", "", row.team_name);
      rowHeader.scope = "row";
      tr.append(rowHeader);
      values(row).forEach((value, index) => {
        const visible = Number.isFinite(value) && (showZero || value > 0);
        const cell = textElement("td", "heat-cell", visible ? valueLabel(value) : "—");
        cell.dataset.empty = String(!Number.isFinite(value) || value <= 0);
        cell.style.background = heatColor(value, hue);
        cell.title = `${row.team_name}, ${headers[index]}: ${valueLabel(value)}`;
        tr.append(cell);
      });
      body.append(tr);
    }
    table.append(head, body);
    container.append(table);
  }

  function renderProbabilityMaps() {
    const teams = teamsByProjectedFinish();
    renderHeatmap("finishHeatmap", dashboard.teams.map((_, index) => `#${index + 1}`), teams, team => team.rank_distribution, {label: "Final rank probability by team"});
    renderHeatmap("seedHeatmap", Array.from({length: dashboard.playoff_team_count}, (_, index) => `Seed ${index + 1}`), teams, team => team.seed_distribution, {hue: 158, label: "Playoff seed probability by team"});
  }

  function renderPositions() {
    const teams = [...dashboard.teams].sort((left, right) => left.power_rank - right.power_rank || left.team_name.localeCompare(right.team_name));
    renderHeatmap("positionHeatmap", dashboard.positions, teams, team => {
      const byPosition = new Map(team.position_outlook.map(row => [row.position, row]));
      return dashboard.positions.map(position => (byPosition.get(position)?.league_percentile ?? 0));
    }, {hue: 41, valueLabel: value => `P${Math.round(value * 100)}`, showZero: true, label: "Position projection percentile by team"});
  }

  function renderPointsProfile() {
    const eligible = dashboard.teams.filter(team => Number.isFinite(team.current_points_for_per_game) && Number.isFinite(team.current_points_against_per_game));
    if (!eligible.length) {
      const container = $("pointsProfileChart");
      container.replaceChildren(textElement("div", "dashboard-state", "Scoring profile appears after the league has completed a week."));
      return;
    }
    DashboardCharts.scatter($("pointsProfileChart"), {
      title: "Current scoring profile",
      description: "Teams are plotted by points scored and points allowed per completed game. Higher scoring and lower points allowed are favorable.",
      xLabel: "Points for per game →",
      yLabel: "Points allowed per game",
      points: eligible.map(team => ({
        x: team.current_points_for_per_game,
        y: team.current_points_against_per_game,
        label: team.team_name,
        selected: team.team_id === selectedTeamId,
        showLabel: team.team_id === selectedTeamId || team.current_rank <= 2,
        detail: `${team.team_name}: ${number(team.current_points_for_per_game)} points for and ${number(team.current_points_against_per_game)} points allowed per game.`
      }))
    });
  }

  function renderMethodNote() {
    const model = dashboard.championship_model;
    const limitations = Array.isArray(model.limitations) ? model.limitations.join(" ") : model.limitations;
    const sampling = dashboard.scenario_sampling;
    const samplingNote = sampling?.capped ? ` ${sampling.methodology}` : "";
    const comparison = dashboard.fantasypros_comparison;
    const settlement = dashboard.host_settlement_policy;
    const comparisonNote = comparison
      ? ` FantasyPros diagnostic: current ranks matched for ${comparison.current_rank_match_count} of ${comparison.team_count} teams; source values are never calculation inputs.`
      : "";
    const settlementNote = settlement?.limitations ? ` ${settlement.limitations}` : "";
    $("dashboardMethodNote").textContent = `${model.label}: ${model.methodology} ${limitations}${samplingNote}${comparisonNote}${settlementNote}`.trim();
  }

  function selectTeam(teamId) {
    if (!dashboard?.teams.some(team => team.team_id === teamId)) return;
    selectedTeamId = teamId;
    $("dashboardTeamSelect").value = teamId;
    renderKpis(selectedTeam());
    renderStandings();
    renderTitleRace();
    renderContenderMap();
    renderSchedule();
    renderWeeklyScoring(selectedTeam());
    renderPointsProfile();
  }

  function render() {
    const team = selectedTeam();
    if (!team) {
      showState("empty", "This weekly bundle contains no teams to analyze.");
      return;
    }
    $("dashboardTitle").textContent = `${dashboard.season} · Week ${dashboard.first_remaining_week}`;
    const sampling = dashboard.scenario_sampling;
    const scenarioLabel = sampling?.capped
      ? `${dashboard.scenario_count.toLocaleString()} local scenarios (bounded from ${sampling.bundle_scenario_count.toLocaleString()})`
      : `${dashboard.scenario_count.toLocaleString()} local scenarios`;
    const powerLabel = ["exact", "holdout_validated"].includes(dashboard.power_engine_mode)
      ? "blind-holdout-validated power"
      : dashboard.power_engine_mode === "independent"
        ? "independent local power"
        : "surrogate power";
    $("dashboardSubtitle").textContent = `${dashboard.teams.length} teams · ${scenarioLabel} · ${powerLabel}`;
    populateTeamPicker();
    renderKpis(team);
    renderStandings();
    renderTitleRace();
    renderContenderMap();
    renderStandingsMovement();
    renderSchedule();
    renderWeeklyScoring(team);
    renderProbabilityMaps();
    renderPositions();
    renderPointsProfile();
    renderMethodNote();
  }

  $("dashboardTeamSelect").addEventListener("change", event => selectTeam(event.target.value));
  for (const container of document.querySelectorAll(".dashboard-table-wrap, .heatmap-wrap, .chart-svg")) {
    bindHorizontalScroll(container);
  }

  return Object.freeze({reset, setBundle});
})();
