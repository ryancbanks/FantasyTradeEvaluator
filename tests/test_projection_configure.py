import unittest

from trade_snapshot._capture_errors import BrowserCaptureError
from trade_snapshot._projection_configure import configure_projection
from trade_snapshot.capture_schema import PageCaptureTask, ProjectionTableSpec


class _Page:
    def __init__(self, result):
        self.result = result

    def evaluate(self, *_args):
        return self.result


class _SequencePage:
    def __init__(self, results):
        self.results = iter(results)

    def evaluate(self, *_args):
        return next(self.results)


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

    def test_fftoday_loading_checks_do_not_consume_filter_change_budget(self):
        fingerprint = "verified-page-state"
        page = _SequencePage([
            {"action": "changed", "dimension": "fftoday dimensions",
             "fingerprint": "previous-page-state", "require_change": True},
            *(
                {"action": "waiting", "dimension": "fftoday content",
                 "fingerprint": "previous-page-state"}
                for _ in range(20)
            ),
            {"action": "ready", "fingerprint": fingerprint},
        ])
        waits = []
        task = PageCaptureTask(
            "fftoday",
            2026,
            1,
            "visible_table",
            "https://www.fftoday.com/rankings/playerproj.php",
            projection=ProjectionTableSpec("ros", "PPR", ("RB",)),
        )

        configure_projection(
            page,
            task,
            200,
            object(),
            lambda: False,
            lambda milliseconds, _cancelled: waits.append(milliseconds),
            lambda _deadline: 1000,
            lambda: None,
        )

        self.assertEqual(len(waits), 21)

    def test_fftoday_canonical_url_change_accepts_already_matching_content(self):
        fingerprint = "already-requested-page-state"
        page = _SequencePage([
            {"action": "changed", "dimension": "fftoday dimensions",
             "fingerprint": fingerprint, "require_change": False},
            {"action": "ready", "fingerprint": fingerprint},
        ])
        task = PageCaptureTask(
            "fftoday",
            2026,
            1,
            "visible_table",
            "https://www.fftoday.com/rankings/playerproj.php",
            projection=ProjectionTableSpec("ros", "PPR", ("RB",)),
        )

        configure_projection(
            page,
            task,
            200,
            object(),
            lambda: False,
            lambda *_args: None,
            lambda _deadline: 1000,
            lambda: None,
        )

    def test_fantasysharks_waits_for_new_table_after_filter_changes(self):
        page = _SequencePage([
            {
                "action": "changed",
                "dimension": "position",
                "fingerprint": "quarterback-table",
                "require_change": True,
            },
            {"action": "ready", "fingerprint": "quarterback-table"},
            {
                "action": "waiting",
                "dimension": "fantasysharks content",
                "fingerprint": "quarterback-table",
            },
            {"action": "ready", "fingerprint": "running-back-table"},
        ])
        waits = []
        task = PageCaptureTask(
            "fantasysharks",
            2026,
            1,
            "visible_table",
            "https://www.fantasysharks.com/apps/bert/forecasts/projections.php",
            projection=ProjectionTableSpec("weekly", "PPR", ("RB",)),
        )

        configure_projection(
            page,
            task,
            200,
            object(),
            lambda: False,
            lambda milliseconds, _cancelled: waits.append(milliseconds),
            lambda _deadline: 1000,
            lambda: None,
        )

        self.assertEqual(len(waits), 3)


if __name__ == "__main__":
    unittest.main()
