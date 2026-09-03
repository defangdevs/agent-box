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
import os
import pathlib
import tempfile
import unittest

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
        "shadow": [],
    }
    state.update(overrides)
    return state


class ConnectCardCheckingTest(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()

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


if __name__ == "__main__":
    unittest.main()
