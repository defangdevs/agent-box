#!/usr/bin/env python3
"""The session CLI's WhatsApp allowlist is the bridge routing contract."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parent.parent
PAYLOAD = (REPO / "tests/golden/vm/payloads/agent-box-session/bin"
           / "agent-box-session")


class WhatsAppSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.registry = self.home / "sessions.json"
        self.registry.write_text(json.dumps({"version": 1, "sessions": {
            "claude1": {"agent": "claude"},
            "codex1": {"agent": "codex", "stopped": True},
            "shell1": {"agent": "shell"},
        }}))
        payload = PAYLOAD.read_text()
        body = payload.split("set -eu\n", 1)[1]
        self.cli = self.home / "session-cli.sh"
        self.cli.write_text("set -eu\n" + body)
        self.cli.chmod(0o700)
        self.profile = self.home / "agent-box-profile"
        self.profile.write_text("#!/bin/sh\ncase \"$1\" in\n"
                                "  default) printf '%s\\n' phone-agent ;;\n"
                                "  launch) printf '%s\\n' '{\"harness\":\"claude\",\"args\":[],\"warnings\":[]}' ;;\n"
                                "esac\n")
        self.profile.chmod(0o700)
        self.capacity = self.home / "capacity"
        self.capacity_log = self.home / "capacity.log"
        self.capacity.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$CAPACITY_LOG\"\n"
            "if [ \"${CAPACITY_FAIL:-0}\" = 1 ]; then\n"
            "  echo 'Session limit reached' >&2\n"
            "  exit 75\n"
            "fi\n")
        self.capacity.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home),
                        AGENT_BOX_AGENTS="claude codex shell",
                        AGENT_BOX_DEFAULT_AGENT="claude",
                        AGENT_BOX_SESSIONS_FILE=str(self.registry),
                        AGENT_BOX_PROFILE_BIN=str(self.profile),
                        AGENT_BOX_CAPACITY_BIN=str(self.capacity),
                        CAPACITY_LOG=str(self.capacity_log),
                        REGISTRY_FLOCK=shutil.which("flock") or "")

    def run_cli(self, *args):
        return subprocess.run(["bash", str(self.cli), "whatsapp", *args],
                              env=self.env, capture_output=True, text=True,
                              timeout=10)

    def test_one_selected_agent_session_is_listed(self):
        candidates = json.loads(self.run_cli("candidates").stdout)
        self.assertEqual(["claude1", "codex1"], [entry["name"] for entry in candidates])
        self.assertFalse(any(entry["selected"] for entry in candidates))
        self.assertEqual(0, self.run_cli("claude1", "on").returncode)
        self.assertEqual(0, self.run_cli("codex1", "on").returncode)
        self.assertEqual("on", self.run_cli("codex1", "status").stdout.strip())
        self.assertEqual([
            {"name": "codex1", "harness": "codex", "stopped": True},
        ], json.loads(self.run_cli("ls").stdout))
        self.assertEqual("off", self.run_cli("claude1", "status").stdout.strip())
        self.assertEqual(0, self.run_cli("clear").returncode)
        self.assertEqual([], json.loads(self.run_cli("ls").stdout))

    def test_spawn_uses_the_selected_or_default_profile(self):
        self.assertEqual(0, self.run_cli("codex1", "on").returncode)
        started = self.run_cli("spawn", "default")
        self.assertEqual(0, started.returncode, started.stderr)
        self.assertEqual("phone-agent", json.loads(started.stdout)["name"])
        sessions = json.loads(self.registry.read_text())["sessions"]
        self.assertTrue(sessions["phone-agent"]["whatsapp"])
        self.assertFalse(sessions["claude1"].get("whatsapp", False))
        self.assertFalse(sessions["codex1"].get("whatsapp", False))

    def test_shell_session_cannot_be_enabled(self):
        result = self.run_cli("shell1", "on")
        self.assertEqual(2, result.returncode)
        self.assertIn("not supported", result.stderr)
        self.assertFalse(json.loads(self.registry.read_text())
                         ["sessions"]["shell1"].get("whatsapp", False))

    def test_activate_revives_the_selected_stopped_session(self):
        self.assertEqual(0, self.run_cli("select", "codex1").returncode)
        activated = self.run_cli("activate", "codex1")
        self.assertEqual(0, activated.returncode, activated.stderr)
        self.assertEqual({"name": "codex1", "harness": "codex",
                          "activated": True}, json.loads(activated.stdout))
        session = json.loads(self.registry.read_text())["sessions"]["codex1"]
        self.assertNotIn("stopped", session)
        self.assertIn("check", self.capacity_log.read_text())

    def test_activate_is_idempotent_for_a_running_target(self):
        self.assertEqual(0, self.run_cli("select", "claude1").returncode)
        activated = self.run_cli("activate", "claude1")
        self.assertEqual(0, activated.returncode, activated.stderr)
        self.assertFalse(json.loads(activated.stdout)["activated"])
        self.assertFalse(self.capacity_log.exists())

    def test_activate_refuses_an_unselected_or_unadmitted_target(self):
        unselected = self.run_cli("activate", "codex1")
        self.assertEqual(2, unselected.returncode)
        self.assertIn("not the selected WhatsApp target", unselected.stderr)

        self.assertEqual(0, self.run_cli("select", "codex1").returncode)
        self.env["CAPACITY_FAIL"] = "1"
        refused = self.run_cli("activate", "codex1")
        self.assertEqual(75, refused.returncode)
        self.assertIn("Session limit reached", refused.stderr)
        self.assertTrue(json.loads(self.registry.read_text())
                        ["sessions"]["codex1"]["stopped"])

    def test_wake_revives_any_agent_session_without_selecting_it(self):
        woke = self.run_cli("wake", "codex1")
        self.assertEqual(0, woke.returncode, woke.stderr)
        self.assertEqual({"name": "codex1", "harness": "codex", "activated": True},
                         json.loads(woke.stdout))
        session = json.loads(self.registry.read_text())["sessions"]["codex1"]
        self.assertNotIn("stopped", session)
        self.assertFalse(session.get("whatsapp", False))
        self.assertIn("check", self.capacity_log.read_text())
        shell = self.run_cli("wake", "shell1")
        self.assertEqual(2, shell.returncode)
        self.assertIn("not supported", shell.stderr)

    def test_wake_respects_the_capacity_gate(self):
        self.env["CAPACITY_FAIL"] = "1"
        refused = self.run_cli("wake", "codex1")
        self.assertEqual(75, refused.returncode)
        self.assertTrue(json.loads(self.registry.read_text())["sessions"]["codex1"]["stopped"])

    def test_route_start_survives_in_the_registry_and_is_listed(self):
        nonce = "0123456789abcdef0123456789abcdef"
        added = subprocess.run(["bash", str(self.cli), "add", "wa-x", "--harness", "claude",
                                "--origin", "webhook", "--prompt", "hi", "--route-start", nonce],
                               env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, added.returncode, added.stderr)
        entry = json.loads(self.registry.read_text())["sessions"]["wa-x"]
        self.assertEqual(entry["routeStart"], nonce)
        # The supervisor clears the kickoff prompt at the first spawn; the mark stays.
        registry = json.loads(self.registry.read_text())
        registry["sessions"]["wa-x"]["initialPrompt"] = None
        self.registry.write_text(json.dumps(registry))
        listed = {i["name"]: i for i in json.loads(self.run_cli("candidates").stdout)}
        self.assertEqual(listed["wa-x"]["start"], nonce)
        self.assertIsNone(listed["claude1"]["start"])
        for bad in ("XYZ", "0123", "0123456789ABCDEF0123456789ABCDEF"):
            refused = subprocess.run(["bash", str(self.cli), "add", "wa-y", "--harness", "claude",
                                      "--route-start", bad],
                                     env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(2, refused.returncode, bad)

    def test_interrupted_route_start_is_adopted_through_the_real_session_cli(self):
        """End to end with the generated session CLI and a real registry: the
        route dies right after `add`, the first spawn clears the kickoff
        prompt, and the replay adopts that session instead of starting another."""
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("wa_cli", REPO / "modules/src/whatsapp-cli.py")
        wa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wa)
        wa.HOME = self.home
        wa.STATE = self.home / ".local/state/local-whatsapp"
        wa.ROUTE_STATE = self.home / ".local/state/agent-box/router/state.json"
        wa.ROUTE_BACKEND_CACHE = wa.ROUTE_STATE.with_name("backend.json")
        sess = self.home / "sess"
        sess.write_text("#!/bin/sh\nexec bash %s \"$@\"\n" % self.cli)
        sess.chmod(0o700)
        hook = self.home / "hook"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o700)
        wa.SESSION_BIN, wa.WEBHOOK_BIN = str(sess), str(hook)
        wa.profile = lambda value=None: None

        class Router(object):
            BACKENDS, DEFAULT_ORDER = {}, ()

            def __init__(self, topic):
                self.topic, self.calls = topic, 0

            def route(self, message, sessions, current, backend=None):
                self.calls += 1
                return {"action": "NEW", "session": None, "links": [], "topic": self.topic, "reason": "llm"}

        request = {"channel": "whatsapp", "message": {"id": "M1", "text": "VAT in Greece?", "ts": 1},
                   "reply": "reply M1", "ack": "ack M1", "recent": []}
        real = wa.run_json

        def dies_after_add(argv, env=None, timeout=60):
            registry = json.loads(self.registry.read_text())
            if argv[1:3] == ["whatsapp", "candidates"] and "wa-greece" in registry["sessions"]:
                raise KeyboardInterrupt("bridge killed right after add")
            return real(argv, env, timeout)
        env = dict(self.env, AGENT_BOX_ROUTER_BACKEND="none", USER="agent")
        with mock.patch.dict(wa.os.environ, env, clear=True):
            with mock.patch.object(wa, "run_json", side_effect=dies_after_add):
                with self.assertRaises(KeyboardInterrupt):
                    wa.route_request(request, Router("Greece"))
            # The first spawn clears the kickoff prompt (supervisor mark_started).
            registry = json.loads(self.registry.read_text())
            entry = registry["sessions"]["wa-greece"]
            self.assertTrue(entry["routeStart"])
            entry["initialPrompt"], entry["hasRun"] = None, True
            self.registry.write_text(json.dumps(registry))
            again = Router("something else")
            out = wa.route_request(request, again)
        self.assertEqual((out["session"], out["reason"]), ("wa-greece", "replay"))
        self.assertEqual(again.calls, 0)
        names = [n for n in json.loads(self.registry.read_text())["sessions"] if n.startswith("wa-")]
        self.assertEqual(names, ["wa-greece"])      # no second session
        state = json.loads(wa.ROUTE_STATE.read_text())
        self.assertEqual(state["decisions"]["M1"]["id"], entry["boxSessionId"])
        self.assertEqual([m["id"] for m in state["sessions"]["wa-greece"]["messages"]], ["M1"])


if __name__ == "__main__":
    unittest.main()
