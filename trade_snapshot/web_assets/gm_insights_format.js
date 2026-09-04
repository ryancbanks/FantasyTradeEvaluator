"use strict";

window.GmInsightsFormat = (() => {
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  const numberFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
  const integerFormatter = new Intl.NumberFormat();
  const percentFormatter = new Intl.NumberFormat(undefined, {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 1
  });
  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric"
  });
  const confidenceLabels = Object.freeze({
    unavailable: "Unavailable",
    descriptive_only: "Descriptive only",
    uncertain: "Uncertain",
    moderate: "Moderate evidence",
    strong: "Strong evidence"
  });
  const valueStyleLabels = Object.freeze({
    value_capturing: "Value-capturing (“stingy”)",
    concessionary: "Concessionary (“generous”)",
    consistently_even_at_display_precision: "Even at display precision",
    even: "Even",
    mixed_no_clear_lean: "Mixed / no clear lean",
    unvalued: "Not valued"
  });
  const fitLabels = Object.freeze({
    verified_mutual_positive_fit: "Verified mutual-positive fit",
    modeled_mutual_positive_fit: "Modeled mutual-positive fit",
    reciprocal_positional_fit: "Reciprocal position fit",
    one_way_positional_fit: "One-way position fit",
    limited: "Limited current evidence"
  });
  const exclusionReasons = Object.freeze({
    selected_current_bundle_not_supplied: "No current weekly model was available",
    current_scoring_profile_is_not_comparable: "League scoring settings changed",
    current_model_is_missing_historical_roster_players: "The current model is missing a player from the pre-trade roster",
    current_model_cannot_score_historical_roster_context: "The current model cannot score the original pre-trade roster",
    trade_contains_unsupported_or_unresolved_asset: "The completed trade contains a draft pick or unresolved asset, so no partial-package value is shown",
    transaction_history_is_incomplete: "The captured transaction ledger is incomplete, so historical value is withheld",
    intervening_league_move_order_is_ambiguous: "Another team moved players during an overlapping execution window",
    playoff_simulation_inputs_are_incomplete: "The historical league-wide playoff inputs were incomplete",
    power_methodology_is_not_exact_at_both_times: "A comparable holdout-validated power method was not available at both dates",
    strength_role_definition_changed: "The lineup-role method changed between the two dates",
    source_health_capture_missing: "No health snapshot was captured close enough to the trade",
    current_health_capture_missing: "No health snapshot was captured close enough to the current model",
    source_health_roster_capture_incomplete: "The health roster near the trade was incomplete",
    current_health_roster_capture_incomplete: "The current health roster was incomplete",
    intermediate_health_roster_capture_incomplete: "A health roster between the trade and today was incomplete",
    health_capture_gap_exceeds_eight_days: "The health-history gap was longer than eight days",
    traded_player_health_status_unknown: "A traded player’s health status was unknown",
    physical_injury_status_observed: "A physical injury was observed after the trade",
    non_physical_unavailability_observed: "A traded player had a non-injury absence",
    unrecognized_health_status_observed: "A health status could not be interpreted",
    current_revaluation_unavailable: "The same historical package could not be revalued"
  });

  function pick(value, ...keys) {
    if (!value || typeof value !== "object") return null;
    for (const key of keys) {
      if (value[key] !== undefined && value[key] !== null) return value[key];
    }
    return null;
  }

  function estimate(value) {
    if (Number.isFinite(value)) return value;
    return value && Number.isFinite(value.estimate) ? value.estimate : null;
  }

  function count(value) {
    const result = estimate(value);
    return Number.isFinite(result) ? integerFormatter.format(result) : null;
  }

  function metricUnit(value, fallback = "number") {
    return typeof value?.unit === "string" ? value.unit.toLowerCase() : fallback;
  }

  function formatMetric(value, fallbackUnit = "number") {
    const result = estimate(value);
    if (!Number.isFinite(result)) return null;
    const unit = metricUnit(value, fallbackUnit).replaceAll("-", "_");
    if (["probability", "probability_0_to_1", "proportion", "fraction", "share", "share_0_to_1"].includes(unit)) {
      return percentFormatter.format(result);
    }
    if (["percentage_point", "percentage_points", "pp"].includes(unit)) return `${numberFormatter.format(result)} pp`;
    if (["percent", "percentage"].includes(unit)) return `${numberFormatter.format(result)}%`;
    if (["per_10_weeks", "per_10_team_weeks", "completed_trades_per_10_observed_weeks", "adds_per_10_observed_weeks"].includes(unit)) {
      return `${numberFormatter.format(result)} / 10 weeks`;
    }
    if (unit === "power_points_per_trade") return `${numberFormatter.format(result)} power points / trade`;
    if (["per_week", "moves_per_week"].includes(unit)) return `${numberFormatter.format(result)} / week`;
    if (unit === "weeks") return `${numberFormatter.format(result)} weeks`;
    if (unit === "days") return `${numberFormatter.format(result)} days`;
    return numberFormatter.format(result);
  }

  function formatDate(value) {
    if (typeof value !== "string" || !value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? value : dateFormatter.format(parsed);
  }

  function confidenceStatus(value) {
    const raw = typeof value?.confidence === "string"
      ? value.confidence
      : value?.confidence?.status ?? value?.evidence_strength ?? value?.status;
    return typeof raw === "string" && confidenceLabels[raw] ? raw : "unavailable";
  }

  function metricDetail(value, empty = "Not enough verified history") {
    if (!value || typeof value !== "object") return empty;
    const parts = [];
    const interval = value.interval || (Array.isArray(value.interval_80)
      ? {lower: value.interval_80[0], upper: value.interval_80[1], level: .8}
      : null);
    if (Number.isFinite(interval?.lower) && Number.isFinite(interval?.upper)) {
      const level = Number.isFinite(interval.level)
        ? (interval.level <= 1 ? interval.level * 100 : interval.level)
        : null;
      const low = formatMetric({...value, estimate: interval.lower});
      const high = formatMetric({...value, estimate: interval.upper});
      parts.push(`${level ? `${numberFormatter.format(level)}% ` : ""}estimate range ${low}–${high}`);
    }
    const sample = value.sample || {};
    const raw = pick(sample, "valued_n", "raw_n", "effective_n");
    if (Number.isFinite(raw)) {
      parts.push(`${integerFormatter.format(raw)} ${raw === 1 ? "observation" : "observations"}`);
    }
    const coverage = value.evidence?.coverage_ratio;
    if (Number.isFinite(coverage)) parts.push(`${percentFormatter.format(coverage)} coverage`);
    else if (value.evidence?.coverage_complete === true) parts.push("Complete history");
    else if (value.evidence?.coverage_complete === false) parts.push("Partial history");
    parts.push(confidenceLabels[confidenceStatus(value)]);
    const reason = value.confidence?.reasons?.find(item => typeof item === "string" && item);
    if (reason) parts.push(reason);
    return parts.join(" · ");
  }

  function percentile(value) {
    const raw = value?.league_percentile;
    if (!Number.isFinite(raw)) return null;
    const scaled = raw <= 1 ? Math.round(raw * 100) : Math.round(raw);
    const mod100 = scaled % 100;
    const suffix = mod100 >= 11 && mod100 <= 13
      ? "th"
      : ({1: "st", 2: "nd", 3: "rd"}[scaled % 10] || "th");
    return `${scaled}${suffix} percentile`;
  }

  function plainText(value) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (Array.isArray(value)) {
      const parts = value.filter(item => typeof item === "string" && item.trim());
      return parts.length ? parts.join(", ") : null;
    }
    return typeof value?.label === "string" ? value.label : null;
  }

  function rankedRowsText(rows, labelKey, countKey = "count") {
    if (!Array.isArray(rows)) return null;
    const parts = rows.slice(0, 4).map(row => {
      const label = plainText(row?.[labelKey]);
      const amount = row?.[countKey];
      return label ? `${label}${Number.isFinite(amount) ? ` (${integerFormatter.format(amount)})` : ""}` : null;
    }).filter(Boolean);
    return parts.length ? parts.join(", ") : null;
  }

  function teamSections(team) {
    const tradeBehavior = team?.trade_behavior || {};
    const roster = team?.roster_construction || {};
    return {
      activity: team?.trade_activity || tradeBehavior.trade_activity || tradeBehavior,
      value: team?.trade_value || tradeBehavior.trade_value || {},
      style: team?.trade_style || tradeBehavior.trade_style || {},
      acquisition: team?.acquisition_behavior || {},
      roster,
      lineup: team?.lineup_behavior || roster.lineup_behavior || {}
    };
  }

  function teamMetrics(team) {
    const {activity, value, acquisition, roster, lineup} = teamSections(team);
    const accessibility = team?.deal_accessibility || {};
    const opportunity = team?.counterparty_value_opportunity || {};
    return {
      activity,
      value,
      acquisition,
      roster,
      lineup,
      accessibility,
      opportunity,
      hindsight: team?.hindsight_value_drift || {},
      tradeRate: pick(activity, "trades_per_10_weeks", "completed_trade_rate", "trade_rate"),
      tradeLikelihood: pick(accessibility, "primary_metric") ?? pick(activity, "next_two_week_trade_propensity", "posterior_next_eligible_week_completed_trade_probability", "trade_likelihood"),
      completedTrades: pick(activity, "completed_trades", "trade_count"),
      valueEdge: pick(value, "relative_power_edge", "relative_power_edge_pp", "typical_value_edge"),
      opportunityMetric: pick(opportunity, "relative_power_opportunity"),
      rosterActivity: pick(acquisition, "acquisitions_per_10_weeks", "adds_per_10_weeks", "moves_per_week"),
      lineupSignal: pick(lineup, "starter_continuity", "lineup_efficiency") ?? pick(roster, "starter_continuity", "lineup_efficiency")
    };
  }

  function valueStyle(team) {
    const {value} = teamSections(team);
    const raw = plainText(pick(value, "tendency_label", "tendency", "value_style", "label"));
    if (!raw) return "Not classified";
    const label = valueStyleLabels[raw] || raw.replaceAll("_", " ");
    const alias = plainText(value.plain_language_alias);
    return alias && !label.toLowerCase().includes(alias.toLowerCase()) ? `${label} (“${alias}”)` : label;
  }

  function valueEdgeText(value) {
    const amount = estimate(value);
    if (!Number.isFinite(amount)) return null;
    const magnitude = formatMetric({...value, estimate: Math.abs(amount)}, metricUnit(value, "percentage_points"));
    if (Math.abs(amount) < 1e-9) return "Even at measured precision";
    return `${magnitude} in ${amount > 0 ? "this team’s" : "trade partners’"} favor`;
  }

  function opportunityText(value) {
    const amount = estimate(value);
    if (!Number.isFinite(amount)) return null;
    const magnitude = formatMetric({...value, estimate: Math.abs(amount)}, metricUnit(value, "power_points_per_trade"));
    if (Math.abs(amount) < 1e-9) return "Even at measured precision";
    return `${amount > 0 ? "+" : "−"}${magnitude} for counterparties`;
  }

  function signedMetric(value, unit = "number") {
    if (!Number.isFinite(value)) return null;
    const magnitude = formatMetric(Math.abs(value), unit);
    return `${value > 0 ? "+" : value < 0 ? "−" : ""}${magnitude}`;
  }

  function compatibilityOwner(teams, teamId) {
    return teams?.find(team => team.team_id === teamId)?.roster_compatibility || null;
  }

  function compatibilityWithPrimary(teams, primaryTeamId, teamId) {
    if (!primaryTeamId || !teamId) return null;
    if (primaryTeamId === teamId) return {is_primary_team: true};
    const owner = compatibilityOwner(teams, primaryTeamId);
    return owner?.partners?.find(row => row.partner_team_id === teamId) || null;
  }

  function fitLabel(value) {
    if (value?.is_primary_team) return "Your selected team";
    return fitLabels[value?.evidence_tier] || "Current fit unavailable";
  }

  function positionNames(rows) {
    if (!Array.isArray(rows)) return [];
    return rows.map(row => plainText(row?.position)).filter(Boolean);
  }

  function humanizeReason(reason) {
    if (typeof reason !== "string" || !reason) return "An eligibility check was not met";
    return exclusionReasons[reason] || reason.replaceAll("_", " ").replace(/^./, value => value.toUpperCase());
  }

  function humanizeReasons(reasons) {
    if (!Array.isArray(reasons)) return [];
    return [...new Set(reasons)].map(humanizeReason);
  }

  return Object.freeze({
    collator,
    numberFormatter,
    integerFormatter,
    percentFormatter,
    confidenceLabels,
    pick,
    estimate,
    count,
    metricUnit,
    formatMetric,
    formatDate,
    confidenceStatus,
    metricDetail,
    percentile,
    plainText,
    rankedRowsText,
    teamSections,
    teamMetrics,
    valueStyle,
    valueEdgeText,
    opportunityText,
    signedMetric,
    compatibilityOwner,
    compatibilityWithPrimary,
    fitLabel,
    positionNames,
    humanizeReason,
    humanizeReasons
  });
})();
