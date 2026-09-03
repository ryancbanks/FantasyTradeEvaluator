# Fantasy Trade Evaluator

A local NFL trade-search app with two power modes: optional FantasyPros ECR/calibration for replicated power, or a fully independent ESPN-hosted model. ESPN supplies league data and Yahoo supplies league-scored projections. Broad forecasts add CBS, FFToday, and FantasySharks and equal-average each accepted independent publisher once. Weekly collection reads each source once; candidate generation, power filtering, roster adjustment, record projection, playoff simulation, ranking, and Excel export all run on the computer afterward.

## What the app does

- Optionally captures current FantasyPros consensus ECR and projection tables rather than freezing one expert list in code.
- Discovers the league size, every team, every rostered player, roster capacity, and IR/reserve state automatically from the connected league.
- Captures signed-in Yahoo projections and automatically attempts ESPN, CBS, FFToday, and FantasySharks projection tables, recording the accepted provider set in the weekly bundle.
- Equal-averages each accepted independent publisher once. Multiple pages, positions, or time horizons from one publisher never create extra votes.
- Imports every league roster, active/IR capacity, standings, scoring profile, completed matchup, remaining matchup, playoff rule, and tiebreak rule.
- Searches ordinary two-team trades or exhaustive three-team agreements across two selected partners. In a three-team search, every moved player may go to either other team and all three teams must give and receive at least one player.
- Applies the chosen package sizes, side imbalance, equal-size-only, and no-forced-drop rules independently to every team in an agreement.
- Independently filters what you give and receive by exact players or canonical positions. Add as many rules as needed, invert any rule with NOT, and combine them from top to bottom with AND, OR, or XOR (exactly one).
- Computes every team's expected final record, projected finish, and playoff probability from shared correlated season scenarios.
- Opens a standalone league dashboard for every ready week with power rankings, projected standings, playoff odds, a disclosed modeled title chance, and nine interactive league views: title race, contender map, standings movement, schedule difficulty, weekly scoring, finish and seed distributions, position-room strength, and scoring profile.
- Builds **General Manager Insights** from verified completed league activity: completed-deal accessibility, exact transaction-time counterparty opportunity, package shape, frequent partners, waiver/free-agent pace, acquisition retention, verified short-term streaming, roster construction, and captured-lineup continuity. It keeps those behavioral questions separate from current roster compatibility, which exhaustively screens every eligible 1-for-1 swap and complementary position need without using manager history. Historical trades show their value at the time beside the same package and pre-trade roster revalued by the selected current model. Injury-screened foresight labels require enough weekly health evidence; injuries never count toward a good/bad foresight label.
- Adds a **Trade Timing Lab** that joins each team's exact completed record to correlated future season simulations. It ranks schedule-pressure watch windows from loss risk, downward record-slope risk, and playoff sensitivity while separately showing losing-streak risk, then compares a shortlisted 1-for-1 trade in the current window with waiting. Future packages are valued only inside the pre-trade scenario paths where that opponent loses and its record slope is downward, after a minimum conditional-sample gate. A candidate must improve both teams by a declared playoff-probability materiality floor, and every displayed package still requires current-health and trade-deadline verification. Completed-deal timing is always labeled participation—not an offer-acceptance probability—and projected high/low scoring spots are never presented as future market prices or ECR.
- Opens a fully local Player Lab for every rostered player and captured waiver candidate, with weekly opponents, ensemble points, uncertainty, source coverage, weekly/remaining-season ECR, and the exact FantasyPros/ESPN/Yahoo values and missing/bye/derived states behind each projection.
- Builds a portable Player Lab from bulk public data: current/prior weekly statistics, NFL team and depth metadata, seven-day add/drop trends, and three seasons of documented injury reports.
- Lets you search, filter, sort, and group players by position, NFL team/depth, fantasy-team owner, projection, ranking, and trend, then inspect weekly charts and season summaries.
- Shows trades where every participant gains playoff probability first and writes the full result plus league outlook to a real `.xlsx` workbook.

Power and fantasy-point forecasting are deliberately separate. In FantasyPros mode, FantasyPros evidence is used to reproduce FantasyPros power. In independent mode, power is transparently derived from the accepted point-projection ensemble. The core compatibility ensemble uses FantasyPros, ESPN, and Yahoo in FantasyPros mode and ESPN plus Yahoo in independent mode. Broad consensus excludes aggregate FantasyPros and FFA projections from the forecast average, then gives ESPN, Yahoo, and each accepted public publisher one vote. Yahoo public/editorial ranks are never converted into fantasy points; the app uses only the league-scored projection tables captured from the selected signed-in Yahoo league.

## Draft Lab

The app also includes a separate local Draft Lab for training a draft-ranking model from provenance-tagged historical preseason data. Its one-click installer downloads the public 2015–2019 and 2021–2025 starter sources once, verifies them, and builds the corpus locally; strict custom corpora remain importable. Draft Lab supports editable league and scoring rules, deterministic historical snake-draft arenas, generation autosaves and resume, model inspection and export, paired regression-baseline benchmarks, persistent manual draft rooms, and optional public ESPN snake-draft synchronization.

Draft Lab does not download or claim complete historical ESPN or Yahoo archives. ESPN synchronization is a credential-free read of a public draft and requires a current board carrying verified ESPN player-ID mappings; private ESPN leagues and Yahoo live drafts remain manual. See the [Draft Lab guide](docs/draft-lab.md) for the supported years, strict JSON contracts, no-leak rules, compute controls, workflow, privacy guarantees, and current limitations.

## Use an installed desktop build

1. Install the build for the machine: Windows `Setup.exe`, macOS `.dmg`, or the Debian/Ubuntu archive. Python and developer tools are not required; use a current desktop Chrome, Brave, or Edge browser for weekly collection.
2. Open **Fantasy Trade Evaluator**.
3. The first time only, choose **Download extension**, extract the ZIP, open `chrome://extensions` in Chrome or Brave (or `edge://extensions` in Edge), turn on **Developer mode**, choose **Load unpacked**, and select the extracted folder containing `manifest.json`.
4. Return to the app and refresh that page once so the browser injects the newly loaded extension. Choose **Connect extension**, open **Fantasy Trade Evaluator Browser Bridge** from the browser's Extensions menu, verify the four-character code, and choose **Pair with app**. Pairing uses a one-time code and never exports browser cookies. Sign in to FantasyPros, ESPN, and Yahoo normally in that same browser before starting a scan; sessions from a different browser profile cannot be copied.

   If version 0.1.x of the browser extension was already installed, download and extract the current extension, replace the contents of the unpacked folder that the browser already uses, click **Reload** for it on the browser's Extensions page, refresh the app, and reconnect. Weekly collection stays disabled until extension 0.2.0 or newer is connected because verified league history requires its expanded ESPN activity capture.

5. Under **Weekly data**, enter the season, first unplayed week, and scoring format. The league size and rosters are scanned automatically.
6. Paste League Home—or any other page inside the matching ESPN Fantasy Football league—and a numeric Yahoo league, team, Players, or Player List page. The app retains only the numeric league identifiers, safely switches Yahoo to All Players, and builds every other projection-source URL automatically. The exact URLs appear under **Automatic source links & debug**.
7. Choose **Scan league & collect**. The extension reuses one temporary tab in the browser where you are already signed in. If a provider still needs sign-in, complete it in that tab and continue from the local app.
8. Select the ready week, then choose **Calculate league outlook** when you want the **League dashboard**. It runs without starting a trade search, and a matching search reuses the same local season baseline. Its championship percentage is clearly labeled as a local strength-weighted model: current bundles do not contain postseason player projections or enough bracket detail to claim an exact title simulation. Dashboard work is bounded to a deterministic 10,000-scenario prefix when an imported bundle asks for more, and the UI discloses whenever that safeguard is active.
9. Choose **Open GM Insights** to compare every team. It runs only when requested. Three independent answers are shown instead of one opaque score: **roster compatibility** uses only the selected week's rosters, projections, capacity, and local power model; **completed-deal accessibility** uses only completed activity; and **counterparty value opportunity** is exactly the negative of that team's exact contemporaneous relative power edge. Compatibility remains available before any manager history exists and its fast screen covers 1-for-1 swaps only, so a limited result never claims that a larger package cannot work. “Stingy” means that the team's measured relative power edge leaned positive in at least three exact transaction-time valuations; “generous” means it leaned negative; neither label is assigned from a small or ambiguous sample. Choose **Search trades with this team** to carry that partner into the two-team search form without starting a run.
10. Choose **Calculate trade timing** to compare every opponent's actual-plus-projected record curve and best pressure windows. It runs only when requested. A current-window result is a candidate, not an authorized action: confirm that trades remain open and verify current player health before proposing it. Later weeks are conditional watches. The named loss/downturn must occur, the app values the package only within those exact pre-trade scenario paths, at least the displayed minimum number of paths must qualify, the league must still allow trades, and the weekly model should be refreshed. The automatic preview simulates only the three strongest power-screened 1-for-1 candidates per opponent that keep both displayed power changes at or above −5; a “none found” result is explicitly limited to that shortlist unless it happened to contain the complete screen. The full trade search remains the source of truth for custom package size, imbalance, equal-size, no-drop, locked-player, and content-filter rules.
11. Choose **Open Player Lab** to search or filter every calculation player by owner and position. Select a player to reconcile the remaining-season total to every weekly ensemble value, inspect opponent and home/away context, compare FantasyPros/ESPN/Yahoo, see provider disagreement and predictive uncertainty, and distinguish published values from values allocated locally from a captured rest-of-season total. “Available” means the bounded waiver pool sealed into this weekly bundle, not every external free agent.
12. Choose **Two-team** or **Three-team**. Two-team mode can search specific opponents or all of them. Three-team mode requires two distinct partners and considers every valid original-owner-to-final-owner player route among those three teams—not only circular swaps. The package limits, imbalance, equal-size, and no-drop rules apply to each team; the optional content filters apply to the aggregate players your team gives and receives. Within a content rule, its player and position conditions both apply. Each additional rule is combined with the accumulated result above it using AND, OR, or XOR; NOT inverts its individual rule. Three-team power is always outside the attested two-team trade shapes, so exact-engine results are labeled `extrapolated` and surrogate-engine results are labeled `surrogate_extrapolated` before the run begins.
13. Choose **Count combinations**. The count is exact, including values larger than the browser's ordinary integer range, and does not construct every agreement in memory. A three-team count is required before its search can start because even small package limits can produce millions or billions of routes.
14. Choose **Start local search**. The app checkpoints the search so an identical interrupted run can resume directly at its next decision-tree position.
15. Choose **Download Excel workbook** for all qualified trades and the full projected league table. Three-team results include every directed transfer leg, each participant's roster/power/playoff impact, and the complete request/run provenance. To keep local export memory bounded, narrow a three-team search to at most 10,000 qualified trades before exporting.

Open **Player Lab** at any time after selecting a ready week. Newly collected bundles include the full captured public player catalog; older imported bundles continue to work with their projection and bounded-waiver player set. The injury-history panel reports a transparent, non-medical tier based only on documented game reports. Its weighted report index is descriptive, not a probability of future injury or a claim of medical "injury proneness." Missing report coverage is shown as unknown, never as a clean history, and a missing stat line is never assumed to be an injury.

On Windows, remove the installed app from **Settings > Apps > Installed apps**, the
**Uninstall Fantasy Trade Evaluator** Start-menu shortcut, or the standalone
`FantasyTradeEvaluator-...-Uninstall.exe` supplied with the release. Uninstalling
removes the application and its shortcuts but retains weekly league data so a
later reinstall can resume where you left off.

When FantasyPros mode is enabled, the first methodology calibration is intentionally bounded and separate from trade search. It fits coefficients on 250 controlled one-player ordinary-power observations, then tests 100 blind, representative multi-player packages. With a standard 14-player roster, those holdouts cover every nonleaking balanced size from 1-for-1 through 13-for-13, including 2-for-2, 3-for-3, and 4-for-4. The saved formula records the exact holdout IDs and covered sizes. Later weeks reuse it only after a fresh blind verification bound to the current FantasyPros page bundle and weekly evidence. A failed reuse check routes through fresh calibration. If that healthy fit still misses blind exactness, publication remains off by default; the user may explicitly opt into a separate **SURROGATE / APPROXIMATE** weekly engine. A surrogate never overwrites the reusable exact formula, never calls FantasyPros during local search, and can never label a power result exact. Observed balanced/no-adjustment shapes are `surrogate`; imbalanced, add/drop, or unobserved shapes are `surrogate_extrapolated`. Independent mode skips this proprietary-method calibration and labels its transparent local power accordingly.

The safe default collects the current week plus each attempted source's rest-of-season or season table, then allocates only unpublished future weeks against the verified NFL schedule. CBS and FFToday season values are converted to the remaining verified schedule before they enter the ensemble. The advanced future-week checkbox is intentionally off because sites may expose only a short rolling window, not every remaining fantasy week.

## One-command source fallback

Native installers are the recommended path because they include the application
runtime. If a native build is unavailable but the machine has Python
3.11 or newer, open a terminal in this project folder and run:

```powershell
python bootstrap_install.py
```

Use `python3 bootstrap_install.py` on systems where Python is named `python3`.
The bootstrap works on Windows x64, macOS x64/ARM64, and supported Debian/Ubuntu
x64/ARM64 hosts. It creates a user-scoped isolated environment, verifies the local
interface and packaged Chrome/Edge extension, and prints the launcher path.
It does not modify the invoking Python installation or require administrator
access. An interrupted upgrade keeps the preceding verified runtime and launcher.

To remove only that source runtime while retaining weekly app data:

```text
python3 bootstrap_install.py --uninstall
```

The bootstrap needs network access to install the pinned Python dependencies.
A current desktop Chrome, Brave, or Edge installation is required for weekly collection.
No FantasyPros API key, paid API, OAuth application, or
per-trade network request is required.

## Weekly evidence and local guarantees

One collection run uses an explicitly paired extension in the user's ordinary signed-in browser and keeps exactly one temporary scan tab open at a time. The short-lived bridge token stays in browser-session storage; cookies, authorization headers, browser storage, owner/member records, and transport URLs are never returned to the app or written to a portable engine bundle or workbook. The standard workflow requires the user to be signed in to the selected ESPN and Yahoo leagues in that browser; FantasyPros sign-in is required only when FantasyPros mode is enabled.

The first complete Player Lab collection for an NFL week makes nine bounded, credential-free bulk reads—never one call per player or trade. Later league scans in that week reuse the validated local cache unless the user explicitly refreshes it; a transiently incomplete collection is used for that run but is not cached, so the next scan retries it. The sources are two nflverse weekly-stat files, three nflverse injury-report files, Sleeper's active-player/add/drop feeds, and DynastyProcess's weekly exact-ID crosswalk. At most four independent downloads run concurrently. Each source fails independently and carries its URL, capture status, timestamp, and content digest in the portable bundle. These profile sources do not receive projection votes and cannot change the no-double-counting forecast ensemble.

A weekly bundle is immutable and content-addressed. It contains the exact scoring profile and all calculation inputs, including explicit missing/bye/not-published states. Loading fails closed on old or altered schemas, incomplete player/week/provider grids, identity ambiguity, changed analyzer contracts, insufficient formula holdouts, or scoring/league mismatches.

Completed transactions and current roster/lineup/injury-status settings are stored separately in a versioned local history database so the portable engine-bundle schema remains stable. Capture records are immutable; only a stable-keyed unresolved-to-canonical player identity may be enriched later, and conflicting mappings fail closed. The first scan can describe the completed transaction ledger ESPN returns for the current season, but transaction-time value claims require a complete transaction capture spanning the event plus a compatible weekly engine captured strictly before its proposal time. Therefore, scans become more useful over time: later scans can value subsequent trades without substituting current rankings for unavailable historical rankings. A raw hindsight comparison re-scores the identical package in its reconstructed pre-trade roster with the selected current model; it never replaces the transaction-time result. Trades containing draft picks or another unresolved asset remain visible as completed activity but are never partially valued from the player-only subset. A good/bad foresight signal requires at least three comparable trades, exact methodology at both times, ACTIVE status for every traded player throughout complete weekly captures no more than eight days apart, and an interval that clears the neutral band. Any captured injury, unknown/missing health status, coverage gap, suspension, or changed methodology excludes that trade from foresight scoring while leaving its raw comparison visible. ESPN identifies transactions with a proposal timestamp rather than a verified execution timestamp, so the first scan containing a completed transaction supplies only a conservative “executed by” bound. If possible execution windows or participant moves make event order unclear, the app leaves that trade unvalued; uncertainty limited to another team suppresses only the league-wide playoff delta. Imported and older bundles remain compatible and still receive current roster compatibility even when GM history has not been collected.

Lineups are solved as exact assignments, including duplicate slots, FLEX, and superflex. IR and reserve players remain owned but do not consume active roster capacity. Trades that permit roster changes fill open active slots before choosing deterministic adds/drops. In a multi-team agreement, each team's drops are optimized locally and scarce free-agent replacements are reserved in ascending team-ID order from the players still available; this deterministic policy is disclosed in the app, result payload, and workbook whenever roster adjustments are enabled. Season simulation applies the captured platform's score rounding, home-team adjustment, standings, divisions, playoff qualification, and supported tiebreak order. A packed baseline with a 128 MiB score-payload ceiling reuses unchanged team-week scores across trade candidates, while changed lineups are recomputed from the same player draws; oversized matrices automatically retain the prior streaming path.

Trade timing reuses those league rules and common random draws. The observed side of a record trajectory appears only when the completed matchup ledger exactly reproduces the current standings. Future slopes use a last-four-point Theil–Sen trend over cumulative win-equivalent percentage (`win=1`, `tie=0.5`, `loss=0`) with a disclosed ±0.02-per-week neutral band. The timing service uses at most a deterministic 1,000-scenario prefix so switching opponents cannot trigger an unbounded recomputation. Historical trade periods are binary—multiple completed trades in one elapsed scoring period count once—and a Week W trade is classified only from results through Week W−1. The league deadline is not retained yet, so elapsed denominators may include periods in which trading was closed. Mixed proposal/execution timestamp meanings are never pooled. Until health evidence can be aligned to every historical decision period, those associations remain descriptive and cannot personalize a future participation projection.

See [weekly source collection](docs/source-collection.md), [runtime data-source notices](THIRD_PARTY_NOTICES.md), [methodology notes](docs/model-notes.md), and [desktop packaging](docs/packaging.md) for the validation contract and supported release targets.

## Build installers from this repository

The repository includes a one-command native builder. It creates a fresh,
builder-owned `.installer-build-venv`, installs the hash-locked build dependency
closure, freezes and self-checks the application, and writes the finished files
plus `SHA256SUMS` to `release/`:

```powershell
py -3.12 build_installer.py
```

The build host must use 64-bit CPython 3.12.13. A Windows Setup and standalone
uninstaller also require Inno Setup; install it once with
`winget install --id JRSoftware.InnoSetup -e -s winget -i`. Use
`--portable-only` when only the Windows portable ZIP is needed. macOS and Linux
build their own native formats, because PyInstaller does not cross-compile.

GitHub can build every supported platform without local packaging tools. Open
**Actions → Build desktop releases → Run workflow** for temporary downloadable
artifacts. Pushing a version tag that exactly matches `pyproject.toml` (for
example, `v0.1.0`) additionally publishes the installers and a combined checksum
file as durable GitHub Release assets. Generated binaries stay outside Git and
the workflow retains its temporary build artifacts for 14 days.

## Verify the source tree

```powershell
.build-venv\Scripts\python.exe -m unittest discover -s tests -v
```
