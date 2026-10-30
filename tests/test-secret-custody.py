#!/usr/bin/env python3
"""Unit tests for the secret custodian (issue #922, PR 1).

The broker and CLI are composed the way the generated module composes them
(library text, then program text) and run as real processes over a real Unix
socket. These tests prove the protocol and the store; the OS boundary itself
(root ownership, other users) needs the VM test, and is NOT claimed here.
"""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "modules" / "src"
LIB = (SRC / "lib" / "secret-protocol.py").read_text()
BROKER = LIB + "\n" + (SRC / "secret-broker.py").read_text()
CLI = LIB + "\n" + (SRC / "secret-cli.py").read_text()
CANARY = "canary-7f3a9-not-a-real-secret"

ns = {"__name__": "secret_lib"}
exec(compile(LIB, "secret-protocol.py", "exec"), ns)


class Broker:
    def __init__(self, tmp, uid=None, user="agent"):
        self.sock = os.path.join(tmp, "b.sock")
        self.root = os.path.join(tmp, "store")
        self.uid = os.getuid() if uid is None else uid
        script = os.path.join(tmp, "broker.py")
        Path(script).write_text(BROKER)
        self.proc = subprocess.Popen(
            [sys.executable, "-I", script, "--user", user, "--uid", str(self.uid),
             "--store-root", self.root, "--socket", self.sock],
            stderr=subprocess.PIPE)
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(self.proc.stderr.read().decode())

    def ask(self, req):
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(self.sock)
        ns["send_frame"](conn, req)
        out = ns["recv_frame"](conn)
        conn.close()
        return out

    def stop(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stderr.close()


class Custody(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.broker = Broker(self.tmp)
        self.addCleanup(self.broker.stop)

    def test_set_list_remove_never_returns_the_value(self):
        seen = [self.broker.ask({"op": "set", "name": "TOKEN", "value": CANARY}),
                self.broker.ask({"op": "list"})]
        seen.append(self.broker.ask({"op": "remove", "name": "TOKEN"}))
        seen.append(self.broker.ask({"op": "list"}))
        self.assertNotIn(CANARY, repr(seen))
        self.assertEqual(seen[1]["secrets"],
                         [{"name": "TOKEN", "version": 1, "configured": True}])
        self.assertEqual(seen[3]["secrets"],
                         [{"name": "TOKEN", "version": 2, "configured": False}])

    def test_records_are_private_files(self):
        self.broker.ask({"op": "set", "name": "TOKEN", "value": CANARY})
        rec = Path(self.broker.root) / "agent" / "TOKEN.rec"
        self.assertEqual(rec.stat().st_mode & 0o777, 0o600)
        self.assertEqual((Path(self.broker.root) / "agent").stat().st_mode & 0o777, 0o700)

    def test_schema_refusals(self):
        for req in ({"op": "get", "name": "TOKEN"},
                    {"op": "list", "uid": 0},
                    {"op": "set", "name": "../x", "value": "v"},
                    {"op": "set", "name": "A", "value": "a\0b"},
                    {"op": "set", "name": "A", "value": ""},
                    {"op": "set", "name": "A", "value": "x" * 40000},
                    {"op": "set", "name": "A", "value": "v", "path": "/etc"},
                    {"op": "remove", "name": "MISSING"}):
            self.assertFalse(self.broker.ask(req)["ok"], req)

    def test_oversize_frame_is_refused_without_allocating(self):
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(self.broker.sock)
        conn.sendall(struct.pack(">I", 0x7FFFFFFF))
        conn.settimeout(5)
        reply = ns["recv_frame"](conn)
        self.assertFalse(reply["ok"])
        conn.close()
        self.assertTrue(self.broker.ask({"op": "list"})["ok"])

    def test_wrong_peer_uid_is_forbidden(self):
        other = Broker(tempfile.mkdtemp(), uid=os.getuid() + 1)
        self.addCleanup(other.stop)
        # The refusal used to race the client's send: the broker closed with
        # the request unread, so some attempts saw EPIPE instead of this.
        for _ in range(30):
            self.assertEqual(other.ask({"op": "list"}), {"ok": False, "error": "forbidden"})

    def test_peer_that_vanishes_does_not_stop_the_broker(self):
        for send in (b"", b"\x00\x00", struct.pack(">I", 10) + b"abc"):
            conn = socket.socket(socket.AF_UNIX)
            conn.connect(self.broker.sock)
            conn.sendall(send)
            conn.close()
            self.assertIsNone(self.broker.proc.poll(), "broker exited")
            self.assertTrue(self.broker.ask({"op": "list"})["ok"])

    def test_namespace_bound_to_identity(self):
        self.broker.ask({"op": "set", "name": "A", "value": "v"})
        with self.assertRaises(ns["SecretError"]):
            ns["Store"](self.broker.root, "agent", "agent", os.getuid() + 1)

    def test_symlinked_record_is_not_followed(self):
        self.broker.ask({"op": "set", "name": "A", "value": "v"})
        victim = Path(self.tmp) / "victim"
        victim.write_text("untouched")
        rec = Path(self.broker.root) / "agent" / "A.rec"
        rec.unlink()
        rec.symlink_to(victim)
        self.assertFalse(self.broker.ask({"op": "set", "name": "A", "value": "w"})["ok"] and
                         victim.read_text() != "untouched")
        self.assertEqual(victim.read_text(), "untouched")

    def test_cli_round_trip_and_value_stays_off_argv(self):
        cli = Path(self.tmp) / "cli.py"
        cli.write_text(CLI)

        def run(*args, stdin=None):
            return subprocess.run([sys.executable, "-I", str(cli), "--socket",
                                   self.broker.sock, *args], input=stdin,
                                  capture_output=True, text=True)
        self.assertEqual(run("set", "TOKEN", "--stdin", stdin=CANARY + "\n").returncode, 0)
        listed = run("list")
        self.assertIn("TOKEN\tv1\tconfigured", listed.stdout)
        self.assertNotIn(CANARY, listed.stdout + listed.stderr)
        self.assertNotEqual(run("set", "TOKEN", CANARY).returncode, 0)
        self.assertEqual(run("remove", "TOKEN").returncode, 0)


if __name__ == "__main__":
    unittest.main()
