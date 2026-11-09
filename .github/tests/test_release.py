import importlib.util
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release_contract", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseContractTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.knowledge = self.root / "apps" / "knowledge"
        self.web = self.root / "apps" / "web"
        self.knowledge.mkdir(parents=True)
        self.web.mkdir(parents=True)
        (self.knowledge / "pyproject.toml").write_text(
            '[project]\nname="knowledge"\nversion="0.1.0"\n'
        )
        (self.knowledge / "uv.lock").write_text(
            'version=1\n[[package]]\nname="knowledge"\nversion="0.1.0"\n'
        )
        (self.web / "package.json").write_text(
            json.dumps({"name": "web", "version": "0.1.0"})
        )
        self.lock = {
            "name": "web",
            "version": "0.1.0",
            "packages": {"": {"name": "web", "version": "0.1.0"}},
        }
        self.write_npm_lock()

    def write_npm_lock(self):
        (self.web / "package-lock.json").write_text(json.dumps(self.lock))

    def test_packages_and_lock_roots_match_release(self):
        release.check_versions(self.root, "0.1.0")

    def test_local_check_needs_no_github_credentials(self):
        with (
            patch.object(release, "ROOT", self.root),
            patch.object(
                release,
                "api",
                side_effect=AssertionError("Local check must not call GitHub"),
            ),
            patch("sys.argv", ["release.py", "check", "--release-version", "0.1.0"]),
            patch.dict(os.environ, {"GITHUB_OUTPUT": ""}),
            redirect_stdout(io.StringIO()) as output,
        ):
            release.main()
        self.assertEqual(json.loads(output.getvalue())["version"], "0.1.0")

    def test_ready_gate_rejects_skipped_release_image_builds(self):
        workflow = (SCRIPT.parents[1] / "workflows" / "ci.yml").read_text()
        script = textwrap.dedent(
            workflow.split("  ready:", 1)[1].split("        run: |", 1)[1]
        )
        cases = [
            ("success", "true", "true", "success", True),
            ("success", "true", "true", "skipped", False),
            ("success", "true", "true", "failure", False),
            ("success", "false", "true", "skipped", True),
            ("success", "true", "false", "skipped", True),
            ("failure", "false", "true", "skipped", False),
        ]
        for checks, release_pr, images, image_result, allowed in cases:
            with self.subTest(
                checks=checks, release_pr=release_pr, image_result=image_result
            ):
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script],
                    check=False,
                    env={
                        **os.environ,
                        "CHECKS_RESULT": checks,
                        "RELEASE_PR": release_pr,
                        "HAS_IMAGES": images,
                        "IMAGES_RESULT": image_result,
                    },
                    capture_output=True,
                )
                self.assertEqual(result.returncode == 0, allowed)

    def test_stale_uv_project_version_is_rejected(self):
        p = self.knowledge / "uv.lock"
        p.write_text(p.read_text().replace('version="0.1.0"', 'version="0.0.9"'))
        with self.assertRaisesRegex(ValueError, "uv.lock"):
            release.check_versions(self.root, "0.1.0")

    def test_both_npm_root_versions_are_checked(self):
        for root in ("outer", "package"):
            with self.subTest(root=root):
                self.lock["version"] = "0.1.0"
                self.lock["packages"][""]["version"] = "0.1.0"
                if root == "outer":
                    self.lock["version"] = "0.0.9"
                else:
                    self.lock["packages"][""]["version"] = "0.0.9"
                self.write_npm_lock()
                with self.assertRaisesRegex(ValueError, "package-lock.json"):
                    release.check_versions(self.root, "0.1.0")

    def test_release_pr_direction_title_and_merge_commit(self):
        pr = {
            "base": {"ref": "main"},
            "head": {
                "ref": "release/v0.1.0",
                "sha": "head",
                "repo": {"full_name": "owner/repo"},
            },
            "title": "Release v0.1.0",
        }
        self.assertTrue(release.check_pr(pr, "0.1.0", "owner/repo"))
        release.check_merge(pr, ["base", "head"])
        with self.assertRaisesRegex(ValueError, "merge commit"):
            release.check_merge(pr, ["base"])
        pr["title"] = "Some release"
        with self.assertRaisesRegex(ValueError, "title"):
            release.check_pr(pr, "0.1.0", "owner/repo")

    def test_feature_pr_cannot_target_main_or_wrong_milestone(self):
        pr = {"base": {"ref": "release/v0.1.0"}, "head": {"ref": "feature/test"}}
        self.assertFalse(release.check_pr(pr, "0.1.0", "owner/repo"))
        with self.assertRaises(ValueError):
            release.check_pr(pr, "0.2.0", "owner/repo")
        pr["base"]["ref"] = "main"
        with self.assertRaises(ValueError):
            release.check_pr(pr, "0.1.0", "owner/repo")

    def test_only_exact_merged_release_commit_can_publish(self):
        pr = {"merged_at": "date", "merge_commit_sha": "merge", "base": {"ref": "main"}}
        self.assertEqual(release.merged_release([pr], "merge"), pr)
        with self.assertRaises(ValueError):
            release.merged_release([pr], "other-commit")
        with self.assertRaises(ValueError):
            release.merged_release([pr, pr], "merge")

    def test_web_is_bundled_and_worker_reuses_knowledge_image(self):
        (self.knowledge / "Dockerfile").write_text("FROM python:3.12-slim\n")
        matrix = release.image_matrix(self.root, "Owner/Repo")
        self.assertEqual(len(matrix), 1)
        self.assertEqual(matrix[0]["service"], "knowledge")
        self.assertEqual(matrix[0]["image"], "ghcr.io/owner/repo-knowledge")


if __name__ == "__main__":
    unittest.main()
