"use strict";

window.TradeFilterUi = (() => {
  const SIDES = ["outgoing", "incoming"];
  const CONNECTORS = new Set(["and", "or", "xor"]);
  const $ = id => document.getElementById(id);
  const teamsBySide = {outgoing: [], incoming: []};

  function clauses(side) {
    return [...$(`${side}FilterClauses`).querySelectorAll("[data-filter-clause]")];
  }

  function control(clause, role) {
    const value = clause.querySelector(`[data-filter-role="${role}"]`);
    if (!value) throw new Error(`Filter rule is missing its ${role} control.`);
    return value;
  }

  function checkedValues(container) {
    return [...container.querySelectorAll('input[type="checkbox"]:checked')]
      .map(input => input.value);
  }

  function packageLabel(side) {
    return side === "outgoing" ? "players you give" : "players you receive";
  }

  function playerAriaLabel(side, ruleNumber) {
    return `${side === "outgoing" ? "Players you give" : "Players you receive"} in rule ${ruleNumber}`;
  }

  function positionAriaLabel(side, ruleNumber) {
    return `${side === "outgoing" ? "Positions you give" : "Positions you receive"} in rule ${ruleNumber}`;
  }

  function rosterCapacitySummary(team) {
    const parts = [];
    if (Number.isInteger(team.active_count) && Number.isInteger(team.active_capacity)) {
      parts.push(`${team.active_count}/${team.active_capacity} active`);
    }
    for (const [kind, capacity] of Object.entries(team.reserve_capacity || {}).sort()) {
      const occupied = team.reserve_occupancy?.[kind] || 0;
      const label = kind === "ROOKIE_RESERVE" ? "Rookie Reserve" : kind;
      parts.push(`${label} ${occupied}/${capacity}`);
    }
    return parts.join(" · ");
  }

  function renderPlayerChoices(side, clause, teams) {
    const container = control(clause, "player-choices");
    const selected = new Set(checkedValues(container));
    container.replaceChildren();
    for (const team of teams) {
      const heading = document.createElement("div");
      const capacity = rosterCapacitySummary(team);
      heading.className = "filter-team-label";
      heading.textContent = capacity ? `${team.name} · ${capacity}` : team.name;
      container.append(heading);
      for (const player of team.players) {
        const rosterStatus = player.roster_status === "ACTIVE"
          ? ""
          : ` · ${player.roster_status === "ROOKIE_RESERVE" ? "Rookie Reserve" : player.roster_status}`;
        const label = document.createElement("label");
        label.className = "filter-choice";
        label.dataset.search = `${player.name} ${team.name} ${player.positions.join(" ")} ${player.roster_status}`.toLowerCase();
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = player.player_id;
        checkbox.dataset.teamId = team.team_id;
        checkbox.dataset.filterChoice = "player";
        checkbox.checked = selected.has(player.player_id);
        const text = document.createElement("span");
        text.textContent = player.positions.length
          ? `${player.name} · ${player.positions.join("/")}${rosterStatus}`
          : `${player.name}${rosterStatus}`;
        label.append(checkbox, text);
        container.append(label);
      }
    }
    if (!container.children.length) {
      const empty = document.createElement("p");
      empty.className = "filter-empty";
      empty.textContent = "No players are available for this side.";
      container.append(empty);
    }
    filterPlayerChoices(clause);
  }

  function renderPositionChoices(clause, positions) {
    const container = control(clause, "position-choices");
    const selected = new Set(checkedValues(container));
    container.replaceChildren();
    for (const position of positions) {
      const label = document.createElement("label");
      label.className = "filter-choice position-choice";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = position;
      checkbox.dataset.filterChoice = "position";
      checkbox.checked = selected.has(position);
      const text = document.createElement("span");
      text.textContent = position;
      label.append(checkbox, text);
      container.append(label);
    }
    if (!container.children.length) {
      const empty = document.createElement("p");
      empty.className = "filter-empty";
      empty.textContent = "No roster positions are available for this side.";
      container.append(empty);
    }
  }

  function filterPlayerChoices(clause) {
    const query = control(clause, "player-search").value.trim().toLowerCase();
    const container = control(clause, "player-choices");
    for (const choice of container.querySelectorAll(".filter-choice")) {
      choice.hidden = Boolean(query) && !choice.dataset.search.includes(query);
    }
    for (const heading of container.querySelectorAll(".filter-team-label")) {
      let row = heading.nextElementSibling;
      let hasVisiblePlayer = false;
      while (row && !row.classList.contains("filter-team-label")) {
        if (row.classList.contains("filter-choice") && !row.hidden) hasVisiblePlayer = true;
        row = row.nextElementSibling;
      }
      heading.hidden = !hasVisiblePlayer;
    }
  }

  function renderClause(side, clause) {
    const positions = [...new Set(
      teamsBySide[side].flatMap(team => team.players.flatMap(player => player.positions))
    )].sort();
    renderPlayerChoices(side, clause, teamsBySide[side]);
    renderPositionChoices(clause, positions);
  }

  function refreshClauseOrder(side) {
    clauses(side).forEach((clause, index) => {
      const number = index + 1;
      control(clause, "rule-label").textContent = `Rule ${number}`;
      control(clause, "connector").closest("label").classList.toggle("hidden", index === 0);
      clause.querySelector('[data-filter-action="remove"]').classList.toggle("hidden", index === 0);
      control(clause, "player-choices").setAttribute("aria-label", playerAriaLabel(side, number));
      control(clause, "position-choices").setAttribute("aria-label", positionAriaLabel(side, number));
    });
  }

  function addClause(side) {
    const first = clauses(side)[0];
    const clause = first.cloneNode(true);
    for (const element of clause.querySelectorAll("[id]")) element.removeAttribute("id");
    for (const input of clause.querySelectorAll('input[type="checkbox"]')) input.checked = false;
    control(clause, "connector").value = "and";
    control(clause, "player-mode").value = "any";
    control(clause, "player-search").value = "";
    control(clause, "position-mode").value = "any";
    control(clause, "player-choices").replaceChildren();
    control(clause, "position-choices").replaceChildren();
    $(`${side}FilterClauses`).append(clause);
    renderClause(side, clause);
    refreshClauseOrder(side);
  }

  function removeClause(side, clause) {
    if (clause !== clauses(side)[0]) clause.remove();
    refreshClauseOrder(side);
  }

  function setEnabled(side) {
    const enabled = $(`${side}FilterEnabled`).checked;
    const controls = $(`${side}FilterControls`);
    controls.classList.toggle("disabled", !enabled);
    for (const element of controls.querySelectorAll("input, select, button")) {
      element.disabled = !enabled;
    }
  }

  function populate({bundle, primaryTeamId, incomingTeams}) {
    const primary = bundle
      ? bundle.teams.find(team => team.team_id === primaryTeamId)
      : null;
    teamsBySide.outgoing = primary ? [primary] : [];
    teamsBySide.incoming = bundle ? incomingTeams : [];
    for (const side of SIDES) {
      for (const clause of clauses(side)) renderClause(side, clause);
      refreshClauseOrder(side);
      setEnabled(side);
    }
  }

  function leafForClause(side, clause, index, threeTeam) {
    const description = `${packageLabel(side)} rule ${index + 1}`;
    const playerMode = control(clause, "player-mode").value;
    const positionMode = control(clause, "position-mode").value;
    const playerChoices = control(clause, "player-choices");
    const playerIds = playerMode === "any" ? [] : checkedValues(playerChoices);
    const positions = positionMode === "any"
      ? []
      : checkedValues(control(clause, "position-choices"));
    if (playerMode !== "any" && !playerIds.length) {
      throw new Error(`Choose at least one player for ${description}.`);
    }
    if (!threeTeam && side === "incoming" && ["include", "only"].includes(playerMode)) {
      const owners = new Set(
        [...playerChoices.querySelectorAll('input[type="checkbox"]:checked')]
          .map(input => input.dataset.teamId)
      );
      if (owners.size > 1) {
        throw new Error("Players that must appear together need to be on the same other team within one rule.");
      }
    }
    if (positionMode !== "any" && !positions.length) {
      throw new Error(`Choose at least one position for ${description}.`);
    }
    if (!playerIds.length && !positions.length) return null;
    return {
      player_ids: playerIds,
      player_mode: playerIds.length ? playerMode : null,
      positions,
      position_mode: positions.length ? positionMode : null
    };
  }

  function expressionForSide(side, threeTeam) {
    if (!$(`${side}FilterEnabled`).checked) return {filter: null, expression: null};
    const rows = clauses(side);
    const terms = rows.map((clause, index) => {
      const leaf = leafForClause(side, clause, index, threeTeam);
      const negated = control(clause, "not").checked;
      if (!leaf) {
        if (rows.length === 1 && !negated) return null;
        throw new Error(`Configure ${packageLabel(side)} rule ${index + 1}, or remove it.`);
      }
      return negated ? {operator: "not", operands: [leaf]} : leaf;
    });
    if (terms.length === 1 && terms[0] === null) return {filter: null, expression: null};
    if (terms.length === 1 && !control(rows[0], "not").checked) {
      return {filter: terms[0], expression: null};
    }
    let expression = terms[0];
    for (let index = 1; index < terms.length; index += 1) {
      const operator = control(rows[index], "connector").value;
      if (!CONNECTORS.has(operator)) throw new Error("A filter rule connector is invalid.");
      expression = {operator, operands: [expression, terms[index]]};
    }
    return {filter: null, expression};
  }

  function requestFields(threeTeam) {
    const result = {};
    for (const side of SIDES) {
      const value = expressionForSide(side, threeTeam);
      if (value.expression) result[`${side}_filter_expression`] = value.expression;
      else result[`${side}_filter`] = value.filter;
    }
    return result;
  }

  function bind(onStructureChange) {
    for (const side of SIDES) {
      $(`${side}FilterEnabled`).addEventListener("change", () => setEnabled(side));
      const controls = $(`${side}FilterControls`);
      controls.addEventListener("input", event => {
        if (event.target.dataset.filterRole === "player-search") {
          filterPlayerChoices(event.target.closest("[data-filter-clause]"));
        }
      });
      controls.addEventListener("change", event => {
        const choice = event.target.dataset.filterChoice;
        if (!choice || !event.target.checked) return;
        const clause = event.target.closest("[data-filter-clause]");
        const mode = control(clause, `${choice}-mode`);
        if (mode.value === "any") mode.value = "include";
      });
      controls.addEventListener("click", event => {
        const button = event.target.closest("[data-filter-action]");
        if (!button) return;
        if (button.dataset.filterAction === "add") addClause(side);
        else if (button.dataset.filterAction === "remove") {
          removeClause(side, button.closest("[data-filter-clause]"));
        }
        onStructureChange();
      });
    }
  }

  return {bind, populate, requestFields};
})();
