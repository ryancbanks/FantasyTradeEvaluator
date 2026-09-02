from pathlib import Path
from hashlib import sha256
import tempfile
import unittest

import release_inventory


class ReleaseInventoryTests(unittest.TestCase):
    def _release(self, root: Path) -> tuple[Path, dict[str, list[dict[str, object]]]]:
        directory = root / "publish"
        directory.mkdir(parents=True)
        (directory / "installer.exe").write_bytes(b"installer")
        (directory / "SHA256SUMS").write_bytes(b"checksums")
        response = {
            "assets": [
                {
                    "name": child.name,
                    "size": child.stat().st_size,
                    "digest": f"sha256:{sha256(child.read_bytes()).hexdigest()}",
                    "state": "uploaded",
                }
                for child in directory.iterdir()
            ]
        }
        return directory, response

    def test_exact_remote_inventory_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, response = self._release(Path(temporary))
            release_inventory.verify_remote_inventory(directory, response)

    def test_resumed_draft_with_extra_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, response = self._release(Path(temporary))
            response["assets"].append(
                {
                    "name": "stale-installer.exe",
                    "size": 10,
                    "digest": f"sha256:{'0' * 64}",
                    "state": "uploaded",
                }
            )
            with self.assertRaisesRegex(ValueError, "wrong asset set"):
                release_inventory.verify_remote_inventory(directory, response)

    def test_missing_or_wrong_sized_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, response = self._release(Path(temporary))
            response["assets"].pop()
            with self.assertRaisesRegex(ValueError, "wrong asset set"):
                release_inventory.verify_remote_inventory(directory, response)

            _, response = self._release(Path(temporary) / "another")
            response["assets"][0]["size"] = 1
            with self.assertRaisesRegex(ValueError, "size or digest mismatch"):
                release_inventory.verify_remote_inventory(
                    Path(temporary) / "another" / "publish", response
                )

    def test_wrong_digest_or_incomplete_upload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, response = self._release(Path(temporary))
            response["assets"][0]["digest"] = f"sha256:{'0' * 64}"
            with self.assertRaisesRegex(ValueError, "size or digest mismatch"):
                release_inventory.verify_remote_inventory(directory, response)

            _, response = self._release(Path(temporary) / "another")
            response["assets"][0]["state"] = "new"
            with self.assertRaisesRegex(ValueError, "invalid asset metadata"):
                release_inventory.verify_remote_inventory(
                    Path(temporary) / "another" / "publish", response
                )

    def test_duplicate_remote_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, response = self._release(Path(temporary))
            response["assets"].append(response["assets"][0].copy())
            with self.assertRaisesRegex(ValueError, "duplicate asset"):
                release_inventory.verify_remote_inventory(directory, response)


if __name__ == "__main__":
    unittest.main()
