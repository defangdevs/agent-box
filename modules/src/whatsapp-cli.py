"""Optional per-user WhatsApp linked-device runtime for agent-box."""

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


REV = "20e25996360cd0d82d09124d7a3816f113d602a8"
FILES = {
    "bridge.mjs": "76de0b8bd7cf9c583ae4e2ab62af5192f3b31aebe9dacbe84441aeae11cbaed3",
    "state.mjs": "05f4aa63ebb4dbe0be83d14b6bc04ac940d6f8ac2abe6c6e73e4cfc6c92ef6fc",
    "package.json": "2bae40c8aadfcaf160e275dfaec65eceacb33ca05db1bdb616144207f7fa3bb2",
    "package-lock.json": "90d22fc38b86ae7f3588505408e175e332145736a11b9da22c2dceb457747e21",
}
# local-router's single file, fetched with the bridge at the same REV. The
# route subcommand loads it from the runtime directory.
ROUTER_FILE = ("local-router/router.py", "ef913fc4c41109e6ed97b5ed6279fe2d491990a5de39a9087a7b83889ea2640c")
HOME = Path.home()
RUNTIME = HOME / ".local/share/local-whatsapp"
STATE = HOME / ".local/state/local-whatsapp"
READY = STATE / "ready"
NODE = HOME / ".nix-profile/bin/node"
NPM = HOME / ".nix-profile/bin/npm"
UNLINK_SETTLE = 2.5
UNLINK_TIMEOUT = 10


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
    router = RUNTIME / "router.py"
    if not router.is_file() or hashlib.sha256(router.read_bytes()).hexdigest() != ROUTER_FILE[1]:
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
        path, expected = ROUTER_FILE
        with urlopen(f"https://raw.githubusercontent.com/defangdevs/local-channels/{REV}/{path}",
                     timeout=30) as response:
            content = response.read(2_000_001)
        if len(content) > 2_000_000 or hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError("WhatsApp source integrity check failed for router.py")
        (stage / "router.py").write_bytes(content)
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
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else None
    if value != "default" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
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
    # The marker carries a pairing token. serve() hands it to the bridge, the
    # bridge's route calls inherit it, and a route from an EARLIER pairing
    # (route calls can outlive their bridge) refuses to write (take_route_lock).
    if not READY.is_file() or not pairing_token():
        pending = READY.with_name(READY.name + ".pending")
        pending.write_text(secrets.token_hex(16))
        pending.chmod(0o600)
        pending.replace(READY)


def pairing_token():
    """The pairing token, or "" when the marker has none (older pairings).
    Only exactly 32 hex characters count, so nothing else in that file can
    reach the bridge's environment."""
    try:
        raw = READY.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""
    return raw if re.fullmatch(r"[0-9a-f]{32}", raw) else ""


def bridge_connected():
    """Whether the supervised bridge still owns its control socket."""
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer.settimeout(1)
        peer.connect(str(STATE / "bridge.sock"))
        return True
    except OSError:
        return False
    finally:
        peer.close()


def unlink():
    """Stop the user bridge and forget this box's linked-device state.

    Removing the ready marker asks the supervisor to stop its child. Wait for
    that managed process before deleting its credentials and queued messages,
    or the still-live bridge could write either one back after this returns.
    The optional runtime stays installed, as every other connection's CLI does
    after logout, so pairing again does not require another download.
    """
    if STATE.is_symlink():
        raise RuntimeError("WhatsApp state cannot be a symlink")
    READY.unlink(missing_ok=True)
    if not STATE.exists():
        forget_route_state()
        return
    # The supervisor reconciles every two seconds. An absent socket before one
    # full tick does not prove a bridge is stopped: it may still be installing
    # the optional runtime on its way to execing bridge.mjs.
    time.sleep(UNLINK_SETTLE)
    deadline = time.monotonic() + UNLINK_TIMEOUT
    while bridge_connected():
        if time.monotonic() >= deadline:
            raise RuntimeError("WhatsApp bridge did not stop; try unlinking again")
        time.sleep(0.1)
    forget_route_state()
    shutil.rmtree(STATE)


def forget_route_state():
    """The router's history holds message text too; Unlink forgets it. A
    route call runs in its own process group and can outlive the bridge, so
    wait for it under the route lock; a call that gets the lock afterwards
    sees READY gone and writes nothing (see route())."""
    root = ROUTE_STATE.parent
    if not root.is_dir() or root.is_symlink():
        return
    with open(root / "lock", "a") as lock:
        deadline = time.monotonic() + ROUTE_UNLINK_WAIT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("a message route is still running; try unlinking again")
                time.sleep(0.2)
        shutil.rmtree(root)


def serve():
    """Refresh the optional runtime, then replace this process with its bridge.

    Unless AGENT_BOX_WHATSAPP_ROUTER is "off", the bridge hands each message
    to `agent-box-whatsapp route` (below): a self-chat has no threads, so the
    router chooses the session. The bridge needs one absolute executable with
    no arguments, so a two-line wrapper in the private runtime directory
    supplies the subcommand."""
    if not paired():
        raise RuntimeError("WhatsApp is not linked; pair the device first")
    install()
    env = dict(os.environ)
    env.pop("LOCAL_WHATSAPP_ROUTE_CMD", None)
    env["AGENT_BOX_WHATSAPP_PAIRING"] = pairing_token()
    if os.environ.get("AGENT_BOX_WHATSAPP_ROUTER", "").strip().lower() not in ("off", "0", "false", "no"):
        wrapper = RUNTIME / "route"
        self_path = Path(sys.argv[0]).resolve()
        wrapper.write_text("#!/bin/sh\nexec %s route\n" % shlex_quote(str(self_path)))
        wrapper.chmod(0o700)
        env["LOCAL_WHATSAPP_ROUTE_CMD"] = str(wrapper)
    os.execve(str(NODE), [str(NODE), str(RUNTIME / "bridge.mjs"), "serve"], env)  # NOSONAR - reviewed: env values are validated tokens and box paths


def shlex_quote(text):
    return "'" + text.replace("'", "'\"'\"'") + "'"


# ------------------------------------------------------------------ routing
#
# POLICY for local-router (the mechanism lives in local-channels). One call
# per inbound message, from the bridge (see local-whatsapp's
# LOCAL_WHATSAPP_ROUTE_CMD): JSON request on stdin, {"session": NAME, ...} on
# stdout. Nothing here is specific to WhatsApp except the channel name in the
# request; email and Slack can call the same command later.
#
# - Candidates are only the sessions the router started itself (recorded in
#   ROUTE_STATE). A session the user started by hand is never chosen.
# - CONTINUE: wake the session if it is stopped or hibernated, renew its
#   inbox:<name> subscription, and emit the message on the bus through the
#   owner-trusted `inbox` source, keyed on the session name. The spool holds
#   it while a woken session starts.
# - NEW / NEW_LINKS: start a session (origin webhook, so it takes a hook slot
#   and can hibernate like any event-started worker) whose kickoff prompt IS
#   the message, plus the linked sessions for NEW_LINKS.
# - Backend: AGENT_BOX_ROUTER_BACKEND (auto by default: the first signed-in
#   harness, Claude/Haiku first) and AGENT_BOX_ROUTER_MODEL. The probe result
#   is cached so a message does not pay for a probe call.

ROUTE_STATE = HOME / ".local/state/agent-box/router/state.json"
ROUTE_BACKEND_CACHE = HOME / ".local/state/agent-box/router/backend.json"
ROUTE_HISTORY_MAX = 40      # messages kept per session
ROUTE_SESSIONS_MAX = 40     # sessions remembered
ROUTE_BACKEND_TTL = 24 * 3600
ROUTE_NO_BACKEND_TTL = 600
# Per-call limits for discovery and routing: two probes (40 + 60 s) and one
# routing call stay inside the bridge's 180 s route deadline.
PROBE_LIMITS = {"claude": {"timeout": 40}, "codex": {"timeout": 60}}
ROUTE_DECISIONS_MAX = 300   # message ids remembered for replay
ROUTE_UNLINK_WAIT = 200     # longer than the bridge's 180 s route deadline
INBOX_TTL_HOURS = "8"       # local-webhook's longest session TTL; renewed on every delivery
SESSION_BIN = os.environ.get("AGENT_BOX_SESSION_BIN", "agent-box-session")
WEBHOOK_BIN = os.environ.get("AGENT_BOX_WEBHOOK_BIN", "agent-box-webhook")
PROFILE_BIN = os.environ.get("AGENT_BOX_PROFILE_BIN", "agent-box-profile")
# A session name never starts with "-", so it can never read as an option
# on the command lines below, whatever a model or the state file says.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,149}")


def checked_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise RuntimeError("invalid session name %r" % (name,))
    return name


def run_json(argv, env=None, timeout=60):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RouteError(result.returncode, (result.stderr or result.stdout).strip()[-500:])
    return json.loads(result.stdout) if result.stdout.strip() else None


class RouteError(RuntimeError):
    def __init__(self, code, detail):
        super().__init__(detail or "exit %d" % code)
        self.code = code


def load_router():
    spec = importlib.util.spec_from_file_location("local_router", str(RUNTIME / "router.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def router_backend(router):
    """The configured backend, or a lazy factory for auto (only called when a
    model decision is needed)."""
    name = os.environ.get("AGENT_BOX_ROUTER_BACKEND", "auto").strip().lower() or "auto"
    model = os.environ.get("AGENT_BOX_ROUTER_MODEL", "").strip()
    if name == "none":
        return None
    if name != "auto":
        if name not in router.BACKENDS:
            choices = ", ".join(sorted(router.BACKENDS))
            raise RuntimeError("AGENT_BOX_ROUTER_BACKEND must be auto, none or one of: " + choices)
        return router.BACKENDS[name](**({"model": model} if model else {}))

    def make(name):
        kwargs = dict(PROBE_LIMITS.get(name, {}))
        if model:
            kwargs["model"] = model
        return router.BACKENDS[name](**kwargs)

    def factory():
        try:
            cached = json.loads(ROUTE_BACKEND_CACHE.read_text())
            fresh = time.time() - cached["at"] < (ROUTE_BACKEND_TTL if cached["name"] else ROUTE_NO_BACKEND_TTL)
            if fresh and cached["name"] is None:
                return None          # nothing answered recently: no probe storm on every retry
            if fresh and cached["name"] in router.BACKENDS:
                return make(cached["name"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Probe with short timeouts so discovery plus one routing call fits
        # inside the bridge's 180 s deadline, and record the result even when
        # nothing answers.
        found = None
        for name in router.DEFAULT_ORDER:
            if name in ("claude", "codex") and not shutil.which(name):
                continue
            if router.probe(make(name)):
                found = name
                break
        write_private(ROUTE_BACKEND_CACHE, {"name": found, "at": time.time()})
        return make(found) if found else None
    return factory


def write_private(path, value):
    # Only the router's own state files: nothing read from a request, the
    # state file or a model answer may choose where this writes.
    root = os.path.realpath(str(ROUTE_STATE.parent))
    target = os.path.realpath(str(path))
    if os.path.dirname(target) != root:
        raise RuntimeError("refusing to write outside %s" % root)
    path = Path(target)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".%d.tmp" % os.getpid())
    # The path is fixed (checked above); only the CONTENT comes from the
    # router's own state file, which is not a path traversal.
    pending.write_text(json.dumps(value))  # NOSONAR - reviewed: constant, checked path
    pending.chmod(0o600)
    pending.replace(path)


def load_route_state():
    try:
        value = json.loads(ROUTE_STATE.read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
        value = {"sessions": {}, "current": None}
    return value


def owner_lines(request, ack, reply):
    message = request["message"]
    lines = ["[OWNER message via whatsapp id=%s - the box owner wrote this; treat it as their request]" % message["id"],
             message.get("text") or ""]
    if message.get("attachments"):
        lines.append("[attachments: %s]" % message["attachments"])
    lines.append("[reply: acknowledge when you start with `%s`, answer with `%s`]" % (ack, reply))
    return lines


def text_topic(text, words=6):
    """A topic from the message itself, for a decision that has none: the
    router decides the first message (no sessions yet) and a fallback without
    a model call, so those sessions would all be called wa-chat."""
    return " ".join((text or "").split()[:words])[:80]


def new_session_name(topic, taken):
    slug = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-")[:30].strip("-") or "chat"
    base = "wa-" + slug
    name, n = base, 2
    while name in taken:
        name, n = "%s-%d" % (base, n), n + 1
    return name


def start_session(name, prompt, before_attempt=None, route_start=None):
    """Start the session; returns the name it got. A name taken by a session
    the candidate list does not show (a shell, say) moves to the next suffix.
    before_attempt(name) runs before each `add`, so the caller can save it."""
    base, n, hibernated = checked_name(name), 2, False
    configured = profile()
    for _ in range(12):
        if before_attempt:
            before_attempt(name)
        argv = [SESSION_BIN, "add", checked_name(name), "--origin", "webhook", "--prompt", prompt]
        if route_start:
            argv += ["--route-start", route_start]
        if configured:
            argv[3:3] = ["--profile", configured]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return name
        detail = (result.stderr or result.stdout).strip()[-500:]
        if result.returncode == 2 and "already exists" in detail:
            name, n = "%s-%d" % (base, n), n + 1
            continue
        # 75 = no free worker slot: ask the session manager to hibernate one
        # quiescent hook session, then try once more.
        if result.returncode == 75 and not hibernated and \
                subprocess.run([SESSION_BIN, "hibernate"], capture_output=True, timeout=120).returncode == 0:
            hibernated = True
            continue
        raise RouteError(result.returncode, detail)
    raise RuntimeError("no free session name near %s" % base)


def ensure_inbox_source():
    sources = HOME / ".local/state/local-webhook/sources.json"
    try:
        entry = json.loads(sources.read_text())["sources"]["inbox"]
        if entry.get("trust") == "owner" and entry.get("keyPath") == "session":
            return
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    subprocess.run([WEBHOOK_BIN, "setup", "inbox"], capture_output=True, timeout=30, check=True)
    # setup keeps fields an existing entry already has, so check the result.
    try:
        entry = json.loads(sources.read_text())["sources"]["inbox"]
    except (OSError, ValueError, KeyError, TypeError):
        entry = None
    if not isinstance(entry, dict) or entry.get("trust") != "owner" or entry.get("keyPath") != "session":
        raise RuntimeError("the inbox source in %s must have keyPath \"session\" and trust \"owner\"" % sources)


def codex_thread(name):
    try:
        thread = json.loads((STATE / "codex-threads.json").read_text())[name]["thread"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return thread if isinstance(thread, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", thread) else None


def deliver(name, harness, request, ack, reply):
    """Wake NAME, renew its inbox subscription, emit the message on the bus."""
    name = checked_name(name)
    run_json([SESSION_BIN, "whatsapp", "wake", name], timeout=180)
    ensure_inbox_source()
    env = dict(os.environ, LOCAL_WEBHOOK_SESSION="%s-%s" % (os.environ.get("USER") or "", name))
    env.pop("LOCAL_WEBHOOK_CODEX_THREAD", None)
    if harness == "codex":
        thread = codex_thread(name)
        if thread is None:
            raise RuntimeError("no registered Codex thread for session %s yet" % name)
        env["LOCAL_WEBHOOK_CODEX_THREAD"] = thread
    subprocess.run([WEBHOOK_BIN, "subscribe", "inbox:" + name, "--ttl", INBOX_TTL_HOURS,
                    "--note", "owner messages the router sends to this session"],
                   capture_output=True, timeout=60, check=True, env=env)
    message = request["message"]
    payload = {"session": name, "text": message.get("text") or "", "channel": request.get("channel") or "",
               "id": message["id"], "attachments": message.get("attachments") or "",
               "reply": "acknowledge when you start with `%s`, answer with `%s`" % (ack, reply)}
    # On stdin, not as an argument: the payload carries the owner's text.
    # A stable delivery id per message: a replayed route re-emits the same
    # delivery, which the bus drops as already seen.
    delivery = "route-" + hashlib.sha256(message["id"].encode()).hexdigest()[:40]
    subprocess.run([WEBHOOK_BIN, "emit", "inbox", "-", "--event", "message", "--delivery-id", delivery],
                   input=json.dumps(payload), text=True, capture_output=True, timeout=60, check=True)


def route_request(request, router=None):
    message = request.get("message") if isinstance(request, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("id"), str) or \
            not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", message["id"]):
        raise RuntimeError("route request needs message.id")
    ack, reply = str(request.get("ack") or ""), str(request.get("reply") or "")
    live = {item["name"]: item for item in (run_json([SESSION_BIN, "whatsapp", "candidates"]) or [])
            if isinstance(item, dict) and NAME_RE.fullmatch(str(item.get("name", "")))}
    state = load_route_state()
    known = state["sessions"]
    decisions = state.setdefault("decisions", {})
    # Removed, malformed, or REPLACED sessions are forgotten: a session the
    # user re-creates under a router name has another registry id.

    def gone(n):
        if n not in live or not NAME_RE.fullmatch(n):
            return True
        return bool(known[n].get("id")) and live[n].get("id") != known[n].get("id")
    for name in [n for n in known if gone(n)]:
        del known[name]

    def remember(name, topic):
        """Record that this message went to NAME, and save. Idempotent, so a
        replay can restore what an interrupted call did not get to write."""
        entry = known.setdefault(name, {"messages": [], "startedAt": time.time()})
        if not entry.get("id"):
            entry["id"] = live.get(name, {}).get("id")
        msgs = list(entry.get("messages") or [])
        if not any(m.get("id") == message["id"] and m.get("role") == "user" for m in msgs):
            msgs.append({"id": message["id"], "role": "user", "text": (message.get("text") or "")[:2000],
                         "ts": message.get("ts"), "topic": topic or ""})
        entry["messages"] = msgs[-ROUTE_HISTORY_MAX:]
        state["current"] = name
        for old in list(decisions)[:max(0, len(decisions) - ROUTE_DECISIONS_MAX)]:
            del decisions[old]
        if len(known) > ROUTE_SESSIONS_MAX:
            def last(n):
                ms = known[n].get("messages") or []
                return (ms[-1].get("ts") or 0) if ms else known[n].get("startedAt", 0)
            for old in sorted(known, key=last)[:len(known) - ROUTE_SESSIONS_MAX]:
                del known[old]
        write_private(ROUTE_STATE, state)

    # A replay (the bridge restarted after this call delivered but before it
    # recorded the answer) repeats the recorded decision instead of deciding
    # again, and restores the history the interrupted call did not write.
    # Its side effects are idempotent: an existing session is not started
    # again, and the emit carries the same delivery id.
    done = decisions.get(message["id"])
    if isinstance(done, dict) and done.get("session") in live:
        name = done["session"]
        sid = live[name].get("id")
        if done.get("id"):
            # Only into the SAME session, never one re-created under the name.
            ours = sid == done["id"]
        else:
            # An interrupted start: adopt the session only if the registry
            # recorded this route's nonce for it (add --route-start). A session someone created by
            # hand under that name cannot, so it is decided again (fail closed).
            nonce = done.get("nonce") if done.get("action") != "CONTINUE" else None
            ours = bool(nonce) and live[name].get("start") == nonce
        if ours:
            done["id"] = sid
            if done.get("action") == "CONTINUE":
                deliver(name, live[name]["harness"], request, ack, reply)
            remember(name, done.get("topic"))
            return {"session": name, "action": done.get("action"), "links": done.get("links") or [],
                    "reason": "replay"}
    # Answers the channel already sent become the assistant side of history.
    for item in request.get("recent") or []:
        if not isinstance(item, dict):
            continue
        msgs = (known.get(item.get("session")) or {}).get("messages") or []
        for i, m in enumerate(msgs):
            if m.get("id") == item.get("id") and m.get("role") == "user":
                if not (i + 1 < len(msgs) and msgs[i + 1].get("role") == "assistant"):
                    msgs.insert(i + 1, {"role": "assistant", "text": str(item.get("reply") or "")[:2000],
                                        "ts": m.get("ts")})
                break
    router = router or load_router()
    sessions = [{"id": name, "messages": known[name].get("messages") or []} for name in known]
    current = state.get("current") if state.get("current") in known else None
    route_message = {"text": message.get("text") or "", "ts": message.get("ts")}
    # The bridge resolves a WhatsApp quote to an earlier inbound message. Only
    # trust a recorded decision whose session is still the same registry entry.
    quoted = message.get("reply_to_message")
    previous = decisions.get(quoted) if isinstance(quoted, str) else None
    if isinstance(previous, dict):
        name, sid = previous.get("session"), previous.get("id")
        if isinstance(name, str) and name in known and sid and live[name].get("id") == sid:
            route_message["reply_to"] = name
    decision = router.route(route_message, sessions, current, backend=router_backend(router))
    if decision.get("reason") == "fallback" and decision.get("error"):
        try:
            ROUTE_BACKEND_CACHE.unlink()
        except OSError:
            pass
    topic = decision.get("topic") or text_topic(message.get("text"))
    if decision["action"] == "CONTINUE":
        name = decision["session"]
        # Recorded BEFORE the delivery, so a crash after it replays, not re-decides.
        decisions[message["id"]] = {"session": name, "action": "CONTINUE", "links": [],
                                    "id": live[name].get("id"), "topic": topic}
        write_private(ROUTE_STATE, state)
        deliver(name, live[name]["harness"], request, ack, reply)
    else:
        name = new_session_name(topic, set(live) | set(known))
        lines = owner_lines(request, ack, reply)
        if decision["action"] == "NEW_LINKS" and decision.get("links"):
            lines.append("Earlier sessions with related context (read their transcripts if you need the details):")
            for link in decision["links"]:
                topics = [m.get("topic") for m in known.get(link, {}).get("messages", []) if m.get("topic")]
                lines.append("- %s: %s" % (link, "; ".join(dict.fromkeys(topics[-3:])) or "no topic labels"))
        lines.append("(This session was started by the message router for this topic. Later owner messages "
                     "on the same topic arrive here as OWNER messages via inbox.)")
        # A durable mark of THIS start: `add --route-start` stores it in the
        # registry for the session's life, and `whatsapp candidates` reads it
        # back as "start" (the kickoff prompt is cleared at the first spawn).
        nonce = secrets.token_hex(16)

        def attempt(candidate):
            # Saved before EACH start attempt, so an interrupted start under
            # a suffixed name is found again by a replay.
            decisions[message["id"]] = {"session": candidate, "action": decision["action"],
                                        "links": decision.get("links") or [], "id": None, "topic": topic,
                                        "nonce": nonce}
            write_private(ROUTE_STATE, state)
        name = start_session(name, "\n".join(lines), attempt, route_start=nonce)
        started = (run_json([SESSION_BIN, "whatsapp", "candidates"]) or [])
        sid = next((i.get("id") for i in started if isinstance(i, dict) and i.get("name") == name), None)
        live[name] = {"name": name, "id": sid}
        decisions[message["id"]]["id"] = sid
    remember(name, topic)
    return {"session": name, "action": decision["action"], "links": decision.get("links") or [],
            "reason": decision.get("reason")}


def route():
    request = json.loads(sys.stdin.read(400_000))
    ROUTE_STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(ROUTE_STATE.with_name("lock"), "a") as lock:
        take_route_lock(lock)
        print(json.dumps(route_request(request)))


def take_route_lock(lock):
    """Lock, then prove the lock still means this pairing. Unlink removes
    the router directory, lock file included; a call that opened the old file
    before that would otherwise lock a deleted inode with no contention and,
    after a new pairing, write into the new state. So the file we hold must
    still be the one at the path, and WhatsApp must still be linked."""
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        current = os.stat(ROUTE_STATE.with_name("lock"))
    except OSError:
        current = None
    held = os.fstat(lock.fileno())
    if current is None or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        raise RuntimeError("WhatsApp was unlinked while this route waited; not routing")
    if not READY.is_file():
        raise RuntimeError("WhatsApp is unlinked; not routing")
    # Bound to the pairing the bridge was started for: a call from before an
    # Unlink and a new pairing carries the old token, whenever it got here.
    if os.environ.get("AGENT_BOX_WHATSAPP_PAIRING") != pairing_token():
        raise RuntimeError("this route belongs to an earlier WhatsApp pairing; not routing")


def pair():
    raw = os.environ.get("LOCAL_WHATSAPP_PHONE", "")
    phone = re.sub(r"[ ()+.-]", "", raw)
    if not (phone.isascii() and phone.isdigit()) or not 7 <= len(phone) <= 15:
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
    if len(sys.argv) != 2 or sys.argv[1] not in (
            "install", "pair", "activate", "serve", "status", "profile", "unlink", "route"):
        raise RuntimeError(
            "usage: agent-box-whatsapp install|pair|activate|serve|status|profile|unlink|route [NAME|default]")
    if sys.argv[1] == "profile":
        print(json.dumps({"profile": profile()}))
        return
    {"install": install, "pair": pair, "activate": activate,
     "serve": serve, "status": status, "unlink": unlink, "route": route}[sys.argv[1]]()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("agent-box-whatsapp: " + str(error), file=sys.stderr)
        sys.exit(1)
