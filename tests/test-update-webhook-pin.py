#!/usr/bin/env python3
"""Release selection and refusal tests for the pin updater; no network."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import subprocess
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

    def test_renamed_pin_pr_is_not_duplicated(self):
        self.pulls = [{"number": 42}]
        self.files = [{"filename": "nix/new-pin.nix", "previous_filename": updater.PIN}]
        self.assertFalse(self.prepare()[0]["changed"])
        self.assertEqual(self.pin.read_text(), self.original)

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

    def test_cli_is_read_only_by_default(self):
        output = io.StringIO()
        with patch.object(updater, "ROOT", self.root), \
                patch.dict(updater.os.environ, {"GITHUB_REPOSITORY": "test/repo"}), \
                patch.object(sys, "argv", ["updater"]), redirect_stdout(output):
            self.assertEqual(updater.main(), 0)
        self.assertTrue(json.loads(output.getvalue())["changed"])
        self.assertEqual(self.pin.read_text(), self.original)

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
        self.assertEqual(
            job["if"],
            "github.repository == 'defangdevs/agent-box' && github.ref == 'refs/heads/master'",
        )
        self.assertEqual(workflow["permissions"], {"contents": "write", "pull-requests": "write"})
        script = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("--body-file", script)
        self.assertIn("git add nix/webhook-pin.nix modules/agent-box.nix", script)
        self.assertNotIn("gh pr merge", script)
        self.assertNotIn("--force", script)

    def publish(self, scenario):
        """Execute the actual publication step with logging git/gh stubs."""
        workflow = yaml.safe_load((ROOT / ".github/workflows/update-webhook-pin.yml").read_text())
        step = next(step for step in workflow["jobs"]["update"]["steps"]
                    if step.get("name") == "Open a bump PR for review")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "commands.jsonl"
            for name in ("git", "gh"):
                stub = root / name
                stub.write_text(f"#!{sys.executable}\n" + r"""
import json, os, pathlib, sys
log = pathlib.Path(os.environ['COMMAND_LOG'])
args = [pathlib.Path(sys.argv[0]).name] + sys.argv[1:]
with log.open('a') as out:
    out.write(json.dumps(args) + '\n')
scenario = os.environ['SCENARIO']
if args[:3] == ['gh', 'pr', 'list']:
    queries = sum(json.loads(line)[:3] == ['gh', 'pr', 'list'] for line in log.read_text().splitlines())
    if scenario == 'query-failure' and queries > 1:
        sys.exit(1)
    if scenario == 'existing' or (scenario == 'ambiguous' and queries > 1):
        print('https://github.com/test/repo/pull/42')
if args[:3] == ['gh', 'pr', 'create'] and scenario != 'success':
    sys.exit(1)
""")
                stub.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       COMMAND_LOG=str(log), SCENARIO=scenario, GITHUB_RUN_ID="123",
                       GITHUB_RUN_ATTEMPT="1", GITHUB_REPOSITORY="test/repo",
                       VERSION="v0.10.0", RUNNER_TEMP=directory)
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", step["run"]],
                                    env=env, text=True, capture_output=True)
            self.assertTrue(log.exists(), result.stderr)
            return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_publication_pushes_before_creating_pr(self):
        result, calls = self.publish("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[:3] for call in calls], [
            ["gh", "pr", "list"], ["git", "switch", "-c"], ["git", "config", "user.name"],
            ["git", "config", "user.email"], ["git", "add", "nix/webhook-pin.nix"],
            ["git", "commit", "-m"], ["gh", "auth", "setup-git"],
            ["git", "push", "origin"], ["gh", "pr", "create"],
        ])
        self.assertEqual(calls[4], ["git", "add", "nix/webhook-pin.nix", "modules/agent-box.nix"])
        self.assertEqual(calls[7], ["git", "push", "origin", "codex/update-local-webhook-123-1"])
        self.assertEqual(calls[8][3:11], ["--repo", "test/repo", "--base", "master", "--head",
                                       "codex/update-local-webhook-123-1", "--title",
                                       "chore(webhook): bump local-webhook to v0.10.0"])
        self.assertEqual(calls[8][-2], "--body-file")

    def test_publication_preserves_existing_pr(self):
        result, calls = self.publish("existing")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--head", calls[0])
        self.assertIn("--base", calls[0])

    def test_ambiguous_create_response_preserves_reconciled_pr(self):
        result, calls = self.publish("ambiguous")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[-1][:3], ["gh", "pr", "list"])
        self.assertFalse(any("--delete" in call for call in calls))

    def test_failed_create_deletes_only_confirmed_orphan(self):
        result, calls = self.publish("failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-2][:3], ["gh", "pr", "list"])
        self.assertEqual(calls[-1], ["git", "push", "origin", "--delete",
                                    "codex/update-local-webhook-123-1"])

    def test_failed_reconciliation_preserves_branch(self):
        result, calls = self.publish("query-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("--delete" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
