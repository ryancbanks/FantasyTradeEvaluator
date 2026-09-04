import unittest
from tempfile import TemporaryDirectory

from tests.test_app_service import payload, wait_for_job
from tests.test_engine_bundle import engine_bundle
from tests.test_weekly_collection import SuccessfulWorkflow, wait_for_collection
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest
from trade_snapshot.weekly_collection import WeeklyCollectionRequest


class TradeSearchProgressIntegrationTests(unittest.TestCase):
    def test_search_publishes_exact_units_and_terminal_timing(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
            started = service.start_search(request)
            finished = wait_for_job(service, started["job_id"])

        operation = finished["operation"]
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(operation["status"], "complete")
        self.assertEqual(operation["activity"], "terminal")
        self.assertEqual(operation["phase"], "searching_trade_combinations")
        self.assertTrue(operation["progress"]["determinate"])
        self.assertEqual(operation["progress"]["completed_units"], 4)
        self.assertEqual(operation["progress"]["total_units"], 4)
        self.assertEqual(operation["progress"]["fraction"], 1)


class WeeklyCollectionProgressIntegrationTests(unittest.TestCase):
    def test_collection_publishes_indeterminate_phases_and_terminal_timing(self):
        workflow = SuccessfulWorkflow()
        with TemporaryDirectory() as directory:
            service = LocalAppService(
                directory, weekly_collection_workflow=workflow
            )
            started = service.start_weekly_collection(
                WeeklyCollectionRequest(2026, 1, "PPR")
            )
            finished = wait_for_collection(
                service.weekly_collection, started["job_id"]
            )

        operation = finished["operation"]
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(operation["status"], "complete")
        self.assertEqual(operation["activity"], "terminal")
        self.assertEqual(operation["phase"], "publishing")
        self.assertFalse(operation["progress"]["determinate"])
        self.assertGreaterEqual(operation["elapsed_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
