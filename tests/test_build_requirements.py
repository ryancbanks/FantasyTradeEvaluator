from pathlib import Path
import re
import tomllib
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = PROJECT_ROOT / "packaging" / "build-requirements.txt"
EXPECTED_PACKAGES = {
    "altgraph",
    "macholib",
    "packaging",
    "pefile",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "pywin32-ctypes",
    "setuptools",
    "xlsxwriter",
}


class BuildRequirementTests(unittest.TestCase):
    def test_hash_locked_requirements_match_the_committed_uv_lock(self):
        requirement_text = REQUIREMENTS.read_text(encoding="utf-8")
        starts = list(
            re.finditer(
                r"(?m)^(?P<name>[a-z0-9][a-z0-9-]*)==(?P<version>[^ ;\\]+)",
                requirement_text,
            )
        )
        self.assertEqual({match.group("name") for match in starts}, EXPECTED_PACKAGES)

        with (PROJECT_ROOT / "uv.lock").open("rb") as source:
            locked = {package["name"]: package for package in tomllib.load(source)["package"]}
        for index, match in enumerate(starts):
            name = match.group("name")
            end = starts[index + 1].start() if index + 1 < len(starts) else None
            block = requirement_text[match.start():end]
            requirement_hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
            package = locked[name]
            lock_hashes = {
                item["hash"].removeprefix("sha256:")
                for item in ([package["sdist"]] + package.get("wheels", []))
            }
            with self.subTest(package=name):
                self.assertEqual(match.group("version"), package["version"])
                self.assertTrue(requirement_hashes)
                self.assertEqual(requirement_hashes, lock_hashes)


if __name__ == "__main__":
    unittest.main()
