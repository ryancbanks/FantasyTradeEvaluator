"""Public façade for the sanitized FantasyPros trade-analyzer contract.

The contract contains no HTTP client: keys, cookies, and request URLs remain in
the collector while only trade meaning and selected response values cross into
the local calculation snapshot.
"""

from ._analyzer_parsing import (
    parse_analyzer_observation,
    parse_playoff_response,
    parse_power_response,
)
from ._analyzer_records import observation_from_record, observation_to_record
from ._analyzer_types import (
    CURRENT_BUNDLE_FINGERPRINT,
    AnalyzerContractError,
    AnalyzerObservation,
    AnalyzerPeriod,
    AnalyzerTradeRequest,
    BundleFingerprint,
    PlayoffOddsChange,
    PlayoffOddsObservation,
    PowerRankingChange,
    PowerRankingObservation,
)


__all__ = (
    "CURRENT_BUNDLE_FINGERPRINT",
    "AnalyzerContractError",
    "AnalyzerObservation",
    "AnalyzerPeriod",
    "AnalyzerTradeRequest",
    "BundleFingerprint",
    "PlayoffOddsChange",
    "PlayoffOddsObservation",
    "PowerRankingChange",
    "PowerRankingObservation",
    "observation_from_record",
    "observation_to_record",
    "parse_analyzer_observation",
    "parse_playoff_response",
    "parse_power_response",
)
