# Calculation model notes

This document separates established FantasyPros behavior from hypotheses that still need blind validation. It contains no league key or private response payload.

## Established observations

- FantasyPros returns a deterministic absolute strength for every league team.
- Displayed power is normalized against the pre-trade league leader:

  `power = 100 * absolute_strength / pre_trade_max_absolute_strength`

  This matched all 18 teams before and after a sampled trade to floating-point precision.
- Narrow controlled QB drops and swaps were additive when the players occupied comparable depth contexts. That established a useful local invariant, not a global player-value formula.
- A controlled RB-for-RB bench swap on 2026-09-01 left both teams' displayed starting lineups unchanged, yet moved the teams by -2.9 and +3.4 power points. Display rounding cannot create that 0.5-point asymmetry. This rejects a single context-independent bench value for every player.
- Four additional displayed-power perturbations used anonymized players outside both displayed starting lineups:

  | Primary sends | Counterparty sends | Primary | Counterparty |
  | --- | --- | ---: | ---: |
  | Player A | Player B | -2.1 | +2.5 |
  | Player C | Player B | -2.7 | +3.2 |
  | Player D | Player E | +0.1 | -0.1 |
  | Player A | Player E | +0.7 | -0.1 |

  In the primary receiving context, Player D's displayed marginal value is about 0.6 points above Player A's; in the counterparty context those two players are approximately equal at display precision. This is direct evidence that marginal depth value depends on the rest of the positional depth chart.
- The current generalized hypothesis is:

  `absolute_strength(roster) = sum(residual[player]) + max_legal_assignment(sum(score[player, roster_role]))`

  Here `roster_role` may be a starter or a scored depth position such as RB3/RB4. The exact role structure and scores still require black-box calibration and blind validation.

- FantasyPros' exposed trade values and ECR labels are not sufficient to recover `base`: sampled players with the same exposed trade value had different exact base contributions.
- Identical playoff-odds requests vary while power scores remain fixed, so a local playoff model should target the expected probability, not equality with one displayed run. A roughly 50,000-trial simulation is an empirical hypothesis only: the current public client bundle contains no simulation loop, trial count, or relevant randomness.

## Authenticated client contract observed on 2026-09-01

The authenticated analyzer loads a dedicated Vue bundle and sends its calculations to the NFL My Playbook Trade Analyzer endpoint. The client supplies a league/team context, period, selected experts, traded player IDs, and optional add/drop IDs. Initial loading and playoff analysis are explicit modes of the same server boundary.

The initial response exposes player values, teams, standings, best free agents, and whether playoff odds are supported. The ordinary trade response contains separate before/after league power rankings, positional rankings, starter rankings, server-selected rosters/lineups, and trade-value assets. The playoff response contains before/after odds for both teams in one response. Switching the full-analysis perspective is therefore a local display action, not another playoff calculation.

Important client transformations:

- raw `score_decimal` values are rounded to one decimal for display;
- the displayed power change is the difference between those two rounded values;
- playoff before/after values and their difference are displayed to one decimal, while the underlying response values remain numeric;
- roster eligibility strings containing QB map to superflex, and strings containing RB/WR/TE map to flex;
- the server, not the browser bundle, supplies the strength, starting lineup, and playoff-odds results.

No league key, cookie, request header, or private response body is retained in these notes. The installed weekly collector must strip query strings and secret-bearing fields before persisting calibration observations.

## Validation-gated FantasyPros methodology mode

The local evaluator recomputes an ordinary trade by changing two rosters and solving a small maximum-weight roster-role assignment. It can also apply one simultaneous three-team agreement by moving each selected player from the original owner to either other participant, then rescoring all three resulting rosters. It never calls FantasyPros for a candidate after the role formula is calibrated. A weekly refresh captures current ECR/projections and reapplies the frozen formula to every player locally.

Three-team agreements are outside the current FantasyPros attestation because its held-out analyzer evidence covers two-team packages only. An exact weekly formula therefore labels three-team power as `extrapolated`; a surrogate weekly formula labels it `surrogate_extrapolated`. This limitation is disclosed before a three-team run, not only in its results. Playoff probabilities remain fully local in either case and reuse the same scenario draws for the before and after league states. If roster adjustments are allowed and multiple teams need scarce free agents, teams reserve their locally optimal replacements in ascending team-ID order from the players still available. The app, result payload, and workbook disclose that deterministic allocation policy.

The initial calibration evidence must contain:

- current rosters and position eligibility;
- one league baseline response and its normalization denominator;
- a residual contribution, if any, for every searched player;
- calibrated assignment scores for each relevant starter and depth role;
- a frozen validation set of unseen multi-player trades.

Calibration is a designed experiment, not an exhaustive trade run. The default design fits on 250 diverse atomic swaps and reserves 100 deterministic, leakage-safe multi-player packages for blind validation; it never invokes the slow playoff analysis. The holdouts cover every balanced package size the roster and budget can prove (normally 1-for-1 through 13-for-13 for 14-player rosters), with 2-for-2, 3-for-3, and 4-for-4 mandatory. A full-roster exchange is excluded because its after-rosters are already the opposing baseline training anchors. Because the proprietary calculation is server-side, the calibration collector also needs a version fingerprint for the analyzer bundle and response schema. A changed fingerprint invalidates the prior formula until the invariant and held-out tests pass again. An unchanged fingerprint only makes the formula eligible for a bounded weekly revalidation: at least 100 distinct ordinary-power holdouts from the current snapshot must keep maximum raw score and delta error at or below `1e-6` and match every displayed change. The formula and weekly report persist the blind IDs and balanced sizes; imbalanced packages, additions, and drops remain outside the exact claim until separately verified. This check happens once during weekly refresh, never during candidate enumeration.

Still to validate:

- the exact starter/depth role structure used for each position;
- whether RB/WR/TE role scores are invariant between named slots and FLEX;
- the smallest rate-safe calibration design for all rostered players;
- whether public projection/ECR fields can replace some black-box observations without losing exactness;
- formula stability from one NFL week to the next.

Until held-out absolute-score error is below `1e-6` and displayed changes agree exactly, results must be labeled **FantasyPros surrogate**, not exact FantasyPros output. Surrogate publication is an explicit, default-off fallback and is permitted only for a converged, identifiable fit with exact-tolerance training error and the complete diverse blind design. Its separate content-addressed disclosure persists the blind maximum score error, display-match rate, holdout identities/sizes, and `source_fit_id`; that fit ID binds the full solver diagnostics evaluated by the healthy-fit publication gate. Results inside an observed balanced/no-adjustment shape are labeled `surrogate`, while other shapes are `surrogate_extrapolated`. Neither is exact.

## Independent playoff-projection ensemble

This mode combines captured FantasyPros, ESPN, and Yahoo projections for game scores, projected records, and playoff probabilities. ESPN, Yahoo, and the cross-provider ensemble are explicitly excluded from the FantasyPros-style power formula. They therefore cannot distort the methodology calibration to chase a proprietary output.

For every player and remaining NFL week, retain:

- source-projected fantasy points and raw projected stats when available;
- opponent, game, kickoff, and bye status;
- injury/availability semantics and position eligibility;
- source publication time when disclosed and retrieval time separately;
- raw source row and a reviewed cross-source player identity.

Retain a source-published remaining-season total separately. Derive a remaining-season total from weekly rows only when every applicable remaining week is present. Missing is never zero.

## Projected records and playoffs

The snapshot also needs current standings, every remaining fantasy matchup, points-for/against, exact tiebreakers, playoff qualifier count, and seeding rules.

For each simulated season:

1. Optimize every team's legal lineup using projected means.
2. Simulate player outcomes with predictive uncertainty and shared NFL game/offense effects.
3. Settle the remaining fantasy schedule using platform score rounding.
4. Update records and tiebreak state.
5. Apply the captured qualification and seeding rules.

Before/after trade comparisons reuse the same simulated football outcomes, keyed by snapshot, seed, iteration, week, game, and player. This makes a no-op trade produce exactly zero change and sharply reduces noise in playoff-odds deltas.

Required outputs per team are current standing, expected final record, expected points, rank distribution, seed distribution, projected standing, and playoff qualification probability.

## Composable package filters

One atomic package rule can constrain players, positions, or both; when both dimensions are active they are combined with AND. Atomic rules may be nested under AND, OR, XOR, and NOT. XOR is true when exactly one of its immediate operands is true. The interface builds an explicit left-associated expression from top to bottom, so each connector combines the new rule with the complete result above it; NOT applies only to the rule beside it.

The filter is part of exact candidate generation rather than a post-search screen. Two-team package counts and three-team decision-tree counts therefore include the full Boolean expression, and a resumed search seeks within the same filtered enumeration. In three-team mode, the outgoing expression evaluates the primary team's complete outgoing package and the incoming expression evaluates its complete incoming package across both partners.

The original single-rule record remains package-filter semantics version 1 so existing request IDs and checkpoints do not change. A recursive expression uses semantics version 2 and its canonical tree is included in request and run identity. Commutative operands are canonically ordered without flattening nesting or removing duplicates; this preserves exact expression meaning while making equivalent operand orderings share an identity.

## Trade timing and projected pressure

The Trade Timing Lab extends, but does not reinterpret, the independent season simulation. Each team receives an actual-plus-projected cumulative record path. An actual result contributes `1` for a win, `0.5` for a tie, and `0` for a loss. The record slope is the Theil–Sen median of pairwise slopes over the latest four cumulative win-equivalent percentages; fewer than three points are insufficient, and values from `-0.02` through `+0.02` per week are neutral.

For every remaining result week, shared correlated scenarios estimate win/tie/loss probability, downward- and upward-slope probability, a two-loss-streak probability, and the joint loss-plus-downward trigger. When both outcome groups contain at least `max(100, 5% of scenarios)` paths, the app also reports playoff probability conditional on winning and conditional on losing that week. The reported pressure sensitivity is the non-negative part of win-conditional minus loss-conditional playoff probability; it is a simulated association, not a causal effect. The sortable pressure percentile is the equal-weight mean of loss probability, downward-slope probability, and that non-negative playoff sensitivity only when each included component is available for every league team in that week. A singleton league percentile is neutral (`0.5`). Pressure is not an offer-acceptance probability.

A delayed trade preserves baseline team scores before its effective week and splices post-trade team scores from that week forward. Baseline and changed rosters use identical player, NFL-team, game, league, and tiebreak draws. Executing in the first remaining week must therefore equal the ordinary immediate-trade projection. For a future loss/downturn watch, the before, execute-now, and delayed projections are all recomputed on the same exact pre-trade trigger paths; the trigger never uses post-trade results. Delay cost is therefore like-for-like inside that conditional subset. Every delayed result assumes today's other rosters remain unchanged.

The automatic timing preview is deliberately bounded: it screens current, injury-eligible 1-for-1 swaps with a displayed-power floor of `-5` for both teams, retains the three strongest per opponent, and simulates the current week plus the two highest-pressure usable future windows on at most 1,000 scenarios. A candidate appears only when each team's paired playoff point estimate improves by at least the larger of `0.25` percentage points or two scenario steps. This is a materiality rule, not a confidence interval. Future valuation also requires at least `max(100, 5% of all scenarios)` exact trigger paths. Because the current league contract does not retain a verified trade deadline, every current package is labeled verification-required and every later package is only a conditional watch. A negative shortlist result does not rule out unsimulated 1-for-1 swaps or larger packages. Custom package, imbalance, capacity, and content constraints continue to belong to the exhaustive trade-search path.

Completed-trade timing remains a separate evidence layer. An elapsed scoring period is one exposure and becomes active if the team participated in one or more eventually completed trades. Because the trade deadline is not captured, the denominator may include periods when trading was closed. Week W is classified only with results through Week W−1. Proposal timestamps and execution timestamps are stratified rather than pooled. Individual rates use Jeffreys smoothing and disclose Wilson intervals. Rate-difference bounds formed from those marginal intervals are labeled heuristic and claim no nominal coverage. Rejected offers and verified accepter roles are absent, so the output never claims an acceptance probability. Historical health is not yet aligned reliably to each decision period; consequently injury-sensitive behavioral labels and future personalization fail closed. Projected player “high” and “low” labels compare one player's week with that same player's active-week projection curve at or after the possible trade week. They are not market prices, future ECR forecasts, or evidence of another manager's beliefs.

## Implemented calculation path

1. Capture and validate one immutable weekly evidence set.
2. Revalidate or recalibrate the FantasyPros power formula against blind ordinary-power holdouts.
3. Fuse the player-week projection ensemble and solve legal lineups.
4. Build the shared record, standings, and playoff scenarios.
5. Enumerate and checkpoint the requested local trade space lazily. Three-team enumeration uses exact subtree counts so it can seek to a checkpoint without replaying preceding candidates.
6. Apply the fast local power gate to every participant, then recompute playoff outcomes only for survivors.
7. Present team outlook and trades where every participant gains in the localhost GUI and `.xlsx` export.
