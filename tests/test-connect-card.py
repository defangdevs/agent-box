#!/usr/bin/env python3
"""render_connect_card()'s "checking" state (agent-box, connect cards).

connect_status() deliberately never blocks a render: a slow or unreachable
network must not hold the whole settings page hostage for a status pill,
so every card starts "checking" on a cold cache and reverts to it whenever
CONNECT_STATUS_TTL lapses between renders (settings-daemon.py's own
docstring on connect_status explains why). render_connect_card() used to
treat "checking" the same as a flow actually in flight (waiting, starting,
exchanging) and render no button at all — so a click during that window
landed on dead space: no form, no request, nothing in the console. Because
different CLIs answer their own status probe at different speeds, this
was not evenly distributed: a fast probe (gh) usually already had a real
button by the time an operator looked, while a slower one (claude, codex)
was still showing "Checking..." with nothing to press.

connect_start() does not read the status cache either way, so offering
the button during "checking" is safe. The one thing that must not
regress is a destructive flow (codex's --device-auth, which deletes the
stored credential as it starts): if the probe would have said "connected"
a moment later, skipping its confirmation on the strength of a guess would
silently drop a working credential. This pins both: a button appears, and
a destructive flow still confirms.

The subject is tests/golden/web/payloads/.../agent-box-settings, not
modules/src/settings-daemon.py, for the reason test-webhook-panel-state.py
gives: the daemon ships with the env-store library prepended, so the
source file alone does not import. The golden payload is that assembled
article, and the golden-snapshot check fails if it stops matching.
"""
import importlib.machinery
import importlib.util
import http.server
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")


def load_daemon():
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    # The two the daemon refuses to start without, and neither has
    # anything to do with the connect cards.
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = os.path.join(
        tempfile.gettempdir(), "agent-box-settings-under-test-connect.env")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test_connect", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


def base_state(**overrides):
    state = {
        "id": "claude", "label": "Claude Code", "note": "note text",
        "state": "idle", "detail": "", "url": None, "code": None,
        "error": None, "needs_code": True, "installed": True,
        "installable": True, "blocked": False, "destructive": False,
        "shadow": [], "removable": False,
    }
    state.update(overrides)
    return state


class ConnectCardCheckingTest(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()

    def test_capacity_notice_keeps_signed_in_state_and_is_visible(self):
        notice = "Signed in; session not started. Session limit reached."
        page = self.daemon.render_connect_card(base_state(state="connected", error=notice))
        self.assertIn(notice, page)
        self.assertIn("Signed in", page)
        self.assertIn(" open", page)

    def test_checking_non_destructive_offers_a_working_button(self):
        html = self.daemon.render_connect_card(
            base_state(state="checking", destructive=False))
        self.assertIn('<button type="submit"', html)
        self.assertIn('action="/settings/connect/start"', html)
        self.assertIn("Checking", html)
        self.assertNotIn("onsubmit=", html)

    def test_checking_destructive_still_confirms(self):
        html = self.daemon.render_connect_card(
            base_state(id="codex", state="checking", destructive=True))
        self.assertIn('<button type="submit"', html)
        self.assertIn('onsubmit="return confirm(', html)

    def test_checking_destructive_confirms_when_not_installed_too(self):
        # A harness is fetched on demand, so a missing binary says nothing
        # about a stored credential: "Install & sign in" on a destructive
        # flow mid-probe must still confirm.
        html = self.daemon.render_connect_card(
            base_state(id="codex", state="checking", destructive=True,
                       installed=False, installable=True))
        self.assertIn("Install & sign in", html)
        self.assertIn('onsubmit="return confirm(', html)

    def test_a_flow_actually_in_flight_still_offers_no_button(self):
        for state in ("waiting", "starting", "exchanging"):
            html = self.daemon.render_connect_card(
                base_state(state=state, url="https://example.com/sign-in"))
            self.assertNotIn(
                'action="/settings/connect/start"', html, "state=%s" % state)

    def test_blocked_still_offers_no_button(self):
        html = self.daemon.render_connect_card(
            base_state(state="checking", blocked=True))
        self.assertNotIn("<form", html)

    def test_idle_is_unaffected(self):
        html = self.daemon.render_connect_card(base_state(state="idle"))
        self.assertIn('<button type="submit"', html)
        self.assertNotIn("onsubmit=", html)

    def test_connected_card_offers_confirmed_native_logout(self):
        page = self.daemon.render_connect_card(
            base_state(state="connected", removable=True))
        self.assertIn('action="/settings/connect/logout"', page)
        self.assertIn("Sign out", page)
        self.assertIn("danger-btn", page)
        self.assertIn("Running sessions may need a restart", page)

    def test_idle_card_has_no_logout_action(self):
        page = self.daemon.render_connect_card(base_state(state="idle"))
        self.assertNotIn("/connect/logout", page)


class WhatsAppConnectTest(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.daemon.ENV_FILE = os.path.join(self.temp.name, "env")

    def serve(self):
        original_connect_flow = self.daemon.connect_flow
        self.daemon.connect_flow = lambda flow_id: (
            {"id": "whatsapp", "bin": None}
            if flow_id == "whatsapp" else original_connect_flow(flow_id)
        )
        self.daemon.Handler.log_message = lambda self, fmt, *args: None
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.daemon.Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/settings" % server.server_address[1]

    def post_start(self, base, phone):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        request = urllib.request.Request(
            base + "/connect/start",
            data=urllib.parse.urlencode({
                "flow": "whatsapp", "phone": phone, "profile": "",
            }).encode(),
            method="POST",
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(request) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode()

    def test_pairing_code_is_extracted_from_bridge_output(self):
        text = "WhatsApp pairing code: 7JVT986A\nEnter it on your phone."
        self.assertEqual("7JVT986A", self.daemon.connect_user_code(text, "whatsapp"))
        self.assertIsNone(self.daemon.connect_user_code("no code yet", "whatsapp"))

    def test_pairing_card_shows_phone_form_then_code(self):
        idle = self.daemon.render_connect_card(base_state(id="whatsapp", state="idle"))
        self.assertIn('name="phone"', idle)
        self.assertIn('type="tel"', idle)
        self.assertIn('inputmode="tel"', idle)
        self.assertIn('autocomplete="off"', idle)
        self.assertNotIn('autocomplete="tel"', idle)
        self.assertIn("starting with + and country code", idle)
        self.assertIn("Pair device", idle)
        waiting = self.daemon.render_connect_card(
            base_state(id="whatsapp", state="waiting", code="7JVT986A"))
        self.assertIn('data-copy="7JVT986A"', waiting)
        self.assertIn("Linked devices", waiting)
        self.assertNotIn('name="phone"', waiting)

    def test_national_phone_number_is_rejected_without_replacing_saved_number(self):
        with open(self.daemon.ENV_FILE, "w", encoding="utf-8") as handle:
            handle.write("LOCAL_WHATSAPP_PHONE=14155550100\n")
        status, body = self.post_start(self.serve(), "(415) 555-0123")
        self.assertEqual(400, status)
        self.assertIn("starting with + and country code", body)
        with open(self.daemon.ENV_FILE, encoding="utf-8") as handle:
            self.assertEqual("LOCAL_WHATSAPP_PHONE=14155550100\n", handle.read())

    def test_international_phone_number_is_saved_as_digits(self):
        self.daemon.whatsapp_profile = lambda profile: ""
        self.daemon.connect_start = lambda flow: {
            "state": "waiting", "error": None,
        }
        status, _ = self.post_start(self.serve(), "+1 (415) 555-0123")
        self.assertEqual(303, status)
        saved = self.daemon.as_dict(self.daemon.load(self.daemon.ENV_FILE))
        self.assertEqual("14155550123", saved["LOCAL_WHATSAPP_PHONE"])

    def test_blank_phone_keeps_the_saved_secret_fallback(self):
        with open(self.daemon.ENV_FILE, "w", encoding="utf-8") as handle:
            handle.write("LOCAL_WHATSAPP_PHONE=14155550100\n")
        self.daemon.whatsapp_profile = lambda profile: ""
        self.daemon.connect_start = lambda flow: {
            "state": "waiting", "error": None,
        }
        status, _ = self.post_start(self.serve(), "")
        self.assertEqual(303, status)
        saved = self.daemon.as_dict(self.daemon.load(self.daemon.ENV_FILE))
        self.assertEqual("14155550100", saved["LOCAL_WHATSAPP_PHONE"])

    def test_connected_status_requires_live_bridge(self):
        class Proc:
            returncode = 0
            stdout = '{"paired": true, "connected": false}'

        self.assertEqual((False, "device linked; bridge is connecting"),
                         self.daemon.parse_whatsapp_status(Proc()))
        Proc.stdout = '{"paired": true, "connected": true}'
        self.assertEqual((True, "linked device connected"),
                         self.daemon.parse_whatsapp_status(Proc()))

    def test_linked_but_reconnecting_whatsapp_can_be_unlinked(self):
        page = self.daemon.render_connect_card(base_state(
            id="whatsapp", state="idle", detail="device linked; bridge is connecting",
            removable=True))
        self.assertIn('action="/settings/connect/logout"', page)
        self.assertIn("Unlink", page)
        self.assertIn("queued messages and routing state", page)


class ConnectionLogoutTest(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.daemon.ENV_FILE = os.path.join(self.temp.name, "env")
        self.daemon._connect_status_cache.clear()
        self.daemon._connect_probe_began.clear()
        self.daemon._connect_invalidated_at.clear()

    def flow(self, name="claude"):
        flow = dict(next(f for f in self.daemon.CONNECT_DEFS if f["id"] == name))
        flow["bin"] = "/bin/" + flow["binary"]
        return flow

    def write_env(self, text):
        with open(self.daemon.ENV_FILE, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_builtin_logout_commands_match_each_native_cli(self):
        commands = {flow["id"]: flow["logout"] for flow in self.daemon.CONNECT_DEFS}
        self.assertEqual(commands, {
            "claude": ["auth", "logout"],
            "codex": ["logout"],
            "github": ["auth", "logout", "--hostname", "github.com"],
            "defang": ["logout", "--non-interactive"],
            "whatsapp": ["unlink"],
        })

    def test_logout_uses_native_command_without_environment_override(self):
        self.write_env("ANTHROPIC_API_KEY=secret\nKEEP=yes\n")
        flow = self.flow()
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs["env"]))
            return self.daemon.subprocess.CompletedProcess(argv, 0, "", "")

        self.daemon._connect_status_cache["claude"] = (1.0, (True, "account"))
        with mock.patch.object(self.daemon.subprocess, "run", side_effect=run):
            self.assertIsNone(self.daemon.connect_logout(flow))

        self.assertEqual([["/bin/claude", "auth", "logout"]], [c[0] for c in calls])
        self.assertNotIn("ANTHROPIC_API_KEY", calls[0][1])
        self.assertEqual(["KEEP"], self.daemon.read_keys())
        self.assertNotIn("claude", self.daemon._connect_status_cache)

    def test_failed_logout_keeps_secret_when_provider_still_connected(self):
        self.write_env("ANTHROPIC_API_KEY=secret\n")
        flow = self.flow()
        replies = [
            self.daemon.subprocess.CompletedProcess([], 1, "", "logout failed"),
            self.daemon.subprocess.CompletedProcess(
                [], 0, '{"loggedIn":true,"email":"still@example.com"}', ""),
        ]
        with mock.patch.object(self.daemon.subprocess, "run", side_effect=replies):
            error = self.daemon.connect_logout(flow)
        self.assertIn("may still be connected", error)
        self.assertEqual(["ANTHROPIC_API_KEY"], self.daemon.read_keys())

    def test_failed_logout_keeps_secret_when_status_is_ambiguous(self):
        self.write_env("ANTHROPIC_API_KEY=secret\n")
        flow = self.flow()
        replies = [
            self.daemon.subprocess.CompletedProcess([], 1, "", "logout failed"),
            self.daemon.subprocess.CompletedProcess([], 1, "", "network unavailable"),
        ]
        with mock.patch.object(self.daemon.subprocess, "run", side_effect=replies):
            error = self.daemon.connect_logout(flow)
        self.assertIn("may still be connected", error)
        self.assertEqual(["ANTHROPIC_API_KEY"], self.daemon.read_keys())

    def test_already_logged_out_is_idempotent_and_removes_override(self):
        self.write_env("ANTHROPIC_API_KEY=secret\n")
        flow = self.flow()
        replies = [
            self.daemon.subprocess.CompletedProcess([], 1, "", "not logged in"),
            self.daemon.subprocess.CompletedProcess(
                [], 0, '{"loggedIn":false,"authMethod":"none"}', ""),
        ]
        with mock.patch.object(self.daemon.subprocess, "run", side_effect=replies):
            self.assertIsNone(self.daemon.connect_logout(flow))
        self.assertEqual([], self.daemon.read_keys())

    def test_github_logout_names_every_stored_account(self):
        flow = self.flow("github")
        answer = self.daemon.subprocess.CompletedProcess([], 0, json.dumps({
            "hosts": {"github.com": [
                {"login": "one", "active": True},
                {"login": "two", "active": False},
            ]}
        }), "")
        with mock.patch.object(self.daemon, "connect_run", return_value=answer):
            commands = self.daemon.connect_logout_commands(flow)
        self.assertEqual(commands, [
            ["auth", "logout", "--hostname", "github.com", "--user", "one"],
            ["auth", "logout", "--hostname", "github.com", "--user", "two"],
        ])


class ConnectStepOrderTest(unittest.TestCase):
    """The order the wizard's steps are numbered in (the code first).

    The link used to be step 1 and the pairing code step 2, so a user on a
    phone opened the sign-in page, found it wanted a code, and switched
    back to copy it. The code and its copy button now come first for a
    flow that shows one, which only works if the numbering follows.

    Both backends render every one of these shapes: since issue #416 a
    card appears for a CLI the daemon can FETCH as well as one it ships,
    so AGENT_BOX_CONNECT_BINS is no longer the card list and neither
    backend is limited to the CLIs it happens to carry.
    """

    def setUp(self):
        self.daemon = load_daemon()

    def steps(self, state):
        """(number, text) per rendered step, tags stripped."""
        html = self.daemon.render_connect_step(state)
        found = re.findall(r"<strong>(\d+)\.</strong>(.*?)</(?:p|span)>", html)
        return [(n, re.sub(r"<[^>]+>", "", t).strip()) for n, t in found]

    def waiting(self, **overrides):
        return base_state(state="waiting", url="https://example.com/sign-in",
                          **overrides)

    def test_a_shown_code_is_step_one_and_the_link_follows(self):
        # codex and gh print the code in the pane: it is carried TO the
        # page, so it has to be on the clipboard before the link is taken.
        steps = self.steps(self.waiting(id="codex", code="56D0-6G7MP",
                                        needs_code=False))
        self.assertEqual(["1", "2"], [n for n, _ in steps])
        self.assertIn("Copy this code", steps[0][1])
        self.assertIn("56D0-6G7MP", steps[0][1])
        self.assertIn("Open the sign-in page", steps[1][1])
        self.assertIn("paste the code", steps[1][1])

    def test_the_copy_button_carries_the_shown_code(self):
        html = self.daemon.render_connect_step(
            self.waiting(id="codex", code="56D0-6G7MP", needs_code=False))
        self.assertIn('data-copy="56D0-6G7MP"', html)
        # ...and it sits in the code's own step, above the link.
        self.assertLess(html.index("data-copy="), html.index("<a href="))

    def test_a_paste_back_flow_says_to_bring_the_code_home(self):
        # claude shows no code in the pane but does want one back, so the
        # link must not read "approve the request" over a paste-back field.
        steps = self.steps(self.waiting(id="claude", code=None,
                                        needs_code=True))
        self.assertEqual(["1", "2"], [n for n, _ in steps])
        self.assertIn("Open the sign-in page", steps[0][1])
        self.assertIn("copy the code it shows", steps[0][1])
        self.assertNotIn("approve the request", steps[0][1])
        self.assertIn("Paste the code the page gives you back", steps[1][1])

    def test_a_flow_with_no_code_either_way_is_just_an_approval(self):
        # defang polls the auth server itself; nothing is typed anywhere.
        steps = self.steps(self.waiting(id="defang", code=None,
                                        needs_code=False))
        self.assertEqual(["1"], [n for n, _ in steps])
        self.assertIn("approve the request", steps[0][1])

    def test_all_three_steps_are_numbered_in_order_when_all_three_show(self):
        steps = self.steps(self.waiting(id="gh", code="EC7A-D4B8",
                                        needs_code=True))
        self.assertEqual(["1", "2", "3"], [n for n, _ in steps])
        self.assertIn("Copy this code", steps[0][1])
        self.assertIn("Open the sign-in page", steps[1][1])
        self.assertIn("Paste the code the page gives you back", steps[2][1])



class ReloginRestartTest(unittest.TestCase):
    """Which sessions a completed sign-in restarts (issue #751).

    Every session of a harness reads the one stored login at start, so an
    expired login breaks them all and a fresh sign-in fixes none until each
    restarts. Only the sessions that USE that login may be restarted: one
    whose env carries a key the harness prefers, or whose config dir is
    moved elsewhere, would come back with the same credential it had.
    """

    SIGNED_IN = 1000

    def setUp(self):
        self.daemon = load_daemon()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.daemon.ENV_FILE = os.path.join(self.tmp.name, "env")
        self.daemon.PROFILES_DIR = os.path.join(self.tmp.name, "profiles")
        self.daemon.CONNECT_DONE_DIR = os.path.join(self.tmp.name, "state")
        os.makedirs(self.daemon.PROFILES_DIR)
        os.makedirs(self.daemon.CONNECT_DONE_DIR)
        self.daemon._relogin_notices.clear()
        self.daemon._connect_probe_began.clear()
        self.flow = next(f for f in self.daemon.CONNECT_DEFS if f["id"] == "claude")
        self.killed = []
        self.sessions = {}
        self.created = {}

        class Proc:
            returncode = 0

        def tmux(*_args):
            proc = Proc()
            proc.stdout = "".join(
                "%s %d\n" % kv for kv in sorted(self.created.items()))
            return proc

        self.daemon.tmux = tmux
        self.daemon.read_sessions = lambda: dict(self.sessions)
        self.daemon.kill_session = self.killed.append
        self.daemon.ensure_harness_session = lambda *a, **k: None
        self.base = {}
        self.daemon.supervisor_environ = lambda: dict(self.base)

    def profile(self, name, text):
        path = os.path.join(self.daemon.PROFILES_DIR, name + ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("HARNESS=claude\n" + text)

    def add(self, name, created=SIGNED_IN - 60, **entry):
        self.sessions[name] = dict({"agent": "claude"}, **entry)
        if created is not None:
            self.created[name] = created

    def restart(self):
        return self.daemon.restart_login_sessions(self.flow, self.SIGNED_IN)

    def mark_done(self, when=SIGNED_IN):
        path = self.daemon.connect_done_path("claude")
        open(path, "w").close()
        os.utime(path, (when, when))

    def test_restarts_only_sessions_on_the_stored_login(self):
        self.profile("keyed", "ANTHROPIC_API_KEY=sk-ant-x\n")
        self.profile("moved", "CLAUDE_CONFIG_DIR=/elsewhere\n")
        self.profile("same", "CLAUDE_CONFIG_DIR=~/.claude\n")
        self.add("main")
        self.add("same", profile="same")
        self.add("keyed", profile="keyed")
        self.add("moved", profile="moved")
        self.add("parked", stopped=True)
        self.add("crashed", died=1)
        self.add("pending", created=None)
        self.add("fresh", created=self.SIGNED_IN + 5)
        self.add("cx", agent="codex")
        notice = self.restart()
        self.assertEqual(self.killed, ["main", "same"])
        self.assertIn("Restarted 2 sessions", notice)
        self.assertIn("keyed (uses ANTHROPIC_API_KEY)", notice)
        self.assertIn("moved (has its own CLAUDE_CONFIG_DIR)", notice)
        for name in ("parked", "crashed", "pending", "fresh", "cx"):
            self.assertNotIn(name, notice)

    def test_an_env_store_key_shadows_the_login_for_every_session(self):
        with open(self.daemon.ENV_FILE, "w", encoding="utf-8") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=tok\n")
        self.add("main")
        notice = self.restart()
        self.assertEqual(self.killed, [])
        self.assertIn("main (uses CLAUDE_CODE_OAUTH_TOKEN)", notice)

    def test_a_key_from_the_box_configuration_shadows_the_login(self):
        # users.<name>.environment / environmentFiles reach a session
        # through the supervisor, never through this daemon's own env.
        self.base = {"ANTHROPIC_API_KEY": "sk-ant-config"}
        self.add("main")
        notice = self.restart()
        self.assertEqual(self.killed, [])
        self.assertIn("main (uses ANTHROPIC_API_KEY)", notice)

    def test_the_config_dir_is_compared_with_the_sign_in_panes(self):
        # The sign-in pane inherits the supervisor's environment too, so a
        # config dir moved THERE is where the new login landed.
        self.base = {"CLAUDE_CONFIG_DIR": "/srv/claude"}
        self.profile("own", "CLAUDE_CONFIG_DIR=/elsewhere\n")
        self.add("main")
        self.add("own", profile="own")
        notice = self.restart()
        self.assertEqual(self.killed, ["main"])
        self.assertIn("own (has its own CLAUDE_CONFIG_DIR)", notice)

    def test_the_marker_is_claimed_once(self):
        # Several renders can see one sign-in finish; only the one that
        # removes the marker restarts anything.
        self.add("main")
        self.mark_done()
        self.daemon.connect_signed_in(self.flow)
        self.daemon.connect_signed_in(self.flow)
        self.assertEqual(self.killed, ["main"])

    def test_no_marker_restarts_nothing(self):
        self.add("main")
        self.daemon.connect_signed_in(self.flow)
        self.assertEqual(self.killed, [])

    def test_a_closed_pane_still_finishes_the_sign_in(self):
        # The pane lingers only CONNECT_LINGER seconds after the CLI exits,
        # and a device flow finishes on another device: a render that comes
        # later finds no pane, only the marker.
        self.add("main")
        self.mark_done()
        flow = dict(self.flow, bin="/bin/true")
        now = self.daemon.time.monotonic()
        self.daemon._connect_status_cache["claude"] = (now, (True, "x"))
        self.daemon._connect_probe_began["claude"] = self.SIGNED_IN + 1
        got = self.daemon.connect_state(flow, keys=set(), tmux_state=(True, set()))
        self.assertEqual(got["state"], "connected")
        self.assertEqual(self.killed, ["main"])
        self.assertIn("Restarted 1 session", got["notice"])
        self.assertFalse(os.path.exists(self.daemon.connect_done_path("claude")))

    def test_a_closed_pane_waits_for_a_fresh_probe(self):
        self.add("main")
        self.mark_done()
        flow = dict(self.flow, bin="/bin/true")
        self.daemon._connect_status_cache["claude"] = (0.0, (True, "x"))
        self.daemon._connect_probing.add("claude")  # no real probe thread
        got = self.daemon.connect_state(flow, keys=set(), tmux_state=(True, set()))
        self.assertEqual(got["state"], "exchanging")
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self.daemon.connect_done_path("claude")))

    def test_a_probe_older_than_the_marker_cannot_discard_it(self):
        # A fresh "not signed in" whose probe began before the sign-in
        # finished says nothing about the new login: re-probe rather than
        # throw the marker, and the restart with it, away.
        self.add("main")
        self.mark_done()
        flow = dict(self.flow, bin="/bin/true")
        now = self.daemon.time.monotonic()
        self.daemon._connect_status_cache["claude"] = (now, (False, ""))
        self.daemon._connect_probe_began["claude"] = self.SIGNED_IN - 1
        self.daemon._connect_probing.add("claude")  # no real probe thread
        got = self.daemon.connect_state(flow, keys=set(), tmux_state=(True, set()))
        self.assertEqual(got["state"], "exchanging")
        self.assertTrue(os.path.exists(self.daemon.connect_done_path("claude")))
        # ...while one that began after it may.
        self.daemon._connect_status_cache["claude"] = (now, (False, ""))
        self.daemon._connect_probe_began["claude"] = self.SIGNED_IN + 1
        got = self.daemon.connect_state(flow, keys=set(), tmux_state=(True, set()))
        self.assertEqual(got["state"], "idle")
        self.assertFalse(os.path.exists(self.daemon.connect_done_path("claude")))
        self.assertEqual(self.killed, [])

    def test_flows_that_reread_their_credential_restart_nothing(self):
        self.add("main", agent="github")
        gh = next(f for f in self.daemon.CONNECT_DEFS if f["id"] == "github")
        self.mark_done()
        self.daemon.connect_signed_in(gh)
        self.assertEqual(self.killed, [])

    def test_notice_shows_on_the_signed_in_card(self):
        self.add("main")
        notice = self.restart()
        self.assertEqual(self.daemon.relogin_notice("claude"), notice)
        page = self.daemon.render_connect_card(
            base_state(state="connected", notice=notice))
        self.assertIn("Restarted 1 session so it uses the new sign-in: main.", page)
        self.assertIn(" open", page)

if __name__ == "__main__":
    unittest.main()
