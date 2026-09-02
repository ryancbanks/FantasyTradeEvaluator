from hashlib import sha256
from pathlib import Path
import shutil
import tempfile
import unittest

import release_assemble


class ReleaseAssemblyTests(unittest.TestCase):
    version = "0.1.0"

    def _artifact_tree(self, root: Path) -> Path:
        source = root / "downloaded"
        for directory_name, filenames in release_assemble.expected_artifacts(
            self.version
        ).items():
            directory = source / directory_name
            directory.mkdir(parents=True)
            checksum_lines = []
            for filename in filenames:
                content = f"verified payload: {directory_name}/{filename}".encode()
                (directory / filename).write_bytes(content)
                checksum_lines.append(f"{sha256(content).hexdigest()}  {filename}\n")
            (directory / release_assemble.CHECKSUM_NAME).write_text(
                "".join(checksum_lines), encoding="ascii", newline="\n"
            )
        return source

    def test_exact_platform_set_is_verified_and_flattened(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._artifact_tree(root)
            destination = root / "publish"
            result = release_assemble.assemble_release(
                source, destination, self.version
            )
            expected = {
                filename
                for filenames in release_assemble.expected_artifacts(
                    self.version
                ).values()
                for filename in filenames
            } | {release_assemble.CHECKSUM_NAME}
            self.assertEqual({path.name for path in result}, expected)
            self.assertEqual({path.name for path in destination.iterdir()}, expected)
            combined = release_assemble._read_manifest(
                destination / release_assemble.CHECKSUM_NAME
            )
            for name, digest in combined.items():
                self.assertEqual(release_assemble._digest(destination / name), digest)

    def test_tampered_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._artifact_tree(root)
            windows = source / "fantasy-trade-evaluator-windows-x64"
            setup = next(windows.glob("*-Setup.exe"))
            setup.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                release_assemble.assemble_release(
                    source, root / "publish", self.version
                )

    def test_empty_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._artifact_tree(root)
            windows = source / "fantasy-trade-evaluator-windows-x64"
            next(windows.glob("*-Setup.exe")).write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "empty payload"):
                release_assemble.assemble_release(
                    source, root / "publish", self.version
                )

    def test_missing_or_extra_platform_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._artifact_tree(root)
            linux = source / "fantasy-trade-evaluator-linux-x64"
            (linux / "unexpected.bin").write_bytes(b"unexpected")
            with self.assertRaisesRegex(ValueError, "wrong payload set"):
                release_assemble.assemble_release(
                    source, root / "publish", self.version
                )

    def test_missing_platform_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._artifact_tree(root)
            shutil.rmtree(source / "fantasy-trade-evaluator-linux-arm64")
            with self.assertRaisesRegex(ValueError, "exact platform set"):
                release_assemble.assemble_release(
                    source, root / "publish", self.version
                )

    def test_manifest_rejects_paths_and_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / release_assemble.CHECKSUM_NAME
            digest = "0" * 64
            for content in (
                f"{digest}  ../payload.exe\n",
                f"{digest}  payload.exe\n{digest}  payload.exe\n",
            ):
                with self.subTest(content=content):
                    manifest.write_text(content, encoding="ascii", newline="\n")
                    with self.assertRaises(ValueError):
                        release_assemble._read_manifest(manifest)

    def test_nonempty_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._artifact_tree(root)
            destination = root / "publish"
            destination.mkdir()
            (destination / "user-file").write_text("keep")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                release_assemble.assemble_release(
                    source, destination, self.version
                )
            self.assertEqual((destination / "user-file").read_text(), "keep")

    def test_invalid_version_is_rejected_before_reading_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "digits and periods"):
                release_assemble.assemble_release(
                    root, root / "publish", "0.1.0/../../unexpected"
                )


if __name__ == "__main__":
    unittest.main()
