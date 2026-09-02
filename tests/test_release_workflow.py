from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

    def test_tag_values_are_never_interpolated_into_shell_source(self):
        self.assertNotIn("${{ github.ref_name }}", self.workflow)
        self.assertIn('os.environ["GITHUB_REF_NAME"]', self.workflow)

    def test_actions_are_immutable_and_checkout_does_not_persist_credentials(self):
        action_references = re.findall(r"uses: [^@\s]+@([^\s]+)", self.workflow)
        self.assertTrue(action_references)
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in action_references)
        )
        self.assertEqual(self.workflow.count("persist-credentials: false"), 3)

    def test_manual_builds_are_unprivileged_and_cannot_publish(self):
        self.assertIn(
            "environment: ${{ github.event_name == 'push' && 'release' || 'build' }}",
            self.workflow,
        )
        self.assertIn(
            "if: github.event_name == 'push' && github.ref_type == 'tag'",
            self.workflow,
        )
        self.assertIn("contents: read", self.workflow)
        self.assertEqual(self.workflow.count("contents: write"), 1)

    def test_native_builds_run_fresh_after_unprivileged_source_verification(self):
        verify_job, release_job = self.workflow.split("\n  release:\n", maxsplit=1)
        self.assertIn(
            'python -m pip install --disable-pip-version-check ".[browser-test]"',
            verify_job,
        )
        self.assertIn("python packaging/smoke_wheel.py", verify_job)
        self.assertNotIn(".[browser-test]", release_job)
        self.assertNotIn("packaging/smoke_wheel.py", release_job)
        self.assertIn("needs: verify", release_job)
        self.assertIn("--only-binary=:all:", release_job)

    def test_build_lock_is_checked_and_canonically_exported(self):
        self.assertIn("uv lock --check", self.workflow)
        self.assertIn("uv export --locked --extra build", self.workflow)
        self.assertIn("diff -u packaging/build-requirements.txt", self.workflow)
        self.assertNotIn("tail -n +5", self.workflow)

    def test_release_is_verified_and_recoverable_before_publication(self):
        self.assertIn("retention-days: 14", self.workflow)
        self.assertNotIn("merge-multiple: true", self.workflow)
        self.assertIn("python release_assemble.py", self.workflow)
        self.assertIn('gh release create "$GITHUB_REF_NAME"', self.workflow)
        self.assertIn("Automated release draft for commit $GITHUB_SHA", self.workflow)
        self.assertIn("--draft", self.workflow)
        self.assertIn('gh release upload "$GITHUB_REF_NAME" publish/* --clobber', self.workflow)
        self.assertIn('[ "$body" != "$marker" ]', self.workflow)
        self.assertIn("python release_inventory.py publish", self.workflow)
        self.assertIn('gh release edit "$GITHUB_REF_NAME" --draft=false', self.workflow)
        self.assertRegex(self.workflow, r"expectedHash = '[0-9a-f]{64}'")


if __name__ == "__main__":
    unittest.main()
