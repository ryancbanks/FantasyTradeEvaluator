"""One-shot orchestration for the public player-data source catalog."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256

from ._public_player_http import (
    DownloadedPublicData,
    PublicPlayerDataCancelled,
    PublicPlayerDataError,
)
from .public_player_data import (
    DataAvailability,
    PublicDataProvenance,
    PublicPlayerDataLimits,
    PublicPlayerDataSnapshot,
    SeasonInjuryReports,
    SeasonPlayerStats,
    SleeperPlayerTrend,
    _aware_time,
    _season,
    public_player_source_urls,
)


def collect_public_player_data(
    season,
    *,
    as_of_week,
    limits,
    cancelled,
    clock,
    fetcher,
):
    _season(season)
    if type(as_of_week) is not int or not 1 <= as_of_week <= 25:
        raise ValueError("as_of_week must be an integer from 1 through 25")
    if not isinstance(limits, PublicPlayerDataLimits):
        raise ValueError("limits must be PublicPlayerDataLimits")
    if not callable(cancelled) or not callable(clock) or not callable(fetcher):
        raise ValueError("cancelled, clock, and fetcher must be callable")
    captured_at = clock()
    _aware_time("clock result", captured_at)
    captured_at = captured_at.astimezone(timezone.utc)
    sources = public_player_source_urls(season)
    from ._public_player_parse import (
        parse_player_id_crosswalk,
        parse_sleeper_players,
        parse_sleeper_trends,
    )

    jobs = (
        (
            _collect_stats,
            (
                season, sources[0], captured_at, limits, cancelled, fetcher,
                True, as_of_week,
            ),
        ),
        (
            _collect_stats,
            (
                season - 1, sources[1], captured_at, limits, cancelled, fetcher,
                False, as_of_week,
            ),
        ),
        *(
            (
                _collect_injuries,
                (
                    season - offset, source, captured_at, limits, cancelled,
                    fetcher, offset == 0, as_of_week,
                ),
            )
            for offset, source in enumerate(sources[2:5])
        ),
        (
            _collect_parsed,
            (
                sources[5], limits.max_sleeper_players_bytes,
                parse_sleeper_players, captured_at, limits, cancelled, fetcher,
                True,
            ),
        ),
        (
            _collect_parsed,
            (
                sources[6], limits.max_sleeper_trends_bytes,
                parse_sleeper_trends, captured_at, limits, cancelled, fetcher,
                False,
            ),
        ),
        (
            _collect_parsed,
            (
                sources[7], limits.max_sleeper_trends_bytes,
                parse_sleeper_trends, captured_at, limits, cancelled, fetcher,
                False,
            ),
        ),
        (
            _collect_parsed,
            (
                sources[8], limits.max_crosswalk_download_bytes,
                parse_player_id_crosswalk, captured_at, limits, cancelled, fetcher,
                True,
            ),
        ),
    )
    with ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="public-player-data"
    ) as executor:
        results = tuple(executor.map(_run_collection_job, jobs))
    (current, current_source), (previous, previous_source) = results[:2]
    injury_pairs = results[2:5]
    (players, player_source), (adds, add_source), (drops, drop_source), (
        crosswalk,
        crosswalk_source,
    ) = results[5:]
    trends = _merge_trends(
        adds if add_source.availability is DataAvailability.OBSERVED else {},
        drops if drop_source.availability is DataAvailability.OBSERVED else {},
    )
    return PublicPlayerDataSnapshot(
        season=season,
        captured_at=captured_at,
        current_stats=current,
        previous_stats=previous,
        injury_history=tuple(pair[0] for pair in injury_pairs),
        sleeper_players=players,
        trends=trends,
        id_crosswalk=crosswalk,
        provenance=(
            current_source,
            previous_source,
            *(pair[1] for pair in injury_pairs),
            player_source,
            add_source,
            drop_source,
            crosswalk_source,
        ),
    )


def _run_collection_job(job):
    collector, arguments = job
    return collector(*arguments)


def _collect_stats(
    season, source, captured_at, limits, cancelled, fetcher, is_current, as_of_week
):
    try:
        payload = _fetch(
            source.url, limits.max_stats_download_bytes, limits, cancelled, fetcher
        )
        if payload.status == 404:
            if is_current and as_of_week == 1:
                return SeasonPlayerStats(
                    season, DataAvailability.NOT_PUBLISHED, ()
                ), _empty_provenance(
                    source, captured_at, DataAvailability.NOT_PUBLISHED
                )
            raise PublicPlayerDataError(
                "required nflverse player stats were not published"
            )
        provenance = _provenance(source, payload, captured_at)
        from ._public_player_parse import gunzip_limited, parse_stats_csv

        decoded = gunzip_limited(
            payload.body, limits.max_stats_decoded_bytes, cancelled,
            "nflverse player stats",
        )
        rows = parse_stats_csv(
            decoded, season, limits.max_stat_rows_per_season, cancelled
        )
        if not rows and is_current and as_of_week == 1:
            return SeasonPlayerStats(
                season, DataAvailability.NOT_PUBLISHED, ()
            ), _empty_provenance(
                source, captured_at, DataAvailability.NOT_PUBLISHED
            )
        _require_bulk_coverage(
            rows, "player stats", is_current=is_current, as_of_week=as_of_week
        )
        return SeasonPlayerStats(season, DataAvailability.OBSERVED, rows), provenance
    except PublicPlayerDataCancelled:
        raise
    except PublicPlayerDataError:
        return SeasonPlayerStats(
            season, DataAvailability.UNAVAILABLE, ()
        ), _unavailable_provenance(source, captured_at)


def _collect_injuries(
    season, source, captured_at, limits, cancelled, fetcher, is_current, as_of_week
):
    try:
        payload = _fetch(
            source.url, limits.max_injury_download_bytes, limits, cancelled, fetcher
        )
        if payload.status == 404:
            if is_current and as_of_week == 1:
                return SeasonInjuryReports(
                    season, DataAvailability.NOT_PUBLISHED, ()
                ), _empty_provenance(
                    source, captured_at, DataAvailability.NOT_PUBLISHED
                )
            raise PublicPlayerDataError(
                "required nflverse injury reports were not published"
            )
        provenance = _provenance(source, payload, captured_at)
        from ._public_player_parse import gunzip_limited, parse_injury_csv

        decoded = gunzip_limited(
            payload.body, limits.max_injury_decoded_bytes, cancelled,
            "nflverse injuries",
        )
        rows = parse_injury_csv(
            decoded, season, limits.max_injury_rows_per_season, cancelled
        )
        if not rows and is_current and as_of_week == 1:
            return SeasonInjuryReports(
                season, DataAvailability.NOT_PUBLISHED, ()
            ), _empty_provenance(
                source, captured_at, DataAvailability.NOT_PUBLISHED
            )
        _require_bulk_coverage(
            rows, "injury reports", is_current=is_current, as_of_week=as_of_week
        )
        return SeasonInjuryReports(
            season, DataAvailability.OBSERVED, rows
        ), provenance
    except PublicPlayerDataCancelled:
        raise
    except PublicPlayerDataError:
        return SeasonInjuryReports(
            season, DataAvailability.UNAVAILABLE, ()
        ), _unavailable_provenance(source, captured_at)


def _collect_parsed(
    source,
    max_bytes,
    parser,
    captured_at,
    limits,
    cancelled,
    fetcher,
    require_nonempty,
):
    try:
        payload = _fetch(
            source.url, max_bytes, limits, cancelled, fetcher
        )
        if payload.status == 404:
            raise PublicPlayerDataError(
                "required evergreen player-data source was not published"
            )
        rows = parser(payload, limits, cancelled)
        if require_nonempty and not rows:
            raise PublicPlayerDataError(
                "required evergreen player-data source was empty"
            )
        return (
            rows,
            _provenance(source, payload, captured_at),
        )
    except PublicPlayerDataCancelled:
        raise
    except PublicPlayerDataError:
        return (), _unavailable_provenance(source, captured_at)


def _fetch(url, max_bytes, limits, cancelled, fetcher):
    if cancelled():
        raise PublicPlayerDataCancelled("public player-data collection was cancelled")
    result = fetcher(
        url,
        timeout_seconds=limits.timeout_seconds,
        max_bytes=max_bytes,
        cancelled=cancelled,
    )
    if not isinstance(result, DownloadedPublicData):
        raise PublicPlayerDataError("public player-data fetcher returned invalid data")
    return result


def _merge_trends(adds, drops):
    return tuple(
        SleeperPlayerTrend(player_id, adds.get(player_id), drops.get(player_id))
        for player_id in sorted(set(adds) | set(drops))
    )


def _provenance(source, payload, captured_at):
    headers = payload.header_map
    available = payload.status == 200
    return PublicDataProvenance(
        source.provider,
        source.dataset,
        source.url,
        DataAvailability.OBSERVED if available else DataAvailability.NOT_PUBLISHED,
        captured_at,
        _http_time(headers.get("last-modified")),
        headers.get("etag") or None,
        sha256(payload.body).hexdigest() if available else None,
        len(payload.body),
    )


def _unavailable_provenance(source, captured_at):
    return _empty_provenance(source, captured_at, DataAvailability.UNAVAILABLE)


def _empty_provenance(source, captured_at, availability):
    return PublicDataProvenance(
        source.provider,
        source.dataset,
        source.url,
        availability,
        captured_at,
        None,
        None,
        None,
        0,
    )


def _require_bulk_coverage(rows, label, *, is_current, as_of_week):
    """Reject successful responses too small to represent a season bulk file."""

    weeks = {row.week for row in rows}
    teams = {row.nfl_team_id for row in rows}
    players = {row.gsis_id for row in rows}
    if is_current:
        expected_weeks = (
            max(0, as_of_week - 1) if label == "player stats" else as_of_week
        )
        if expected_weeks == 0:
            return
        minimum_rows = 64
        minimum_players = 32
        minimum_teams = 16
        minimum_weeks = min(4, expected_weeks)
    else:
        minimum_rows = 256 if label == "player stats" else 128
        minimum_players = 64
        minimum_teams = 24
        minimum_weeks = 8
    if (
        len(rows) < minimum_rows
        or len(players) < minimum_players
        or len(teams) < minimum_teams
        or len(weeks) < minimum_weeks
    ):
        raise PublicPlayerDataError(
            f"nflverse {label} response had implausibly incomplete season coverage"
        )


def _http_time(value):
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        raise PublicPlayerDataError(
            "public source Last-Modified header was invalid"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicPlayerDataError("public source Last-Modified header lacked a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = ("collect_public_player_data",)
