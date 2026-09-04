"use strict";

window.PlayerLabProvenanceUi = (() => {
  const $ = id => document.getElementById(id);
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  const integerFormatter = new Intl.NumberFormat();

  function create(format) {
    const required = [
      "array", "evidenceNumber", "humanize", "node", "providerKey",
      "providerLabel", "statusLabel", "timeNode"
    ];
    if (required.some(key => typeof format?.[key] !== "function")) {
      throw new Error("Player Lab provenance formatters are unavailable.");
    }

    function appendProviderStatusObservations(container, source) {
      const observations = Array.isArray(source.provider_status_observations)
        ? source.provider_status_observations
        : [];
      for (const observation of observations) {
        if (!observation || typeof observation.designation !== "string") continue;
        const scope = observation.source_scope === "weekly"
          ? `weekly source${Number.isInteger(observation.source_week) ? `, Week ${observation.source_week}` : ""}`
          : observation.source_scope === "ros"
            ? "rest-of-season source"
            : "provider source";
        const status = format.node(
          "span",
          "player-lab-provider-status",
          `Provider status observed: ${observation.designation} (${scope})`
        );
        const captured = format.timeNode(observation.captured_at, "Captured");
        if (captured) status.append(" · ", captured);
        container.append(status);
      }
    }

    function rawStatEntries(source) {
      if (!source?.raw_projected_stats || typeof source.raw_projected_stats !== "object") return [];
      return Object.entries(source.raw_projected_stats)
        .filter(([name, value]) => name && Number.isFinite(value))
        .sort(([left], [right]) => collator.compare(left, right));
    }

    function appendRawStatPeriod(container, label, source) {
      const entries = rawStatEntries(source);
      if (!entries.length) return false;
      const group = format.node("section", "player-lab-raw-period");
      group.append(format.node("h5", "", label));
      const values = format.node("dl", "player-lab-raw-values");
      for (const [name, value] of entries) {
        const row = format.node("div", "player-lab-raw-value");
        row.dataset.statProvider = format.providerKey(source);
        row.dataset.statName = name;
        const result = format.node("dd", "", format.evidenceNumber(value));
        result.title = `Exact stored value: ${String(value)}`;
        row.append(format.node("dt", "", format.humanize(name)), result);
        values.append(row);
      }
      group.append(values);
      container.append(group);
      return true;
    }

    function renderRawStats(outlook, player) {
      const container = $("playerLabRawStats");
      container.replaceChildren();
      const heading = format.node("div", "player-lab-grid-heading");
      heading.append(
        format.node("h4", "", "Retained raw projected stats"),
        format.node("p", "", "Keys are scoped by provider; same-named fields are never merged")
      );
      container.append(heading);
      for (const provider of outlook.providers) {
        const key = format.providerKey(provider);
        const card = format.node("details", "player-lab-raw-card");
        card.append(format.node("summary", "", `${format.providerLabel(provider)} stat components`));
        const content = format.node("div", "player-lab-raw-card-content");
        let hasStats = false;
        for (const week of player.weeks || []) {
          const source = (week.provider_values || []).find(value => format.providerKey(value) === key);
          hasStats = appendRawStatPeriod(content, `Week ${week.week}`, source) || hasStats;
        }
        const remaining = (player.provider_remaining_season || [])
          .find(value => format.providerKey(value) === key);
        hasStats = appendRawStatPeriod(content, "Full rest of season", remaining) || hasStats;
        if (!hasStats) {
          content.append(format.node(
            "p", "player-lab-muted", "No raw stat components were retained for this provider."
          ));
        }
        card.append(content);
        container.append(card);
      }
    }

    function freshnessCard(title, capturedAt, sourceUpdatedAt, detail = "") {
      const card = format.node("article", "player-lab-freshness-card");
      card.append(format.node("strong", "", title));
      if (detail) card.append(format.node("span", "player-lab-muted", detail));
      const captured = format.timeNode(capturedAt, "Captured");
      const updated = format.timeNode(sourceUpdatedAt, "Source updated");
      card.append(captured || format.node("span", "player-lab-muted", "Capture time unavailable"));
      card.append(updated || format.node("span", "player-lab-muted", "Source update time unavailable"));
      return card;
    }

    function expertPanelDetail(snapshot) {
      const panels = Array.isArray(snapshot.expert_panels) ? snapshot.expert_panels : [];
      if (!panels.length) {
        return Number.isFinite(snapshot.expert_count) ? `${snapshot.expert_count} experts` : "";
      }
      const policies = [...new Set(panels.map(panel => panel.expert_selection_policy).filter(Boolean))];
      const descriptions = [...new Set(panels.map(panel => panel.expert_group_description).filter(Boolean))];
      return [
        `Position panels · ${panels.map(panel => `${panel.position} ${panel.expert_count} · ${panel.expert_group_title || "Expert group unavailable"}`).join(" · ")}`,
        panels.length > 1 && Number.isFinite(snapshot.expert_count) ? `${snapshot.expert_count} unique experts` : "",
        descriptions.join(" / "),
        policies.length ? `Selection policy ${policies.join(", ")}` : ""
      ].filter(Boolean).join(" · ");
    }

    function renderFreshness(outlook) {
      const container = $("playerLabFreshness");
      container.replaceChildren();
      const heading = format.node("div", "player-lab-grid-heading");
      heading.append(
        format.node("h3", "", "Evidence freshness"),
        format.node("p", "", "Weekly collection and source-update times")
      );
      container.append(heading);
      for (const snapshot of outlook.ecr_snapshots || []) {
        container.append(freshnessCard(
          `FantasyPros ECR · ${format.humanize(snapshot.period)}`,
          snapshot.captured_at,
          snapshot.source_updated_at || snapshot.source_published_at,
          expertPanelDetail(snapshot)
        ));
      }
      for (const provider of outlook.providers) {
        container.append(freshnessCard(
          `${format.providerLabel(provider)} projections`,
          provider.captured_at,
          provider.source_published_at,
          "Latest evidence retained for this weekly bundle"
        ));
      }
      const snapshot = outlook.profile_snapshot;
      if (!snapshot) {
        container.append(freshnessCard(
          "Public player profiles", null, null,
          "Not retained in this legacy bundle · projection-only Player Lab"
        ));
        return;
      }
      for (const source of format.array(snapshot.provenance)) {
        const size = Number.isFinite(source.byte_count)
          ? `${integerFormatter.format(source.byte_count)}-byte source response size at capture`
          : "Source response size unavailable";
        container.append(freshnessCard(
          `${format.providerLabel(source.provider)} · ${format.humanize(source.dataset)}`,
          source.captured_at,
          source.source_updated_at,
          `${format.statusLabel(source.status)} · ${size}`
        ));
      }
      if (format.array(snapshot.materialization_issues).length) {
        const issue = format.node("article", "player-lab-freshness-card player-lab-source-warning");
        issue.append(
          format.node("strong", "", "Identity conflicts quarantined"),
          format.node(
            "span", "player-lab-muted",
            `${integerFormatter.format(snapshot.materialization_issues.length)} public rows were withheld instead of guessed.`
          )
        );
        container.append(issue);
      }
    }

    return Object.freeze({appendProviderStatusObservations, renderFreshness, renderRawStats});
  }

  return Object.freeze({create});
})();
