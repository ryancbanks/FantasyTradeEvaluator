# Weekly source collection contract

The installed application collects each provider once per weekly snapshot and performs all trade enumeration and season simulation locally. It never makes one upstream request per candidate trade. FantasyPros analyzer observations are collected only for the small initial/revalidation methodology experiment.

## Supported acquisition paths

### FantasyPros

The supported path is an explicitly paired Chrome/Edge extension operating in the user's ordinary signed-in browser. It captures only the response bodies and tables already used by My Playbook. The collector retains semantic league data and sanitized analyzer observations only; it removes request URLs, query strings, cookies, headers, and the league key before persistence. The installed workflow does not require or request a FantasyPros API key, and the extension has no cookie permission.

### ESPN

ESPN publishes a signed-in [Complete Projections](https://fantasy.espn.com/football/players/projections) table and defines `PROJ` as its projected score for the upcoming game. No supported public ESPN fantasy-football projections API was found in ESPN's developer documentation. The default connector therefore captures the rendered projection table once, page by page, using the user's normal access; it does not depend on undocumented internal endpoints.

The refresh first attempts exactly two bounded, cookie-free ESPN JSON reads: one league snapshot and one season-level `proTeamSchedules_wl` schedule snapshot. Only an explicit 401/403 from either exact URL enables a signed-in retry inside the extension's already-open scan tab. ESPN's page makes those two exact credentialed requests, so the browser attaches its session internally; neither cookie values nor request headers cross the extension boundary. The extension returns only the two size-, type-, redirect-, timeout-, and schema-checked JSON objects. All other public-read failures stop the refresh without retrying. A strict parser requires the current 32-team plus FA schema, one reciprocal home/away appearance for every game, and exactly one explicit game or bye for every team/week. It normalizes ESPN abbreviations and attaches the verified opponent, home/away flag, and provider-independent game ID to every materialized provider row. Schema drift, duplicate games, conflicting provider context, or missing coverage rejects the refresh; no per-player, per-week, or per-trade schedule request is made.

### Yahoo

Yahoo's official [Fantasy Sports API](https://developer.yahoo.com/fantasysports/guide/) requires OAuth and application registration. The installed no-setup workflow instead captures projection rows from a Yahoo league page the user can already view. The app accepts numeric league-home, team, **Players**, and **Player List** (`/playersearch`) addresses, including Yahoo's filtered query strings and season-prefixed paths. It reduces them to the corresponding Players page with the **All Players** roster-status filter before collection, then selects the exact native projection-period and position controls. Before collecting tables, it reads the visible **Receptions** modifier from that same league's Settings page exactly once and requires it to match the selected Standard, Half-PPR, or PPR mode. The collector does not access Fantasy Plus-only fields unless the signed-in user is entitled to them.

An ESPN link may be copied from League Home, My Team, Standings, Scoreboard, Schedule, Rosters, Settings, Transactions, or another page inside the ESPN Fantasy Football league. Only one numeric `leagueId` is retained; route, team, week, view, invite, filter, and hash-route state are discarded before the app constructs its own allowlisted ESPN projection and league-data addresses. When FantasyPros provides a linked ESPN ID, the pasted ID must match it. When FantasyPros omits or changes the shape of its ESPN link, the pasted League Home ID supplies the missing identifier and the later roster/team cross-check still fails closed on the wrong league.

## One-pass collection sequence

1. Pair the extension with a one-use, two-minute code approved from the extension popup. The extension opens one temporary scan tab in the user's normal browser profile; it never opens a second collector tab.
2. Discover the team count and every roster from the signed-in FantasyPros league, then independently capture and cross-check ESPN's league settings, full rosters, standings, completed and remaining fantasy schedules, roster slots/cap, scoring rules, playoff/tiebreak rules, and the complete NFL pro-team schedule payload. No team count or player-by-player roster entry is required.
3. Capture the page's current **Latest ECR** group, its exact per-position expert IDs, weekly/remaining-season ECR, the player crosswalk, and every remaining FantasyPros weekly projection page. The collector verifies the group identity and its recency/accuracy semantics instead of guessing which experts belong in the consensus. ECR changes refresh player inputs; they do not refit formula coefficients. FantasyPros has no verified public in-season projection table for the full remaining season, so the collector does not invent one.
4. Reuse the same scan tab for ESPN and then Yahoo; never keep more than one collector tab open. Closing the app tab, scan tab, or completed session revokes the bridge token and closes the remaining scan tab.
5. Normalize every required provider/player/week cell. A missing or failed row is stored with an explicit status and is never converted to zero.
6. Convert source responses and visible tables into credential-screened, content-addressed capture artifacts, retain those artifacts in a local-only archive, normalize the calculation rows, validate the required provider grid, then publish the immutable snapshot atomically. Cookies, authorization headers, browser storage, and unfiltered transport bodies are never archived.
7. If no validated formula exists—or the public analyzer fingerprint changed—run the bounded power-only calibration experiment: 250 atomic training observations plus 100 leakage-safe blind package observations. The blind design deterministically covers every feasible balanced package size the budget permits and must include 2-for-2, 3-for-3, and 4-for-4. If a compatible exact formula exists, revalidate it on the same scoped design from the current weekly snapshot. Reuse is allowed only when maximum raw score and delta errors are each at most `1e-6` and every displayed change matches; a missing, stale, insufficient, or failed report routes the refresh back through calibration.
8. Exact publication remains the default. An explicit, unchecked-by-default user option may publish a freshly fitted current-week surrogate only when the solver converged, the design is identifiable, training fits within the exact tolerance, and the full diverse blind design ran. The bundle stores a separate content-addressed surrogate disclosure—not an exact attestation—with blind maximum score error, display-match rate, holdout IDs/sizes, and the source fit ID. That fit ID binds the full solver diagnostics checked by the publication gate. A surrogate is never eligible for weekly formula reuse and never replaces the canonical exact formula.
9. Close the scan tab and disconnect the short-lived extension session. Recompute weekly player strengths from current ECR locally; ESPN/Yahoo data feed weekly scoring only, and all later filtering, scenario generation, trade search, standings, playoff analysis, and spreadsheet export remain offline.

The default mode attempts every remaining FantasyPros weekly page and the
current weekly plus rest-of-season ESPN and Yahoo views. A still-empty future
FantasyPros page, or one whose visible season is stale, becomes a timestamped
`not_published` attempt instead of a fabricated projection. An unavailable
optional ESPN or Yahoo page likewise remains a typed provider outcome. The
current FantasyPros page is mandatory. The refresh may continue only when at
least two providers remain configured, and each player/week still has to meet
that source quorum after schedule-aware materialization. Missing ESPN/Yahoo
weekly rows may be allocated from a captured provider rest-of-season total;
FantasyPros rows are never allocated from a nonexistent FantasyPros ROS page.
The future-week option requests additional direct ESPN and Yahoo weeks when
those sites publish them; Yahoo normally exposes only a short rolling window.

## Privacy and freshness

- Browser profiles, cookies, and OAuth tokens remain under the normal browser's control. The extension has no cookie permission and never returns browser storage or credentials to the app. None is exported to Excel or copied into calibration artifacts.
- A capture timestamp records when the app observed a row. A provider publication time is stored separately only when the provider supplies one.
- Provider injury/status labels are retained as sanitized, timestamped observations with their weekly or rest-of-season source scope. They are never treated as proof that a player will or will not play, and derived projection rows preserve that source scope.
- Snapshot readiness requires the exact content-addressed scoring profile and an explicit row from every provider retained in the ensemble configuration for every computation-domain player and remaining week. Providers omitted after a typed page-level failure remain visible in the attempt manifest, while the configured ensemble must still satisfy its quorum. The full verified profile is persisted in the portable engine bundle; ID-only legacy bundles fail closed instead of reconstructing settings from a scoring label.
- Weekly materialization uses the separately verified NFL schedule for game context and byes. Source points and raw projected stats are left unchanged; schedule enrichment never invents a player projection.
- Failed providers are visible in the GUI. A degraded ensemble may run only when its configured minimum observed-provider count is still met; the app never silently drops a provider.
- Matching FantasyPros client-bundle and response-schema fingerprints are necessary but not sufficient for reuse. Every weekly refresh binds its revalidation report to the saved formula, current methodology fingerprint, and current snapshot; a changed fingerprint or failed weekly holdout gate invalidates reuse until calibration passes again.
- Calibration proof is scoped. The current ordinary-power experiment validates only the balanced `(n, n)` package sizes listed in its content-addressed evidence, with no implicit free-agent addition or roster drop. Imbalanced and add/drop searches must fail closed or be labeled outside the exact FantasyPros scope until a separate explicit analyzer experiment validates those operations.
- Imported or collected surrogate bundles require a second explicit acceptance before combination counting or trade search. All such searches remain offline. Observed balanced/no-adjustment shapes are labeled `surrogate`; unobserved, imbalanced, and add/drop shapes are labeled `surrogate_extrapolated`. Neither label is an exact FantasyPros claim.

## Portable installation target

There is no single binary format that runs on literally every device. The release matrix produces self-contained application installers for Windows 11+, current macOS, and supported Debian/Ubuntu 64-bit Linux, covering x64 and ARM64 where the frozen runtime is validated. Python, Codex, compilers, and developer tools are not prerequisites for those native builds. Weekly collection requires a current desktop Chrome or Edge browser that permits a user-loaded Manifest V3 extension. A separate source bootstrap covers supported desktops that already have Python 3.11+, but it is not a substitute for an OS-native artifact. Mobile operating systems and locked-down devices that forbid extension installation are outside the initial target.
