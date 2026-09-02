import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trade_snapshot.identity_io import load_identity_registry, save_identity_registry
from trade_snapshot.identity_match import ProviderPlayerRecord, reconcile_player_identities


def row(provider, player_id, name="A.J. Brown", position="WR", team="PHI"):
    return ProviderPlayerRecord(provider, player_id, name, position, team)


class IdentityMatchTests(unittest.TestCase):
    def test_resolves_only_unique_exact_metadata_and_persists_content_addressed_registry(self):
        registry = reconcile_player_identities(
            (row("fantasypros", "101"), row("espn", "202"), row("yahoo", "303"))
        )
        self.assertEqual(len(registry.players), 1)
        self.assertEqual(registry.players[0].canonical_player_id, "fantasypros:101")
        self.assertEqual(len(registry.players[0].provider_references), 3)
        self.assertEqual(registry.unresolved, ())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            save_identity_registry(registry, path)
            self.assertEqual(load_identity_registry(path), registry)
            changed = copy.deepcopy(json.loads(path.read_text("utf-8")))
            changed["registry"]["players"][0]["display_name"] = "Changed"
            path.write_text(json.dumps(changed), "utf-8")
            with self.assertRaisesRegex(ValueError, "does not match registry_id"):
                load_identity_registry(path)

    def test_previous_stable_provider_ids_survive_team_change(self):
        first = reconcile_player_identities(
            (row("fantasypros", "101"), row("espn", "202"))
        )
        second = reconcile_player_identities(
            (
                row("fantasypros", "101", team="DAL"),
                row("espn", "202", team="DAL"),
                row("yahoo", "303", team="DAL"),
            ),
            first,
        )
        player = second.players[0]
        self.assertEqual(player.nfl_team_id, "DAL")
        self.assertEqual(len(player.provider_references), 3)

    def test_ambiguous_or_punctuation_different_rows_remain_explicitly_unresolved(self):
        registry = reconcile_player_identities(
            (
                row("fantasypros", "101", name="Chris Smith"),
                row("fantasypros", "102", name="Chris Smith"),
                row("espn", "201", name="Chris Smith"),
                row("yahoo", "301", name="AJ Brown"),
            )
        )
        self.assertEqual(len(registry.players), 2)
        self.assertEqual(len(registry.unresolved), 2)
        reasons = {item.reason for item in registry.unresolved}
        self.assertIn("exact name/position/team match is ambiguous", reasons)
        self.assertIn("no exact name/position/team anchor match", reasons)

    def test_duplicate_provider_rows_cannot_silently_attach_to_one_player(self):
        registry = reconcile_player_identities(
            (
                row("fantasypros", "101"),
                row("espn", "201"),
                row("espn", "202"),
            )
        )
        self.assertEqual(len(registry.players[0].provider_references), 1)
        self.assertEqual(len(registry.unresolved), 2)
        self.assertTrue(
            all("ambiguous" in item.reason for item in registry.unresolved)
        )

    def test_second_id_for_an_already_mapped_provider_stays_unresolved(self):
        previous = reconcile_player_identities(
            (row("fantasypros", "101"), row("espn", "201"))
        )
        registry = reconcile_player_identities(
            (
                row("fantasypros", "101"),
                row("espn", "201"),
                row("espn", "202"),
            ),
            previous,
        )
        self.assertEqual(len(registry.players[0].provider_references), 2)
        self.assertEqual(len(registry.unresolved), 1)
        self.assertIn("already has an ID", registry.unresolved[0].reason)

    def test_provider_specific_idp_positions_match_the_same_player(self):
        registry = reconcile_player_identities((
            row("fantasypros", "101", name="Safety One", position="DB", team="DET"),
            row("espn", "201", name="Safety One", position="S", team="DET"),
            row("yahoo", "301", name="Safety One", position="SS", team="DET"),
        ))
        self.assertEqual(len(registry.players), 1)
        self.assertEqual(registry.players[0].position, "DB")
        self.assertEqual(len(registry.players[0].provider_references), 3)

    def test_team_defenses_match_uniquely_by_position_and_nfl_team(self):
        registry = reconcile_player_identities((
            row(
                "fantasypros", "houston-texans-defense",
                name="Houston Texans", position="DST", team="HOU",
            ),
            row(
                "espn", "600034", name="Texans D/ST", position="D/ST", team="HOU",
            ),
            row(
                "yahoo", "dst:HOU", name="Texans", position="DEF", team="HOU",
            ),
        ))

        self.assertEqual(len(registry.players), 1)
        self.assertEqual(registry.players[0].display_name, "Houston Texans")
        self.assertEqual(
            {reference.key for reference in registry.players[0].provider_references},
            {
                ("fantasypros", "houston-texans-defense"),
                ("espn", "600034"),
                ("yahoo", "dst:HOU"),
            },
        )
        self.assertEqual(registry.unresolved, ())

    def test_team_defense_matching_remains_ambiguous_with_duplicate_team_anchors(self):
        registry = reconcile_player_identities((
            row("fantasypros", "hou-a", name="Houston A", position="DST", team="HOU"),
            row("fantasypros", "hou-b", name="Houston B", position="DST", team="HOU"),
            row("yahoo", "dst:HOU", name="Texans", position="DST", team="HOU"),
        ))

        self.assertEqual(len(registry.players), 2)
        self.assertEqual(len(registry.unresolved), 1)
        self.assertIn("ambiguous", registry.unresolved[0].reason)


if __name__ == "__main__":
    unittest.main()
