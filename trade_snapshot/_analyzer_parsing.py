"""Response-body parsers for the FantasyPros analyzer contract."""

from collections.abc import Mapping, Sequence

from ._analyzer_types import (
    CURRENT_BUNDLE_FINGERPRINT,
    AnalyzerContractError,
    AnalyzerObservation,
    AnalyzerTradeRequest,
    BundleFingerprint,
    PlayoffOddsChange,
    PlayoffOddsObservation,
    PowerRankingChange,
    PowerRankingObservation,
    _canonical_id,
    _mapping,
    _required_mapping,
    _require_request,
)


def parse_power_response(
    request: AnalyzerTradeRequest,
    response: Mapping[str, object],
) -> PowerRankingObservation:
    """Select exact scores for the requested teams from an ordinary response."""

    _require_request(request)
    response = _mapping("ordinary response", response)
    expected_key = request.response_period_key
    if expected_key not in response:
        available = sorted(set(response).intersection({"ros", "dynasty", "pre", "dyn"}))
        if available:
            raise AnalyzerContractError(
                f"expected response period {expected_key!r}, found {available!r}"
            )
        raise AnalyzerContractError(f"ordinary response is missing period {expected_key!r}")
    period_result = _mapping(f"response period {expected_key}", response[expected_key])
    rankings = _required_mapping(period_result, "powerRankings", "response period")
    before = _power_rows(rankings, "before")
    after = _power_rows(rankings, "after")
    return PowerRankingObservation(
        semantic_period=request.period,
        response_period_key=expected_key,
        team1=_power_change(request.team1_id, before, after),
        team2=_power_change(request.team2_id, before, after),
    )


def parse_playoff_response(
    request: AnalyzerTradeRequest,
    response: Mapping[str, object],
) -> PlayoffOddsObservation:
    """Read both teams' playoff odds from one full-analysis response."""

    _require_request(request)
    response = _mapping("full-analysis response", response)
    playoffs = _required_mapping(response, "playoffs", "full-analysis response")
    return PlayoffOddsObservation(
        team1=_playoff_change(playoffs, "team1", request.team1_id),
        team2=_playoff_change(playoffs, "team2", request.team2_id),
    )


def parse_analyzer_observation(
    request: AnalyzerTradeRequest,
    ordinary_response: Mapping[str, object],
    full_response: Mapping[str, object] | None = None,
    *,
    bundle: BundleFingerprint = CURRENT_BUNDLE_FINGERPRINT,
) -> AnalyzerObservation:
    """Build the complete sanitized observation; raw responses are not retained."""

    return AnalyzerObservation(
        request=request,
        power=parse_power_response(request, ordinary_response),
        playoffs=(
            parse_playoff_response(request, full_response)
            if full_response is not None
            else None
        ),
        bundle=bundle,
    )


def _power_rows(
    rankings: Mapping[str, object],
    moment: str,
) -> dict[str, Mapping[str, object]]:
    if moment not in rankings:
        raise AnalyzerContractError(f"powerRankings is missing {moment}")
    rows = rankings[moment]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise AnalyzerContractError(f"powerRankings.{moment} must be an array")
    indexed: dict[str, Mapping[str, object]] = {}
    for index, row in enumerate(rows):
        row = _mapping(f"powerRankings.{moment}[{index}]", row)
        if "teamId" not in row:
            raise AnalyzerContractError(f"powerRankings.{moment}[{index}] is missing teamId")
        team_id = _canonical_id("response teamId", row["teamId"])
        if team_id in indexed:
            raise AnalyzerContractError(
                f"powerRankings.{moment} contains duplicate team {team_id}"
            )
        indexed[team_id] = row
    return indexed


def _power_change(
    team_id: str,
    before: Mapping[str, Mapping[str, object]],
    after: Mapping[str, Mapping[str, object]],
) -> PowerRankingChange:
    if team_id not in before or team_id not in after:
        raise AnalyzerContractError(f"power rankings are missing requested team {team_id}")
    for moment, row in (("before", before[team_id]), ("after", after[team_id])):
        if "score_decimal" not in row:
            raise AnalyzerContractError(
                f"power rankings {moment} row for team {team_id} is missing score_decimal"
            )
    return PowerRankingChange(
        team_id,
        before[team_id]["score_decimal"],
        after[team_id]["score_decimal"],
    )


def _playoff_change(
    playoffs: Mapping[str, object],
    team_label: str,
    team_id: str,
) -> PlayoffOddsChange:
    before_key = f"oddsBefore_{team_label}"
    after_key = f"oddsAfter_{team_label}"
    for key in (before_key, after_key):
        if key not in playoffs:
            raise AnalyzerContractError(f"playoffs is missing {key}")
    return PlayoffOddsChange(team_id, playoffs[before_key], playoffs[after_key])
