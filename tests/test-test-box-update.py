#!/usr/bin/env python3
"""Operator trial lifecycle without modifying the host's runtime profile."""

import contextlib
import io
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock


HELPER = Path(__file__).resolve().parents[1] / "scripts/test-box-update.py"


class TrialTest(unittest.TestCase):
    def setUp(self):
        self.helper = runpy.run_path(str(HELPER))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / "profile"
        self.generation = self.root / "generation"
        (self.generation / "bin").mkdir(parents=True)
        (self.generation / "bin/agentbox").touch()
        self.profile.symlink_to(self.generation)
        self.state = self.root / "state"
        self.current = "1" * 40
        self.target = "2" * 40
        self.calls = []
        self.fail = False
        self.noop = False
        self.cli = {"profile_origin": lambda profile: ("owner/repo", self.current),
                    "REV_RE": re.compile(r"[0-9a-f]{40}"),
                    "source_tree": self.resolve}
        patch = mock.patch("subprocess.run", side_effect=self.run_command)
        patch.start()
        self.addCleanup(patch.stop)
        self.output = contextlib.ExitStack()
        self.output.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.output.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(self.output.close)

    def resolve(self, profile, verb, **kwargs):
        self.calls.append((verb, kwargs))
        return self.target

    def run_command(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "nix-store":
            self.assertTrue(kwargs["check"])
            self.assertIn("--indirect", cmd)
            self.assertEqual(cmd[-1], str(self.generation))
            Path(cmd[2]).symlink_to(self.generation)
        elif not self.fail and not self.noop:
            self.current = cmd[cmd.index("--rev") + 1]
        return subprocess.CompletedProcess(cmd, 1 if self.fail else 0)

    def invoke(self, *args):
        parsed = self.helper["parser"]().parse_args([
            *args, "--profile", str(self.profile), "--state-dir", str(self.state)])
        return self.helper["operate"](parsed, self.cli)

    def test_check_resolves_pr_without_creating_state_or_pinning(self):
        self.assertEqual(self.invoke("install", "--pr", "923", "--check"), 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["ref"], "refs/pull/923/head")

    def test_branch_is_resolved_once_and_installed_by_full_sha(self):
        self.invoke("install", "--ref", "refs/heads/feature")
        cmd = self.calls[-1]
        self.assertEqual(cmd[cmd.index("--rev") + 1], self.target)
        self.assertIn("--force", cmd)
        self.assertNotIn("refs/heads/feature", cmd)

    def test_restore_uses_pinned_old_executable_and_removes_record_after_success(self):
        self.invoke("install", "--pr", "923")
        self.assertTrue((self.state / "trial.json").exists())
        self.invoke("restore")
        self.assertEqual(self.current, "1" * 40)
        self.assertEqual(self.calls[-1][0], str(self.state / "baseline/bin/agentbox"))
        self.assertFalse((self.state / "trial.json").exists())
        self.assertFalse((self.state / "baseline").is_symlink())

    def test_failed_install_keeps_baseline_and_can_be_restored(self):
        self.fail = True
        self.assertEqual(self.invoke("install", "--pr", "923"), 1)
        self.assertTrue((self.state / "trial.json").exists())
        self.assertTrue((self.state / "baseline").is_symlink())
        self.fail = False
        self.assertEqual(self.invoke("restore"), 0)

    def test_failed_restore_keeps_recovery_state(self):
        self.invoke("install", "--pr", "923")
        self.fail = True
        self.assertEqual(self.invoke("restore"), 1)
        self.assertTrue((self.state / "trial.json").exists())
        self.assertTrue((self.state / "baseline").is_symlink())

    def test_second_trial_cannot_overwrite_original_baseline(self):
        self.invoke("install", "--pr", "923")
        saved = (self.state / "trial.json").read_text()
        with self.assertRaisesRegex(ValueError, "restore it first"):
            self.invoke("install", "--pr", "924")
        self.assertEqual((self.state / "trial.json").read_text(), saved)

    def test_noop_update_cannot_be_reported_as_installed(self):
        self.noop = True
        with self.assertRaisesRegex(ValueError, "did not match"):
            self.invoke("install", "--pr", "923")
        self.assertTrue((self.state / "trial.json").exists())

    def test_status_never_updates(self):
        self.invoke("install", "--pr", "923")
        self.calls.clear()
        self.assertEqual(self.invoke("status"), 0)
        self.assertEqual(self.calls, [])
        self.assertTrue((self.state / "trial.json").exists())
        self.assertTrue((self.state / "baseline").is_symlink())

    def test_missing_baseline_is_refused_without_erasing_record(self):
        self.invoke("install", "--pr", "923")
        (self.state / "baseline").unlink()
        with self.assertRaisesRegex(ValueError, "closure is missing"):
            self.invoke("restore")
        self.assertTrue((self.state / "trial.json").exists())

    def test_invalid_pr_is_rejected_before_resolution(self):
        for pr in ("0", "-1"):
            with self.assertRaisesRegex(ValueError, "positive"):
                self.invoke("install", "--pr", pr)
        self.assertEqual(self.calls, [])

    def test_already_current_does_not_create_a_trial(self):
        self.target = self.current
        self.assertEqual(self.invoke("install", "--pr", "923"), 0)
        self.assertFalse((self.state / "trial.json").exists())
        self.assertEqual(len(self.calls), 1)

    def test_pin_failure_prevents_installation(self):
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "nix-store")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke("install", "--pr", "923")
        self.assertEqual(self.current, "1" * 40)
        self.assertFalse((self.state / "trial.json").exists())

    def test_writable_state_directory_is_refused(self):
        self.state.mkdir(mode=0o777)
        self.state.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "mode 0700"):
            self.invoke("install", "--pr", "923")
        self.assertEqual(len(self.calls), 1)

    def test_restore_loads_the_pinned_cli_instead_of_the_candidate(self):
        self.invoke("install", "--pr", "923")
        cli = dict(self.cli, ConfigError=ValueError, UpdateError=RuntimeError)
        with mock.patch("os.geteuid", return_value=0), mock.patch(
                "runpy.run_path", return_value=cli) as load:
            # Only exercise main's loader selection; operate's uid check is
            # covered separately using the real test runner's ownership.
            with mock.patch.dict(self.helper["main"].__globals__,
                                 operate=lambda args, loaded: 0):
                self.assertEqual(self.helper["main"]([
                    "restore", "--profile", str(self.profile),
                    "--state-dir", str(self.state)]), 0)
        load.assert_called_once_with(str(self.state / "baseline/bin/agentbox"))

    def test_agent_cannot_invoke_operator_helper(self):
        with mock.patch("os.geteuid", return_value=1000):
            self.assertEqual(self.helper["main"](["restore"]), 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
