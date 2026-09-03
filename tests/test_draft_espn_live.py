from copy import deepcopy
from dataclasses import replace
import unittest

from tests.draft_fixtures import small_historical_corpus
from trade_snapshot.draft_assistant import drafter_for_pick
from trade_snapshot.draft_espn_live import (
    EspnDraftSyncError,
    EspnPublicDraftAdapter,
)
from trade_snapshot.draft_history import DraftPlayerBoard
from trade_snapshot.espn_free_read import EspnFreeReadError, EspnUnauthorizedError


_TEAM_ORDER = (40, 20, 10, 30)


def current_board(*, omit_mapping_for=()):
    players = tuple(
        replace(player, player_id=f"current-{player.player_id}", actual_weeks=())
        for player in small_historical_corpus().seasons[0].players
    )
    canonical_ids = sorted(player.player_id for player in players)
    provider_ids = {
        player_id: ("-16001" if index == 0 else str(10_000 + index))
        for index, player_id in enumerate(canonical_ids)
        if player_id not in omit_mapping_for
    }
    return DraftPlayerBoard(
        2026,
        "2026-08-01T00:00:00+00:00",
        "2026-09-01T00:00:00+00:00",
        players,
        provider_ids,
    )


def draft_payload(board, *, pick_count=5, draft_type="SNAKE"):
    canonical_ids = sorted(player.player_id for player in board.players)
    picks = []
    for overall in range(1, pick_count + 1):
        drafter = drafter_for_pick(overall, len(_TEAM_ORDER))
        picks.append({
            "overallPickNumber": overall,
            "roundId": (overall - 1) // len(_TEAM_ORDER) + 1,
            "roundPickNumber": (overall - 1) % len(_TEAM_ORDER) + 1,
            "teamId": _TEAM_ORDER[drafter - 1],
            "playerId": int(board.espn_player_ids[canonical_ids[overall - 1]]),
            "keeper": False,
            "reservedForKeeper": False,
            "bidAmount": 0,
        })
    return {
        "id": 123,
        "seasonId": 2026,
        "settings": {"draftSettings": {
            "type": draft_type,
            "pickOrder": list(_TEAM_ORDER),
            "keeperCount": 0,
            "keeperCountFuture": 0,
        }},
        "draftDetail": {
            "drafted": pick_count == 20,
            "inProgress": 0 < pick_count < 20,
            "picks": picks,
        },
    }


class EspnPublicDraftAdapterTests(unittest.TestCase):
    def test_maps_an_untraded_snake_draft_in_explicit_pick_order(self):
        board = current_board()
        payload = draft_payload(board)
        payload["draftDetail"]["picks"].reverse()
        calls = []

        def read_draft(season, league_id, cancelled):
            calls.append((season, league_id, cancelled()))
            return payload

        observed = EspnPublicDraftAdapter(read_draft=read_draft).poll(
            league_id="123", season=2026, board=board, team_count=4, roster_size=5
        )

        canonical_ids = sorted(player.player_id for player in board.players)
        self.assertEqual(observed.assistant_picks, (
            (1, canonical_ids[0]),
            (2, canonical_ids[1]),
            (3, canonical_ids[2]),
            (4, canonical_ids[3]),
            (4, canonical_ids[4]),
        ))
        self.assertEqual(observed.team_order, tuple(str(value) for value in _TEAM_ORDER))
        self.assertEqual(calls, [(2026, "123", False)])
        self.assertFalse(observed.drafted)
        self.assertTrue(observed.in_progress)

    def test_empty_not_started_draft_is_valid_when_order_is_known(self):
        board = current_board()
        observed = EspnPublicDraftAdapter(
            read_draft=lambda *_args: draft_payload(board, pick_count=0)
        ).poll(league_id="123", season=2026, board=board, team_count=4, roster_size=5)
        self.assertEqual(observed.assistant_picks, ())
        self.assertFalse(observed.drafted)
        self.assertFalse(observed.in_progress)

    def test_explicit_access_denial_never_falls_back_to_credentials(self):
        board = current_board()

        def denied(*_args):
            raise EspnUnauthorizedError("denied")

        with self.assertRaisesRegex(EspnDraftSyncError, "public.*private|private.*public"):
            EspnPublicDraftAdapter(read_draft=denied).poll(
                league_id="123", season=2026, board=board, team_count=4, roster_size=5
            )

        with self.assertRaisesRegex(EspnDraftSyncError, "public.*private|private.*public"):
            EspnPublicDraftAdapter(
                read_draft=lambda *_args: {"messages": ["You are not authorized"]}
            ).poll(
                league_id="123", season=2026, board=board,
                team_count=4, roster_size=5,
            )

        def unavailable(*_args):
            raise EspnFreeReadError("unavailable")

        with self.assertRaisesRegex(EspnDraftSyncError, "ID.*season.*public"):
            EspnPublicDraftAdapter(read_draft=unavailable).poll(
                league_id="123", season=2026, board=board, team_count=4, roster_size=5
            )

    def test_rejects_auction_offline_and_keeper_drafts(self):
        board = current_board()
        for draft_type in ("AUCTION", "OFFLINE"):
            with self.subTest(draft_type=draft_type), self.assertRaisesRegex(
                EspnDraftSyncError, "snake"
            ):
                EspnPublicDraftAdapter(
                    read_draft=lambda *_args, value=draft_type: draft_payload(
                        board, draft_type=value
                    )
                ).poll(
                    league_id="123", season=2026, board=board,
                    team_count=4, roster_size=5,
                )

        keepers = draft_payload(board)
        keepers["settings"]["draftSettings"]["keeperCount"] = 1
        with self.assertRaisesRegex(EspnDraftSyncError, "keeper"):
            EspnPublicDraftAdapter(read_draft=lambda *_args: keepers).poll(
                league_id="123", season=2026, board=board, team_count=4, roster_size=5
            )

    def test_rejects_ambiguous_or_nonstandard_pick_order(self):
        board = current_board()
        base = draft_payload(board)
        cases = []

        wrong_team = deepcopy(base)
        wrong_team["draftDetail"]["picks"][0]["teamId"] = 20
        cases.append((wrong_team, "untraded snake order"))

        duplicate_team = deepcopy(base)
        duplicate_team["settings"]["draftSettings"]["pickOrder"][-1] = 40
        cases.append((duplicate_team, "ambiguous"))

        missing_pick = deepcopy(base)
        del missing_pick["draftDetail"]["picks"][1]
        cases.append((missing_pick, "contiguous"))

        wrong_round = deepcopy(base)
        wrong_round["draftDetail"]["picks"][4]["roundId"] = 1
        cases.append((wrong_round, "round"))

        traded_bid = deepcopy(base)
        traded_bid["draftDetail"]["picks"][0]["bidAmount"] = 5
        cases.append((traded_bid, "auction"))

        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                EspnDraftSyncError, message
            ):
                EspnPublicDraftAdapter(read_draft=lambda *_args, row=payload: row).poll(
                    league_id="123", season=2026, board=board,
                    team_count=4, roster_size=5,
                )

    def test_rejects_unmapped_players_and_source_mismatches(self):
        complete_board = current_board()
        first_player = sorted(player.player_id for player in complete_board.players)[0]
        incomplete_board = current_board(omit_mapping_for={first_player})
        payload = draft_payload(complete_board, pick_count=1)
        with self.assertRaisesRegex(EspnDraftSyncError, "-16001.*not mapped"):
            EspnPublicDraftAdapter(read_draft=lambda *_args: payload).poll(
                league_id="123", season=2026, board=incomplete_board,
                team_count=4, roster_size=5,
            )

        wrong_league = draft_payload(complete_board)
        wrong_league["id"] = 124
        with self.assertRaisesRegex(EspnDraftSyncError, "different league"):
            EspnPublicDraftAdapter(read_draft=lambda *_args: wrong_league).poll(
                league_id="123", season=2026, board=complete_board,
                team_count=4, roster_size=5,
            )

        with self.assertRaisesRegex(ValueError, "board season"):
            EspnPublicDraftAdapter(read_draft=lambda *_args: payload).poll(
                league_id="123", season=2025, board=complete_board,
                team_count=4, roster_size=5,
            )


if __name__ == "__main__":
    unittest.main()
