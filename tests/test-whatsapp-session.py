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
        self.capacity.write_text("#!/bin/sh\nexit 0\n")
        self.capacity.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home),
                        AGENT_BOX_AGENTS="claude codex shell",
                        AGENT_BOX_DEFAULT_AGENT="claude",
                        AGENT_BOX_SESSIONS_FILE=str(self.registry),
                        AGENT_BOX_PROFILE_BIN=str(self.profile),
                        AGENT_BOX_CAPACITY_BIN=str(self.capacity),
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


if __name__ == "__main__":
    unittest.main()
