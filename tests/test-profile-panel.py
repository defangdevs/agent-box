#!/usr/bin/env python3
"""The settings page's agent-profile panel (issue #321, step 5).

#321 asked for an *Agent profile* concept and said what it is for: "we
need to pick an Agent profile to start a session". #337 shipped the
storage, the CLI and `agent-box-session add --profile`; this is the half a
user with no shell can reach, which on this box is most of them.

What these tests pin is the part that is easy to get quietly wrong:

* a profile is where a token ends up, so the panel lists environment KEY
  NAMES and never a value — the rule `agent-box-profile show` and
  `env ls` already keep, and the one whose failure is silent;
* the profile NAME is recorded on the session, not just the arguments it
  resolved to. The spawn wrapper re-reads that name at every start to
  apply the profile's environment, so a session created here with the
  name dropped would start with the right flags and none of the env, and
  nothing would say so;
* a blank field CLEARS its key rather than keeping the old value. An
  empty MODEL left in the file resolves to `--model ''`, which starts
  nothing;
* the name is a path component (one file per profile), so it is matched
  against PROFILE_NAME_RE before it reaches the filesystem.

The subject is tests/golden/web/payloads/.../agent-box-settings, not
modules/src/settings-daemon.py, for the reason test-webhook-panel-state.py
gives: the daemon ships with the env-store library prepended, so the
source file alone does not import. The golden payload is that assembled
article, and the golden-snapshot check fails if it stops matching.
"""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")

# A stand-in for `agent-box-profile launch`. The real resolver is a shell
# script that needs jq and the env-store CLI on its PATH; what the daemon
# depends on is its CONTRACT — {harness, args, envKeys, warnings} on
# stdout — and that is what this pins. The daemon must not grow its own
# copy of the key-to-flag mapping, so a test that let it pass without
# calling anything would be testing the wrong thing.
#
# The profiles directory is baked in at write time rather than read from
# the environment, because the daemon passes NONE: it runs this the way
# systemd runs the real one, on the unit's own environment. Reaching for
# an env var here made every route test fail with "could not resolve",
# which is exactly the answer a resolver that cannot find its files gives.
# The shebang is this interpreter's own path, not /usr/bin/env: the nix
# sandbox this runs in as a flake check has no /usr/bin, so an env shebang
# makes every exec fail — and the daemon reports that as "could not resolve
# profile", which reads like a bug in the code under test.
FAKE_LAUNCH = """#!%(python)s
import json, os, sys
name = sys.argv[2]
override = sys.argv[3] if len(sys.argv) > 3 else ""
path = os.path.join(%(profiles)r, name + ".env")
if not os.path.exists(path):
    sys.stderr.write("no such profile\\n")
    sys.exit(2)
store = {}
for line in open(path):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        store[key] = value.strip('"')
harness = override or store.get("HARNESS") or "claude"
args = []
if store.get("MODEL"):
    args += ["--model", store["MODEL"]]
warnings = []
if store.get("SYSTEM_PROMPT") and harness == "codex":
    warnings.append("codex takes no appended system prompt.")
print(json.dumps({"harness": harness, "args": args,
                  "envKeys": [], "warnings": warnings}))
"""


def daemon_with(**env):
    """Import the settings daemon under exactly this environment.

    Its paths and its harness list are read at import, which is the whole
    point: what the unit hands the daemon is what the panel works with.
    """
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


class ProfileFixture(unittest.TestCase):
    """A throwaway ~/.config/agent-box with profiles in it, plus the fake
    resolver. Shared by both suites; it defines no tests of its own, so
    the rendering cases do not run a second time under the route ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, "agent-box")
        self.profiles = os.path.join(self.conf, "profiles")
        os.makedirs(self.profiles)
        self.env_file = os.path.join(self.conf, "env")
        open(self.env_file, "w").close()
        self.sessions_file = os.path.join(self.conf, "sessions.json")
        self.write_sessions({})
        self.launch = os.path.join(self.tmp.name, "fake-launch")
        with open(self.launch, "w") as handle:
            handle.write(FAKE_LAUNCH % {"profiles": self.profiles,
                                        "python": sys.executable})
        os.chmod(self.launch, 0o755)

    def write_sessions(self, sessions):
        with open(self.sessions_file, "w") as handle:
            json.dump({"sessions": sessions}, handle)

    def read_sessions(self):
        with open(self.sessions_file) as handle:
            return json.load(handle)["sessions"]

    def write_profile(self, name, text):
        with open(os.path.join(self.profiles, name + ".env"), "w") as handle:
            handle.write(text)

    def daemon(self, **extra):
        env = {
            "AGENT_BOX_SETTINGS_ENV_FILE": self.env_file,
            "AGENT_BOX_SESSIONS_FILE": self.sessions_file,
            "AGENT_BOX_AGENTS": "claude,codex,shell",
            "AGENT_BOX_DEFAULT_AGENT": "claude",
            "AGENT_BOX_PROFILE_BIN": self.launch,
            "HOME": self.tmp.name,
        }
        env.update(extra)
        return daemon_with(**env)


class ProfilePanel(ProfileFixture):
    """What the panel shows."""

    # --- reading -----------------------------------------------------

    def test_a_profile_is_read_through_the_env_stores_own_parser(self):
        """A SYSTEM_PROMPT spans lines for the same reason a PEM does
        (issue #212), so the panel must not parse the file itself."""
        self.write_profile("triage",
                           'HARNESS=claude\nMODEL=sonnet\n'
                           'SYSTEM_PROMPT="Triage only.\nDo not fix."\n')
        module = self.daemon()
        profiles = module.read_profiles()
        self.assertEqual(sorted(profiles), ["triage"])
        reserved = profiles["triage"]["reserved"]
        self.assertEqual(reserved["MODEL"], "sonnet")
        self.assertEqual(reserved["SYSTEM_PROMPT"], "Triage only.\nDo not fix.")

    def test_a_name_that_is_not_a_profile_name_is_not_listed(self):
        """The name is a path component, so the directory is filtered on
        the way OUT as well as on the way in."""
        self.write_profile("ok", "HARNESS=claude\n")
        with open(os.path.join(self.profiles, "not a profile.env"), "w") as fh:
            fh.write("HARNESS=claude\n")
        with open(os.path.join(self.profiles, "README"), "w") as fh:
            fh.write("ignored\n")
        self.assertEqual(sorted(self.daemon().read_profiles()), ["ok"])

    def test_a_missing_profiles_directory_is_empty_not_an_error(self):
        """A box where nobody has made a profile still renders a page."""
        module = self.daemon(AGENT_BOX_SETTINGS_ENV_FILE=os.path.join(
            self.tmp.name, "nowhere", "env"))
        self.assertEqual(module.read_profiles(), {})

    # --- rendering ---------------------------------------------------

    def test_the_panel_lists_env_key_names_and_never_a_value(self):
        """The rule that matters most here: a profile is exactly where a
        token ends up, and a leak would be silent."""
        self.write_profile("triage",
                           "HARNESS=claude\nGH_TOKEN=ghp_supersecret\n")
        module = self.daemon()
        html = module.render_profiles(module.read_profiles())
        self.assertIn("GH_TOKEN", html)
        self.assertNotIn("ghp_supersecret", html)

    def test_the_session_row_picker_offers_no_profile_first(self):
        """The picker adds a choice; it does not take the default away.
        Every session on this box until now had no profile."""
        self.write_profile("triage", "HARNESS=claude\n")
        module = self.daemon()
        options = module.render_profile_options(module.read_profiles())
        self.assertTrue(options.startswith('<option value="">'))
        self.assertIn('<option value="triage">triage (claude)</option>', options)

    def test_a_session_started_from_a_profile_says_so(self):
        self.write_sessions({"main": {"agent": "claude", "profile": "triage"},
                             "other": {"agent": "codex"}})
        module = self.daemon()
        html = module.render_sessions()
        self.assertIn("prof-tag", html)
        self.assertIn(">triage<", html)

    def test_a_profile_name_is_escaped_into_the_page(self):
        """The name reaches an href-less attribute and a confirm() string,
        so it is data, not markup (the rule issue #277 states)."""
        self.write_profile("a-b_1", "HARNESS=claude\nMODEL=<script>\n")
        module = self.daemon()
        html = module.render_profiles(module.read_profiles())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_summary_line_is_escaped_exactly_once(self):
        """Its parts are escaped as they go in, so escaping the join too
        turns "&" into visible entity text. The test above does not catch
        that on its own: the same value also reaches the edit form's value
        attribute, where it IS escaped once, so the assertion passes on
        that copy while the summary is wrong."""
        self.write_profile("p", "HARNESS=claude\nMODEL=a&b\n")
        module = self.daemon()
        html = module.render_profiles(module.read_profiles())
        # The summary is the <span class="meta"> in the row's <summary>.
        meta = html.split('<span class="meta">')[1].split("</span>")[0]
        self.assertIn("a&amp;b", meta)
        self.assertNotIn("&amp;amp;", meta)

    def test_no_resolver_means_no_picker_and_a_clear_refusal(self):
        """The settings unit is socket activated with stopIfChanged=false,
        so a daemon that survived an update can still be running on the
        environment it started with. Offering a picker that cannot work
        sends the operator hunting for a profile that is sitting right
        there in the list."""
        self.write_profile("triage", "HARNESS=claude\n")
        module = self.daemon(AGENT_BOX_PROFILE_BIN="")
        options = module.render_profile_options(module.read_profiles())
        self.assertNotIn("triage", options)


class ProfileRoutes(ProfileFixture):
    """The four POST verbs, driven over HTTP against the real handler.

    Rendering tests can be run on functions; these cannot: what is being
    pinned is what the handler WRITES, and the interesting failures live
    between the form and the file.
    """

    def serve(self, **extra):
        module = self.daemon(**extra)
        self.module = module
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), module.Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return module, "http://127.0.0.1:%d/settings" % server.server_address[1]

    def post(self, base, path, **fields):
        """POST a form; return (status, body) WITHOUT following the
        redirect — the redirect target is the whole answer for a verb
        that succeeded."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(
            base + path,
            data=urllib.parse.urlencode(fields).encode(), method="POST")
        try:
            response = urllib.request.build_opener(NoRedirect).open(request)
            return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def profile_text(self, name):
        path = os.path.join(self.profiles, name + ".env")
        return open(path).read() if os.path.exists(path) else None

    def test_saving_a_profile_writes_only_the_fields_that_were_filled_in(self):
        """A blank field CLEARS its key rather than storing "". An empty
        MODEL present in the file resolves to `--model ''`, which starts
        nothing — so the form is the whole truth about a profile."""
        _, base = self.serve()
        status, _ = self.post(base, "/profiles/set", name="review",
                              HARNESS="claude", MODEL="opus", EFFORT="",
                              SYSTEM_PROMPT="Review only.")
        self.assertEqual(status, 303)
        text = self.profile_text("review")
        self.assertIn("HARNESS=claude", text)
        self.assertIn("MODEL=opus", text)
        self.assertNotIn("EFFORT=", text)
        # Re-saving with the field blank drops it, rather than leaving the
        # value the form was showing a moment ago.
        self.post(base, "/profiles/set", name="review", HARNESS="claude",
                  MODEL="", EFFORT="", SYSTEM_PROMPT="")
        text = self.profile_text("review")
        self.assertNotIn("MODEL=", text)
        self.assertNotIn("Review only.", text)

    def test_a_key_the_harness_cannot_use_is_reported_when_it_is_saved(self):
        """Not at the next session start, which is where the operator has
        stopped looking. The resolver has the last word on this, so the
        answer comes from it and not from a second copy here."""
        _, base = self.serve()
        status, body = self.post(base, "/profiles/set", name="cx",
                                 HARNESS="codex", MODEL="", EFFORT="",
                                 SYSTEM_PROMPT="be brief")
        self.assertEqual(status, 200)
        self.assertIn("codex takes no appended system prompt", body)

    def test_environment_keys_are_added_and_removed_one_at_a_time(self):
        """Dropping a key and setting it to "" are different states in the
        store, and only one of them is what "remove this" means."""
        _, base = self.serve()
        self.post(base, "/profiles/set", name="review", HARNESS="claude")
        self.post(base, "/profiles/setkey", name="review",
                  key="API_TOKEN", value="hunter2")
        self.assertIn("hunter2", self.profile_text("review"))
        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("API_TOKEN", page)
        self.assertNotIn("hunter2", page)
        self.post(base, "/profiles/delkey", name="review", key="API_TOKEN")
        self.assertNotIn("API_TOKEN", self.profile_text("review"))

    def test_a_reserved_key_cannot_be_set_as_environment(self):
        """It is launch config, and it is cleared by saving the form with
        that field blank. Two doors onto one state, with no reason to
        prefer either, is how they drift apart."""
        _, base = self.serve()
        self.post(base, "/profiles/set", name="review", HARNESS="claude")
        status, _ = self.post(base, "/profiles/setkey", name="review",
                              key="HARNESS", value="codex")
        self.assertEqual(status, 400)

    def test_a_name_that_is_not_a_profile_name_never_reaches_the_disk(self):
        """One file per profile, so the name is a path component."""
        _, base = self.serve()
        for name in ("../escape", "has space", ""):
            status, _ = self.post(base, "/profiles/set", name=name,
                                  HARNESS="claude")
            self.assertEqual(status, 400, name)
        self.assertEqual(os.listdir(self.profiles), [])

    def test_an_unknown_harness_is_refused(self):
        _, base = self.serve()
        status, body = self.post(base, "/profiles/set", name="ok",
                                 HARNESS="nosuch")
        self.assertEqual(status, 400)
        self.assertIn("Unknown harness", body)

    def test_an_unknown_verb_is_a_404_not_a_traceback(self):
        _, base = self.serve()
        status, _ = self.post(base, "/profiles/nope", name="ok")
        self.assertEqual(status, 404)

    def test_adding_a_session_records_the_profile_name_not_just_its_args(self):
        """The load-bearing one. The spawn wrapper re-reads the NAME at
        every start to apply the profile's environment (#337), so a
        session created with the name dropped starts with the right flags
        and none of the environment — and nothing says so."""
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\nMODEL=sonnet\n")
        status, _ = self.post(base, "/sessions/add", back="settings",
                              agent="shell", profile="triage", cwd="~",
                              prompt="")
        self.assertEqual(status, 303)
        entries = list(self.read_sessions().values())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["profile"], "triage")
        self.assertEqual(entries[0]["extraArgs"], ["--model", "sonnet"])

    def test_the_profiles_harness_wins_over_the_select_beside_it(self):
        """A <select> always posts a value, so this form cannot tell
        "chose claude" from "left it alone" — and a rule that turned on a
        difference the page cannot see would be a guess. The CLI's
        `--profile P --agent H` is still the way to say the other thing."""
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\n")
        self.post(base, "/sessions/add", back="settings", agent="shell",
                  profile="triage", cwd="~", prompt="")
        self.assertEqual(list(self.read_sessions().values())[0]["agent"],
                         "claude")

    def test_adding_a_session_with_no_profile_is_unchanged(self):
        """The picker adds a choice. Everything that worked before it must
        still work exactly as it did."""
        _, base = self.serve()
        self.post(base, "/sessions/add", back="settings", agent="codex",
                  profile="", cwd="~", prompt="")
        entry = list(self.read_sessions().values())[0]
        self.assertEqual(entry["agent"], "codex")
        self.assertIsNone(entry["profile"])
        self.assertEqual(entry["extraArgs"], [])

    def test_a_resolver_that_prints_a_non_object_is_not_a_500(self):
        """json.loads takes any JSON value; .get() on a list is an
        AttributeError, which would be a traceback in the journal and a 500
        on the page instead of the answer this path is written to give."""
        with open(self.launch, "w") as handle:
            handle.write("#!%s\nprint('[]')\n" % sys.executable)
        os.chmod(self.launch, 0o755)
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\n")
        status, body = self.post(base, "/sessions/add", back="settings",
                                 agent="claude", profile="triage", cwd="~",
                                 prompt="")
        self.assertEqual(status, 400)
        self.assertIn("Could not resolve profile", body)

    def test_a_box_with_no_resolver_says_so_rather_than_blaming_the_profile(self):
        """The two are fixed differently: one is a restart of this daemon,
        the other is a hunt for a profile that was deleted."""
        _, base = self.serve(AGENT_BOX_PROFILE_BIN="")
        self.write_profile("triage", "HARNESS=claude\n")
        status, body = self.post(base, "/sessions/add", back="settings",
                                 agent="claude", profile="triage", cwd="~",
                                 prompt="")
        self.assertEqual(status, 503)
        self.assertIn("no profile resolver", body)
        self.assertNotIn("may have just been deleted", body)
        self.assertEqual(self.read_sessions(), {})

    def test_deleting_a_profile_waits_for_a_write_in_flight(self):
        """Otherwise a save can read the file, the delete can unlink it,
        and the save can write it back — a profile the operator deleted,
        quietly recreated. Asserted by holding the store's own lock and
        watching the delete wait for it, rather than by reading the source
        for the word "locked"."""
        module = self.daemon()
        self.write_profile("triage", "HARNESS=claude\n")
        path = module.profile_path("triage")
        done = threading.Event()
        with module.locked(path):
            thread = threading.Thread(
                target=lambda: (module.profile_remove("triage"), done.set()))
            thread.start()
            # While the lock is held the delete cannot have happened. A
            # generous wait, because proving a negative on a thread that is
            # meant to be blocked is the one case where a sleep is the test.
            self.assertFalse(done.wait(0.5))
            self.assertTrue(os.path.exists(path))
        thread.join(5)
        self.assertTrue(done.is_set(), "the delete never completed")
        self.assertIsNone(self.profile_text("triage"))

    def test_a_profile_that_vanished_between_render_and_submit_is_an_error(self):
        """Not a session quietly started as something else."""
        _, base = self.serve()
        status, body = self.post(base, "/sessions/add", back="settings",
                                 agent="claude", profile="gone", cwd="~",
                                 prompt="")
        self.assertEqual(status, 400)
        self.assertIn("Could not resolve profile", body)
        self.assertEqual(self.read_sessions(), {})

    def test_deleting_a_profile_leaves_running_sessions_alone(self):
        """They keep the arguments they were created with, which is what
        the banner promises and what the registry entry already holds."""
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\nMODEL=sonnet\n")
        self.post(base, "/sessions/add", back="settings", agent="claude",
                  profile="triage", cwd="~", prompt="")
        self.post(base, "/profiles/delete", name="triage")
        self.assertIsNone(self.profile_text("triage"))
        entry = list(self.read_sessions().values())[0]
        self.assertEqual(entry["profile"], "triage")
        self.assertEqual(entry["extraArgs"], ["--model", "sonnet"])
        # And the page still renders, naming the profile the session was
        # started as even though it is gone.
        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("Agent profiles", page)
        self.assertIn("prof-tag", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
