#!/usr/bin/env python3
"""Partial page renders and the multi-topic live feed.

Two costs this pins down, both seen on a live box while rotating a token:

  * Every form post and every live refresh fetched the WHOLE settings page
    and kept one or two regions of it. Rendering that page forks the webhook
    CLI once per session, the email CLI and the WhatsApp bridge, so saving a
    single secret took one to several seconds, with nothing on screen to say
    it was under way. A request may now name the regions it keeps in
    X-Agent-Box-Sections; the daemon renders only those.

  * The page polled {BASE}/resources every 5s and, while a sign-in card was
    busy, {BASE}/status every 2.5s. Both now travel on the session feed's
    existing SSE stream as the "resources" and "connect" topics.

The subject is the golden payload, as in test-settings-json.py: the daemon
ships with the env-store library prepended, so the source file alone does not
import.
"""
import http.client
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")


def load_daemon(env_file):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = env_file
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ["AGENT_BOX_SETTINGS_BASE"] = "/agent/settings"
    os.environ["AGENT_BOX_SETTINGS_PORT"] = "0"
    os.environ["AGENT_BOX_TMUX_TMPDIR"] = tempfile.mkdtemp(prefix="abx-tmux.")
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test_sections", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


def region(page, region_id):
    """The opening tag of one region and what follows it, or None."""
    match = re.search(r'<[a-z]+ [^>]*id="%s"[^>]*>' % re.escape(region_id), page)
    return page[match.start():] if match else None


class Expensive(Exception):
    """Raised by a probe that a partial render must not reach."""


class SectionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handle, cls.env_file = tempfile.mkstemp(prefix="abx-env.")
        os.close(handle)
        cls.daemon = load_daemon(cls.env_file)
        cls.daemon.set_key("GH_TOKEN", "ghp_old_value")
        cls.server = cls.daemon.make_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d/agent/settings" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.unlink(cls.env_file)

    def request(self, method, path, sections=None, body=None):
        headers = {}
        if sections is not None:
            headers["X-Agent-Box-Sections"] = sections
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, method=method,
                                     headers=headers,
                                     data=body.encode() if body else None)
        with urllib.request.urlopen(req, timeout=10) as answer:
            return answer.status, answer.read().decode("utf-8")

    def forbid_expensive(self):
        """Patch every slow probe of the page to raise."""
        stack = []
        for name in ("webhook_view", "webhook_unavailable", "render_webhooks",
                     "render_email_addresses", "connect_flows",
                     "profile_usage", "render_model_hints",
                     "render_update_line", "render_reboot_line"):
            patcher = mock.patch.object(self.daemon, name,
                                        side_effect=Expensive(name))
            patcher.start()
            stack.append(patcher)
        self.addCleanup(lambda: [p.stop() for p in stack])

    def test_parse_sections_keeps_known_ids_only(self):
        parse = self.daemon.parse_sections
        self.assertIsNone(parse(None))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("tab-bar nonsense"))
        self.assertEqual(parse("msg-slot, secrets-list tab-bar"),
                         frozenset({"msg-slot", "secrets-list"}))

    def test_no_header_is_the_full_page(self):
        _, page = self.request("GET", "/")
        for region_id in ("msg-slot", "secrets-list", "sessions-list",
                          "profiles-list", "profile-picker"):
            self.assertIsNotNone(region(page, region_id), region_id)
        self.assertIn("GH_TOKEN", region(page, "secrets-list"))

    def test_secrets_region_renders_without_the_slow_probes(self):
        self.forbid_expensive()
        status, page = self.request("GET", "/", "msg-slot secrets-list")
        self.assertEqual(status, 200)
        self.assertIn("GH_TOKEN", region(page, "secrets-list"))
        # The regions nobody asked for are not rendered at all.
        self.assertIsNone(region(page, "sessions-list"))
        self.assertIsNone(region(page, "profiles-list"))

    def test_unknown_names_fall_back_to_the_full_page(self):
        # The workspace's tab-bar is not a settings region: a request naming
        # only that must not get an empty settings page.
        _, page = self.request("GET", "/", "tab-bar")
        self.assertIsNotNone(region(page, "sessions-list"))

    def test_saving_a_secret_answers_with_the_requested_regions(self):
        # The case from the field: rotate GH_TOKEN. The POST is followed to
        # the page by urllib (as by fetch()), carrying the header along.
        with mock.patch.object(self.daemon, "webhook_view",
                               side_effect=Expensive("webhook_view")), \
                mock.patch.object(self.daemon, "render_email_addresses",
                                  side_effect=Expensive("email")):
            status, page = self.request(
                "POST", "/set", "msg-slot secrets-list connect-list",
                "key=GH_TOKEN&value=ghp_new_value")
        self.assertEqual(status, 200)
        self.assertIn('data-kind="ok"', region(page, "msg-slot"))
        self.assertIn("GH_TOKEN", region(page, "secrets-list"))
        self.assertNotIn("ghp_new_value", page)
        with open(self.env_file) as handle:
            self.assertIn("ghp_new_value", handle.read())

    def test_a_rejected_post_also_answers_partially(self):
        self.forbid_expensive()
        req = urllib.request.Request(
            self.base + "/set", method="POST",
            headers={"X-Agent-Box-Sections": "msg-slot secrets-list",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=b"key=1BAD&value=x")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        page = caught.exception.read().decode("utf-8")
        caught.exception.close()
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("Invalid key name", region(page, "msg-slot"))

    def test_the_header_does_not_leak_into_the_next_request(self):
        # Keep-alive reuses one handler thread; each request sets its own.
        self.request("GET", "/", "secrets-list")
        _, page = self.request("GET", "/")
        self.assertIsNotNone(region(page, "sessions-list"))


class WatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handle, cls.env_file = tempfile.mkstemp(prefix="abx-env.")
        os.close(handle)
        cls.daemon = load_daemon(cls.env_file)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.env_file)

    def setUp(self):
        d = self.daemon
        self.connect_calls = 0
        self.resource_calls = 0
        self.busy = False

        def connect_sample():
            self.connect_calls += 1
            return "fp%d" % self.connect_calls, self.busy

        def resources_payload():
            self.resource_calls += 1
            return {"ok": True, "n": self.resource_calls}

        for name, value in (("session_fingerprint", lambda: "sess"),
                            ("connect_sample", connect_sample),
                            ("resources_payload", resources_payload),
                            ("EVENTS_TICK", 0.01),
                            ("EVENTS_RESOURCES_TICK", 0.05),
                            ("EVENTS_CONNECT_TICK", 0.02),
                            ("EVENTS_CONNECT_ACTIVE", 0.2)):
            patcher = mock.patch.object(d, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_for(self, watcher, topics, seconds):
        self.assertIsNotNone(watcher.subscribe(topics))
        try:
            time.sleep(seconds)
            return dict(watcher.values)
        finally:
            watcher.release(topics)
            time.sleep(0.05)   # let the sampler notice and exit

    def test_parse_topics(self):
        parse = self.daemon.parse_topics
        self.assertEqual(parse(None), ("sessions",))
        self.assertEqual(parse("bogus"), ("sessions",))
        self.assertEqual(parse("connect,resources,sessions"),
                         ("sessions", "resources", "connect"))

    def test_sessions_only_stream_samples_nothing_else(self):
        watcher = self.daemon.SessionWatcher()
        watcher.connect_active()
        values = self.run_for(watcher, ("sessions",), 0.2)
        self.assertEqual(values["sessions"], "sess")
        self.assertEqual(self.resource_calls, 0)
        self.assertEqual(self.connect_calls, 0)

    def test_resources_follow_their_own_clock(self):
        watcher = self.daemon.SessionWatcher()
        values = self.run_for(watcher, ("sessions", "resources"), 0.3)
        self.assertEqual(json.loads(values["resources"])["ok"], True)
        # ~0.3s at a 0.05s clock: several samples, far fewer than ticks.
        self.assertGreaterEqual(self.resource_calls, 3)
        self.assertLessEqual(self.resource_calls, 10)

    def test_connect_is_not_sampled_until_a_sign_in_is_active(self):
        # connect_state() answers prompts and reaps panes: an idle page must
        # not drive it just by being open.
        watcher = self.daemon.SessionWatcher()
        self.run_for(watcher, ("sessions", "connect"), 0.15)
        self.assertEqual(self.connect_calls, 0)

    def test_connect_window_closes_once_the_cards_settle(self):
        watcher = self.daemon.SessionWatcher()
        watcher.connect_active()
        self.run_for(watcher, ("sessions", "connect"), 0.6)
        # Sampled during the 0.2s window, then stopped.
        self.assertGreaterEqual(self.connect_calls, 2)
        self.assertLessEqual(self.connect_calls, 15)

    def test_a_busy_card_keeps_the_window_open(self):
        self.busy = True
        watcher = self.daemon.SessionWatcher()
        watcher.connect_active()
        values = self.run_for(watcher, ("sessions", "connect"), 0.6)
        # Well past the 0.2s window, still sampling at the 0.02s clock.
        self.assertGreater(self.connect_calls, 15)
        self.assertEqual(json.loads(values["connect"]),
                         {"fp": "fp%d" % self.connect_calls})

    def test_a_failing_topic_does_not_stop_the_session_feed(self):
        watcher = self.daemon.SessionWatcher()
        fingerprints = iter(["a", "b", "c", "d"] + ["d"] * 1000)
        with mock.patch.object(self.daemon, "resources_payload",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(self.daemon, "session_fingerprint",
                                  side_effect=lambda: next(fingerprints)):
            values = self.run_for(watcher, ("sessions", "resources"), 0.2)
        self.assertEqual(values["sessions"], "d")
        self.assertEqual(values["resources"], "")


class StreamTest(unittest.TestCase):
    """The frames on the wire, through the real handler."""

    @classmethod
    def setUpClass(cls):
        handle, cls.env_file = tempfile.mkstemp(prefix="abx-env.")
        os.close(handle)
        cls.daemon = load_daemon(cls.env_file)
        cls.server = cls.daemon.make_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.unlink(cls.env_file)

    def frames(self, query, topics):
        """The latest frame per topic, read until every topic in `topics`
        has sent one. The watcher is module-wide, so a stream can also see
        a fingerprint left over from the previous test first."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/agent/settings/sessions/events" + query)
        answer = conn.getresponse()
        self.assertEqual(answer.status, 200)
        found = {}
        event = None
        deadline = time.monotonic() + 8
        while not set(topics) <= set(found):
            self.assertLess(time.monotonic(), deadline, found)
            line = answer.fp.readline().decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: ") and event:
                found[event] = json.loads(line[len("data: "):])
                event = None
        conn.close()
        return found

    def test_default_stream_is_sessions_only(self):
        with mock.patch.object(self.daemon, "session_fingerprint",
                               return_value="s1"), \
                mock.patch.object(self.daemon, "resources_payload",
                                  side_effect=AssertionError("sampled")):
            got = self.frames("", ["sessions"])
            # Give a stray resources frame time to (wrongly) arrive.
            time.sleep(0.2)
        self.assertEqual(set(got), {"sessions"})

    def test_resources_frames_carry_the_reading(self):
        with mock.patch.object(self.daemon, "session_fingerprint",
                               return_value="s2"), \
                mock.patch.object(self.daemon, "resources_payload",
                                  return_value={"ok": True, "cpu": None}):
            got = self.frames("?topics=sessions,resources",
                              ["sessions", "resources"])
        self.assertEqual(got["resources"], {"ok": True, "cpu": None})


if __name__ == "__main__":
    unittest.main(verbosity=2)
