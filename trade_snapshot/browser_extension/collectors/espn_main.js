(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  const readRoot = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl";

  const readJson = async (expectedUrl, maximumBytes, deadline, fantasyFilter = null) => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("espn_read_timeout");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), remaining);
    try {
      const headers = {"Accept": "application/json"};
      if (fantasyFilter !== null) headers["X-Fantasy-Filter"] = fantasyFilter;
      const response = await fetch(expectedUrl, {
        method: "GET",
        headers,
        credentials: "include",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal
      });
      if (response.url !== expectedUrl || response.status !== 200) {
        throw new Error(response.status === 401 || response.status === 403 ?
          "espn_not_authorized" : "espn_unexpected_response");
      }
      const mediaType = (response.headers.get("Content-Type") || "").split(";", 1)[0]
        .trim().toLowerCase();
      const declaredHeader = response.headers.get("Content-Length");
      const declared = declaredHeader === null ? null : Number(declaredHeader);
      if (mediaType !== "application/json" ||
          (declared !== null && (!Number.isFinite(declared) || declared < 0 ||
           declared > maximumBytes))) {
        throw new Error("espn_unsupported_response");
      }
      if (!response.body) throw new Error("espn_unsupported_response");
      const reader = response.body.getReader();
      const chunks = [];
      let length = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > maximumBytes) {
          await reader.cancel().catch(() => {});
          throw new Error("espn_response_too_large");
        }
        chunks.push(value);
      }
      if (!length) throw new Error("espn_response_shape");
      const body = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        body.set(chunk, offset);
        offset += chunk.byteLength;
      }
      const text = new TextDecoder("utf-8", {fatal: true}).decode(body);
      const value = JSON.parse(text);
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("espn_response_shape");
      }
      return value;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("espn_read_timeout");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  };

  handlers["espn.authenticated_json"] = async (options) => {
    if (location.protocol !== "https:" || location.hostname.toLowerCase() !==
        "fantasy.espn.com" || !/^\/football\/players\/projections\/?$/.test(location.pathname)) {
      throw new Error("espn_provenance");
    }
    const views = [
      "mTeam", "mRoster", "mSettings", "mMatchup", "mStandings", "mTransactions2"
    ];
    const transactionFilter = JSON.stringify({transactions: {limit: 1000}});
    const query = new URLSearchParams();
    for (const view of views) query.append("view", view);
    const leagueUrl = `${readRoot}/seasons/${options.season}/segments/0/leagues/` +
      `${options.league_id}?${query.toString()}`;
    const proTeamUrl = `${readRoot}/seasons/${options.season}?view=proTeamSchedules_wl`;
    const deadline = Date.now() + options.timeout_ms;
    const league = await readJson(
      leagueUrl, options.maximum_bytes, deadline, transactionFilter
    );
    const proTeams = await readJson(proTeamUrl, options.maximum_bytes, deadline);
    return {league, pro_teams: proTeams};
  };
})();
