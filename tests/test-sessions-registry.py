#!/usr/bin/env python3
"""The settings daemon's session routes against a registry that does not
parse (issue #279).

Why this exists
---------------
sessions.json is the only record of what this box runs, and the web UI is
the only way most operators here can touch it -- they have no shell. Both
halves of the box used to read "cannot parse" as "there are no sessions",
and the web half acted on it: every mutation route wrote that empty answer
back plus its own edit, so ONE click on Add session republished the registry
with a single entry. Every other session was delisted for good, with its
hasRun, boxSessionId and stopped gone; the panes kept running as unmanaged
tmux sessions that nothing respawns, and re-adding a name started a fresh
conversation.

So the routes are held to three things:

  * a registry they could not READ is never republished -- the file comes
    through a refused add, delete or restart byte for byte;
  * the refusal is VISIBLE. The banner is the whole feedback channel on
    these pages, and the failure it replaces was silent: "Session added",
    over a registry that no longer mentioned anything else;
  * a version the daemon did not write is preserved, not stamped back down
    to 1.

The subject is tests/golden/web/payloads/.../agent-box-settings, not
modules/src/settings-daemon.py, for the reason test-profile-panel.py gives:
the daemon ships with the env-store library prepended, so the source file
alone does not import. The golden payload is that assembled article, and the
golden-snapshot check fails if it stops matching.

The other half of #279 -- the supervisor moving the bad file aside so the
box self-heals instead of idling -- is pinned in tests/test-registry.py,
next to the rest of the registry's write protocol.
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

CORRUPT = [
    # A truncating crash, or a hand edit that lost a brace.
    ("truncated", '{"version": 1, "sessions": {"live": {"agent": "clau'),
    # Nothing JSON about it at all.
    ("not json", "not json\n"),
    # The shape that hid this for so long: jq reads a STREAM of values, so
    # the supervisor's filter yielded the first document's session names and
    # only then failed, while json.load refuses the file outright.
    ("trailing garbage",
     '{"version": 1, "sessions": {"live": {"agent": "claude"}}}\nnot json\n'),
    # Top level is not an object.
    ("a list", '[{"name": "live"}]'),
    # .sessions is not an object -- the shape the native backend's seed got
    # wrong (issue #356).
    ("sessions as a list", '{"version": 1, "sessions": [{"name": "live"}]}'),
]


def daemon_with(**env):
    """Import the shipped daemon under a throwaway environment, the way the
    unit hands it one."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ.update(env)
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class SessionRoutes(unittest.TestCase):
    """The three mutation routes, driven over HTTP against the real handler.

    Over HTTP rather than on the functions, for the reason the profile-route
    suite gives: what is being pinned is what the handler WRITES, and the
    interesting failures live between the form and the file.
    """

    LIVE = {
        "claude": {"agent": "claude", "hasRun": True,
                   "boxSessionId": "9d3f-abc", "stopped": True},
        "codex": {"agent": "codex", "hasRun": True},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, "agent-box")
        os.makedirs(self.conf)
        self.env_file = os.path.join(self.conf, "env")
        open(self.env_file, "w").close()
        self.sessions_file = os.path.join(self.conf, "sessions.json")

    # --- fixtures ---------------------------------------------------------
    def write_raw(self, text):
        with open(self.sessions_file, "w") as handle:
            handle.write(text)

    def raw(self):
        with open(self.sessions_file) as handle:
            return handle.read()

    def document(self):
        return json.loads(self.raw())

    def serve(self, **extra):
        env = {
            "AGENT_BOX_SETTINGS_ENV_FILE": self.env_file,
            "AGENT_BOX_SESSIONS_FILE": self.sessions_file,
            "AGENT_BOX_AGENTS": "claude,codex,shell",
            "AGENT_BOX_DEFAULT_AGENT": "claude",
            "HOME": self.tmp.name,
        }
        env.update(extra)
        module = daemon_with(**env)
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), module.Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.module = module
        self.base = "http://127.0.0.1:%d/settings" % server.server_address[1]
        return module, self.base

    def post(self, path, **fields):
        """POST a form; return (status, Location) WITHOUT following the
        redirect -- the redirect target is the whole answer here."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(fields).encode(), method="POST")
        try:
            response = urllib.request.build_opener(NoRedirect).open(request)
            return response.status, response.headers.get("Location", "")
        except urllib.error.HTTPError as exc:
            # A 303 with the redirect handler disabled arrives here. Closing
            # it keeps the run free of ResourceWarnings, which a reader of a
            # failing log should not have to discount.
            with exc:
                return exc.code, exc.headers.get("Location", "")

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.read().decode()

    def each_route(self):
        """The three verbs, each as (label, callable)."""
        return [
            ("add", lambda: self.post("/sessions/add", back="settings",
                                      agent="shell", cwd="~", prompt="")),
            ("delete", lambda: self.post("/sessions/delete", back="settings",
                                         name="claude")),
            # "restart" doubles as Start on the stopped session above, which
            # is the branch that writes.
            ("restart", lambda: self.post("/sessions/restart", back="settings",
                                          name="claude")),
        ]

    # --- the refusal ------------------------------------------------------
    def test_a_registry_that_cannot_be_read_is_never_republished(self):
        """The bug itself. Each verb, against each way the file can be
        broken, must leave it byte for byte as it was."""
        for label, text in CORRUPT:
            for verb, call in self.each_route():
                with self.subTest(corruption=label, route=verb):
                    self.setUp()
                    self.write_raw(text)
                    self.serve()
                    status, location = call()
                    self.assertEqual(status, 303)
                    self.assertIn("ok=session_registry_unreadable", location)
                    self.assertEqual(self.raw(), text)

    def test_the_refusal_is_a_banner_the_operator_can_read(self):
        """Silence was the bug: the route answered "Session added" over a
        registry it had just emptied. An operator here has no shell, so the
        page has to say what happened and that the box repairs itself."""
        self.write_raw("not json\n")
        self.serve()
        _, location = self.post("/sessions/add", back="settings",
                                agent="shell", cwd="~", prompt="")
        page = self.get("/?" + location.split("?", 1)[1])
        self.assertIn("could not be read", page)
        self.assertIn("nothing was changed", page)

    def test_a_corrupt_registry_renders_as_no_sessions_rather_than_a_500(self):
        """The READ paths keep answering {}. A page that renders an empty
        list beats a traceback on every route that mentions a session, and
        the supervisor moves the bad file aside within a tick or two -- so
        this stays a transient empty list, not a rewrite."""
        self.write_raw("not json\n")
        self.serve()
        self.assertIn("<html", self.get("/").lower())

    # --- what still has to work ------------------------------------------
    LEAVES = {
        "add": {"claude", "codex", "shell"},
        "delete": {"codex"},
        "restart": {"claude", "codex"},
    }

    def test_a_readable_registry_is_still_added_to_deleted_from_and_started(self):
        """The negative control: the refusal must not have been bought by
        refusing everything."""
        for verb, call in self.each_route():
            with self.subTest(route=verb):
                self.setUp()
                self.write_raw(json.dumps({"version": 1, "sessions": self.LIVE}))
                self.serve()
                status, location = call()
                self.assertEqual(status, 303)
                self.assertNotIn("unreadable", location)
                self.assertEqual(set(self.document()["sessions"]),
                                 self.LEAVES[verb])
                if verb == "restart":
                    # Start is the branch that writes: it clears `stopped`.
                    self.assertNotIn(
                        "stopped", self.document()["sessions"]["claude"])

    def test_a_missing_registry_is_a_first_boot_not_a_refusal(self):
        """Nothing to lose and nothing to preserve: creating the file is
        exactly what an add does on a box that has never had one."""
        self.serve()
        status, location = self.post("/sessions/add", back="settings",
                                     agent="shell", cwd="~", prompt="")
        self.assertEqual(status, 303)
        self.assertNotIn("unreadable", location)
        self.assertEqual(list(self.document()["sessions"]), ["shell"])

    # --- the version ------------------------------------------------------
    def test_an_unknown_version_is_preserved_not_stamped_back_to_one(self):
        """A v2 registry is a document this daemon does not understand.
        Writing 1 over it tells every other reader that it does."""
        self.write_raw(json.dumps({"version": 2, "sessions": self.LIVE}))
        self.serve()
        self.post("/sessions/add", back="settings",
                  agent="shell", cwd="~", prompt="")
        self.assertEqual(self.document()["version"], 2)

    def test_a_registry_with_no_version_still_gets_one(self):
        self.write_raw(json.dumps({"sessions": self.LIVE}))
        self.serve()
        self.post("/sessions/add", back="settings",
                  agent="shell", cwd="~", prompt="")
        self.assertEqual(self.document()["version"], 1)

    # --- the smaller silent loss -----------------------------------------
    def test_an_entry_that_is_not_an_object_survives_a_write(self):
        """read_sessions drops it for DISPLAY, which is right -- there is no
        row to draw. Dropping it on the way to a write would delete it, which
        is the same data loss in a smaller place, so the write path keeps
        every entry verbatim."""
        self.write_raw(json.dumps(
            {"version": 1, "sessions": dict(self.LIVE, odd="not an object")}))
        self.serve()
        self.post("/sessions/add", back="settings",
                  agent="shell", cwd="~", prompt="")
        self.assertEqual(self.document()["sessions"]["odd"], "not an object")

    def test_starting_an_entry_that_is_not_an_object_is_not_a_traceback(self):
        """The route pops a field off whatever it finds under that name."""
        self.write_raw(json.dumps({"version": 1, "sessions": {"odd": None}}))
        self.serve()
        status, location = self.post("/sessions/restart", back="settings",
                                     name="odd")
        self.assertEqual(status, 303)
        self.assertNotIn("unreadable", location)


if __name__ == "__main__":
    unittest.main(verbosity=2)
