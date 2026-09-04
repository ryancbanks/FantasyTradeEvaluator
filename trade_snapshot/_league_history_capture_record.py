"""Record encoding and ownership identity for league-history captures."""

from __future__ import annotations

from typing import Mapping

from ._scenario_random import content_id
from ._league_history_acquisition import HistoryAcquisitionEvidence


_LEGACY_CAPTURE_ID_VERSION = 1


def capture_identity_record(capture) -> dict[str, object]:
    from .league_history import LEAGUE_HISTORY_SCHEMA_VERSION

    if capture.identity_schema_version == LEAGUE_HISTORY_SCHEMA_VERSION:
        return capture_content_record(capture)
    return _legacy_identity_record(capture)


def capture_content_record(capture) -> dict[str, object]:
    from .league_history import LEAGUE_HISTORY_SCHEMA_VERSION

    return {
        **_legacy_identity_record(capture),
        "schema_version": LEAGUE_HISTORY_SCHEMA_VERSION,
        "identity_schema_version": capture.identity_schema_version,
        "host_snapshot_id": capture.host_snapshot_id,
        "roster_ownership_id": capture.roster_ownership_id,
        "acquisition_evidence": capture.acquisition_evidence.to_record(),
    }


def capture_from_record(capture_type, value: object):
    from .league_history import (
        HistoryTeam,
        HistoryTeamRoster,
        HistoryTransaction,
        _array,
        _datetime_from_record,
    )

    legacy_fields = {
        "schema_version",
        "league_key",
        "season",
        "captured_at",
        "coverage_start",
        "coverage_end",
        "transaction_history_complete",
        "roster_complete",
        "lineup_complete",
        "teams",
        "transactions",
        "rosters",
        "capture_id",
    }
    current_fields = legacy_fields | {
        "acquisition_evidence",
        "host_snapshot_id",
        "identity_schema_version",
        "roster_ownership_id",
    }
    if not isinstance(value, Mapping):
        raise ValueError("league history capture fields are invalid")
    if set(value) == legacy_fields and value.get("schema_version") == 1:
        acquisition = None
        host_snapshot_id = None
        identity_schema_version = 1
        expected_roster_ownership_id = None
    elif set(value) == current_fields and value.get("schema_version") == 2:
        acquisition = HistoryAcquisitionEvidence.from_record(
            value["acquisition_evidence"]
        )
        host_snapshot_id = value["host_snapshot_id"]
        identity_schema_version = value["identity_schema_version"]
        expected_roster_ownership_id = value["roster_ownership_id"]
    else:
        raise ValueError("league history capture schema version is unsupported")
    result = capture_type(
        league_key=value["league_key"],
        season=value["season"],
        captured_at=_datetime_from_record("captured_at", value["captured_at"]),
        coverage_start=_datetime_from_record(
            "coverage_start", value["coverage_start"]
        ),
        coverage_end=_datetime_from_record("coverage_end", value["coverage_end"]),
        transaction_history_complete=value["transaction_history_complete"],
        roster_complete=value["roster_complete"],
        lineup_complete=value["lineup_complete"],
        teams=tuple(
            HistoryTeam.from_record(item)
            for item in _array("history teams", value["teams"])
        ),
        transactions=tuple(
            HistoryTransaction.from_record(item)
            for item in _array("history transactions", value["transactions"])
        ),
        rosters=tuple(
            HistoryTeamRoster.from_record(item)
            for item in _array("history rosters", value["rosters"])
        ),
        host_snapshot_id=host_snapshot_id,
        acquisition_evidence=acquisition,
        identity_schema_version=identity_schema_version,
    )
    if value["capture_id"] != result.capture_id:
        raise ValueError("league history capture does not match capture_id")
    if (
        expected_roster_ownership_id is not None
        and expected_roster_ownership_id != result.roster_ownership_id
    ):
        raise ValueError("league history capture roster ownership is invalid")
    return result


def roster_ownership_id(rosters) -> str:
    return content_id(
        "history-roster",
        {
            "teams": [
                {
                    "player_ids": [
                        player.canonical_player_id for player in roster.players
                    ],
                    "team_id": roster.team_id,
                }
                for roster in rosters
            ]
        },
    )


def _legacy_identity_record(capture) -> dict[str, object]:
    from .league_history import _timestamp

    return {
        "schema_version": _LEGACY_CAPTURE_ID_VERSION,
        "league_key": capture.league_key,
        "season": capture.season,
        "captured_at": _timestamp(capture.captured_at),
        "coverage_start": _timestamp(capture.coverage_start),
        "coverage_end": _timestamp(capture.coverage_end),
        "transaction_history_complete": capture.transaction_history_complete,
        "roster_complete": capture.roster_complete,
        "lineup_complete": capture.lineup_complete,
        "teams": [row.to_record() for row in capture.teams],
        "transactions": [row.to_record() for row in capture.transactions],
        "rosters": [row.to_record() for row in capture.rosters],
    }


__all__ = (
    "capture_content_record",
    "capture_from_record",
    "capture_identity_record",
    "roster_ownership_id",
)
