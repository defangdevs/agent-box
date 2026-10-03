#!/usr/bin/env python3
"""The settings daemon's session API for portals (issue #787).

Defang Station lists a box's sessions and frees slots from its own page, so
the registry has to reach it as JSON. What these tests pin:

* the list is an ALLOW-LIST of metadata: no argv, env, prompts or transcript
  ids, however much sessions.json holds (the key set is asserted);
* `state` and `capacity` come from the box's own capacity arithmetic;
* stop parks (flag, no deletion), 404s an unknown name without creating a
  stub entry, and frees a slot the next list shows;
* restart / delete answer 404 / 503 with a JSON reason for a caller that
  asks for JSON, and keep their old answers for a browser.

The subject is the GOLDEN PAYLOAD, for the reason test-profile-panel.py
gives.
"""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")

ROW_KEYS = {"name", "agent", "profile", "state", "exit_status", "origin",
            "remote_control", "remote_control_name", "working_directory",
            "ephemeral", "created_at", "hook"}
SECRET = "TASK-TEXT-NEVER-EXPOSED"


def daemon_with(**env):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ.update(env)
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_sessions_api_test", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


def entry(**fields):
    base = {"agent": "claude", "skipPermissions": True, "remoteControl": True,
            "remoteControlName": None, "workingDirectory": None,
            "extraArgs": ["--model", SECRET], "profile": None,
            "initialPrompt": SECRET, "resumePrompt": SECRET,
            "boxSessionId": "00000000-0000-0000-0000-000000000000",
            "hasRun": True}
    base.update(fields)
    return base


class SessionsApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        conf = os.path.join(root, "agent-box")
        os.makedirs(conf)
        env_file = os.path.join(conf, "env")
        open(env_file, "w").close()
        self.sessions_file = os.path.join(conf, "sessions.json")
        self.module = daemon_with(
            AGENT_BOX_SETTINGS_ENV_FILE=env_file,
            AGENT_BOX_SESSIONS_FILE=self.sessions_file,
            AGENT_BOX_AGENTS="claude,codex,shell",
            AGENT_BOX_DEFAULT_AGENT="claude",
            HOME=root)
        m = self.module
        self.live = set()
        self.limit = 4
        m.capacity_live = lambda: set(self.live)
        m.capacity_limit = lambda: self.limit
        m.kill_session = self.kill
        self.killed = []
        self.kill_ok = True
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/settings" % server.server_address[1]
        self.root = self.base.rsplit("/settings", 1)[0]

    def kill(self, name):
        self.killed.append(name)
        self.live.discard(name)
        return self.kill_ok

    def write(self, sessions):
        with open(self.sessions_file, "w") as handle:
            json.dump({"sessions": sessions}, handle)

    def read(self):
        with open(self.sessions_file) as handle:
            return json.load(handle)["sessions"]

    def url(self, path):
        return self.root + self.module.SESS_BASE + path

    def request(self, path, method="GET", json_accept=False, **fields):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        data = (urllib.parse.urlencode(fields).encode()
                if method == "POST" else None)
        req = urllib.request.Request(self.url(path), data=data, method=method)
        if json_accept:
            req.add_header("Accept", "application/json")
        try:
            response = urllib.request.build_opener(NoRedirect).open(req)
            return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.headers

    def listing(self):
        status, body, headers = self.request("/sessions/list")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"].split(";")[0], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(body), body

    def by_name(self):
        return {s["name"]: s for s in self.listing()[0]["sessions"]}

    # --- GET /sessions/list ---------------------------------------------

    def test_list_carries_only_the_allowed_keys_and_never_task_text(self):
        self.write({"claude": entry(origin="user", createdAt=1790871234,
                                    profile="review", ephemeral=True,
                                    hook={"source": "github",
                                          "repository": "o/r",
                                          "token": SECRET})})
        self.live = {"claude"}
        data, body = self.listing()
        self.assertNotIn(SECRET, body)
        self.assertNotIn("00000000-0000", body)
        (row,) = data["sessions"]
        self.assertEqual(set(row), ROW_KEYS)
        self.assertEqual(row["hook"], {"source": "github", "repository": "o/r"})
        self.assertEqual(row["origin"], "user")
        self.assertEqual(row["created_at"], 1790871234)
        self.assertEqual(row["profile"], "review")
        self.assertTrue(row["ephemeral"])
        self.assertEqual(row["working_directory"], os.path.expanduser("~"))

    def test_states_and_capacity(self):
        self.limit = 2
        self.write({
            "a-run": entry(),
            "b-died": entry(died=7),
            "c-stop": entry(stopped=True),
            "d-wait": entry(),
            "e-over": entry(),
        })
        self.live = {"a-run", "b-died"}
        data, _ = self.listing()
        states = {s["name"]: s["state"] for s in data["sessions"]}
        self.assertEqual(states, {"a-run": "running", "b-died": "died",
                                  "c-stop": "stopped", "d-wait": "starting",
                                  "e-over": "queued"})
        self.assertEqual(self.by_name()["b-died"]["exit_status"], 7)
        self.assertIsNone(self.by_name()["a-run"]["exit_status"])
        # Stopped and died hold no slot; the live one and the two pending do,
        # so an overfull registry is a queue and only one of them is admitted.
        self.assertEqual(data["capacity"], {"used": 3, "limit": 2})

    def test_an_entry_from_before_origin_reads_null(self):
        self.write({"old": entry()})
        row = self.by_name()["old"]
        self.assertIsNone(row["origin"])
        self.assertIsNone(row["created_at"])
        self.write({"odd": entry(origin="somewhere-else")})
        self.assertIsNone(self.by_name()["odd"]["origin"])

    def test_list_answers_503_when_capacity_cannot_be_read(self):
        def boom():
            raise OSError("no tmux")
        self.module.capacity_live = boom
        status, body, _ = self.request("/sessions/list")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ok"])

    def test_list_answers_503_when_the_registry_is_unreadable(self):
        with open(self.sessions_file, "w") as handle:
            handle.write("{not json")
        status, body, _ = self.request("/sessions/list")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ok"])

    def test_died_without_a_pane_is_starting_not_died(self):
        self.write({"gone": entry(died=1)})
        self.live = set()
        self.assertEqual(self.by_name()["gone"]["state"], "starting")

    def test_shell_pane_is_unmetered_and_not_queued(self):
        self.limit = 1
        self.write({"worker": entry(), "terminal": entry(agent="shell")})
        self.live = {"worker"}
        data, _ = self.listing()
        states = {s["name"]: s["state"] for s in data["sessions"]}
        self.assertEqual(states, {"worker": "running", "terminal": "starting"})
        self.assertEqual(data["capacity"], {"used": 1, "limit": 1})

    # --- POST /sessions/stop --------------------------------------------

    def test_stop_parks_frees_the_slot_and_keeps_the_entry(self):
        self.limit = 1
        self.write({"claude": entry()})
        self.live = {"claude"}
        self.assertEqual(self.listing()[0]["capacity"]["used"], 1)
        status, _, _ = self.request("/sessions/stop", "POST", name="claude")
        self.assertEqual(status, 303)
        self.assertEqual(self.killed, ["claude"])
        self.assertTrue(self.read()["claude"]["stopped"])
        data, _ = self.listing()
        self.assertEqual(data["sessions"][0]["state"], "stopped")
        self.assertEqual(data["capacity"]["used"], 0)

    def test_stop_is_500_when_tmux_kill_fails(self):
        self.write({"claude": entry()})
        self.kill_ok = False
        status, body, _ = self.request("/sessions/stop", "POST",
                                       json_accept=True, name="claude")
        self.assertEqual(status, 500)
        self.assertFalse(json.loads(body)["ok"])

    def test_stop_unknown_name_is_404_and_creates_no_stub(self):
        self.write({"claude": entry()})
        for name in ("ghost", "bad name!", ""):
            status, body, _ = self.request("/sessions/stop", "POST",
                                           json_accept=True, name=name)
            self.assertEqual(status, 404)
            self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(list(self.read()), ["claude"])
        self.assertEqual(self.killed, [])

    def test_stop_busy_registry_is_503_with_a_reason(self):
        self.write({"claude": entry()})

        def busy():
            raise self.module.RegistryBusy("held")
        self.module.sessions_lock = busy
        status, body, _ = self.request("/sessions/stop", "POST",
                                       json_accept=True, name="claude")
        self.assertEqual(status, 503)
        self.assertIn("reason", json.loads(body))
        self.assertNotIn("stopped", self.read()["claude"])

    # --- restart / delete ------------------------------------------------

    def test_restart_revives_or_503s_when_full(self):
        self.limit = 1
        self.write({"a": entry(), "b": entry(stopped=True)})
        self.live = {"a"}
        status, body, _ = self.request("/sessions/restart", "POST",
                                       json_accept=True, name="b")
        self.assertEqual(status, 503)
        self.assertIn("Session limit reached", json.loads(body)["reason"])
        self.assertTrue(self.read()["b"]["stopped"])
        self.request("/sessions/stop", "POST", name="a")
        status, _, _ = self.request("/sessions/restart", "POST",
                                    json_accept=True, name="b")
        self.assertEqual(status, 303)
        self.assertNotIn("stopped", self.read()["b"])

    def test_restart_and_delete_unknown_are_404_for_json_only(self):
        self.write({"a": entry()})
        for verb in ("restart", "delete"):
            status, body, _ = self.request("/sessions/" + verb, "POST",
                                           json_accept=True, name="ghost")
            self.assertEqual(status, 404, verb)
            self.assertFalse(json.loads(body)["ok"])
            # A browser's stale tab keeps the banner it always got.
            status, _, _ = self.request("/sessions/" + verb, "POST", name="ghost")
            self.assertEqual(status, 303, verb)
        self.assertEqual(list(self.read()), ["a"])

    def test_json_delete_of_an_unknown_name_kills_nothing(self):
        self.write({"a": entry()})
        self.live = {"ghost"}
        status, _, _ = self.request("/sessions/delete", "POST",
                                    json_accept=True, name="ghost")
        self.assertEqual(status, 404)
        self.assertEqual(self.killed, [])

    def test_accept_header_is_case_insensitive(self):
        self.write({"a": entry()})
        req = urllib.request.Request(self.url("/sessions/delete"), method="POST",
                                     data=b"name=ghost")
        req.add_header("Accept", "Application/JSON")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)

    def test_delete_removes_the_entry(self):
        self.write({"a": entry(), "b": entry()})
        status, _, _ = self.request("/sessions/delete", "POST",
                                    json_accept=True, name="a")
        self.assertEqual(status, 303)
        self.assertEqual(list(self.read()), ["b"])


class Creation(unittest.TestCase):
    def test_add_records_origin_and_creation_time(self):
        src = DAEMON.read_text()
        self.assertIn('"origin": "user"', src)
        self.assertIn('"origin": "pairing" if only_rc else "sign_in"', src)


if __name__ == "__main__":
    unittest.main()
