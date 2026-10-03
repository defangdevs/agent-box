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
        self.env = dict(os.environ, HOME=str(self.home),
                        AGENT_BOX_AGENTS="claude codex shell",
                        AGENT_BOX_DEFAULT_AGENT="claude",
                        AGENT_BOX_SESSIONS_FILE=str(self.registry),
                        REGISTRY_FLOCK=shutil.which("flock") or "")

    def run_cli(self, *args):
        return subprocess.run(["bash", str(self.cli), "whatsapp", *args],
                              env=self.env, capture_output=True, text=True,
                              timeout=10)

    def test_only_enabled_agent_sessions_are_listed(self):
        self.assertEqual([], json.loads(self.run_cli("ls").stdout))
        self.assertEqual(0, self.run_cli("claude1", "on").returncode)
        self.assertEqual(0, self.run_cli("codex1", "on").returncode)
        self.assertEqual("on", self.run_cli("codex1", "status").stdout.strip())
        self.assertEqual([
            {"name": "claude1", "harness": "claude", "stopped": False},
            {"name": "codex1", "harness": "codex", "stopped": True},
        ], json.loads(self.run_cli("ls").stdout))
        self.assertEqual(0, self.run_cli("claude1", "off").returncode)
        self.assertEqual([{"name": "codex1", "harness": "codex", "stopped": True}],
                         json.loads(self.run_cli("ls").stdout))

    def test_shell_session_cannot_be_enabled(self):
        result = self.run_cli("shell1", "on")
        self.assertEqual(2, result.returncode)
        self.assertIn("not supported", result.stderr)
        self.assertFalse(json.loads(self.registry.read_text())
                         ["sessions"]["shell1"].get("whatsapp", False))


if __name__ == "__main__":
    unittest.main()
