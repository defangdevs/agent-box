#!/usr/bin/env python3
"""connect_start()'s INSTALL half (agent-box, connect cards, issue #544).

Two faults compounded here, and either one alone is enough to make the
"Install & sign in" button look like it does nothing at all.

The first is that nothing could find nix. CONNECT_NIX_BIN used to fall
back to the bare name "nix" and leave the pane's shell to resolve it. The
NixOS module pins a store path, so it never noticed; a NATIVE box cannot
pin one (bin/agentbox says why where it declines to set AGENT_BOX_NIX_BIN)
and its pane PATH is the tmux server's:

    ~/.nix-profile/bin:/nix/var/nix/profiles/agent-box/bin:/usr/local/bin:
    /usr/bin:/bin:/usr/sbin:/sbin

A multi-user install puts nix in /nix/var/nix/profiles/default/bin, which
is on none of that. An interactive shell finds it anyway because
/etc/profile's nix-daemon.sh prepends that directory — the pane runs a
non-interactive `sh -c`, which sources nothing. So every card that had to
fetch its CLI first ran "nix: not found": claude, codex and defang alike,
on every native box.

The second is that the failure was invisible. The install was chained with
`|| exit 1`, and `exit` leaves the WHOLE shell — so a failed install
skipped both the "[agent-box] exit=" marker and the CONNECT_LINGER sleep
that keeps the last screen readable. tmux dropped the session within
milliseconds, connect_state() found no pane and fell back to "idle", and
the card rendered "Not installed" with error None. Every install failure,
whatever its cause, was indistinguishable from the button doing nothing.

The subject is tests/golden/web/payloads/.../agent-box-settings rather
than modules/src/settings-daemon.py, for the reason test-connect-card.py
gives: the daemon ships with the env-store library prepended, so the
source file alone does not import.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import stat
import tempfile
import unittest
import unittest.mock

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")

# The PATH a sign-in pane actually gets on a native box, measured off a
# deployed one's tmux server. Nothing here carries nix.
NATIVE_PANE_PATH = (
    "/home/agent/.nix-profile/bin:/nix/var/nix/profiles/agent-box/bin:"
    "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)


def load_daemon(env=None):
    saved = dict(os.environ)
    os.environ.clear()
    # Deliberately NOT the caller's PATH: these tests are about what is
    # findable, so an inherited PATH carrying nix would make the native
    # regression pass for the wrong reason.
    os.environ["PATH"] = "/nonexistent-for-tests"
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = os.path.join(
        tempfile.gettempdir(), "agent-box-settings-under-test-install.env")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ.update(env or {})
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test_install", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


def fake_nix(directory):
    """An executable named nix, so os.access(X_OK) has something to find."""
    path = pathlib.Path(directory) / "nix"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class HermeticHome(unittest.TestCase):
    """A HOME of its own, held for the whole test.

    connect_flows() resolves ~/.nix-profile/bin/<cli> at CALL time, so a
    developer box with claude already installed would report the card as
    installed and skip the install branch these tests are about.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        # PATH as well as HOME, and for the whole test rather than only
        # the load: connect_nix_bin() calls shutil.which("nix") at CALL
        # time, so a host nix on the developer's PATH would satisfy the
        # tests that assert nothing is findable.
        patch = unittest.mock.patch.dict(
            os.environ, {"HOME": self.home, "PATH": "/nonexistent-for-tests"})
        patch.start()
        self.addCleanup(patch.stop)
        self.daemon = load_daemon({"HOME": self.home})


class ConnectNixBinTest(HermeticHome):

    def test_a_pinned_nix_wins(self):
        """The NixOS module's store path beats every other candidate."""
        pinned = fake_nix(self.tmp.name)
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        daemon = load_daemon({"AGENT_BOX_NIX_BIN": pinned,
                              "HOME": self.home})
        daemon.CONNECT_NIX_FALLBACKS = (fake_nix(other),)
        self.assertEqual(daemon.connect_nix_bin(other), pinned)

    def test_a_stale_pin_falls_through_to_a_real_nix(self):
        """A pin is preferred, not trusted: the store path may be gone.

        Returning it unchecked pinned the card to a script that could not
        run, while a perfectly good nix sat in the default profile.
        """
        gone = os.path.join(self.tmp.name, "collected", "bin", "nix")
        real = fake_nix(self.tmp.name)
        daemon = load_daemon({"AGENT_BOX_NIX_BIN": gone, "HOME": self.home})
        daemon.CONNECT_NIX_FALLBACKS = (real,)
        self.assertEqual(daemon.connect_nix_bin(NATIVE_PANE_PATH), real)

    def test_a_stale_pin_with_no_other_nix_is_reported(self):
        """...and when there is no other nix, it is named as missing."""
        gone = os.path.join(self.tmp.name, "collected", "bin", "nix")
        daemon = load_daemon({"AGENT_BOX_NIX_BIN": gone, "HOME": self.home})
        daemon.CONNECT_NIX_FALLBACKS = (
            os.path.join(self.tmp.name, "absent", "nix"),)
        self.assertIsNone(daemon.connect_nix_bin(NATIVE_PANE_PATH))

    def test_the_pane_path_is_searched(self):
        """A single-user install lands in the profile the pane PATH names."""
        nix = fake_nix(self.tmp.name)
        self.assertEqual(
            self.daemon.connect_nix_bin(self.tmp.name + ":/usr/bin"), nix)

    def test_native_pane_path_still_resolves_nix(self):
        """The regression: nix is off the pane PATH, in the default profile.

        Before the fix this returned the bare name "nix", which the pane's
        shell then failed to resolve — the install never ran.
        """
        nix = fake_nix(self.tmp.name)
        self.daemon.CONNECT_NIX_FALLBACKS = (nix,)
        found = self.daemon.connect_nix_bin(NATIVE_PANE_PATH)
        self.assertEqual(found, nix)
        self.assertTrue(os.path.isabs(found), "a bare name is not resolvable")

    def test_no_nix_anywhere_is_reported_not_guessed(self):
        """None, so the caller can say so instead of writing a dead script."""
        self.daemon.CONNECT_NIX_FALLBACKS = (
            os.path.join(self.tmp.name, "absent", "nix"),)
        self.assertIsNone(self.daemon.connect_nix_bin(NATIVE_PANE_PATH))


class ConnectInstallScriptTest(HermeticHome):
    """What connect_start() actually hands tmux for an uninstalled CLI."""

    def setUp(self):
        super().setUp()
        self.nix = fake_nix(self.tmp.name)
        self.daemon.CONNECT_NIX_FALLBACKS = (self.nix,)
        self.daemon.CONNECT_NIXPKGS = "https://example.invalid/nixpkgs.tar.xz"
        self.daemon.tmux_server_up = lambda: True
        self.daemon.live_sessions = lambda: set()
        self.daemon.connect_server_path = lambda: NATIVE_PANE_PATH
        self.daemon.connect_state = lambda flow, **kw: {
            "state": "idle", "error": None}
        self.calls = []
        self.daemon.tmux = lambda *args: self.calls.append(args)

    def flow(self, flow_id="claude"):
        found = [f for f in self.daemon.connect_flows() if f["id"] == flow_id]
        self.assertTrue(found, "no %s card" % flow_id)
        self.assertIsNone(found[0]["bin"],
                          "this test needs an UNinstalled CLI")
        return found[0]

    def script(self, flow_id="claude"):
        self.daemon.connect_start(self.flow(flow_id))
        self.assertEqual(len(self.calls), 1, "expected one new-session")
        return self.calls[0][-1]

    def test_the_install_runs_the_resolved_nix_not_a_bare_name(self):
        script = self.script()
        self.assertIn(self.nix + " profile add", script)

    def test_a_failed_install_still_reaches_the_exit_marker(self):
        """The whole point: no `exit` between the install and the marker.

        `|| exit 1` left the shell before printf and sleep, so the pane
        vanished with nothing on it and the card could never say "failed".
        """
        script = self.script()
        self.assertNotIn("exit 1", script)
        install, _, rest = script.partition(" && ")
        self.assertIn("profile add", install)
        self.assertIn("[agent-box] exit=", rest)
        self.assertIn("sleep", rest)
        self.assertLess(rest.index("[agent-box] exit="), rest.index("sleep"))

    def test_no_nix_fails_the_card_with_something_readable(self):
        self.daemon.CONNECT_NIX_FALLBACKS = (
            os.path.join(self.tmp.name, "absent", "nix"),)
        state = self.daemon.connect_start(self.flow())
        self.assertEqual(state["state"], "failed")
        self.assertIn("no nix", state["error"])
        self.assertEqual(self.calls, [], "must not start a pane it cannot use")


if __name__ == "__main__":
    unittest.main(verbosity=2)
