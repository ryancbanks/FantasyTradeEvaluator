# Fantasy Trade Evaluator

A local NFL trade-search app that uses current FantasyPros ECR, FantasyPros/ESPN/Yahoo projections, the complete host-league schedule, and an empirically verified reproduction of FantasyPros roster power. Weekly collection uses the signed-in pages once; candidate generation, power filtering, roster adjustment, record projection, playoff simulation, ranking, and Excel export all run on the computer afterward.

## What the app does

- Captures FantasyPros' current **Latest ECR** group, including the exact
  per-position expert panels selected by the page, and the projection tables
  that are actually published rather than freezing an expert list in code.
- Discovers the league size, every team, every rostered player, roster capacity, and IR/reserve state automatically from the connected league.
- Captures ESPN and Yahoo projections and forms a weekly player-level ensemble.
- Imports every league roster, active/IR capacity, standings, scoring profile, completed matchup, remaining matchup, playoff rule, and tiebreak rule.
- Searches ordinary two-team trades or exhaustive three-team agreements across two selected partners. In a three-team search, every moved player may go to either other team and all three teams must give and receive at least one player.
- Applies the chosen package sizes, side imbalance, equal-size-only, and no-forced-drop rules independently to every team in an agreement.
- Independently filters what you give and receive by exact players or canonical positions. Add as many rules as needed, invert any rule with NOT, and combine them from top to bottom with AND, OR, or XOR (exactly one).
- Computes every team's expected final record, projected finish, and playoff probability from shared deterministic season scenarios. The current default uses independent player shocks until historical forecast-versus-actual data can calibrate shared game/team correlations.
- Opens a standalone league dashboard for every ready week with power rankings, projected standings, playoff odds, a disclosed modeled title chance, and nine interactive league views: title race, contender map, standings movement, schedule difficulty, weekly scoring, finish and seed distributions, position-room strength, and scoring profile.
- Opens a fully local Player Lab for every rostered player and captured waiver candidate, with weekly opponents, ensemble points, uncertainty, source coverage, weekly/remaining-season ECR, and the exact FantasyPros/ESPN/Yahoo values and missing/bye/derived states behind each projection.
- Shows trades where every participant gains playoff probability first and writes the full result plus league outlook to a real `.xlsx` workbook.

FantasyPros power and fantasy-point projection are deliberately separate. Only FantasyPros evidence is used to reproduce FantasyPros power. ESPN and Yahoo affect the weekly point distributions, projected records, and playoff probabilities; they do not alter the reproduced FantasyPros power formula.

## Use an installed desktop build

1. Install the build for the machine: Windows `Setup.exe`, macOS `.dmg`, or the Debian/Ubuntu archive. Python and developer tools are not required; use a current desktop Chrome or Edge browser for weekly collection.
2. Open **Fantasy Trade Evaluator**.
3. The first time only, choose **Download extension**, extract the ZIP, open `chrome://extensions` (or `edge://extensions`), turn on **Developer mode**, choose **Load unpacked**, and select the extracted folder containing `manifest.json`.
4. Return to the app, choose **Connect extension**, open **Fantasy Trade Evaluator Browser Bridge** from the browser's Extensions menu, and choose **Pair with app**. Pairing uses a one-time code and never exports browser cookies. Sign in to FantasyPros, ESPN, and Yahoo normally in that same browser before starting a scan; sessions from a different browser profile cannot be copied.
5. Under **Weekly data**, enter the season, first unplayed week, and scoring format. The league size and rosters are scanned automatically.
6. Paste any page from the matching numeric Yahoo league (league home, team, **Players**, or **Player List**). Filtered, `/playersearch`, and season-prefixed Yahoo addresses are accepted. Also paste League Home—or any other page inside the matching ESPN Fantasy Football league. The app retains only ESPN's numeric `leagueId`; that pasted ID is used when FantasyPros does not expose its own ESPN link and is cross-checked when FantasyPros does.
7. Choose **Scan league & collect**. The extension reuses one temporary tab in the browser where you are already signed in. If a provider still needs sign-in, complete it in that tab and continue from the local app.
8. Select the ready week. The **League dashboard** calculates immediately from that bundle without starting a trade search. Open **Data coverage and model limits** to see source-published versus derived coverage, missing provider cells, and the exact limitations for every calculation. Its championship percentage is clearly labeled as a local strength-weighted model: current bundles retain full rest-of-season totals and the NFL schedule, but do not yet materialize postseason player/week cells or enough bracket detail to claim an exact title simulation. Dashboard work is bounded to a deterministic 10,000-scenario prefix when an imported bundle asks for more, and the UI discloses whenever that safeguard is active.
9. Use **Player Lab** to search or filter every calculation player by owner and position. Select a player to see the full NFL rest-of-season total separately from the materialized fantasy regular-season slice, inspect opponent and home/away context, compare FantasyPros/ESPN/Yahoo, see provider disagreement and predictive uncertainty, and distinguish published values from values allocated locally from a captured rest-of-season total. Provider injury/status designations retain their capture time and weekly/ROS source scope so differing labels are visible; they are observations only and are never converted into certain availability or appearance probabilities. Residual points and raw stat components are divided evenly across missing active weeks and labeled as local allocations, not provider-published matchup shapes. Expand **Retained raw projected stats** to inspect each provider's retained source stat components; keys remain provider-scoped, so equal field names from different sources never collide. A full-horizon total built from complete published weekly rows is labeled `derived_weekly` instead of appearing missing. “Available” means the bounded waiver pool sealed into this weekly bundle, not every external free agent.
10. Choose **Two-team** or **Three-team**. Two-team mode can search specific opponents or all of them. Three-team mode requires two distinct partners and considers every valid original-owner-to-final-owner player route among those three teams—not only circular swaps. The package limits, imbalance, equal-size, and no-drop rules apply to each team; the optional content filters apply to the aggregate players your team gives and receives. Within a content rule, its player and position conditions both apply. Each additional rule is combined with the accumulated result above it using AND, OR, or XOR; NOT inverts its individual rule. Three-team power is always outside the attested two-team trade shapes, so exact-engine results are labeled `extrapolated` and surrogate-engine results are labeled `surrogate_extrapolated` before the run begins.
11. Choose **Count combinations**. The count is exact, including values larger than the browser's ordinary integer range, and does not construct every agreement in memory. A three-team count is required before its search can start because even small package limits can produce millions or billions of routes.
12. Choose **Start local search**. The app checkpoints the search so an identical interrupted run can resume directly at its next decision-tree position.
13. Choose **Download Excel workbook** for all qualified trades and the full projected league table. Three-team results include every directed transfer leg, each participant's roster/power/playoff impact, and the complete request/run provenance. To keep local export memory bounded, narrow a three-team search to at most 10,000 qualified trades before exporting.

On Windows, remove the installed app from **Settings > Apps > Installed apps**, the
**Uninstall Fantasy Trade Evaluator** Start-menu shortcut, or the standalone
`FantasyTradeEvaluator-...-Uninstall.exe` supplied with the release. Uninstalling
removes the application and its shortcuts but retains weekly league data so a
later reinstall can resume where you left off.

The first methodology calibration is intentionally bounded and separate from trade search. It fits coefficients on 250 controlled one-player ordinary-power observations, then tests 100 blind, representative multi-player packages. With a standard 14-player roster, those holdouts cover every nonleaking balanced size from 1-for-1 through 13-for-13, including 2-for-2, 3-for-3, and 4-for-4. The saved formula records the exact holdout IDs and covered sizes. Later weeks reuse it only after a fresh blind verification bound to the current FantasyPros page bundle and weekly evidence. A failed reuse check routes through fresh calibration. If that healthy fit still misses blind exactness, publication remains off by default; the user may explicitly opt into a separate **SURROGATE / APPROXIMATE** weekly engine. A surrogate never overwrites the reusable exact formula, never calls FantasyPros during local search, and can never label a power result exact. Observed balanced/no-adjustment shapes are `surrogate`; imbalanced, add/drop, or unobserved shapes are `surrogate_extrapolated`.

The safe default attempts every remaining FantasyPros weekly page because FantasyPros has no verified public in-season rest-of-season projection table. Published weeks are retained directly; an empty future page or a page still showing the prior season is recorded as `not_published`, never relabeled as a current projection. The current FantasyPros page remains required, while ESPN and Yahoo contribute their current weekly and rest-of-season views when available. A refresh can degrade only when at least two providers still satisfy the configured source quorum. The optional future-week checkbox asks ESPN and Yahoo for additional direct weekly pages when those sites publish them.

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
A current desktop Chrome or Edge installation is required for weekly collection.
No FantasyPros API key, paid API, OAuth application, or
per-trade network request is required.

## Weekly evidence and local guarantees

One collection run uses an explicitly paired extension in the user's ordinary signed-in browser and keeps exactly one temporary scan tab open at a time. The short-lived bridge token stays in browser-session storage; cookies, authorization headers, browser storage, owner/member records, and transport URLs are never returned to the app or written to a portable engine bundle or workbook. Before Yahoo projections are collected, the app verifies the selected league's reception scoring from its visible Settings table and stops with a direct message if it does not match the chosen Standard, Half PPR, or PPR mode.

A weekly bundle is immutable and content-addressed. It contains the exact scoring profile, NFL schedule, ensemble configuration, ECR, raw projection evidence, formula, rosters, and all current calculation inputs, including explicit missing/bye/not-published states. Loading rebuilds the strength model from those inputs and fails closed on old or altered schemas, incomplete player/week/provider grids, detached provider values or identities, schedule conflicts, identity ambiguity, changed analyzer contracts, and insufficient formula holdouts. A projection-source manifest binds every normalized projection to its captured artifact and records provider attempt outcomes, source scoring format, provider-total versus locally recomputed points, and base-format versus exact-host-rule compatibility. Sanitized capture artifacts are also kept in a content-addressed, local-only archive for repair and re-normalization; they are never copied into a workbook or portable bundle. The custom-scoring warning is removed only when every retained source proves exact local recomputation. The live collection cross-checks the host and FantasyPros leagues. Its portable league source manifest retains only a privacy-safe opaque league binding, source provider, content IDs, capture times, and completed-history availability—never the provider's private league ID. Captured FantasyPros standings and probabilities are retained separately as a comparison benchmark for drift review and are never blended into local outlooks or playoff odds.

Lineups are solved as exact assignments, including duplicate slots, FLEX, and superflex. IR and reserve players remain owned but do not consume active roster capacity or start in the first remaining week; future activation remains unknown rather than projecting today's reserve placement through the season. Trade evaluation fills trade-created vacancies back to the team's pre-trade active size from the bounded weekly waiver pool. No-drop mode forbids removals but still permits those additions; a candidate is rejected if its vacancy cannot be filled. When drops are enabled, each team's drops are optimized locally. In a multi-team agreement, scarce free-agent replacements are reserved in ascending team-ID order from the players still available; this deterministic policy is disclosed in the app, result payload, and workbook. Season simulation applies deterministic two-decimal settlement, captured home-team adjustments, standings, divisions, playoff qualification, and the supported tiebreak order. Player scores are unbounded unless an explicit numeric floor is persisted in the scenario configuration. Exact platform rounding and forecast-vs-actual uncertainty calibration remain explicit data-contract limitations; playoff odds are model estimates, not calibrated probabilities.

ESPN scans stop at ingestion with the affected player name and ID when a
rostered player has `proTeamId=0`.
That unassigned-player state has no trustworthy NFL schedule, so the app does not
silently convert it into a season-long zero projection.

See the [feature data-contract audit](docs/data-contract-audit.md), [weekly source collection](docs/source-collection.md), [methodology notes](docs/model-notes.md), and [desktop packaging](docs/packaging.md) for the validation contract, known data limits, acquisition plan, and supported release targets.

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
