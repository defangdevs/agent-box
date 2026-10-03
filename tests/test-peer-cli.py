#!/usr/bin/env python3
"""Focused protocol tests for agent-box-peer (issue #699)."""

import argparse
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
PAYLOAD = Path(os.environ.get("AGENT_BOX_PEER_CLI", REPO / "modules/src/peer-cli.py"))
SPEC = importlib.util.spec_from_file_location("peer_cli", PAYLOAD)
peer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(peer)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PeerCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = os.environ.copy()
        self.addCleanup(self.restore_env)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.old)

    def box(self, name, endpoint):
        os.environ["AGENT_BOX_PEER_STATE_DIR"] = str(self.root / name / "peers")
        os.environ["LOCAL_WEBHOOK_STATE_DIR"] = str(self.root / name / "local-webhook")
        os.environ["AGENT_BOX_WEBHOOK_URL"] = endpoint

    @staticmethod
    def capture(fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            fn(*args)
        return out.getvalue().strip()

    def test_pairing_installs_generic_receiver_and_sends_signed_message(self):
        self.box("a", "https://a.example/claude/webhook")
        invitation = self.capture(peer.cmd_invite,
                                  argparse.Namespace(label="B", expires_hours=24))

        self.box("b", "https://b.example/other/webhook")
        with mock.patch("sys.stdin", io.StringIO(invitation)):
            response = self.capture(peer.cmd_accept, argparse.Namespace(label="A"))
        b_sources = json.loads((self.root / "b/local-webhook/sources.json").read_text())
        source_name, source = next(iter(b_sources["sources"].items()))
        self.assertTrue(source["agentBoxPeer"])
        self.assertEqual(source["format"], "generic")
        self.assertEqual(source["keyPath"], "inbox")
        self.assertEqual(source["senderPath"], "from")

        self.box("a", "https://a.example/claude/webhook")
        with mock.patch("sys.stdin", io.StringIO(response)):
            self.capture(peer.cmd_confirm, argparse.Namespace())

        sent = []

        def deliver(request, timeout):
            sent.append((request, timeout))
            return Response()

        with mock.patch("urllib.request.urlopen", deliver):
            result = self.capture(peer.cmd_send, argparse.Namespace(
                label="B", inbox="ops", message="hello from A", from_name="claude",
                timeout=5))
        self.assertIn("Accepted by B", result)
        request, timeout = sent[0]
        self.assertEqual(timeout, 5)
        self.assertEqual(request.full_url, "https://b.example/other/webhook/" + source_name)
        raw = request.data
        expected = "sha256=" + hmac.new(
            Path(source["secretFile"]).read_text().strip().encode("utf-8"), raw,
            hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-agent-box-signature-256"), expected)
        payload = json.loads(raw)
        self.assertEqual(payload["inbox"], "ops")
        self.assertEqual(payload["body"], "hello from A")
        self.assertEqual(payload["from"], "claude")

        with mock.patch.dict(os.environ, {"USER": "agent"}):
            who = self.capture(peer.cmd_whoami, argparse.Namespace())
            self.assertIn("address\tagent@a.example", who)
            self.assertIn("inbox\tB\t" + peer.safe_name(
                next(iter(peer.load_peers()["peers"]))) + ":default", who)
            with mock.patch("urllib.request.urlopen", deliver):
                self.capture(peer.cmd_send, argparse.Namespace(
                    label="B", inbox="ops", message="x", from_name="", timeout=5))
            self.assertEqual(json.loads(sent[-1][0].data)["from"], "agent@a.example")

    def test_revoke_leaves_a_manual_collision_alone(self):
        self.box("a", "https://a.example/claude/webhook")
        invitation = self.capture(peer.cmd_invite,
                                  argparse.Namespace(label="B", expires_hours=24))
        self.box("b", "https://b.example/other/webhook")
        with mock.patch("sys.stdin", io.StringIO(invitation)):
            self.capture(peer.cmd_accept, argparse.Namespace(label="A"))
        sources_path = self.root / "b/local-webhook/sources.json"
        config = json.loads(sources_path.read_text())
        name = next(iter(config["sources"]))
        config["sources"][name]["agentBoxPeer"] = False
        sources_path.write_text(json.dumps(config))
        self.capture(peer.cmd_revoke, argparse.Namespace(label="A"))
        self.assertIn(name, json.loads(sources_path.read_text())["sources"])
        self.assertTrue(Path(config["sources"][name]["secretFile"]).exists())


if __name__ == "__main__":
    unittest.main()
