from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
import unittest

from trade_snapshot.identity import (
    IdentityRegistry,
    ManualMappingProvenance,
    PlayerIdentity,
    ProviderReference,
    UnresolvedProviderRecord,
)


class PlayerIdentityTests(unittest.TestCase):
    def test_exact_lookup_and_immutable_defensive_collections(self):
        references = [ProviderReference("espn", "123")]
        player = PlayerIdentity(
            canonical_player_id="nfl-patrick-mahomes",
            display_name="Patrick Mahomes",
            position="QB",
            nfl_team_id="KC",
            provider_references=references,
        )
        registry = IdentityRegistry([player])
        references.append(ProviderReference("yahoo", "999"))

        self.assertIs(registry.lookup("espn", "123"), player)
        self.assertIsNone(registry.lookup("ESPN", "123"))
        self.assertIsNone(registry.lookup("espn", "0123"))
        self.assertEqual(len(player.provider_references), 1)
        with self.assertRaises(FrozenInstanceError):
            player.display_name = "Changed"

    def test_rejects_duplicate_canonical_ids_and_reference_collisions(self):
        first = _player("p1", ProviderReference("espn", "123"))

        with self.assertRaisesRegex(ValueError, "duplicate canonical_player_id"):
            IdentityRegistry([first, _player("p1", ProviderReference("yahoo", "456"))])
        with self.assertRaisesRegex(ValueError, "mapped to multiple"):
            IdentityRegistry([first, _player("p2", ProviderReference("espn", "123"))])
        with self.assertRaisesRegex(ValueError, "duplicate provider reference"):
            PlayerIdentity(
                "p1",
                "Player p1",
                "RB",
                "KC",
                (ProviderReference("espn", "123"), ProviderReference("espn", "123")),
            )

    def test_unresolved_rows_stay_explicit_and_are_never_name_merged(self):
        player = PlayerIdentity(
            "p1",
            "Patrick Mahomes",
            "QB",
            "KC",
            (ProviderReference("espn", "123"),),
        )
        unresolved = UnresolvedProviderRecord(
            provider_reference=ProviderReference("yahoo", "unknown-7"),
            display_name="Patrick Mahomes",
            position="QB",
            nfl_team_id="KC",
            reason="provider identifier has not been audited",
        )
        registry = IdentityRegistry([player], [unresolved])

        self.assertIsNone(registry.lookup("yahoo", "unknown-7"))
        self.assertIs(registry.unresolved_for("yahoo", "unknown-7"), unresolved)
        with self.assertRaisesRegex(ValueError, "both resolved and unresolved"):
            IdentityRegistry(
                [
                    player,
                    _player("p2", ProviderReference("yahoo", "unknown-7")),
                ],
                [unresolved],
            )

    def test_manual_mapping_provenance_and_json_record_round_trip(self):
        mapped_at = datetime(2026, 9, 1, 12, 34, 56, 123456, tzinfo=timezone.utc)
        audit = ManualMappingProvenance(
            mapped_by="league-owner",
            mapped_at=mapped_at,
            evidence="FantasyPros profile and NFL team matched",
        )
        registry = IdentityRegistry(
            [
                PlayerIdentity(
                    "p1",
                    "Player One",
                    "WR",
                    "DAL",
                    (ProviderReference("fantasypros", "7001", audit),),
                )
            ],
            [
                UnresolvedProviderRecord(
                    ProviderReference("yahoo", "y-404"),
                    "Player Unknown",
                    "WR",
                    "FA",
                    "no verified canonical match",
                )
            ],
        )

        record = registry.to_record()
        json.dumps(record, allow_nan=False)
        rebuilt = IdentityRegistry.from_record(record)

        self.assertEqual(rebuilt, registry)
        self.assertEqual(
            rebuilt.lookup("fantasypros", "7001").provider_references[0].manual_mapping,
            audit,
        )
        record["players"][0]["display_name"] = "Mutated"
        manual_record = record["players"][0]["provider_references"][0][
            "manual_mapping"
        ]
        manual_record["evidence"] = "Changed"
        self.assertEqual(registry.players[0].display_name, "Player One")
        self.assertEqual(
            registry.players[0].provider_references[0].manual_mapping.evidence,
            "FantasyPros profile and NFL team matched",
        )

    def test_record_readers_reject_unknown_fields_at_every_level(self):
        registry = IdentityRegistry(
            [_player("p1", ProviderReference("espn", "123", _audit()))],
            [
                UnresolvedProviderRecord(
                    ProviderReference("yahoo", "404"),
                    "Unknown Player",
                    "TE",
                    "FA",
                    "not matched",
                )
            ],
        )
        mutations = (
            lambda record: record.update({"unknown": True}),
            lambda record: record["players"][0].update({"unknown": True}),
            lambda record: record["players"][0]["provider_references"][0].update(
                {"unknown": True}
            ),
            lambda record: record["players"][0]["provider_references"][0][
                "manual_mapping"
            ].update({"unknown": True}),
            lambda record: record["unresolved"][0].update({"unknown": True}),
        )

        for mutate in mutations:
            with self.subTest(mutate=mutate):
                record = registry.to_record()
                mutate(record)
                with self.assertRaisesRegex(ValueError, "missing or unknown"):
                    IdentityRegistry.from_record(record)

        for schema_version in (True, 1.0, 2, "1"):
            with self.subTest(schema_version=schema_version):
                record = registry.to_record()
                record["schema_version"] = schema_version
                with self.assertRaisesRegex(ValueError, "schema_version"):
                    IdentityRegistry.from_record(record)

    def test_rejects_invalid_fields_and_unaudited_manual_shapes(self):
        factories = (
            lambda: ProviderReference("", "1"),
            lambda: ProviderReference("espn", " "),
            lambda: PlayerIdentity("", "Player", "RB", "KC"),
            lambda: PlayerIdentity("p1", "", "RB", "KC"),
            lambda: PlayerIdentity("p1", "Player", "", "KC"),
            lambda: PlayerIdentity("p1", "Player", "RB", ""),
            lambda: ManualMappingProvenance(
                "user", datetime(2026, 9, 1), "matched manually"
            ),
            lambda: ManualMappingProvenance("", datetime.now(timezone.utc), "evidence"),
            lambda: ManualMappingProvenance("user", datetime.now(timezone.utc), ""),
            lambda: UnresolvedProviderRecord(
                ProviderReference("espn", "1", _audit()),
                "Player",
                "RB",
                "KC",
                "still unresolved",
            ),
        )

        for factory in factories:
            with self.subTest(factory=factory):
                with self.assertRaises(ValueError):
                    factory()


def _audit() -> ManualMappingProvenance:
    return ManualMappingProvenance(
        "league-owner",
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        "manually verified",
    )


def _player(player_id: str, reference: ProviderReference) -> PlayerIdentity:
    return PlayerIdentity(
        player_id,
        f"Player {player_id}",
        "RB",
        "KC",
        (reference,),
    )


if __name__ == "__main__":
    unittest.main()
