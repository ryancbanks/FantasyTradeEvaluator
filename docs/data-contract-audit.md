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

## End-to-end feature matrix

| Functionality | Data required | Acquisition and retained form | Delivery and validation | Status / remaining action |
| --- | --- | --- | --- | --- |
| Weekly private-league scan | League identity; teams; standings; rosters; slot/cap/reserve rules; scoring; fantasy matchups; playoff settings; NFL schedule | ESPN public JSON with a signed-in extension fallback; FantasyPros league capture for roster/team cross-check; Yahoo Settings for reception-scoring verification | Normalized host snapshot feeds weekly assembly. A portable source manifest retains the opaque local league binding, binding scope, host provider, host and FantasyPros content IDs/capture times, and completed-history availability; the complete NFL schedule retains its own source, content ID, and capture time | **Ready.** Exact source dimensions and team/roster coverage fail closed; portable artifacts never contain the private external league ID |
| FantasyPros-style team power | The ECR/features named by the persisted formula; complete rosters; player eligibility; analyzer fingerprint and blind evidence | The reliable default formula consumes FantasyPros rest-of-season Latest ECR. A future formula may name other retained features, in which case readiness makes those exact horizons mandatory | The bundle rebuilds the model from formula-bound inputs on every load. Matching representative holdouts produces `holdout_validated`; it never implies exhaustive proof. Other shapes are `extrapolated`, and a failed fit can only publish as an explicitly accepted surrogate | **Holdout validated** for listed balanced, no-adjustment shapes; otherwise explicitly surrogate/extrapolated |
| Two-team trade enumeration and player/team filters | Ownership; package-size/imbalance rules; player positions and IDs; counterparties; power model | Entire search space is generated locally from the immutable bundle | Request parsing validates ownership and filter semantics before enumeration | **Ready.** No new upstream data is required per trade |
| Three-team trade enumeration | Same data as two-team search plus all three ownership transfers and shared free-agent allocation | Local decision tree over the same bundle | Team/player universes and deterministic allocation policy are validated | **Extrapolated** for FantasyPros-style power until three-team analyzer evidence exists; playoff calculations remain local |
| No-drop and roster balancing | Active cap; capacity-exempt placements; pre-trade active size; legal waiver candidates and eligibility | ESPN roster rules/reserve slots plus a bounded FantasyPros-best/ECR waiver pool | Drop permission and vacancy filling are independent: no-drop mode still fills trade-created vacancies, never removes a player, and rejects the candidate if the bounded pool cannot fill it | **Ready with bounded-pool disclosure.** The app does not claim the pool is the host's complete free-agent list |
| Projection ensemble | Provider player identity; projection publication status; weekly points; ROS points; raw stats; provider injury/status labels; provider weights; NFL game context; uncertainty | One bounded weekly browser session attempts each planned FantasyPros/ESPN/Yahoo page once and captures the NFL schedule once. Successful tables are retained; page failures are typed task outcomes; a player omitted from a proven-complete table is an artifact-bound `not_published` row | A projection-source manifest binds every normalized row or verified omission to its exact capture artifact and records task outcomes, source scoring format, point basis, host-scoring compatibility, position scope, and source period. A player remains usable when the configured two-of-three quorum survives. Status-label coverage explicitly counts providers that reported nothing | **Ready with limitations.** Only provider-total/base-format sources are currently supported. Unproved locally-recomputed/exact-host-rule claims fail closed; status designations remain observations, not calibrated appearance probabilities |
| Full remaining-season projection horizon | Every remaining NFL week, including fantasy playoff weeks; per-player byes | Collection plans through NFL week 18. ROS scope is converted to each player's scheduled active weeks | Full-horizon totals feed Player Lab and any explicitly configured projection feature; the reliable default power formula uses FantasyPros ROS ECR only. The fantasy regular-season slice alone feeds current season simulation. Future direct rows are subtracted before any ROS residual allocation | **Collected, retained, and consumed with a named limitation.** Residual points and raw stats are divided evenly across missing active weeks; this local shape is never labeled as a provider-published matchup projection. Playoff-week game cells are intentionally deferred until bracket simulation is implemented |
| Player Lab | Weekly fused values; full-NFL-ROS values; individual provider values and raw stat components; direct/ROS-derived provenance; provider status observations; ECR; owner; eligibility; waiver provenance | Reads only the selected bundle | Provider IDs, values, provider-scoped raw-stat keys, capture times, origin, status scope, and retained ROS totals reconcile to the same evidence used by simulation. Full ROS and the fantasy regular-season slice are labeled separately. Every week reports expected providers, reporting providers, providers with no status label, and whether status coverage is complete | **Ready with limitations.** Publication time appears only when disclosed; a blank label is visibly unknown rather than agreement, and designations are not converted to certain availability or appearance probabilities |
| Weekly lineup scoring | Legal slots; player-week projection distribution; bye and player-week availability | Roster ownership, eligibility, and materialized regular-season projections | Simulation eligibility must equal strength-model eligibility. A current reserve occupant cannot start in the first remaining week; that placement is not projected forward as evidence of future availability | **Ready with limitations.** Provider totals are not yet recomputed under every custom ESPN scoring item, and future reserve activation/drop decisions are not modeled |
| Projected standings | Current record/PF/PA; completed matchups for history-based tiebreaks; all remaining matchups; simulated scores; ranking rules | ESPN standings/settings/schedule plus local scenario results | Team outlook carries current rank, expected final record, expected PF/PA, and the complete rank distribution. Required tiebreak inputs are checked before any standings/playoff/search consumer can run | **Ready with limitations** for the implemented ESPN rule subset. The host response does not prove the complete multi-team settlement contract, so the local tiebreak sequence is labeled a reconstruction; unsupported or underspecified rules fail closed |
| Playoff qualification odds | Projected standings data plus qualifier count, divisions, seeding/reseeding and tiebreak rules | ESPN league settings and local shared season scenarios | Before/after states reuse identical random draws; seed distributions and playoff probability are returned and exported. Player scores are unbounded unless the persisted scenario configuration names a numeric floor | **Ready with limitations.** Cross-provider disagreement plus fixed position floors has not been calibrated against forecast errors and actuals, so these are model estimates rather than calibrated probabilities; shared game/team correlations also remain uncalibrated |
| FantasyPros standings comparison | Captured current/projected standings plus playoff/title probabilities, mapped to canonical league teams | A bundle-bound comparison benchmark retains its content ID, source league-artifact ID, capture time, and exact team coverage | The dashboard shows every source value beside signed local-minus-source drift, summarizes current-rank/record agreement, and uses rank agreement as a settlement diagnostic. The benchmark remains excluded from local calculation inputs | **Ready as diagnostic evidence.** Drift is visible without contaminating the local model |
| Exact championship odds | Playoff-week player projections; portable NFL schedule; exact bracket rounds/byes; playoff tie/home-bonus rules | The NFL schedule and full ROS evidence are portable, but playoff-week ensemble cells and the exact bracket are not yet materialized | No exact bracket consumer exists yet | **Not ready.** The only allowed interim value is a clearly labeled strength-weighted playoff-field proxy |
| Team dashboard and Excel export | Current and projected team outcomes; trade deltas; methodology/source provenance | Uses existing simulation and bundle data; no new collection | Both two- and three-team `.xlsx` Team Outlook sheets carry the same 14 fields: current rank/record, projected record and PF/PA, mean rank, playoff chance, and rank/seed distributions. Run Details freezes direct versus ROS-derived cell coverage and the scoring, availability, marginal-uncertainty, correlation, championship-proxy, and relevant as-of-time limitations; source rows carry the opaque league binding and capture evidence | **Ready with limitations.** It inherits the named simulation limits; a missing first-week kickoff timestamp is explicitly disclosed because a partially played week cannot be modeled safely |
| Multiple league workspaces | Stable league identity; exact scoring-profile identity; bundle history per league; active selection | The data layer maps provider/private league identity to a workspace-random opaque binding; portable bundles carry only that binding and exact scoring-profile identity. A separate concurrent workspace catalog owns user aliases and local provider URLs | Catalog/profile identity must resolve to the same opaque bundle binding before a bundle, history capture, or cache entry can be associated. Season plus a PPR label is never identity | **Binding layer ready; catalog integration must unify IDs.** A second random profile ID or deterministic hash must not fork one league into multiple identities |
| Historical trends and model calibration | Timestamped forecasts, actual player/game outcomes, league snapshots and model version | Append-only local history datasets keyed to canonical player, game, week, league binding, and bundle ID | Consumers should request explicit dataset versions rather than read mutable current-state tables | **Separate dataset required.** This is also the correct input for calibrating game/team correlation loadings |
| Public player profiles and draft tools | Bio/news/injury evidence, transactions, college/draft observations, ADP and league draft state | Versioned profile, availability, history, and draft datasets collected independently of weekly trade enumeration | Join through canonical player IDs and explicit league/bundle IDs; do not enlarge every engine bundle with unrelated history | **Separate sidecars required.** Missing sidecars must degrade the related view, never the trade calculations |

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

## Missing-data acquisition plan

| Missing data | Best acquisition path | Storage and delivery contract | Safe behavior until available |
| --- | --- | --- | --- |
| Exact cross-provider scoring compatibility | Capture all disclosed stat components and complete host scoring items once per refresh; compute fantasy points locally under the host rules | Versioned scoring-normalization artifact keyed by provider row, scoring-profile ID, and formula version | Disclose that provider totals were verified only to STD/HALF/PPR; reject a rule that cannot be represented safely |
| Player-week availability | Join retained provider designations and ESPN roster state with timestamped public NFL practice/game-status reports and historical appearances | Availability sidecar keyed by canonical player, NFL game/week, source, observed time, and a calibrated appearance estimate | Never treat Q/O/IR text as a certain outcome or infer OUT/PUP/suspension from a blank projection; retain the source observation while disclosing that future availability is unknown |
| Exact platform settlement | Capture or version the platform's score precision, regular/playoff tie handling, home bonus, qualifier, and bracket rules | Typed settlement/bracket policy referenced by the league state | Fail closed on unsupported tie/out-of-position rules; do not silently substitute generic rules |
| Exact championship simulation | Materialize playoff-week provider cells from retained ROS evidence and the persisted NFL schedule, then simulate the typed bracket | Postseason scenario dataset keyed by bundle ID and the same random-draw identity used for before/after comparisons | Do not label a proxy as championship probability |
| Calibrated correlations and uncertainty | Retain timestamped forecasts and actual results; fit league/game/team/player residual loadings locally | Content-addressed calibration result with training window, holdout metrics, and factor loadings | Label zero shared loadings as an independent-outcome assumption |
| Reviewed identity provenance | Retain the calculation-player subset of the identity registry plus unresolved-row diagnostics | Bundle-bound identity manifest keyed by canonical and provider player IDs | Reject conflicting IDs; never name-match an ambiguous player silently |

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

## Delivery architecture

Use immutable weekly engine bundles only for data needed by trade search and
season simulation. Keep mutable or growing concerns in separately versioned
sidecars:

```text
local league workspace
  league binding + source manifest
  weekly engine bundles
    standings / rosters / rules
    NFL schedule + ensemble configuration
    strength formula + rebuilt model + methodology evidence
    regular-season projection grid
    raw projection evidence + ECR
    bounded waiver pool
  availability snapshots
  public player profiles
  forecast-versus-actual history
  draft datasets
```

All joins use canonical player IDs plus an explicit league, bundle, game, week,
and scoring identity where applicable. A feature reads one declared version of
each dataset. Missing optional sidecars disable or qualify only that feature;
they do not mutate or contaminate an already-published weekly engine bundle.

## Release gate

A weekly bundle is publishable only when roster/team ownership is exact, every
calculation player has one legal eligibility contract, every regular-season
player/week ensemble cell exists, every fused provider identity and value is
backed by retained evidence, both ECR horizons exist, and methodology evidence
is valid. Its saved schedule must cover every NFL regular-season week through
week 18, and its evidence may contain only calculation players. The schedule,
ensemble configuration, formula, and ECR must rebuild the saved strength model
exactly.
The runtime `data_readiness` report then describes model limitations that are
not yet publication blockers. A feature marked **not ready** must remain absent
or clearly unavailable in the interface; a warning is not permission to invent
the missing calculation.

The bundle constructor already rejects missing or incompatible core ECR,
formula, projection-grid, scoring-identity, roster, and schedule evidence, so a
successfully loaded current-schema bundle normally reaches only the ready or
limited states. The readiness report still recomputes those gates from the
bundle rather than asserting constants, and the catalog copies its top-level
decision. This keeps API, dashboard, and workbook disclosures consistent and
prevents a future optional or partially migrated input from being presented as
ready by default.
