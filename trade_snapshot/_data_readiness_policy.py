"""Stable limitation and interpretation text for data-readiness outputs."""

_SCORING_LIMITATION = (
    "Provider fantasy-point totals are verified to the selected STD/HALF/PPR "
    "format, but have not been recomputed from raw stats under every custom host rule."
)
_AVAILABILITY_LIMITATION = (
    "Provider injury/status designations are retained as timestamped observations "
    "only. They are not calibrated appearance probabilities, and future reserve "
    "activation/drop decisions are not modeled."
)
_INDEPENDENT_CORRELATION_LIMITATION = (
    "Scenario outcomes use independent player shocks; shared league, game, and "
    "NFL-team correlations have not been calibrated."
)
_CONFIGURED_CORRELATION_LIMITATION = (
    "Shared scenario loadings are configured, but the bundle does not retain "
    "calibration evidence proving those league, game, and NFL-team correlations."
)
_MARGINAL_UNCERTAINTY_LIMITATION = (
    "Player-week uncertainty uses cross-provider disagreement plus fixed "
    "position-specific floors; it has not been calibrated against forecast errors "
    "and actual results, so playoff odds are model estimates, not calibrated "
    "probabilities."
)
_CHAMPIONSHIP_PROXY_LIMITATION = (
    "Championship probability is a strength-weighted playoff-field proxy, not an "
    "exact bracket simulation."
)
_AS_OF_TIME_LIMITATION = (
    "At least one scheduled NFL game in the first remaining week lacks a kickoff "
    "timestamp, so the bundle cannot prove that every game was unplayed at capture "
    "time; partially played weeks are not modeled."
)
_ROS_ALLOCATION_LIMITATION = (
    "Residual rest-of-season points and raw stat components are divided evenly "
    "across missing active NFL weeks. Those weekly shapes are local allocations, "
    "not provider-published matchup projections."
)
_FANTASYPROS_BENCHMARK_POLICY = (
    "Comparison only: retained FantasyPros standings and probabilities are used "
    "for model-drift review and are never blended into local team outlooks or "
    "playoff odds."
)
_HOST_SETTLEMENT_POLICY_LIMITATION = (
    "The host snapshot does not retain an authoritative, proof-bound contract for "
    "every multi-team tiebreak settlement case. The local tiebreak sequence and "
    "balanced head-to-head policy are a declared reconstruction; current-rank "
    "comparison with FantasyPros is diagnostic, not proof for future tied scenarios."
)

