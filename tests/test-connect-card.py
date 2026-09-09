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
import re
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


if __name__ == "__main__":
    unittest.main()
