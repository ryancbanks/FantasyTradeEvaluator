# Fantasy Trade Evaluator

A local NFL trade-search app that uses current FantasyPros ECR, FantasyPros/ESPN/Yahoo projections, the complete host-league schedule, and an empirically verified reproduction of FantasyPros roster power. Weekly collection uses the signed-in pages once; candidate generation, power filtering, roster adjustment, record projection, playoff simulation, ranking, and Excel export all run on the computer afterward.

## What the app does

- Captures the current FantasyPros consensus ECR and projection tables rather than freezing one expert list in code.
- Discovers the league size, every team, every rostered player, roster capacity, and IR/reserve state automatically from the connected league.
- Keeps any practical number of season-scoped league workspaces, each with its own saved ESPN/Yahoo connection, selected team, weekly history, and isolated methodology data. Old leagues can be archived without deleting them.
- Captures ESPN and Yahoo projections and forms a weekly player-level ensemble.
- Imports every league roster, active/IR capacity, standings, scoring profile, completed matchup, remaining matchup, playoff rule, and tiebreak rule.
- Searches ordinary two-team trades or exhaustive three-team agreements across two selected partners. In a three-team search, every moved player may go to either other team and all three teams must give and receive at least one player.
- Applies the chosen package sizes, side imbalance, equal-size-only, and no-forced-drop rules independently to every team in an agreement.
- Independently filters what you give and receive by exact players or canonical positions. Add as many rules as needed, invert any rule with NOT, and combine them from top to bottom with AND, OR, or XOR (exactly one).
- Computes every team's expected final record, projected finish, and playoff probability from shared correlated season scenarios.
- Shows measured progress and an evidence-labeled time-remaining estimate for local work. Exact combination progress comes from work-unit counts; shorter operations stay visibly indeterminate and learn timing ranges from successful app runs in the same browser, even when the local port changes.
- Includes a results workbench that can require every participant to improve, set a minimum playoff gain for every team, and sort by playoff gain, power gain, or package size before writing the full result plus league outlook to a real `.xlsx` workbook.

FantasyPros power and fantasy-point projection are deliberately separate. Only FantasyPros evidence is used to reproduce FantasyPros power. ESPN and Yahoo affect the weekly point distributions, projected records, and playoff probabilities; they do not alter the reproduced FantasyPros power formula.

## Use an installed desktop build

1. Install the build for the machine: Windows `Setup.exe`, macOS `.dmg`, or the Debian/Ubuntu archive. Python and developer tools are not required; use a current desktop Chrome or Edge browser for weekly collection.
2. Open **Fantasy Trade Evaluator**.
3. The first time only, choose **Download extension**, extract the ZIP, open `chrome://extensions` (or `edge://extensions`), turn on **Developer mode**, choose **Load unpacked**, and select the extracted folder containing `manifest.json`.
4. Return to the app and refresh that page once so Chrome injects the newly loaded extension. Choose **Connect extension**, open **Fantasy Trade Evaluator Browser Bridge** from the browser's Extensions menu, verify the four-character code, and choose **Pair with app**. Pairing uses a one-time code and never exports browser cookies. Sign in to FantasyPros, ESPN, and Yahoo normally in that same browser before starting a scan; sessions from a different browser profile cannot be copied.
5. Under **Weekly data**, choose **Add league**. Give the workspace a recognizable name and save its season, scoring format, ESPN league link, and Yahoo league link. Paste League Home—or any other page inside the matching ESPN Fantasy Football league. For Yahoo, league home, team, **Players**, and **Player List** pages are accepted, including filtered, `/playersearch`, and season-prefixed addresses. The app retains only the numeric league identifiers.
6. Add as many league workspaces as needed. The selector keeps each league's weeks and preferred **Your team** choice separate. Archive an old league to hide it from the normal list without deleting its data; enable **Show archived leagues** to restore it. Portable weeks imported before they are assigned remain under **Unassigned imports** and can be attached to the right workspace later.
7. Select the league, enter its first unplayed week, and choose **Scan selected league & collect**. The league size and rosters are detected automatically. The extension reuses one temporary tab in the browser where you are already signed in. If a provider still needs sign-in, complete it in that tab and continue from the local app.
8. Select the ready week and choose **Two-team** or **Three-team**. Two-team mode can search specific opponents or all of them. Three-team mode requires two distinct partners and considers every valid original-owner-to-final-owner player route among those three teams—not only circular swaps. The package limits, imbalance, equal-size, and no-drop rules apply to each team; the optional content filters apply to the aggregate players your team gives and receives. Within a content rule, its player and position conditions both apply. Each additional rule is combined with the accumulated result above it using AND, OR, or XOR; NOT inverts its individual rule. Three-team power is always outside the attested two-team trade shapes, so exact-engine results are labeled `extrapolated` and surrogate-engine results are labeled `surrogate_extrapolated` before the run begins.
9. Choose **Count combinations**. The count is exact, including values larger than the browser's ordinary integer range, and does not construct every agreement in memory. A three-team count is required before its search can start because even small package limits can produce millions or billions of routes.
10. Choose **Start local search**. The app checkpoints the search so an identical interrupted run can resume directly at its next decision-tree position. Progress uses exact examined/total counts once enumeration begins. Time remaining is withheld until enough real throughput has been observed; early and one-shot estimates are clearly identified as historical estimates from this computer.
11. Use **Trade result workbench** to focus the loaded preview: require every team to gain playoff probability, require a minimum gain for every participant, or change the ranking. These controls do not discard qualified trades from the saved search or workbook.
12. Choose **Download Excel workbook** for all qualified trades and the full projected league table. Three-team results include every directed transfer leg, each participant's roster/power/playoff impact, and the complete request/run provenance. To keep local export memory bounded, narrow a three-team search to at most 10,000 qualified trades before exporting.

On Windows, remove the installed app from **Settings > Apps > Installed apps**, the
**Uninstall Fantasy Trade Evaluator** Start-menu shortcut, or the standalone
`FantasyTradeEvaluator-...-Uninstall.exe` supplied with the release. Uninstalling
removes the application and its shortcuts but retains weekly league data so a
later reinstall can resume where you left off.

The first methodology calibration is intentionally bounded and separate from trade search. It fits coefficients on 250 controlled one-player ordinary-power observations, then tests 100 blind, representative multi-player packages. With a standard 14-player roster, those holdouts cover every nonleaking balanced size from 1-for-1 through 13-for-13, including 2-for-2, 3-for-3, and 4-for-4. The saved formula records the exact holdout IDs and covered sizes. Later weeks reuse it only after a fresh blind verification bound to the current FantasyPros page bundle and weekly evidence. A failed reuse check routes through fresh calibration. If that healthy fit still misses blind exactness, publication remains off by default; the user may explicitly opt into a separate **SURROGATE / APPROXIMATE** weekly engine. A surrogate never overwrites the reusable exact formula, never calls FantasyPros during local search, and can never label a power result exact. Observed balanced/no-adjustment shapes are `surrogate`; imbalanced, add/drop, or unobserved shapes are `surrogate_extrapolated`.

The safe default collects the current week plus each site's rest-of-season table, then allocates only the unpublished future weeks against the verified NFL schedule. The advanced future-week checkbox is intentionally off because Yahoo normally exposes only a short rolling window, not every remaining fantasy week.

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

A weekly bundle is immutable and content-addressed. It contains the exact scoring profile and all calculation inputs, including explicit missing/bye/not-published states. Loading fails closed on old or altered schemas, incomplete player/week/provider grids, identity ambiguity, changed analyzer contracts, insufficient formula holdouts, or scoring/league mismatches.

League names, provider identifiers, the saved **Your team** choice, archive state, and bundle-to-league assignments live in a local SQLite catalog beside the app data. The list is paginated rather than capped. Those private workspace details are not added to portable weekly bundles or Excel workbooks, and the immutable bundle and search-checkpoint schema versions are unchanged.

Search checkpoints and downloaded workbooks remain on disk, while only a small bounded set of recent completed previews stays in memory. Starting more searches releases older simulation baselines and previews so long app sessions cannot accumulate result sets in RAM.

Lineups are solved as exact assignments, including duplicate slots, FLEX, and superflex. IR and reserve players remain owned but do not consume active roster capacity. Trades that permit roster changes fill open active slots before choosing deterministic adds/drops. In a multi-team agreement, each team's drops are optimized locally and scarce free-agent replacements are reserved in ascending team-ID order from the players still available; this deterministic policy is disclosed in the app, result payload, and workbook whenever roster adjustments are enabled. Season simulation applies the captured platform's score rounding, home-team adjustment, standings, divisions, playoff qualification, and supported tiebreak order.

See [weekly source collection](docs/source-collection.md), [methodology notes](docs/model-notes.md), and [desktop packaging](docs/packaging.md) for the validation contract and supported release targets.

## Verify the source tree

```powershell
.build-venv\Scripts\python.exe -m unittest discover -s tests -v
```
