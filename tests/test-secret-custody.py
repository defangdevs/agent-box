#!/usr/bin/env python3
"""Unit tests for the secret custodian (issue #922).

The broker, worker, and CLI are composed the way the generated module
composes them (library text, then program text) and run as real processes
over a real Unix socket. These tests prove the protocol, the store, the
encryption, and the job engine (with DirectLauncher, which runs a job as
THIS process's own uid). The OS boundary itself - a systemd transient unit
really dropping to another uid, root really being unreadable by the agent
user, two Linux users - needs the VM test (tests/secrets.nix) and is NOT
claimed here.

Needs a python3 that has the `cryptography` package (the broker's pinned
interpreter always does; a bare system python3 usually does not). Point
AGENT_BOX_TEST_PYBIN at one if `sys.executable` lacks it, e.g.:
  nix build --no-link --print-out-paths \
    '.#legacyPackages.$(nix eval --raw --impure --expr builtins.currentSystem).python3.withPackages(ps: [ps.cryptography])'
"""
import json
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
PROTOCOL = (SRC / "lib" / "secret-protocol.py").read_text()
JOBS = (SRC / "lib" / "secret-jobs.py").read_text()
LIB = PROTOCOL + "\n" + JOBS
BROKER = LIB + "\n" + (SRC / "secret-broker.py").read_text()
CLI = PROTOCOL + "\n" + (SRC / "secret-cli.py").read_text()
# secret-worker.py is standalone (see its own header) and runs under a bare
# python3, deliberately without `cryptography` - do not prepend PROTOCOL here.
WORKER = (SRC / "secret-worker.py").read_text()
CANARY = "canary-7f3a9-not-a-real-secret"

PYBIN = os.environ.get("AGENT_BOX_TEST_PYBIN", sys.executable)

ns = {"__name__": "secret_lib"}
exec(compile(LIB, "secret-lib.py", "exec"), ns)


def check_crypto_available():
    try:
        subprocess.run([PYBIN, "-I", "-c", "import cryptography"],
                      check=True, capture_output=True)
    except Exception as exc:
        raise unittest.SkipTest(
            f"{PYBIN} has no `cryptography` package; set AGENT_BOX_TEST_PYBIN "
            f"to an interpreter that does ({exc})")


class Broker:
    def __init__(self, tmp, uid=None, user="agent", direct_launcher=True):
        self.tmp = tmp
        self.sock = os.path.join(tmp, "b.sock")
        self.root = os.path.join(tmp, "store")
        self.uid = os.getuid() if uid is None else uid
        script = os.path.join(tmp, "broker.py")
        Path(script).write_text(BROKER)
        worker = os.path.join(tmp, "worker.py")
        Path(worker).write_text(WORKER)
        args = [PYBIN, "-I", script, "--user", user, "--uid", str(self.uid),
               "--store-root", self.root, "--socket", self.sock,
               "--worker", worker]
        if direct_launcher:
            args.append("--dev-direct-launcher")
        self.proc = subprocess.Popen(args, stderr=subprocess.PIPE)
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(self.proc.stderr.read().decode())

    def ask(self, req):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(15)
        conn.connect(self.sock)
        ns["send_frame"](conn, req)
        out = ns["recv_frame"](conn)
        conn.close()
        return out

    def wait_job(self, job_id, timeout=5, profile=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.ask({"op": "status", "jobId": job_id, "profile": profile})
            if resp["state"] not in ("pending", "running"):
                return resp
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in {timeout}s")

    def stop(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stderr.close()


class Custody(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_crypto_available()

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

    def test_records_are_private_files_and_hold_no_plaintext(self):
        self.broker.ask({"op": "set", "name": "TOKEN", "value": CANARY})
        rec_path = Path(self.broker.root) / "_global" / "TOKEN.rec"
        self.assertEqual(rec_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((Path(self.broker.root) / "_global").stat().st_mode & 0o777, 0o700)
        raw = rec_path.read_bytes()
        self.assertNotIn(CANARY.encode(), raw)
        rec = json.loads(raw)
        self.assertIn("nonce", rec)
        self.assertIn("ct", rec)
        self.assertNotIn("value", rec)

    def test_root_key_file_is_root_only_and_shared_across_profiles(self):
        self.broker.ask({"op": "set", "name": "TOKEN", "value": CANARY})
        self.broker.ask({"op": "set", "profile": "worka", "name": "TOKEN", "value": "other"})
        key_path = Path(self.broker.root).parent / ".rootkey"
        self.assertTrue(key_path.exists())
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(key_path.read_bytes()), 32)

    def test_ciphertext_cannot_be_swapped_between_slots(self):
        # AAD binds (user, namespace, name, version); copying one record's
        # ciphertext onto another name's file must fail to decrypt rather
        # than silently returning the wrong plaintext.
        self.broker.ask({"op": "set", "name": "A", "value": "value-a"})
        self.broker.ask({"op": "set", "name": "B", "value": "value-b"})
        rec_a = Path(self.broker.root) / "_global" / "A.rec"
        rec_b = Path(self.broker.root) / "_global" / "B.rec"
        rec_b.write_bytes(rec_a.read_bytes())
        resp = self.broker.ask({"op": "exec", "argv": ["echo", "hi"], "secretNames": ["B"]})
        status = self.broker.wait_job(resp["jobId"])
        self.assertEqual(status["state"], "error")

    def test_schema_refusals(self):
        for req in ({"op": "get", "name": "TOKEN"},
                    {"op": "list", "uid": 0},
                    {"op": "set", "name": "../x", "value": "v"},
                    {"op": "set", "name": "A", "value": "a\0b"},
                    {"op": "set", "name": "A", "value": ""},
                    {"op": "set", "name": "A", "value": "x" * 40000},
                    {"op": "set", "name": "A", "value": "v", "path": "/etc"},
                    {"op": "remove", "name": "MISSING"},
                    {"op": "set", "profile": "../etc", "name": "A", "value": "v"},
                    {"op": "exec", "argv": ["echo"], "command": "echo"},
                    {"op": "exec", "command": "echo hi"},
                    {"op": "exec", "argv": ["echo"], "secretNames": ["not valid!"]},
                    {"op": "exec", "argv": ["echo"], "env": {"TOKEN": "x"},
                     "secretNames": ["TOKEN"]}):
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
            ns["Store"](self.broker.root, "_global", "agent", os.getuid() + 1, b"\x00" * 32)

    def test_symlinked_record_is_not_followed(self):
        self.broker.ask({"op": "set", "name": "A", "value": "v"})
        victim = Path(self.tmp) / "victim"
        victim.write_text("untouched")
        rec = Path(self.broker.root) / "_global" / "A.rec"
        rec.unlink()
        rec.symlink_to(victim)
        self.broker.ask({"op": "set", "name": "A", "value": "w"})
        self.assertEqual(victim.read_text(), "untouched")

    def test_cli_round_trip_and_value_stays_off_argv(self):
        cli = Path(self.tmp) / "cli.py"
        cli.write_text(CLI)

        def run(*args, stdin=None):
            return subprocess.run([PYBIN, "-I", str(cli), "--socket",
                                   self.broker.sock, *args], input=stdin,
                                  capture_output=True, text=True)
        self.assertEqual(run("set", "TOKEN", "--stdin", stdin=CANARY + "\n").returncode, 0)
        listed = run("list")
        self.assertIn("TOKEN\tv1\tconfigured", listed.stdout)
        self.assertNotIn(CANARY, listed.stdout + listed.stderr)
        self.assertNotEqual(run("set", "TOKEN", CANARY).returncode, 0)
        self.assertEqual(run("remove", "TOKEN").returncode, 0)

    def test_cli_profile_flag_warns_without_it(self):
        cli = Path(self.tmp) / "cli.py"
        cli.write_text(CLI)

        def run(*args, stdin=None):
            return subprocess.run([PYBIN, "-I", str(cli), "--socket",
                                   self.broker.sock, *args], input=stdin,
                                  capture_output=True, text=True)
        no_profile = run("set", "A", "--stdin", stdin="v\n")
        self.assertIn("deprecated", no_profile.stderr.lower())
        with_profile = run("--profile", "work", "set", "A", "--stdin", stdin="v\n")
        self.assertNotIn("deprecated", with_profile.stderr.lower())

    # --- Per-profile isolation --------------------------------------------

    def test_profiles_do_not_see_each_others_secrets(self):
        self.broker.ask({"op": "set", "profile": "work", "name": "TOKEN", "value": "work-value"})
        self.broker.ask({"op": "set", "profile": "play", "name": "TOKEN", "value": "play-value"})
        self.broker.ask({"op": "set", "name": "TOKEN", "value": "global-value"})
        work = self.broker.ask({"op": "list", "profile": "work"})["secrets"]
        play = self.broker.ask({"op": "list", "profile": "play"})["secrets"]
        glob = self.broker.ask({"op": "list"})["secrets"]
        self.assertEqual([s["name"] for s in work], ["TOKEN"])
        self.assertEqual([s["name"] for s in play], ["TOKEN"])
        self.assertEqual([s["name"] for s in glob], ["TOKEN"])
        # Each namespace's own version counter is independent.
        self.assertEqual(work[0]["version"], 1)
        self.assertEqual(play[0]["version"], 1)

    # --- exec_with_secrets --------------------------------------------------

    def test_exec_delivers_secret_to_child_env_only(self):
        self.broker.ask({"op": "set", "name": "TOKEN", "value": CANARY})
        out_file = os.path.join(self.tmp, "out.txt")
        resp = self.broker.ask({
            "op": "exec",
            "argv": ["sh", "-c", f'printf %s "$TOKEN" > {out_file}'],
            "secretNames": ["TOKEN"],
        })
        self.assertTrue(resp["ok"], resp)
        status = self.broker.wait_job(resp["jobId"])
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["exitCode"], 0)
        # The value never appears in anything the broker hands back...
        self.assertNotIn(CANARY, json.dumps(status))
        # ...but it really did reach the child, which wrote it to a file the
        # broker never saw.
        self.assertEqual(Path(out_file).read_text(), CANARY)

    def test_exec_redacts_secret_from_captured_output(self):
        self.broker.ask({"op": "set", "name": "TOKEN", "value": CANARY})
        resp = self.broker.ask({
            "op": "exec", "argv": ["sh", "-c", 'echo "token is $TOKEN"'],
            "secretNames": ["TOKEN"],
        })
        status = self.broker.wait_job(resp["jobId"])
        self.assertEqual(status["state"], "done")
        self.assertNotIn(CANARY, status["stdout"])
        self.assertIn("***redacted***", status["stdout"])

    def test_exec_rejects_unknown_secret_name(self):
        resp = self.broker.ask({"op": "exec", "argv": ["echo", "hi"],
                                "secretNames": ["NOPE"]})
        status = self.broker.wait_job(resp["jobId"])
        self.assertEqual(status["state"], "error")

    def test_exec_idempotency_key_replays_the_same_job(self):
        first = self.broker.ask({
            "op": "exec", "argv": ["echo", "hi"], "secretNames": [],
            "idempotencyKey": "run-1",
        })
        second = self.broker.ask({
            "op": "exec", "argv": ["echo", "hi"], "secretNames": [],
            "idempotencyKey": "run-1",
        })
        self.assertEqual(first["jobId"], second["jobId"])
        conflicting = self.broker.ask({
            "op": "exec", "argv": ["echo", "different"], "secretNames": [],
            "idempotencyKey": "run-1",
        })
        self.assertFalse(conflicting["ok"])

    def test_exec_timeout_kills_the_job(self):
        resp = self.broker.ask({
            "op": "exec", "argv": ["sleep", "30"], "secretNames": [],
            "timeoutMs": 300,
        })
        status = self.broker.wait_job(resp["jobId"], timeout=10)
        self.assertEqual(status["state"], "timeout")

    def test_exec_cancel_stops_a_running_job(self):
        resp = self.broker.ask({"op": "exec", "argv": ["sleep", "30"], "secretNames": []})
        time.sleep(0.2)
        cancelled = self.broker.ask({"op": "cancel", "jobId": resp["jobId"]})
        self.assertIn(cancelled["state"], ("cancelled", "done", "error"))
        status = self.broker.wait_job(resp["jobId"], timeout=10)
        self.assertIn(status["state"], ("cancelled", "error"))

    def test_exec_concurrency_cap_per_user(self):
        jobs = []
        for _ in range(ns["SECRET_MAX_JOBS_PER_USER"]):
            resp = self.broker.ask({"op": "exec", "argv": ["sleep", "5"], "secretNames": []})
            self.assertTrue(resp["ok"], resp)
            jobs.append(resp["jobId"])
        over = self.broker.ask({"op": "exec", "argv": ["sleep", "5"], "secretNames": []})
        self.assertFalse(over["ok"])
        for job_id in jobs:
            self.broker.ask({"op": "cancel", "jobId": job_id})


if __name__ == "__main__":
    unittest.main()
