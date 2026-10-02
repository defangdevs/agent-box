#!/usr/bin/env python3
"""The settings daemon's Codex pairing API (issue #780).

Defang Station pairs the Codex apps from its own page, so the manual pairing
code has to reach it as JSON. What these tests pin is what is easy to get
quietly wrong:

* the code is a credential for this box's Codex: it appears in the GET
  answer and NOWHERE else - not in the daemon's own output, not in
  sessions.json, not in an error string - and the non-manual `pairingCode`
  is never exposed at all;
* `claimed` is sticky, so a poller that looks away for a moment cannot miss
  it;
* the status codes Station keys on (404 / 409 / 429 / 503 / 303), and
  `application/json` on the GET, because that content type is how Station
  tells "route not on this box" from "box broken";
* one remote-control session, never two: two rc sessions kill each other's
  daemon (issue #159), and a TUI codex session cannot be paired at all.

The control socket is a real WebSocket over a real Unix socket, served by a
small fake in this file, so the client in the daemon is exercised for real
(handshake, masked frames, the initialize/initialized exchange) rather than
stubbed out. The same client was checked by hand against a live codex 0.159
app-server for status/read, client/list and pairing/status.

The subject is the GOLDEN PAYLOAD, for the reason test-profile-panel.py
gives.
"""
import base64
import contextlib
import hashlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")

CODE = "ABCD-EFGH"
QR = "QRPAYLOAD-NEVER-EXPOSED"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# A stand-in `codex`. The shebang is this interpreter's own path: the nix
# sandbox this runs in has no /usr/bin/env.
FAKE_CODEX = """#!%(python)s
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    if os.path.exists(%(signed_in)r):
        print("Logged in using ChatGPT")
        sys.exit(0)
    print("Not logged in")
    sys.exit(1)
if args[:3] == ["app-server", "daemon", "version"]:
    if not os.path.exists(%(sock)r):
        sys.exit(1)
    print(json.dumps({"status": "running", "socketPath": %(sock)r}))
    sys.exit(0)
sys.exit(2)
"""


def daemon_with(**env):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ.update(env)
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_pairing_test", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class FakeControlSocket:
    """The slice of codex's app-server the daemon talks to."""

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.claimed = False
        self.status = "connected"
        self.devices = [{"clientId": "cli_1", "displayName": "Raph's iPhone",
                         "deviceType": "phone", "platform": "ios",
                         "deviceModel": "iPhone17,1", "appVersion": "1.2026.270",
                         "lastSeenAt": 1790870000, "osVersion": "26"}]
        self.fail = {}
        self.delay = {}
        # Protocol violations the fake saw. Recorded rather than asserted:
        # the serving thread swallows errors, so an assert there would pass
        # silently (and Sonar S5779 rightly objects).
        self.violations = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(8)
        threading.Thread(target=self.accept, daemon=True).start()

    def check(self, ok, message):
        if not ok:
            self.violations.append(message)

    def close(self):
        self.listener.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    @staticmethod
    def recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def read_frame(self, conn):
        b0, b1 = self.recv_exact(conn, 2)
        self.check(b1 & 0x80, "client frames must be masked")
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.recv_exact(conn, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.recv_exact(conn, 8))[0]
        mask = self.recv_exact(conn, 4)
        data = self.recv_exact(conn, n)
        return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    @staticmethod
    def send_frame(conn, obj):
        data = json.dumps(obj).encode()
        head = bytes([0x81])
        if len(data) < 126:
            head += bytes([len(data)])
        else:
            head += bytes([126]) + struct.pack(">H", len(data))
        conn.sendall(head + data)

    def serve(self, conn):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(1024)
            key = [line.split(b": ", 1)[1] for line in buf.split(b"\r\n")
                   if line.lower().startswith(b"sec-websocket-key")][0]
            accept = base64.b64encode(
                hashlib.sha1(key + GUID.encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket"
                          "\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s"
                          "\r\n\r\n" % accept).encode())
            initialized = False
            while True:
                msg = self.read_frame(conn)
                method = msg.get("method")
                if method == "initialized":
                    initialized = True
                    continue
                if method == "initialize":
                    self.check(msg["params"]["capabilities"]["experimentalApi"]
                               is True, "initialize without experimentalApi")
                    self.send_frame(conn, {"id": msg["id"], "result": {}})
                    continue
                self.check(initialized, "request before initialized")
                self.calls.append((method, msg.get("params")))
                time.sleep(self.delay.get(method, 0))
                # A notification first, as the real server sends them.
                self.send_frame(conn, {"method": "remoteControl/status/changed",
                                       "params": {}})
                if method in self.fail:
                    self.send_frame(conn, {"id": msg["id"], "error": {
                        "message": self.fail[method]}})
                else:
                    self.send_frame(conn, {"id": msg["id"],
                                           "result": self.result(method, msg)})
        except (EOFError, OSError):
            pass
        finally:
            conn.close()

    def result(self, method, msg):
        params = msg.get("params") or {}
        if method == "remoteControl/status/read":
            return {"status": self.status, "serverName": "box.example",
                    "installationId": "i", "environmentId": "env_1"}
        if method == "remoteControl/enable":
            self.status = "connected"
            return {"status": self.status, "serverName": "box.example"}
        if method == "remoteControl/pairing/start":
            self.check(params == {"manualCode": True}, "bad pairing/start")
            self.claimed = False
            return {"pairingCode": QR, "manualPairingCode": CODE,
                    "environmentId": "env_1",
                    "expiresAt": int(time.time()) + 300}
        if method == "remoteControl/pairing/status":
            self.check(params == {"manualPairingCode": CODE},
                       "bad pairing/status")
            return {"claimed": self.claimed}
        if method == "remoteControl/client/list":
            self.check(params["environmentId"] == "env_1", "bad client/list")
            return {"data": list(self.devices), "nextCursor": None}
        if method == "remoteControl/client/revoke":
            self.devices = [d for d in self.devices
                            if d["clientId"] != params["clientId"]]
            return {}
        return {}


class Pairing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.conf = os.path.join(root, "agent-box")
        os.makedirs(self.conf)
        self.env_file = os.path.join(self.conf, "env")
        open(self.env_file, "w").close()
        self.sessions_file = os.path.join(self.conf, "sessions.json")
        self.write_sessions({})
        self.signed_in = os.path.join(root, "signed-in")
        open(self.signed_in, "w").close()
        self.sock_path = os.path.join(root, "control.sock")
        self.codex = os.path.join(root, "codex")
        with open(self.codex, "w") as handle:
            handle.write(FAKE_CODEX % {"python": sys.executable,
                                       "signed_in": self.signed_in,
                                       "sock": self.sock_path})
        os.chmod(self.codex, 0o755)
        self.control = FakeControlSocket(self.sock_path)
        self.addCleanup(self.control.close)
        self.addCleanup(lambda: self.assertEqual(self.control.violations, []))
        self.module = daemon_with(
            AGENT_BOX_SETTINGS_ENV_FILE=self.env_file,
            AGENT_BOX_SESSIONS_FILE=self.sessions_file,
            AGENT_BOX_AGENTS="claude,codex,shell",
            AGENT_BOX_DEFAULT_AGENT="claude",
            AGENT_BOX_CONNECT_BINS="codex=" + self.codex,
            HOME=root)
        m = self.module
        m.capacity_live = lambda: set()
        m.capacity_limit = lambda: 100
        # The daemon polls on a 2 s cache and rate-limits starts to one per
        # 5 s; a test must not sleep for either.
        m.CODEX_CACHE_TTL = 0
        m.CODEX_START_MIN_INTERVAL = 0
        m.CODEX_DAEMON_WAIT = 2
        self.flow = self.in_home(m.connect_flow, "codex")
        self.assertIsNotNone(self.flow)
        self.probe()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/settings" % server.server_address[1]
        os.environ["HOME"] = root
        self.addCleanup(os.environ.pop, "HOME", None)

    def in_home(self, call, *args):
        saved = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        try:
            return call(*args)
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved

    def probe(self):
        """Fill the sign-in status cache synchronously; the daemon's own
        refresh is a background thread."""
        self.module.connect_probe(self.flow)

    def write_sessions(self, sessions):
        with open(self.sessions_file, "w") as handle:
            json.dump({"sessions": sessions}, handle)

    def sessions(self):
        with open(self.sessions_file) as handle:
            return json.load(handle)["sessions"]

    def get(self):
        response = urllib.request.urlopen(self.base + "/codex/pairing")
        self.assertEqual(response.headers["Content-Type"].split(";")[0],
                         "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return json.loads(response.read())["pairing"]

    def post(self, path, **fields):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(
            self.base + path, data=urllib.parse.urlencode(fields).encode(),
            method="POST")
        try:
            response = urllib.request.build_opener(NoRedirect).open(request)
            return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    # --- the happy path -------------------------------------------------

    def test_start_then_get_answers_waiting_with_the_manual_code(self):
        status, _ = self.post("/codex/pairing/start")
        self.assertEqual(status, 303)
        pairing = self.get()
        self.assertEqual(pairing["state"], "waiting")
        self.assertEqual(pairing["code"], CODE)
        self.assertGreater(pairing["expires_at"], time.time())
        self.assertEqual(pairing["server_name"], "box.example")
        self.assertIsNone(pairing["error"])
        self.assertEqual(pairing["devices"][0], {
            "client_id": "cli_1", "display_name": "Raph's iPhone",
            "device_type": "phone", "platform": "ios",
            "device_model": "iPhone17,1", "app_version": "1.2026.270",
            "last_seen_at": 1790870000})
        self.assertIsNone(pairing["devices_error"])

    def test_the_non_manual_code_is_never_exposed(self):
        self.post("/codex/pairing/start")
        body = urllib.request.urlopen(self.base + "/codex/pairing").read().decode()
        self.assertNotIn(QR, body)
        self.assertNotIn("pairing_code", body)

    def test_start_makes_exactly_one_remote_control_session(self):
        self.post("/codex/pairing/start")
        self.post("/codex/pairing/start")
        rc = {n: s for n, s in self.sessions().items() if s["agent"] == "codex"}
        self.assertEqual(len(rc), 1)
        (session,) = rc.values()
        self.assertTrue(session["remoteControl"])
        self.assertIsNone(session["profile"])

    def test_a_tui_codex_session_does_not_count_as_one_to_pair(self):
        self.write_sessions({"codex": {"agent": "codex", "remoteControl": False}})
        self.post("/codex/pairing/start")
        flags = sorted(s["remoteControl"] for s in self.sessions().values())
        self.assertEqual(flags, [False, True])

    def test_claimed_is_sticky(self):
        self.post("/codex/pairing/start")
        self.assertEqual(self.get()["state"], "waiting")
        self.control.claimed = True
        pairing = self.get()
        self.assertEqual(pairing["state"], "claimed")
        self.assertIsNone(pairing["code"])
        # Codex forgetting the code, or us never asking again, changes nothing.
        self.control.claimed = False
        self.assertEqual(self.get()["state"], "claimed")
        self.assertEqual(self.get()["state"], "claimed")
        # A new start clears it.
        self.post("/codex/pairing/start")
        self.assertEqual(self.get()["state"], "waiting")

    def test_cancel_forgets_the_code_and_always_answers_303(self):
        self.post("/codex/pairing/start")
        self.assertEqual(self.post("/codex/pairing/cancel")[0], 303)
        pairing = self.get()
        self.assertEqual(pairing["state"], "ready")
        self.assertIsNone(pairing["code"])
        self.assertEqual(self.post("/codex/pairing/cancel")[0], 303)

    def test_an_unclaimed_code_past_its_expiry_is_expired(self):
        self.post("/codex/pairing/start")
        with self.module._codex_lock:
            self.module._codex_pairing["expires_at"] = int(time.time()) - 1
        pairing = self.get()
        self.assertEqual(pairing["state"], "expired")
        self.assertIsNone(pairing["code"])

    def test_pairing_status_is_not_called_unless_a_code_is_waiting(self):
        self.get()
        self.post("/codex/pairing/start")
        self.control.calls.clear()
        self.post("/codex/pairing/cancel")
        self.get()
        self.assertNotIn("remoteControl/pairing/status",
                         [name for name, _ in self.control.calls])

    # --- refusals -------------------------------------------------------

    def test_signed_out_reports_so_and_refuses_to_start(self):
        os.unlink(self.signed_in)
        self.probe()
        pairing = self.get()
        self.assertEqual(pairing["state"], "signed_out")
        self.assertIsNone(pairing["code"])
        status, _ = self.post("/codex/pairing/start")
        self.assertEqual(status, 409)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(self.sessions(), {})

    def test_a_start_inside_the_interval_is_429(self):
        self.module.CODEX_START_MIN_INTERVAL = 60
        self.assertEqual(self.post("/codex/pairing/start")[0], 303)
        self.assertEqual(self.post("/codex/pairing/start")[0], 429)

    def test_a_full_box_answers_503(self):
        self.module.capacity_limit = lambda: 0
        status, _ = self.post("/codex/pairing/start")
        self.assertEqual(status, 503)

    def test_codex_not_yet_installed_is_signed_out_and_start_is_409(self):
        """The card exists wherever codex is installable, so the GET is not
        a 404 there: the box can still offer Codex, it just is not signed
        in."""
        os.chmod(self.codex, 0o644)
        self.module._connect_status_cache.clear()
        saved = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.assertEqual(self.get()["state"], "signed_out")
        status, _ = self.post("/codex/pairing/start")
        self.assertEqual(status, 409)
        self.assertEqual(self.sessions(), {})

    # --- failures are reported, redacted -------------------------------

    def test_a_pairing_start_failure_is_failed_with_a_redacted_reason(self):
        self.control.fail["remoteControl/pairing/start"] = (
            "remote control pairing start failed at `https://chatgpt.com/x`: "
            "HTTP 401, request-id: abc-123, cf-ray: 9f-LAX, body: "
            '{"detail":"token_invalidated: sign in again"}')
        status, _ = self.post("/codex/pairing/start")
        self.assertEqual(status, 303)
        pairing = self.get()
        self.assertEqual(pairing["state"], "failed")
        self.assertEqual(pairing["error"], "token_invalidated: sign in again")
        self.assertIsNone(pairing["code"])

    def test_a_listing_failure_does_not_fail_the_read(self):
        self.control.fail["remoteControl/client/list"] = "boom https://x.test/y"
        self.post("/codex/pairing/start")
        pairing = self.get()
        self.assertEqual(pairing["state"], "waiting")
        self.assertEqual(pairing["devices"], [])
        self.assertEqual(pairing["devices_error"], "boom [redacted]")

    def test_redaction_drops_urls_ids_and_anything_code_shaped(self):
        redact = self.module.codex_redact
        self.assertNotIn("http", redact("see https://a.test/b?c=1 now"))
        self.assertNotIn("abc", redact("request-id: abc-1, cf-ray: zz"))
        self.assertNotIn(CODE, redact("the code %s was wrong" % CODE))
        self.assertLessEqual(len(redact("x" * 5000)), self.module.CODEX_ERROR_MAX)

    def test_a_daemon_that_never_comes_up_is_starting_then_failed(self):
        self.control.close()
        self.module.CODEX_DAEMON_WAIT = 0.3
        status, _ = self.post("/codex/pairing/start")
        self.assertEqual(status, 303)
        self.assertEqual(self.get()["state"], "starting")
        self.module.CODEX_STARTING_GRACE = 0
        self.assertEqual(self.get()["state"], "failed")

    def test_nothing_outstanding_and_no_daemon_starts_nothing(self):
        self.control.close()
        self.assertEqual(self.get()["state"], "ready")
        self.assertEqual(self.sessions(), {})

    # --- devices --------------------------------------------------------

    def test_revoke_removes_a_device_and_the_next_read_shows_it_gone(self):
        self.get()
        status, _ = self.post("/codex/devices/revoke", client_id="cli_1")
        self.assertEqual(status, 303)
        self.assertIn(("remoteControl/client/revoke",
                       {"environmentId": "env_1", "clientId": "cli_1"}),
                      self.control.calls)
        self.assertEqual(self.get()["devices"], [])

    def test_revoking_an_unknown_device_is_404(self):
        self.assertEqual(
            self.post("/codex/devices/revoke", client_id="cli_nope")[0], 404)
        self.assertEqual(
            self.post("/codex/devices/revoke", client_id="../x y")[0], 404)
        self.assertNotIn("remoteControl/client/revoke",
                         [name for name, _ in self.control.calls])

    # --- the code stays in memory --------------------------------------

    def test_the_code_is_in_no_file_and_no_output(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.post("/codex/pairing/start")
            self.get()
            self.control.claimed = True
            self.get()
            self.control.claimed = False
            self.post("/codex/pairing/start")
            self.control.fail["remoteControl/pairing/status"] = "bad " + CODE
            self.get()
        self.assertNotIn(CODE, out.getvalue() + err.getvalue())
        for root, _, files in os.walk(self.tmp.name):
            for name in files:
                path = os.path.join(root, name)
                if path == self.sock_path:
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
                self.assertNotIn(CODE.encode(), data, path)
                self.assertNotIn(QR.encode(), data, path)

    # --- review findings (PR #781) --------------------------------------

    def test_a_stopped_rc_session_is_revived_not_waited_on(self):
        self.write_sessions({"codex": {"agent": "codex", "remoteControl": True,
                                       "stopped": True}})
        self.assertEqual(self.post("/codex/pairing/start")[0], 303)
        self.assertNotIn("stopped", self.sessions()["codex"])
        self.assertEqual(list(self.sessions()), ["codex"])
        self.assertEqual(self.get()["state"], "waiting")

    def test_a_crashed_rc_session_says_so_instead_of_timing_out(self):
        self.write_sessions({"codex": {"agent": "codex", "remoteControl": True,
                                       "died": 1}})
        self.control.close()
        self.assertEqual(self.post("/codex/pairing/start")[0], 303)
        pairing = self.get()
        self.assertEqual(pairing["state"], "failed")
        self.assertIn("crashed", pairing["error"])

    def test_a_cancel_during_a_start_is_not_undone(self):
        self.control.delay["remoteControl/pairing/start"] = 1.0
        result = []
        thread = threading.Thread(
            target=lambda: result.append(self.post("/codex/pairing/start")))
        thread.start()
        deadline = time.time() + 5
        while ("remoteControl/pairing/start" not in
               [name for name, _ in self.control.calls]):
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        self.post("/codex/pairing/cancel")
        thread.join()
        self.assertEqual(result[0][0], 303)
        pairing = self.get()
        self.assertEqual(pairing["state"], "ready")
        self.assertIsNone(pairing["code"])

    # --- the sign-in default -------------------------------------------

    def test_a_finished_codex_sign_in_defaults_to_the_tui(self):
        self.assertEqual(self.module.codex_session_default(), "tui")
        self.in_home(self.module.connect_signed_in, self.flow)
        (session,) = self.sessions().values()
        self.assertFalse(session["remoteControl"])

    def test_the_env_setting_makes_a_codex_sign_in_start_remote_control(self):
        self.module.set_key("AGENT_BOX_CODEX_SESSION_DEFAULT", "remote-control")
        self.assertEqual(self.module.codex_session_default(), "remote-control")
        self.in_home(self.module.connect_signed_in, self.flow)
        (session,) = self.sessions().values()
        self.assertTrue(session["remoteControl"])
        self.assertIsNone(session["profile"])

    def test_any_other_value_is_the_tui(self):
        self.module.set_key("AGENT_BOX_CODEX_SESSION_DEFAULT", "banana")
        self.assertEqual(self.module.codex_session_default(), "tui")


if __name__ == "__main__":
    unittest.main(verbosity=2)
