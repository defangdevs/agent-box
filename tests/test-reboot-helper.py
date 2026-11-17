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

# Refusals that wait for a slow client would make the run take seconds.
rh.CLIENT_TIMEOUT_S = 0.3
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
        # A refused client may be closed before it finishes sending; that
        # is the refusal working, not a test error.
        try:
            if payload is not None:
                c.sendall(payload)
            replies.append(c.recv(64))
        except OSError:
            replies.append(b"<timeout>")
        c.close()
    # close() alone does not reliably wake a thread blocked in accept().
    try:
        srv.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
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

# --- the front-door pinner -------------------------------------------------

proc = tempfile.mkdtemp()
files = tempfile.mkdtemp()


def fake_proc(pid, cgroup, maps):
    os.makedirs(os.path.join(proc, pid))
    open(os.path.join(proc, pid, "cgroup"), "w").write(cgroup)
    open(os.path.join(proc, pid, "maps"), "w").write(maps)


def mkfile(name, size, mode=0o644):
    p = os.path.join(files, name)
    open(p, "wb").write(b"x" * size)
    os.chmod(p, mode)
    return p


bin_a = mkfile("caddy", 4096)
lib_a = mkfile("libc.so", 1024)
writable = mkfile("writable.so", 1024, 0o666)
empty = mkfile("empty", 0)
fake_proc("10", "0::/system.slice/caddy.service\n",
          "0-1 r-xp 0 08:01 5 %s\n0-1 r--p 0 08:01 6 %s\n"
          "0-1 rw-p 0 00:00 0 [heap]\n0-1 rw-p 0 00:00 0\n"
          "0-1 r--p 0 08:01 7 %s (deleted)\n" % (bin_a, lib_a, lib_a))
fake_proc("11", "0::/system.slice/system-agent\\x2dbox\\x2dsettings.slice/"
          "agent-box-settings@root.service\n", "0-1 r--p 0 08:01 8 %s\n" % writable)
fake_proc("12", "0::/user.slice/user-1000.slice/session-1.scope\n",
          "0-1 r--p 0 08:01 9 %s\n" % empty)
fake_proc("13", "0::/system.slice/not-caddy.service.d\n", "")
os.makedirs(os.path.join(proc, "self"))

units = {"caddy.service", "agent-box-settings@root.service"}
check("unit_pids matches the unit's own cgroup leaf only",
      sorted(rh.unit_pids(units, proc)) == ["10", "11"])
check("mapped_files keeps file paths, drops anon/heap/deleted",
      rh.mapped_files(["10"], proc) == {bin_a, lib_a})


class FakeLibc:
    def __init__(self, fail_mlock=False):
        self.next = 0x1000
        self.maps = {}
        self.fail_mlock = fail_mlock

    def mmap(self, _addr, size, prot, flags, fd, off):
        assert prot == rh.PROT_READ and flags == rh.MAP_SHARED and off == 0
        addr = self.next
        self.next += 0x100000
        self.maps[addr] = size
        return addr

    def mlock(self, addr, size):
        return -1 if self.fail_mlock else 0

    def munmap(self, addr, size):
        del self.maps[addr.value]
        return 0


me = os.getuid()
lc = FakeLibc()
pin = rh.Pinner(lc, owner=me, limit=1 << 20)
pin.refresh({bin_a, lib_a, writable, empty, "/nonexistent"})
pinned = {v[2] for v in pin.pinned.values()}
check("pins own regular files", pinned == {bin_a, lib_a})
check("refuses a group/other-writable file", writable not in pinned)
check("refuses an empty file", empty not in pinned)
check("total counts pinned bytes", pin.total() == 4096 + 1024)
pin.refresh({bin_a, lib_a})
check("refresh is idempotent", len(lc.maps) == 2)
pin.refresh({lib_a})
check("a file no longer mapped is unpinned",
      {v[2] for v in pin.pinned.values()} == {lib_a} and len(lc.maps) == 1)

other = rh.Pinner(FakeLibc(), owner=me + 1)
other.refresh({bin_a})
check("a file owned by someone else is refused", not other.pinned)

small = rh.Pinner(FakeLibc(), owner=me, limit=2048)
skipped = small.refresh({bin_a, lib_a})
check("the byte limit skips what does not fit",
      skipped == 1 and small.total() == 1024)

lc_fail = FakeLibc(fail_mlock=True)
nolock = rh.Pinner(lc_fail, owner=me)
nolock.refresh({bin_a})
check("a failed mlock is unmapped and not recorded",
      not nolock.pinned and not lc_fail.maps)

lc_io = FakeLibc()
keep = rh.Pinner(lc_io, owner=me)
keep.refresh({bin_a})
real_stat = os.stat


def flaky_stat(path, *a, **k):
    if path == bin_a:
        raise OSError(5, "Input/output error")
    return real_stat(path, *a, **k)


os.stat = flaky_stat
try:
    keep.refresh({bin_a})
finally:
    os.stat = real_stat
check("a stat that fails (EIO) keeps the existing pin",
      {v[2] for v in keep.pinned.values()} == {bin_a} and len(lc_io.maps) == 1)

order = []
orig_shield, orig_reply = rh.shield_from_shutdown, rh.reply
rh.shield_from_shutdown = lambda: order.append("shield")
rh.reply = lambda c, t: (order.append(t.strip().decode()), orig_reply(c, t))[1]
scenario(128, me, [b"REBOOT\n"])
rh.shield_from_shutdown, rh.reply = orig_shield, orig_reply
check("SIGTERM is ignored before OK is sent",
      order.index("shield") < order.index("OK"))

errs = []
rh.mapped_files(["10", "99"], proc, errors=errs)
check("an exited process is not a discovery error", not errs)
os.chmod(os.path.join(proc, "10", "maps"), 0)
if os.getuid() != 0:
    rh.mapped_files(["10"], proc, errors=errs)
    check("an unreadable maps file is a discovery error", len(errs) == 1)
os.chmod(os.path.join(proc, "10", "maps"), 0o644)
lc_inc = FakeLibc()
inc = rh.Pinner(lc_inc, owner=me)
inc.refresh({bin_a})
inc.refresh(set(), complete=False)
check("an incomplete discovery unpins nothing", len(lc_inc.maps) == 1)
inc.refresh(set())
check("a complete discovery still unpins", not lc_inc.maps)

if failures:
    print("%d failure(s)" % len(failures))
    sys.exit(1)
print("all passed")
