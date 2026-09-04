# Draft Lab

Draft Lab is the local workspace for training, checking, and using a fantasy-football draft model. It is separate from Trade Lab: it works from historical preseason data that you import, runs historical snake drafts and seasons on this computer, and turns the resulting model into a persistent manual or public-ESPN draft-day assistant.

## The five-step workflow

1. **Install or import honest data.** Use **Install 2015–2019 and 2021–2025 starter corpus** for the built-in public pack, or import your own strict historical corpus. You may also import a previously exported model and a current preseason board.
2. **Configure the arena.** Start from the built-in PPR preset or a league preset created from one of your synced league bundles, then edit the teams, roster, scoring, schedule, playoff, and strategy settings.
3. **Estimate and train.** Choose the historical years and training budget, inspect the local-work estimate, and start the background job. Every completed generation is autosaved.
4. **Inspect and benchmark.** Review the captured showcase roster, weekly results, standings, and playoff bracket, then run paired scenarios against the non-neural regression baseline.
5. **Use the draft assistant.** Choose a model, current board, draft slot, and strategy. Record picks manually, or connect a public ESPN snake draft for on-demand or 15-second automatic refresh; recommendations appear when your slot is on the clock.

## Historical years and the no-leak rule

Historical training accepts only **2015–2019 and 2021–2025**. The 2020 season is intentionally excluded. A 2015 pack is valid only when it contains a genuine 2015 preseason snapshot—not rankings reconstructed after games were played.

Draft Lab does not include or claim access to complete historical ESPN, Yahoo, or FantasyPros projection archives. Its optional starter-corpus installer uses only public Fantasy Football Calculator 12-team PPR ADP snapshots and CC-BY-4.0 nflverse schedules, weekly rosters, and weekly statistics. From those inputs it builds a transparent preseason baseline by carrying each player's preceding regular-season per-game counting stats over the target season's scheduled game count. The result includes projected PPR points, projected points per game, projected games, and detailed projected counting stats alongside ADP. You may instead import data that you obtained lawfully and whose provenance you can describe.

### One-click starter corpus

The starter installer downloads one bounded file at a time over an HTTPS host allowlist. nflverse release sizes and SHA-256 digests are pinned when published; every downloaded file also receives a local SHA-256 receipt. Interrupted downloads remain as `.part` files and resume with a validated byte range. A completed manifest, transform version, and identical file hashes reuse the existing content-addressed corpus without rebuilding it.

The installer never invents an ESPN, Yahoo, or FantasyPros historical projection. Its local carry-forward baseline is labeled separately and cannot inspect the target season: no prior-season appearances means the point and detailed-stat projection fields remain explicitly missing. Fantasy Football Calculator players join to the nflverse Week 1 roster only through a unique normalized name + position + NFL-team match; no fuzzy identity match is accepted. nflverse schedules supply kickoff dates and byes, prior-season rosters supply team-tenure evidence, and target-season weekly statistics remain outcome-only fields. Because Draft Lab v1 models one fixed bye per player-season, a player is excluded when nflverse records actual play during the preseason team's bye, as can happen after an in-season team change.

The builder is starter transform version 4. Version 3 fixed a scoring error in earlier starter corpora: team offense was incorrectly retained in DST outcomes and prior-season projections. Version 4 adds conservatively timed weekly availability evidence for roster-aware simulation. Rebuild older starter corpora and retrain their models to evaluate this policy; do not use pre-version-3 starter models for recommendations. Existing files remain readable for inspection. The installer reuses already downloaded nflverse files after verifying their pinned size and SHA-256, so rebuilding does not require downloading the historical statistics again. DST records now contain only canonical defensive scoring fields, and fumble recoveries use opponent-fumble recoveries rather than fumbles committed by the defense.

The receipt shows source URLs, licenses, byte sizes, SHA-256 hashes, exact-match coverage, exclusions, and limited samples for every gap. **Ready with gaps** is usable and means at least one source row could not be joined without guessing. **Incompatible** means a source, schema, checksum, season, or minimum draft-pool invariant failed closed. Installation and corpus construction remain sequential to minimize memory and CPU pressure.

Starter DST scoring has a specific source limitation: nflverse team totals do not identify which unit scored a fumble-recovery touchdown. These touchdowns are retained as `dst_unclassified_recovery_touchdowns`, counted in coverage gaps, and excluded from default DST points rather than awarding offensive recovery touchdowns to the defense. Points-allowed buckets use the opponent's final game score, not host-specific exclusions for return touchdowns. Thus the starter's DST outcome points are a disclosed approximation, not an exact ESPN/Yahoo scoring replica. The [upstream stat definitions](https://github.com/nflverse/nflfastR/blob/master/R/calculate_stats.R) document the underlying counters.

Each historical season keeps two kinds of data separate:

- `preseason_features` are observations captured during that same calendar season before kickoff. The recorded `preseason_as_of` timestamp must include a timezone and precede `season_kickoff_at`; a stale prior-year snapshot is rejected.
- `actual_weeks` are realized weekly outcomes used only after a draft choice has been made. During a historical season, lineup selection can use the preseason view and outcomes from earlier weeks, never the current week's result or a future result.

Player names, player IDs, NFL-team IDs, and similar identity fields are metadata, not model inputs. Feature-policy v1 is opt-in: a model input must explicitly be a projection, rank, ECR, or ADP field, or a `projected_stat.<stat-name>` field. FantasyPros, ESPN, Yahoo, and ensemble copies may use their matching namespace. Identity fields and names suggesting actual, final, future, postseason, or other realized outcomes fail at import. This is stricter than guessing from a blacklist and prevents the model from memorizing identities or hindsight.

For simulated lineup selection, `projected_fantasy_points`, `projected_points`, and `projected_stat.*` values are season totals. Draft Lab converts them to weekly estimates using the first finite positive value resolved from `projected_games` and then `projected_games_played`. Each field retains the normal source precedence: ensemble, generic, then the mean of available FantasyPros, ESPN, and Yahoo copies. If neither games field has a usable positive value, the divisor is the configured number of regular-season weeks. Rank, ECR, and ADP fallbacks are not rescaled.

## JSON import contract

Draft Lab accepts three strict, versioned JSON document types. An import must be one JSON object with exactly the documented fields. Duplicate keys, `NaN`, infinity, unknown fields, malformed types, and altered content-addressed IDs are rejected.

### Historical corpus

The root record has:

| Field | Required value |
| --- | --- |
| `kind` | `historical_draft_corpus` |
| `schema_version` | `2` when availability reports are included; legacy `1` without them remains readable |
| `corpus_id` | `draft_corpus_` followed by the record's computed 64-character SHA-256 content ID |
| `preseason_feature_policy_version` | `1` |
| `seasons` | One or more historical-season records, with no duplicate year |
| `provenance` | One or more source records |

Each season contains `season`, `preseason_as_of`, `season_kickoff_at`, `available_weeks`, and `players`. Every player contains:

- `player_id`, `display_name`, primary `position`, and `eligible_positions`;
- `nfl_team_id`, `bye_week`, `nfl_experience_years`, `rookie`, and `first_year_on_team`;
- numeric-or-null `preseason_features`; and
- one `actual_weeks` row for every listed available week.

An actual-week row contains `week`, `status`, and `stats`. Status is `played`, `inactive`, `bye`, or `missing`. A played row requires numeric statistics; every other status requires an empty stats object. A configured training or playoff week cannot remain `missing`. Every provenance row contains `source`, timezone-aware retrieval `captured_at`, `scope`, `preseason_feature_names`, `preseason_source_as_of`, `license`, and `source_url`; the final two fields may be `null`, but their keys must still be present. Each preseason feature name must be bound to exactly one provenance row, and `preseason_source_as_of` must give a pre-kickoff source timestamp for every season carrying those fields. Missing, extra, duplicated, post-kickoff, or source-after-retrieval contradictions fail closed.

Schema-version-2 seasons can also carry `availability_reports`. Each report contains `player_id`, effective `week`, `status` (`active`, `out`, `ir`, or `season_ending_ir`), `source_week`, and explicit `source`. The source week must precede the effective week. These reports are simulation-only observations, not preseason neural inputs; actual-week outcomes never substitute for an availability report.

### Current player board

The root record has `kind: "draft_player_board"`, `board_id`, `season`, `preseason_as_of`, `season_kickoff_at`, `preseason_feature_policy_version: 1`, and `players`. Player records use the same preseason metadata and features as a corpus, but `actual_weeks` must be an empty array. An unmapped board uses schema version 1. A board with a one-to-one `espn_player_ids` map uses schema version 2 and can reconcile a public ESPN draft. Any board used with a model must have enough unique players to complete every roster and expose every preseason feature family required by that model.

### Trained model

A model exported by Draft Lab is a `fantasy_draft_model` schema-version-2 record. It includes the learned brain, compatible league configuration, source corpus ID, training years and generation, evaluation policy, summary metrics, and creation time. Draft Lab saves that portable JSON locally before presenting it. Use **Download trained model** for a browser-managed copy, or **Show saved model** to reveal the original in Explorer, Finder, or the platform file manager when browser policy blocks downloads. Preserve the JSON unchanged; importing it later verifies all nested content IDs before it becomes selectable.

Content IDs are integrity checks, not labels to invent. A placeholder such as `draft_corpus_<hash>` will not import. A corpus or board producer must serialize the matching versioned record and compute its canonical ID with the Draft Lab domain library; normal users should retain the ID supplied with a valid data pack or exported file. Browser imports are capped at 128 MB to keep parsing memory bounded; split or reduce wider source packs before importing.

## League rules and strategy seats

The built-in preset is a 12-team PPR snake draft. A synced league bundle can supply an editable starting structure only when its scoring capture exposes supported linear stat weights. Unsupported bundles remain visible but disabled with a reason; Draft Lab never guesses that a setting such as “yards per point” is a direct stat multiplier. All usable presets remain editable:

- 2–32 teams;
- ordered starting slots plus bench size;
- explicit eligible positions for every starting-slot type;
- optional per-position roster limits;
- numeric scoring weights applied to the imported raw weekly statistics;
- regular-season weeks, playoff-team count, and playoff weeks; and
- a strategy assignment for every seat.

Strategy-seat counts must add up exactly to the team count. **Balanced / none** imposes no positional delay. **Streaming QB** and **Streaming TE** defer that position to the final three rounds, **Streaming DST** defers defense to the final round, and **Late-round QB** defers quarterback until after round 9. These are hard draft constraints, not descriptive labels; an incompatible combination of roster rules, player supply, and strategies can leave a team without a legal pick.

Historical simulation requires one playoff week per single-elimination round. For example, a six-team playoff needs three playoff weeks. Regular-season matchups use a deterministic round-robin schedule generated for the configured team count and weeks; they do not recreate a historical home league's actual schedule. Synced divisions, division-winner berths, host tiebreakers, reseeding, and multiweek matchups are not represented, so **Copy supported league structure** is deliberately not labeled an exact clone.

## Training, autosave, resume, and export

Choose **Estimate local work** before training. The estimate reports simulated leagues, brain-seat season evaluations, neural scores, learned parameters, approximate autosave size, and approximate population memory. It is a work-unit estimate rather than a clock estimate; after the first completed generation, the progress view uses measured local runtime to show an ETA. The same corpus, league configuration, evolution settings, and seed produce deterministic results.

The main work controls are:

- **Population:** how many competing draft brains are evaluated per generation.
- **Generations:** how many rounds of selection and mutation run.
- **Full seat sweeps / generation:** how many times each brain is evaluated from every draft position in every selected season.
- **Training years:** which imported seasons supply the arenas.
- **Candidate window:** the maximum preseason-ranked shortlist scored by the neural model at each pick. `0` scores every legal candidate; a smaller positive window reduces work while retaining leading candidates from each available position.
- **Elite fraction:** the leading share copied into the next generation before crossover.
- **Mutation rate and magnitude:** how often offspring parameters change and how far they may move. The UI starts at the calibrated `25,000` magnitude; lower values explore more cautiously.
- **Seed:** the repeatable assignment, tie-break, and evolution stream.

Population, generations, full seat sweeps, league size, training years, and the candidate window multiply the arena work quickly. Within each sweep, every brain drafts once from every seat in every selected historical season; its fitness is averaged across that complete set, so no brain is rewarded merely for drawing a preferred position. Every draft uses snake order: the seat order reverses on each round. Each cohort sweep uses one same-seed all-regression control league whose seat-indexed results provide the comparison for all candidate rotations. Reusing that control preserves the same-seat comparison while reducing a sweep from `2 × team_count` leagues to `team_count + 1`. Selection fitness is the drafter's improvement over its control seat, so draft-slot and deterministic tie-break luck are not mistaken for learning. Reported championship, playoff, and finish totals remain the evolved drafter's raw outcomes, and fitness may be negative. Begin with one small generation, inspect its measured runtime and results, and scale only after the data and league rules behave as expected. Draft Lab runs one training or benchmark job at a time so two heavy jobs cannot silently compete for the machine.

At the end of each completed generation, Draft Lab atomically saves a compact checkpoint: the shared feature schema and regression baseline are stored once, followed by learned genomes. If you request a stop during a generation, that partial generation is discarded and the latest completed checkpoint remains usable. The screen lists autosaves. You may continue the original target, increase the generation target, or choose **Use autosaved champion** to turn the checkpoint winner into a portable model immediately. Extending a one-generation trial is deterministic and matches an uninterrupted run. A changed corpus, league, population, strategy mix, seed, mutation setting, full-seat-sweep count, or candidate window is rejected. Normally completed training also saves its final champion model automatically. Re-promoting the same checkpoint reuses an existing matching model instead of filling the catalog with duplicates. Checkpoints and models created by the former sampled-seat evaluator remain on disk but are labeled incompatible and cannot be resumed or used as if they had passed full draft-position evaluation; retrain them under the current policy.

## Inspecting and testing a model

The **Last batch** view shows fitness by generation and the best single-arena showcase captured in the final generation. That showcase is not guaranteed to belong to the saved final-generation champion. Select any showcase team to compare its final and originally drafted rosters, inspect injury roster activity, select a week's starting lineup, and review results, final standings, and the single-elimination bracket. This is an audit trail and an example—not evidence by itself that the model generalized.

### Simulated injury roster management

Before each simulated week, the shared roster policy benches players with applicable out/IR reports, excludes players on their known bye, and chooses the highest-estimated legal starting lineup from the remaining roster. By default, it drops a player only on an explicit `season_ending_ir` report; a missing stat line, zero score, or absence from later games is not season-ending evidence. Open roster places are filled from the simulated league's unrostered player pool, respecting league roster limits. Claims use modeled reverse-standings priority, with at most one claim per team per pass. Lineup and replacement estimates begin with the preseason projected per-game score and blend in completed earlier games only; the selected week's actual points are displayed afterward and never help choose that week's starters. The starter corpus does not include archived ESPN/Yahoo/FantasyPros weekly forecasts or historical matchup projection shapes, so this is a leak-free local weekly estimate—not a claim that those provider projections were reconstructed. This is a common evaluation policy for all drafters, not a learned waiver strategy or an exact ESPN/Yahoo waiver-rule replica; no live-account roster is changed.

**Optional zero-point absence inference** adds three editable league rules: `zero_point_out_weeks`, `zero_point_ir_weeks`, and `zero_point_drop_weeks`. Each is disabled by default (`0`); enabled values must be 1–25 and strictly increase in that order. For example, 2 / 4 / 8 benches after two consecutive zero-point weeks, models IR after four, and releases the roster slot after eight. These are inferred absences, not medical diagnoses: a healthy backup can score zero. The inference uses only completed weeks for QB/RB/WR/TE, never DST/K; byes pause a streak and missing data resets it. Set the drop threshold to 0 to disable inferred drops, or all three to 0 to rely only on explicit status evidence. Saved configurations preserve their chosen thresholds, including disabled values.

The automatic starter corpus conservatively delays 2016-and-later weekly roster statuses by one week because these files do not establish pre-game publication times. The 2015 starter has no usable week-specific availability reports; its reported status remains unknown. Starter weekly statuses do not establish season-ending IR, so drops require an explicit report in an imported corpus unless optional zero-point drop inference is enabled. Unknown availability does not prove health. Review the displayed report count, policy, and each move's source; the activity table distinguishes benched players, drops, waiver additions, and unfilled vacancies. The selected-week lineup shows the estimate used at lock and the realized points separately. Existing showcases without these fields remain inspectable, but new roster-aware evaluation requires rebuilt data and fresh training.

Use **Run 100 paired scenarios** for the stronger check. Each scenario compares the evolved model with its zero-neural regression baseline under the same evaluation season, focal draft slot, opponent policies, and seed. Season and seat selection is stratified: every season×seat cell is visited before one repeats. Results report wins/ties/losses, point and point-percentile changes, finish improvement, playoff and championship-rate changes, and a season-clustered 95% interval for the mean point-percentile change. The verdict is `improved`, `worse`, or `inconclusive` from that interval.

When possible, benchmark on imported years that were not used for training. Draft Lab allows overlapping years, so selecting a genuine holdout remains the user's responsibility. Paired historical performance reduces comparison noise, but it is not a guarantee of future-season results.

## Draft assistant

The assistant requires a compatible trained model and a current preseason board. The board must contain at least one finite, recognized model input for enough players to fill every roster; field names with only `null` values do not count as usable coverage. Select your Drafter number and optional strategy, then record every league pick in the displayed snake order. The app rejects skipped turns, duplicate players, and picks assigned to the wrong drafter. **Undo last pick** safely corrects the most recent entry.

When it is your turn, Draft Lab ranks legal available players and shows an overall utility plus a short roster-need explanation. The saved result also retains the regression and learned-neural components for programmatic inspection. On other teams' turns the screen waits for the next manual entry. Each recorded or undone pick is saved to the local assistant session.

For a public ESPN snake draft, enter its numeric league ID and matching season, then choose **Sync ESPN public draft** or enable 15-second automatic refresh. The first successful sync permanently binds that saved room to the provider, league, season, and ESPN team order; a different source requires a new room. The adapter reads one credential-free public draft response per poll, verifies an ordinary fixed-order snake draft, maps every observed ESPN player ID through the board, and appends only exact contiguous picks not already saved. Repeated polls are idempotent. It refuses auctions, keepers, traded or ambiguous pick orders, missing mappings, source conflicts, and automatic rollback. Private ESPN leagues and Yahoo live drafts remain manual; the app never asks for draft-site cookies or credentials.

## Local files and privacy

Corpus, source downloads and receipts, board, checkpoint, model, and assistant-session files stay in the application's local data directory. Draft Lab performs corpus transformation, training, simulation, benchmarking, ranking, and persistence locally; it makes no per-pick or per-simulation web request. Network access occurs only while you explicitly install the starter corpus or use the optional public ESPN draft poll described above. The browser interface talks to the app through its private loopback session.

An exported model contains its feature schema, learned parameters, league rules, corpus content ID, training years, aggregate metrics, and evaluation-policy version. Current model schema version 2 proves that the champion was selected under full snake-draft seat sweeps. Earlier schema-version-1 models remain on disk for provenance but are cataloged as incompatible and cannot be used by the benchmark, draft assistant, or export path until retrained. Models do not embed historical corpus rows, weekly outcomes, player names, or player IDs. Corpus and current-board JSON remain separate local files unless you choose to copy them elsewhere.

## Current limitations

- Custom historical data packs and current player boards must be supplied as strict JSON. The built-in starter corpus contains FFC ADP and a disclosed prior-season nflverse projection baseline, not archived provider projections.
- There is no supported claim of complete ESPN, Yahoo, or FantasyPros historical projection archives.
- Drafts are fixed-order snake drafts without pick trading, auctions, keepers, or third-round reversal.
- Historical seasons use generated round-robin matchups and a disclosed injury-replacement waiver policy; they do not simulate trades, FAAB bidding, exact host waiver calendars, or individual managers' roster decisions.
- Weekly scoring is only as complete and accurate as the imported raw stats and configured scoring weights.
- The learned model is tied to its saved league configuration and the feature families available in its training corpus and current board.
- Public live synchronization currently supports only ESPN fixed-order snake drafts with a fully mapped board; private leagues, Yahoo drafts, auctions, keepers, offline drafts, and traded-pick orders require manual entry.
