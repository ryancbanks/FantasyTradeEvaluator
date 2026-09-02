import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fte_native_smoke", PROJECT_ROOT / "packaging" / "smoke_native_release.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("native smoke verifier could not be loaded")
NATIVE_SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE_SMOKE)


class NativeSmokeTests(unittest.TestCase):
    def test_windows_cleanup_only_runs_for_the_registered_test_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            installed = Path(temporary) / "installed"
            installed.mkdir()
            native_uninstaller = installed / "unins000.exe"
            native_uninstaller.write_bytes(b"uninstaller")
            with (
                patch.object(
                    NATIVE_SMOKE,
                    "_registered_windows_install_matches",
                    return_value=False,
                ),
                patch.object(NATIVE_SMOKE.subprocess, "run") as run,
            ):
                NATIVE_SMOKE._cleanup_windows_smoke_install(installed)
            run.assert_not_called()

            with (
                patch.object(
                    NATIVE_SMOKE,
                    "_registered_windows_install_matches",
                    return_value=True,
                ),
                patch.object(NATIVE_SMOKE.subprocess, "run") as run,
                patch.object(NATIVE_SMOKE, "_wait_until_removed") as wait,
            ):
                NATIVE_SMOKE._cleanup_windows_smoke_install(installed)
            self.assertEqual(Path(run.call_args.args[0][0]), native_uninstaller)
            self.assertIn("/VERYSILENT", run.call_args.args[0])
            wait.assert_called_once_with(installed)

    def test_windows_smoke_refuses_to_replace_a_registered_install(self):
        with patch.object(NATIVE_SMOKE, "_windows_install_registered", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "clean Windows user"):
                NATIVE_SMOKE._windows(Path("release"), Path("temporary"))

    def test_waits_for_self_deleting_uninstaller(self):
        with tempfile.TemporaryDirectory() as temporary:
            leftover = Path(temporary) / "installed"
            leftover.mkdir()

            def finish_cleanup(_seconds):
                leftover.rmdir()

            with patch.object(NATIVE_SMOKE.time, "sleep", side_effect=finish_cleanup):
                NATIVE_SMOKE._wait_until_removed(leftover, timeout=1)
            self.assertFalse(leftover.exists())

    def test_reports_uninstaller_that_never_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            leftover = Path(temporary) / "installed"
            leftover.mkdir()
            with patch.object(
                NATIVE_SMOKE.time, "monotonic", side_effect=(0.0, 2.0)
            ):
                with self.assertRaisesRegex(RuntimeError, "left this path behind"):
                    NATIVE_SMOKE._wait_until_removed(leftover, timeout=1)


if __name__ == "__main__":
    unittest.main()
