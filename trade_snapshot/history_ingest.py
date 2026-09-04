"""Canonical, privacy-minimal bridge from ESPN activity to local history."""

from datetime import datetime, timezone
from hashlib import sha256

from .engine_bundle import EngineBundle
from .espn_activity import (
    EspnActivityCapture,
    EspnActivityKind,
    EspnTransactionAssetKind,
)
from .espn_league import espn_lineup_slot_name
from .league_history import (
    HistoryBundleBinding,
    HISTORY_CAPTURE_BINDING_TOLERANCE,
    HistoryAcquisitionEvidence,
    HistoryAcquisitionOutcome,
    HistoryRosterPlayer,
    HistorySkipCount,
    HistoryTeam,
    HistoryTeamRoster,
    HistoryTimestampBasis,
    HistoryTransaction,
    HistoryTransactionAsset,
    HistoryTransactionAssetKind,
    HistoryTransactionKind,
    LeagueHistoryCapture,
)
from .source_manifest import LeagueBindingScope
from .weekly_assembly import AssembledWeeklyEvidence


_KINDS = {
    EspnActivityKind.TRADE: HistoryTransactionKind.TRADE,
    EspnActivityKind.WAIVER: HistoryTransactionKind.WAIVER,
    EspnActivityKind.FREE_AGENT: HistoryTransactionKind.FREE_AGENT,
}
_ASSET_KINDS = {
    EspnTransactionAssetKind.PLAYER: HistoryTransactionAssetKind.PLAYER,
    EspnTransactionAssetKind.UNSUPPORTED_NON_PLAYER:
        HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER,
}


def canonicalize_espn_history(
    source: EspnActivityCapture,
    assembled: AssembledWeeklyEvidence,
    bundle: EngineBundle,
    *,
    bundle_captured_at: datetime,
) -> tuple[LeagueHistoryCapture, HistoryBundleBinding]:
    """Resolve provider IDs and discard the raw league identifier."""

    if not isinstance(source, EspnActivityCapture):
        raise ValueError("source must be an EspnActivityCapture")
    if not isinstance(assembled, AssembledWeeklyEvidence):
        raise ValueError("assembled must be AssembledWeeklyEvidence")
    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    bound_at = _aware(bundle_captured_at)
    league = assembled.league_inputs
    manifest = bundle.source_manifest
    if (
        league.source_provider != "espn"
        or league.source_league_id != source.source_league_id
        or league.league_state.season != source.season
        or bundle.state.season != source.season
        or manifest.host_provider != "espn"
        or manifest.league_binding_scope is not LeagueBindingScope.WORKSPACE
        or getattr(league.league_state, "snapshot_id", None)
        != manifest.host_snapshot_id
        or getattr(league, "captured_at", None) != manifest.host_captured_at
        or source.captured_at != manifest.host_captured_at
        or source.captured_at > bound_at
        or bound_at - source.captured_at > HISTORY_CAPTURE_BINDING_TOLERANCE
    ):
        raise ValueError("ESPN activity does not match the assembled weekly bundle")

    team_by_source = {
        provider_id: canonical_id
        for canonical_id, provider_id in league.team_ids_for("espn").items()
    }
    bundle_team_names = {team.team_id: team.name for team in bundle.state.teams}
    if set(team_by_source.values()) != set(bundle_team_names):
        raise ValueError("ESPN activity team mapping does not cover the bundle")

    def team_id(source_id):
        if source_id is None:
            return None
        try:
            return team_by_source[source_id]
        except KeyError:
            raise ValueError("ESPN activity references an unknown team") from None

    def player_id(source_id):
        identity = assembled.identities.lookup("espn", source_id)
        return None if identity is None else identity.canonical_player_id

    teams = tuple(
        HistoryTeam(canonical_id, bundle_team_names[canonical_id])
        for canonical_id in sorted(bundle_team_names)
    )
    rosters = []
    for source_roster in source.rosters:
        canonical_team = team_id(source_roster.source_team_id)
        players = []
        for entry in source_roster.entries:
            canonical_player = player_id(entry.source_player_id)
            if canonical_player is None:
                raise ValueError("a current ESPN roster player is not exactly resolved")
            slot = espn_lineup_slot_name(entry.lineup_slot_id)
            players.append(
                HistoryRosterPlayer(
                    canonical_player,
                    slot,
                    entry.injury_status,
                )
            )
        rosters.append(HistoryTeamRoster(canonical_team, tuple(players)))

    canonical_ownership = {
        row.team_id: frozenset(player.canonical_player_id for player in row.players)
        for row in rosters
    }
    bundle_ownership = {
        row.team_id: frozenset(row.player_ids) for row in bundle.rosters
    }
    assembled_rosters = getattr(league, "rosters", None)
    assembled_ownership = (
        None
        if assembled_rosters is None
        else {row.team_id: frozenset(row.player_ids) for row in assembled_rosters}
    )
    if canonical_ownership != bundle_ownership or (
        assembled_ownership is not None
        and canonical_ownership != assembled_ownership
    ):
        raise ValueError(
            "ESPN activity rosters do not exactly match the selected weekly bundle"
        )

    transactions = []
    for event in source.transactions:
        transactions.append(
            HistoryTransaction(
                transaction_id=_source_transaction_key(
                    manifest.league_binding_id, event
                ),
                recorded_at=event.proposed_at,
                timestamp_basis=HistoryTimestampBasis.ESPN_PROPOSED_DATE,
                effective_week=event.scoring_period_id,
                kind=_KINDS[event.kind],
                assets=tuple(
                    HistoryTransactionAsset(
                        index,
                        (
                            player_id(item.source_player_id)
                            if item.asset_kind is EspnTransactionAssetKind.PLAYER
                            else None
                        ),
                        team_id(item.from_source_team_id),
                        team_id(item.to_source_team_id),
                        _source_asset_key(
                            manifest.league_binding_id, event, index, item
                        ),
                        _ASSET_KINDS[item.asset_kind],
                    )
                    for index, item in enumerate(event.items)
                ),
                bid_amount=event.bid_amount,
                accepted_at=event.accepted_at,
                processed_at=event.processed_at,
                expires_at=event.expires_at,
            )
        )

    league_key = manifest.league_binding_id
    acquisition = HistoryAcquisitionEvidence(
        provider="espn",
        attempted_at=source.captured_at,
        outcome=(
            HistoryAcquisitionOutcome.CAPTURED_COMPLETE
            if source.transactions_complete
            else HistoryAcquisitionOutcome.CAPTURED_PARTIAL
        ),
        completeness_policy="espn_executed_supported_transactions_v2",
        normalized_transaction_count=len(transactions),
        returned_transaction_count=source.returned_transaction_count,
        transaction_limit=source.transaction_limit,
        earliest_source_event_at=source.earliest_returned_proposed_at,
        latest_source_event_at=source.latest_returned_proposed_at,
        skipped=tuple(
            HistorySkipCount(row.reason.value, row.count)
            for row in source.skipped_transactions
        ),
    )
    capture = LeagueHistoryCapture(
        league_key=league_key,
        season=source.season,
        captured_at=source.captured_at,
        coverage_start=datetime(source.season, 1, 1, tzinfo=timezone.utc),
        coverage_end=source.captured_at,
        transaction_history_complete=source.transactions_complete,
        roster_complete=True,
        lineup_complete=True,
        teams=teams,
        transactions=tuple(transactions),
        rosters=tuple(rosters),
        host_snapshot_id=manifest.host_snapshot_id,
        acquisition_evidence=acquisition,
    )
    binding = HistoryBundleBinding(
        league_key,
        source.season,
        bundle.bundle_id,
        bound_at,
        manifest.host_snapshot_id,
        manifest.host_captured_at,
        capture.capture_id,
        capture.roster_ownership_id,
    )
    return capture, binding


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("bundle_captured_at must be timezone aware")
    return value.astimezone(timezone.utc)


def _source_asset_key(league_binding_id, event, index, item):
    """Pseudonymize provider asset identity before it crosses history storage."""

    payload = "\0".join(
        (
            "espn",
            league_binding_id,
            event.source_transaction_id,
            str(index),
            item.asset_kind.value,
            item.source_player_id,
        )
    ).encode("utf-8")
    return f"source_asset_{sha256(payload).hexdigest()}"


def _source_transaction_key(league_binding_id, event):
    payload = "\0".join(
        ("espn", league_binding_id, event.source_transaction_id)
    ).encode("utf-8")
    return f"espn_event_{sha256(payload).hexdigest()}"


__all__ = ("canonicalize_espn_history",)
