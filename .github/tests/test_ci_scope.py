import importlib.util
import os
import subprocess
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_scope.py"
spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class CIScopeTests(unittest.TestCase):
    def test_feature_checks_follow_changed_area(self):
        cases = [
            ([], "false", "false"),
            (["README.md", "docs/release.md"], "false", "false"),
            (["apps/web/UPSTREAM.md"], "false", "false"),
            (["tools/evaluation/gold_set/gold.jsonl"], "false", "false"),
            (["tools/sql/corpus-documents.sql"], "false", "false"),
            (["apps/web/src/App.svelte"], "true", "false"),
            (["apps/web/package-lock.json"], "true", "false"),
            (["apps/knowledge/uv.lock"], "false", "true"),
            (["apps/knowledge/tests/test_health.py"], "false", "true"),
            (["tools/evaluation/evaluate.py"], "false", "true"),
            (["apps/web/src/a.ts", "apps/knowledge/src/a.py"], "true", "true"),
            (["compose.yaml"], "true", "true"),
            ([".github/workflows/ci.yml"], "true", "true"),
            (["new-unclassified-file"], "true", "true"),
        ]
        for paths, web, knowledge in cases:
            with self.subTest(paths=paths):
                self.assertEqual(
                    scope.select_checks(paths), {"web": web, "knowledge": knowledge}
                )

    def test_release_always_checks_both_apps(self):
        for paths in ([], ["README.md"], ["apps/web/src/a.ts"]):
            with self.subTest(paths=paths):
                self.assertEqual(
                    scope.select_checks(paths, release_pr=True),
                    {"web": "true", "knowledge": "true"},
                )

    def test_ready_gate_requires_selected_jobs_to_succeed(self):
        workflow = (SCRIPT.parents[1] / "workflows" / "ci.yml").read_text()
        script = textwrap.dedent(
            workflow.split("  ready:", 1)[1].split("        run: |", 1)[1]
        )
        # Docs, frontend-only and backend-only changes may skip unrelated jobs.
        cases = [
            ("false", "skipped", "false", "skipped", True),
            ("true", "success", "false", "skipped", True),
            ("false", "skipped", "true", "success", True),
            ("true", "success", "true", "success", True),
            ("true", "skipped", "false", "skipped", False),
            ("true", "failure", "false", "skipped", False),
            ("false", "skipped", "true", "skipped", False),
            ("false", "skipped", "true", "cancelled", False),
            ("true", "success", "true", "failure", False),
        ]
        for web, web_result, knowledge, knowledge_result, allowed in cases:
            with self.subTest(web=web_result, knowledge=knowledge_result):
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script],
                    check=False,
                    env={
                        **os.environ,
                        "CHECKS_RESULT": "success",
                        "WEB_REQUIRED": web,
                        "WEB_RESULT": web_result,
                        "KNOWLEDGE_REQUIRED": knowledge,
                        "KNOWLEDGE_RESULT": knowledge_result,
                        "RELEASE_PR": "false",
                        "HAS_IMAGES": "true",
                        "IMAGES_RESULT": "skipped",
                    },
                    capture_output=True,
                )
                self.assertEqual(result.returncode == 0, allowed)


if __name__ == "__main__":
    unittest.main()
