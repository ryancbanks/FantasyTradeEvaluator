from pathlib import Path
from tempfile import TemporaryDirectory
import socket
import unittest
from unittest.mock import patch

from tests.test_app_service import payload, wait_for_job
from tests.test_engine_bundle import engine_bundle
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest


def _forbidden_external_call(*_args, **_kwargs):
    raise AssertionError("loaded-bundle search crossed an external-data boundary")


class OfflineRuntimeTests(unittest.TestCase):
    def test_loaded_bundle_search_and_export_do_not_call_network_or_analyzer(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(
                directory,
                weekly_collection_workflow=_forbidden_external_call,
            )
            service.import_bundle(bundle.to_record())
            request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))

            with (
                patch(
                    "trade_snapshot.browser_capture.BrowserCollector.collect",
                    _forbidden_external_call,
                ),
                patch(
                    "trade_snapshot.espn_free_read.EspnFreeReadClient.__call__",
                    _forbidden_external_call,
                ),
                patch(
                    "trade_snapshot.production_collection."
                    "ProductionWeeklyCollectionWorkflow.__call__",
                    _forbidden_external_call,
                ),
                patch(
                    "trade_snapshot.production_calibration."
                    "BrowserCalibrationFactory._capture",
                    _forbidden_external_call,
                ),
                patch(
                    "trade_snapshot.bundle_provenance.urlopen",
                    _forbidden_external_call,
                ),
                patch(
                    "urllib.request.OpenerDirector.open",
                    _forbidden_external_call,
                ),
                patch.object(socket, "create_connection", _forbidden_external_call),
            ):
                estimate = service.estimate_search(request)
                started = service.start_search(request)
                finished = wait_for_job(service, started["job_id"])
                results = service.job_results(started["job_id"])
                exported = service.export_job(started["job_id"])
                export_path = service.export_path(exported["filename"])

            self.assertEqual(estimate["candidate_count"], 4)
            self.assertEqual(finished["status"], "complete", finished["error"])
            self.assertEqual(results["total_count"], 4)
            self.assertEqual(exported["trade_count"], 4)
            self.assertTrue(Path(export_path).is_file())


if __name__ == "__main__":
    unittest.main()
