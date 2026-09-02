from dataclasses import replace
import unittest

from league_ingest_fixtures import complete_snapshot
from trade_snapshot.identity import IdentityRegistry, PlayerIdentity, ProviderReference
from trade_snapshot.identity_match import reconcile_player_identities
from trade_snapshot.league_ingest import (
    CanonicalPlayerProviderId,
    host_player_records,
    ingest_host_league_artifact,
    normalize_host_league_snapshot,
)
from trade_snapshot.league_source import (
    ProviderPlayerId,
    SourceLeaguePlayer,
    SourceTeamRoster,
)
from trade_snapshot.league_state import RosterRules


def identities_for(snapshot):
    return reconcile_player_identities(host_player_records(snapshot))


class LeagueIngestTests(unittest.TestCase):
    def test_normalizes_complete_18_team_league_and_provider_mappings(self):
        snapshot = complete_snapshot()
        result = normalize_host_league_snapshot(snapshot, identities_for(snapshot))

        self.assertEqual(len(result.league_state.teams), 18)
        self.assertEqual(len(result.rosters), 18)
        self.assertEqual(len(result.eligibilities), 18)
        self.assertTrue(result.completed_history_available)
        self.assertTrue(result.league_state.completed_history_is_usable)
        self.assertIs(result.scoring_profile, snapshot.scoring_profile)
        self.assertEqual(result.captured_at, snapshot.captured_at)
        self.assertEqual(result.source_provider, "fantasypros")
        self.assertEqual(
            result.league_state.scoring_profile_id,
            snapshot.scoring_profile.scoring_profile_id,
        )
        self.assertEqual(result.team_ids_for("fantasypros")["fantasypros:team:1"], "1")
        self.assertEqual(result.team_ids_for("espn")["fantasypros:team:1"], "espn-1")
        self.assertEqual(result.player_ids_for("espn")["fantasypros:p1"], "e1")
        self.assertEqual(result.rosters[0].player_ids, ("fantasypros:p1",))

    def test_completed_history_unavailable_is_preserved_without_fabrication(self):
        snapshot = complete_snapshot(completed_history=False)
        result = normalize_host_league_snapshot(snapshot, identities_for(snapshot))

        self.assertFalse(result.completed_history_available)
        self.assertEqual(result.league_state.completed_matchups, ())
        self.assertFalse(result.league_state.completed_history_is_complete)

    def test_full_ir_ownership_normalizes_while_only_active_players_use_cap(self):
        baseline = complete_snapshot(2)
        extra_ids = tuple(f"p-extra-{index}" for index in range(14))
        extras = tuple(
            SourceLeaguePlayer(
                player_id,
                f"Extra Player {index}",
                "RB",
                "ARI",
                ("RB", "FLEX"),
                (
                    ProviderPlayerId("fantasypros", player_id),
                    ProviderPlayerId("espn", f"e-extra-{index}"),
                ),
            )
            for index, player_id in enumerate(extra_ids)
        )
        owned = ("p1", *extra_ids)
        source_roster = SourceTeamRoster(
            "1",
            owned,
            {extra_ids[-1]: "IR"},
        )
        self.assertIsInstance(hash(source_roster), int)
        snapshot = replace(
            baseline,
            players=(*baseline.players, *extras),
            rosters=(source_roster, baseline.rosters[1]),
            roster_rules=RosterRules(14, ("RB",), {"IR": 1}),
        )

        result = normalize_host_league_snapshot(snapshot, identities_for(snapshot))
        normalized = next(
            row for row in result.rosters if row.team_id == "fantasypros:team:1"
        )

        self.assertEqual(len(normalized.player_ids), 15)
        self.assertEqual(normalized.current_size, 15)
        self.assertEqual(normalized.active_size, 14)
        self.assertEqual(
            normalized.capacity_exempt_player_ids,
            frozenset({f"fantasypros:{extra_ids[-1]}"}),
        )
        with self.assertRaisesRegex(ValueError, "exceeds the verified roster cap"):
            replace(
                snapshot,
                rosters=(SourceTeamRoster("1", owned), baseline.rosters[1]),
            )

    def test_every_rostered_player_requires_an_exact_identity(self):
        snapshot = complete_snapshot()
        with self.assertRaisesRegex(ValueError, "not exactly resolved"):
            normalize_host_league_snapshot(snapshot, IdentityRegistry(()))

    def test_all_claimed_host_provider_ids_must_be_verified_by_registry(self):
        snapshot = complete_snapshot()
        identities = IdentityRegistry(tuple(
            PlayerIdentity(
                f"fantasypros:{player.source_player_id}",
                player.display_name,
                player.position,
                player.nfl_team_id,
                (ProviderReference("fantasypros", player.source_player_id),),
            )
            for player in snapshot.players
        ))
        with self.assertRaisesRegex(ValueError, "not verified"):
            normalize_host_league_snapshot(snapshot, identities)

    def test_resolved_identity_position_must_match_host_metadata(self):
        snapshot = complete_snapshot()
        registry = identities_for(snapshot)
        first = registry.players[0]
        changed = replace(first, position="WR")
        mismatched = IdentityRegistry((changed, *registry.players[1:]))
        with self.assertRaisesRegex(ValueError, "position conflicts"):
            normalize_host_league_snapshot(snapshot, mismatched)

    def test_host_player_records_preserve_only_explicit_provider_ids(self):
        snapshot = complete_snapshot(4)
        rows = host_player_records(snapshot)

        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {(row.provider, row.provider_player_id) for row in rows},
            {
                (provider, value)
                for index in range(1, 5)
                for provider, value in (("fantasypros", f"p{index}"), ("espn", f"e{index}"))
            },
        )

    def test_normalized_provider_mapping_must_equal_identity_evidence(self):
        snapshot = complete_snapshot()
        result = normalize_host_league_snapshot(snapshot, identities_for(snapshot))
        first = result.player_provider_ids[0]
        tampered = CanonicalPlayerProviderId(
            first.canonical_player_id, first.provider, "wrong-id"
        )
        with self.assertRaisesRegex(ValueError, "match identity evidence exactly"):
            replace(
                result,
                player_provider_ids=(tampered, *result.player_provider_ids[1:]),
            )

    def test_artifact_adapter_is_narrow_and_expected_count_is_rechecked(self):
        snapshot = complete_snapshot()
        registry = identities_for(snapshot)

        class Adapter:
            def to_host_league_snapshot(self, artifact, *, expected_team_count):
                self.seen = (artifact, expected_team_count)
                return snapshot

        adapter = Adapter()
        result = ingest_host_league_artifact(
            {"volatile": "artifact"}, adapter=adapter, identities=registry,
            expected_team_count=18,
        )
        self.assertEqual(adapter.seen, ({"volatile": "artifact"}, 18))
        self.assertEqual(len(result.league_state.teams), 18)

        with self.assertRaisesRegex(ValueError, "did not honor"):
            ingest_host_league_artifact(
                object(), adapter=adapter, identities=registry, expected_team_count=17,
            )

    def test_adapter_must_return_verified_provider_neutral_snapshot(self):
        class BadAdapter:
            def to_host_league_snapshot(self, artifact, *, expected_team_count):
                return {"teams": []}

        with self.assertRaisesRegex(ValueError, "invalid snapshot type"):
            ingest_host_league_artifact(
                object(), adapter=BadAdapter(), identities=IdentityRegistry(()),
                expected_team_count=18,
            )


if __name__ == "__main__":
    unittest.main()
