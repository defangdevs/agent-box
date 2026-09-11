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

So the routes are held to four things:

  * a registry they could not READ is never republished -- the file comes
    through a refused add, delete or restart byte for byte;
  * a registry they could not LOCK is not written either (issue #633).
    Timing out used to fall through and run the read-modify-write anyway,
    which is the pre-#254 lost update performed at the one moment there is
    provably another writer -- and answered with a 303 the browser reads as
    "done". Now the route changes nothing and says 503;
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
import concurrent.futures
import fcntl
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import stat
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


class RouteCase(unittest.TestCase):
    """Fixtures for driving the three mutation routes over HTTP against the
    real handler.

    Over HTTP rather than on the functions, for the reason the profile-route
    suite gives: what is being pinned is what the handler WRITES, and the
    interesting failures live between the form and the file.

    No tests of its own -- SessionRoutes and LockRefusal each bring their
    own, and both need every fixture here.
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
        module.capacity_live = lambda: set()
        module.capacity_limit = lambda: 100
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

    def post_body(self, path, **fields):
        """POST a form and return the response BODY, redirect or not."""
        request = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(fields).encode(), method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.read().decode()

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


class SessionRoutes(RouteCase):
    """A registry the routes could not READ is never republished (#279)."""

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
        # Issue #549: this refusal rides the ok= redirect channel (its
        # name describes the mechanism, not that the news is good), and
        # used to render in the exact same green as "Session added".
        self.assertIn('data-kind="error"', page)
        self.assertIn('role="alert"', page)

    def test_a_successful_add_is_a_banner_the_operator_can_trust(self):
        """The other half of #549: a REAL success must stay green, not just
        a failure turn red. Same route, an intact registry this time."""
        self.serve()
        _, location = self.post("/sessions/add", back="settings",
                                agent="shell", cwd="~", prompt="")
        page = self.get("/?" + location.split("?", 1)[1])
        self.assertIn("Session added", page)
        self.assertIn('data-kind="ok"', page)
        self.assertIn('role="status"', page)

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


class LockRefusal(RouteCase):
    """A registry the route could not LOCK is not written either (#633).

    Same three verbs, same fixture, one difference: the sidecar lock is
    held (or cannot be made) while the request runs. The evidence in the
    issue was a mutator that answered 0 and changed the file while another
    writer held the lock; here the equivalent is a 303 saying "Session
    added" over a document some other writer is mid-way through replacing.
    """

    LOCK_WAIT = 0.4

    def serve(self, **extra):
        module, base = super().serve(**extra)
        # The shipped bound is 10s, which is right on a box and would make
        # this file thirty seconds of waiting. The knob exists for exactly
        # this, like REGISTRY_LOCK_WAIT on the shell side.
        module.SESSIONS_LOCK_WAIT = self.LOCK_WAIT
        return module, base

    def hold_the_lock(self):
        """Hold the sidecar the way any of the five writers holds it."""
        handle = open(self.sessions_file + ".lock", "a", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    def unwritable_config_dir(self):
        """No sidecar, and no way to create one: a read-only home or a full
        disk. The other half of the acceptance -- lock CREATION failure."""
        if os.geteuid() == 0:
            self.skipTest("root ignores the directory mode this test needs")
        os.chmod(self.conf, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.conf, 0o700)

    def refuse_every_verb(self):
        """Post all three verbs at one registry and assert each is a 503
        that changed nothing.

        One fixture for all three, unlike the #279 suite above: a refusal
        writes nothing, so the file the second verb meets is the same file
        the first one did -- which makes "byte for byte" an assertion about
        the whole sequence rather than three fresh starts.
        """
        before = self.raw()
        for verb, call in self.each_route():
            with self.subTest(route=verb):
                status, _ = call()
                self.assertEqual(status, 503)
                self.assertEqual(self.raw(), before)

    def test_a_registry_that_cannot_be_locked_is_never_rewritten(self):
        """A holder that times us out. The daemon used to give up waiting
        and do the read-modify-write anyway."""
        self.write_raw(json.dumps({"version": 1, "sessions": self.LIVE}))
        self.serve()
        self.hold_the_lock()
        self.refuse_every_verb()

    def test_a_sidecar_that_cannot_be_created_is_refused_too(self):
        """Lock CREATION failure, the other half of the acceptance."""
        self.write_raw(json.dumps({"version": 1, "sessions": self.LIVE}))
        self.serve()
        self.unwritable_config_dir()
        self.refuse_every_verb()

    def test_the_refusal_is_something_the_operator_can_read_and_act_on(self):
        """503 is for the client; the body is for the person. They have no
        shell here, so "try again" has to be on the page."""
        self.write_raw(json.dumps({"version": 1, "sessions": self.LIVE}))
        self.serve()
        self.hold_the_lock()
        page = self.post_body("/sessions/add", back="settings",
                              agent="shell", cwd="~", prompt="")
        self.assertIn("nothing was done", page)
        self.assertIn("try again", page)
        self.assertIn('data-kind="error"', page)

    def test_reads_stay_available_while_a_writer_holds_the_lock(self):
        """The refusal is scoped to mutations. A page that stopped
        rendering the session list under contention would take the whole
        UI down every time two writers met."""
        # Names nothing else on the page could be spelling: "claude" and
        # "codex" are harness names too, so LIVE's own keys would pass this
        # against a page listing no sessions at all.
        self.write_raw(json.dumps({"version": 1, "sessions": {
            "zeta-one": {"agent": "claude"}, "zeta-two": {"agent": "codex"}}}))
        self.serve()
        self.hold_the_lock()
        page = self.get("/")
        self.assertIn("zeta-one", page)
        self.assertIn("zeta-two", page)

    def test_a_refusal_does_not_stop_the_next_attempt(self):
        """Retryable in the plainest sense: the same route, one lock
        release apart. Nothing is latched onto the registry or the
        daemon."""
        self.write_raw(json.dumps({"version": 1, "sessions": self.LIVE}))
        self.serve()
        holder = self.hold_the_lock()
        status, _ = self.post("/sessions/add", back="settings",
                              agent="shell", cwd="~", prompt="")
        self.assertEqual(status, 503)
        fcntl.flock(holder, fcntl.LOCK_UN)
        status, location = self.post("/sessions/add", back="settings",
                                     agent="shell", cwd="~", prompt="")
        self.assertEqual(status, 303)
        self.assertNotIn("unreadable", location)
        self.assertEqual(set(self.document()["sessions"]),
                         set(self.LIVE) | {"shell"})

    def test_concurrent_adds_all_survive_when_the_lock_works(self):
        """The case the refusal must not have been bought with. This
        daemon is a ThreadingHTTPServer, so it races itself: six adds at
        once have to leave six sessions, not one."""
        adds = 6
        self.write_raw(json.dumps({"version": 1, "sessions": {}}))
        self.serve()
        start = threading.Barrier(adds)
        results = []
        lock = threading.Lock()

        def add():
            start.wait(timeout=30)
            status, _ = self.post("/sessions/add", back="settings",
                                  agent="shell", cwd="~", prompt="")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=add) for _ in range(adds)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(results, [303] * adds)
        self.assertEqual(len(self.document()["sessions"]), adds,
                         self.document()["sessions"])


class CapacityRoutes(RouteCase):
    def test_concurrent_ui_adds_share_pending_capacity(self):
        self.write_raw(json.dumps({"version": 1, "sessions": {}}))
        module, _ = self.serve()
        module.capacity_limit = lambda: 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.post("/sessions/add", agent="shell")[0], range(6)))
        self.assertEqual(sorted(results), [303, 303, 503, 503, 503, 503])
        self.assertEqual(len(self.document()["sessions"]), 2)


    def test_add_refuses_at_capacity_without_writing(self):
        self.write_raw(json.dumps({"version": 1, "sessions": {"a": {}}}))
        module, _ = self.serve()
        module.capacity_limit = lambda: 1
        before = self.raw()
        status, _ = self.post("/sessions/add", agent="shell")
        self.assertEqual(status, 503)
        self.assertEqual(self.raw(), before)

    def test_restart_stopped_refuses_but_running_restart_succeeds(self):
        self.write_raw(json.dumps({"version": 1, "sessions": {
            "a": {}, "b": {"stopped": True}}}))
        module, _ = self.serve()
        module.capacity_limit = lambda: 1
        module.kill_session = lambda name: None
        before = self.raw()
        status, _ = self.post("/sessions/restart", name="b")
        self.assertEqual(status, 503)
        self.assertEqual(self.raw(), before)
        status, _ = self.post("/sessions/restart", name="a")
        self.assertEqual(status, 303)

    def test_signin_stays_successful_and_records_autostart_notice(self):
        self.write_raw(json.dumps({"version": 1, "sessions": {"a": {"agent": "shell"}}}))
        module, _ = self.serve()
        module.capacity_limit = lambda: 1
        before = self.raw()
        message = module.ensure_harness_session("claude", True)
        self.assertIn("Signed in; session not started", message)
        self.assertEqual(module._session_start_notices["claude"], message)
        self.assertEqual(self.raw(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
