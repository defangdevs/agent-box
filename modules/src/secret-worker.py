"""agent-box-secret-worker - the fixed, unprivileged executable a job runs
as (issue #922, PR 2 slice).

Launched already as the agent user (systemd's `User=` in production,
DirectLauncher's plain subprocess in dev/test mode - see
lib/secret-jobs.py). It reads ONE length-prefixed JSON frame from its own
stdin - the request plus the resolved secret values, never a uid, a path,
or anything the caller chose beyond what the broker already validated - then
resets stdin to /dev/null (there is no interactive stdin or TTY in v1) and
execs the real command. Everything after that point is the workload: its
stdout/stderr flow back to the broker exactly the way any child process's
would, through the pipes the launcher already wired up, which is also where
redaction happens (lib/secret-jobs.py's _drain), not here - this process
never needs to know which of its own bytes happen to be secret.

Never logs, never writes the frame or the resolved values anywhere but the
child's own environment.

Deliberately standalone: this file needs nothing from secret-protocol.py (no
`cryptography`, no Store) and is not spliced with it, so its own dependency
closure stays as small as the ordinary agent user's own python3 - the broker
and the crypto library are what need isolating, not this one.
"""
import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import urllib.parse


def read_frame(stream):
    header = stream.read(4)
    if len(header) != 4:
        sys.stderr.write("agent-box-secret-worker: no request frame\n")
        raise SystemExit(2)
    (length,) = struct.unpack(">I", header)
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


# The packaged build replaces the placeholder with the store path of bash
# (a literal /bin/bash does not exist on NixOS). Unpackaged runs - tests, a
# root-less dev box - leave it alone and fall back to what is on this host.
BASH_PLACEHOLDER = "@" + "BASH@"
PACKAGED_BASH = "@BASH@"


def _bash_path():
    if PACKAGED_BASH != BASH_PLACEHOLDER:
        return PACKAGED_BASH
    import shutil
    return shutil.which("bash") or "/bin/bash"


# --- Built-in browser jobs (fill, save-cookies, load-cookies) ----------------
#
# A browser job runs THIS fixed code instead of a caller's command. It talks
# Chrome DevTools Protocol to the agent's own browser on a LOOPBACK port -
# the host is fixed here, only the port comes from the request - and it
# checks the page's origin, as the browser itself reports it, against the
# origins stored with the secret before a value goes anywhere.
#
# What that buys, and what it does not: it stops a value from being typed
# into a page the agent did not mean to be on (a lookalike domain, a
# redirect, an instruction injected into what the agent read). It does not
# stop the agent itself, which controls this same browser and can read a
# filled field back. See the PR and issue #922.
#
# stdlib only (see the header): a minimal RFC 6455 client is enough for
# CDP's unfragmented JSON text messages.

CDP_HOST = "127.0.0.1"
CDP_TIMEOUT_S = 15
CDP_MAX_MESSAGE = 16 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class BrowserJobError(Exception):
    """A refusal or failure to report; never carries a secret value."""

    def __init__(self, message, code=4):
        super().__init__(message)
        self.code = code


def _http_json(port, path):
    conn = http.client.HTTPConnection(CDP_HOST, port, timeout=CDP_TIMEOUT_S)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read(CDP_MAX_MESSAGE)
        if resp.status != 200:
            raise BrowserJobError(f"{path} answered HTTP {resp.status}")
        return json.loads(body.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise BrowserJobError(f"no DevTools endpoint on {CDP_HOST}:{port}: {error}")
    finally:
        conn.close()


def _pick_target(port, target_id):
    if target_id:
        return target_id
    pages = [t for t in _http_json(port, "/json/list") if t.get("type") == "page"]
    if len(pages) != 1:
        raise BrowserJobError(
            f"{len(pages)} pages are open; pass targetId (see http://{CDP_HOST}:{port}/json/list)")
    return pages[0]["id"]


class Cdp:
    def __init__(self, port, target_id):
        self.sock = socket.create_connection((CDP_HOST, port), timeout=CDP_TIMEOUT_S)
        self.next_id = 0
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        # No Origin header: Chromium refuses a DevTools websocket that sends
        # one it was not told to allow, and accepts one that sends none.
        self.sock.sendall((
            f"GET /devtools/page/{target_id} HTTP/1.1\r\n"
            f"Host: {CDP_HOST}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii"))
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise BrowserJobError(f"DevTools refused target {target_id}: {status.decode('latin-1')}")
        expect = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest())  # NOSONAR - RFC 6455 handshake digest, not a security use
        if expect not in head:
            raise BrowserJobError("DevTools handshake failed")

    def _read_until(self, marker):
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BrowserJobError("DevTools connection closed")
            self.buffer += chunk
            if len(self.buffer) > 65536 and marker == b"\r\n\r\n":
                raise BrowserJobError("DevTools handshake too large")
        head, self.buffer = self.buffer.split(marker, 1)
        return head

    def _read_exact(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self.buffer)))
            if not chunk:
                raise BrowserJobError("DevTools connection closed")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_message(self):
        message = b""
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exact(8))
            if length > CDP_MAX_MESSAGE or len(message) + length > CDP_MAX_MESSAGE:
                raise BrowserJobError("DevTools message too large")
            mask = self._read_exact(4) if second & 0x80 else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise BrowserJobError("DevTools connection closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return json.loads(message.decode("utf-8"))

    def call(self, method, **params):
        self.next_id += 1
        call_id = self.next_id
        self._send_frame(0x1, json.dumps({"id": call_id, "method": method,
                                          "params": params}).encode("utf-8"))
        while True:
            message = self._recv_message()
            if message.get("id") != call_id:
                continue  # an event, or a stale reply
            if "error" in message:
                error = message["error"]
                raise BrowserJobError(f"{method}: {error.get('message', 'failed')}")
            return message.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _main_frame(cdp):
    frame = cdp.call("Page.getFrameTree")["frameTree"]["frame"]
    return frame["id"], frame.get("loaderId"), frame.get("securityOrigin")


def _host_matches(cookie_domain, origins):
    """True if a cookie for cookie_domain would be sent to one of origins
    (the host itself, or a parent domain it sits under)."""
    domain = cookie_domain.lstrip(".").lower()
    for origin in origins:
        host = (urllib.parse.urlsplit(origin).hostname or "").lower()
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


# Runs as `this` = the resolved input. Refuses unless the node is still in
# the main document of the checked origin, editable, and takes focus; then
# sets the value through the element's own prototype setter (what a
# framework's value tracking sees, as typing would) and fires input and
# change. focus() runs the page's focus handlers synchronously, and they can
# detach the node or make it read-only, so every check runs again AFTER it.
FILL_FUNCTION = (
    "function (origin, value) {"
    " var el = this;"
    " function check() {"
    "  if (window.top !== window || self.origin !== origin) return 'origin';"
    "  if (!el.isConnected || el.ownerDocument !== document) return 'detached';"
    "  if (el.disabled || el.readOnly) return 'not editable';"
    "  return null; }"
    " var refused = check(); if (refused) return refused;"
    " this.focus();"
    " refused = check(); if (refused) return refused;"
    " if (document.activeElement !== this) return 'no focus';"
    " var proto = this instanceof HTMLTextAreaElement"
    "   ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
    " Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, value);"
    " this.dispatchEvent(new InputEvent('input',"
    "   {bubbles: true, inputType: 'insertReplacementText'}));"
    " this.dispatchEvent(new Event('change', {bubbles: true}));"
    " return 'ok'; }")


def _partition_key(cookie):
    """(ok, key): a cookie's CDP partitionKey in the one shape that can be
    stored and set again ({topLevelSite, hasCrossSiteAncestor}), None for
    an unpartitioned cookie, or ok=False for a partition that cannot be
    carried (opaque, or a shape this code does not know). Such a cookie is
    skipped: restoring it without its partition would widen where the
    browser sends it."""
    if cookie.get("partitionKeyOpaque"):
        return False, None
    key = cookie.get("partitionKey")
    if key is None:
        return True, None
    if not isinstance(key, dict) or not set(key) <= {"topLevelSite", "hasCrossSiteAncestor"}:
        return False, None
    if not isinstance(key.get("topLevelSite"), str):
        return False, None
    if not isinstance(key.get("hasCrossSiteAncestor", False), bool):
        return False, None
    return True, dict(key)


def _all_cookies(cdp):
    """Every cookie in the browser context, not only the ones a URL would
    get: Network.getCookies filters by the URL's path, so a session cookie
    scoped to Path=/app is invisible to a query for origin + "/". The
    caller filters by host."""
    try:
        return cdp.call("Storage.getCookies")["cookies"]
    except BrowserJobError:
        return cdp.call("Network.getAllCookies")["cookies"]


def _browser_fill(cdp, spec, value):
    origins = spec["origins"]
    frame_id, loader_id, origin = _main_frame(cdp)
    if origin not in origins:
        raise BrowserJobError(
            f"refused: the page origin is {origin}, not one this secret is for "
            f"({', '.join(origins)})", code=3)
    root = cdp.call("DOM.getDocument", depth=0)["root"]["nodeId"]
    node_id = cdp.call("DOM.querySelector", nodeId=root, selector=spec["selector"])["nodeId"]
    if not node_id:
        raise BrowserJobError(f"no element matches {spec['selector']}")
    node = cdp.call("DOM.describeNode", nodeId=node_id)["node"]
    if node.get("localName") not in ("input", "textarea"):
        raise BrowserJobError(f"{spec['selector']} is a <{node.get('localName')}>, not an input")
    object_id = cdp.call("DOM.resolveNode", nodeId=node_id)["object"]["objectId"]
    # Re-read the frame before the value goes in: a navigation since the
    # first check gives a new loaderId, and the value is not sent.
    if _main_frame(cdp) != (frame_id, loader_id, origin):
        raise BrowserJobError("refused: the page navigated during the fill", code=3)
    # Check and write in ONE call on the resolved node, never through
    # Input.insertText: that types into whatever has focus when it lands,
    # which page script can move after any separate check. Page script
    # cannot run in the middle of this function, and the object id belongs
    # to the document it was resolved in, so a navigation since makes the
    # call fail rather than write elsewhere.
    result = cdp.call(
        "Runtime.callFunctionOn", objectId=object_id, returnByValue=True,
        arguments=[{"value": origin}, {"value": value}],
        functionDeclaration=FILL_FUNCTION)
    outcome = result.get("result", {}).get("value")
    if outcome == "origin":
        raise BrowserJobError("refused: the page navigated during the fill", code=3)
    if outcome != "ok":
        raise BrowserJobError(f"{spec['selector']} could not be filled ({outcome})")
    return {"filled": True, "origin": origin, "selector": spec["selector"]}


def _browser_save_cookies(cdp, spec):
    origins = spec["origins"]
    keep = []
    for cookie in _all_cookies(cdp):
        if not _host_matches(cookie.get("domain", ""), origins):
            continue
        ok, partition = _partition_key(cookie)
        if not ok:
            continue
        item = {key: cookie[key] for key in
                ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
                if key in cookie}
        if partition is not None:
            item["partitionKey"] = partition
        keep.append(item)
    if not keep:
        raise BrowserJobError(f"the browser holds no cookies for {', '.join(origins)}")
    # stdout of a save-cookies job goes to the broker's store, never to the
    # caller (Job.capture in lib/secret-jobs.py).
    return {"cookies": keep, "origins": []}


def _browser_load_cookies(cdp, spec, value):
    origins = spec["origins"]
    state = json.loads(value)
    params = []
    for cookie in state.get("cookies", []):
        if not _host_matches(cookie.get("domain", ""), origins):
            continue
        item = {key: cookie[key] for key in
                ("name", "value", "domain", "path", "httpOnly", "secure", "sameSite")
                if key in cookie}
        if isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0:
            item["expires"] = cookie["expires"]
        ok, partition = _partition_key(cookie)
        if not ok:
            continue
        if partition is not None:
            item["partitionKey"] = partition
        params.append(item)
    if params:
        cdp.call("Network.setCookies", cookies=params)
    return {"loaded": len(params), "origins": origins}


def run_browser_job(frame):
    spec = frame["browser"]
    mode = spec["mode"]
    value = frame["secrets"].get(spec.get("secret")) if spec.get("secret") else None
    cdp = None
    try:
        cdp = Cdp(spec["port"], _pick_target(spec["port"], spec.get("targetId")))
        if mode == "fill":
            result = _browser_fill(cdp, spec, value)
        elif mode == "save-cookies":
            result = _browser_save_cookies(cdp, spec)
        elif mode == "load-cookies":
            result = _browser_load_cookies(cdp, spec, value)
        else:
            raise BrowserJobError(f"unknown browser mode {mode}")
    except BrowserJobError as error:
        sys.stderr.write(f"agent-box-secret-worker: {error}\n")
        return error.code
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Never str(error) for these: a KeyError/ValueError message can
        # quote the data it choked on. The type is enough to debug with.
        sys.stderr.write(f"agent-box-secret-worker: browser job failed ({type(error).__name__})\n")
        return 4
    finally:
        if cdp is not None:
            cdp.close()
    sys.stdout.write(json.dumps(result) + ("" if mode == "save-cookies" else "\n"))
    sys.stdout.flush()
    return 0


def main():
    frame = read_frame(sys.stdin.buffer)

    if frame.get("browser"):
        raise SystemExit(run_browser_job(frame))

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    env = {}
    for key in ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TERM", "LANG"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(frame.get("env") or {})
    env.update(frame["secrets"])

    cwd = frame.get("cwd") or env.get("HOME") or "/"
    try:
        os.chdir(cwd)
    except OSError as error:
        sys.stderr.write(f"agent-box-secret-worker: cannot chdir to {cwd}: {error}\n")
        raise SystemExit(126)

    if frame.get("shell"):
        shell = _bash_path() if frame["shell"] == "bash" else "/bin/sh"
        argv = [shell, "-c", frame["command"]]
    else:
        argv = frame["argv"]

    try:
        os.execvpe(argv[0], argv, env)
    except OSError as error:
        sys.stderr.write(f"agent-box-secret-worker: cannot exec {argv[0]}: {error}\n")
        raise SystemExit(127)


if __name__ == "__main__":
    main()
