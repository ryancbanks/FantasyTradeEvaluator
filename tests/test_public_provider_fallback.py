from types import SimpleNamespace
import unittest

from trade_snapshot.capture_schema import CaptureProvider
from trade_snapshot.public_projection_collection import (
    assemble_with_public_fallback,
    retain_complete_public_providers,
)
from trade_snapshot.public_projection_plan import public_projection_tasks
from trade_snapshot.weekly_collection import WeeklyCollectionError


def row(provider, suffix):
    return SimpleNamespace(provider=provider, task_id=f"task-{suffix}")


class PublicProviderFallbackTests(unittest.TestCase):
    def test_partial_provider_plan_is_rejected_as_one_unit(self):
        tasks = tuple(
            task
            for task in public_projection_tasks(
                season=2026,
                week=1,
                horizon="weekly",
                scoring="PPR",
                positions=("QB", "RB"),
            )
            if task.provider is CaptureProvider.FFTODAY
        )
        artifacts = tuple(SimpleNamespace(task_id=task.task_id) for task in tasks)

        kept_rows, kept_tasks = retain_complete_public_providers(
            artifacts[:1], tasks[:1], tasks
        )

        self.assertEqual(kept_rows, ())
        self.assertEqual(kept_tasks, ())
        self.assertEqual(
            retain_complete_public_providers(artifacts, tasks, tasks),
            (artifacts, tasks),
        )

    def test_semantically_bad_provider_is_quarantined_without_new_reads(self):
        projections = (
            row(CaptureProvider.ESPN, "espn"),
            row(CaptureProvider.CBS, "cbs"),
            row(CaptureProvider.FFTODAY, "fftoday"),
            row(CaptureProvider.FANTASYSHARKS, "fantasysharks"),
        )
        attempts = []

        def assemble(candidate, broad_consensus):
            providers = tuple(item.provider for item in candidate)
            attempts.append(providers)
            if not broad_consensus:
                return providers
            if CaptureProvider.CBS in providers:
                raise ValueError("malformed CBS fixture")
            return providers

        result, retained = assemble_with_public_fallback(
            projections,
            assemble,
            broad_consensus=True,
        )

        self.assertEqual(
            result,
            (
                CaptureProvider.ESPN,
                CaptureProvider.FFTODAY,
                CaptureProvider.FANTASYSHARKS,
            ),
        )
        self.assertEqual(tuple(item.provider for item in retained), result)
        self.assertGreater(len(attempts), 1)

    def test_invalid_public_consensus_fails_with_actionable_choice(self):
        projections = (
            row(CaptureProvider.ESPN, "espn"),
            row(CaptureProvider.CBS, "cbs"),
        )
        def invalid(candidate, broad_consensus):
            if not broad_consensus:
                return tuple(item.provider for item in candidate)
            raise ValueError("bad data")

        with self.assertRaisesRegex(
            WeeklyCollectionError, "turn Broad projection consensus off or retry"
        ):
            assemble_with_public_fallback(
                projections,
                invalid,
                broad_consensus=True,
            )

    def test_single_source_mode_never_swallows_required_source_failure(self):
        projections = (row(CaptureProvider.ESPN, "espn"),)
        def invalid(_candidate, _broad_consensus):
            raise ValueError("required ESPN failed")

        with self.assertRaisesRegex(ValueError, "required ESPN"):
            assemble_with_public_fallback(
                projections,
                invalid,
                broad_consensus=False,
            )


if __name__ == "__main__":
    unittest.main()
