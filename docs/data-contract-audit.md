# Feature data-contract audit

This audit answers four questions for every calculation feature:

1. What data does the feature require?
2. Is that data collected or derived?
3. Does the feature receive the exact data it expects?
4. What is the safe acquisition and delivery path for anything missing?

The runtime version of this audit is returned as `data_readiness` in every
bundle summary and rendered in the app's **Data coverage and model limits**
panel. It reports coverage for the selected immutable weekly bundle; this
document records the broader design contract.

## Scope and version baseline

`EngineBundle` schema version 8 is the current calculation contract. Its player
universe is intentionally bounded to players owned in the captured league plus
the retained waiver-replacement pool. It is not a general player database, a
history database, or a draft-training corpus. A consumer may use a bundle only
after checking the bundle's own readiness; the existence of a file with a
plausible name is not evidence.

Several features are being developed in parallel. Their code is useful, but
their data models do not supersede version 8:

- the Draft Lab donor branch serializes an older, incompatible schema-version-6
  engine bundle;
- the public player-profile donor branch calls its expanded bundle schema
  version 10 and accepts legacy versions, but porting that bundle class would
  regress current version-8 ownership, source, benchmark, and methodology
  contracts; and
- the multi-league/progress work owns local workspace selection and operational
  timing, not calculation evidence.

Those features must be selectively integrated through sidecar contracts. If a
future calculation bundle genuinely needs a new reference to those sidecars,
the next bundle migration should use schema version 11, avoiding the unrelated
version-9/version-10 histories already present in donor work. Importers must
perform an explicit migration; they must never reinterpret an older record as a
newer schema because some field names happen to overlap.

## Status vocabulary

- **Ready** means the required data is present, identity-bound, validated, and
  consumed by the calculation.
- **Ready with limitations** means the feature is usable, but the result must
  disclose a named modeling limitation.
- **Model estimate with limitations** means the inputs needed by the local
  scenario model are present, but the reported playoff percentage is not a
  forecast-vs-actual calibrated probability.
- **Holdout validated** means representative blind trades of the same package
  shape matched the captured FantasyPros response within the publication
  tolerance. It is empirical validation, not exhaustive proof of every player
  combination with that shape.
- **Extrapolated** means the calculation is local and deterministic, but lies
  outside the package shapes covered by those representative blind holdouts.
- **Not ready** means the app must not label the result exact or silently invent
  the missing data.

## Core calculation consumer map

| Consumer | Required data and grain | Acquisition | Authoritative storage and provenance | Readiness, delivery, and safe degradation |
| --- | --- | --- | --- | --- |
| Weekly private-league scan and bundle publication | Exact league/season; canonical teams and players; ownership and capacity-exempt placements; standings; completed and remaining fantasy matchups; roster, scoring, playoff, and tiebreak settings; complete NFL week 1–18 schedule | ESPN credential-free JSON when available, with the signed-in extension as the private-league fallback; FantasyPros league pages provide an independent roster/team capture and benchmark; Yahoo Settings verifies reception format | Normalized host snapshot, `WeeklySourceManifest`, schedule artifact, identity registry, and projection manifest are content-addressed into `EngineBundle` v8. The private provider league ID remains only in the local binding catalog; the portable bundle carries a random opaque `league_binding_id` and sanitized evidence IDs/times | **Implemented and fail closed.** A missing team, ownership conflict, invalid eligibility, incomplete schedule, scoring mismatch, or source-dimension mismatch prevents publication. Optional history/profile/draft acquisition must not prevent an otherwise valid core bundle |
| Canonical identity and calculation-player universe | One unambiguous canonical player for every owned or bounded-waiver player; provider IDs; NFL team; legal positions/slots; ownership | Exact provider IDs and conservative crosswalks from captured league, projection, and ECR rows | Bundle-bound identity registry and eligibilities; unresolved/ambiguous rows remain diagnostics. Projection evidence is restricted to the exact owned-plus-waiver universe | **Implemented for the bounded universe.** No silent name-only match. Full public-catalog and draft identities must join through a separately versioned alias graph rather than expanding the bundle |
| Projection acquisition and source-state interpretation | Planned provider/period/position/scoring page; exact attempt outcome; capture artifact; published rows; page completeness; player-specific bye context | One bounded weekly pass attempts the planned FantasyPros, ESPN, and Yahoo pages; successful pages and typed failures are retained | `ProjectionSourceManifest` binds each row to provider, period, scoring basis, point basis, source artifact/digest, capture/publication time, and attempt outcome | **Implemented with base-format limitations.** `not_published` is valid only when a successfully captured artifact proves complete coverage for the player's declared scope and the player is absent. A failed, blocked, partial, or unproved page is `unavailable`; code must not manufacture per-player `not_published` rows |
| Projection ensemble and full ROS materialization | Canonical player/provider identity; weekly and ROS points; raw projected stats when disclosed; configured weights/quorum; schedule/byes; direct-versus-derived origin; status observations | Retained weekly/ROS provider rows and the complete NFL schedule; direct future rows are subtracted before allocating a ROS residual across missing scheduled weeks | `projection_evidence`, `EnsembleConfig`, and the materialized v8 grid. Every ensemble value can be traced back to its provider rows; Player Lab receives the same values used by simulation | **Implemented with limitations.** Two-of-three can survive a verified omission. Even residual allocation is disclosed as local shaping, not a provider matchup forecast. Missing raw components mean exact custom-host scoring is not ready |
| FantasyPros-style team power | Complete bounded rosters and eligibility; every feature explicitly named by the persisted formula; selected `Latest ECR` panels; formula/calibration evidence | Current weekly and ROS ECR pages, including per-position expert-panel selection and source update time; analyzer captures are used only for fit/holdout evidence | `StrengthFormula`, rebuilt `StrengthModel`, ECR snapshots, methodology attestation, and optional surrogate disclosure live in the bundle. The default reliable formula consumes FantasyPros ROS ECR, not an invented FantasyPros ROS projection | **Holdout validated only for the listed representative balanced/no-adjustment shapes.** Matching blind holdouts is empirical scope evidence, not the proprietary formula or universal exactness. Other shapes are `extrapolated`; a failed fit can publish only as an explicitly accepted `surrogate` |
| Two-team trade generation and filters | Canonical ownership; counterparties; package-size and imbalance bounds; required/included/excluded player and position predicates on either side; no-drop option; power threshold | Entire candidate space is generated locally from one immutable bundle and one validated request | Request plus bundle ID defines the run; filters are applied to canonical IDs/eligibility before expensive season simulation | **Implemented.** No web call is needed per trade. Unknown players, ownership conflicts, impossible filter combinations, or a missing target team fail before enumeration |
| Three-team trade generation | Same bounded player/roster inputs, three-way transfer ownership, package constraints, and deterministic roster adjustment | Local enumeration from the selected bundle | Run definition, transfer graph, candidate index, and bundle/model identities support deterministic resume | **Implemented locally; power results are extrapolated.** There is no three-team FantasyPros analyzer evidence. Local playoff deltas remain usable under the season-model limitations |
| Roster feasibility, no-drop, and vacancy fill | Active roster cap; capacity-exempt placements; legal slot eligibility; player availability for a drop; complete candidate pool for every required position | Host roster rules and current placements plus bounded FantasyPros-best/ECR waiver replacements | Bundle `WaiverPool` retains source order, required positions, minimum size, scoring-profile ID, and selection algorithm; post-trade adjustment records added/dropped players | **Implemented with bounded-pool disclosure.** No-drop forbids removal but still fills trade-created vacancies. A candidate that cannot be repaired from the bounded pool is rejected; the app never calls the pool the host's complete free-agent list |
| Trade legality at the host | Trade deadline/window; roster locks; player game locks; undroppable/tradeable flags; pending claims/trades; keeper/pick rules where applicable | Must come from a timestamped host settings/status/activity capture at the analysis boundary | Future typed `HostTransactionPolicy` and per-asset status sidecar, bound to league, season, host snapshot, and observed time | **Not ready.** Current results are analytical roster scenarios, not guaranteed executable host transactions. Until captured, the UI/export must say legality unverified rather than infer permission from ownership |
| Weekly lineup scoring and season scenarios | Legal starting slots; scheduled opponent/week; ensemble distribution; bye; current reserve placement; scoring rules; persisted uncertainty/correlation/floor configuration | Bundle roster/settings/schedule plus locally materialized player-week grid | Prepared score scenarios are shared between baseline and every trade so before/after deltas reuse identical draws; configuration and random-stream identity are persisted | **Implemented with limitations.** Current reserve placement blocks first-week starting but is not projected indefinitely. Provider totals are not exact custom-host recomputations, and shared game/team residual correlations are not yet calibrated |
| Projected standings | Current wins/losses/ties, PF/PA and rank; completed results required by tiebreakers; every remaining matchup; simulated scores; typed settlement order | ESPN standings, settings, completed/remaining schedule, and local shared scenarios | Season result carries expected final record/PF/PA, rank distribution, and settlement limitation. Prerequisites gate downstream dashboard, playoff, and trade consumers | **Implemented for a reconstructed ESPN subset.** The captured fields do not prove every multi-team tie rule, score precision, or correction policy; unsupported or underspecified settlement fails closed instead of silently using a generic rule |
| Playoff qualification model | Projected standings plus qualifier count, divisions/berths, seeding/reseeding, tie policy, and regular-season boundary | Host playoff settings and local scenario ranking | The same scenario rows return before/after playoff deltas and complete seed distributions | **Model estimate with limitations.** It is not forecast-vs-actual calibrated; source disagreement, marginal uncertainty, and game/team correlation remain model assumptions |
| Championship view | Playoff-week player cells; exact bracket rounds/byes/reseeding; playoff tie/home-bonus rules | Full NFL schedule and ROS totals exist, but exact playoff-week cells and bracket settlement do not | A future postseason scenario dataset must share bundle, scoring, availability, and random-draw identities | **Not ready.** The existing field-conditioned power share is a clearly labeled proxy and must never be relabeled championship probability |
| Team dashboard | Bundle, prepared baseline, score scenarios, current/local projected outcomes, power, position totals, source benchmark and limitations | Pure local read of the selected bundle and scenario results | Dashboard schema v2 exposes local values, distributions, data readiness, reconstructed settlement policy, and side-by-side FantasyPros benchmark drift | **Implemented with inherited limitations.** FantasyPros values are comparison-only and never calculation input |
| Excel export | Qualified trade rows; both/all-three team outlooks; run/search definition; model/scenario/source/readiness evidence | Pure local export after search | Workbook freezes the result rows, 14-field Team Outlook contract, Run Details, source IDs/times, opaque league binding, projection coverage, and all named limitations | **Implemented.** Export must use the exact selected bundle and search run; if readiness is absent or mismatched, export is blocked rather than producing an unattributed workbook |
| Player Lab for calculation players | Weekly fused values; full-NFL-ROS values; individual provider rows/raw stats; direct/derived origin; status observations; ECR; ownership/eligibility; waiver provenance | Pure local read of v8 | Player Lab schema v5 reconciles every displayed provider cell to bundle evidence, separates full ROS from fantasy-regular-season totals, and lists expected/reporting/unknown status providers | **Implemented with limitations.** Blank status is unknown, not healthy; provider labels are observations, not calibrated appearance probabilities; the view covers the bounded calculation universe, not every NFL player |
| FantasyPros dashboard benchmark | Captured source current/projected rank and record, playoff/title values, exact team map, artifact/capture identity | FantasyPros projected-standings capture once per weekly refresh | `FantasyProsLeagueBenchmark` is bound to host snapshot/team IDs and displayed as local-minus-source drift | **Implemented as diagnostic evidence, but still mandatory in v8.** It never changes local calculations, yet current bundle construction requires exact coverage. A future schema should move it to an optional bundle-bound sidecar so its failure disables comparison rather than core publication |

## Auxiliary and concurrent-feature consumer map

| Consumer | Required data and grain | Acquisition | Authoritative storage and provenance | Readiness, delivery, and safe degradation |
| --- | --- | --- | --- | --- |
| Multiple-league workspace | Stable user-visible league profile; provider coordinates; opaque binding; exact season/scoring identity; bundle associations; selected team; archive state | User adds a league once; weekly scans update associations. Private provider coordinates remain local | Local SQLite catalog owns aliases/URLs and paginated profiles; portable v8 bundles own their random `league_binding_id`. A mapping table must make those identities one-to-one | **Concurrent implementation needs an identity join before release.** Its separately generated profile ID cannot become a second league identity. Any bundle/history/cache/job lookup must include the resolved binding and season; an unassigned bundle remains isolated, not guessed into a same-size league |
| Compute progress, cancellation, and ETA | Operation ID; resolved league/bundle/request identity; phase; exact completed/total work units; active versus paused time; cancellation state; observed phase throughput | Produced locally by collection/search/training workers; user-auth waits explicitly pause active time | Operational job store plus bounded persisted throughput observations keyed by operation kind, phase, implementation version, and coarse workload class | **Operational data, never calculation evidence.** Concurrent code provides current-run phase units and sample-based ETA; wiring every compute path and persisting cross-run observations remain integration gates. ETA is withheld without enough comparable samples, resets at phase changes, and excludes sign-in pauses. Missing/corrupt timing history removes ETA but cannot block or alter results |
| League activity/history ingestion | Opaque bundle league binding; season; host snapshot; canonical teams/assets; roster/lineup captures; complete transaction attempts and coverage; separate proposal/process/completion/observation times | Optional ESPN activity capture during refresh, with the exact endpoint, current 1,000-transaction request cap, and normalized/skipped-event accounting | Append-only local history database. Every capture records history capture ID, host snapshot ID/time, canonical roster ownership digest, coverage interval, completeness booleans, returned count/cap, acquisition policy/version, attempt outcome/reason, earliest/latest source time, and skipped-reason counts | **Sidecar only.** Hitting the 1,000-row cap means transaction history is incomplete. The store must use the bundle's random opaque binding, never a deterministic hash of a private league ID. Database/schema/acquisition failure cannot block core bundle publication. Reads are `as_of` the selected bundle so a prior analysis cannot see future captures |
| GM insights: activity, deal access, acquisition, roster/lineup style, compatibility, and proposal guidance | As-of trades/adds/drops/waivers; separate event times; moved assets and bids; complete observation window; roster and lineup snapshots; current roster needs/compatibility; at-time/current valuations; evidence IDs | History sidecar plus current v8 bundle, with no new per-view web read | Versioned GM result references bundle, history revision/capture IDs, analysis-as-of boundary, coverage for each subsection, and the exact transaction/roster evidence behind a claim | **Descriptive per subsection.** Rates require a proven trading-window start/end; otherwise show counts with partial-window disclosure. Missing lineup snapshots disables lineup style without hiding trade counts; missing valuation disables value/hindsight sections. Sparse associations must not become manager acceptance probabilities or automated personalization. Team/manager name similarity is insufficient to bridge seasons |
| Historical trade at-time valuation | Verified trade assets and proposal/processing timestamp; nearest prior bundle; at-time rosters; scoring/formula/source/methodology comparability; scenario configuration | Join append-only history to a bundle captured at or before the event and within a declared lag | Result retains source bundle/capture, valuation lag, methodology status, scenario count, per-team power/playoff delta, history revision, and evidence IDs | **Conditional.** No prior comparable bundle, coverage gap, ambiguous timestamp, incomplete assets, or future-data risk makes that event unvalued with a typed reason; it must not fall back to today's rankings |
| Current revaluation of a historical trade | At-time valuation plus current bundle, canonical assets, current eligibility/roster feasibility, and comparable scoring/formula semantics | Local replay against the selected current bundle | Nested revaluation records current bundle ID/time, power-edge drift, methodology compatibility, and explicit foresight-eligibility reasons | **Conditional and separate from hindsight.** A changed scoring profile, formula semantics, source basis, role model, or unresolved player makes comparison ineligible rather than silently comparable |
| Trade-timing recommendations | Current bundle and schedule; as-of history; current injuries/roles; team trajectories; historical willingness; legal trading window; locked/undroppable status; feasible roster adjustment; projection lineage | Local shortlist/search plus league-history sidecar; legal status requires the host policy/status capture described above | Timing result references selected bundle, history revision, scenario count/config, projection/formula identities, skipped-candidate reasons, evidence IDs, and trigger/window definitions | **Model guidance with explicit gates.** The current automatic preview is a bounded 1-for-1 shortlist, not an exhaustive package-timing search. History absence can degrade to current-data-only opportunities; health-unmatched history cannot personalize acceptance; unknown host legality changes the label to analytical/non-executable. One infeasible candidate is skipped with a reason and does not fail the endpoint. A source-sidecar failure cannot affect core search |
| Public all-player catalog/profile | Stable public alias graph; NFL player/team identity; bio/depth/status; weekly actual raw stats; injury/practice events; trends; exact source attempts and licenses | Bounded nflverse datasets for weekly stats/injury reports, Sleeper player/trending endpoints, and the DynastyProcess source-published ID crosswalk; only lawful configured sources | Separate content-addressed `PlayerProfileSnapshot`/catalog sidecar joined by canonical ID. `AuxiliarySourceManifest` records every dataset attempt, digest, scope/completeness, time, parser version, attribution/license, and absence reason | **Selective port required.** The donor profile functionality must not bring its bundle-v10 class into v8. Missing profile rows show unavailable sections without changing Player Lab's calculation cells. Injury burden and add/drop trends remain descriptive, not projection votes or appearance probabilities |
| Stable public alias graph | Namespaced external player/team IDs, effective season/time, source-published edges, canonical target, ambiguity/collision state | Exact crosswalk fields from FantasyPros/provider links plus public ID tables; manual review for unresolved collisions | One versioned graph sidecar with content ID; edges carry source artifact/digest and observed time. League, profile, projection, history, and draft adapters consume the same graph version | **Required before full-catalog integration.** No silent display-name match, no last-writer-wins collision, and no feature-local duplicate crosswalk. Ambiguity disables that player row/action only unless the player is in the core calculation universe, where publication fails |
| Independent/public projection collection | Exact provider/period/position/scoring plan; provider rows/raw stats; source universe completeness; publication/capture time; usage/attribution terms | Bounded public-source tasks, including the currently developed CBS/FantasySharks paths and ESPN payload where supported; each optional publisher is isolated | Provider artifacts plus `AuxiliarySourceManifest`; only a provider whose full declared plan passes coverage validation contributes an ensemble vote. Calculation subsets are copied into a future bundle; full-catalog rows stay in a sidecar | **Best effort, never synthetic consensus.** A failed or partial publisher contributes nothing and remains `unavailable`; it cannot generate fabricated player-level `not_published`. Provider totals are base-format values until raw components prove exact host rescoring |
| FantasyPros-free engine publication | Host league state; complete lawful projection/ECR-or-rank evidence; independent waiver universe; an explicitly independent power formula and validation disclosure; no FantasyPros-only required fields | Reuse the host scan plus complete independent/public source plans; collect each source once and compute locally | A deliberate future v11 calculation contract with an independent source manifest, formula, waiver pool, and optional comparison sidecars; never serialize fake FantasyPros ECR/benchmark objects to satisfy v8 fields | **Not ready in v8.** Independent source/planning modules exist, but v8 still requires FantasyPros ECR, league source, and benchmark artifacts. Until a real migration exists, do not call an independent bundle FantasyPros-equivalent or promise post-subscription refresh |
| Full-catalog projection view | All public-catalog players, weekly cells, provider values/status, positions/teams, and provider coverage metadata | Reuse independently captured projection artifacts; no additional call per player | Separate compact `PlayerLabProjectionSnapshot` keyed to alias-graph/source-manifest versions, not embedded into the bounded v8 bundle | **Sidecar contract needed.** Missing players/providers remain explicitly unavailable. Only rows whose identities and artifacts are proven may be promoted into a future calculation bundle |
| Draft Lab historical training | A leakage-safe preseason universe for every training season; eligibility/team/bye; preseason rank/ECR/ADP/projection/raw-stat features as-of before kickoff; actual weekly raw outcomes after decisions; feature policy; league/scoring config; source rights | User-imported lawful historical packs; no claim that complete historical ESPN/Yahoo archives are available | Separate `PreseasonPlayerUniverse` per season plus `HistoricalCorpus`. Corpus provenance references auxiliary source manifests, source-as-of and capture times, feature ownership, actual-outcome sources, license/attribution, digests, completeness, and no-leak validation | **Donor logic is useful but its bundle-v6 type is incompatible.** Training must not read current v8 player projections as historical preseason evidence or infer missing actual stats as zero. A season/feature without pre-kickoff provenance is rejected, not backfilled with hindsight |
| Draft model training/benchmark/checkpoint | Valid corpus versions; exact feature schema/policy; supported league rules; strategy seats; deterministic seed; generations/appearances; paired baseline; holdout-year selection | Entirely local compute after import | Models/checkpoints retain corpus IDs, universe/source-manifest IDs, configuration, feature policy/schema, code/model version, seed, training years, generation, and paired benchmark metrics | **Can degrade only by stopping/withholding a model.** Progress uses operational work units/ETA. Autosaves at completed-generation boundaries; cancellation cannot publish a partial generation. Historical paired improvement is not evidence of future-season accuracy, especially without a true holdout year |
| Draft-day board and assistant | Current-season preseason player universe; ADP/ECR/projection features; exact board coverage; provider player-ID map; legal roster/position limits; pick order; observed picks | Lawful board import; optional public ESPN fixed-snake polling; private/unsupported drafts require manual picks unless a future signed-in extension adapter is proven | `DraftBoardSourceManifest` plus content-addressed board/session. Manifest records attempts, source-as-of/capture, digest, player/feature/position coverage, identity-graph version, license, provider/order format, and completeness. Pick observations carry source and observed time | **Conditional.** Board/model feature or player-supply mismatch blocks recommendations. Unsupported auction/keeper/traded-pick/private formats fall back to manual entry, never guessed reconciliation. The assistant must not use an incomplete board merely because rostered v8 players are present |

## Acquisition feasibility

| Feasibility class | Data | What the product can truthfully promise |
| --- | --- | --- |
| Available in the current weekly pass | Host teams/rosters/standings/settings/schedules; complete NFL schedule; current FantasyPros ECR/analyzer/benchmark evidence; planned provider projection tables; disclosed raw stat columns and status text | Capture once, validate, content-address, and calculate locally for the rest of that bundle's life. A source can still be temporarily unavailable, so the prior valid bundle and typed attempt reason must remain visible |
| Obtainable with a bounded optional host read | Completed transaction ledger, roster/lineup snapshots, trade window/deadline, processing settings, locks, and undroppable/tradeable flags when the host exposes them | Attempt once per refresh through the public or existing signed-in session path and retain a sanitized sidecar. The product cannot promise a field the host response does not expose |
| Obtainable from public datasets, subject to terms and changing schemas | Public identities, actual weekly stats, injury/practice reports, depth/status metadata, add/drop trends, and additional projection publishers | Use an explicit finite source catalog, retain attribution/license status and exact attempt evidence, and degrade each dataset independently. Availability today does not justify an undocumented forever guarantee |
| Accumulated locally over time | Forecast-versus-actual errors, calibrated appearance rates, correlation/loadings, manager activity windows, and historical timing response | Begin an append-only dataset now and improve estimates only after declared sample/holdout gates. The app cannot reconstruct forecasts that were never captured before kickoff |
| Lawful import required | Historical preseason projections/ranks/ADP and outcomes for Draft Lab, especially archives not published by current provider endpoints | Accept strict user-supplied packs with pre-kickoff timestamps, completeness, source, digest, and license/usage provenance. Do not claim the app can scrape a complete historical ESPN/Yahoo archive when none is proven |
| Not directly observable | The proprietary FantasyPros formula, future player availability, manager intent/acceptance probability, and future performance | Reproduce observed behavior only within declared blind-holdout tolerance and label extrapolation; treat availability/acceptance/performance as modeled uncertainty, never captured fact |

## Data that is present and now reaches its consumer

- The full NFL regular-season ROS horizon is retained, rather than truncating
  source totals at the fantasy regular-season boundary.
- Player-specific bye weeks are removed from ROS allocation, and directly
  published future-week values are subtracted before allocating the residual.
- Current rank, expected points for/against, rank probabilities, and seed
  probabilities now reach both the local app response and Excel export.
- Typed reserve placement reaches roster-cap and post-trade adjustment logic;
  it is not misused as a substitute for missing player-week availability.
- A rostered ESPN player with `proTeamId=0` is rejected at league ingestion
  with its name and source ID. The calculation never reaches schedule assembly
  with a generic `FA` team or invents a season-long zero/bye assumption.
- Starting slots contribute required collection positions even when the current
  rosters have no player at that position. Bench-only positions are still
  collected for owned players but do not create irrelevant waiver requirements.
  A generic IDP slot expands to the concrete DL/LB/DB player groups exposed by
  the league source; it does not create an impossible aggregate-IDP requirement.
- Missing raw stat fields remain unknown instead of becoming numeric zero.
- Provider injury/status labels remain sanitized timestamped observations with
  their weekly or ROS source scope. Player Lab shows each provider's wording,
  freshness, and cross-provider disagreement; readiness reports the same
  coverage. These labels never mutate `ProjectionStatus` or become a certain
  start/sit assumption.
- Every ECR position page retains its own exact selected expert panel. Merged
  snapshots keep those panels by position and expose only their deduplicated
  union as an aggregate; they never pretend that every position used one panel.
- Each ECR page must identify FantasyPros' `Latest ECR` group and prove the
  site's stated recent-update/accuracy policy. The exact selected expert IDs
  must match the ranking payload; a generic or stale consensus label is not
  accepted as equivalent.
- ECR artifacts retain league scoring separately from the source page's scoring
  label. Reception-sensitive RB/WR/TE pages follow STD/HALF/PPR, while the
  unprefixed QB/K/DST/DL/LB/DB pages are validated and retained as STD source
  pages without losing their PPR or half-PPR league context.
- FantasyPros' exact ECR update timestamp is retained when the page exposes its
  Unix timestamp. Ambiguous display text is preserved but never guessed into a
  date. Playwright and signed-in extension capture both retain the same optional
  scalar.
- Fused projection provider identities must match retained provider evidence;
  search and simulation eligibility must be identical.
- Provider weights, source quorum, uncertainty floors, the NFL schedule, and the
  strength formula are persisted and content-addressed. Loading a bundle
  rebuilds the strength model from those inputs rather than trusting a detached
  cached score table.
- The projection-source manifest retains page-attempt outcomes by provider and
  binds every normalized projection row to its source artifact. Runtime
  readiness, the local UI, and workbook Run Details expose source scoring
  formats, provider-total versus locally recomputed point basis, and
  base-format-only versus exact-host-rules compatibility. The custom-scoring
  caveat disappears only when every retained source proves local recomputation
  under the exact host rules.
- Host and FantasyPros league captures are bound through a portable source
  manifest. The runtime readiness report and workbooks expose the opaque league
  binding, sanitized content IDs, capture times, and bundle-wide capture window;
  the private provider league ID remains only in the local binding catalog.
- Bundle selectors and readiness summaries include a stable, shortened label
  derived from that opaque binding (for example, `ESPN workspace 1a2b3c4d5e6f`).
  Same-size leagues in the same season are therefore distinguishable without
  leaking either provider's private league ID.
- A schedule must contain the complete NFL regular-season week range through
  week 18 before collection can publish or a portable bundle can load. A
  truncated source can never masquerade as complete rest-of-season coverage.
- Every retained projection-evidence row must belong to the exact owned-plus-
  waiver calculation-player universe. Unresolved or unrelated rows stay in
  collection diagnostics and cannot skew readiness, provenance, or Player Lab.
- A missing optional provider horizon remains explicitly unavailable. The
  ensemble can still run when its configured quorum is met, while a power
  formula that directly consumes that provider makes it mandatory and blocks
  publication if its evidence is absent.
- Waiver candidates must pass both provider/quorum screening and the same
  schedule-aware weekly materializer used by rostered players. Every retained
  weekly row is also checked against the full NFL horizon, including rows after
  the fantasy calculation window. Evidence outside the calculation scope or an
  observed projection on a verified bye cannot admit a candidate or abort an
  otherwise valid refresh.

## Cross-cutting evidence contracts

### Auxiliary source manifest

Every sidecar collector should emit one content-addressed
`AuxiliarySourceManifest`; sidecars must not each invent a smaller definition of
"provenance." One manifest contains one ordered attempt row per planned
resource with at least:

- dataset kind and schema, provider, sanitized request identity, season, period,
  position/scoring scope, and relevant league binding;
- started, completed, captured, and source-published/modified timestamps as
  separate optional fields, all timezone-aware;
- exact outcome (`captured`, `not_published`, `unavailable`, `auth_required`,
  `cancelled`, or `rejected`) and a stable reason code rather than an exception
  string alone;
- artifact content digest and byte count only when bytes were actually captured,
  plus parser and collection-policy versions;
- expected/observed pages, rows, positions, weeks, and players; cursor/page cap;
  declared source universe; and the test that proved completeness; and
- source URL with secrets removed, required attribution, license/usage status,
  and local raw-artifact reference when retention is permitted.

`not_published` is a positive claim about a successfully observed, proven-
complete scope. It is not a synonym for timeout, parser failure, authentication
failure, missing license permission, or a row outside the requested universe.
If completeness is not proven, the source or scope is `unavailable`. This rule
applies equally to projections, public profiles, historical packs, and draft
boards.

### Identity and time boundaries

The stable public alias graph is the only cross-dataset identity authority.
Each node is a namespaced external ID or canonical player/team ID; each edge
records its source artifact, observed/effective time, and review state. A
collision or ambiguous component remains unresolved. Display names may help a
human review an edge but never create one automatically.

Every result also declares its temporal boundary. The following timestamps are
not interchangeable: source publication, proposal, host processing/acceptance,
observed completion, host capture, history capture, bundle publication, and
analysis `as_of`. Consumers choose the field whose semantics they require and
must not substitute a later observation for an earlier decision time.

### Custom scoring

A provider's displayed fantasy-point total is a source observation under that
provider's stated format. Exact host rescoring requires the provider's raw stat
components, a complete typed host scoring policy, and a versioned mapping from
source stat semantics to host stat IDs, including bonuses, thresholds,
fractional/negative rules, overrides, and defense/special-teams behavior. Missing
components remain unknown, never numeric zero. The result may say
`exact_host_rules` only when recomputation and reconciliation are proven for
every retained calculation row.

## Prioritized acquisition plan

| Priority | Missing or incomplete data | Best bounded acquisition path | Storage and consumer delivery | Safe behavior until available |
| --- | --- | --- | --- | --- |
| P0 | One identity for league profile, bundles, history, jobs, and caches | During league setup, resolve local provider coordinates through the existing private binding catalog and reuse the bundle's random opaque binding | One-to-one local profile-to-`league_binding_id` association, season scoped; every sidecar/result carries that binding | Keep an unassociated bundle in an explicit unassigned workspace. Never match by league size, name, PPR label, or a second deterministic/random ID |
| P0 | Reliable auxiliary acquisition evidence | Wrap each optional collector in the `AuxiliarySourceManifest` contract above and archive credential-screened typed artifacts locally where permitted | Sidecar snapshot refers to manifest ID; API/UI/export return summarized attempts and reason codes | A failed optional attempt disables or reduces only its dependent feature; it cannot roll back a valid v8 refresh |
| P0 | Proven negative publication state | Teach each adapter how it proves table/page universe completeness before deriving omissions | Omission row references the successful artifact, exact scope, and manifest attempt | Use `unavailable` for failed/partial/unproved pages. Never fabricate `not_published` merely to make a rectangular table |
| P0 | Host trade legality | Capture trade deadline/window, processing policy, roster/player locks, undroppable/tradeable flags, pending transactions, and applicable keeper/pick constraints with host snapshot time | Typed `HostTransactionPolicy` plus per-asset status sidecar, referenced by search/timing result | Label trades analytical and legality unverified; do not claim they can be submitted to the host |
| P0 | As-of league history evidence | Make ESPN activity a separately caught optional phase; record the 1,000-row request cap, whether it was reached, all attempt outcomes, source time range, counts, normalized/skipped reasons, and roster digest | Append-only history capture keyed to bundle opaque binding, season, capture/host IDs, exact roster ownership digest, and manifest ID | Core bundle still publishes. A cap hit marks transaction coverage incomplete; GM/timing panels say history unavailable or partial. No future capture may appear in an older bundle's snapshot |
| P0 | Full custom-scoring compatibility | Capture all source-disclosed raw projected stat components and the complete host scoring items once per refresh; map and reconcile locally | Versioned scoring-normalization artifact keyed by source row/artifact, scoring-profile ID, stat-mapping version, and formula | Continue with disclosed STD/HALF/PPR provider totals; mark custom host scoring incompatible and fail any formula that requires exact host rescoring |
| P0 | A genuinely FantasyPros-free refresh path | Remove mandatory FantasyPros league/ECR/benchmark fields only through an explicit v11 design; use complete independent source plans, waiver evidence, and separately validated independent power | Independent calculation manifest/formula/disclosure in v11; FantasyPros benchmark becomes an optional sidecar | Continue loading the last valid v8 bundle offline, but do not claim a new independent weekly bundle can be published or that its power reproduces FantasyPros |
| P1 | Public alias coverage | Merge exact FantasyPros crosswalk IDs, provider-link IDs, public crosswalk files, and reviewed conflict resolutions into one graph | Content-addressed alias graph shared by profile, projection, history, and draft materializers | Keep an ambiguous public player isolated. If it is required by v8 ownership, fail bundle publication; otherwise omit/mark only that auxiliary row |
| P1 | Full public player profile evidence | Collect bounded nflverse weekly stats/injuries, Sleeper player metadata/trends, and lawful crosswalk data with independent attempt handling | Profile/catalog sidecar plus auxiliary manifest and alias-graph ID; preserve raw stats and source wording/times | Show available profile sections and explicit gaps. Never turn trend/status evidence into an ensemble vote or certain game availability |
| P1 | Independent projection sources | Run each configured provider's finite position/week/scoring plan once; retain only providers whose declared plan passes completeness | Provider projection sidecar plus auxiliary manifest; promote only proven, identity-resolved owned/waiver rows into a future bundle | Drop a partial provider from the ensemble, disclose the failed attempt, and keep the existing quorum policy. Never average a partial page silently |
| P1 | Draft preseason player universe and current board provenance | Build current preseason features from bounded lawful sources and accept historical packs only with pre-kickoff source-as-of evidence | Separate `PreseasonPlayerUniverse`, `DraftBoardSourceManifest`, and `HistoricalCorpus`; reference alias graph and auxiliary manifests | Reject a season/feature with hindsight risk, a board that cannot fill every configured roster, or an unknown license state where redistribution is required |
| P1 | Exact platform settlement | Capture/version score precision, correction policy, regular/playoff ties, home bonus, qualifier/division berths, reseeding, and bracket/multiweek rules | Typed settlement/bracket policy referenced by league state and host artifact | Preserve the reconstructed-rule disclosure; fail closed on unsupported or underspecified rules |
| P1 | Historical forecast/actual calibration data | Save each published forecast before kickoff and join later official actuals without mutating the forecast | Append-only forecast/actual sidecar keyed by league binding, bundle, canonical player, game/week, scoring identity, and source time | Keep playoff percentages labeled model estimates and shared residual loadings uncalibrated |
| P2 | Player-week appearance probability | Combine timestamped status/practice reports with historical appearances, separating observation from fitted outcome | Availability sidecar with model version, training window, holdout metrics, canonical player/game/week, and observed-as-of time | Display source labels only; never treat Q/O/IR or a blank provider row as a certain outcome |
| P2 | Calibrated uncertainty and correlations | Fit marginal forecast errors and game/team/player residual loadings from saved forecast/actual pairs | Content-addressed calibration result with training/holdout window, scoring population, features, metrics, and loadings | Disclose the current position floors and zero shared loadings as assumptions |
| P2 | Championship simulation inputs | Materialize playoff-week provider cells and exact host bracket/settlement after both pass readiness | Postseason scenario dataset keyed by bundle and the same random stream used in comparisons | Retain the field-conditioned power-share proxy label; do not report it as championship odds |
| P2 | Cross-run ETA history | Persist bounded successful phase-throughput observations by operation/phase/implementation/workload class | Operational SQLite table with expiry and outlier-resistant summaries; excluded from all bundle and result content IDs | Show progress without an ETA until enough current-run samples exist; a corrupt timing store is disposable |

## Captured data that should remain auxiliary

- FantasyPros projected standings, playoff odds, and modeled title odds are
  retained as a bundle-bound, timestamped comparison benchmark. They remain
  auxiliary evidence for drift and regression review and are never blended
  into local season simulation as though they were independent observations.
- FantasyPros team “needs” labels are presentation hints, not inputs to power,
  trade legality, or playoff calculations.
- FantasyPros player `espn_id` / `yahoo_id` crosswalk fields should seed the
  reviewed identity manifest when present and must agree with IDs parsed from
  provider projection links. They should not replace the conservative
  ambiguity checks. This integration belongs beside the active browser/player
  profile work so there is one identity authority rather than two competing
  implementations.
- Raw browser tables and private transport details do not belong in a portable
  bundle. Credential-screened, typed capture artifacts are retained in the
  local-only `raw-captures` archive so a normalization failure can be diagnosed
  without another provider request. Portable bundles retain normalized rows,
  content digests, and validation metadata only; cookies, authorization
  headers, browser storage, and secret-bearing URLs are never retained.
- Completed transaction history, proposal/process timestamps, roster snapshots,
  and manager tendencies are mutable league sidecars. They are joined as-of a
  selected bundle; they do not change that bundle's content ID.
- Public stats, injuries, player metadata, add/drop trends, and full-catalog
  projections belong to player/profile sidecars. Only the identity-proven,
  bounded calculation subset may be copied into a future calculation bundle.
- Historical draft outcomes, preseason universes, boards, training checkpoints,
  and models remain Draft Lab datasets. They never become ECR/projection
  evidence for an in-season trade merely because a canonical player ID matches.
- Progress, elapsed time, ETA samples, pause/cancel state, and UI selections are
  operational state. They are intentionally excluded from scientific
  provenance, bundle IDs, and deterministic result identities.

## Delivery architecture

Use immutable weekly engine bundles only for data needed by trade search and
season simulation. Keep mutable or growing concerns in separately versioned
sidecars:

```text
local league workspace
  private league profile/catalog
    provider coordinates + user alias + selected team
    one-to-one opaque league-binding association
    bundle associations + archive state
  weekly EngineBundle v8 artifacts (immutable, portable)
    standings / rosters / rules
    NFL schedule + ensemble configuration
    strength formula + rebuilt model + methodology evidence
    regular-season projection grid
    raw projection evidence + ECR
    bounded waiver pool
    FantasyPros comparison benchmark
  league-history sidecar (append-only, as-of reads)
    acquisition manifests + roster/lineup snapshots
    transactions + timestamp semantics + evidence IDs
    bundle/history bindings + revisions
  public player-data sidecars
    stable public alias graph
    AuxiliarySourceManifest records
    profiles / raw actual stats / injury observations / trends
    independent and full-catalog projection snapshots
  draft sidecars
    PreseasonPlayerUniverse + DraftBoardSourceManifest
    HistoricalCorpus + source manifests
    checkpoints / models / assistant sessions / observed picks
  operational stores (replaceable)
    job state + exact phase work units
    cancellation + persisted ETA observations
  local-only raw-captures archive (credential screened)
```

All joins use canonical player IDs plus an explicit league, bundle, game, week,
and scoring identity where applicable. A feature reads one declared version of
each dataset. Missing optional sidecars disable or qualify only that feature;
they do not mutate or contaminate an already-published weekly engine bundle.

## Release gates and safe degradation

| Artifact or feature | Publication/use gate | Safe degradation when the gate fails |
| --- | --- | --- |
| `EngineBundle` v8 | Exact team/roster ownership; one legal eligibility contract per calculation player; complete NFL weeks 1–18; complete regular-season ensemble grid; every value/verified omission artifact-bound; formula-required ECR/projection horizons; scoring/source identity; bounded waiver pool; exact v8 FantasyPros benchmark/team coverage; formula rebuild matches saved strength model; valid methodology evidence | Do not publish/load the bundle. Preserve typed collection failures and the prior valid bundle. History/profile/draft/timing failures are not part of this gate; the benchmark remains a current v8 coupling until a future migration removes it |
| Power result | Bundle gate plus formula evidence and declared package-shape coverage | Return `extrapolated`, `surrogate`, or `surrogate_extrapolated` as applicable. Never upgrade representative holdout evidence to an exact proprietary-method claim |
| Trade-search result | Bundle power, roster-adjustment, projected-standings, and playoff prerequisites ready; request identities/filters valid | Do not run an invalid candidate universe. Unknown host trade policy may still allow clearly labeled analytical results, but not an executable/legal claim |
| Dashboard/workbook | Exact selected bundle, baseline/scenario identity, readiness object, and—for current v8—the mandatory bound benchmark | Render/export local values with inherited limitations. After a future optional-benchmark migration, hide/mark only the missing comparison; never substitute source benchmark values |
| League workspace association | Exactly one local profile resolves to the bundle opaque binding and season; scoring and host identity agree | Place bundle in an explicit unassigned area. Do not infer an association or expose private IDs in the portable artifact |
| History capture | Opaque binding/season/host snapshot agree; acquisition manifest present; canonical teams/assets; roster digest and coverage/timestamp semantics validated | Reject/quarantine that history capture only. Keep core bundle and previous valid history. GM/timing reports expose the omission |
| GM insight | As-of history revision exists and the requested statistic's coverage prerequisites are met | Return per-section unavailable/partial reasons. Use counts rather than rates when the trading window is unknown |
| Historical valuation | Complete event assets/timestamp plus an at-or-before comparable bundle within the allowed lag; no future history visible | Mark that transaction unvalued with a stable reason while valuing other eligible transactions |
| Trade timing | Current bundle ready; every recommended candidate feasible; trajectory/health/history provenance disclosed; legality evidence required for executable wording | Skip individual infeasible candidates. Fall back to current-data-only analytical timing when history is absent; withhold executable wording when deadline/locks are unknown |
| Player profile/catalog | Alias graph resolves the player; each displayed dataset has a valid auxiliary attempt and provenance/license record | Keep the player/section visibly unavailable. Never modify core projection cells or manufacture negative rows |
| Independent projection provider | Full declared provider plan captured and validated for the contributing scope; identities and point basis known | Exclude the provider from that ensemble and disclose it as unavailable. Continue only if configured quorum survives |
| Draft historical corpus | Preseason source-as-of precedes kickoff; feature policy rejects identity/outcome leakage; actual-week coverage/scoring and source provenance valid; universe can support configured drafts | Reject the affected season/corpus. Do not train on partial, hindsight-filled, or zero-filled rows |
| Draft board/assistant | Board manifest complete; model feature families covered; canonical/provider mapping unambiguous; legal player supply; draft format/order supported | Disable recommendations or use manual pick entry. Never guess missing players, roll back observed picks, or reinterpret auction/keeper/traded-pick drafts as ordinary snake drafts |
| Draft checkpoint/model | Complete generation boundary; corpus/universe/config/feature-policy/code identities match; paired evaluation result retained | Keep the last complete checkpoint. Discard a cancelled partial generation and do not publish a model from it |
| Progress/ETA | Progress uses exact monotone phase units; ETA uses sufficient same-phase active-time observations | Continue the operation with indeterminate/determinate progress and no ETA. Timing-store loss never invalidates the output |

The runtime `data_readiness` report recomputes core gates from the selected
bundle instead of asserting constants. Sidecar services need equivalent
feature-level readiness records, composed with bundle readiness at the API
boundary. An overall warning is not sufficient: every unavailable output must
carry the specific missing input/evidence reason, and every unaffected feature
must remain usable.

## Integration order

1. Preserve `EngineBundle` v8 and finish the opaque league-binding join. Add the
   auxiliary manifest and negative-publication rules before importing any
   full-catalog or Draft Lab data path.
2. Make history ingestion optional and as-of-safe, then gate GM valuation and
   trade timing per result. Capture host trade legality before presenting a
   recommendation as executable.
3. Selectively port profile/catalog and independent-projection domain modules
   behind sidecars and the shared alias graph. Do not merge the donor bundle-v10
   implementation; reserve schema version 11 for a deliberate future bundle
   migration if one is truly required.
4. Port Draft Lab as its own data domain. Introduce
   `PreseasonPlayerUniverse`, `DraftBoardSourceManifest`, and strengthened
   `HistoricalCorpus` provenance before enabling imported data for training or
   draft-day advice; do not merge its bundle-v6 implementation.
5. Wire every long operation to exact phase work units, cancellation, and
   persisted observed-throughput ETA. Keep that store operational and
   replaceable, outside all calculation and evidence hashes.
6. Add raw-stat host rescoring, exact settlement/bracket policy, and historical
   forecast calibration as later evidence-backed upgrades. Until each gate is
   met, retain the precise limitation labels above.
