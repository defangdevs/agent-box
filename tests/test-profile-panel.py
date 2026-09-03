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
harness = override or store.get("HARNESS")
if not harness:
    # No box default left (issue #493): a profile names its harness or it
    # is not startable, and the real resolver says so here too.
    sys.stderr.write("no HARNESS set\\n")
    sys.exit(2)
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

    def test_the_session_row_picker_asks_rather_than_defaulting(self):
        """The picker is the row's ONLY control now (issue #493), so it can
        no longer lead with a "no profile" value: there is no assistant
        <select> beside it to fall through to, and no box-wide default
        assistant behind that. It asks instead, and offers `shell` as the
        one entry that is not a profile file."""
        self.write_profile("triage", "HARNESS=claude\n")
        module = self.daemon()
        options = module.render_profile_options(module.read_profiles())
        self.assertTrue(options.startswith('<option value="" disabled selected>'))
        self.assertIn("Choose a profile", options)
        self.assertIn('<option value="triage">triage (claude)</option>', options)
        # `shell` is offered, and never as a profile file.
        self.assertIn('<option value=":shell">', options)
        # Nothing posts an empty profile any more.
        self.assertNotIn('<option value="">', options)

    def test_a_profile_may_be_named_shell_without_colliding(self):
        """A profile NAME of "shell" is legal - what #493 refuses is
        HARNESS=shell - so the pseudo-entry cannot use the bare word as its
        value. It uses ":shell", which PROFILE_NAME_RE can never match."""
        self.write_profile("shell", "HARNESS=claude\n")
        module = self.daemon()
        options = module.render_profile_options(module.read_profiles())
        self.assertIn('<option value="shell">shell (claude)</option>', options)
        self.assertIn('<option value=":shell">', options)
        self.assertFalse(module.PROFILE_NAME_RE.match(module.SHELL_PSEUDO_PROFILE))

    def test_a_session_started_from_a_profile_says_so(self):
        self.write_sessions({"main": {"agent": "claude", "profile": "triage"},
                             "other": {"agent": "codex"}})
        module = self.daemon()
        html = module.render_sessions()
        self.assertIn("prof-tag", html)
        self.assertIn(">triage<", html)
        # The tag is its own balanced fragment. It was once a
        # `</span><span…` splice appended to the harness name, correct only
        # while the caller wrapped that name in exactly one <span>.
        self.assertEqual(html.count("<span"), html.count("</span>"))

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
        status, body = self.post(base, "/profiles/setkey", name="review",
                                 key="HARNESS", value="codex")
        self.assertEqual(status, 400)
        self.assertIn("Assistant is already a profile option", body)
        self.assertNotIn("HARNESS, MODEL, EFFORT, SYSTEM_PROMPT", body)

    def test_a_name_that_is_not_a_profile_name_never_reaches_the_disk(self):
        """One file per profile, so the name is a path component."""
        _, base = self.serve()
        for name in ("../escape", "has space", ""):
            status, _ = self.post(base, "/profiles/set", name=name,
                                  HARNESS="claude")
            self.assertEqual(status, 400, name)
        self.assertEqual(os.listdir(self.profiles), [])

    def test_an_unknown_harness_is_refused(self):
        """A profile cannot select an assistant unavailable on this box."""
        _, base = self.serve()
        status, body = self.post(base, "/profiles/set", name="ok",
                                 HARNESS="nosuch")
        self.assertEqual(status, 400)
        self.assertIn("Unknown assistant", body)

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

    def test_the_auto_name_comes_from_the_profile_not_the_harness(self):
        """Two profiles on the same harness ("triage" and "reviewer", both
        claude) used to both mint from "claude" ("claude", then "claude-2")
        — indistinguishable in the row, for the same reason issue #277
        named. The profile is the more specific worker identity, so it is
        the name gen_session_name mints from."""
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\n")
        self.write_profile("reviewer", "HARNESS=claude\n")
        self.post(base, "/sessions/add", back="settings",
                  profile="triage", cwd="~", prompt="")
        self.post(base, "/sessions/add", back="settings",
                  profile="reviewer", cwd="~", prompt="")
        self.assertEqual(set(self.read_sessions()), {"triage", "reviewer"})

    def test_the_profiles_harness_wins_over_the_select_beside_it(self):
        """A <select> always posts a value, so this form cannot tell
        "chose claude" from "left it alone" — and a rule that turned on a
        difference the page cannot see would be a guess. The CLI's
        `--profile P --harness H` is still the way to say the other thing."""
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\n")
        self.post(base, "/sessions/add", back="settings", agent="shell",
                  profile="triage", cwd="~", prompt="")
        self.assertEqual(list(self.read_sessions().values())[0]["agent"],
                         "claude")

    def test_a_profile_with_no_harness_is_refused(self):
        """There is nothing left to rescue it with. The row's assistant
        <select> used to be handed to the resolver as an override for
        exactly this case; with the select gone (issue #493) a profile that
        names no assistant is simply not startable, and the resolver is
        what says so."""
        _, base = self.serve()
        self.write_profile("modelonly", "MODEL=opus\n")
        status, _ = self.post(base, "/sessions/add", back="settings",
                              profile="modelonly", cwd="~", prompt="")
        self.assertEqual(status, 400)
        self.assertEqual(self.read_sessions(), {})

    def test_a_profile_that_names_a_harness_still_wins(self):
        """The override is passed ONLY when the profile names none, so the
        rule above it is unchanged."""
        _, base = self.serve()
        self.write_profile("triage", "HARNESS=claude\n")
        self.post(base, "/sessions/add", back="settings", agent="codex",
                  profile="triage", cwd="~", prompt="")
        self.assertEqual(list(self.read_sessions().values())[0]["agent"],
                         "claude")

    def test_removing_a_key_never_creates_a_profile(self):
        """update() writes the file whether or not the load found one, so
        this used to create an empty profile — header only, no harness —
        and then list it in the panel and offer it in the picker. A stale
        tab whose profile was deleted in another one reaches this path."""
        module, base = self.serve()
        self.post(base, "/profiles/delkey", name="ghost", key="SOME_KEY")
        self.assertIsNone(self.profile_text("ghost"))
        # No PROFILE is created. Taking the store's lock does leave its own
        # ghost.env.lock sidecar behind, which is why this asserts on what
        # the page can see rather than on the directory being empty:
        # read_profiles() reads *.env, so a stray lock file is not a profile.
        self.assertEqual(module.read_profiles(), {})
        self.assertEqual([f for f in os.listdir(self.profiles)
                          if f.endswith(".env")], [])

    def test_a_delete_landing_mid_write_does_not_resurrect_the_profile(self):
        """The existence check has to be INSIDE the lock. Outside it, a
        concurrent /profiles/delete lands between the check and the write,
        and the write recreates the profile that was just removed — with
        values from a form that may itself be stale.

        Forced, not hoped for: the store's lock is held here, the write is
        started on a thread and blocks on it, the profile is deleted, and
        only then is the lock released. That is exactly the interleaving,
        every run."""
        module = self.daemon()
        self.write_profile("triage", "HARNESS=claude\n")
        path = module.profile_path("triage")
        result = {}
        with module.locked(path):
            thread = threading.Thread(
                target=lambda: result.update(
                    written=module.profile_write(
                        "triage", [("TOKEN", "x")], must_exist=True)))
            thread.start()
            # The writer is now blocked on the lock we hold. Delete the file
            # under it — the unlink needs no lock of its own to reach the
            # filesystem, which is the whole point of the race.
            thread.join(0.3)
            self.assertTrue(thread.is_alive(),
                            "the write did not block on the lock, so this "
                            "test is not exercising the race it names")
            os.unlink(path)
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the write never finished")
        self.assertIs(result.get("written"), False)
        self.assertFalse(os.path.exists(path),
                         "the write recreated a deleted profile")

    def test_adding_a_key_to_a_missing_profile_is_a_404(self):
        """Creating a profile is /profiles/set's job. This form only ever
        appears inside an existing profile's fold, so reaching it for one
        that is gone means the page is stale."""
        module, base = self.serve()
        status, body = self.post(base, "/profiles/setkey", name="ghost",
                                 key="TOKEN", value="x")
        self.assertEqual(status, 404)
        self.assertIn("No profile named", body)
        self.assertEqual(module.read_profiles(), {})
        self.assertEqual([f for f in os.listdir(self.profiles)
                          if f.endswith(".env")], [])

    def test_adding_a_session_with_no_profile_is_refused(self):
        """It used to fall through to the row's assistant <select>, and
        behind that to the box default. Both are gone (issue #493), so a
        post naming no profile has nothing left to mean - and the page says
        so rather than picking an assistant on the operator's behalf."""
        _, base = self.serve()
        status, body = self.post(base, "/sessions/add", back="settings",
                                 profile="", cwd="~", prompt="")
        self.assertEqual(status, 400)
        self.assertIn("Pick a profile", body)
        self.assertEqual(self.read_sessions(), {})

    def test_the_shell_pseudo_profile_starts_a_bare_shell(self):
        """The one picker entry that is not a profile file: it records no
        profile name, because there is none to re-read at the next spawn."""
        _, base = self.serve()
        self.post(base, "/sessions/add", back="settings", profile=":shell",
                  cwd="~", prompt="")
        entry = list(self.read_sessions().values())[0]
        self.assertEqual(entry["agent"], "shell")
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
        self.assertIn("Could not load profile", body)

    def test_a_box_with_no_resolver_says_so_rather_than_blaming_the_profile(self):
        """The two are fixed differently: one is a restart of this daemon,
        the other is a hunt for a profile that was deleted."""
        _, base = self.serve(AGENT_BOX_PROFILE_BIN="")
        self.write_profile("triage", "HARNESS=claude\n")
        status, body = self.post(base, "/sessions/add", back="settings",
                                 agent="claude", profile="triage", cwd="~",
                                 prompt="")
        self.assertEqual(status, 503)
        self.assertIn("Profiles are temporarily unavailable", body)
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
        self.assertIn("Could not load profile", body)
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
        self.assertIn("<h2>Profiles</h2>", page)
        self.assertNotIn("harness plus the model", page)
        self.assertIn("reasoning level", page)
        self.assertIn("prof-tag", page)


class ProfilesAreNotBuiltAroundABoxDefault(ProfileFixture):
    """Issue #493, bullets 2 and 3.

    A profile is a WORKER: a harness plus the model, effort and prompt that
    tell two workers on that harness apart. Two things follow, and both used
    to be false.

    `shell` is not one of those harnesses. It has no model, effort or system
    prompt to configure, so a profile built around it configures nothing --
    the resolver could only warn that all three were ignored. It stays a
    session KIND (`--harness shell` still works); it stops being a profile
    answer.

    And there is no "box default" harness to fall back to. A box now starts
    with no harness installed and installs them on demand, so the default
    named nothing anybody chose, and a profile resolving through it would
    quietly become a different worker the day the box's configuration
    changed. The picker's blank entry is now a prompt, not a value.
    """

    # Borrowed, not inherited: subclassing ProfileRoutes would re-run its
    # own two dozen tests under this class's name for two helpers.
    serve = ProfileRoutes.serve
    post = ProfileRoutes.post

    def test_shell_is_not_offered_as_a_profile_harness(self):
        _, base = self.serve()
        page = urllib.request.urlopen(base + "/").read().decode()
        editor = page.split('id="profile-editor"', 1)[1].split("</form>", 1)[0]
        self.assertIn('<option value="claude"', editor)
        self.assertNotIn('<option value="shell"', editor)

    def test_shell_is_refused_as_a_profile_harness(self):
        _, base = self.serve()
        status, body = self.post(base, "/profiles/set", name="sh",
                                 HARNESS="shell")
        self.assertEqual(status, 400)
        self.assertIn("Unknown assistant", body)
        self.assertEqual(os.listdir(self.profiles), [])

    def test_the_picker_offers_no_box_default_entry(self):
        _, base = self.serve()
        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertNotIn("default assistant", page)
        # The blank entry survives as a PROMPT, and cannot be submitted.
        self.assertIn('<option value="" disabled', page)
        self.assertIn('name="HARNESS" aria-label="Assistant" required', page)

    def test_a_profile_with_no_harness_is_refused(self):
        """The form asks; the handler is what actually enforces it, because
        a POST does not have to come from the form."""
        _, base = self.serve()
        status, body = self.post(base, "/profiles/set", name="empty",
                                 MODEL="opus")
        self.assertEqual(status, 400)
        self.assertIn("Pick an assistant", body)
        self.assertEqual(os.listdir(self.profiles), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
