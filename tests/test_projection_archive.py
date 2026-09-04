from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trade_snapshot.capture_schema import GenericTableArtifact, VisibleTable, VisibleTableCell
from trade_snapshot.projection_archive import (
    load_projection_archive,
    projection_archive_catalog,
    save_projection_archive,
)


CAPTURED_AT = "2026-09-03T12:34:56Z"


class ProjectionArchiveTests(unittest.TestCase):
    def test_espn_team_defense_uses_the_negative_roster_identity(self):
        artifact = GenericTableArtifact(
            task_id="captask_" + "4" * 64,
            provider="espn",
            season=2026,
            week=1,
            kind="visible_table",
            captured_at=CAPTURED_AT,
            horizon="ros",
            scoring="PPR",
            position_scope=("ALL",),
            source_period_text="2026 | Full Season | PPR | ALL | ESPN",
            segments_captured=1,
            complete=True,
            tables=(
                VisibleTable(
                    (
                        tuple(VisibleTableCell(value) for value in (
                            "PLAYER", "TEAM", "POS", "GP", "FPTS", "FPPG",
                        )),
                        (
                            VisibleTableCell(
                                "Houston Texans D/ST",
                                (
                                    "https://www.espn.com/nfl/player/_/id/"
                                    "16034/houston-texans-dst",
                                ),
                            ),
                            VisibleTableCell("HOU"),
                            VisibleTableCell("DST"),
                            VisibleTableCell("17"),
                            VisibleTableCell("118.0"),
                            VisibleTableCell("6.94"),
                        ),
                    )
                ),
            ),
        )

        with TemporaryDirectory() as directory:
            archive = load_projection_archive(
                save_projection_archive(directory, (artifact,))
            )

        self.assertEqual(archive.rows[0].identity_provider, "espn")
        self.assertEqual(archive.rows[0].provider_player_id, "-16034")
        self.assertEqual(archive.rows[0].position, "DST")
        self.assertEqual(archive.rows[0].nfl_team_id, "HOU")

    def test_fantasypros_team_defenses_have_stable_team_identity(self):
        artifact = GenericTableArtifact(
            task_id="captask_" + "3" * 64,
            provider="fantasypros",
            season=2026,
            week=1,
            kind="visible_table",
            captured_at=CAPTURED_AT,
            horizon="weekly",
            scoring="PPR",
            position_scope=("DST",),
            source_period_text="2026 | Week 1 | PPR | DST",
            segments_captured=1,
            complete=True,
            tables=(
                VisibleTable(
                    (
                        (VisibleTableCell("PLAYER"), VisibleTableCell("FPTS")),
                        (
                            VisibleTableCell(
                                "Arizona Cardinals",
                                (
                                    "https://www.fantasypros.com/nfl/projections/"
                                    "arizona-defense.php",
                                ),
                            ),
                            VisibleTableCell("4.7"),
                        ),
                    )
                ),
            ),
        )

        with TemporaryDirectory() as directory:
            archive = load_projection_archive(
                save_projection_archive(directory, (artifact,))
            )

        self.assertEqual(len(archive.rows), 1)
        self.assertEqual(archive.rows[0].provider_player_id, "dst:ARI")
        self.assertEqual(archive.rows[0].display_name, "Arizona Cardinals")
        self.assertEqual(archive.rows[0].position, "DST")
        self.assertEqual(archive.rows[0].nfl_team_id, "ARI")

    def test_preserves_every_structured_row_stat_and_traversal_evidence(self):
        artifact = projection_artifact()
        with TemporaryDirectory() as directory:
            path = save_projection_archive(directory, (artifact,))
            archive = load_projection_archive(path)
            repeated = save_projection_archive(directory, (artifact,))
            catalog = projection_archive_catalog(directory)

        self.assertEqual(repeated, path)
        self.assertEqual(len(archive.rows), 3)
        self.assertEqual(
            {row.provider_player_id for row in archive.rows}, {"101", "102", "103"}
        )
        first = next(row for row in archive.rows if row.provider_player_id == "101")
        self.assertEqual(first.identity_provider, "yahoo")
        self.assertEqual(first.display_name, "Alpha Runner")
        self.assertEqual(first.position, "RB")
        self.assertEqual(first.nfl_team_id, "DET")
        self.assertEqual(first.opponent_team_id, "CHI")
        self.assertFalse(first.is_home)
        self.assertEqual(first.projected_fantasy_points, 12.5)
        self.assertEqual(dict(first.raw_projected_stats), {"rush_yds": 71.0})
        third = next(row for row in archive.rows if row.provider_player_id == "103")
        self.assertIsNone(third.projected_fantasy_points)
        self.assertEqual(dict(third.raw_projected_stats), {"rec": 0.0, "tgt": 1.0})
        self.assertEqual(archive.sources[0].segments_captured, 4)
        self.assertEqual(archive.sources[0].table_count, 2)
        self.assertEqual(archive.sources[0].row_count, 3)
        self.assertEqual(catalog[0]["status"], "ready")
        self.assertEqual(catalog[0]["row_count"], 3)
        self.assertEqual(catalog[0]["segments_captured"], 4)
        self.assertEqual(catalog[0]["positions"], ["RB", "WR"])
        self.assertEqual(catalog[0]["stat_names"], ["rec", "rush_yds", "tgt"])

    def test_content_address_and_saved_bytes_ignore_capture_input_order(self):
        first = projection_artifact()
        second = replace(first, task_id="captask_" + "2" * 64)
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            left_path = save_projection_archive(left, (first, second))
            right_path = save_projection_archive(right, (second, first))
            left_bytes = (left_path / "archive.json").read_bytes()
            right_bytes = (right_path / "archive.json").read_bytes()

        self.assertEqual(left_path.name, right_path.name)
        self.assertEqual(left_bytes, right_bytes)

    def test_archive_never_persists_capture_or_private_league_urls(self):
        artifact = projection_artifact()
        with TemporaryDirectory() as directory:
            path = save_projection_archive(directory, (artifact,))
            persisted = (path / "archive.json").read_text(encoding="utf-8")
            summary = (path / "summary.json").read_text(encoding="utf-8")

        self.assertNotIn("fantasysports.yahoo.com", persisted)
        self.assertNotIn("sports.yahoo.com", persisted)
        self.assertNotIn("leagueId", persisted)
        self.assertNotIn("private-token", persisted)
        self.assertNotIn("http", persisted.casefold())
        self.assertNotIn("http", summary.casefold())

    def test_tampering_duplicate_keys_nan_and_oversize_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = save_projection_archive(directory, (projection_artifact(),))
            archive_file = path / "archive.json"
            original = archive_file.read_text(encoding="utf-8")

            changed = json.loads(original)
            changed["rows"][0]["projected_fantasy_points"] = 999.0
            archive_file.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "archive_id"):
                load_projection_archive(path)

            archive_file.write_text(
                original.replace(
                    '"archive_id":', '"archive_id":"duplicate","archive_id":', 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_projection_archive(path)

            archive_file.write_text(
                original.replace(
                    '"projected_fantasy_points":12.5',
                    '"projected_fantasy_points":NaN',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-finite JSON constant"):
                load_projection_archive(path)

            archive_file.write_text(original, encoding="utf-8")
            with patch("trade_snapshot.projection_archive._MAX_ARCHIVE_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    load_projection_archive(path)

    def test_catalog_rejects_a_tampered_summary_without_loading_data_rows(self):
        with TemporaryDirectory() as directory:
            path = save_projection_archive(directory, (projection_artifact(),))
            summary_file = path / "summary.json"
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
            summary["row_count"] += 1
            summary_file.write_text(json.dumps(summary), encoding="utf-8")

            catalog = projection_archive_catalog(directory)

        self.assertEqual(catalog[0]["status"], "invalid")
        self.assertIn("summary content", catalog[0]["error"])

    def test_failed_publish_is_atomic_and_removes_staging_directory(self):
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.projection_archive.os.replace",
            side_effect=OSError("simulated publish failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated publish failure"):
                save_projection_archive(directory, (projection_artifact(),))
            self.assertEqual(tuple(Path(directory).iterdir()), ())

    def test_private_fields_are_rejected_before_persistence(self):
        artifact = projection_artifact(stat_header="MEMBER ID")
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "private or transport"):
                save_projection_archive(directory, (artifact,))
            self.assertEqual(tuple(Path(directory).iterdir()), ())
        artifact = replace(
            projection_artifact(), source_period_text="Week 1 | token=private-token"
        )
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "private session"):
                save_projection_archive(directory, (artifact,))
            self.assertEqual(tuple(Path(directory).iterdir()), ())


def projection_artifact(*, stat_header="RUSH YDS"):
    task_id = "captask_" + "1" * 64
    tables = (
        VisibleTable(
            (
                tuple(VisibleTableCell(value) for value in (
                    "PLAYER", "TEAM", "POS", "OPP", "FPTS", stat_header,
                )),
                (
                    player_cell("Alpha Runner", 101),
                    VisibleTableCell("DET"),
                    VisibleTableCell("RB"),
                    VisibleTableCell("@CHI"),
                    VisibleTableCell("12.5"),
                    VisibleTableCell("71"),
                ),
                (
                    player_cell("Beta Runner", 102),
                    VisibleTableCell("GB"),
                    VisibleTableCell("RB"),
                    VisibleTableCell("vs DET"),
                    VisibleTableCell("8.25"),
                    VisibleTableCell("42.5"),
                ),
            )
        ),
        VisibleTable(
            (
                tuple(VisibleTableCell(value) for value in (
                    "PLAYER", "TEAM", "POS", "STATUS", "FPTS", "REC", "TGT",
                )),
                (
                    player_cell("Gamma Receiver", 103),
                    VisibleTableCell("MIN"),
                    VisibleTableCell("WR"),
                    VisibleTableCell("Questionable"),
                    VisibleTableCell("--"),
                    VisibleTableCell("0"),
                    VisibleTableCell("1"),
                ),
            )
        ),
    )
    return GenericTableArtifact(
        task_id=task_id,
        provider="yahoo",
        season=2026,
        week=1,
        kind="visible_table",
        captured_at=CAPTURED_AT,
        horizon="weekly",
        scoring="PPR",
        position_scope=("ALL",),
        source_period_text="2026 full player table | Week 1 | PPR",
        segments_captured=4,
        complete=True,
        tables=tables,
    )


def player_cell(name, player_id):
    return VisibleTableCell(
        name,
        (
            f"https://sports.yahoo.com/nfl/players/{player_id}/"
            "?leagueId=1530398&token=private-token",
        ),
    )


if __name__ == "__main__":
    unittest.main()
