#!/usr/bin/env python3
"""Shared capacity policy and real concurrent CLI admission (issue #662)."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "modules/src"
spec = importlib.util.spec_from_file_location("capacity", SRC / "lib/session-capacity.py")
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


def expand(path):
    return re.sub(r"^\s*@@include:(\S+)@@\s*$",
                  lambda m: expand(SRC / m[1]), path.read_text(), flags=re.M)


class Policy(unittest.TestCase):
    def test_pending_and_unregistered_live_names_hold_slots(self):
        with self.assertRaises(capacity.SessionCapacityError):
            capacity.capacity_check({"pending": {}}, ["new"],
                                    live={"unregistered"}, limit=2)

    def test_stopped_is_free_only_after_pane_exits(self):
        sessions = {"stopped": {"stopped": True}}
        capacity.capacity_check(sessions, ["new"], live=set(), limit=1)
        with self.assertRaises(capacity.SessionCapacityError):
            capacity.capacity_check(sessions, ["new"], live={"stopped"}, limit=1)

    def test_died_never_holds_a_slot_even_with_its_pane_still_up(self):
        # A crash is flagged `died`, not `stopped` (issue #516), so its pane
        # lingers as a post-mortem shell tmux still reports, and its entry
        # never gets `stopped` either -- before this fix that meant a died
        # session held its slot for good (issue #523), unlike a stopped one
        # which frees its slot once the pane actually exits.
        sessions = {"crashed": {"died": 1}}
        capacity.capacity_check(sessions, ["new"], live=set(), limit=1)
        capacity.capacity_check(sessions, ["new"], live={"crashed"}, limit=1)

    def test_restart_does_not_need_a_second_slot(self):
        capacity.capacity_check({"a": {}, "b": {}}, ["a"], live={"a"}, limit=1)

    def test_boot_queue_respects_limit_without_deadlocking(self):
        sessions = {"a": {}, "b": {}, "c": {}}
        capacity.capacity_check(sessions, ["a"], spawning=True, live=set(), limit=2)
        capacity.capacity_check(sessions, ["b"], spawning=True, live={"a"}, limit=2)
        with self.assertRaises(capacity.SessionCapacityError):
            capacity.capacity_check(sessions, ["c"], spawning=True, live={"a"}, limit=2)
        sessions["a"]["stopped"] = True
        capacity.capacity_check(sessions, ["c"], spawning=True, live={"b"}, limit=2)

    def test_running_and_pending_are_not_double_counted(self):
        result = capacity.capacity_check({"a": {}}, ["b"], live={"a"}, limit=2)
        self.assertEqual(result["used"], 1)

    def test_capacity_live_excludes_connect_flow_panes(self):
        # The settings page's sign-in flow runs on this same tmux socket as
        # a "_connect-<flow>" pane (settings-daemon.py's CONNECT_PREFIX) but
        # was never registered as a session; it must not cost a slot.
        proc = subprocess.CompletedProcess(
            [], 0, stdout="main\n_connect-abc123\n", stderr="")
        with mock.patch.object(capacity.capacity_subprocess, "run",
                                return_value=proc):
            self.assertEqual(capacity.capacity_live(), {"main"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.registry = self.work / "sessions.json"
        self.limit = self.work / "limit"
        self.limit.write_text("2\n")
        self.registry.write_text(json.dumps({"version": 1, "sessions": {}}))
        self.cli = self.work / "session"
        self.cli.write_text(expand(SRC / "session-cli.sh"))
        helper = self.work / "capacity"
        helper.write_text("#!" + sys.executable + "\n" +
                          (SRC / "lib/session-capacity.py").read_text() + "\n" +
                          (SRC / "session-capacity-cli.py").read_text())
        helper.chmod(0o755)
        tmux = self.work / "tmux"
        tmux.write_text("#!" + shutil.which("bash") + "\n"
                        "echo 'no server running' >&2\nexit 1\n")
        tmux.chmod(0o755)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.work),
                    "USER": "capacity-test", "AGENT_BOX_AGENTS": "shell",
                    "AGENT_BOX_DEFAULT_AGENT": "shell",
                    "AGENT_BOX_SESSIONS_FILE": str(self.registry),
                    "AGENT_BOX_SESSION_LIMIT_FILE": str(self.limit),
                    "AGENT_BOX_CAPACITY_BIN": str(helper),
                    "AGENT_BOX_TMUX_BIN": str(tmux),
                    "AGENT_BOX_FLOCK_BIN": shutil.which("flock")}

    def run_cli(self, *args):
        return subprocess.run([shutil.which("bash"), str(self.cli), *args],
                              env=self.env, capture_output=True, text=True,
                              timeout=15)

    def test_concurrent_adds_reserve_exactly_two_slots(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.run_cli("add", "s%d" % i), range(8)))
        self.assertEqual(sorted(r.returncode for r in results), [0, 0] + [75] * 6,
                         [(r.returncode, r.stderr) for r in results])
        self.assertEqual(len(json.loads(self.registry.read_text())["sessions"]), 2)

    def test_restart_all_refuses_without_partial_mutation(self):
        self.registry.write_text(json.dumps({"version": 1, "sessions": {
            "a": {}, "b": {}, "c": {"stopped": True}}}))
        before = self.registry.read_bytes()
        result = self.run_cli("restart", "--all")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertIn("Session limit reached", result.stderr)

    def test_stop_frees_capacity_and_restart_rechecks(self):
        for name in ["a", "b"]:
            self.assertEqual(self.run_cli("add", name).returncode, 0)
        self.assertEqual(self.run_cli("stop", "a").returncode, 0)
        self.assertEqual(self.run_cli("add", "c").returncode, 0)
        self.assertEqual(self.run_cli("restart", "a").returncode, 75)
        self.assertEqual(self.run_cli("restart", "b").returncode, 0)

    def test_unavailable_probe_fails_closed(self):
        self.env["AGENT_BOX_TMUX_BIN"] = str(self.work / "missing")
        before = self.registry.read_bytes()
        self.assertEqual(self.run_cli("add", "a").returncode, 75)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_invalid_limit_fails_closed(self):
        for value in ["0", "-1", "abc"]:
            self.limit.write_text(value)
            self.assertEqual(self.run_cli("add", "a").returncode, 75)

    def test_capacity_verb_does_not_create_the_registry(self):
        # A read-only check must never be what CREATES the registry: a
        # supervisor seed sees an "existing" (empty) file as one already
        # populated and never seeds the NixOS-declared sessions onto it.
        self.registry.unlink()
        result = self.run_cli("capacity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["used"], 0)
        self.assertFalse(self.registry.exists())


if __name__ == "__main__":
    unittest.main()
