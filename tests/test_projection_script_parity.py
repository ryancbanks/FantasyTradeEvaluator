from pathlib import Path
import unittest

from trade_snapshot._capture_scripts import (
    ADVANCE_PROJECTION_SCRIPT,
    CONFIGURE_PROJECTION_SCRIPT,
    PROJECTION_TABLE_SCRIPT,
)


EXTENSION_COLLECTORS = (
    Path(__file__).resolve().parents[1]
    / "trade_snapshot"
    / "browser_extension"
    / "collectors"
)


def _extension_function(filename: str, function_name: str) -> str:
    source = (EXTENSION_COLLECTORS / filename).read_text(encoding="utf-8")
    marker = f"  const {function_name} = "
    try:
        body = source.split(marker, 1)[1].split("\n\n  handlers[", 1)[0].rstrip()
    except IndexError as exc:
        raise AssertionError(f"could not find {function_name} in {filename}") from exc
    if not body.endswith(";"):
        raise AssertionError(f"{function_name} is not a complete assignment")
    return body[:-1]


class ProjectionScriptParityTests(unittest.TestCase):
    def test_extension_collectors_match_direct_browser_scripts(self) -> None:
        pairs = (
            (
                "projection_configure.js",
                "configureProjection",
                CONFIGURE_PROJECTION_SCRIPT,
            ),
            ("projection_read.js", "readProjection", PROJECTION_TABLE_SCRIPT),
            (
                "projection_advance.js",
                "advanceProjection",
                ADVANCE_PROJECTION_SCRIPT,
            ),
        )
        for filename, function_name, direct_script in pairs:
            with self.subTest(filename=filename):
                self.assertEqual(
                    _extension_function(filename, function_name).strip(),
                    direct_script.strip(),
                )


if __name__ == "__main__":
    unittest.main()
