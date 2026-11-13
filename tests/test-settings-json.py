#!/usr/bin/env python3
"""The settings daemon's machine-readable reads (issue #642).

A portal that has handed a user into their box (issue #541,
docs/portal-handoff.md) also DRIVES it: Defang Station renders the connect
cards and the Environment panel in its own UI and calls this daemon for the
state behind them. That makes two reads part of a wire contract rather than
page plumbing, and pins them here:

  GET {BASE}/env      -> {"ok": true, "keys": [...]}   NAMES ONLY, never a value
  GET {BASE}/connect  -> {"ok": true, "flows": [...]}  every card in one answer

The value rule is the load-bearing one. This daemon has never had a route
that returns a stored secret -- the page lists names and nothing else -- and
`{BASE}/env` must not become the first. The test therefore writes a real
value into a real env store and asserts the answer does not contain it.

Driven over HTTP against the daemon's own request handler, not by calling a
render function: what is under test is the route table, including that
`?flow=<unknown>` still 404s and that an unknown path under the base still
falls through to the page (which is what tells a client "this box predates
that route" -- 200 with HTML, not 404).

The subject is tests/golden/web/payloads/.../agent-box-settings, not
modules/src/settings-daemon.py, for the reason test-connect-card.py gives:
the daemon ships with the env-store library prepended, so the source file
alone does not import.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")

SECRET = "sk-ant-this-exact-string-must-never-be-served"


def load_daemon(env_file):
    """Import the shipped daemon with a clean environment and port 0.

    Port 0 rather than a fixed one: make_server() falls back to
    127.0.0.1:PORT without LISTEN_FDS, and an ephemeral port lets this run
    beside anything else on the machine.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = env_file
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ["AGENT_BOX_SETTINGS_BASE"] = "/agent/settings"
    os.environ["AGENT_BOX_SETTINGS_PORT"] = "0"
    # No tmux server and no CLIs: every card reads "blocked", which is fine.
    # This is about the route table, not about a sign-in.
    os.environ["AGENT_BOX_TMUX_TMPDIR"] = tempfile.mkdtemp(prefix="abx-tmux.")
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test_json", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class SettingsJsonRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handle, cls.env_file = tempfile.mkstemp(prefix="abx-env.")
        os.close(handle)
        cls.daemon = load_daemon(cls.env_file)
        # A real write through the daemon's own env store, so the read below
        # is answering about a value that genuinely exists.
        cls.daemon.set_key("ANTHROPIC_API_KEY", SECRET)
        cls.daemon.set_key("GH_TOKEN", "ghp_also_never_served")
        cls.server = cls.daemon.make_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/agent/settings" % (
            cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.unlink(cls.env_file)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as answer:
            return answer.status, answer.headers, answer.read().decode("utf-8")

    def test_resources_are_bounded_readings_without_secrets(self):
        reading = {"available_bytes": 4096, "total_bytes": 8192}
        with mock.patch.object(self.daemon, "resource_memory", return_value=reading), \
                mock.patch.object(self.daemon, "resource_disk", return_value=reading):
            status, headers, body = self.get("/resources")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(set(data), {"ok", "sampled_at", "cpu", "memory", "disk", "sessions"})
        self.assertTrue(data["ok"])
        for name in ("memory", "disk"):
            self.assertGreater(data[name]["total_bytes"], 0)
            self.assertGreaterEqual(data[name]["available_bytes"], 0)
            self.assertLessEqual(data[name]["available_bytes"], data[name]["total_bytes"])
        self.assertNotIn(SECRET, body)
        self.assertNotIn(self.env_file, body)

    def test_a_failed_source_keeps_other_readings(self):
        with mock.patch.object(self.daemon, "resource_memory", side_effect=OSError(SECRET)), \
                mock.patch.object(self.daemon, "resource_disk", return_value={"available_bytes": 1, "total_bytes": 2}):
            data = json.loads(self.get("/resources")[2])
        self.assertIsNone(data["memory"])
        self.assertIsNotNone(data["disk"])
        self.assertNotIn(SECRET, json.dumps(data))

    def test_resources_use_the_admission_policy_including_queued_workers(self):
        entries = {"running": {"agent": "claude", "origin": "webhook"},
                   "queued": {"agent": "codex", "origin": "webhook"},
                   "manual": {"agent": "claude", "origin": "user"},
                   "stopped": {"agent": "claude", "stopped": True},
                   "died": {"agent": "codex", "died": 1},
                   "shell": {"agent": "shell"}}
        with mock.patch.object(self.daemon, "load_sessions", return_value=(entries, None)), \
                mock.patch.object(self.daemon, "capacity_live_checked", return_value={"running", "died", "shell"}), \
                mock.patch.object(self.daemon, "capacity_limit", return_value=2):
            data = json.loads(self.get("/resources")[2])
        self.assertEqual(data["sessions"], {"used": 2, "limit": 2})

    def test_cpu_samples_deltas_and_ignores_double_counted_guest_time(self):
        sampler = self.daemon.ResourceSampler()
        real_monotonic = self.daemon.time.monotonic
        test_thread = threading.get_ident()
        scripted = iter([1, 1.1, 6, 11, 50])

        def clock():
            # Other threads, such as the running server, get the real clock.
            if threading.get_ident() != test_thread:
                return real_monotonic()
            return next(scripted)

        with mock.patch.object(self.daemon.time, "monotonic", side_effect=clock), \
                mock.patch.object(self.daemon, "open", create=True, side_effect=[
                    mock.mock_open(read_data="cpu  100 0 0 100 0 0 0 0 90 0\n")(),
                    mock.mock_open(read_data="cpu  125 0 0 175 0 0 0 0 115 0\n")(),
                    mock.mock_open(read_data="cpu  1 0 0 1 0 0 0 0 0 0\n")(),
                    mock.mock_open(read_data="cpu  25 0 0 75 0 0 0 0 0 0\n")(),
                ]):
            self.assertIsNone(sampler.cpu())
            self.assertIsNone(sampler.cpu())  # Concurrent callers reuse the baseline.
            self.assertEqual(sampler.cpu(), 25)
            self.assertIsNone(sampler.cpu())  # Reset counters are not negative load.
            self.assertIsNone(sampler.cpu())  # A stale baseline is not current load.

    def test_memory_uses_available_instead_of_free(self):
        with mock.patch.object(self.daemon, "open", create=True,
                               new=mock.mock_open(read_data="MemTotal: 8192 kB\nMemFree: 100 kB\nMemAvailable: 4096 kB\n")):
            self.assertEqual(self.daemon.resource_memory(),
                             {"available_bytes": 4096 * 1024, "total_bytes": 8192 * 1024})

    def test_disk_uses_space_available_to_the_user_on_the_home_filesystem(self):
        usage = mock.Mock(f_bavail=10, f_bfree=20, f_frsize=4096, f_blocks=100)
        with mock.patch.object(self.daemon.os, "statvfs", return_value=usage) as statvfs:
            self.assertEqual(self.daemon.resource_disk(),
                             {"available_bytes": 40960, "total_bytes": 409600})
        statvfs.assert_called_once_with(os.path.expanduser("~"))

    def test_env_lists_key_names(self):
        status, headers, body = self.get("/env")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(json.loads(body),
                         {"ok": True, "keys": ["ANTHROPIC_API_KEY", "GH_TOKEN"]})

    def test_env_never_serves_a_value(self):
        # The whole reason this route can exist. Checked against the raw
        # body rather than the parsed shape, so a value smuggled into any
        # field at all would fail this.
        _, _, body = self.get("/env")
        self.assertNotIn(SECRET, body)
        self.assertNotIn("ghp_also_never_served", body)

    def test_env_is_not_cached(self):
        _, headers, _ = self.get("/env")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_connect_lists_every_card_in_one_answer(self):
        status, _, body = self.get("/connect")
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertTrue(answer["ok"])
        listed = [flow["id"] for flow in answer["flows"]]
        # Whatever this box offers, and in connect_flows()' own order.
        self.assertEqual(
            listed, [flow["id"] for flow in self.daemon.connect_flows()])
        for flow in answer["flows"]:
            # The same shape a single-card read answers with.
            self.assertIn("state", flow)
            self.assertIn("installed", flow)

    def test_one_card_is_unchanged(self):
        listed = json.loads(self.get("/connect")[2])["flows"]
        if not listed:
            self.skipTest("this build offers no connect cards")
        first = listed[0]["id"]
        answer = json.loads(self.get("/connect?flow=%s" % first)[2])
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["flow"]["id"], first)
        self.assertNotIn("flows", answer)

    def test_an_unknown_flow_still_answers_404(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.get("/connect?flow=no-such-provider")
        self.assertEqual(raised.exception.code, 404)

    def test_an_unknown_route_still_falls_through_to_the_page(self):
        # How a client tells "this box predates that route" from "this box is
        # broken": the page, with 200, and NOT application/json. Pinned
        # because a portal's capability check reads exactly this.
        status, headers, _ = self.get("/no-such-route")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
