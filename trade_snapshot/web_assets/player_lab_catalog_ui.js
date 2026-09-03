"use strict";

window.PlayerLabCatalogUi = (() => {
  const $ = id => document.getElementById(id);
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  const integerFormatter = new Intl.NumberFormat();
  const AVAILABLE_OWNER = "__available__";
  const POSITION_ORDER = Object.freeze(["QB", "RB", "WR", "TE", "K", "DST"]);
  const TREND_ORDER = Object.freeze({rising: 3, steady: 2, falling: 1, unknown: 0});
  const SORTS = Object.freeze([
    ["overall", "Overall ranking"],
    ["ros_points", "Rest-of-season points"],
    ["trend", "Recent performance trend"],
    ["depth", "Depth chart"],
    ["weekly_ecr", "Weekly ECR"],
    ["ros_ecr", "Rest-of-season ECR"],
    ["disagreement", "Provider disagreement"],
    ["name", "Player name"]
  ]);

  function create(format) {
    const required = [
      "describe", "humanize", "statusLabel", "number", "rank", "ecrRank",
      "ecrDetail", "finite", "overallRank", "overallRankBasis", "positionRank"
    ];
    if (required.some(key => typeof format?.[key] !== "function")) {
      throw new Error("Player Lab catalog formatters are unavailable.");
    }

    function prepareControls(outlook) {
      $("playerLabSearch").value = "";
      $("playerLabProjectionMin").value = "";
      $("playerLabProjectionMax").value = "";
      $("playerLabGroup").value = "none";
      $("playerLabTrendFilter").value = "";
      const owner = $("playerLabOwnerFilter");
      owner.replaceChildren(new Option("All teams and available players", ""));
      const owners = new Map();
      for (const player of outlook.players) {
        if (player.owner) owners.set(player.owner.team_id, player.owner.team_name);
      }
      for (const [teamId, teamName] of [...owners].sort((left, right) => collator.compare(left[1], right[1]))) {
        owner.add(new Option(teamName, teamId));
      }
      if (outlook.players.some(player => !player.owner)) {
        owner.add(new Option("Available / outside calculation pool", AVAILABLE_OWNER));
      }

      const nflTeam = $("playerLabNflTeamFilter");
      nflTeam.replaceChildren(new Option("All NFL teams", ""));
      const nflTeams = [...new Set(outlook.players.map(player => player.nfl_team_id).filter(Boolean))]
        .sort(collator.compare);
      for (const value of nflTeams) nflTeam.add(new Option(value, value));

      const position = $("playerLabPositionFilter");
      position.replaceChildren(new Option("All positions", ""));
      const positions = [...new Set(outlook.players.map(player => player.position).filter(Boolean))]
        .sort(collator.compare);
      for (const value of positions) position.add(new Option(value, value));

      const sort = $("playerLabSort");
      sort.replaceChildren(...SORTS.map(([value, label]) => new Option(label, value)));
      sort.value = "overall";
    }

    function normalizedSearch(player) {
      const description = format.describe(player);
      return [
        player.name, player.position, player.nfl_team_id, player.owner?.team_name,
        description.depth?.position, description.profile?.status, ...(player.eligible_slots || [])
      ].filter(Boolean).join(" ").toLocaleLowerCase();
    }

    function metricCompare(left, right, field, descending) {
      const leftValue = field === "weekly_ecr" || field === "rest_of_season_ecr" ? left[field]?.rank : left[field];
      const rightValue = field === "weekly_ecr" || field === "rest_of_season_ecr" ? right[field]?.rank : right[field];
      const leftMissing = !Number.isFinite(leftValue);
      const rightMissing = !Number.isFinite(rightValue);
      if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
      if (!leftMissing && leftValue !== rightValue) {
        return descending ? rightValue - leftValue : leftValue - rightValue;
      }
      return collator.compare(left.name, right.name) || collator.compare(left.player_id, right.player_id);
    }

    function rankCompare(left, right) {
      const leftRank = format.overallRank(left);
      const rightRank = format.overallRank(right);
      if ((leftRank === null) !== (rightRank === null)) return leftRank === null ? 1 : -1;
      if (leftRank !== null && leftRank !== rightRank) return leftRank - rightRank;
      return metricCompare(left, right, "remaining_projected_points", true);
    }

    function trendCompare(left, right) {
      const leftTrend = format.describe(left).performanceTrend;
      const rightTrend = format.describe(right).performanceTrend;
      const directionDifference = (TREND_ORDER[rightTrend.direction] || 0) - (TREND_ORDER[leftTrend.direction] || 0);
      if (directionDifference) return directionDifference;
      const leftChange = format.finite(leftTrend.change);
      const rightChange = format.finite(rightTrend.change);
      if ((leftChange === null) !== (rightChange === null)) return leftChange === null ? 1 : -1;
      if (leftChange !== null && leftChange !== rightChange) return rightChange - leftChange;
      return rankCompare(left, right);
    }

    function depthCompare(left, right) {
      const nflTeam = collator.compare(left.nfl_team_id || "ZZZ", right.nfl_team_id || "ZZZ");
      if (nflTeam) return nflTeam;
      const positionDifference = positionIndex(left.position) - positionIndex(right.position);
      if (positionDifference) return positionDifference;
      const leftOrder = format.finite(format.describe(left).depth?.order) ?? 999;
      const rightOrder = format.finite(format.describe(right).depth?.order) ?? 999;
      if (leftOrder !== rightOrder) return leftOrder - rightOrder;
      return rankCompare(left, right);
    }

    function playerCompare(left, right) {
      switch ($("playerLabSort").value) {
        case "overall": return rankCompare(left, right);
        case "weekly_ecr": return metricCompare(left, right, "weekly_ecr", false);
        case "ros_ecr": return metricCompare(left, right, "rest_of_season_ecr", false);
        case "trend": return trendCompare(left, right);
        case "depth": return depthCompare(left, right);
        case "disagreement": return metricCompare(left, right, "average_provider_disagreement", true);
        case "name": return collator.compare(left.name, right.name) || collator.compare(left.player_id, right.player_id);
        default: return metricCompare(left, right, "remaining_projected_points", true);
      }
    }

    function filteredPlayers(outlook) {
      const query = $("playerLabSearch").value.trim().toLocaleLowerCase();
      const owner = $("playerLabOwnerFilter").value;
      const nflTeam = $("playerLabNflTeamFilter").value;
      const position = $("playerLabPositionFilter").value;
      const trend = $("playerLabTrendFilter").value;
      const minimum = numericFilterValue($("playerLabProjectionMin").value);
      const maximum = numericFilterValue($("playerLabProjectionMax").value);
      return outlook.players.filter(player => {
        const ownerMatches = !owner || (owner === AVAILABLE_OWNER ? !player.owner : player.owner?.team_id === owner);
        const points = format.finite(player.remaining_projected_points);
        return ownerMatches
          && (!nflTeam || player.nfl_team_id === nflTeam)
          && (!position || player.position === position)
          && trendMatches(player, trend)
          && (minimum === null || (points !== null && points >= minimum))
          && (maximum === null || (points !== null && points <= maximum))
          && (!query || normalizedSearch(player).includes(query));
      }).sort((left, right) => groupCompare(left, right) || playerCompare(left, right));
    }

    function groupCompare(left, right) {
      const grouping = $("playerLabGroup").value;
      if (grouping === "position") {
        const knownDifference = positionIndex(left.position) - positionIndex(right.position);
        return knownDifference || collator.compare(left.position || "", right.position || "");
      }
      if (grouping === "nfl_team") {
        const team = collator.compare(left.nfl_team_id || "ZZZ", right.nfl_team_id || "ZZZ");
        if (team) return team;
        const position = positionIndex(left.position) - positionIndex(right.position);
        if (position) return position;
        return (format.finite(format.describe(left).depth?.order) ?? 999) - (format.finite(format.describe(right).depth?.order) ?? 999);
      }
      if (grouping === "fantasy_team") {
        const leftName = left.owner?.team_name || "ZZZ Available";
        const rightName = right.owner?.team_name || "ZZZ Available";
        return collator.compare(leftName, rightName)
          || collator.compare(left.owner?.team_id || "ZZZ", right.owner?.team_id || "ZZZ");
      }
      return 0;
    }

    function positionIndex(value) {
      const index = POSITION_ORDER.indexOf(value);
      return index < 0 ? 99 : index;
    }

    function numericFilterValue(value) {
      if (typeof value !== "string" || value.trim() === "") return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }

    function trendMatches(player, filter) {
      if (!filter) return true;
      const [source, direction] = filter.includes(":")
        ? filter.split(":", 2)
        : ["performance", filter];
      const description = format.describe(player);
      const actual = source === "market"
        ? description.marketTrend?.direction || "unknown"
        : description.performanceTrend.direction;
      return actual === direction;
    }

    function pageSize() {
      const value = Number($("playerLabPageSize").value);
      return [50, 100, 200].includes(value) ? value : 100;
    }

    function pageCount(players) {
      return Math.max(1, Math.ceil(players.length / pageSize()));
    }

    function selectionForPage(players, currentPage, preferredId) {
      const firstIndex = (currentPage - 1) * pageSize();
      const pagePlayers = players.slice(firstIndex, firstIndex + pageSize());
      return pagePlayers.some(player => player.player_id === preferredId)
        ? preferredId
        : pagePlayers[0]?.player_id || null;
    }

    function render(outlook, players, selectedPlayerId, currentPage, onSelect) {
      const body = $("playerLabTableBody");
      body.replaceChildren();
      const size = pageSize();
      const firstIndex = (currentPage - 1) * size;
      const pagePlayers = players.slice(firstIndex, firstIndex + size);
      const firstShown = pagePlayers.length ? firstIndex + 1 : 0;
      const lastShown = firstIndex + pagePlayers.length;
      $("playerLabCount").textContent = `${players.length.toLocaleString()} of ${outlook.players.length.toLocaleString()} players · showing ${firstShown.toLocaleString()}–${lastShown.toLocaleString()}`;
      renderPagination(players, currentPage);
      if (!players.length) {
        const row = document.createElement("tr");
        const cell = node("td", "player-lab-no-results", "No players match these filters.");
        cell.colSpan = 8;
        row.append(cell);
        body.append(row);
        return;
      }
      let previousGroup = null;
      for (const player of pagePlayers) {
        const group = groupFor(player);
        if (group && group.key !== previousGroup) {
          body.append(groupRow(group.label));
          previousGroup = group.key;
        }
        body.append(playerRow(player, selectedPlayerId, onSelect));
      }
    }

    function renderPagination(players, currentPage) {
      const pages = pageCount(players);
      $("playerLabPageStatus").textContent = `Page ${integerFormatter.format(currentPage)} of ${integerFormatter.format(pages)}`;
      $("playerLabPreviousPage").disabled = currentPage <= 1;
      $("playerLabNextPage").disabled = currentPage >= pages;
    }

    function groupFor(player) {
      switch ($("playerLabGroup").value) {
        case "position":
          return {key: player.position || "unknown", label: player.position || "Position unavailable"};
        case "nfl_team": {
          const team = player.nfl_team_id || "unknown";
          return {key: team, label: team === "unknown" ? "NFL team unavailable" : `${team} · depth chart`};
        }
        case "fantasy_team": {
          const owner = player.owner;
          return owner
            ? {key: `owned-${owner.team_id}`, label: owner.team_name}
            : {key: "available", label: "Available / outside calculation pool"};
        }
        default:
          return null;
      }
    }

    function groupRow(label) {
      const row = document.createElement("tr");
      row.className = "player-lab-group-row";
      const cell = node("th", "", label);
      cell.scope = "row";
      cell.colSpan = 8;
      row.append(cell);
      return row;
    }

    function playerRow(player, selectedPlayerId, onSelect) {
      const description = format.describe(player);
      const row = document.createElement("tr");
      const selected = player.player_id === selectedPlayerId;
      row.dataset.playerId = player.player_id;
      row.setAttribute("aria-selected", String(selected));
      const identity = document.createElement("th");
      identity.scope = "row";
      const choose = node("button", "player-lab-player-button", player.name);
      choose.type = "button";
      choose.dataset.playerId = player.player_id;
      choose.setAttribute("aria-pressed", String(selected));
      choose.addEventListener("click", () => onSelect(player.player_id, true));
      identity.append(choose);
      secondaryText(identity, player.nfl_team_id || "NFL team unavailable");

      const owner = node("td", "", ownerText(player));
      secondaryText(owner, format.humanize(player.availability));
      const position = node("td", "", player.position || "—");
      secondaryText(position, description.depthLabel);
      const points = node("td", "player-lab-number", format.number(player.remaining_projected_points));
      secondaryText(points, `${format.number(player.average_weekly_points)} / active week`);
      const trend = node("td");
      trend.append(node("span", `player-lab-trend is-${description.performanceTrend.direction}`, description.performanceLabel));
      secondaryText(trend, description.marketLabel);
      const overall = node("td", "player-lab-number", format.rank(format.overallRank(player)));
      const projectedPositionRank = format.positionRank(player);
      const positionLabel = projectedPositionRank === null
        ? "Position rank —"
        : `${player.position || "Pos"} ${format.rank(projectedPositionRank)}`;
      secondaryText(overall, `${positionLabel} · ${format.overallRankBasis(player)}`);
      const rosEcr = node("td", "player-lab-number", format.ecrRank(player.rest_of_season_ecr));
      secondaryText(rosEcr, format.ecrDetail(player.rest_of_season_ecr));
      const complete = format.finite(player.provider_complete_week_count) ?? 0;
      const total = format.finite(player.total_week_count) ?? 0;
      const sources = node("td", "", total ? `${complete}/${total} complete` : "No forecast rows");
      secondaryText(sources, total ? `${format.finite(player.all_direct_week_count) ?? 0} all-direct · Provider σ ${format.number(player.average_provider_disagreement)}` : description.profile ? "Public profile only" : "Projection-only legacy row");
      row.append(identity, owner, position, points, trend, overall, rosEcr, sources);
      return row;
    }

    function ownerText(player) {
      return player.owner?.team_name || format.statusLabel(player.availability);
    }

    function node(tag, className = "", text = "") {
      const element = document.createElement(tag);
      if (className) element.className = className;
      element.textContent = text;
      return element;
    }

    function secondaryText(parent, value) {
      parent.append(node("span", "player-lab-muted", value));
    }

    function updateSelection(previousId, nextId) {
      for (const row of $("playerLabTableBody").querySelectorAll("tr[data-player-id]")) {
        if (row.dataset.playerId !== previousId && row.dataset.playerId !== nextId) continue;
        const selected = row.dataset.playerId === nextId;
        row.setAttribute("aria-selected", String(selected));
        row.querySelector(".player-lab-player-button")?.setAttribute("aria-pressed", String(selected));
      }
    }

    return Object.freeze({
      filteredPlayers, pageCount, prepareControls, render, selectionForPage,
      updateSelection
    });
  }

  return Object.freeze({create});
})();
