#!/usr/bin/env python3
"""Native tests for temporary resumable browser uploads (issue #898).

The HTTP tests drive the assembled settings daemon exactly as Caddy does.
One test deliberately closes a TCP connection halfway through a declared
chunk, then resumes from the offset the daemon persisted. No Nix or browser
is required:

    python3 tests/test-receive.py
"""

import base64
import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")
USER = "alice"
WORK = None
STATE = None
UPLOADS = None
daemon = None


def setUpModule():
    global WORK, STATE, UPLOADS, daemon
    WORK = tempfile.mkdtemp(prefix="agent-box-receive-")
    STATE = os.path.join(WORK, "state")
    UPLOADS = os.path.join(WORK, "uploads")
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({
        "PATH": saved.get("PATH", "/usr/bin:/bin"),
        "HOME": WORK,
        "AGENT_BOX_SETTINGS_USER": USER,
        "AGENT_BOX_SETTINGS_ENV_FILE": os.path.join(WORK, "env"),
        "AGENT_BOX_SESSIONS_FILE": os.path.join(WORK, "sessions.json"),
        "AGENT_BOX_SETTINGS_BASE": "/alice/settings",
        "AGENT_BOX_RECEIVE_STATE_DIR": STATE,
        "AGENT_BOX_UPLOADS_DIR": UPLOADS,
    })
    try:
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_receive_under_test", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        daemon = importlib.util.module_from_spec(spec)
        loader.exec_module(daemon)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    daemon.Handler.log_message = lambda self, fmt, *args: None


def tearDownModule():
    shutil.rmtree(WORK, ignore_errors=True)


def content_id(blob):
    leaves = bytearray()
    for offset in range(0, len(blob), 8 << 20):
        leaves.extend(hashlib.sha256(blob[offset:offset + (8 << 20)]).digest())
    prefix = ("agent-box-content-v1\n%d\n" % len(blob)).encode("ascii")
    return "sha256-tree-v1:" + hashlib.sha256(prefix + leaves).hexdigest()


def metadata(name, size, modified=123456789, content=None):
    def encoded(value):
        return base64.b64encode(value.encode()).decode()
    fingerprint = "%s\n%d\n%d" % (name, size, modified)
    identity = content_id(content) if content is not None else \
        "sha256-tree-v1:" + "0" * 64
    return "filename %s,fingerprint %s,content %s" % (
        encoded(name), encoded(fingerprint), encoded(identity))


class ReceiveHTTPTest(unittest.TestCase):
    server = None
    port = 0

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), daemon.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.rmtree(UPLOADS, ignore_errors=True)
        self.slot, self.token, self.record = daemon.receive_create(
            3600, 64 << 20)
        self.page = "/alice/receive/%s/" % self.slot
        self.collection = self.page + "files"
        self.resource = self.collection + "/upload"

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    def auth(self):
        return {"Authorization": "Bearer " + self.token,
                "Tus-Resumable": "1.0.0"}

    def create(self, name, length, content=None):
        headers = self.auth()
        headers.update({"Upload-Length": str(length),
                        "Upload-Metadata": metadata(
                            name, length, content=content)})
        return self.request("POST", self.collection, headers=headers)

    def patch(self, body, offset):
        headers = self.auth()
        headers.update({"Upload-Offset": str(offset),
                        "Content-Type": "application/offset+octet-stream"})
        return self.request("PATCH", self.resource, body=body, headers=headers)

    def head(self, if_match=None):
        headers = self.auth()
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.request("HEAD", self.resource, headers=headers)

    def test_public_page_contains_no_capability(self):
        status, headers, body = self.request("GET", self.page)
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), body)
        self.assertIn(b"one resumable upload", body)
        self.assertIn(b"Drop a file here", body)
        self.assertIn(b"event.dataTransfer.files", body)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'",
                      headers["Content-Security-Policy"])

    def test_forward_auth_verifier_accepts_only_the_capability(self):
        forwarded = {"Authorization": "Bearer " + self.token,
                     "X-Forwarded-Uri": self.collection}
        status, _headers, _body = self.request(
            "GET", "/alice/receive/_verify", headers=forwarded)
        self.assertEqual(status, 204)
        for value, uri in (("", self.collection),
                           ("Bearer wrong.wrong", self.collection),
                           (self.token, self.collection),
                           ("Bearer " + self.token, "/alice/settings")):
            status, _headers, _body = self.request(
                "GET", "/alice/receive/_verify",
                headers={"Authorization": value, "X-Forwarded-Uri": uri})
            self.assertEqual(status, 401, (value, uri))

    def test_secret_is_hashed_at_rest(self):
        text = pathlib.Path(STATE, self.slot + ".json").read_text()
        self.assertNotIn(self.token, text)
        self.assertNotIn(self.token.split(".", 1)[1], text)
        self.assertIn("secret_sha256", json.loads(text))

    def test_minted_slot_never_looks_like_a_cli_option(self):
        with mock.patch.object(
                daemon.secrets, "token_urlsafe",
                side_effect=["-123456789abcdef", "A123456789abcdef",
                             "s" * 43]):
            slot, token, _record = daemon.receive_create(3600, 1024)
        self.assertEqual(slot, "A123456789abcdef")
        self.assertEqual(token, slot + "." + "s" * 43)

    def test_create_head_patch_and_publish(self):
        blob = (b"resumable upload\n" * 400000)[:5 << 20]
        status, headers, _body = self.create(
            "artifact.bin", len(blob), blob)
        self.assertEqual(status, 201)
        self.assertEqual(headers["Upload-Offset"], "0")
        status, headers, body = self.patch(blob[:2 << 20], 0)
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(headers["Upload-Offset"], str(2 << 20))
        status, headers, _body = self.head()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Upload-Offset"], str(2 << 20))
        self.assertEqual(headers["Upload-Content"], content_id(blob))
        self.assertNotIn("ETag", headers)
        status, headers, _body = self.patch(blob[2 << 20:], 2 << 20)
        self.assertEqual(status, 204)
        self.assertEqual(headers["Upload-Offset"], str(len(blob)))
        self.assertEqual(headers["Upload-Complete"], "?1")
        etag = '"' + content_id(blob) + '"'
        self.assertEqual(headers["ETag"], etag)
        self.assertNotIn("Upload-Content", headers)
        self.assertEqual(headers["Upload-SHA256"],
                         hashlib.sha256(blob).hexdigest())
        self.assertEqual(self.head(etag)[0], 200)
        status, mismatch_headers, _body = self.head(
            '"sha256-tree-v1:' + '0' * 64 + '"')
        self.assertEqual(status, 412)
        self.assertEqual(mismatch_headers["ETag"], etag)
        final = pathlib.Path(UPLOADS, headers["Upload-Filename"])
        self.assertEqual(hashlib.sha256(final.read_bytes()).digest(),
                         hashlib.sha256(blob).digest())

    def test_zero_length_upload_completes_at_creation(self):
        status, headers, _body = self.create("empty.bin", 0, b"")
        self.assertEqual(status, 201)
        self.assertEqual(headers["Upload-Complete"], "?1")
        final = pathlib.Path(UPLOADS, headers["Upload-Filename"])
        self.assertTrue(final.is_file())
        self.assertEqual(final.stat().st_size, 0)
        self.assertEqual(self.head()[1]["Upload-Offset"], "0")

    def test_create_recovers_an_empty_partial_left_before_record_update(self):
        daemon.receive_ensure_dirs()
        pathlib.Path(daemon.receive_partial_path(self.slot)).touch(mode=0o600)
        status, headers, _body = self.create(
            "recovered-create.bin", 4, b"done")
        self.assertEqual(status, 201)
        self.assertEqual(headers["Upload-Offset"], "0")
        self.assertEqual(self.patch(b"done", 0)[0], 204)

    def test_interrupted_request_resumes_from_persisted_bytes(self):
        blob = b"0123456789abcdef"
        self.assertEqual(self.create("resume.bin", len(blob), blob)[0], 201)
        request = (
            "PATCH %s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Authorization: Bearer %s\r\nTus-Resumable: 1.0.0\r\n"
            "Upload-Offset: 0\r\nContent-Type: application/offset+octet-stream\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n"
        ) % (self.resource, self.token, len(blob))
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall(request.encode("ascii") + blob[:7])
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, headers, _body = self.head()
            if status == 200 and headers.get("Upload-Offset") == "7":
                break
            time.sleep(0.02)
        self.assertEqual(headers.get("Upload-Offset"), "7")
        status, headers, _body = self.patch(blob[7:], 7)
        self.assertEqual(status, 204)
        final = pathlib.Path(UPLOADS, headers["Upload-Filename"])
        self.assertEqual(final.read_bytes(), blob)

    def test_head_finishes_a_full_partial_left_by_a_daemon_crash(self):
        blob = b"durable-before-publish"
        self.assertEqual(self.create("recovered.bin", len(blob), blob)[0], 201)
        pathlib.Path(daemon.receive_partial_path(self.slot)).write_bytes(blob)
        status, headers, _body = self.head()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Upload-Complete"], "?1")
        final = pathlib.Path(UPLOADS, headers["Upload-Filename"])
        self.assertEqual(final.read_bytes(), blob)

    def test_wrong_offset_does_not_modify_partial(self):
        self.assertEqual(self.create("offset.bin", 6)[0], 201)
        self.assertEqual(self.patch(b"abc", 0)[0], 204)
        status, headers, _body = self.patch(b"def", 0)
        self.assertEqual(status, 409)
        self.assertEqual(headers["Upload-Offset"], "3")
        self.assertEqual(self.head()[1]["Upload-Offset"], "3")

    def test_content_mismatch_is_never_published(self):
        expected = b"document version one"
        changed = b"document version two"
        self.assertEqual(len(expected), len(changed))
        self.assertEqual(self.create(
            "report.docx", len(expected), expected)[0], 201)
        status, _headers, body = self.patch(changed, 0)
        self.assertEqual(status, 422)
        self.assertIn(b"do not match", body)
        self.assertFalse(any(pathlib.Path(UPLOADS).glob("report*")))

    def test_slot_binds_to_only_one_file(self):
        self.assertEqual(self.create("one.bin", 3)[0], 201)
        status, headers, _body = self.create("two.bin", 3)
        self.assertEqual(status, 409)
        self.assertEqual(headers["Location"], self.resource)

    def test_size_limit_is_enforced_before_a_partial_is_created(self):
        record = daemon.receive_read(self.slot)
        record["max_bytes"] = 4
        daemon.receive_write(record)
        status, headers, _body = self.create("large.bin", 5)
        self.assertEqual(status, 413)
        self.assertEqual(headers["Tus-Max-Size"], "4")
        self.assertFalse(os.path.exists(daemon.receive_partial_path(self.slot)))

    def test_new_upload_cannot_overcommit_space_reserved_by_another(self):
        other, _token, record = daemon.receive_create(3600, 100)
        daemon.receive_ensure_dirs()
        pathlib.Path(daemon.receive_partial_path(other)).write_bytes(b"x" * 20)
        record.update({"status": "uploading", "length": 100,
                       "name": "other.bin",
                       "metadata": metadata("other.bin", 100),
                       "fingerprint": "other.bin\n100\n123456789"})
        daemon.receive_write(record)
        usage = mock.Mock(free=daemon.RECEIVE_FREE_RESERVE + 84)
        with mock.patch.object(daemon.shutil, "disk_usage", return_value=usage):
            status, _headers, _body = self.create("bounded.bin", 5)
        self.assertEqual(status, 507)

    def test_filename_is_metadata_not_a_path(self):
        blob = b"safe"
        self.assertEqual(self.create(
            "../../outside.txt", len(blob), blob)[0], 201)
        status, headers, _body = self.patch(blob, 0)
        self.assertEqual(status, 204)
        self.assertTrue(headers["Upload-Filename"].startswith("outside-"))
        self.assertFalse(os.path.exists(os.path.join(WORK, "outside.txt")))

    def test_non_ascii_filename_is_safe_in_response_headers(self):
        blob = b"safe"
        self.assertEqual(self.create("caf\u00e9.txt", len(blob), blob)[0], 201)
        status, headers, _body = self.patch(blob, 0)
        self.assertEqual(status, 204)
        self.assertIn("caf%C3%A9-", headers["Upload-Filename"])

    def test_expired_slot_is_refused_and_pruned(self):
        record = daemon.receive_read(self.slot)
        record["expires"] = int(time.time()) - 1
        daemon.receive_write(record)
        self.assertEqual(self.head()[0], 401)
        self.assertEqual(daemon.receive_prune(), 1)
        self.assertFalse(os.path.exists(
            os.path.join(STATE, self.slot + ".json")))


class ReceiveCLITest(unittest.TestCase):
    def test_cli_prints_secret_only_in_fragment_and_can_revoke(self):
        assembled = os.path.join(WORK, "agent-box-receive.py")
        with open(assembled, "w") as output:
            subprocess.run([
                sys.executable, str(REPO / "bin" / "assemble-module.py"),
                "--resolve", str(REPO / "modules" / "src" / "receive-cli.py"),
            ], check=True, stdout=output)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": WORK,
               "AGENT_BOX_URL": "https://box.example/alice/",
               "AGENT_BOX_RECEIVE_STATE_DIR": STATE,
               "AGENT_BOX_UPLOADS_DIR": UPLOADS}
        made = subprocess.run(
            [sys.executable, assembled, "--ttl", "1h", "--max-size", "500M"],
            check=True, text=True, capture_output=True, env=env)
        url = made.stdout.strip()
        self.assertRegex(
            url, r"^https://box\.example/alice/receive/[A-Za-z0-9_-]+/#"
                 r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
        slot = url.split("/receive/", 1)[1].split("/", 1)[0]
        listed = subprocess.run(
            [sys.executable, assembled, "list"], check=True, text=True,
            capture_output=True, env=env)
        self.assertIn(slot, listed.stdout)
        subprocess.run([sys.executable, assembled, "revoke", slot],
                       check=True, text=True, capture_output=True, env=env)
        self.assertFalse(os.path.exists(os.path.join(STATE, slot + ".json")))


if __name__ == "__main__":
    unittest.main()
