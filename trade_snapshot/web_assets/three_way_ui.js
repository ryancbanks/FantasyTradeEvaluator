"use strict";

window.ThreeWayUi = (() => {
  const FORMAT = "three_team";
  const $ = id => document.getElementById(id);
  let twoTeamSkipSmallPreference = true;

  function isSelected() {
    return document.querySelector('input[name="tradeFormat"]:checked')?.value === FORMAT;
  }

  function selectedPartnerSlots() {
    return [$("partnerTeamA").value, $("partnerTeamB").value].filter(Boolean);
  }

  function selectedCounterpartyIds(bundle) {
    const selected = new Set(selectedPartnerSlots());
    return bundle
      ? bundle.teams.filter(team => selected.has(team.team_id)).map(team => team.team_id)
      : [...selected].sort();
  }

  function fillPartnerSelect(select, bundle, excludedIds, previousValue) {
    select.replaceChildren(new Option("Choose a team", ""));
    if (bundle) {
      for (const team of bundle.teams) {
        if (!excludedIds.has(team.team_id)) select.add(new Option(team.name, team.team_id));
      }
    }
    select.value = [...select.options].some(option => option.value === previousValue)
      ? previousValue
      : "";
  }

  function syncPartnerOptions(bundle, primaryTeamId, changedPartner = null) {
    let partnerA = $("partnerTeamA").value;
    let partnerB = $("partnerTeamB").value;
    if (partnerA === primaryTeamId) partnerA = "";
    if (partnerB === primaryTeamId) partnerB = "";
    if (partnerA && partnerA === partnerB) {
      if (changedPartner === "b") partnerA = "";
      else partnerB = "";
    }
    fillPartnerSelect($("partnerTeamA"), bundle, new Set([primaryTeamId, partnerB]), partnerA);
    partnerA = $("partnerTeamA").value;
    fillPartnerSelect($("partnerTeamB"), bundle, new Set([primaryTeamId, partnerA]), partnerB);
  }

  function syncFormatControls(bundle = null) {
    const threeTeam = isSelected();
    const skipSmall = $("skipSmall");
    if (threeTeam) {
      if (!skipSmall.disabled) twoTeamSkipSmallPreference = skipSmall.checked;
      skipSmall.checked = false;
    } else if (skipSmall.disabled) {
      skipSmall.checked = twoTeamSkipSmallPreference;
    }
    skipSmall.disabled = threeTeam;
    $("skipSmallRow").classList.toggle("disabled-option", threeTeam);
    $("skipSmallRow").setAttribute("aria-disabled", String(threeTeam));
    $("counterparties").disabled = threeTeam;
    $("twoTeamCounterpartyField").classList.toggle("hidden", threeTeam);
    $("threeTeamPartners").classList.toggle("hidden", !threeTeam);
    for (const id of ["partnerTeamA", "partnerTeamB"]) {
      $(id).disabled = !threeTeam;
      $(id).required = threeTeam;
    }
    $("threeTeamRulesHelp").classList.toggle("hidden", !threeTeam);
    $("minOutgoingLabel").textContent = threeTeam ? "Minimum each team sends" : "Minimum you send";
    $("maxOutgoingLabel").textContent = threeTeam ? "Maximum each team sends" : "Maximum you send";
    $("minIncomingLabel").textContent = threeTeam ? "Minimum each team receives" : "Minimum you receive";
    $("maxIncomingLabel").textContent = threeTeam ? "Maximum each team receives" : "Maximum you receive";
    $("noDropsLabel").textContent = threeTeam
      ? "Do not force any team to drop a player"
      : "Do not force either team to drop a player";
    $("tradeFormatHelp").textContent = threeTeam
      ? "Choose exactly two partner teams. Every valid player route is considered; three-team power is always extrapolated."
      : "Two-team mode searches each selected opponent independently.";
    const adjustmentNotice = $("freeAgentAllocationHelp");
    const showAdjustmentPolicy = threeTeam && !$("noDrops").checked;
    adjustmentNotice.classList.toggle("hidden", !showAdjustmentPolicy);
    $("freeAgentAllocationPolicy").textContent = showAdjustmentPolicy
      ? bundle?.three_team_free_agent_allocation_policy
        || "Choose a ready weekly bundle to load the allocation policy."
      : "";
  }

  function exactCandidateCount(value) {
    const raw = value.candidate_count_text ?? value.candidate_count;
    if (typeof raw === "string" && /^\d+$/.test(raw)) return BigInt(raw);
    if (typeof raw === "number" && Number.isSafeInteger(raw) && raw >= 0) return BigInt(raw);
    throw new Error("The server did not return an exact candidate count.");
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

  function orderedTeamImpacts(row, participantTeamIds) {
    const byTeam = new Map(row.team_impacts.map(impact => [impact.team_id, impact]));
    return participantTeamIds.map(teamId => byTeam.get(teamId)).filter(Boolean);
  }

  function appendImpactLine(container, label, value, className = "") {
    const line = document.createElement("div");
    line.className = `impact-line ${className}`.trim();
    const heading = document.createElement("strong");
    heading.textContent = `${label}: `;
    line.append(heading, document.createTextNode(value));
    container.append(line);
  }

  function impactCell(impact, signed, percent) {
    const cell = document.createElement("td");
    cell.className = "team-impact-cell";
    appendImpactLine(cell, "Gives", impact.give.join("; ") || "None");
    appendImpactLine(cell, "Receives", impact.receive.join("; ") || "None");
    const rosterMoves = [
      impact.adds.length ? `adds ${impact.adds.join("; ")}` : "",
      impact.drops.length ? `drops ${impact.drops.join("; ")}` : ""
    ].filter(Boolean).join(" · ");
    if (rosterMoves) appendImpactLine(cell, "Roster", rosterMoves);
    appendImpactLine(cell, "Power", signed(impact.power_delta), impact.power_delta >= 0 ? "gain" : "loss");
    appendImpactLine(
      cell,
      "Playoff",
      `${percent(impact.playoff_before)} → ${percent(impact.playoff_after)} (${signed(impact.playoff_delta, true)})`,
      impact.playoff_delta >= 0 ? "gain" : "loss"
    );
    return cell;
  }

  function renderTradeRows(
    rows,
    participantTeamIds,
    bundle,
    {signed, percent, powerEvidenceLabel}
  ) {
    const names = new Map((bundle?.teams || []).map(team => [team.team_id, team.name]));
    const firstImpacts = rows.length ? orderedTeamImpacts(rows[0], participantTeamIds) : [];
    const participantNames = firstImpacts.length === 3
      ? firstImpacts.map(impact => impact.team_name)
      : participantTeamIds.map(teamId => names.get(teamId) || teamId);
    setResultHeaders(["Player movement", ...participantNames, "Combined playoff", "Power evidence"]);
    const body = $("resultsBody");
    for (const row of rows) {
      const result = document.createElement("tr");
      if (row.all_teams_gain) result.className = "mutual-row";
      const movement = document.createElement("td");
      movement.className = "transfer-list";
      for (const transfer of row.transfers) {
        const leg = document.createElement("div");
        leg.className = "transfer-leg";
        leg.textContent = `${transfer.from_team_name} → ${transfer.to_team_name}: ${transfer.players.map(player => player.name).join("; ")}`;
        movement.append(leg);
      }
      result.append(movement);
      const impacts = orderedTeamImpacts(row, participantTeamIds);
      for (const impact of impacts) result.append(impactCell(impact, signed, percent));
      for (let index = impacts.length; index < 3; index += 1) {
        const missing = document.createElement("td");
        missing.textContent = "Impact unavailable";
        result.append(missing);
      }
      const combined = document.createElement("td");
      combined.textContent = signed(row.combined_playoff_delta, true);
      combined.className = row.combined_playoff_delta >= 0 ? "gain" : "loss";
      const evidence = document.createElement("td");
      evidence.textContent = powerEvidenceLabel(row.power_methodology_status);
      result.append(combined, evidence);
      body.append(result);
    }
  }

  return Object.freeze({
    FORMAT,
    exactCandidateCount,
    isSelected,
    renderTradeRows,
    selectedCounterpartyIds,
    selectedPartnerSlots,
    syncFormatControls,
    syncPartnerOptions
  });
})();
