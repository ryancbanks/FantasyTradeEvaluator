"use strict";

window.LeagueUi = (() => {
  const UNASSIGNED = "unassigned";
  const ACTIVE_KEY = "fantasy-trade-evaluator.active-league.v1";
  let profiles = [];
  let apiRequest = null;
  let onSelection = null;
  let onError = null;
  let editingProfileId = null;
  let unassignedBundleCount = 0;
  let refreshGeneration = 0;

  const $ = id => document.getElementById(id);

  function selectedId() {
    return $("leagueSelect").value;
  }

  function selectedProfile() {
    const id = selectedId();
    return profiles.find(profile => profile.profile_id === id) || null;
  }

  function isUnassigned() {
    return selectedId() === UNASSIGNED;
  }

  function usesFantasyPros() {
    return $("useFantasyPros")?.checked ?? true;
  }

  function canCollect() {
    const profile = selectedProfile();
    return Boolean(
      profile
      && !profile.archived
      && profile.yahoo_league_id
      && (usesFantasyPros() || profile.espn_league_id)
    );
  }

  function canonicalEspnUrl(profile) {
    if (!profile?.espn_league_id) return "";
    return `https://fantasy.espn.com/football/league?leagueId=${profile.espn_league_id}&seasonId=${profile.season}`;
  }

  function canonicalYahooUrl(profile) {
    if (!profile?.yahoo_league_id) return "";
    return `https://football.fantasysports.yahoo.com/f1/${profile.yahoo_league_id}/players?status=ALL`;
  }

  async function loadEveryProfile(includeArchived) {
    const rows = [];
    let cursor = null;
    let loadedUnassignedBundleCount = 0;
    let firstPage = true;
    const seenCursors = new Set();
    do {
      const query = new URLSearchParams({
        limit: "200",
        include_archived: includeArchived ? "true" : "false"
      });
      if (cursor) query.set("cursor", cursor);
      const page = await apiRequest(`/api/leagues?${query}`);
      if (!Array.isArray(page.profiles)) {
        throw new Error("The saved league list is invalid.");
      }
      rows.push(...page.profiles);
      if (page.unassigned_bundle_count !== undefined) {
        loadedUnassignedBundleCount = Number(page.unassigned_bundle_count);
        if (
          !Number.isSafeInteger(loadedUnassignedBundleCount)
          || loadedUnassignedBundleCount < 0
        ) {
          throw new Error("The unassigned weekly-data count is invalid.");
        }
      } else if (firstPage) {
        throw new Error("The saved league list is missing its weekly-data count.");
      }
      firstPage = false;
      if (page.next_cursor === null || page.next_cursor === undefined) break;
      if (typeof page.next_cursor !== "string" || !page.next_cursor) {
        throw new Error("The league page cursor is invalid.");
      }
      if (seenCursors.has(page.next_cursor)) {
        throw new Error("The saved league list repeated a page. Refresh and try again.");
      }
      seenCursors.add(page.next_cursor);
      cursor = page.next_cursor;
    } while (true);
    return {profiles: rows, unassignedBundleCount: loadedUnassignedBundleCount};
  }

  async function refresh(preferredId = null, {notify = true} = {}) {
    const generation = ++refreshGeneration;
    const previous = selectedId();
    const loaded = await ProgressUi.run(
      "league-list",
      "Loading saved league workspaces",
      () => loadEveryProfile($("showArchivedLeagues").checked)
    );
    if (generation !== refreshGeneration) return selectedProfile();
    profiles = [...loaded.profiles].sort((left, right) =>
      right.season - left.season || left.name.localeCompare(right.name)
    );
    unassignedBundleCount = loaded.unassignedBundleCount;
    const select = $("leagueSelect");
    select.replaceChildren(new Option(
      profiles.length || unassignedBundleCount
        ? "Choose a league"
        : "Add your first league",
      ""
    ));
    for (const profile of profiles) {
      const suffix = profile.archived ? " · archived" : "";
      select.add(new Option(
        `${profile.name} · ${profile.season}${suffix}`,
        profile.profile_id
      ));
    }
    if (unassignedBundleCount) {
      select.add(new Option(
        `Unassigned imports · ${unassignedBundleCount}`,
        UNASSIGNED
      ));
    }
    const remembered = ProgressUi.readDeviceValue(ACTIVE_KEY);
    const choices = new Set([...select.options].map(option => option.value));
    const target = [
      preferredId,
      previous,
      remembered,
      profiles.find(row => !row.archived)?.profile_id,
      unassignedBundleCount ? UNASSIGNED : null
    ].find(value => value && choices.has(value));
    select.value = target || "";
    select.disabled = select.options.length <= 1;
    renderSelection();
    if (notify && onSelection) await onSelection(selectedId());
    return selectedProfile();
  }

  function renderSelection() {
    const profile = selectedProfile();
    const meta = $("leagueMeta");
    const edit = $("editLeagueButton");
    const archive = $("archiveLeagueButton");
    if (profile) {
      const espn = profile.espn_league_id
        ? `ESPN league …${profile.espn_league_id.slice(-4)}`
        : "ESPN connection needed for independent mode";
      const yahoo = profile.yahoo_league_id
        ? `Yahoo league …${profile.yahoo_league_id.slice(-4)}`
        : "Yahoo connection needed";
      meta.textContent = `${profile.scoring} scoring · ${espn} · ${yahoo}${profile.archived ? " · archived (view only)" : ""}`;
      edit.disabled = false;
      archive.disabled = false;
      archive.textContent = profile.archived ? "Restore league" : "Archive league";
    } else if (isUnassigned()) {
      meta.textContent = "These older or portable weeks are intact but have not been assigned to a league workspace.";
      edit.disabled = true;
      archive.disabled = true;
      archive.textContent = "Archive league";
    } else {
      meta.textContent = "Add a league once, then its connection and weekly history stay together.";
      edit.disabled = true;
      archive.disabled = true;
      archive.textContent = "Archive league";
    }
    ProgressUi.writeDeviceValue(ACTIVE_KEY, selectedId() || null);
    renderAssignmentChoices();
  }

  async function changeSelection() {
    closeEditor();
    renderSelection();
    if (onSelection) await onSelection(selectedId());
  }

  function openEditor(profile = null) {
    editingProfileId = profile?.profile_id || null;
    $("leagueEditorTitle").textContent = profile
      ? `Edit ${profile.name}`
      : "Add a league";
    $("leagueName").value = profile?.name || "";
    $("collectionSeason").value = String(profile?.season || new Date().getFullYear());
    $("collectionScoring").value = profile?.scoring || "PPR";
    $("hostLeagueUrl").value = profile ? canonicalEspnUrl(profile) : "";
    $("yahooProjectionUrl").value = profile ? canonicalYahooUrl(profile) : "";
    $("leagueEditor").classList.remove("hidden");
    $("leagueName").focus();
  }

  function closeEditor() {
    editingProfileId = null;
    $("leagueEditor").classList.add("hidden");
  }

  function editorPayload() {
    return {
      name: $("leagueName").value.trim(),
      season: Number($("collectionSeason").value),
      scoring: $("collectionScoring").value,
      host_league_url: $("hostLeagueUrl").value.trim(),
      yahoo_projection_league_url: $("yahooProjectionUrl").value.trim()
    };
  }

  async function saveEditor() {
    const endpoint = editingProfileId
      ? `/api/leagues/${editingProfileId}`
      : "/api/leagues";
    $("saveLeagueButton").disabled = true;
    $("cancelLeagueEditButton").disabled = true;
    try {
      return await ProgressUi.run(
        editingProfileId ? "league-update" : "league-create",
        editingProfileId ? "Saving league settings" : "Creating league workspace",
        async () => {
          const saved = await apiRequest(endpoint, {
            method: "POST",
            body: JSON.stringify(editorPayload())
          });
          closeEditor();
          await refresh(saved.profile_id);
          return saved;
        }
      );
    } finally {
      $("saveLeagueButton").disabled = false;
      $("cancelLeagueEditButton").disabled = false;
    }
  }

  async function toggleArchive() {
    const profile = selectedProfile();
    if (!profile) return;
    const action = profile.archived ? "restore" : "archive";
    setBusy(true);
    try {
      await ProgressUi.run(
        `league-${action}`,
        profile.archived ? "Restoring league workspace" : "Archiving league workspace",
        async () => {
          await apiRequest(
            `/api/leagues/${profile.profile_id}/${action}`,
            {method: "POST", body: ""}
          );
          await refresh(profile.archived ? profile.profile_id : null);
        }
      );
    } finally {
      setBusy(false);
    }
  }

  async function saveMyTeam(teamId, bundleId) {
    const profile = selectedProfile();
    if (
      !profile
      || typeof teamId !== "string"
      || !teamId
      || typeof bundleId !== "string"
      || !bundleId
    ) return;
    const updated = await apiRequest(`/api/leagues/${profile.profile_id}/team`, {
      method: "POST",
      body: JSON.stringify({team_id: teamId, bundle_id: bundleId})
    });
    const index = profiles.findIndex(row => row.profile_id === updated.profile_id);
    if (index >= 0) profiles[index] = updated;
  }

  function bundleCatalogPath() {
    const id = selectedId();
    return id ? `/api/leagues/${id}/bundles` : null;
  }

  function importPath() {
    const profile = selectedProfile();
    return profile
      ? `/api/leagues/${profile.profile_id}/bundles/import`
      : "/api/bundles/import";
  }

  function collectionPayload() {
    const profile = selectedProfile();
    if (!profile || profile.archived) {
      throw new Error("Choose an active league before collecting weekly data.");
    }
    return {
      league_profile_id: profile.profile_id,
      week: Number($("collectionWeek").value),
      include_future_weekly: $("includeFutureWeekly").checked,
      allow_surrogate_power: $("allowSurrogatePower").checked,
      use_fantasypros: $("useFantasyPros").checked,
      use_broad_consensus: $("useBroadConsensus").checked,
      refresh_public_player_data: $("refreshPublicPlayerData").checked
    };
  }

  function renderAssignmentChoices() {
    const wrapper = $("assignBundleControls");
    if (!wrapper) return;
    const select = $("assignLeagueSelect");
    const active = profiles.filter(profile => !profile.archived);
    select.replaceChildren(new Option("Choose a league", ""));
    for (const profile of active) {
      select.add(new Option(`${profile.name} · ${profile.season}`, profile.profile_id));
    }
    wrapper.classList.toggle("hidden", !isUnassigned() || active.length === 0);
  }

  async function assignBundle(bundleId) {
    const profileId = $("assignLeagueSelect").value;
    if (!profileId) {
      throw new Error("Choose the league that owns this weekly bundle.");
    }
    await ProgressUi.run(
      "bundle-assign",
      "Assigning weekly data",
      async () => {
        await apiRequest(
          `/api/leagues/${profileId}/bundles/${bundleId}/assign`,
          {method: "POST", body: ""}
        );
        await refresh(profileId);
      }
    );
  }

  function setBusy(busy) {
    if (busy) closeEditor();
    for (const id of [
      "leagueSelect",
      "addLeagueButton",
      "editLeagueButton",
      "archiveLeagueButton",
      "showArchivedLeagues",
      "assignLeagueSelect",
      "assignBundleButton"
    ]) {
      const element = $(id);
      if (element) element.disabled = busy;
    }
    for (const element of $("leagueEditor").elements) element.disabled = busy;
    if (!busy) {
      $("leagueSelect").disabled = $("leagueSelect").options.length <= 1;
      renderSelection();
      renderAssignmentChoices();
    }
  }

  function bind(options) {
    if (!options || typeof options.api !== "function") {
      throw new TypeError("League UI requires an API request function.");
    }
    apiRequest = options.api;
    onSelection = typeof options.onSelection === "function"
      ? options.onSelection
      : null;
    onError = typeof options.onError === "function"
      ? options.onError
      : error => console.error(error);
    $("leagueSelect").addEventListener("change", () => {
      changeSelection().catch(onError);
    });
    $("showArchivedLeagues").addEventListener("change", () => {
      refresh(null).catch(onError);
    });
    $("addLeagueButton").addEventListener("click", () => openEditor());
    $("editLeagueButton").addEventListener("click", () => openEditor(selectedProfile()));
    $("archiveLeagueButton").addEventListener("click", () => {
      toggleArchive().catch(onError);
    });
    $("leagueEditor").addEventListener("submit", event => {
      event.preventDefault();
      saveEditor().catch(onError);
    });
    $("cancelLeagueEditButton").addEventListener("click", closeEditor);
  }

  return Object.freeze({
    UNASSIGNED,
    assignBundle,
    bind,
    bundleCatalogPath,
    canCollect,
    collectionPayload,
    importPath,
    isUnassigned,
    refresh,
    saveMyTeam,
    selectedId,
    selectedProfile,
    setBusy
  });
})();
