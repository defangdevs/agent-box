#!/usr/bin/env python3
"""Release selection and refusal tests for the pin updater; no network."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update_webhook_pin as updater  # noqa: E402

OLD = "a" * 40
NEW = "b" * 40
CURRENT = b"VERSION = '0.9.0'\n"
RELEASE = b"VERSION = '0.10.0'\nraise RuntimeError('must never execute upstream source')\n"


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "nix").mkdir()
        self.pin = self.root / updater.PIN
        self.original = (
            '# Preserve this comment.\n{\n  repo = "defangdevs/local-channels";\n'
            f'  rev = "{OLD}";\n  sha256 = "{updater.digest(CURRENT)}";\n}}\n'
        )
        self.pin.write_text(self.original)
        self.tags = [
            {"name": "v0.9.0", "commit": {"sha": OLD}},
            {"name": "v0.10.0", "commit": {"sha": NEW}},
            {"name": "v9.0.0-rc1", "commit": {"sha": "c" * 40}},
            {"name": "local-slack-v99.0.0", "commit": {"sha": "d" * 40}},
        ]
        self.pulls = []
        self.files = []
        self.runs = [{
            "head_sha": NEW, "head_branch": "main", "event": "push",
            "path": ".github/workflows/ci.yml", "status": "completed",
            "conclusion": "success", "html_url": "https://github.com/ci/1",
        }]
        self.comparison = [{"status": "ahead", "commits": [
            {"sha": NEW, "commit": {"message": "local-webhook 0.10.0: change defaults\n\nDetails"}},
        ]}]
        self.payloads = {OLD: CURRENT, NEW: RELEASE}
        self.api_calls = []
        self.start_patch("api", side_effect=self.api)
        self.start_patch("download", side_effect=lambda sha: self.payloads[sha])

    def start_patch(self, name, **kwargs):
        patcher = patch.object(updater, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def api(self, endpoint, paginate=False):
        self.api_calls.append(endpoint)
        if endpoint == "repos/test/repo/pulls?state=open&per_page=100":
            result = [self.pulls]
        elif endpoint == "repos/test/repo/pulls/42/files?per_page=100":
            result = [self.files]
        elif "/tags?" in endpoint:
            result = [self.tags[:1], self.tags[1:]]  # Highest tag is on a later page.
        elif endpoint == f"repos/{updater.UPSTREAM}":
            result = {"default_branch": "main"}
        elif "/actions/workflows/ci.yml/runs?" in endpoint:
            self.assertIn(f"event=push&head_sha={NEW}", endpoint)
            result = {"workflow_runs": self.runs}
        elif "/compare/" in endpoint:
            result = self.comparison
        else:
            self.fail(f"Unexpected API request: {endpoint}")
        return copy.deepcopy(result)

    def prepare(self):
        return updater.prepare(self.root, "test/repo")

    def test_newest_stable_release_hash_and_changelog(self):
        plan, updated, body = self.prepare()
        self.assertEqual(plan, {"changed": True, "version": "v0.10.0", "rev": NEW})
        fields = updater.pin_fields(updated)
        self.assertEqual(fields["sha256"], updater.digest(RELEASE))
        self.assertEqual(fields["rev"], NEW)
        self.assertTrue(updated.startswith("# Preserve this comment."))
        self.assertIn("change defaults", body)
        self.assertIn("https://github.com/ci/1", body)
        self.assertIn("Approve workflows to run", body)
        self.assertEqual(self.pin.read_text(), self.original)  # Preparation is read-only.

    def test_diverged_pre_squash_pin_is_flagged_for_review(self):
        self.comparison[0]["status"] = "diverged"
        self.assertIn("History diverged", self.prepare()[2])

    def test_manual_pin_pr_is_not_duplicated(self):
        self.pulls = [{"number": 42}]
        self.files = [{"filename": updater.PIN}]
        plan, updated, _ = self.prepare()
        self.assertFalse(plan["changed"])
        self.assertIn("PR #42", plan["reason"])
        self.assertEqual(updated, self.original)
        self.assertEqual(len(self.api_calls), 2)

    def test_unrelated_pr_does_not_block_update(self):
        self.pulls = [{"number": 42}]
        self.files = [{"filename": "README.md"}]
        self.assertTrue(self.prepare()[0]["changed"])

    def test_latest_pin_is_noop(self):
        self.pin.write_text(self.original.replace(OLD, NEW))
        self.assertFalse(self.prepare()[0]["changed"])

    def test_no_version_downgrade(self):
        self.tags = self.tags[:1]
        self.tags[0]["commit"]["sha"] = NEW
        self.assertFalse(self.prepare()[0]["changed"])

    def test_older_release_is_not_proposed(self):
        self.tags = [{"name": "v0.8.0", "commit": {"sha": NEW}}]
        self.assertFalse(self.prepare()[0]["changed"])

    def test_unsafe_revision_or_repository_is_refused(self):
        self.tags[1]["commit"]["sha"] = "$(touch unexpected)"
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            self.prepare()
        self.pin.write_text(self.original.replace(updater.UPSTREAM, "other/repo"))
        with self.assertRaisesRegex(ValueError, "Unexpected upstream"):
            self.prepare()

    def test_tag_must_match_payload_version(self):
        self.payloads[NEW] = b"VERSION = '0.8.0'\n"
        with self.assertRaisesRegex(ValueError, "tag does not match"):
            self.prepare()

    def test_current_hash_must_match(self):
        self.payloads[OLD] += b"# unexpected change\n"
        with self.assertRaisesRegex(ValueError, "pinned hash"):
            self.prepare()

    def test_no_stable_tags(self):
        self.tags = []
        with self.assertRaisesRegex(ValueError, "No stable"):
            self.prepare()

    def test_never_pin_behind_history(self):
        self.comparison[0]["status"] = "behind"
        with self.assertRaisesRegex(ValueError, "does not advance"):
            self.prepare()

    def test_exact_latest_upstream_ci_must_be_green(self):
        original = copy.deepcopy(self.runs)
        for key, value in (
            ("head_sha", OLD), ("head_branch", "feature"), ("event", "pull_request"),
            ("path", ".github/workflows/other.yml"), ("status", "in_progress"),
            ("conclusion", "failure"), ("conclusion", "cancelled"),
        ):
            with self.subTest(key=key, value=value):
                self.runs = copy.deepcopy(original)
                self.runs[0][key] = value
                with self.assertRaisesRegex(ValueError, "not green"):
                    self.prepare()
                self.assertEqual(self.pin.read_text(), self.original)
        self.runs = []
        with self.assertRaisesRegex(ValueError, "not green"):
            self.prepare()

    def test_cli_writes_only_after_all_checks_pass(self):
        with patch.object(updater, "ROOT", self.root), \
                patch.dict(updater.os.environ, {"GITHUB_REPOSITORY": "test/repo"}), \
                patch.object(sys, "argv", ["updater", "--write"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.runs = []
            self.assertEqual(updater.main(), 1)
            self.assertEqual(self.pin.read_text(), self.original)
            self.runs = [{
                "head_sha": NEW, "head_branch": "main", "event": "push",
                "path": ".github/workflows/ci.yml", "status": "completed",
                "conclusion": "success", "html_url": "https://github.com/ci/1",
            }]
            self.assertEqual(updater.main(), 0)
            self.assertEqual(updater.pin_fields(self.pin.read_text())["rev"], NEW)

    def test_changelog_pagination(self):
        self.comparison.append({"commits": [
            {"sha": "c" * 40, "commit": {"message": "Later page change"}},
        ]})
        self.assertIn("Later page change", self.prepare()[2])


class WorkflowTests(unittest.TestCase):
    def test_workflow_is_scheduled_review_only_and_scoped(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/update-webhook-pin.yml").read_text())
        triggers = workflow.get("on", workflow.get(True))  # PyYAML's YAML 1.1 boolean.
        self.assertEqual(triggers["schedule"], [{"cron": "23 7 * * *"}])
        self.assertIn("workflow_dispatch", triggers)
        job = workflow["jobs"]["update"]
        self.assertIn("github.ref == 'refs/heads/master'", job["if"])
        self.assertEqual(workflow["permissions"], {"contents": "write", "pull-requests": "write"})
        script = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("--body-file", script)
        self.assertIn("git add nix/webhook-pin.nix modules/agent-box.nix", script)
        self.assertNotIn("gh pr merge", script)
        self.assertNotIn("--force", script)


if __name__ == "__main__":
    unittest.main()
