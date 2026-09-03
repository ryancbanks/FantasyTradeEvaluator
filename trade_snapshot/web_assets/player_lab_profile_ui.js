"use strict";

window.PlayerLabProfileUi = (() => {
  const $ = id => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const numberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1, minimumFractionDigits: 1});
  const integerFormatter = new Intl.NumberFormat();
  const descriptionCache = new WeakMap();

  function node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function array(value) {
    return Array.isArray(value) ? value : [];
  }

  function finite(value) {
    return Number.isFinite(value) ? value : null;
  }

  function number(value) {
    return Number.isFinite(value) ? numberFormatter.format(value) : "—";
  }

  function humanize(value) {
    if (typeof value !== "string" || !value) return "Unavailable";
    return value.replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
  }

  function describe(player) {
    if (!player || typeof player !== "object") {
      return {
        profile: null, depth: null, performanceTrend: {status: "unknown", direction: "unknown", sample_size: 0},
        marketTrend: null, depthLabel: "Depth unavailable", performanceLabel: "Not enough history",
        performanceDetail: "Needs more completed stat lines", marketLabel: "Sleeper market trend unavailable"
      };
    }
    const cached = descriptionCache.get(player);
    if (cached) return cached;
    const profile = player?.profile && typeof player.profile === "object" ? player.profile : null;
    const depth = profile?.depth_chart && typeof profile.depth_chart === "object"
      ? profile.depth_chart
      : player?.depth_chart_position || Number.isFinite(player?.depth_chart_order)
        ? {position: player.depth_chart_position || null, order: finite(player.depth_chart_order)}
        : null;
    const rawPerformance = profile?.performance_trend || player?.performance_trend;
    const performanceTrend = rawPerformance && typeof rawPerformance === "object"
      ? {...rawPerformance, direction: ["rising", "falling", "steady"].includes(rawPerformance.direction) ? rawPerformance.direction : "unknown"}
      : {status: "unknown", direction: "unknown", sample_size: 0};
    const rawMarket = profile?.market_trend || player?.market_trend;
    const marketTrend = rawMarket && typeof rawMarket === "object"
      ? {
          ...rawMarket,
          direction: rawMarket.status === "observed" && ["rising", "falling", "steady"].includes(rawMarket.direction)
            ? rawMarket.direction
            : "unknown"
        }
      : null;
    const description = {
      profile,
      depth,
      performanceTrend,
      marketTrend,
      depthLabel: formatDepth(depth, player?.position),
      performanceLabel: formatPerformance(performanceTrend.direction),
      performanceDetail: formatPerformanceDetail(performanceTrend),
      marketLabel: formatMarket(marketTrend)
    };
    descriptionCache.set(player, description);
    return description;
  }

  function formatDepth(depth, fallbackPosition) {
    if (!depth) return "Depth unavailable";
    const position = depth.position || fallbackPosition || "Position";
    return depth.order === null || depth.order === undefined
      ? `${position} depth order unavailable`
      : `${position}${depth.order}`;
  }

  function formatPerformance(direction) {
    if (direction === "rising") return "Rising";
    if (direction === "falling") return "Falling";
    if (direction === "steady") return "Steady";
    return "Not enough history";
  }

  function formatPerformanceDetail(value) {
    if (value.direction === "unknown") return "Needs more recorded stat lines";
    const pieces = [];
    if (Number.isFinite(value.sample_size)) pieces.push(`${integerFormatter.format(value.sample_size)} recorded lines`);
    if (Number.isFinite(value.change)) pieces.push(`${value.change > 0 ? "+" : ""}${number(value.change)} points`);
    return pieces.join(" · ") || humanize(value.method || value.basis || "recent game comparison");
  }

  function formatMarket(value) {
    if (!value) return "Sleeper market trend unavailable";
    const adds = finite(value.adds);
    const drops = finite(value.drops);
    if (value.status === "observed" && adds !== null && drops !== null) {
      const net = finite(value.net_adds) ?? adds - drops;
      return `${net > 0 ? "+" : ""}${integerFormatter.format(net)} net adds`;
    }
    if (adds !== null) return `${integerFormatter.format(adds)} Sleeper adds · drops unavailable`;
    if (drops !== null) return `${integerFormatter.format(drops)} Sleeper drops · adds unavailable`;
    return "Sleeper market trend unavailable";
  }

  function render(outlook, player, detail) {
    const chart = $("playerLabChart");
    const seasonStats = $("playerLabSeasonStats");
    chart.replaceChildren();
    seasonStats.replaceChildren();
    if (!player) return;
    appendProfileFacts(detail, player);
    detail.append(renderMarketTrend(player), renderAvailability(player));
    renderChart(outlook, player, chart);
    renderSeasonStats(player, seasonStats);
  }

  function appendProfileFacts(container, player) {
    const {profile} = describe(player);
    if (!profile) {
      container.append(node("p", "player-lab-legacy-note", "This older bundle contains projection evidence only. Collect a new week to add public stats, depth, trends, and documented availability history."));
      return;
    }
    const facts = node("dl", "player-lab-facts");
    const rows = [
      ["Roster status", profile.status || (profile.active === true ? "Active" : profile.active === false ? "Inactive" : "Unavailable")],
      ["Experience", Number.isFinite(profile.years_experience) ? `${integerFormatter.format(profile.years_experience)} years` : "Unavailable"],
      ["Jersey", Number.isFinite(profile.jersey_number) ? `#${integerFormatter.format(profile.jersey_number)}` : "Unavailable"],
      ["Fantasy eligibility", array(profile.fantasy_positions).join(" · ") || array(player.eligible_slots).join(" · ") || "Unavailable"]
    ];
    for (const [label, content] of rows) facts.append(node("dt", "", label), node("dd", "", content));
    container.append(facts);
  }

  function renderMarketTrend(player) {
    const card = node("section", "player-lab-insight player-lab-market");
    card.append(node("h4", "", "Roster-market movement"));
    const {marketTrend: value} = describe(player);
    const adds = finite(value?.adds);
    const drops = finite(value?.drops);
    if (adds === null && drops === null) {
      card.append(node("p", "player-lab-muted", "Seven-day add/drop activity was not available for this player."));
    } else if (value?.status === "observed" && adds !== null && drops !== null) {
      const net = finite(value.net_adds) ?? adds - drops;
      card.append(node("strong", `player-lab-market-net ${net >= 0 ? "gain" : "loss"}`, `${net > 0 ? "+" : ""}${integerFormatter.format(net)} net adds`));
      card.append(node("p", "", `${integerFormatter.format(adds)} adds · ${integerFormatter.format(drops)} drops over the captured trend window.`));
    } else {
      card.append(
        node("strong", "player-lab-market-net", "Net movement unavailable"),
        node(
          "p",
          "",
          `${adds === null ? "Adds not published" : `${integerFormatter.format(adds)} adds`} · ${drops === null ? "Drops not published" : `${integerFormatter.format(drops)} drops`}. Direction stays unknown until both counts are observed.`
        )
      );
    }
    card.append(node("p", "player-lab-attribution", "Trending data via Sleeper (Powered by Sleeper). Its separate top-100 add and drop feeds can leave one count unpublished; market activity is context, not a performance forecast."));
    return card;
  }

  function renderAvailability(player) {
    const card = node("section", "player-lab-insight player-lab-availability");
    card.append(node("h4", "", "Documented injury-report history"));
    const {profile} = describe(player);
    const history = profile?.historical_availability || player?.historical_availability;
    if (!history || history.status !== "observed") {
      card.append(
        node("strong", "player-lab-risk is-unknown", "Not classified · insufficient qualifying history"),
        node("p", "", "Retained availability records may still appear below, but they do not contain enough qualifying injury designations or recorded stat-line exposure for a documented-burden tier. Missing history is never treated as a clean injury history.")
      );
      appendCurrentDesignation(card, history?.current_designation, profile);
      appendAffectedAreas(card, history);
      appendAvailabilityContexts(card, history);
      appendAvailabilityEvidence(card, history);
      appendAvailabilityCaveat(card, history);
      return card;
    }
    const burdenTier = history.burden_tier || history.risk_tier;
    const burdenIndex = finite(history.burden_index) ?? finite(history.risk_score);
    const tier = burdenTier === "elevated"
      ? "Elevated documented burden"
      : burdenTier === "moderate"
        ? "Moderate documented burden"
        : "Lower documented burden";
    const risk = node("strong", `player-lab-risk is-${burdenTier || "unknown"}`, tier);
    if (burdenIndex !== null) risk.append(` · weighted report index ${number(burdenIndex)}`);
    const outCount = array(history.out_weeks).length;
    const doubtfulCount = array(history.doubtful_weeks).length;
    const questionableCount = array(history.questionable_weeks).length;
    const seasons = array(history.seasons_observed);
    const recordedExposure = array(history.recorded_stat_line_exposure).length;
    card.append(
      risk,
      node("p", "", `${integerFormatter.format(finite(history.distinct_report_weeks) ?? 0)} documented report weeks across ${integerFormatter.format(seasons.length)} available season${seasons.length === 1 ? "" : "s"} · ${outCount} out · ${doubtfulCount} doubtful · ${questionableCount} questionable · ${integerFormatter.format(recordedExposure)} recorded stat lines used only as coverage evidence.`)
    );
    appendCurrentDesignation(card, history.current_designation, profile);
    appendAffectedAreas(card, history);
    appendAvailabilityContexts(card, history);
    appendAvailabilityEvidence(card, history);
    appendAvailabilityCaveat(card, history);
    return card;
  }

  function appendCurrentDesignation(container, designation, fallbackProfile) {
    const status = designation?.status || fallbackProfile?.injury_status;
    const bodyPart = designation?.body_part || fallbackProfile?.injury_body_part;
    const practice = designation?.practice_participation || fallbackProfile?.practice_participation;
    const row = node("p", "player-lab-current-designation");
    row.append(node("strong", "", "Current captured designation: "));
    row.append(status ? [status, bodyPart, practice].filter(Boolean).map(humanize).join(" · ") : "none published in retained metadata");
    container.append(row);
  }

  function appendAffectedAreas(container, history) {
    const affected = array(history.affected_body_areas);
    if (!affected.length) return;
    const list = affected.slice(0, 5).map(value => {
      const label = value.label || value.body_area || value.area || "Unspecified";
      const count = finite(value.documented_weeks);
      return count === null ? label : `${label} (${integerFormatter.format(count)} weeks)`;
    });
    const row = node("p", "player-lab-affected-areas");
    row.append(node("strong", "", "Documented areas: "), list.join(" · "));
    container.append(row);
  }

  function appendAvailabilityContexts(container, history) {
    const contexts = array(history?.availability_contexts);
    if (!contexts.length) return;
    const labels = contexts.slice(0, 5).map(value => {
      const count = finite(value.documented_weeks);
      const suffix = count === null
        ? ""
        : ` (${integerFormatter.format(count)} documented week${count === 1 ? "" : "s"})`;
      return `${humanize(value.context)}${suffix}`;
    });
    const row = node("p", "player-lab-availability-contexts");
    row.append(node("strong", "", "Documented non-injury contexts: "), labels.join(" · "));
    container.append(row);
  }

  function appendAvailabilityEvidence(container, history) {
    const evidence = array(history.weekly_evidence).slice().sort((left, right) =>
      (finite(right.season) ?? 0) - (finite(left.season) ?? 0)
      || (finite(right.week) ?? 0) - (finite(left.week) ?? 0)
    );
    if (!evidence.length) return;
    const disclosure = node("details", "player-lab-availability-evidence");
    disclosure.append(node("summary", "", `Latest documented availability records (${integerFormatter.format(evidence.length)})`));
    const list = node("ul");
    for (const value of evidence.slice(0, 8)) {
      const reportLabels = [value.report_primary_injury, value.report_secondary_injury].filter(Boolean);
      const practiceLabels = [value.practice_primary_injury, value.practice_secondary_injury].filter(Boolean);
      const report = value.report_status || reportLabels.length
        ? `Game: ${humanize(value.report_status || "status unavailable")}${reportLabels.length ? ` · ${reportLabels.map(humanize).join(" / ")}` : ""}`
        : null;
      const practice = value.practice_status || practiceLabels.length
        ? `Practice: ${humanize(value.practice_status || "status unavailable")}${practiceLabels.length ? ` · ${practiceLabels.map(humanize).join(" / ")}` : ""}`
        : null;
      const detail = [report, practice].filter(Boolean).join("; ") || "Availability evidence captured";
      list.append(node("li", "", `${value.season || "Season"} week ${value.week || "—"}: ${detail}`));
    }
    disclosure.append(list);
    container.append(disclosure);
  }

  function appendAvailabilityCaveat(container, history) {
    const unavailable = array(history?.seasons_unavailable);
    const coverage = unavailable.length ? ` Missing source seasons: ${unavailable.join(", ")}.` : "";
    const method = typeof history?.method === "string" ? ` ${history.method}` : "";
    container.append(node(
      "p",
      "player-lab-risk-caveat",
      `This summarizes historical, source-documented availability records; it is not a medical prediction, a probability of future injury, or an inference from missing stat lines.${coverage}${method}`
    ));
  }

  function renderSeasonStats(player, container) {
    const heading = node("div", "player-lab-grid-heading");
    heading.append(node("h4", "", "Season performance"), node("p", "", "Public weekly stats; Standard and PPR scoring remain explicitly separate"));
    container.append(heading);
    container.append(
      seasonCard(player, seasonRecord(player, "current_season"), "Current season"),
      seasonCard(player, seasonRecord(player, "previous_season"), "Prior season")
    );
  }

  function seasonRecord(player, period) {
    const {profile} = describe(player);
    const value = profile?.[period] || player?.[period];
    if (value && typeof value === "object" && Array.isArray(value.weeks)) return value;
    const legacyKey = period === "current_season" ? "current_season_stats" : "previous_season_stats";
    const legacy = array(player?.[legacyKey]);
    return legacy.length
      ? {season: legacy[0]?.season, availability: "observed", weeks: legacy, recorded_stat_lines: legacy.length}
      : null;
  }

  function seasonCard(player, season, fallbackLabel) {
    const card = node("article", "player-lab-season-card");
    card.append(node("h5", "", Number.isFinite(season?.season) ? String(season.season) : fallbackLabel));
    const available = season && ["available", "observed"].includes(season.availability);
    const weeks = available ? array(season.weeks) : [];
    if (!available) {
      card.append(node(
        "p",
        "player-lab-muted",
        season?.note || `Weekly stats ${humanize(season?.availability || "not retained").toLocaleLowerCase()}.`
      ));
      return card;
    }
    const ppr = weeks.map(row => finite(row.fantasy_points_ppr)).filter(value => value !== null);
    const standard = weeks.map(row => finite(row.fantasy_points_standard)).filter(value => value !== null);
    const selected = weeks.map(row => finite(row.fantasy_points_selected)).filter(value => value !== null);
    const scoring = {STD: "Standard", HALF: "Half-PPR", PPR: "PPR"}[season.scoring_mode] || "Selected";
    const totals = node("div", "player-lab-season-totals");
    totals.append(
      compactStat("Stat lines", finite(season.recorded_stat_lines) ?? weeks.length),
      compactStat(`${scoring} points`, sum(selected)),
      compactStat(`${scoring} / game`, average(selected)),
      compactStat("PPR points", sum(ppr)),
      compactStat("PPR / game", average(ppr)),
      compactStat("Standard points", sum(standard))
    );
    card.append(totals);
    const stats = selectedStatTotals(player, weeks);
    if (stats.length) {
      const list = node("dl", "player-lab-stat-list");
      for (const value of stats) list.append(node("dt", "", value.label), node("dd", "", statNumber(value.value)));
      card.append(list);
    }
    return card;
  }

  function compactStat(label, value) {
    const wrapper = node("span");
    wrapper.append(node("small", "", label), node("strong", "", Number.isFinite(value) ? number(value) : "—"));
    return wrapper;
  }

  function sum(values) {
    return values.length ? values.reduce((total, value) => total + value, 0) : null;
  }

  function average(values) {
    return values.length ? sum(values) / values.length : null;
  }

  function selectedStatTotals(player, weeks) {
    const keys = statKeysForPosition(player.position);
    if (!weeks.length) return [];
    const totals = new Map(keys.map(([key]) => [key, 0]));
    for (const row of weeks) {
      for (const [key, value] of Object.entries(row.stat_values || {})) {
        if (totals.has(key) && Number.isFinite(value)) {
          totals.set(key, totals.get(key) + value);
        }
      }
    }
    return keys.map(([key, label]) => ({label, value: totals.get(key)}));
  }

  function statKeysForPosition(position) {
    if (position === "QB") {
      return [["passing_yards", "Pass yds"], ["passing_tds", "Pass TD"], ["passing_interceptions", "INT"], ["rushing_yards", "Rush yds"]];
    }
    if (["RB", "FB"].includes(position)) {
      return [["carries", "Carries"], ["rushing_yards", "Rush yds"], ["rushing_tds", "Rush TD"], ["receptions", "Rec"]];
    }
    if (["WR", "TE"].includes(position)) {
      return [["targets", "Targets"], ["receptions", "Rec"], ["receiving_yards", "Rec yds"], ["receiving_tds", "Rec TD"]];
    }
    if (position === "K") {
      return [["fg_made", "FG made"], ["fg_att", "FG att"], ["pat_made", "PAT made"], ["pat_att", "PAT att"]];
    }
    if (isDefensivePosition(position)) {
      return [["def_tackles_solo", "Solo tackles"], ["def_tackle_assists", "Assists"], ["def_sacks", "Sacks"], ["def_interceptions", "INT"]];
    }
    return [];
  }

  function isDefensivePosition(position) {
    return ["DL", "DE", "DT", "LB", "DB", "CB", "S", "IDP"].includes(position);
  }

  function statNumber(value) {
    return Number.isInteger(value) ? integerFormatter.format(value) : number(value);
  }

  function renderChart(outlook, player, container) {
    container.setAttribute("role", "region");
    container.setAttribute("aria-label", "Weekly actual and projected fantasy points");
    const current = seasonRecord(player, "current_season");
    const previous = seasonRecord(player, "previous_season");
    const scoringMode = outlook?.scoring_mode || current?.scoring_mode || previous?.scoring_mode;
    const scoringLabel = {STD: "Standard", HALF: "Half-PPR", PPR: "PPR"}[scoringMode] || "Selected-scoring";
    const series = [
      chartSeries(`Current ${scoringLabel} actual`, "actual-current", array(current?.weeks).map(row => [row.week, selectedActual(row)])),
      chartSeries(`Prior ${scoringLabel} actual`, "actual-prior", array(previous?.weeks).map(row => [row.week, selectedActual(row)])),
      chartSeries("League-model projection", "projection", array(player.weeks).map(row => [row.week, finite(row.projected_points)]))
    ].filter(value => value.points.length);
    const heading = node("div", "player-lab-grid-heading");
    heading.append(node("h4", "", "Weekly points"), node("p", "", `${scoringLabel} formula actuals alongside the captured league-model projection`));
    container.append(heading);
    if (!series.length) {
      container.append(node("p", "player-lab-chart-empty", "No weekly actual or projection points were retained for this player."));
      return;
    }
    container.append(
      buildChartSvg(player.name, series),
      chartLegend(series),
      node("p", "player-lab-chart-basis", "Historical points use NFLverse Standard/PPR columns (Half-PPR is their midpoint), so custom league bonuses may differ."),
      chartTable(series)
    );
  }

  function selectedActual(row) {
    return finite(row?.fantasy_points_selected);
  }

  function chartSeries(label, className, values) {
    const points = values.filter(([week, value]) => Number.isFinite(week) && value !== null).sort((left, right) => left[0] - right[0]);
    return {label, className, points};
  }

  function buildChartSvg(playerName, series) {
    const width = 680;
    const height = 250;
    const margin = {top: 18, right: 18, bottom: 35, left: 46};
    const weeks = series.flatMap(value => value.points.map(point => point[0]));
    const values = series.flatMap(value => value.points.map(point => point[1]));
    const minimumWeek = Math.min(...weeks);
    const maximumWeek = Math.max(...weeks);
    const floor = Math.floor(Math.min(0, ...values) / 5) * 5;
    const ceiling = Math.max(floor + 5, Math.ceil(Math.max(0, ...values) / 5) * 5);
    const x = week => margin.left + ((week - minimumWeek) / Math.max(1, maximumWeek - minimumWeek)) * (width - margin.left - margin.right);
    const y = value => height - margin.bottom - ((value - floor) / (ceiling - floor)) * (height - margin.top - margin.bottom);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.classList.add("player-lab-chart-svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-labelledby", "playerLabChartTitle playerLabChartDescription");
    svg.append(
      svgText("title", "playerLabChartTitle", `${playerName} weekly points`),
      svgText("desc", "playerLabChartDescription", "Line chart of NFLverse formula actuals and remaining captured league-model projections. Custom league scoring may differ. A data table follows the chart.")
    );
    const gridValues = [...new Set([floor, floor + (ceiling - floor) / 2, 0, ceiling])].sort((left, right) => left - right);
    for (const value of gridValues) {
      svg.append(svgLine(margin.left, y(value), width - margin.right, y(value), "player-lab-chart-grid"));
      svg.append(svgLabel(margin.left - 8, y(value) + 4, number(value), "player-lab-chart-y-label", "end"));
    }
    for (let week = minimumWeek; week <= maximumWeek; week += 1) {
      svg.append(svgLabel(x(week), height - 12, `W${week}`, "player-lab-chart-x-label", "middle"));
    }
    for (const value of series) {
      const polyline = document.createElementNS(SVG_NS, "polyline");
      polyline.setAttribute("points", value.points.map(point => `${x(point[0])},${y(point[1])}`).join(" "));
      polyline.setAttribute("class", `player-lab-chart-line ${value.className}`);
      svg.append(polyline);
      for (const point of value.points) svg.append(svgCircle(x(point[0]), y(point[1]), value.className));
    }
    return svg;
  }

  function svgText(tag, id, content) {
    const value = document.createElementNS(SVG_NS, tag);
    value.id = id;
    value.textContent = content;
    return value;
  }

  function svgLine(x1, y1, x2, y2, className) {
    const value = document.createElementNS(SVG_NS, "line");
    for (const [name, coordinate] of Object.entries({x1, y1, x2, y2})) value.setAttribute(name, String(coordinate));
    value.setAttribute("class", className);
    return value;
  }

  function svgLabel(x, y, content, className, anchor) {
    const value = document.createElementNS(SVG_NS, "text");
    value.setAttribute("x", String(x));
    value.setAttribute("y", String(y));
    value.setAttribute("text-anchor", anchor);
    value.setAttribute("class", className);
    value.textContent = content;
    return value;
  }

  function svgCircle(x, y, className) {
    const value = document.createElementNS(SVG_NS, "circle");
    value.setAttribute("cx", String(x));
    value.setAttribute("cy", String(y));
    value.setAttribute("r", "3.5");
    value.setAttribute("class", `player-lab-chart-point ${className}`);
    return value;
  }

  function chartLegend(series) {
    const legend = node("ul", "player-lab-chart-legend");
    for (const value of series) {
      const item = node("li");
      item.append(node("span", `player-lab-chart-swatch ${value.className}`), value.label);
      legend.append(item);
    }
    return legend;
  }

  function chartTable(series) {
    const disclosure = node("details", "player-lab-chart-table");
    disclosure.append(node("summary", "", "Weekly chart values as a table"));
    const weeks = [...new Set(series.flatMap(value => value.points.map(point => point[0])))].sort((left, right) => left - right);
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const label of ["Week", ...series.map(value => value.label)]) headRow.append(node("th", "", label));
    head.append(headRow);
    const body = document.createElement("tbody");
    for (const week of weeks) {
      const row = document.createElement("tr");
      const weekCell = node("th", "", `Week ${week}`);
      weekCell.scope = "row";
      row.append(weekCell);
      for (const value of series) {
        const point = value.points.find(candidate => candidate[0] === week);
        row.append(node("td", "player-lab-number", point ? number(point[1]) : "—"));
      }
      body.append(row);
    }
    table.append(head, body);
    disclosure.append(table);
    return disclosure;
  }

  return Object.freeze({describe, render});
})();
