#!/usr/bin/env python3
"""Request the rendered Caddy routes, including errors and upstream headers."""
import base64
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from test_agentbox import render


class Upstream(http.server.BaseHTTPRequestHandler):
    server_version = "identifying-upstream"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"upstream ok")

    def log_message(self, *args):
        pass


class HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def exercise(source, directory):
    directory.mkdir()
    http_port, https_port = free_port(), free_port()
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    # Keep the rendered routing/header handlers. Only replace deployment
    # dependencies: privileged ports, ACME, admin socket and upstream sockets.
    config = source.replace("admin unix//run/caddy/admin.sock", f"""admin off
  http_port {http_port}
  https_port {https_port}
  skip_install_trust""")
    config = re.sub(r"(?ms)^  tls \{\n.*?^  \}", "  tls internal", config)
    config = re.sub(r"(?m)^import /var/lib/agent-box-sites/.*\n", "", config)
    config = re.sub(r"unix//run/[^\s{}]+", f"127.0.0.1:{upstream.server_port}", config)
    path = directory / "Caddyfile"
    path.write_text(config)
    env = dict(os.environ, XDG_DATA_HOME=str(directory / "data"),
               XDG_CONFIG_HOME=str(directory / "config"),
               WEB_PASSWORD_ALGORITHM_AGENT="bcrypt",
               WEB_PASSWORD_HASH_AGENT=subprocess.check_output(
                   ["caddy", "hash-password", "--algorithm", "bcrypt",
                    "--bcrypt-cost", "4", "--plaintext", "testpassword"], text=True).strip(),
               WEB_COOKIE_SECRET_AGENT="test-cookie")
    with (directory / "caddy.log").open("w+") as log:
        proc = subprocess.Popen(["caddy", "run", "--config", str(path),
                                 "--adapter", "caddyfile"], env=env,
                                stdout=log, stderr=log)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise AssertionError("Caddy exited during startup")
                try:
                    for host in ("box.test", "alias.test"):
                        conn = HTTPSConnection(host, https_port,
                                               context=ssl._create_unverified_context())
                        conn.connect()
                        conn.close()
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise AssertionError("Caddy did not start")

            def request(host, target, status, auth=False, tls=True, authority=None):
                if tls:
                    conn = HTTPSConnection(host, https_port,
                                           context=ssl._create_unverified_context())
                else:
                    conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=20)
                headers = {"Host": authority or host}
                if auth:
                    token = base64.b64encode(b"agent:testpassword").decode()
                    headers["Authorization"] = f"Basic {token}"
                conn.request("GET", target, headers=headers)
                response = conn.getresponse()
                body = response.read()
                fields = {key.lower(): value for key, value in response.getheaders()}
                conn.close()
                assert response.status == status, (host, target, response.status, body)
                assert "server" not in fields, (host, target, fields)
                assert "via" not in fields, (host, target, fields)
                return fields, body

            for host in ("box.test", "alias.test"):
                fields, _ = request(host, "/some/path?x=1", 308, tls=False)
                assert fields["location"] == f"https://{host}/some/path?x=1", fields
                fields, body = request(host, "/agent/webhook/github", 200)
                assert body == b"upstream ok"
            fields, _ = request("unconfigured.test", "/some/path?x=1", 308, tls=False)
            assert fields["location"] == "https://unconfigured.test/some/path?x=1"
            request("box.test", "/", 404, authority="unconfigured.test")
            fields, _ = request("alias.test", "/some/path?x=1", 301)
            assert fields["location"] == "https://box.test/some/path?x=1", fields
            fields, body = request("box.test", "/", 401)
            assert "www-authenticate" in fields
            assert fields["content-type"].startswith("text/html")
            assert b"Sign in required" in body
            request("box.test", "/agent", 302)
            request("box.test", "/agent/receive/invalid", 404)
            for target in ("/", "/agent/", "/agent/main/", "/agent/settings/",
                           "/agent/downloads/report.html"):
                fields, body = request("box.test", target, 200, auth=True)
                assert body == b"upstream ok"
                if target.endswith("report.html"):
                    assert fields["content-disposition"] == "attachment", fields
                    assert fields["content-security-policy"] == "sandbox; frame-ancestors 'none'", fields
                else:
                    assert fields["content-security-policy"] == "frame-ancestors 'self'", fields
            # A proxy error must also pass through the error header handler.
            upstream.shutdown()
            upstream.server_close()
            request("box.test", "/", 502, auth=True)
            request("alias.test", "/agent/webhook/github", 502)
            print(f"PASS: {directory.name} redirects, auth, proxy, downloads and errors")
        except Exception:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            upstream.shutdown()
            upstream.server_close()
            worker.join(timeout=20)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    cfg = root / "config.json"
    cfg.write_text(json.dumps({
        "domain": "box.test", "web": {"enable": True, "aliases": ["alias.test"]},
        "users": {"agent": {"root": True}},
    }))
    native = (render(root, cfg) / "etc/agent-box/Caddyfile").read_text()
    exercise(native, root / "native")
    for index, argument in enumerate(sys.argv[1:]):
        exercise(Path(argument).read_text(), root / f"nixos-{index}")
