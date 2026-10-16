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
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "modules/src"
spec = importlib.util.spec_from_file_location("capacity", SRC / "lib/session-capacity.py")
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)
exec(compile((SRC / "lib/session-hibernation.py").read_text(),
             str(SRC / "lib/session-hibernation.py"), "exec"), capacity.__dict__)


def expand(path):
    return re.sub(r"^\s*@@include:(\S+)@@\s*$",
                  lambda m: expand(SRC / m[1]), path.read_text(), flags=re.M)


class Policy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.limit = self.work / "limit"
        self.meminfo = self.work / "meminfo"
        self.env = mock.patch.dict(os.environ, {
            "AGENT_BOX_SESSION_LIMIT_FILE": str(self.limit),
            "AGENT_BOX_MEMINFO_FILE": str(self.meminfo),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_automatic_limit_is_one_per_started_gib_of_ram(self):
        self.limit.write_text("auto\n")
        for memory_kib, expected in [
                (1, 1),
                (capacity.CAPACITY_KIB_PER_GIB, 1),
                (4 * capacity.CAPACITY_KIB_PER_GIB - 1, 4),
                (62 * capacity.CAPACITY_KIB_PER_GIB + 1, 63)]:
            self.meminfo.write_text("MemTotal: %d kB\n" % memory_kib)
            self.assertEqual(capacity.capacity_limit(), expected)

    def test_missing_limit_file_uses_automatic_limit(self):
        self.meminfo.write_text("MemTotal: 2097152 kB\n")
        self.assertEqual(capacity.capacity_limit(), 2)

    def test_explicit_limit_overrides_ram(self):
        self.limit.write_text("7\n")
        self.assertEqual(capacity.capacity_limit(), 7)

    def test_malformed_automatic_inputs_fail_closed(self):
        self.limit.write_text("auto\n")
        for value in ["", "MemTotal: nope kB\n", "MemTotal: 4 MB\n"]:
            self.meminfo.write_text(value)
            with self.assertRaises(ValueError):
                capacity.capacity_limit()

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

    def test_died_entry_remains_its_own_revival_candidate(self):
        # A died entry must still admit ITSELF: the supervisor's own spawn
        # decision for the name being revived has to see it in `pending`, or
        # `agent-box-session restart` on a died session can never re-admit it
        # to clear the stale flag -- a real deadlock hit in CI (issue #523):
        # excluding a died name from `pending` too, not just from counting
        # against everyone else, made `agent-box-session restart` on a died
        # session hang forever, because the one name being spawned was never
        # a candidate for its own slot.
        sessions = {"revived": {"died": 99}}
        capacity.capacity_check(sessions, ["revived"], spawning=True,
                                live=set(), limit=1)

    def test_shell_entries_are_unmetered(self):
        sessions = {"terminal": {"agent": "shell"}}
        result = capacity.capacity_check(sessions, ["worker"],
                                         live={"terminal"}, limit=1)
        self.assertEqual(result["used"], 0)

        capacity.capacity_check({"worker": {}, **sessions}, ["terminal"],
                                spawning=True, live={"worker"}, limit=1)

    def test_new_shell_is_exempt_before_its_registry_entry_exists(self):
        capacity.capacity_check({"worker": {}}, ["terminal"],
                                live={"worker"}, limit=1,
                                exempt={"terminal"})

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


class Hibernation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.live = self.home / "live"
        self.tmux = self.home / "tmux"
        self.tmux.write_text(
            "#!" + shutil.which("bash") + "\n"
            "if [ \"$1 $2 $3\" = '-L agent-box list-sessions' ]; then "
            "cat \"$TEST_LIVE_FILE\"; exit 0; fi\n"
            "echo unexpected tmux call >&2; exit 1\n")
        self.tmux.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "USER": "tester",
            "CODEX_HOME": str(self.home / ".codex"),
            "AGENT_BOX_TMUX_BIN": str(self.tmux),
            "TEST_LIVE_FILE": str(self.live),
            "AGENT_BOX_HIBERNATE_IDLE_S": "0",
            "LOCAL_WEBHOOK_STATE_DIR": str(self.home / "webhook"),
            "AGENT_BOX_ENVSTORE_BIN": str(self.home / "missing-envstore"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_json(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, separators=(",", ":")) + "\n")
        return path

    def state(self, name, launch, native=None):
        value = {"launchSessionId": launch}
        if native:
            value["nativeSessionId"] = native
        self.write_json(".local/state/agent-box/session/%s.json" % name, value)

    def claude(self, name="idle", closed=True):
        launch = "11111111-1111-4111-8111-111111111111"
        self.state(name, launch, launch)
        transcript = self.home / ".claude/projects/-work" / (launch + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        record = ({"type": "assistant", "message": {
            "stop_reason": "end_turn", "content": []}}
                  if closed else
                  {"type": "user", "message": {"content": "work"}})
        transcript.write_text(json.dumps(record, separators=(",", ":")) + "\n")
        self.live.write_text(name + "\n")
        return {name: {"agent": "claude"}}, transcript

    def test_closed_claude_turn_is_stable_candidate(self):
        sessions, transcript = self.claude()
        selected = capacity.hibernation_candidate(sessions)
        self.assertEqual(selected["name"], "idle")
        self.assertEqual(selected["nativeSessionId"],
                         "11111111-1111-4111-8111-111111111111")
        verified = capacity.hibernation_candidate(
            sessions, name="idle", token=selected["token"])
        self.assertEqual(verified["token"], selected["token"])
        with transcript.open("a") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": "new"}})
                         + "\n")
        self.assertIsNone(capacity.hibernation_candidate(
            sessions, name="idle", token=selected["token"]))

    def test_open_truncated_and_recent_claude_turns_stay_resident(self):
        sessions, transcript = self.claude(closed=False)
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        transcript.write_text('{"type":"assistant"')
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        transcript.write_text(json.dumps({"type": "assistant", "message": {
            "stop_reason": "end_turn"}}) + "\n")
        os.environ["AGENT_BOX_HIBERNATE_IDLE_S"] = "60"
        self.assertIsNone(capacity.hibernation_candidate(sessions))

    def test_outstanding_routing_state_excludes_claude(self):
        sessions, _ = self.claude()
        self.write_json("webhook/filter.tester-idle.json", {
            "enabled": True, "topics": [{"topic": "github:o/r"}]})
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        self.write_json("webhook/filter.tester-idle.json", {
            "enabled": True, "topics": "malformed"})
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        self.write_json("webhook/filter.tester-idle.json", {
            "enabled": False, "topics": [{"topic": "github:o/r"}]})
        self.write_json(".local/state/agent-box/lease/idle.json", {
            "outcome": None})
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        self.write_json(".local/state/agent-box/lease/idle.json", {
            "outcome": "resolved"})
        self.write_json(".local/state/local-whatsapp/target.json", [])
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        self.write_json(".local/state/local-whatsapp/target.json", {
            "session": "idle"})
        self.write_json(".local/state/local-whatsapp/messages.json", {
            "messages": {"m": {"reply": None, "control": False}}})
        self.assertIsNone(capacity.hibernation_candidate(sessions))

    def test_completed_codex_rollout_is_candidate_but_remote_control_is_not(self):
        launch = "22222222-2222-4222-8222-222222222222"
        native = "33333333-3333-4333-8333-333333333333"
        self.state("codex-idle", launch, native)
        rollout = (self.home / ".codex/sessions/2026/10/16"
                   / ("rollout-2026-10-16T00-00-00-" + native + ".jsonl"))
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text("\n".join(json.dumps(record, separators=(",", ":"))
                                     for record in [
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "event_msg", "payload": {"type": "thread_settings_applied"}},
        ]) + "\n")
        self.live.write_text("codex-idle\n")
        sessions = {"codex-idle": {"agent": "codex", "remoteControl": False}}
        selected = capacity.hibernation_candidate(sessions)
        self.assertEqual(selected["nativeSessionId"], native)
        with rollout.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {
                "type": "future_unknown_state"}}) + "\n")
        self.assertIsNone(capacity.hibernation_candidate(sessions))
        rollout.write_text("\n".join(json.dumps(record, separators=(",", ":"))
                                     for record in [
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ]) + "\n")
        sessions["codex-idle"]["remoteControl"] = True
        self.assertIsNone(capacity.hibernation_candidate(sessions))

    def test_newer_codex_marker_beats_stale_completed_native_pointer(self):
        launch = "55555555-5555-4555-8555-555555555555"
        stale = "66666666-6666-4666-8666-666666666666"
        current = "77777777-7777-4777-8777-777777777777"
        self.state("codex-idle", launch, stale)
        root = self.home / ".codex/sessions/2026/10/16"
        root.mkdir(parents=True, exist_ok=True)
        old = root / ("rollout-2026-10-16T00-00-00-" + stale + ".jsonl")
        old.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete"}}) + "\n")
        new = root / ("rollout-2026-10-16T00-00-01-" + current + ".jsonl")
        new.write_text("\n".join(json.dumps(record) for record in [
            {"message": "[agent-box session %s]" % launch},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ]) + "\n")
        os.utime(new, (time.time() + 1, time.time() + 1))
        self.live.write_text("codex-idle\n")
        sessions = {"codex-idle": {"agent": "codex", "remoteControl": False}}
        self.assertIsNone(capacity.hibernation_candidate(sessions))

    def test_shell_ephemeral_hook_and_unmapped_sessions_are_ineligible(self):
        sessions, _ = self.claude()
        for update in [
                {"agent": "shell"},
                {"ephemeral": True},
                {"origin": "webhook"},
                {"stopped": True},
                {"died": 1}]:
            entry = {"agent": "claude", **update}
            self.assertIsNone(capacity.hibernation_candidate({"idle": entry}))
        self.live.write_text("unknown\n")
        self.assertIsNone(capacity.hibernation_candidate(
            {"unknown": {"agent": "claude"}}))


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
                          (SRC / "lib/session-hibernation.py").read_text() + "\n" +
                          (SRC / "session-capacity-cli.py").read_text())
        helper.chmod(0o755)
        tmux = self.work / "tmux"
        tmux.write_text("#!" + shutil.which("bash") + "\n"
                        "echo 'no server running' >&2\nexit 1\n")
        tmux.chmod(0o755)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.work),
                    "USER": "capacity-test", "AGENT_BOX_AGENTS": "claude shell",
                    "AGENT_BOX_DEFAULT_AGENT": "claude",
                    "AGENT_BOX_SESSIONS_FILE": str(self.registry),
                    "AGENT_BOX_SESSION_LIMIT_FILE": str(self.limit),
                    "AGENT_BOX_CAPACITY_BIN": str(helper),
                    "AGENT_BOX_TMUX_BIN": str(tmux),
                    "AGENT_BOX_HIBERNATE_IDLE_S": "0",
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

    def test_shell_add_does_not_consume_or_need_capacity(self):
        for name in ["a", "b"]:
            self.assertEqual(self.run_cli("add", name).returncode, 0)
        result = self.run_cli("add", "terminal", "--harness", "shell")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_cli("add", "worker").returncode, 75)
        self.assertEqual(json.loads(self.registry.read_text())["sessions"]["terminal"]["agent"],
                         "shell")

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
        for value in ["0", "-1", "abc", "AUTO"]:
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

    def make_live_claude(self, name="idle"):
        launch = "44444444-4444-4444-8444-444444444444"
        state = self.work / ".local/state/agent-box/session" / (name + ".json")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"launchSessionId": launch,
                                     "nativeSessionId": launch}))
        transcript = self.work / ".claude/projects/-work" / (launch + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "assistant", "message": {
            "stop_reason": "end_turn"}}) + "\n")
        live = self.work / "live"
        live.write_text(name + "\n")
        tmux = self.work / "live-tmux"
        tmux.write_text(
            "#!" + shutil.which("bash") + "\n"
            "case \"$3\" in\n"
            "  list-sessions) cat \"$TEST_LIVE_FILE\" ;;\n"
            "  kill-session)\n"
            "    if [ -n \"${TEST_ACTIVATE_REGISTRY:-}\" ]; then\n"
            "      jq 'del(.sessions.idle.stopped) | "
            "del(.sessions.idle.hibernatedAt) | "
            "del(.sessions.idle.hibernationGeneration)' "
            "\"$TEST_ACTIVATE_REGISTRY\" > \"$TEST_ACTIVATE_REGISTRY.next\"\n"
            "      mv \"$TEST_ACTIVATE_REGISTRY.next\" \"$TEST_ACTIVATE_REGISTRY\"\n"
            "    fi\n"
            "    : > \"$TEST_LIVE_FILE\" ;;\n"
            "  has-session) grep -Fx \"${5#=}\" \"$TEST_LIVE_FILE\" >/dev/null ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n")
        tmux.chmod(0o755)
        self.env["AGENT_BOX_TMUX_BIN"] = str(tmux)
        self.env["TEST_LIVE_FILE"] = str(live)
        self.registry.write_text(json.dumps({"version": 1, "sessions": {
            name: {"agent": "claude", "hasRun": True,
                   "boxSessionId": launch}}}))
        return live

    def test_hibernate_parks_only_under_pressure_and_restart_clears_marker(self):
        live = self.make_live_claude()
        self.limit.write_text("2\n")
        before = self.registry.read_bytes()
        result = self.run_cli("hibernate")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(live.read_text(), "idle\n")

        self.limit.write_text("1\n")
        result = self.run_cli("hibernate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["name"], "idle")
        entry = json.loads(self.registry.read_text())["sessions"]["idle"]
        self.assertTrue(entry["stopped"])
        self.assertRegex(entry["hibernatedAt"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(live.read_text(), "")
        self.assertRegex(self.run_cli("ls").stdout,
                         r"(?m)^idle\s+claude\s+hibernated$")

        result = self.run_cli("restart", "idle")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = json.loads(self.registry.read_text())["sessions"]["idle"]
        self.assertNotIn("stopped", entry)
        self.assertNotIn("hibernatedAt", entry)
        self.assertNotIn("hibernationGeneration", entry)

    def test_hibernate_respects_exclusion(self):
        self.make_live_claude()
        self.limit.write_text("1\n")
        result = self.run_cli("hibernate", "--exclude", "idle")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(self.registry.read_text())
                         ["sessions"]["idle"].get("stopped", False))

    def test_activation_racing_the_kill_wins_without_a_stale_stop(self):
        self.make_live_claude()
        self.limit.write_text("1\n")
        self.env["TEST_ACTIVATE_REGISTRY"] = str(self.registry)
        result = self.run_cli("hibernate")
        self.assertEqual(result.returncode, 1, result.stderr)
        entry = json.loads(self.registry.read_text())["sessions"]["idle"]
        self.assertNotIn("stopped", entry)
        self.assertNotIn("hibernatedAt", entry)
        self.assertNotIn("hibernationGeneration", entry)


if __name__ == "__main__":
    unittest.main()
