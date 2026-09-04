import json
from hashlib import sha256
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import tarfile
import tomllib
import unittest
from unittest.mock import patch

import release_build
from trade_snapshot import app_launcher
from trade_snapshot.local_server import _STATIC


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AppLauncherTests(unittest.TestCase):
    def test_launcher_forwards_only_local_server_options(self):
        data = PROJECT_ROOT / ".test-app-data"
        workflow = object()
        bridge = object()
        with (
            patch.object(app_launcher, "ExtensionCommandBridge", return_value=bridge),
            patch.object(
                app_launcher,
                "create_production_weekly_collection_workflow",
                return_value=workflow,
            ) as create_workflow,
            patch.object(app_launcher, "serve_local_app") as serve,
        ):
            result = app_launcher.main([
                "--data-directory", str(data), "--port", "4321", "--no-browser"
            ])
        self.assertEqual(result, 0)
        create_workflow.assert_called_once_with(bridge)
        serve.assert_called_once_with(
            data,
            port=4321,
            open_browser=False,
            weekly_collection_workflow=workflow,
            extension_bridge=bridge,
        )

    def test_launcher_sanitizes_expected_startup_failures(self):
        with patch.object(app_launcher, "serve_local_app", side_effect=OSError("occupied")):
            with patch("sys.stderr") as stderr:
                result = app_launcher.main(["--no-browser"])
        self.assertEqual(result, 2)
        rendered = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("could not start", rendered)

    def test_frozen_launcher_opens_a_visible_error_page(self):
        with patch.object(app_launcher, "serve_local_app", side_effect=OSError("occupied")):
            with patch.object(app_launcher.sys, "frozen", True, create=True):
                with patch.object(app_launcher.webbrowser, "open") as opened:
                    result = app_launcher.main(["--no-browser"])
        self.assertEqual(result, 2)
        self.assertTrue(opened.call_args.args[0].startswith("data:text/html"))
        self.assertIn("occupied", opened.call_args.args[0])
        self.assertIn("color-scheme", opened.call_args.args[0])
        self.assertIn("%23071014", opened.call_args.args[0])

    def test_self_check_mode_has_machine_readable_result(self):
        ready = {
            "status": "ready",
            "web_assets": True,
            "browser_extension": "1.0.0",
        }
        with patch.object(app_launcher, "runtime_self_check", return_value=ready):
            with patch("builtins.print") as output:
                self.assertEqual(app_launcher.main(["--self-check"]), 0)
        self.assertEqual(json.loads(output.call_args.args[0]), ready)

    def test_runtime_self_check_fails_when_any_required_web_asset_is_missing(self):
        with (
            patch.object(
                app_launcher,
                "_required_web_assets",
                return_value=("index.html", "missing-feature.js"),
            ),
            patch.object(app_launcher, "_lifecycle_self_check"),
            self.assertRaisesRegex(RuntimeError, "local interface assets"),
        ):
            app_launcher.runtime_self_check()


class ReleaseBuildTests(unittest.TestCase):
    def test_supported_release_targets_are_explicit(self):
        cases = (
            ("win32", "AMD64", "windows-x64"),
            ("darwin", "x86_64", "macos-x64"),
            ("darwin", "arm64", "macos-arm64"),
            ("linux", "x86_64", "linux-x64"),
            ("linux", "aarch64", "linux-arm64"),
        )
        for system, machine, expected in cases:
            with self.subTest(expected):
                self.assertEqual(release_build.current_target(system, machine).tag, expected)
        with self.assertRaisesRegex(ValueError, "Windows releases"):
            release_build.current_target("win32", "arm64")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            release_build.current_target("freebsd", "x86_64")

    def test_version_and_runtime_dependencies_have_one_pinned_owner(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as source:
            metadata = tomllib.load(source)
        project = metadata["project"]
        self.assertEqual(project["version"], "0.2.1")
        self.assertEqual(release_build.project_version(), project["version"])
        self.assertEqual(project["dependencies"], ["XlsxWriter==3.2.9"])
        self.assertEqual(
            project["optional-dependencies"]["browser-test"],
            [
                "playwright==1.62.0",
                "greenlet==3.5.5",
                "pyee==13.0.1",
                "typing_extensions==4.16.0",
            ],
        )
        self.assertEqual(
            project["optional-dependencies"]["build"],
            ["PyInstaller==6.22.2"],
        )
        self.assertEqual(
            metadata["build-system"]["requires"],
            ["setuptools==84.0.0", "wheel==0.48.0"],
        )

    def test_release_tag_must_exactly_match_the_project_version(self):
        expected = f"v{release_build.project_version()}"
        release_build.validate_release_tag(expected)
        for invalid in (expected + ".1", expected[1:], "v2.0.0"):
            with self.subTest(invalid):
                with self.assertRaisesRegex(ValueError, "release tag must be exactly"):
                    release_build.validate_release_tag(invalid)

    def test_release_output_cannot_target_broad_or_external_directories(self):
        with self.assertRaisesRegex(ValueError, "dedicated directory"):
            release_build._safe_output(PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "not owned"):
            release_build._safe_output(PROJECT_ROOT / "docs")
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(ValueError, "inside the project"):
                release_build._safe_output(Path(outside))
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as empty:
            output = release_build._safe_output(Path(empty))
            (output / "existing-release.zip").write_bytes(b"old")
            self.assertEqual(release_build._safe_output(output), output)

    def test_extension_and_web_assets_are_mandatory_in_frozen_build(self):
        spec = (PROJECT_ROOT / "packaging" / "fantasy_trade_evaluator.spec").read_text()
        self.assertIn('collect_data_files("trade_snapshot.web_assets")', spec)
        self.assertIn('collect_data_files("trade_snapshot.browser_extension")', spec)
        self.assertIn('"THIRD_PARTY_NOTICES"', spec)
        for excluded in (
            '"playwright"',
            '"greenlet"',
            '"pyee"',
            '"typing_extensions"',
            '"trade_snapshot._playwright_capture"',
        ):
            self.assertIn(excluded, spec)
        self.assertNotIn(".local-browsers", spec)
        self.assertNotIn("collect_all", spec)
        self.assertEqual(list((PROJECT_ROOT / "packaging" / "hooks").glob("hook-playwright*")), [])
        self.assertFalse((PROJECT_ROOT / "packaging" / "pyi_rth_playwright.py").exists())
        self.assertIn('"LSMinimumSystemVersion": "14.0"', spec)
        self.assertIn("--self-check", (PROJECT_ROOT / "release_build.py").read_text())
        launcher = (PROJECT_ROOT / "trade_snapshot" / "app_launcher.py").read_text()
        self.assertNotIn("playwright", launcher.casefold())
        self.assertNotIn("chromium", launcher.casefold())

    def test_self_check_inventory_covers_every_extension_resource(self):
        root = PROJECT_ROOT / "trade_snapshot" / "browser_extension"
        manifest = json.loads((root / "manifest.json").read_text())
        expected = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and "tests" not in path.relative_to(root).parts
            and path.suffix.casefold() in {".css", ".html", ".js", ".json", ".md"}
        }
        self.assertEqual(
            set(app_launcher._required_extension_assets(manifest)),
            expected,
        )

    def test_self_check_inventory_covers_every_served_and_linked_web_asset(self):
        root = PROJECT_ROOT / "trade_snapshot" / "web_assets"
        page = (root / "index.html").read_text(encoding="utf-8")
        linked = {
            match.removeprefix("/")
            for match in re.findall(r'(?:href|src)="(/[^"?]+\.(?:css|js))"', page)
        }
        packaged = {
            path.name
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() in {".css", ".html", ".js"}
        }
        required = set(app_launcher._required_web_assets())

        self.assertEqual(required, {"index.html", *linked})
        self.assertEqual(required, packaged)
        self.assertEqual(set(_STATIC), {f"/{name}" for name in linked})
        self.assertEqual({value[0] for value in _STATIC.values()}, linked)

    def test_wheel_smoke_uses_the_runtime_web_asset_inventory(self):
        smoke = (PROJECT_ROOT / "packaging" / "smoke_wheel.py").read_text()

        self.assertIn("_required_web_assets", smoke)
        self.assertNotIn("('index.html','app.js','styles.css')", smoke)

    def test_source_distribution_manifest_carries_release_inputs(self):
        manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for entry in (
            "include THIRD_PARTY_NOTICES.md",
            "include bootstrap_install.py",
            "include release_build.py",
            "recursive-include docs *.md",
            "recursive-include packaging *",
        ):
            self.assertIn(entry, manifest)
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)["project"]
        self.assertEqual(project["license-files"], ["THIRD_PARTY_NOTICES.md"])
        wheel_smoke = (PROJECT_ROOT / "packaging" / "smoke_wheel.py").read_text()
        self.assertIn("distribution('fantasy-trade-evaluator').files", wheel_smoke)

    def test_release_notice_allowlist_excludes_source_only_browser_test_runtime(self):
        retired = {
            "Playwright-LICENSE.txt",
            "Playwright-NOTICE.txt",
            "Playwright-ThirdPartyNotices.txt",
            "Playwright-Bundled-JavaScript-LICENSES.txt",
            "Node-LICENSE.txt",
            "greenlet-LICENSE.txt",
            "greenlet-PSF-LICENSE.txt",
            "pyee-LICENSE.txt",
            "typing-extensions-LICENSE.txt",
            "Chrome-for-Testing-CREDITS.html",
        }
        self.assertTrue(retired.isdisjoint(release_build.SOURCE_NOTICE_FILENAMES))
        source = (PROJECT_ROOT / "release_build.py").read_text()
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", source)
        self.assertNotIn("sync_playwright", source)

    def test_frozen_application_requires_complete_third_party_notices(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            application = Path(temporary) / "application"
            notices = application / "_internal" / "THIRD_PARTY_NOTICES"
            notices.mkdir(parents=True)
            for name in release_build.NOTICE_FILENAMES:
                (notices / name).write_text(f"notice: {name}")
            self.assertEqual(release_build._assert_application_notices(application), notices)
            (notices / "CPython-LICENSE.txt").unlink()
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                release_build._assert_application_notices(application)

    def test_notice_source_fails_closed_for_unset_or_foreign_directories(self):
        for missing in (None, ""):
            with self.subTest(missing):
                with self.assertRaisesRegex(ValueError, "required"):
                    release_build.validated_notice_source(missing)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            foreign = Path(temporary)
            with self.assertRaisesRegex(ValueError, "not owned"):
                release_build.validated_notice_source(str(foreign))
            owned = release_build._safe_output(foreign)
            notices = owned / "legal-notices"
            notices.mkdir()
            for name in release_build.SOURCE_NOTICE_FILENAMES:
                (notices / name).write_text(name)
            self.assertEqual(release_build.validated_notice_source(str(notices)), notices)
            (notices / "unexpected-secret.txt").write_text("secret")
            with self.assertRaisesRegex(ValueError, "allowlist"):
                release_build.validated_notice_source(str(notices))

    def test_native_inventory_requires_a_reviewed_license_mapping(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            application = Path(temporary) / "application"
            notices = application / "_internal" / "THIRD_PARTY_NOTICES"
            notices.mkdir(parents=True)
            for name in release_build.SOURCE_NOTICE_FILENAMES:
                (notices / name).write_text(name)
            (application / "FantasyTradeEvaluator.exe").write_bytes(b"launcher")
            (application / "_internal" / "libcrypto-3-x64.dll").write_bytes(b"ssl")
            inventory = release_build._write_native_inventory(application).read_text()
            self.assertIn("CPython-THIRD-PARTY-LICENSES.rst", inventory)
            (application / "_internal" / "mystery.dll").write_bytes(b"unknown")
            with self.assertRaisesRegex(RuntimeError, "no reviewed license mapping"):
                release_build._write_native_inventory(application)

    def test_external_release_commands_are_bounded(self):
        with patch.object(release_build.subprocess, "run") as run:
            release_build._run(["builder"], timeout=17)
        run.assert_called_once_with(
            ["builder"], cwd=PROJECT_ROOT, env=None, check=True, timeout=17
        )

    def test_checksum_generation_streams_each_artifact(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            root = Path(temporary)
            artifact = root / "release.bin"
            artifact.write_bytes(b"release payload")
            checksum = release_build._write_checksums(root, [artifact]).read_text()
        self.assertEqual(
            checksum,
            f"{sha256(b'release payload').hexdigest()}  release.bin\n",
        )

    def test_windows_packages_compile_setup_and_standalone_uninstaller(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            output = Path(temporary)
            application = output / "application"
            application.mkdir()
            compiler = output / "ISCC.exe"

            def compile_package(command, **_kwargs):
                script = Path(command[-1]).name
                suffix = "Setup" if script == "installer.iss" else "Uninstall"
                compiled = output / (
                    f"FantasyTradeEvaluator-0.1.0-windows-x64-{suffix}.exe"
                )
                compiled.write_bytes(b"compiled")

            with (
                patch.object(release_build, "_find_iscc", return_value=compiler),
                patch.object(release_build, "_run", side_effect=compile_package) as run,
            ):
                packages = release_build._windows_packages(application, output, "0.1.0")

        self.assertEqual(
            tuple(package.name for package in packages),
            (
                "FantasyTradeEvaluator-0.1.0-windows-x64-Setup.exe",
                "FantasyTradeEvaluator-0.1.0-windows-x64-Uninstall.exe",
            ),
        )
        setup_command, uninstall_command = (call.args[0] for call in run.call_args_list)
        self.assertEqual(Path(setup_command[-1]).name, "installer.iss")
        self.assertIn(f"/DSourceDir={application}", setup_command)
        self.assertEqual(Path(uninstall_command[-1]).name, "uninstaller.iss")
        self.assertNotIn(f"/DSourceDir={application}", uninstall_command)
        for command in (setup_command, uninstall_command):
            self.assertIn("/DAppVersion=0.1.0", command)
            self.assertIn(f"/DOutputDir={output}", command)

    def test_windows_portable_cleanup_removes_only_installer_artifacts(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            output = Path(temporary)
            setup = output / "FantasyTradeEvaluator-0.1.0-windows-x64-Setup.exe"
            uninstall = output / "FantasyTradeEvaluator-0.1.0-windows-x64-Uninstall.exe"
            unrelated = output / "keep.exe"
            for path in (setup, uninstall, unrelated):
                path.write_bytes(b"artifact")

            release_build._remove_windows_packages(output, "0.1.0")

            self.assertFalse(setup.exists())
            self.assertFalse(uninstall.exists())
            self.assertTrue(unrelated.exists())

    def test_windows_uninstaller_delegates_without_touching_weekly_data(self):
        installer = (PROJECT_ROOT / "packaging" / "windows" / "installer.iss").read_text()
        launcher = (PROJECT_ROOT / "packaging" / "windows" / "uninstaller.iss").read_text()
        uninstall_key = (
            "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
            "{FD659318-22E8-45E3-A51B-1BF298CBFC90}_is1"
        )

        self.assertIn('AppId={{FD659318-22E8-45E3-A51B-1BF298CBFC90}', installer)
        self.assertIn("UninstallDisplayName={#AppName}", installer)
        self.assertIn('Filename: "{uninstallexe}"', installer)
        for directive in (
            "CreateAppDir=no",
            "CreateUninstallRegKey=no",
            "Uninstallable=no",
        ):
            self.assertIn(directive, launcher)
        self.assertIn(uninstall_key, launcher)
        self.assertIn("HKCU64", launcher)
        self.assertIn("HKCU32", launcher)
        self.assertIn("FileExists(Uninstaller)", launcher)
        self.assertIn("Exec(Uninstaller, Parameters", launcher)
        self.assertIn("ewWaitUntilTerminated", launcher)
        self.assertIn("/VERYSILENT /SUPPRESSMSGBOXES /NORESTART", launcher)
        self.assertNotIn("[Files]", launcher)
        for script in (installer, launcher):
            self.assertNotIn("[UninstallDelete]", script)
            self.assertNotIn(r"{localappdata}\FantasyTradeEvaluator", script)

    def test_linux_archive_contains_relocatable_app_and_executable_installers(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            root = Path(temporary)
            application = root / "frozen"
            application.mkdir()
            (application / "FantasyTradeEvaluator").write_bytes(b"executable")
            archive = release_build._linux_archive(
                application,
                root,
                "0.1.0",
                release_build.ReleaseTarget("linux", "x64"),
            )
            with tarfile.open(archive, "r:gz") as package:
                members = {member.name: member for member in package.getmembers()}
                prefix = "FantasyTradeEvaluator-0.1.0-linux-x64"
                target_metadata = package.extractfile(
                    members[f"{prefix}/release-target.txt"]
                ).read().decode("utf-8")
        self.assertIn(f"{prefix}/app/FantasyTradeEvaluator", members)
        self.assertIn("system=linux\n", target_metadata)
        self.assertIn("architecture=x64\n", target_metadata)
        self.assertEqual(members[f"{prefix}/install.sh"].mode & 0o777, 0o755)
        self.assertEqual(members[f"{prefix}/uninstall.sh"].mode & 0o777, 0o755)

    def test_linux_installer_uses_verified_random_staging(self):
        installer = (PROJECT_ROOT / "packaging" / "linux" / "install.sh").read_text()
        self.assertIn('mktemp -d -- "$data_root/.fantasy-trade-evaluator-new.XXXXXX"', installer)
        self.assertIn('[ -L "$stage" ] || [ ! -d "$stage" ]', installer)
        self.assertIn('"$stage/FantasyTradeEvaluator" --self-check', installer)
        self.assertIn('target_architecture=$(sed', installer)
        self.assertNotIn('new-$$', installer)

    @unittest.skipIf(os.name == "nt", "requires a POSIX shell and symbolic links")
    def test_linux_installer_rejects_a_mktemp_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            fake_bin = root / "fake-bin"
            home = root / "home"
            outside = root / "outside"
            for directory in (package / "app", fake_bin, home, outside):
                directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT_ROOT / "packaging" / "linux" / "install.sh", package)
            (package / "app" / "FantasyTradeEvaluator").write_bytes(b"app")
            (package / "fantasy-trade-evaluator.svg").write_text("<svg/>")
            self._write_linux_target(package)
            malicious_stage = home / ".local" / "share" / ".fantasy-trade-evaluator-new.link"
            malicious_stage.parent.mkdir(parents=True)
            malicious_stage.symlink_to(outside, target_is_directory=True)
            fake_mktemp = fake_bin / "mktemp"
            fake_mktemp.write_text(f"#!/bin/sh\nprintf '%s\\n' '{malicious_stage}'\n")
            fake_mktemp.chmod(0o755)
            environment = os.environ | {
                "HOME": str(home),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }
            result = subprocess.run(
                ["sh", str(package / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Refusing unsafe staging directory", result.stderr)
            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "requires a POSIX shell and symbolic links")
    def test_linux_install_supports_spaced_xdg_paths_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._linux_test_package(root)
            home = root / "home"
            data = home / "Local Apps" / "share"
            binary = home / "Local $ Apps%" / "bin"
            home.mkdir()
            environment = os.environ | {
                "HOME": str(home),
                "XDG_DATA_HOME": str(data),
                "XDG_BIN_HOME": str(binary),
            }
            subprocess.run(["sh", str(package / "install.sh")], check=True, env=environment)
            desktop = (data / "applications" / "fantasy-trade-evaluator.desktop").read_text()
            escaped_binary = str(binary).replace("$", r"\\$").replace("%", "%%")
            self.assertIn(f'Exec="{escaped_binary}/fantasy-trade-evaluator"', desktop)
            self.assertIn(r"Local\sApps", desktop)
            subprocess.run(["sh", str(package / "uninstall.sh")], check=True, env=environment)
            self.assertFalse((binary / "fantasy-trade-evaluator").exists())

    @unittest.skipIf(os.name == "nt", "requires a POSIX shell and symbolic links")
    def test_linux_upgrade_rolls_back_if_launcher_integration_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._linux_test_package(root)
            home = root / "home"
            data = home / ".local" / "share"
            install = data / "fantasy-trade-evaluator"
            fake_bin = root / "fake-bin"
            install.mkdir(parents=True)
            fake_bin.mkdir()
            (install / "old-marker").write_text("old")
            failing_ln = fake_bin / "ln"
            failing_ln.write_text("#!/bin/sh\nexit 91\n")
            failing_ln.chmod(0o755)
            environment = os.environ | {
                "HOME": str(home),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }
            result = subprocess.run(
                ["sh", str(package / "install.sh")], check=False, env=environment
            )
            self.assertEqual(result.returncode, 91)
            self.assertTrue((install / "old-marker").is_file())
            self.assertFalse((install / "FantasyTradeEvaluator").exists())

    @unittest.skipIf(os.name == "nt", "requires a POSIX shell and symbolic links")
    def test_linux_installer_rejects_desktop_directory_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._linux_test_package(root)
            home = root / "home"
            collision = home / ".local" / "share" / "applications" / "fantasy-trade-evaluator.desktop"
            collision.mkdir(parents=True)
            result = subprocess.run(
                ["sh", str(package / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=os.environ | {"HOME": str(home)},
            )
            self.assertEqual(result.returncode, 2)
            self.assertTrue(collision.is_dir())

    @unittest.skipIf(os.name == "nt", "requires a POSIX shell")
    def test_linux_installer_rejects_wrong_release_architecture_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._linux_test_package(root)
            home = root / "home"
            fake_bin = root / "fake-bin"
            home.mkdir()
            fake_bin.mkdir()
            host = "arm64" if self._linux_architecture() == "x64" else "x86_64"
            fake_uname = fake_bin / "uname"
            fake_uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{host}'\n")
            fake_uname.chmod(0o755)
            result = subprocess.run(
                ["sh", str(package / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=os.environ
                | {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("This package is for", result.stderr)
            self.assertFalse((home / ".local").exists())

    @unittest.skipIf(os.name == "nt", "requires a POSIX shell and symbolic links")
    def test_linux_uninstaller_preserves_unowned_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._linux_test_package(root)
            home = root / "home"
            launcher = home / ".local" / "bin" / "fantasy-trade-evaluator"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("user file")
            subprocess.run(
                ["sh", str(package / "uninstall.sh")],
                check=True,
                env=os.environ | {"HOME": str(home)},
            )
            self.assertEqual(launcher.read_text(), "user file")

    @staticmethod
    def _linux_test_package(root: Path) -> Path:
        package = root / "package"
        (package / "app").mkdir(parents=True)
        shutil.copy2(PROJECT_ROOT / "packaging" / "linux" / "install.sh", package)
        shutil.copy2(PROJECT_ROOT / "packaging" / "linux" / "uninstall.sh", package)
        executable = package / "app" / "FantasyTradeEvaluator"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        (package / "fantasy-trade-evaluator.svg").write_text("<svg/>")
        ReleaseBuildTests._write_linux_target(package)
        return package

    @staticmethod
    def _linux_architecture() -> str:
        return {
            "x86_64": "x64",
            "amd64": "x64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }[platform.machine().lower()]

    @staticmethod
    def _write_linux_target(package: Path) -> None:
        (package / "release-target.txt").write_text(
            "fantasy-trade-evaluator release target v1\n"
            "system=linux\n"
            f"architecture={ReleaseBuildTests._linux_architecture()}\n"
            "version=0.1.0\n"
        )

    def test_repeat_macos_package_replaces_only_its_exact_dmg(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            root = Path(temporary)
            application = root / "Fantasy Trade Evaluator.app"
            application.mkdir()
            dmg = root / "FantasyTradeEvaluator-0.1.0-macos-arm64.dmg"
            dmg.write_bytes(b"old image")
            with patch.dict(release_build.os.environ, {}, clear=True):
                with patch.object(release_build, "_run") as run:
                    with patch.object(Path, "symlink_to"):
                        result = release_build._macos_dmg(
                            application,
                            root,
                            "0.1.0",
                            release_build.ReleaseTarget("macos", "arm64"),
                        )
            self.assertEqual(result, dmg)
            self.assertFalse(dmg.exists())
            self.assertEqual(run.call_args_list[-1].args[0][0], "hdiutil")


if __name__ == "__main__":
    unittest.main()
