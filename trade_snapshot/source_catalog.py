"""Safe, user-visible catalog of automatic weekly source URLs."""

from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit

from .capture_schema import CaptureProvider
from .espn_free_read import EspnFreeReadClient
from .independent_source_plan import build_independent_weekly_source_plan
from .positions import CANONICAL_PLAYER_POSITIONS
from .public_player_data import public_player_source_urls
from .source_plan import build_weekly_source_plan
from .weekly_collection import WeeklyCollectionRequest


_FP_ANALYZER = "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"
_ESPN_PROJECTIONS = "https://fantasy.espn.com/football/players/projections"
_CBS_CONSENSUS = (
    "https://www.cbssports.com/fantasy/football/rankings/ppr/top200/consensus/"
)
_SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
_SLEEPER_DOCS = "https://docs.sleeper.com/"
_FFA_ACCURACY = "https://fantasyfootballanalytics.net/which-projections-are-most-accurate"
_NFL_REGULAR_SEASON_END_WEEK = 18


def weekly_source_catalog(request: WeeklyCollectionRequest) -> dict[str, object]:
    """Describe calculation and reference sources without opening any page."""

    if not isinstance(request, WeeklyCollectionRequest):
        raise ValueError("request must be a WeeklyCollectionRequest")
    positions = tuple(sorted(CANONICAL_PLAYER_POSITIONS - {"IDP"}))
    builder = (
        build_weekly_source_plan
        if request.use_fantasypros
        else build_independent_weekly_source_plan
    )
    preview_weeks = _projection_preview_weeks(request)
    plan = builder(
        season=request.season,
        as_of_week=request.week,
        remaining_weeks=preview_weeks,
        scoring=request.scoring,
        player_positions=positions,
        include_future_weekly=request.include_future_weekly,
        broad_consensus=request.use_broad_consensus,
    )
    urls = defaultdict(list)
    for task in plan.tasks:
        provider = task.provider.value
        if task.url not in urls[provider]:
            urls[provider].append(task.url)

    calculation = []
    if request.use_fantasypros:
        calculation.append(
            _source(
                "FantasyPros",
                "required",
                urls.get("fantasypros", (_FP_ANALYZER,)),
                (
                    "League rosters, current ECR, and weekly power-method calibration. "
                    + (
                        "Its aggregate point projection is retained for FantasyPros-style power only and is excluded from the broad forecast average."
                        if request.use_broad_consensus
                        else "Its point projection participates in the established FantasyPros, ESPN, and Yahoo core ensemble."
                    )
                ),
            )
        )
    else:
        calculation.append(
            _source(
                "FantasyPros",
                "off",
                (_FP_ANALYZER,),
                "Turned off. The independent engine never visits or depends on FantasyPros.",
            )
        )

    espn_urls = list(urls.get("espn", (_ESPN_PROJECTIONS,)))
    league_id = _espn_league_id(request.host_league_url)
    if league_id is not None:
        runtime = f"{_ESPN_PROJECTIONS}?leagueId={league_id}"
        espn_urls = [runtime, *EspnFreeReadClient.urls(request.season, league_id)]
        espn_note = (
            "Required league rosters, rules, standings, schedule, and one exact "
            "current-season full-season projection table. "
            "Private leagues are read through the paired browser session when necessary."
        )
    else:
        espn_note = (
            "Required host-league data and one exact current-season full-season "
            "projection table. The league-specific URL "
            "appears here after an ESPN league ID is pasted or discovered from FantasyPros."
        )
    if request.use_broad_consensus:
        espn_note += " ESPN is one independent forecast vote."
    else:
        espn_note += " ESPN participates in the core forecast ensemble."
    calculation.append(_source("ESPN", "required", espn_urls, espn_note))
    yahoo_urls = (
        (request.yahoo_projection_league_url,)
        if request.yahoo_projection_league_url is not None
        else urls.get(
            "yahoo",
            ("https://football.fantasysports.yahoo.com/f1/players",),
        )
    )
    calculation.append(
        _source(
            "Yahoo",
            "required",
            yahoo_urls,
            "Signed-in current-season player projections and league scoring verification. "
            "Position pages are traversed to their verified end before capture. Yahoo is "
            "a calculation input in both the core ensemble and broad consensus.",
        )
    )
    for provider, key, note in (
        (
            "CBS Sports",
            "cbs",
            "Public publisher season projections, converted to the remaining schedule.",
        ),
        (
            "FFToday",
            "fftoday",
            "Public weekly QB/RB/WR/TE/K and season QB/RB/WR/TE/K/DL/LB/DB tables; season totals are schedule-adjusted locally. DST and weekly IDP are excluded because their public rows do not expose stable player identity links.",
        ),
        (
            "FantasySharks",
            "fantasysharks",
            "Public weekly and Rest of Year point projections selected automatically.",
        ),
    ):
        calculation.append(
            _source(
                provider,
                "best_effort" if request.use_broad_consensus else "off",
                urls.get(key, ()),
                (
                    f"{note} The app attempts this built-in source automatically. It contributes one equal vote only when accepted by coverage validation; rejected captures contribute nothing."
                    if request.use_broad_consensus
                    else f"{note} Broad consensus is off, so this source is not visited."
                ),
            )
        )

    reference = (
        _source(
            "FFA accuracy study",
            "reference",
            (_FFA_ACCURACY,),
            "Methodology reference supporting simple multi-source aggregation. FFA is itself an aggregate and is not counted as a projection source.",
        ),
        _source(
            "CBS Consensus",
            "reference",
            (_CBS_CONSENSUS,),
            "Public ordinal expert rankings. Shown for comparison, not mixed into fantasy-point projections.",
        ),
        _source(
            "Sleeper",
            "reference",
            (_SLEEPER_PLAYERS, _SLEEPER_DOCS),
            "Documented public player metadata and API reference. Sleeper documents no equivalent projection endpoint, so it is not assigned invented point values.",
        ),
    )
    public_profile_sources = public_player_source_urls(request.season)
    profile_sources = (
        _source(
            "nflverse",
            "best_effort",
            tuple(
                source.url
                for source in public_profile_sources
                if source.provider == "nflverse"
            ),
            "Public current/prior weekly player stats and three seasons of documented injury reports. Missing unpublished seasons stay explicitly unavailable. nflverse provenance is retained under its CC-BY-4.0 license.",
        ),
        _source(
            "Sleeper",
            "best_effort",
            tuple(
                source.url
                for source in public_profile_sources
                if source.provider == "sleeper"
            ),
            "Public player, team/depth, current status, and seven-day add/drop metadata for Player Lab. Trending data is displayed with Sleeper attribution and never becomes a projection vote.",
        ),
        _source(
            "DynastyProcess",
            "best_effort",
            tuple(
                source.url
                for source in public_profile_sources
                if source.provider == "dynastyprocess"
            ),
            "Public weekly exact ESPN/Sleeper/GSIS identifier crosswalk for Player Lab. It is runtime-fetched with attribution to the DynastyProcess data repository under GPL-3.0 and never becomes a projection vote.",
        ),
    )
    return {
        "mode": "fantasypros" if request.use_fantasypros else "independent",
        "projection_mode": (
            "broad_consensus" if request.use_broad_consensus else "core_ensemble"
        ),
        "calculation_sources": calculation,
        "reference_sources": list(reference),
        "profile_sources": list(profile_sources),
        "weekly_projection_preview": {
            "scope": (
                "remaining_nfl_weeks"
                if request.include_future_weekly
                else "current_week_only"
            ),
            "weeks": list(preview_weeks),
            "league_end_discovered_during_scan": request.include_future_weekly,
        },
    }


def _source(provider, status, urls, note):
    values = tuple(urls)
    if (
        not isinstance(provider, str)
        or not provider
        or status not in {"required", "best_effort", "reference", "off"}
        or any(not isinstance(value, str) or not value.startswith("https://") for value in values)
        or not isinstance(note, str)
        or not note
    ):
        raise ValueError("source catalog entry is invalid")
    return {
        "provider": provider,
        "status": status,
        "urls": list(values),
        "note": note,
    }


def _espn_league_id(url):
    if url is None:
        return None
    parsed = urlsplit(url)
    values = [
        value for key, value in parse_qsl(parsed.query)
        if key.casefold() == "leagueid"
    ]
    if len(values) != 1:
        raise ValueError("normalized ESPN league URL has no unique league ID")
    return values[0]


def _projection_preview_weeks(request):
    """Return a safe superset until the league's exact endpoint is captured."""

    if not request.include_future_weekly:
        return (request.week,)
    final_week = max(request.week, _NFL_REGULAR_SEASON_END_WEEK)
    return tuple(range(request.week, final_week + 1))


__all__ = ("weekly_source_catalog",)
