from dataclasses import replace
from datetime import datetime, timedelta, timezone
import copy
import json
import math
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot._scenario_random import content_id
from trade_snapshot.identity import ManualMappingProvenance, ProviderReference
from trade_snapshot.identity import IdentityRegistry, PlayerIdentity
from trade_snapshot.engine_bundle import EngineBundle
from trade_snapshot.player_outlook import build_player_outlook
from trade_snapshot.player_profile_outlook import profile_catalog_record, profile_record
from trade_snapshot.player_profile_materialize import (
    _crosswalk_components,
    PlayerProfileMaterializationError,
    materialize_player_profiles,
)
from trade_snapshot.player_profiles import (
    PlayerAvailabilityEvent,
    PlayerAvailabilitySeason,
    PlayerGameStats,
    PlayerProfile,
    PlayerProfileProvenance,
    PlayerProfileSnapshot,
)
from trade_snapshot.public_player_data import (
    DataAvailability,
    PlayerInjuryReport,
    PlayerWeekStats,
    PublicDataProvenance,
    PublicPlayerDataSnapshot,
    PublicPlayerIdCrosswalk,
    SeasonInjuryReports,
    SeasonPlayerStats,
    SleeperPlayerMetadata,
    SleeperPlayerTrend,
    public_player_source_urls,
)


NOW = datetime(2026, 9, 2, 18, tzinfo=timezone.utc)


def _game(player_week: int = 1) -> PlayerGameStats:
    return PlayerGameStats(
        season=2026,
        week=player_week,
        game_id=f"game-{player_week}",
        nfl_team_id="NFL-P1",
        opponent_team_id="OPP-P1",
        fantasy_points_standard=10 + player_week,
        fantasy_points_ppr=12 + player_week,
        stat_values={"receptions": 2, "receiving_yards": 50 + player_week},
    )


def _profile(player_id: str = "p1") -> PlayerProfile:
    is_primary = player_id == "p1"
    bundle = engine_bundle()
    calculation_row = next(
        (
            row
            for row in bundle.projections
            if row.canonical_player_id == player_id
        ),
        None,
    )
    position = "RB" if calculation_row is None else calculation_row.position
    nfl_team_id = (
        f"NFL-{player_id}" if calculation_row is None else calculation_row.nfl_team_id
    )
    return PlayerProfile(
        canonical_player_id=player_id,
        display_name=bundle.player_names.get(player_id, player_id.upper()),
        position=position,
        nfl_team_id=nfl_team_id,
        provider_references=(ProviderReference("espn", f"espn-{player_id}"),),
        fantasy_positions=(position,),
        active=True,
        status="Active",
        injury_status=None,
        injury_body_part=None,
        practice_participation=None,
        depth_chart_position="RB",
        depth_chart_order=1,
        years_experience=3,
        jersey_number=22,
        headshot_url="https://example.test/p1.png",
        adds=40,
        drops=5,
        current_season_stats=(),
        previous_season_stats=(
            replace(_game(1), season=2025, fantasy_points_ppr=9.0),
        ) if is_primary else (),
        availability_history=(
            PlayerAvailabilityEvent(
                season=2025,
                week=6,
                nfl_team_id="NFL-P1",
                report_primary_injury="hamstring",
                report_secondary_injury=None,
                report_status="out",
                practice_primary_injury="hamstring",
                practice_secondary_injury=None,
                practice_status="did_not_participate",
                source_modified_at=NOW,
            ),
        ) if is_primary else (),
    )


def profile_snapshot(*player_ids: str) -> PlayerProfileSnapshot:
    selected = player_ids or tuple(engine_bundle().player_names)
    return PlayerProfileSnapshot(
        league_snapshot_id="snapshot-1",
        season=2026,
        as_of_week=1,
        captured_at=NOW,
        identity_registry_id="identity_" + "1" * 64,
        source_data_id="public_player_data_" + "2" * 64,
        current_stats_availability="observed",
        previous_stats_availability="observed",
        players=tuple(_profile(player_id) for player_id in selected),
        provenance=(
            PlayerProfileProvenance(
                provider="nflverse",
                dataset="weekly_player_stats",
                source_url="https://example.test/stats.csv",
                captured_at=NOW,
                source_updated_at=None,
                etag=None,
                status="observed",
                content_sha256="a" * 64,
                byte_count=123,
            ),
        ),
        injury_history_availability=(
            PlayerAvailabilitySeason(2026, "observed"),
            PlayerAvailabilitySeason(2025, "observed"),
            PlayerAvailabilitySeason(2024, "not_published"),
        ),
    )


def _public_stats_row(
    gsis_id: str = "g1", *, week: int = 1, game_id: str | None = None
) -> PlayerWeekStats:
    return PlayerWeekStats(
        gsis_id=gsis_id,
        display_name="Public Player",
        position="RB",
        season=2026,
        week=week,
        game_id=game_id or f"2026_{week}_KC_LV",
        nfl_team_id="KC",
        opponent_team_id="LV",
        headshot_url="https://static.www.nfl.com/image/private/test.png",
        fantasy_points_standard=10.0 + week,
        fantasy_points_ppr=12.0 + week,
        stat_values=(("receptions", 2.0), ("receiving_yards", 40.0)),
    )


def _sleeper_player(
    player_id: str = "s1", *, gsis_id: str | None = "g1",
    espn_id: str | None = "101", nfl_team_id: str | None = "KC"
) -> SleeperPlayerMetadata:
    return SleeperPlayerMetadata(
        sleeper_player_id=player_id,
        gsis_id=gsis_id,
        espn_id=espn_id,
        display_name="Public Player",
        position="RB",
        fantasy_positions=("RB", "FLEX"),
        nfl_team_id=nfl_team_id,
        active=True,
        status="Active",
        injury_status="Questionable",
        injury_body_part="Hamstring",
        practice_participation="Limited",
        depth_chart_position="RB",
        depth_chart_order=1,
        years_experience=3,
        jersey_number=22,
        news_updated_ms=1,
    )


def _public_data(
    *,
    sleeper_players=None,
    current_rows=None,
    injury_rows=None,
    id_crosswalk=(),
) -> PublicPlayerDataSnapshot:
    sources = public_player_source_urls(2026)
    provenance = tuple(
        PublicDataProvenance(
            provider=row.provider,
            dataset=row.dataset,
            requested_url=row.url,
            availability=DataAvailability.OBSERVED,
            captured_at=NOW,
            source_updated_at=None,
            etag=None,
            content_sha256=f"{index:x}" * 64,
            byte_count=100 + index,
        )
        for index, row in enumerate(sources, start=1)
    )
    return PublicPlayerDataSnapshot(
        season=2026,
        captured_at=NOW,
        current_stats=SeasonPlayerStats(
            2026, DataAvailability.OBSERVED,
            tuple(current_rows if current_rows is not None else (_public_stats_row(),)),
        ),
        previous_stats=SeasonPlayerStats(2025, DataAvailability.OBSERVED, ()),
        injury_history=(
            SeasonInjuryReports(
                2026,
                DataAvailability.OBSERVED,
                tuple(injury_rows if injury_rows is not None else ()),
            ),
            SeasonInjuryReports(2025, DataAvailability.OBSERVED, ()),
            SeasonInjuryReports(2024, DataAvailability.OBSERVED, ()),
        ),
        sleeper_players=tuple(
            sleeper_players
            if sleeper_players is not None
            else (_sleeper_player(), _sleeper_player("999", gsis_id=None, espn_id=None))
        ),
        trends=(SleeperPlayerTrend("s1", 20, 3),),
        id_crosswalk=tuple(id_crosswalk),
        provenance=provenance,
    )


def _identity_registry(*, conflict: bool = False) -> IdentityRegistry:
    players = [
        PlayerIdentity(
            "p1", "P1", "RB", "KC", (ProviderReference("espn", "101"),)
        )
    ]
    if conflict:
        players.append(
            PlayerIdentity(
                "p2", "P2", "RB", "KC", (ProviderReference("gsis", "g1"),)
            )
        )
    return IdentityRegistry(tuple(players))


class PlayerProfileDomainTests(unittest.TestCase):
    def test_crosswalk_components_are_deterministic_for_large_disjoint_catalog(self):
        rows = tuple(
            PublicPlayerIdCrosswalk(
                gsis_id=f"g{index:05d}",
                espn_id=f"e{index:05d}",
                sleeper_id=f"s{index:05d}",
            )
            for index in range(4_000)
        )

        forward = _crosswalk_components(rows)
        reverse = _crosswalk_components(tuple(reversed(rows)))

        self.assertEqual(forward, reverse)
        self.assertEqual(len(forward), len(rows))
        self.assertEqual(
            forward[0],
            (("espn", "e00000"), ("gsis", "g00000"), ("sleeper", "s00000")),
        )

    def test_profile_snapshot_enforces_as_of_boundaries_during_import(self):
        snapshot = profile_snapshot("p1")
        player = snapshot.players[0]
        current_stat = replace(_game(1), season=snapshot.season)
        with self.assertRaisesRegex(ValueError, "precede.*as-of"):
            replace(
                snapshot,
                players=(replace(player, current_season_stats=(current_stat,)),),
            )

        future_availability = PlayerAvailabilityEvent(
            season=snapshot.season,
            week=snapshot.as_of_week + 1,
            nfl_team_id="NFL-P1",
            report_primary_injury="Ankle",
            report_secondary_injury=None,
            report_status="out",
            practice_primary_injury="Ankle",
            practice_secondary_injury=None,
            practice_status="did_not_participate",
            source_modified_at=NOW,
        )
        record = snapshot.to_record()
        record["players"][0]["availability_history"].append(
            future_availability.to_record()
        )
        with self.assertRaisesRegex(ValueError, "must not follow.*as-of"):
            PlayerProfileSnapshot.from_record(record)

    def test_profile_history_supports_the_two_year_window_at_2012_boundary(self):
        season = PlayerAvailabilitySeason(2010, "not_published")
        event = PlayerAvailabilityEvent(
            season=2010,
            week=1,
            nfl_team_id="LAC",
            report_primary_injury="Ankle",
            report_secondary_injury=None,
            report_status="questionable",
            practice_primary_injury="Ankle",
            practice_secondary_injury=None,
            practice_status="limited",
            source_modified_at=NOW,
        )

        self.assertEqual(season.season, 2010)
        self.assertEqual(event.season, 2010)

    def test_source_update_time_does_not_depend_on_the_local_clock(self):
        provenance = PlayerProfileProvenance(
            provider="nflverse",
            dataset="weekly_player_stats",
            source_url="https://example.test/stats.csv",
            captured_at=NOW,
            source_updated_at=NOW + timedelta(minutes=5),
            etag=None,
            status="observed",
            content_sha256="a" * 64,
            byte_count=123,
        )

        self.assertGreater(provenance.source_updated_at, provenance.captured_at)

    def test_portable_availability_and_provenance_states_are_coherent(self):
        snapshot = profile_snapshot("p1")
        observed = snapshot.provenance[0]

        for field in ("current_stats_availability", "previous_stats_availability"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "availability"):
                    replace(snapshot, **{field: "available"})
        with self.assertRaisesRegex(ValueError, "status"):
            replace(observed, status="available")
        with self.assertRaisesRegex(ValueError, "observed.*bytes"):
            replace(observed, content_sha256=None)
        with self.assertRaisesRegex(ValueError, "unobserved.*bytes"):
            replace(observed, status="not_published")

        unavailable = replace(
            observed,
            status="unavailable",
            content_sha256=None,
            byte_count=0,
        )
        self.assertEqual(unavailable.status, "unavailable")
        with self.assertRaisesRegex(ValueError, "availability"):
            PlayerAvailabilitySeason(2026, "available")

    def test_snapshot_is_canonical_content_addressed_and_strict_json(self):
        snapshot = profile_snapshot("p2", "p1")
        record = snapshot.to_record()

        self.assertEqual(record["schema_version"], 1)
        self.assertEqual([row["canonical_player_id"] for row in record["players"]], ["p1", "p2"])
        self.assertTrue(snapshot.profile_snapshot_id.startswith("profiles_"))
        self.assertEqual(PlayerProfileSnapshot.from_record(record), snapshot)
        json.dumps(record, allow_nan=False)

        tampered = copy.deepcopy(record)
        tampered["players"][0]["display_name"] = "Changed"
        with self.assertRaisesRegex(ValueError, "does not match profile_snapshot_id"):
            PlayerProfileSnapshot.from_record(tampered)

        unknown = copy.deepcopy(record)
        unknown["cookie"] = "secret"
        with self.assertRaisesRegex(ValueError, "fields"):
            PlayerProfileSnapshot.from_record(unknown)

        boolean_version = copy.deepcopy(record)
        boolean_version["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema version"):
            PlayerProfileSnapshot.from_record(boolean_version)

    def test_profile_rejects_duplicate_games_and_nonfinite_stats(self):
        with self.assertRaisesRegex(ValueError, "duplicate current-season player week"):
            replace(_profile(), current_season_stats=(_game(1), _game(1)))
        with self.assertRaisesRegex(ValueError, "finite"):
            replace(_game(), stat_values={"receptions": math.nan})

    def test_bundle_embeds_profiles_without_narrowing_the_profile_universe(self):
        bundle = engine_bundle()
        required_ids = tuple(bundle.player_names)
        attached = replace(
            bundle,
            player_profiles=profile_snapshot(*required_ids, "outside-calculation-pool"),
        )

        record = attached.to_record()
        restored = EngineBundle.from_record(record)

        self.assertEqual(record["schema_version"], 10)
        self.assertEqual(restored, attached)
        self.assertIn(
            "outside-calculation-pool",
            restored.player_profiles.players_by_id,
        )
        self.assertNotEqual(bundle.bundle_id, attached.bundle_id)

    def test_bundle_rejects_wrong_context_or_missing_calculation_player(self):
        bundle = engine_bundle()
        complete = profile_snapshot(*bundle.player_names)
        with self.assertRaisesRegex(ValueError, "league snapshot"):
            replace(
                bundle,
                player_profiles=replace(complete, league_snapshot_id="wrong"),
            )
        missing = tuple(player_id for player_id in bundle.player_names if player_id != "p1")
        with self.assertRaisesRegex(ValueError, "calculation player"):
            replace(bundle, player_profiles=profile_snapshot(*missing))

    def test_bundle_rejects_conflicting_profile_identity_fields(self):
        bundle = engine_bundle()
        complete = profile_snapshot(*bundle.player_names)
        replacements = (
            ("display_name", "Different Player", "display name"),
            ("position", "WR", "position"),
            ("nfl_team_id", "DIFFERENT", "NFL team"),
        )
        for field, value, message in replacements:
            with self.subTest(field=field):
                players = tuple(
                    replace(player, **{field: value})
                    if player.canonical_player_id == "p1"
                    else player
                    for player in complete.players
                )
                conflicting = replace(complete, players=players)
                with self.assertRaisesRegex(ValueError, message):
                    replace(bundle, player_profiles=conflicting)

    def test_schema_eight_bundle_loads_as_profile_absent(self):
        current = engine_bundle().to_record()
        current.pop("player_profiles")
        current.pop("player_lab_projections")
        current["schema_version"] = 8
        legacy_content = {
            key: value
            for key, value in current.items()
            if key not in {"kind", "schema_version", "bundle_id"}
        }
        current["bundle_id"] = content_id("engine", legacy_content)

        loaded = EngineBundle.from_record(current)

        self.assertIsNone(loaded.player_profiles)
        self.assertEqual(loaded.to_record()["schema_version"], 10)

    def test_materializer_uses_exact_ids_and_retains_unmatched_public_players(self):
        public = _public_data(
            injury_rows=(
                PlayerInjuryReport(
                    "g1", "Public Player", "RB", 2026, 1, "KC",
                    "Hamstring", None, "questionable", "Hamstring", None,
                    "limited", NOW,
                ),
            )
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=_identity_registry(),
            public_data=public,
        )

        canonical = snapshot.players_by_id["p1"]
        self.assertEqual(canonical.display_name, "P1")
        self.assertEqual(canonical.current_season_stats[0].fantasy_points_ppr, 13.0)
        self.assertEqual(canonical.availability_history[0].report_status, "questionable")
        self.assertEqual(canonical.adds, 20)
        self.assertIn("sleeper:999", snapshot.players_by_id)
        self.assertEqual(snapshot.materialization_issues, ())

    def test_league_identity_team_wins_when_public_metadata_is_stale(self):
        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=1,
            identities=_identity_registry(),
            public_data=_public_data(
                sleeper_players=(_sleeper_player(nfl_team_id="LV"),)
            ),
        )

        self.assertEqual(snapshot.players_by_id["p1"].nfl_team_id, "KC")

    def test_materializer_retains_every_valid_sleeper_metadata_row(self):
        historical = _sleeper_player(
            "historical", gsis_id=None, espn_id=None, nfl_team_id=None
        )
        retired = replace(
            historical,
            sleeper_player_id="retired",
            display_name="Retired Player",
            active=False,
            status="Inactive",
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=1,
            identities=_identity_registry(),
            public_data=_public_data(
                sleeper_players=(historical, retired), current_rows=(), injury_rows=()
            ),
        )

        self.assertEqual(
            set(snapshot.players_by_id),
            {"p1", "sleeper:historical", "sleeper:retired"},
        )
        self.assertEqual(len(snapshot.players), 3)
        self.assertEqual(
            snapshot.players_by_id["sleeper:retired"].display_name,
            "Retired Player",
        )

    def test_conflicting_crosswalk_is_quarantined_without_guessing(self):
        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=1,
            identities=_identity_registry(conflict=True),
            public_data=_public_data(sleeper_players=(_sleeper_player(),)),
        )

        self.assertEqual(len(snapshot.materialization_issues), 1)
        self.assertIn("conflicting", snapshot.materialization_issues[0].reason)
        self.assertIsNone(snapshot.players_by_id["p1"].active)
        self.assertIn("p2", snapshot.players_by_id)

    def test_disjoint_crosswalk_components_cannot_merge_two_people(self):
        identities = IdentityRegistry((
            PlayerIdentity(
                "p1",
                "P1",
                "RB",
                "KC",
                (
                    ProviderReference("espn", "101"),
                    ProviderReference("gsis", "g2"),
                ),
            ),
        ))
        public = _public_data(
            id_crosswalk=(
                PublicPlayerIdCrosswalk("g1", "101", "s1"),
                PublicPlayerIdCrosswalk("g2", "202", "s2"),
            ),
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=identities,
            public_data=public,
        )

        self.assertEqual(len(snapshot.materialization_issues), 2)
        self.assertTrue(all(
            "multiple IDs for one provider" in issue.reason
            for issue in snapshot.materialization_issues
        ))
        self.assertEqual(snapshot.players_by_id["p1"].current_season_stats, ())
        self.assertEqual(
            len(snapshot.players_by_id["gsis:g1"].current_season_stats), 1
        )

    def test_crosswalk_cannot_add_a_second_provider_id_to_registry_player(self):
        identities = IdentityRegistry((
            PlayerIdentity(
                "p1",
                "P1",
                "RB",
                "KC",
                (
                    ProviderReference("espn", "101"),
                    ProviderReference("gsis", "g2"),
                ),
            ),
        ))
        public = _public_data(
            id_crosswalk=(
                PublicPlayerIdCrosswalk("g1", "101", "s1"),
            ),
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=identities,
            public_data=public,
        )

        self.assertTrue(any(
            "multiple IDs for one provider" in issue.reason
            for issue in snapshot.materialization_issues
        ))
        self.assertEqual(snapshot.players_by_id["p1"].current_season_stats, ())
        self.assertEqual(
            len(snapshot.players_by_id["gsis:g1"].current_season_stats), 1
        )
        self.assertNotIn(
            ("gsis", "g1"),
            {reference.key for reference in snapshot.players_by_id["p1"].provider_references},
        )

    def test_sleeper_metadata_cannot_add_a_second_exact_provider_id(self):
        public = _public_data(
            sleeper_players=(
                _sleeper_player("s2", gsis_id="g2", espn_id="101"),
            ),
            id_crosswalk=(
                PublicPlayerIdCrosswalk("g1", "101", "s1"),
            ),
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=_identity_registry(),
            public_data=public,
        )

        self.assertEqual(len(snapshot.materialization_issues), 1)
        self.assertIn(
            "conflicting exact provider ID",
            snapshot.materialization_issues[0].reason,
        )
        self.assertIsNone(snapshot.players_by_id["p1"].active)

    def test_exact_public_crosswalk_merges_sparse_sleeper_and_gsis_evidence(self):
        sparse = _sleeper_player("s1", gsis_id=None, espn_id=None)
        public = _public_data(
            sleeper_players=(sparse,),
            id_crosswalk=(
                PublicPlayerIdCrosswalk("g1", "101", None),
                PublicPlayerIdCrosswalk("g1", "101", "s1"),
            ),
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=_identity_registry(),
            public_data=public,
        )

        self.assertIn("p1", snapshot.players_by_id)
        self.assertNotIn("sleeper:s1", snapshot.players_by_id)
        self.assertNotIn("gsis:g1", snapshot.players_by_id)
        profile = snapshot.players_by_id["p1"]
        self.assertEqual(len(profile.current_season_stats), 1)
        self.assertTrue(profile.active)
        self.assertEqual(
            {reference.key for reference in profile.provider_references},
            {("espn", "101"), ("gsis", "g1"), ("sleeper", "s1")},
        )

    def test_contradictory_crosswalk_component_is_quarantined(self):
        public = _public_data(
            sleeper_players=(_sleeper_player("s1", gsis_id=None, espn_id=None),),
            id_crosswalk=(
                PublicPlayerIdCrosswalk("g1", "101", "s1"),
                PublicPlayerIdCrosswalk("g1", "202", "s2"),
            ),
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=_identity_registry(),
            public_data=public,
        )

        self.assertEqual(len(snapshot.materialization_issues), 1)
        self.assertIn("multiple IDs", snapshot.materialization_issues[0].reason)
        self.assertNotIn("sleeper:s1", snapshot.players_by_id)
        self.assertIn("gsis:g1", snapshot.players_by_id)

    def test_team_defense_uses_exact_team_identity_without_player_ids(self):
        defense = replace(
            _sleeper_player("KC", gsis_id=None, espn_id=None),
            display_name="KC D/ST",
            position="DEF",
            fantasy_positions=("DEF",),
        )
        identities = IdentityRegistry((
            PlayerIdentity(
                "kc-defense", "Kansas City D/ST", "DST", "KC",
                (ProviderReference("espn", "160"),),
            ),
        ))

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=1,
            identities=identities,
            public_data=_public_data(
                sleeper_players=(defense,), current_rows=(), injury_rows=()
            ),
        )

        self.assertIn("kc-defense", snapshot.players_by_id)
        self.assertNotIn("sleeper:KC", snapshot.players_by_id)
        self.assertEqual(snapshot.players_by_id["kc-defense"].position, "DST")

    def test_portable_profiles_strip_private_manual_mapping_provenance(self):
        bundle = engine_bundle()
        identities = []
        for player_id, display_name in bundle.player_names.items():
            projection = next(
                row
                for row in bundle.projections
                if row.canonical_player_id == player_id
            )
            references = ()
            if player_id == "p1":
                references = (
                    ProviderReference(
                        "espn",
                        "private-map-id",
                        ManualMappingProvenance(
                            "operator@example.test", NOW, "private roster note"
                        ),
                    ),
                )
            identities.append(
                PlayerIdentity(
                    player_id,
                    display_name,
                    projection.position,
                    projection.nfl_team_id,
                    references,
                )
            )
        snapshot = materialize_player_profiles(
            league_snapshot_id=bundle.state.snapshot_id,
            as_of_week=bundle.state.first_remaining_week,
            identities=IdentityRegistry(tuple(identities)),
            public_data=_public_data(
                sleeper_players=(), current_rows=(), injury_rows=()
            ),
        )
        attached = replace(bundle, player_profiles=snapshot)

        serialized = json.dumps(
            {
                "profiles": snapshot.to_record(),
                "bundle": attached.to_record(),
                "outlook": build_player_outlook(attached),
            },
            allow_nan=False,
        )
        self.assertNotIn("operator@example.test", serialized)
        self.assertNotIn("private roster note", serialized)
        self.assertTrue(all(
            reference.manual_mapping is None
            for profile in snapshot.players
            for reference in profile.provider_references
        ))

    def test_matching_names_do_not_merge_without_an_exact_crosswalk(self):
        sparse = _sleeper_player("unlinked", gsis_id=None, espn_id=None)
        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=2,
            identities=_identity_registry(),
            public_data=_public_data(sleeper_players=(sparse,)),
        )

        self.assertIn("sleeper:unlinked", snapshot.players_by_id)
        self.assertIn("gsis:g1", snapshot.players_by_id)

    def test_expected_profile_shape_failure_uses_typed_error(self):
        rows = (
            _public_stats_row(week=1, game_id="game-a"),
            _public_stats_row(week=1, game_id="game-b"),
        )
        with self.assertRaises(PlayerProfileMaterializationError):
            materialize_player_profiles(
                league_snapshot_id="snapshot-1",
                as_of_week=2,
                identities=_identity_registry(),
                public_data=_public_data(current_rows=rows),
            )

    def test_materializer_retains_exact_id_injury_only_players(self):
        orphan = PlayerInjuryReport(
            "orphan-gsis", "Injury Only", "WR", 2026, 1, "KC",
            "Ankle", None, "questionable", None, None, None, NOW,
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=1,
            identities=_identity_registry(),
            public_data=_public_data(
                sleeper_players=(), current_rows=(), injury_rows=(orphan,)
            ),
        )

        profile = snapshot.players_by_id["gsis:orphan-gsis"]
        self.assertEqual(profile.display_name, "Injury Only")
        self.assertEqual(profile.position, "WR")
        self.assertEqual(profile.nfl_team_id, "KC")
        self.assertEqual(len(profile.availability_history), 1)
        self.assertEqual(
            {reference.key for reference in profile.provider_references},
            {("gsis", "orphan-gsis")},
        )
        self.assertIn("p1", snapshot.players_by_id)

    def test_as_of_week_keeps_stats_before_boundary_and_reports_through_boundary(self):
        retained_stats = tuple(_public_stats_row(week=week) for week in range(1, 5))
        future_stats = (
            replace(
                _public_stats_row(week=5),
                fantasy_points_ppr=-100.0,
                headshot_url="https://static.www.nfl.com/image/private/future.png",
            ),
            replace(_public_stats_row(week=6), fantasy_points_ppr=-100.0),
        )
        previous_stat = replace(
            _public_stats_row(week=18),
            season=2025,
            game_id="2025_18_KC_LV",
        )
        boundary_report = PlayerInjuryReport(
            "g1", "Public Player", "RB", 2026, 5, "KC",
            "Hamstring", None, "questionable", None, None, None, NOW,
        )
        future_report = replace(boundary_report, week=6, report_status="out")
        previous_report = replace(boundary_report, season=2025, week=18)
        public = _public_data(
            current_rows=(*retained_stats, *future_stats),
            injury_rows=(boundary_report, future_report),
        )
        public = replace(
            public,
            previous_stats=SeasonPlayerStats(
                2025, DataAvailability.OBSERVED, (previous_stat,)
            ),
            injury_history=(
                public.injury_history[0],
                SeasonInjuryReports(
                    2025, DataAvailability.OBSERVED, (previous_report,)
                ),
                public.injury_history[2],
            ),
        )

        snapshot = materialize_player_profiles(
            league_snapshot_id="snapshot-1",
            as_of_week=5,
            identities=_identity_registry(),
            public_data=public,
        )

        profile = snapshot.players_by_id["p1"]
        self.assertEqual(
            [row.week for row in profile.current_season_stats],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [(row.season, row.week) for row in profile.previous_season_stats],
            [(2025, 18)],
        )
        self.assertEqual(
            [(row.season, row.week) for row in profile.availability_history],
            [(2025, 18), (2026, 5)],
        )
        trend = profile_record(profile, snapshot, "PPR")["performance_trend"]
        self.assertEqual(trend["direction"], "rising")
        self.assertEqual(trend["sample_size"], 4)

    def test_compact_catalog_profile_matches_detailed_status_and_trends(self):
        snapshot = profile_snapshot("p1")
        profile = snapshot.players_by_id["p1"]

        detailed = profile_record(profile, snapshot, "PPR")
        compact = profile_catalog_record(profile, snapshot, "PPR")
        catalog_trend_fields = {
            "status", "direction", "change", "adds", "drops", "net_adds"
        }

        self.assertEqual(
            compact,
            {
                "status": detailed["status"],
                "depth_chart": detailed["depth_chart"],
                "market_trend": {
                    field: value
                    for field, value in detailed["market_trend"].items()
                    if field in catalog_trend_fields
                },
                "performance_trend": {
                    field: value
                    for field, value in detailed["performance_trend"].items()
                    if field in catalog_trend_fields
                },
                "historical_availability": {
                    field: detailed["historical_availability"][field]
                    for field in ("status", "burden_index", "burden_tier")
                },
            },
        )

    def test_outlook_exposes_documented_history_and_never_calls_unknown_low(self):
        bundle = engine_bundle()
        attached = replace(bundle, player_profiles=profile_snapshot(*bundle.player_names))
        outlook = build_player_outlook(attached)
        p1 = next(row for row in outlook["players"] if row["player_id"] == "p1")
        availability = p1["profile"]["historical_availability"]

        self.assertEqual(outlook["schema_version"], 2)
        self.assertEqual(outlook["profile_scope"], "captured_public_catalog")
        self.assertEqual(availability["status"], "observed")
        self.assertEqual(availability["out_weeks"], [{"season": 2025, "week": 6}])
        self.assertEqual(availability["affected_body_areas"][0]["body_area"], "Hamstring")
        self.assertIn("Descriptive documented game-report burden", availability["method"])
        self.assertIn("not a probability", availability["method"])

        unavailable = replace(
            attached.player_profiles,
            injury_history_availability=(
                PlayerAvailabilitySeason(2026, "not_published"),
                PlayerAvailabilitySeason(2025, "not_published"),
                PlayerAvailabilitySeason(2024, "not_published"),
            ),
        )
        unknown = build_player_outlook(replace(bundle, player_profiles=unavailable))
        unknown_p1 = next(row for row in unknown["players"] if row["player_id"] == "p1")
        risk = unknown_p1["profile"]["historical_availability"]
        self.assertEqual(risk["status"], "unknown")
        self.assertEqual(risk["burden_tier"], "unknown")
        self.assertIsNone(risk["burden_index"])

    def test_player_without_report_or_stat_exposure_has_unknown_availability(self):
        bundle = engine_bundle()
        attached = replace(bundle, player_profiles=profile_snapshot(*bundle.player_names))

        outlook = build_player_outlook(attached)
        no_evidence = next(
            row for row in outlook["players"] if row["player_id"] == "q1"
        )["profile"]["historical_availability"]

        self.assertEqual(no_evidence["status"], "unknown")
        self.assertEqual(no_evidence["burden_tier"], "unknown")
        self.assertIsNone(no_evidence["burden_index"])
        self.assertEqual(no_evidence["player_evidence_seasons"], [])

    def test_lower_availability_tier_requires_meaningful_player_exposure(self):
        snapshot = profile_snapshot("p1")
        one_line = replace(
            _profile(),
            current_season_stats=(_game(1),),
            previous_season_stats=(),
            availability_history=(),
        )

        insufficient = profile_record(one_line, snapshot, "PPR")[
            "historical_availability"
        ]

        self.assertEqual(insufficient["status"], "unknown")
        self.assertEqual(insufficient["burden_tier"], "unknown")
        self.assertEqual(insufficient["exposure_status"], "insufficient")
        self.assertEqual(len(insufficient["recorded_stat_line_exposure"]), 1)

        enough_lines = replace(
            one_line,
            current_season_stats=tuple(_game(week) for week in range(1, 9)),
        )
        observed = profile_record(enough_lines, snapshot, "PPR")[
            "historical_availability"
        ]

        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["burden_tier"], "lower")
        self.assertEqual(observed["burden_index"], 0.0)
        self.assertEqual(observed["exposure_status"], "sufficient")

    def test_non_body_availability_contexts_are_not_injury_areas(self):
        labels = (
            "Not injury related - resting player",
            "Personal matter",
            "Illness",
            "Hamstring",
        )
        history = tuple(
            PlayerAvailabilityEvent(
                season=2025,
                week=week,
                nfl_team_id="NFL-P1",
                report_primary_injury=label,
                report_secondary_injury=None,
                report_status="questionable",
                practice_primary_injury=label,
                practice_secondary_injury=None,
                practice_status="limited",
                source_modified_at=NOW,
            )
            for week, label in enumerate(labels, start=1)
        )

        availability = profile_record(
            replace(_profile(), availability_history=history),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(
            availability["affected_body_areas"],
            [{"body_area": "Hamstring", "documented_weeks": 1}],
        )
        self.assertEqual(
            {row["context"] for row in availability["availability_contexts"]},
            {"Rest", "Personal matter", "Illness"},
        )

    def test_practice_only_evidence_does_not_imply_lower_availability_burden(self):
        practice_only = PlayerAvailabilityEvent(
            season=2026,
            week=1,
            nfl_team_id="NFL-P1",
            report_primary_injury=None,
            report_secondary_injury=None,
            report_status=None,
            practice_primary_injury="Hamstring",
            practice_secondary_injury=None,
            practice_status="limited",
            source_modified_at=NOW,
        )
        profile = replace(
            _profile(),
            current_season_stats=(),
            previous_season_stats=(),
            availability_history=(practice_only,),
        )

        availability = profile_record(profile, profile_snapshot("p1"), "PPR")[
            "historical_availability"
        ]

        self.assertEqual(availability["status"], "unknown")
        self.assertEqual(availability["burden_tier"], "unknown")
        self.assertIsNone(availability["burden_index"])
        self.assertEqual(availability["exposure_status"], "none")
        self.assertEqual(len(availability["weekly_evidence"]), 1)

    def test_non_injury_designation_does_not_imply_lower_availability_burden(self):
        non_injury = PlayerAvailabilityEvent(
            season=2026,
            week=1,
            nfl_team_id="NFL-P1",
            report_primary_injury="Not injury related - resting player",
            report_secondary_injury=None,
            report_status="questionable",
            practice_primary_injury="Not injury related - resting player",
            practice_secondary_injury=None,
            practice_status="limited",
            source_modified_at=NOW,
        )
        profile = replace(
            _profile(),
            current_season_stats=(),
            previous_season_stats=(),
            availability_history=(non_injury,),
        )

        availability = profile_record(profile, profile_snapshot("p1"), "PPR")[
            "historical_availability"
        ]

        self.assertEqual(availability["status"], "unknown")
        self.assertEqual(availability["burden_tier"], "unknown")
        self.assertIsNone(availability["burden_index"])
        self.assertEqual(availability["exposure_status"], "none")
        self.assertEqual(availability["distinct_report_weeks"], 0)
        self.assertEqual(availability["out_weeks"], [])
        self.assertEqual(availability["doubtful_weeks"], [])
        self.assertEqual(availability["questionable_weeks"], [])
        self.assertEqual(availability["out_report_without_recorded_stat_line"], [])
        self.assertEqual(
            availability["availability_contexts"],
            [{"context": "Rest", "documented_weeks": 1}],
        )
        self.assertEqual(len(availability["weekly_evidence"]), 1)

    def test_game_report_labels_take_priority_over_mixed_practice_notes(self):
        mixed = PlayerAvailabilityEvent(
            season=2026,
            week=1,
            nfl_team_id="NFL-P1",
            report_primary_injury="Not injury related - personal matter",
            report_secondary_injury=None,
            report_status="questionable",
            practice_primary_injury="Travel / personal note",
            practice_secondary_injury=None,
            practice_status="limited",
            source_modified_at=NOW,
        )
        availability = profile_record(
            replace(
                _profile(),
                current_season_stats=(),
                previous_season_stats=(),
                availability_history=(mixed,),
            ),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(availability["status"], "unknown")
        self.assertEqual(availability["distinct_report_weeks"], 0)
        self.assertEqual(availability["affected_body_areas"], [])
        self.assertEqual(len(availability["weekly_evidence"]), 1)

    def test_novel_report_label_is_scored_without_inventing_a_body_area(self):
        novel = PlayerAvailabilityEvent(
            season=2026,
            week=1,
            nfl_team_id="NFL-P1",
            report_primary_injury="Novel soft-tissue site",
            report_secondary_injury=None,
            report_status="questionable",
            practice_primary_injury=None,
            practice_secondary_injury=None,
            practice_status=None,
            source_modified_at=NOW,
        )
        availability = profile_record(
            replace(
                _profile(),
                current_season_stats=(),
                previous_season_stats=(),
                availability_history=(novel,),
            ),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(availability["status"], "observed")
        self.assertEqual(availability["distinct_report_weeks"], 1)
        self.assertGreater(availability["burden_index"], 0)
        self.assertEqual(availability["affected_body_areas"], [])
        self.assertEqual(
            availability["weekly_evidence"][0]["report_primary_injury"],
            "Novel soft-tissue site",
        )

    def test_live_nflverse_body_labels_are_retained_and_scored(self):
        labels = (
            "Abdomen", "Fibula", "Tibia", "Lung", "Pelvis",
            "Glute", "Throat", "Hernia", "Kidney",
        )
        history = tuple(
            PlayerAvailabilityEvent(
                season=2025,
                week=week,
                nfl_team_id="NFL-P1",
                report_primary_injury=label,
                report_secondary_injury=None,
                report_status="out",
                practice_primary_injury=label,
                practice_secondary_injury=None,
                practice_status="did_not_participate",
                source_modified_at=NOW,
            )
            for week, label in enumerate(labels, start=1)
        )
        availability = profile_record(
            replace(
                _profile(),
                current_season_stats=(),
                previous_season_stats=(),
                availability_history=history,
            ),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(availability["distinct_report_weeks"], len(labels))
        self.assertGreater(availability["burden_index"], 0)
        self.assertEqual(
            {row["body_area"] for row in availability["affected_body_areas"]},
            {"Abdomen", "Lower leg", "Lung", "Pelvis", "Glute", "Throat", "Hernia", "Kidney"},
        )

    def test_mixed_real_injury_and_non_injury_context_counts_real_body_area(self):
        mixed = PlayerAvailabilityEvent(
            season=2026,
            week=2,
            nfl_team_id="NFL-P1",
            report_primary_injury="Ankle",
            report_secondary_injury="Not Injury Related - Personal",
            report_status="questionable",
            practice_primary_injury="Ankle",
            practice_secondary_injury=None,
            practice_status="limited",
            source_modified_at=NOW,
        )
        availability = profile_record(
            replace(
                _profile(),
                current_season_stats=(),
                previous_season_stats=(),
                availability_history=(mixed,),
            ),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(availability["status"], "observed")
        self.assertEqual(availability["distinct_report_weeks"], 1)
        self.assertEqual(
            availability["affected_body_areas"],
            [{"body_area": "Ankle", "documented_weeks": 1}],
        )

    def test_coaching_decision_variants_are_non_injury_contexts(self):
        history = tuple(
            PlayerAvailabilityEvent(
                season=2026,
                week=week,
                nfl_team_id="NFL-P1",
                report_primary_injury=label,
                report_secondary_injury=None,
                report_status="doubtful",
                practice_primary_injury=label,
                practice_secondary_injury=None,
                practice_status="did_not_participate",
                source_modified_at=NOW,
            )
            for week, label in enumerate(
                ("Coach's Decision", "Coaching Decision"), start=17
            )
        )

        availability = profile_record(
            replace(
                _profile(),
                current_season_stats=(),
                previous_season_stats=(),
                availability_history=history,
            ),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(availability["burden_tier"], "unknown")
        self.assertEqual(availability["distinct_report_weeks"], 0)
        self.assertEqual(availability["affected_body_areas"], [])
        self.assertEqual(
            availability["availability_contexts"],
            [{"context": "Coach decision", "documented_weeks": 2}],
        )

    def test_compound_nir_annotation_does_not_hide_real_injury(self):
        compound = PlayerAvailabilityEvent(
            season=2026,
            week=9,
            nfl_team_id="NFL-P1",
            report_primary_injury="Ankle [Not Injury Related - Personal, Thursday Only]",
            report_secondary_injury=None,
            report_status="questionable",
            practice_primary_injury=(
                "Ankle [Not Injury Related - Personal, Thursday Only]"
            ),
            practice_secondary_injury=None,
            practice_status="limited",
            source_modified_at=NOW,
        )

        availability = profile_record(
            replace(
                _profile(),
                current_season_stats=(),
                previous_season_stats=(),
                availability_history=(compound,),
            ),
            profile_snapshot("p1"),
            "PPR",
        )["historical_availability"]

        self.assertEqual(availability["status"], "observed")
        self.assertEqual(availability["distinct_report_weeks"], 1)
        self.assertEqual(
            availability["affected_body_areas"],
            [{"body_area": "Ankle", "documented_weeks": 1}],
        )
        self.assertEqual(availability["availability_contexts"], [])

    def test_partial_market_trend_does_not_treat_censored_side_as_zero(self):
        record = profile_record(
            replace(_profile(), adds=None, drops=4),
            profile_snapshot("p1"),
            "PPR",
        )

        self.assertEqual(
            record["market_trend"],
            {
                "status": "partial",
                "adds": None,
                "drops": 4,
                "net_adds": None,
                "direction": "unknown",
                "method": (
                    "Sleeper publishes bounded top-add and top-drop lists; absence "
                    "from one list is unknown, not zero."
                ),
            },
        )

    def test_team_defense_history_is_explicitly_outside_player_stat_scope(self):
        defense = replace(
            _profile(),
            position="DST",
            fantasy_positions=("DST",),
            current_season_stats=(),
            previous_season_stats=(),
        )

        record = profile_record(defense, profile_snapshot("p1"), "PPR")

        self.assertEqual(record["current_season"]["availability"], "not_applicable")
        self.assertEqual(record["previous_season"]["availability"], "not_applicable")
        self.assertEqual(record["current_season"]["weeks"], [])
        self.assertIn("D/ST", record["current_season"]["note"])
        self.assertEqual(record["performance_trend"]["status"], "unknown")
        self.assertIn("D/ST", record["performance_trend"]["method"])

    def test_profile_history_uses_selected_scoring_mode_without_losing_raw_points(self):
        profile = replace(
            _profile(), current_season_stats=tuple(_game(week) for week in range(1, 5))
        )

        record = profile_record(profile, profile_snapshot("p1"), "HALF")

        week = record["current_season"]["weeks"][0]
        self.assertEqual(record["current_season"]["scoring_mode"], "HALF")
        self.assertEqual(week["fantasy_points_standard"], 11.0)
        self.assertEqual(week["fantasy_points_ppr"], 13.0)
        self.assertEqual(week["fantasy_points_selected"], 12.0)
        self.assertEqual(record["performance_trend"]["scoring_mode"], "HALF")
        self.assertEqual(
            record["performance_trend"]["basis"],
            "Half-PPR fantasy points per recorded stat line",
        )


if __name__ == "__main__":
    unittest.main()
