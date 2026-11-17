"""Tests for modules/src/tailscale-cli.py with fake tailscale and systemctl.

Nothing here needs root, a network or a real tailnet: the fakes record their
arguments and answer `status --json` from a file the test writes.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "modules/src/tailscale-cli.py"

FAKE_TAILSCALE = """#!%(python)s
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("tailscale " + json.dumps(args) + "\\n")
if "--socket" in args:
    i = args.index("--socket")
    args = args[:i] + args[i + 2:]
if args[:1] == ["up"]:
    for a in args:
        if a.startswith("file:"):
            with open(a[5:]) as key:
                open(os.environ["FAKE_LOG"], "a").write("key " + key.read() + "\\n")
    sys.exit(int(os.environ.get("FAKE_UP_STATUS", "0")))
if args[:1] == ["status"]:
    print(open(os.environ["FAKE_STATUS"]).read())
"""

FAKE_SYSTEMCTL = """#!%(python)s
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("systemctl " + json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_SYSTEMCTL_STATUS", "0")))
"""


class TailscaleCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        (self.home / "sites").mkdir(parents=True)
        self.pkg = root / "pkg"
        (self.pkg / "bin").mkdir(parents=True)
        self.tools = root / "tools"
        self.tools.mkdir()
        python = sys.executable
        for path, text in ((self.pkg / "bin/tailscale", FAKE_TAILSCALE),
                           (self.tools / "systemctl", FAKE_SYSTEMCTL)):
            path.write_text(text % {"python": python})
            path.chmod(0o755)
        self.socket = root / "tailscaled.sock"
        self.socket.write_text("")
        self.state_dir = root / "var-lib"
        self.state_dir.mkdir()
        self.log = root / "log"
        self.status = root / "status.json"
        self.set_status("Running", "box.tail.test.", [])
        self.env = {
            "HOME": str(self.home), "USER": "owner",
            "PATH": str(self.tools) + os.pathsep + os.environ.get("PATH", ""),
            "AGENT_BOX_TAILSCALE_PKG": str(self.pkg),
            "AGENT_BOX_TAILSCALE_SOCKET": str(self.socket),
            "AGENT_BOX_TAILSCALE_STATE_DIR": str(self.state_dir),
            "FAKE_LOG": str(self.log), "FAKE_STATUS": str(self.status),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def set_status(self, state, name, cert_domains):
        self.status.write_text(json.dumps({
            "BackendState": state, "Self": {"DNSName": name},
            "CertDomains": cert_domains}))

    def run_cli(self, *args, stdin="", **extra):
        env = dict(self.env, **extra)
        return subprocess.run([sys.executable, "-I", str(CLI)] + list(args),
                              input=stdin, capture_output=True, text=True,
                              env=env, timeout=60, check=False)

    def calls(self):
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()

    @property
    def state(self):
        return self.home / ".local/state/agent-box-tailscale"

    def test_configure_validates_login_server(self):
        for bad in ("http://hs.example", "https://hs.example/path",
                    "https://user@hs.example", "https://hs.example:99999"):
            proc = self.run_cli("configure", "--login-server", bad)
            self.assertNotEqual(proc.returncode, 0, bad)
        proc = self.run_cli("configure", "--login-server", "https://hs.example:8443/")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = json.loads((self.state / "config.json").read_text())
        self.assertEqual(config["loginServer"], "https://hs.example:8443")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_configure_auth_key_from_stdin(self):
        proc = self.run_cli("configure", "--auth-key-stdin", stdin="tskey-auth-abc123\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        key = self.state / "authkey"
        self.assertEqual(key.read_text(), "tskey-auth-abc123")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        proc = self.run_cli("configure", "--auth-key-stdin", stdin="bad key; rm\n")
        self.assertNotEqual(proc.returncode, 0)
        # An empty field clears a key left from an abandoned attempt.
        proc = self.run_cli("configure", "--auth-key-stdin", stdin="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(key.exists())

    def test_connect_headscale_uses_local_ca(self):
        self.run_cli("configure", "--login-server", "https://hs.example",
                     "--auth-key-stdin", stdin="hskey-auth-12345678")
        proc = self.run_cli("connect")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertIn('systemctl ["--no-ask-password", "start", "agent-box-tailscale.service"]',
                      calls)
        up = [json.loads(c[len("tailscale "):]) for c in calls
              if c.startswith("tailscale ") and '"up"' in c][0]
        for flag in ("--reset", "--operator=owner", "--ssh=false",
                     "--accept-routes=false", "--accept-dns=false",
                     "--netfilter-mode=off"):
            self.assertIn(flag, up)
        self.assertEqual(up[up.index("--login-server") + 1], "https://hs.example")
        self.assertIn("key hskey-auth-12345678", calls)
        # The key file is gone once the node has joined.
        self.assertFalse((self.state / "authkey").exists())
        self.assertTrue((self.state / "enabled").is_file())
        site = (self.home / "sites/agent-box-tailnet.caddy").read_text()
        self.assertIn("https://box.tail.test {", site)
        self.assertIn("tls internal", site)
        self.assertIn("import agent_box_routes", site)
        self.assertIn('systemctl ["--no-ask-password", "reload", "caddy.service"]', calls)
        self.assertIn("https://box.tail.test/owner/", proc.stdout)

    def test_connect_ts_net_with_https_gets_trusted_certificate(self):
        self.set_status("Running", "box.tail1234.ts.net.", ["box.tail1234.ts.net"])
        proc = self.run_cli("connect")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        site = (self.home / "sites/agent-box-tailnet.caddy").read_text()
        self.assertIn("https://box.tail1234.ts.net {", site)
        self.assertNotIn("tls internal", site)
        up = [c for c in self.calls() if '"up"' in c][0]
        self.assertNotIn("--login-server", up)
        self.assertNotIn("--auth-key", up)

    def test_connect_ts_net_without_https_falls_back_to_local_ca(self):
        self.set_status("Running", "box.tail1234.ts.net.", [])
        proc = self.run_cli("connect")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        site = (self.home / "sites/agent-box-tailnet.caddy").read_text()
        self.assertIn("tls internal", site)
        self.assertIn("turn on HTTPS", proc.stdout)

    def test_failed_up_still_deletes_key_and_writes_no_site(self):
        self.run_cli("configure", "--auth-key-stdin", stdin="tskey-auth-abc123")
        proc = self.run_cli("connect", FAKE_UP_STATUS="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.state / "authkey").exists())
        self.assertFalse((self.home / "sites/agent-box-tailnet.caddy").exists())

    def test_refused_unit_start_stops_before_up(self):
        proc = self.run_cli("connect", FAKE_SYSTEMCTL_STATUS="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(any('"up"' in c for c in self.calls()))

    def test_unsafe_dns_name_is_never_written_into_caddy(self):
        self.set_status("Running", "box.tail.test {\n  respond hi\n}.", [])
        proc = self.run_cli("connect")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.home / "sites/agent-box-tailnet.caddy").exists())

    def test_status(self):
        self.run_cli("configure", "--login-server", "https://hs.example")
        proc = self.run_cli("status")
        data = json.loads(proc.stdout)
        self.assertEqual(data["backendState"], "Running")
        self.assertEqual(data["dnsName"], "box.tail.test")
        self.assertEqual(data["url"], "https://box.tail.test/owner/")
        self.assertEqual(data["loginServer"], "https://hs.example")
        self.assertFalse(data["enabled"])
        self.socket.unlink()
        data = json.loads(self.run_cli("status").stdout)
        self.assertEqual(data["backendState"], "")
        self.assertEqual(data["url"], "")

    def test_disconnect(self):
        self.run_cli("connect")
        self.log.unlink()
        proc = self.run_cli("disconnect")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertTrue(any('"logout"' in c for c in calls))
        self.assertIn('systemctl ["--no-ask-password", "stop", "agent-box-tailscale.service"]',
                      calls)
        self.assertFalse((self.home / "sites/agent-box-tailnet.caddy").exists())
        self.assertFalse((self.state / "enabled").exists())

    def test_stopped_erases_state_only_after_disconnect(self):
        (self.state_dir / "tailscaled.state").write_text("{}")
        (self.state_dir / "certs").mkdir()
        marker = self.home / "marker"
        marker.write_text("")
        env = {"AGENT_BOX_TAILSCALE_MARKER": str(marker)}
        self.assertEqual(self.run_cli("stopped", **env).returncode, 0)
        self.assertTrue((self.state_dir / "tailscaled.state").exists())
        marker.unlink()
        self.assertEqual(self.run_cli("stopped", **env).returncode, 0)
        self.assertEqual(list(self.state_dir.iterdir()), [])
        self.assertTrue(self.state_dir.is_dir())

    def test_set_operator_needs_a_valid_owner(self):
        proc = self.run_cli("set-operator", AGENT_BOX_TAILSCALE_OWNER="bad owner")
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_cli("set-operator", AGENT_BOX_TAILSCALE_OWNER="owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any('"set", "--operator=owner"' in c for c in self.calls()))

    def test_node_name_is_a_dns_label(self):
        sys.path.insert(0, str(CLI.parent))
        import importlib.util
        spec = importlib.util.spec_from_file_location("tailscale_cli", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.socket.gethostname = lambda: "Agent_Box.Example--"
        self.assertEqual(module.node_name(), "agent-box-example")


if __name__ == "__main__":
    unittest.main(verbosity=2)
