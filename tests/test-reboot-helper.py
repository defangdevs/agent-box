"""Native tests for modules/src/reboot-helper.py (issue #1054).

Runs the real serve() loop on a unix socket as the current user, with
the sysrq trigger and mask replaced by plain files. The refusals are the
point: wrong request bytes, a long request, a slow client and a sysrq mask
without the reboot bit must never write to the trigger.

Usage: python3 tests/test-reboot-helper.py [path/to/reboot-helper.py]
"""
import importlib.util
import os
import socket
import sys
import tempfile
import threading

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "modules", "src",
    "reboot-helper.py")
spec = importlib.util.spec_from_file_location("reboot_helper", path)
rh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rh)

failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def scenario(mask, allowed_uid, payloads, once_after=None):
    """Run serve() against `payloads` (list of byte strings, one client
    each). Returns (replies, bytes written to the trigger)."""
    tmp = tempfile.mkdtemp()
    sock_path = os.path.join(tmp, "s.sock")
    trigger = os.path.join(tmp, "trigger")
    maskf = os.path.join(tmp, "mask")
    open(maskf, "w").write("%s\n" % mask)
    tfd = os.open(trigger, os.O_WRONLY | os.O_CREAT, 0o600)
    mfd = os.open(maskf, os.O_RDONLY)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(4)
    slept = []
    done = threading.Event()

    def run():
        try:
            rh.serve(srv, allowed_uid, tfd, mfd, sleep=slept.append,
                     once=False)
        except OSError:
            pass
        done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    replies = []
    for payload in payloads:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(sock_path)
        if payload is not None:
            c.sendall(payload)
        try:
            replies.append(c.recv(64))
        except OSError:
            replies.append(b"<timeout>")
        c.close()
    srv.close()
    done.wait(5)
    os.close(tfd)
    return replies, open(trigger, "rb").read(), slept


me = os.getuid()

r, wrote, slept = scenario(128, me, [b"REBOOT\n"])
check("valid request answered OK", r == [b"OK\n"])
check("writes exactly s,u,b", wrote == b"sub")
check("pauses between steps are bounded",
      slept and all(0 < x <= 5 for x in slept))

r, wrote, _ = scenario(1, me, [b"REBOOT\n"])
check("sysrq=1 accepted", r == [b"OK\n"] and wrote == b"sub")

r, wrote, _ = scenario(16, me, [b"REBOOT\n"])
check("mask without reboot bit refused",
      r == [b"ERR sysrq-disabled\n"] and wrote == b"")

r, wrote, _ = scenario(0, me, [b"REBOOT\n"])
check("sysrq=0 refused", wrote == b"")

for name, payload in (
        ("other bytes", b"c"),
        ("crash key", b"REBOOT\nc"),
        ("lowercase", b"reboot\n"),
        ("no newline", b"REBOOT"),
        ("long", b"REBOOT\n" + b"x" * 500),
        ("empty line", b"\n")):
    r, wrote, _ = scenario(128, me, [payload])
    check("refused: " + name, wrote == b"" and r[0].startswith(b"ERR"))

r, wrote, _ = scenario(128, me + 1, [b"REBOOT\n"])
check("other uid gets no answer and no write",
      wrote == b"" and r[0] in (b"", b"<timeout>"))

r, wrote, _ = scenario(128, me, [b"junk", b"REBOOT\n"])
check("a refused request does not block a later valid one",
      r[1] == b"OK\n" and wrote == b"sub")

if failures:
    print("%d failure(s)" % len(failures))
    sys.exit(1)
print("all passed")
