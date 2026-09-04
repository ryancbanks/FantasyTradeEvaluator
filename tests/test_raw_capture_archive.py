import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_production_collection import _league_artifact
from trade_snapshot.capture_schema import (
    CaptureKind,
    GenericTableArtifact,
    PageCaptureTask,
    ProjectionTableSpec,
    VisibleTable,
    VisibleTableCell,
)
from trade_snapshot.raw_capture_archive import (
    _credential_free,
    archive_private_league_capture,
    archive_public_captures,
)


class RawCaptureArchiveTests(unittest.TestCase):
    def test_partitions_public_and_private_content_and_is_idempotent(self):
        projection_task, projection = _projection_capture()
        league_task = PageCaptureTask(
            "fantasypros",
            2026,
            1,
            "league_source",
            "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
        )
        league = _league_artifact(league_task)
        binding = "league_" + "1" * 32

        with TemporaryDirectory() as directory:
            root = Path(directory)
            public = archive_public_captures(
                root, ((projection_task, projection),)
            )[0]
            private = archive_private_league_capture(
                root, binding, (league_task, league)
            )

            self.assertEqual(
                public.parent,
                (root / "raw-captures" / "public").resolve(),
            )
            self.assertEqual(
                private.parent,
                (root / "raw-captures" / "private-leagues" / binding).resolve(),
            )
            self.assertEqual(json.loads(public.read_text("utf-8")), projection.to_record())
            self.assertEqual(json.loads(private.read_text("utf-8")), league.to_record())
            self.assertNotIn(binding, public.read_text("utf-8"))
            self.assertEqual(
                archive_public_captures(root, ((projection_task, projection),))[0],
                public,
            )
            self.assertEqual(
                archive_private_league_capture(
                    root, binding, (league_task, league)
                ),
                private,
            )

    def test_rejects_scope_confusion_unsafe_partition_and_collision(self):
        projection_task, projection = _projection_capture()
        league_task = PageCaptureTask(
            "fantasypros",
            2026,
            1,
            "league_source",
            "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
        )
        league = _league_artifact(league_task)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "public raw captures"):
                archive_public_captures(root, ((league_task, league),))
            with self.assertRaisesRegex(ValueError, "opaque local"):
                archive_private_league_capture(
                    root, "../league_unsafe", (league_task, league)
                )
            path = archive_public_captures(
                root, ((projection_task, projection),)
            )[0]
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "collision"):
                archive_public_captures(root, ((projection_task, projection),))

    def test_refuses_nested_credentials_and_signed_url_parameters(self):
        for value in (
            {"payload": {"espn_s2": "secret"}},
            {"url": "https://example.test/data?access_token=secret"},
            {"url": "https://user:password@example.test/data"},
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "credential-bearing"
            ):
                _credential_free(value)


def _projection_capture() -> tuple[PageCaptureTask, GenericTableArtifact]:
    task = PageCaptureTask(
        "espn",
        2026,
        1,
        CaptureKind.VISIBLE_TABLE,
        "https://fantasy.espn.com/football/players/projections",
        projection=ProjectionTableSpec("weekly", "PPR", ("ALL",)),
    )
    table = VisibleTable(
        (
            (VisibleTableCell("PLAYER"), VisibleTableCell("FPTS")),
            (
                VisibleTableCell(
                    "Player One",
                    ("https://www.espn.com/nfl/player/_/id/201/player-one",),
                ),
                VisibleTableCell("12.0"),
            ),
        )
    )
    artifact = GenericTableArtifact(
        task.task_id,
        task.provider,
        task.season,
        task.week,
        task.kind,
        "2026-09-01T01:00:00Z",
        task.projection.horizon,
        task.projection.scoring,
        task.projection.position_scope,
        "2026 | Week 1 | PPR | ALL",
        1,
        True,
        (table,),
    )
    return task, artifact


if __name__ == "__main__":
    unittest.main()
