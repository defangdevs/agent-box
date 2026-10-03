"""Optional per-user WhatsApp linked-device runtime for agent-box."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen


REV = "52059e30642be1b0ee04c8f4401d21c7d932fc67"
FILES = {
    "bridge.mjs": "1526b8e2b4784edb95a6a5f3a9337d5e19e0821c83ae522353965023d1e90d9a",
    "state.mjs": "4f5125000fbb44b43c9dc7909ee293c61b5c3a6ae44f83620bd506470344e81b",
    "package.json": "2ee16b0da02a289bf68d71811c39f51e27a2f16e9a810690b69c3091fab28df1",
    "package-lock.json": "d030965125393662c5effbea6e25c98512e9fd29e470343010096ec413096110",
}
HOME = Path.home()
RUNTIME = HOME / ".local/share/local-whatsapp"
STATE = HOME / ".local/state/local-whatsapp"
READY = STATE / "ready"
NODE = HOME / ".nix-profile/bin/node"
NPM = HOME / ".nix-profile/bin/npm"


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("WhatsApp directory cannot be a symlink")
    path.chmod(0o700)


def ensure_node():
    if NODE.is_file() and NPM.is_file():
        return
    nix = (shutil.which("nix") or "/nix/var/nix/profiles/default/bin/nix")
    if not Path(nix).is_file():
        raise RuntimeError("Nix is unavailable; cannot install optional Node runtime")
    print("Installing optional Node runtime for WhatsApp; this may take a few minutes.", flush=True)
    subprocess.run([nix, "profile", "add", "--profile", str(HOME / ".nix-profile"),
                    "nixpkgs#nodejs_22"], check=True, timeout=900)
    if not NODE.is_file() or not NPM.is_file():
        raise RuntimeError("Node installation finished without node and npm")


def runtime_matches():
    if not (RUNTIME / "node_modules").is_dir():
        return False
    for name, expected in FILES.items():
        source = RUNTIME / name
        if not source.is_file():
            return False
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            return False
    return True


def install():
    ensure_node()
    backup = RUNTIME.with_name(RUNTIME.name + ".previous")
    pending = RUNTIME.with_name(RUNTIME.name + ".pending")
    if any(path.is_symlink() for path in (RUNTIME, backup, pending)):
        raise RuntimeError("WhatsApp runtime cannot be a symlink")
    if not RUNTIME.exists():
        for candidate in (pending, backup):
            if candidate.exists():
                candidate.rename(RUNTIME)
                break
    if runtime_matches():
        for old in (pending, backup):
            if old.exists():
                shutil.rmtree(old)
        return
    private_dir(RUNTIME.parent)
    with tempfile.TemporaryDirectory(prefix="local-whatsapp-", dir=RUNTIME.parent) as raw:
        stage = Path(raw)
        for name, expected in FILES.items():
            url = (f"https://raw.githubusercontent.com/defangdevs/local-channels/"
                   f"{REV}/local-whatsapp/{name}")
            with urlopen(url, timeout=30) as response:
                content = response.read(2_000_001)
            if len(content) > 2_000_000 or hashlib.sha256(content).hexdigest() != expected:
                raise RuntimeError("WhatsApp source integrity check failed for " + name)
            (stage / name).write_bytes(content)
        print("Installing pinned WhatsApp dependencies.", flush=True)
        env = dict(os.environ)
        env["PATH"] = str(NODE.parent) + os.pathsep + env.get("PATH", "")
        subprocess.run([str(NPM), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                       cwd=stage, env=env, check=True, timeout=900)
        if any(path.is_symlink() for path in (RUNTIME, backup, pending)):
            raise RuntimeError("WhatsApp runtime cannot be a symlink")
        if pending.exists() and RUNTIME.exists():
            shutil.rmtree(pending)
        if RUNTIME.exists():
            RUNTIME.rename(pending)
        try:
            stage.rename(RUNTIME)
        except OSError:
            if pending.exists() and not RUNTIME.exists():
                pending.rename(RUNTIME)
            raise
        for old in (pending, backup):
            if old.exists():
                shutil.rmtree(old)
    RUNTIME.chmod(0o700)


def paired():
    path = STATE / "auth/creds.json"
    try:
        return bool(json.loads(path.read_text()).get("me"))
    except (OSError, ValueError, AttributeError):
        return False


def profile(value=None):
    config = STATE / "config.json"
    if value is None:
        try:
            data = json.loads(config.read_text())
            value = data.get("profile") if isinstance(data, dict) else None
        except (OSError, ValueError, AttributeError):
            value = None
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else None
    if value != "default" and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise RuntimeError("WhatsApp profile must be a profile name or 'default'")
    private_dir(STATE)
    if config.is_symlink():
        raise RuntimeError("WhatsApp configuration cannot be a symlink")
    pending = config.with_name(config.name + ".pending")
    pending.write_text(json.dumps({"profile": None if value == "default" else value}))
    pending.chmod(0o600)
    pending.replace(config)
    return profile()


def status():
    connected = False
    if NODE.is_file() and (RUNTIME / "bridge.mjs").is_file():
        try:
            result = subprocess.run([str(NODE), str(RUNTIME / "bridge.mjs"), "status"],
                                    capture_output=True, text=True, timeout=5, check=False)
            connected = result.returncode == 0 and json.loads(result.stdout).get("connected") is True
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    print(json.dumps({"connected": connected, "paired": paired(),
                      "enabled": READY.is_file(), "profile": profile()}))


def activate():
    if not paired():
        raise RuntimeError("WhatsApp is not linked; pair the device first")
    private_dir(STATE)
    READY.touch(mode=0o600)


def pair():
    phone = re.sub(r"\D", "", os.environ.get("LOCAL_WHATSAPP_PHONE", ""))
    if not 7 <= len(phone) <= 15:
        raise RuntimeError("Set LOCAL_WHATSAPP_PHONE to international digits before pairing")
    install()
    private_dir(STATE)
    subprocess.run([str(NODE), str(RUNTIME / "bridge.mjs"), "pair"], check=True,
                   timeout=240, env=dict(os.environ, LOCAL_WHATSAPP_PHONE=phone))
    activate()
    print("WhatsApp linked. The agent-box supervisor is starting the bridge.", flush=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "profile":
        print(json.dumps({"profile": profile(sys.argv[2])}))
        return
    if len(sys.argv) != 2 or sys.argv[1] not in ("install", "pair", "activate", "status", "profile"):
        raise RuntimeError("usage: agent-box-whatsapp install|pair|activate|status|profile [NAME|default]")
    if sys.argv[1] == "profile":
        print(json.dumps({"profile": profile()}))
        return
    {"install": install, "pair": pair, "activate": activate, "status": status}[sys.argv[1]]()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("agent-box-whatsapp: " + str(error), file=sys.stderr)
        sys.exit(1)
