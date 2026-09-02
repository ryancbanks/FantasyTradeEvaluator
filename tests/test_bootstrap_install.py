from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import bootstrap_install


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def fake_runtime(runtime: Path) -> None:
    scripts = runtime / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    executable = scripts / (
        "fantasy-trade-evaluator.exe" if os.name == "nt" else "fantasy-trade-evaluator"
    )
    executable.write_text("launcher")


class BootstrapInstallTests(unittest.TestCase):
    def test_supported_hosts_and_user_scoped_defaults_are_explicit(self):
        self.assertEqual(
            bootstrap_install.supported_host("win32", "AMD64"),
            ("win32", "x64"),
        )
        self.assertEqual(
            bootstrap_install.supported_host("darwin", "arm64"),
            ("darwin", "arm64"),
        )
        self.assertEqual(
            bootstrap_install.supported_host("linux", "aarch64"),
            ("linux", "arm64"),
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            bootstrap_install.supported_host("freebsd", "x86_64")
        with self.assertRaisesRegex(ValueError, "Windows source installation"):
            bootstrap_install.supported_host("win32", "arm64")

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            local = home / "Local Data"
            self.assertEqual(
                bootstrap_install.default_install_root(
                    platform_name="win32",
                    machine="x86_64",
                    environment={"LOCALAPPDATA": str(local)},
                    home=home,
                ),
                local / "Programs" / "FantasyTradeEvaluatorSource",
            )
            self.assertEqual(
                bootstrap_install.default_install_root(
                    platform_name="darwin",
                    machine="arm64",
                    environment={},
                    home=home,
                ),
                home
                / "Library"
                / "Application Support"
                / "FantasyTradeEvaluatorSource",
            )
            self.assertEqual(
                bootstrap_install.default_install_root(
                    platform_name="linux",
                    machine="aarch64",
                    environment={"XDG_DATA_HOME": str(local)},
                    home=home,
                ),
                local / "FantasyTradeEvaluatorSource",
            )
            with self.assertRaisesRegex(ValueError, "must be absolute"):
                bootstrap_install.default_install_root(
                    platform_name="linux",
                    machine="x86_64",
                    environment={"XDG_DATA_HOME": "relative"},
                    home=home,
                )

    def test_install_publishes_only_verified_runtime_and_upgrades_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            target = home / "Apps" / "FantasyTradeEvaluatorSource"

            def build(runtime, _source):
                fake_runtime(runtime)

            with patch.object(bootstrap_install, "_build_runtime", side_effect=build):
                first = bootstrap_install.install(PROJECT_ROOT, target, home=home)
                first_body = first.read_text()
                first_runtime = next((target / "versions").iterdir())
                second = bootstrap_install.install(PROJECT_ROOT, target, home=home)

            runtimes = tuple((target / "versions").iterdir())
            self.assertEqual(first, second)
            self.assertEqual(len(runtimes), 1)
            self.assertNotEqual(runtimes[0], first_runtime)
            self.assertNotEqual(second.read_text(), first_body)
            self.assertIn(runtimes[0].name, second.read_text())
            self.assertNotIn(str(home), second.read_text())
            self.assertEqual(
                (target / bootstrap_install.INSTALL_MARKER).read_text(),
                bootstrap_install.MARKER_CONTENT,
            )

    def test_failed_upgrade_preserves_launcher_and_prior_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            target = home / "Apps" / "FantasyTradeEvaluatorSource"

            def build(runtime, _source):
                fake_runtime(runtime)

            with patch.object(bootstrap_install, "_build_runtime", side_effect=build):
                launcher = bootstrap_install.install(PROJECT_ROOT, target, home=home)
            before = launcher.read_bytes()
            prior = tuple((target / "versions").iterdir())

            def fail(runtime, _source):
                runtime.mkdir()
                raise RuntimeError("download failed")

            with patch.object(bootstrap_install, "_build_runtime", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "download failed"):
                    bootstrap_install.install(PROJECT_ROOT, target, home=home)

            self.assertEqual(launcher.read_bytes(), before)
            self.assertEqual(tuple((target / "versions").iterdir()), prior)

    def test_refuses_unowned_broad_or_external_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            unowned = home / "existing"
            unowned.mkdir()
            with self.assertRaisesRegex(ValueError, "not owned"):
                bootstrap_install.install(PROJECT_ROOT, unowned, home=home)
            with self.assertRaisesRegex(ValueError, "dedicated directory"):
                bootstrap_install.install(PROJECT_ROOT, home, home=home)
            with tempfile.TemporaryDirectory() as outside:
                with self.assertRaisesRegex(ValueError, "inside the user home"):
                    bootstrap_install.install(
                        PROJECT_ROOT,
                        Path(outside) / "runtime",
                        home=home,
                    )

    def test_uninstall_removes_only_bootstrap_owned_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            target = home / "Apps" / "FantasyTradeEvaluatorSource"

            def build(runtime, _source):
                fake_runtime(runtime)

            with patch.object(bootstrap_install, "_build_runtime", side_effect=build):
                bootstrap_install.install(PROJECT_ROOT, target, home=home)
            self.assertTrue(bootstrap_install.uninstall(target, home=home))
            self.assertFalse(target.exists())
            self.assertFalse(bootstrap_install.uninstall(target, home=home))

            target.mkdir(parents=True)
            (target / "user-file").write_text("keep")
            with self.assertRaisesRegex(ValueError, "not owned"):
                bootstrap_install.uninstall(target, home=home)
            self.assertEqual((target / "user-file").read_text(), "keep")

    def test_runtime_build_uses_only_base_package_and_bounded_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            builder = Mock()

            def create(path):
                scripts = Path(path) / ("Scripts" if os.name == "nt" else "bin")
                scripts.mkdir(parents=True)

            builder.create.side_effect = create
            calls = []

            def run(command, *, env, timeout):
                calls.append((command, env, timeout))

            with (
                patch.object(bootstrap_install.venv, "EnvBuilder", return_value=builder),
                patch.object(bootstrap_install, "_run", side_effect=run),
            ):
                bootstrap_install._build_runtime(runtime, PROJECT_ROOT)

            self.assertEqual(len(calls), 2)
            python = str(bootstrap_install._runtime_python(runtime))
            self.assertEqual([call[0][0] for call in calls], [python, python])
            self.assertEqual(calls[0][0][2:4], ["pip", "install"])
            self.assertEqual(calls[0][0][-1], str(PROJECT_ROOT))
            self.assertIn("--self-check", calls[1][0])
            self.assertTrue(
                all("PLAYWRIGHT_BROWSERS_PATH" not in call[1] for call in calls)
            )
            self.assertEqual([call[2] for call in calls], [1_200, 180])


if __name__ == "__main__":
    unittest.main()
