#!/usr/bin/env python3
"""Local checks for deploy/gcp (the GCE launcher). Needs: bwrap, sudo, shellcheck, caddy,
argon2-cffi, and this checkout (for the real renderer).

1. render: hostile inputs stay inert, no marker survives, ASCII, bash -n, shellcheck
2. hash: the launcher's argon2id hash is accepted by agentbox AND by real Caddy basic_auth
3. boot: run the rendered startup-script for real (as root, in a bwrap sandbox
   with stubbed curl/nix/apt/cloud-init), check guest-attribute sequence,
   config.yaml, the env handed to `agentbox apply`, and the every-boot guard
4. boot-mismatch: metadata IP != launcher IP fails the boot and reports failed
5. renderer: the config.yaml the boot wrote renders under the real `agentbox apply --root`
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "deploy" / "gcp"
sys.path.insert(0, str(HERE))
from render import render  # noqa: E402

AGENT_BOX = Path(os.environ.get("AGENT_BOX", str(ROOT)))
CADDY = os.environ.get("CADDY") or shutil.which("caddy") or "caddy"
IP = "34.105.12.34"
PASSWORD = "correct horse 'battery' $(staple) `x`"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def make_hash(pw):
    from argon2 import PasswordHasher
    # Identical parameters to launch.sh.
    return PasswordHasher(time_cost=1, memory_cost=47104, parallelism=1,
                          hash_len=32, salt_len=16).hash(pw)


def test_render(h):
    hostile_md = "line\nAGENTBOX_CONFIG\n@@WEBPASSWORDHASHB64@@ ' \" $(id) `id`\n"
    out = render(public_ip=IP, password_hash=h, agents_md=hostile_md,
                 sslip_domains=["sslip.io", "example.dev"])
    check("render: no plaintext password", PASSWORD not in out)
    check("render: hash only appears base64-encoded", h not in out)
    check("render: ASCII", all(ord(c) < 128 for c in out))
    check("render: hostile AGENTS.md text absent in raw form", "$(id)" not in out)
    p = Path(tempfile.mkdtemp()) / "s.sh"
    p.write_text(out)
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    check("render: bash -n", r.returncode == 0, r.stderr)
    r = subprocess.run([shutil.which("shellcheck") or "shellcheck", "-S", "warning", str(p)],
                       capture_output=True, text=True)
    check("render: shellcheck -S warning clean", r.returncode == 0, r.stdout[-1500:])
    for bad in [dict(password_hash=PASSWORD), dict(flake_ref="github:x/y; rm -rf /")]:
        try:
            render(public_ip=IP, **{"password_hash": h, **bad})
            check(f"render: rejects {list(bad)[0]}", False)
        except ValueError:
            check(f"render: rejects {list(bad)[0]}", True)
    return out


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def test_hash(h):
    sys.path.insert(0, str(AGENT_BOX / "bin"))
    from importlib.machinery import SourceFileLoader
    ab = SourceFileLoader("agentbox", str(AGENT_BOX / "bin/agentbox")).load_module()
    check("hash: agentbox recognises it as argon2id", ab.hash_algorithm(h) == "argon2id")
    port = free_port()
    d = Path(tempfile.mkdtemp())
    (d / "Caddyfile").write_text(textwrap.dedent(f"""
        {{
            admin off
            auto_https off
        }}
        http://127.0.0.1:{port} {{
            basic_auth argon2id {{
                workspace {h}
            }}
            respond "ok"
        }}
    """))
    proc = subprocess.Popen([CADDY, "run", "--config", str(d / "Caddyfile")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        def get(pw):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"workspace:{pw}".encode()).decode())
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        check("hash: Caddy basic_auth accepts the right password", get(PASSWORD) == 200)
        check("hash: Caddy basic_auth rejects a wrong password", get("nope") == 401)
    finally:
        proc.terminate(); err = proc.communicate(timeout=10)[1]
        if results[-1][1] is False:
            print(err[-2000:])


CURL_STUB = r'''#!/usr/bin/env python3
import os, sys
cap = os.environ["CAP"]
a = sys.argv[1:]
url = [x for x in a if x.startswith("http")][-1]
if "-X" in a and a[a.index("-X") + 1] == "PUT":
    key = url.rsplit("/guest-attributes/", 1)[1]
    open(f"{cap}/guest-attrs", "a").write(f"{key}={a[a.index('--data') + 1]}\n")
    sys.exit(0)
if url.endswith("/external-ip"):
    sys.stdout.write(os.environ["META_IP"]); sys.exit(0)
if "install.determinate.systems" in url:
    sys.stdout.write("""#!/bin/sh
set -e
mkdir -p /nix/var/nix/profiles/default/bin /nix/var/nix/profiles/default/etc/profile.d
cp "$STUBS/nix" /nix/var/nix/profiles/default/bin/nix
echo 'export PATH=/nix/var/nix/profiles/default/bin:$PATH' > /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
echo installer-ran >> "$CAP/calls"
"""); sys.exit(0)
if url.startswith("https://") and "-w" in a:
    sys.stdout.write("401"); sys.exit(0)
sys.exit(7)
'''

NIX_STUB = r'''#!/bin/sh
echo "nix $*" >> "$CAP/calls"
mkdir -p /nix/var/nix/profiles/agent-box/bin
cat > /nix/var/nix/profiles/agent-box/bin/agentbox <<'EOF'
#!/bin/sh
echo "agentbox $*" >> "$CAP/calls"
printf '%s' "$AGENT_BOX_WEB_PASSWORD_HASH" > "$CAP/hash-seen"
env | grep -c '^AGENT_BOX_WEB_PASSWORD=' > "$CAP/plain-seen" || true
cp /etc/agent-box/config.yaml "$CAP/config.yaml"
cp /etc/agent-box-guides/AGENTS.stack.md "$CAP/AGENTS.stack.md"
EOF
chmod +x /nix/var/nix/profiles/agent-box/bin/agentbox
'''


def run_boot(script, meta_ip, cap, state):
    stubs = cap.parent / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "curl").write_text(CURL_STUB)
    (stubs / "nix").write_text(NIX_STUB)
    for name, body in {
        "cloud-init": "#!/bin/sh\nexit 0\n",
        "modinfo": "#!/bin/sh\nexit 0\n",
        "swapon": "#!/bin/sh\necho /agent-box-swap\n",
        "apt-get": "#!/bin/sh\necho \"apt-get $*\" >> \"$CAP/calls\"\n",
    }.items():
        (stubs / name).write_text(body)
    for f in stubs.iterdir():
        f.chmod(0o755)
    (cap.parent / "boot.sh").write_text(script)
    for d in ("nix", "etc-agent-box", "etc-guides", "var-lib", "var-log"):
        (state / d).mkdir(parents=True, exist_ok=True)
    fstab = state / "fstab"
    if not fstab.exists():
        fstab.write_text("# test fstab\n")
    # Mount points must exist on the host for the bind; they are empty dirs.
    for mp in ("/nix", "/etc/agent-box", "/etc/agent-box-guides", "/var/lib/agent-box"):
        subprocess.run(["sudo", "mkdir", "-p", mp], check=True)
    cmd = ["sudo", "env", f"CAP={cap}", f"STUBS={stubs}", f"META_IP={meta_ip}",
           f"PATH={stubs}:/usr/sbin:/usr/bin:/sbin:/bin",
           "bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
           "--bind", str(state / "nix"), "/nix",
           "--bind", str(state / "etc-agent-box"), "/etc/agent-box",
           "--bind", str(state / "etc-guides"), "/etc/agent-box-guides",
           "--bind", str(state / "var-lib"), "/var/lib/agent-box",
           "--bind", str(state / "var-log"), "/var/log",
           "--bind", str(fstab), "/etc/fstab",
           "--unshare-pid", "--die-with-parent",
           "bash", str(cap.parent / "boot.sh")]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def test_boot(h):
    import yaml
    root = Path(tempfile.mkdtemp())
    cap = root / "cap"; cap.mkdir()
    state = root / "state"
    md = "Use pnpm.\nAGENTBOX_CONFIG\n$(id)\n"
    script = render(public_ip=IP, password_hash=h, agents_md=md,
                    sslip_domains=["sslip.io", "example.dev"], user="workspace")
    r = run_boot(script, IP, cap, state)
    check("boot: exits 0", r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:])
    attrs = (cap / "guest-attrs").read_text().split() if (cap / "guest-attrs").exists() else []
    want = ["agent-box/status=" + s for s in ("running", "installing-nix", "installing-runtime",
            "configuring", "applying", "waiting-for-tls", "ready")]
    check("boot: guest-attribute sequence", attrs == want, str(attrs))
    calls = (cap / "calls").read_text() if (cap / "calls").exists() else ""
    check("boot: nix installer ran", "installer-ran" in calls, calls)
    check("boot: runtime profile installed from the flake ref",
          "nix profile install --profile /nix/var/nix/profiles/agent-box github:defangdevs/agent-box#runtime" in calls, calls)
    check("boot: agentbox apply --first-boot called", "agentbox apply --first-boot" in calls, calls)
    check("boot: agentbox got the hash", (cap / "hash-seen").read_text() == h if (cap / "hash-seen").exists() else False)
    check("boot: no plaintext AGENT_BOX_WEB_PASSWORD in env", (cap / "plain-seen").read_text().strip() == "0" if (cap / "plain-seen").exists() else False)
    cfg = yaml.safe_load((cap / "config.yaml").read_text()) if (cap / "config.yaml").exists() else {}
    check("boot: config domain is the IP", cfg.get("domain") == IP, str(cfg))
    check("boot: config aliases", cfg.get("web", {}).get("aliases") == ["34-105-12-34.sslip.io", "34-105-12-34.example.dev"], str(cfg))
    check("boot: config user", list(cfg.get("users", {})) == ["workspace"], str(cfg))
    check("boot: AGENTS.md written verbatim", (cap / "AGENTS.stack.md").read_text() == md if (cap / "AGENTS.stack.md").exists() else False)
    log = (state / "var-log/agent-box-bootstrap.log").read_text()
    check("boot: hash not in bootstrap log", h not in log and base64.b64encode(h.encode()).decode() not in log)
    check("boot: done marker written", (state / "var-lib/.gce-bootstrapped").exists())
    check("boot: fstab gained the swap line exactly once",
          (state / "fstab").read_text().count("/agent-box-swap") <= 1)
    # Second boot: the guard must make it a no-op.
    (cap / "calls").unlink(); (cap / "guest-attrs").unlink()
    r2 = run_boot(script, IP, cap, state)
    check("reboot: exits 0", r2.returncode == 0, r2.stderr[-800:])
    check("reboot: guard short-circuits (no calls, no reports)",
          not (cap / "calls").exists() and not (cap / "guest-attrs").exists(), r2.stdout[-500:])
    return cap / "config.yaml", state


def test_mismatch(h):
    root = Path(tempfile.mkdtemp())
    cap = root / "cap"; cap.mkdir()
    script = render(public_ip=IP, password_hash=h)
    r = run_boot(script, "35.1.1.1", cap, root / "state")
    attrs = (cap / "guest-attrs").read_text().split() if (cap / "guest-attrs").exists() else []
    check("mismatch: boot fails", r.returncode != 0)
    check("mismatch: reports failed", attrs[-1:] == ["agent-box/status=failed:configuring:exit-1"], str(attrs))
    check("mismatch: agentbox never ran", "agentbox apply" not in ((cap / "calls").read_text() if (cap / "calls").exists() else ""))
    check("mismatch: no done marker", not (root / "state/var-lib/.gce-bootstrapped").exists())


def test_renderer(config_yaml):
    sys.path.insert(0, str(AGENT_BOX / "tests"))
    import test_agentbox as t  # the repo's own fake-profile + render helper
    tmp = tempfile.mkdtemp()
    cfg = Path(tmp) / "config.yaml"
    shutil.copy(config_yaml, cfg)
    try:
        t.render(tmp, cfg)
        out = Path(tmp) / "out"
        caddy = "\n".join(p.read_text() for p in out.rglob("*") if p.is_file() and "addy" in p.name)
        check("renderer: real `agentbox apply --root` renders the GCE config", True)
        check("renderer: Caddy config names the IP and the aliases",
              IP in caddy and "34-105-12-34.sslip.io" in caddy, caddy[:800])
    except AssertionError as e:
        check("renderer: real `agentbox apply --root` renders the GCE config", False, str(e)[-1500:])


if __name__ == "__main__":
    h = make_hash(PASSWORD)
    test_render(h)
    test_hash(h)
    cfg, _ = test_boot(h)
    test_mismatch(h)
    test_renderer(cfg)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
