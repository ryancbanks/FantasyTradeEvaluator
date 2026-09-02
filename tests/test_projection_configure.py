import unittest

from trade_snapshot._capture_errors import BrowserCaptureError
from trade_snapshot._projection_configure import configure_projection
from trade_snapshot.capture_schema import PageCaptureTask, ProjectionTableSpec


class _Page:
    def __init__(self, result):
        self.result = result

    def evaluate(self, *_args):
        return self.result


class ProjectionConfigurationErrorTests(unittest.TestCase):
    def test_yahoo_filter_failure_names_the_player_list_problem(self):
        task = PageCaptureTask(
            "yahoo",
            2026,
            1,
            "visible_table",
            "https://football.fantasysports.yahoo.com/f1/players",
            projection=ProjectionTableSpec("weekly", "PPR", ("RB",)),
        )
        with self.assertRaisesRegex(
            BrowserCaptureError, "Yahoo Player List.*projection period"
        ):
            configure_projection(
                _Page({"action": "error", "dimension": "yahoo period"}),
                task,
                200,
                object(),
                lambda: False,
                lambda *_args: None,
                lambda _deadline: 1000,
                lambda: None,
            )


if __name__ == "__main__":
    unittest.main()
