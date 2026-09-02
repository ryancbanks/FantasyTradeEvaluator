from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_installer


EXACT_IDENTITY = build_installer.InterpreterIdentity("CPython", (3, 12, 13), 64)


class InstallerBuilderTests(unittest.TestCase):
    @staticmethod
    def _write_environment(path: Path, *, system_site: str = "false") -> Path:
        executable = build_installer._environment_python(path)
        executable.parent.mkdir(parents=True)
        executable.touch()
        (path / build_installer._ENVIRONMENT_MARKER).write_text(
            build_installer._ENVIRONMENT_MARKER_CONTENT,
            encoding="utf-8",
        )
        (path / "pyvenv.cfg").write_text(
            f"include-system-site-packages = {system_site}\n",
            encoding="utf-8",
        )
        return executable

    def test_platform_specific_environment_interpreter(self):
        environment = Path("build-environment")
        self.assertEqual(
            build_installer._environment_python(environment, platform_name="nt"),
            environment / "Scripts" / "python.exe",
        )
        self.assertEqual(
            build_installer._environment_python(environment, platform_name="posix"),
            environment / "bin" / "python",
        )

    def test_identity_requires_exact_64_bit_cpython(self):
        invalid = (
            (build_installer.InterpreterIdentity("PyPy", (3, 12, 13), 64), "CPython"),
            (build_installer.InterpreterIdentity("CPython", (3, 12, 12), 64), "3.12.13"),
            (build_installer.InterpreterIdentity("CPython", (3, 12, 13), 32), "64-bit"),
        )
        for identity, message in invalid:
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(RuntimeError, message):
                    build_installer._validate_identity(identity, "test runtime")

    def test_environment_must_be_owned_and_exclude_system_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / build_installer.BUILD_ENVIRONMENT_NAME
            self._write_environment(environment, system_site="true")
            with patch.object(
                build_installer, "_interpreter_identity", return_value=EXACT_IDENTITY
            ):
                with self.assertRaisesRegex(RuntimeError, "exclude system"):
                    build_installer._validate_environment(environment, root)

    def test_prepare_refuses_to_replace_an_unowned_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / build_installer.BUILD_ENVIRONMENT_NAME
            environment.mkdir()
            (environment / "user-file").write_text("keep")
            with patch.object(
                build_installer, "_current_identity", return_value=EXACT_IDENTITY
            ):
                with self.assertRaisesRegex(ValueError, "unowned"):
                    build_installer.prepare_build_environment(root, environment)
            self.assertEqual((environment / "user-file").read_text(), "keep")

    def test_prepare_replaces_only_an_owned_environment_with_a_clean_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / build_installer.BUILD_ENVIRONMENT_NAME
            self._write_environment(environment)
            (environment / "stale-package").write_text("remove")
            replacement = root / f"{build_installer.BUILD_ENVIRONMENT_NAME}-new-test"
            replacement_executable = self._write_environment(replacement)
            final_executable = build_installer._environment_python(environment)
            with (
                patch.object(
                    build_installer, "_current_identity", return_value=EXACT_IDENTITY
                ),
                patch.object(
                    build_installer, "_create_environment", return_value=replacement
                ),
                patch.object(
                    build_installer,
                    "_interpreter_identity",
                    return_value=EXACT_IDENTITY,
                ),
            ):
                self.assertEqual(
                    build_installer.prepare_build_environment(root, environment),
                    final_executable,
                )
            self.assertFalse((environment / "stale-package").exists())
            self.assertFalse(replacement_executable.exists())
            self.assertTrue(final_executable.is_file())

    def test_dependency_install_uses_the_hash_locked_requirements(self):
        executable = Path("isolated-python")
        root = Path("project")
        with patch.object(build_installer.subprocess, "run") as run:
            build_installer.install_build_dependencies(executable, root)
        run.assert_called_once_with(
            [
                str(executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--require-hashes",
                "--only-binary=:all:",
                "--requirement",
                str(root / "packaging" / "build-requirements.txt"),
            ],
            cwd=root,
            check=True,
            timeout=1_200,
        )

    def test_main_installs_dependencies_then_forwards_build_options(self):
        executable = Path("isolated-python")
        with (
            patch.object(
                build_installer,
                "prepare_build_environment",
                return_value=executable,
            ),
            patch.object(build_installer, "install_build_dependencies") as install,
            patch.object(build_installer, "invoke_release_builder") as build,
        ):
            result = build_installer.main(
                ["--output", "release-candidate", "--portable-only"]
            )
        self.assertEqual(result, 0)
        install.assert_called_once_with(executable)
        build.assert_called_once_with(
            executable,
            output=Path("release-candidate"),
            portable_only=True,
        )

    def test_build_failure_exit_code_is_preserved(self):
        with (
            patch.object(
                build_installer,
                "prepare_build_environment",
                return_value=Path("isolated-python"),
            ),
            patch.object(
                build_installer,
                "install_build_dependencies",
                side_effect=subprocess.CalledProcessError(17, ["pip"]),
            ),
        ):
            self.assertEqual(build_installer.main([]), 17)

    def test_timeout_is_reported_without_a_traceback(self):
        with patch.object(
            build_installer,
            "prepare_build_environment",
            side_effect=subprocess.TimeoutExpired(["venv"], 300),
        ):
            with patch("sys.stderr") as stderr:
                self.assertEqual(build_installer.main([]), 2)
        rendered = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("Installer build failed", rendered)


if __name__ == "__main__":
    unittest.main()
