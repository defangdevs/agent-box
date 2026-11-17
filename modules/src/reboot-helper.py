"""agent-box-reboot-helper - a reboot that needs nothing from the disk.

The settings page's "Reboot box" button normally runs `sudo systemctl
reboot`: two programs, both read from disk at the moment of the request.
When the disk has gone away (issue #1053) the page still answers - it runs
from RAM - and the button does nothing, because the `exec` cannot find its
program. This helper is the last resort for that case.

It is a root daemon that is ALREADY RUNNING when the disk fails. Everything
it needs is open or loaded before it serves the first request:

  * /proc/sysrq-trigger is open for writing, so a reboot is a `write()` on
    a descriptor it holds - no path lookup, no exec;
  * every module it uses is imported at the top, and mlockall() keeps its
    pages resident, so a stalled disk cannot page the code out;
  * the listening socket is bound, so no systemd socket activation (which
    would have to start a process on demand) is involved.

It then drops root. The open descriptor keeps working; what is left is a
process that can write to one file and nothing else.

Misuse hardening (the point of the design, not an afterthought):

  * Only ONE agent user may connect: the kernel reports the peer's uid
    (SO_PEERCRED) and anything else is closed unanswered. The socket file
    is also 0600 and owned by that user.
  * The request is exactly the seven bytes b"REBOOT\\n". The helper never
    forwards a client byte anywhere: what it writes to sysrq-trigger comes
    from the constants below, so no client can select another sysrq key
    (crash, kill-all, ...). Any other request, longer, shorter, or slow, is
    refused.
  * It refuses outright unless the kernel's kernel.sysrq mask allows the
    reboot function (bit 128, or the value 1).
  * One request starts the sequence; later ones are answered "BUSY".

The front door has to survive too: the request reaches this helper through
Caddy and the settings daemon, and a code page of theirs that was never
loaded since boot is a SIGBUS once the disk is gone (a KVM fault test
killed Caddy on its first TLS handshake). So a second, root process forked
before the drop pins every file the AGENT_BOX_REBOOT_PIN_UNITS processes
have mapped: mmap + mlock, refreshed every PIN_REFRESH_S so an update's new
store paths are picked up. It takes no client input, pins only root-owned
files nobody else can write, and stops at PIN_MAX_BYTES.

The sequence is the classic sync (s), remount read-only (u), reboot (b),
with a pause between each. The kernel runs s and u asynchronously and `b`
does not wait for them, so the pauses are what give them a chance. They are
bounded: a dead disk cannot stop the final `b`.
"""
import ctypes
import grp
import os
import pwd
import signal
import socket
import stat
import struct
import sys
import time

REQUEST = b"REBOOT\n"
SYSRQ_TRIGGER = "/proc/sysrq-trigger"
SYSRQ_MASK = "/proc/sys/kernel/sysrq"
# Bit 128 of kernel.sysrq allows reboot/poweroff; 1 allows everything.
SYSRQ_REBOOT_BIT = 128
# The ONLY bytes ever written to the trigger. Never derived from input.
SEQUENCE = ((b"s", 3.0), (b"u", 3.0), (b"b", 0.0))
CLIENT_TIMEOUT_S = 2.0
NOBODY = 65534
PIN_REFRESH_S = 60
# Caddy plus the settings daemon came to 72 MiB in the fault test.
PIN_MAX_BYTES = 256 << 20
PROT_READ = 1
MAP_SHARED = 1


def sysrq_allows_reboot(mask_fd):
    """True when kernel.sysrq lets the `b` function run."""
    try:
        raw = os.pread(mask_fd, 32, 0).strip()
        value = int(raw)
    except (OSError, ValueError):
        return False
    return value == 1 or bool(value & SYSRQ_REBOOT_BIT)


def peer_uid(conn):
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def read_request(conn):
    """Read at most len(REQUEST)+1 bytes before a deadline; the caller
    compares them with REQUEST. One byte too many is read on purpose so a
    longer message is a mismatch and not a silently truncated match."""
    conn.settimeout(CLIENT_TIMEOUT_S)
    data = b""
    limit = len(REQUEST) + 1
    deadline = time.monotonic() + CLIENT_TIMEOUT_S
    while len(data) < limit and time.monotonic() < deadline:
        try:
            chunk = conn.recv(limit - len(data))
        except OSError:
            break
        if not chunk:
            break
        data += chunk
        if data == REQUEST:
            # Complete request: stop here instead of waiting for EOF. A
            # trailing byte would not have been read yet, so ask once
            # more without blocking.
            conn.settimeout(0.05)
            try:
                extra = conn.recv(1)
            except OSError:
                extra = b""
            return data + extra
    return data


def reply(conn, text):
    try:
        conn.sendall(text)
    except OSError:
        pass


def run_sequence(trigger_fd, sleep, delays=True):
    for key, pause in SEQUENCE:
        os.write(trigger_fd, key)
        if pause and delays:
            sleep(pause)


def serve(sock, allowed_uid, trigger_fd, mask_fd, sleep=time.sleep,
          delays=True, once=False):
    started = False
    while True:
        conn, _ = sock.accept()
        with conn:
            try:
                uid = peer_uid(conn)
            except OSError:
                continue
            if uid != allowed_uid:
                # Not answered: a stranger learns nothing, not even that a
                # reboot helper is here.
                continue
            if read_request(conn) != REQUEST:
                reply(conn, b"ERR request\n")
                continue
            if started:
                reply(conn, b"BUSY\n")
                continue
            if not sysrq_allows_reboot(mask_fd):
                reply(conn, b"ERR sysrq-disabled\n")
                continue
            started = True
            reply(conn, b"OK\n")
        try:
            run_sequence(trigger_fd, sleep, delays)
        except OSError as exc:
            sys.stderr.write("agent-box-reboot-helper: sysrq write: %s\n" % exc)
            started = False
        if once:
            return


def unit_pids(units, proc="/proc"):
    """PIDs whose cgroup v2 path ends in one of `units`."""
    pids = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "cgroup")) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("0::") and line.rsplit("/", 1)[-1] in units:
                pids.append(name)
                break
    return pids


def mapped_files(pids, proc="/proc"):
    """Paths of the files these processes have mapped."""
    paths = set()
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "maps")) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split(None, 5)
            if len(fields) < 6 or not fields[5].startswith("/"):
                continue
            if not fields[5].endswith(" (deleted)"):
                paths.add(fields[5])
    return paths


def pinnable(st, owner=0):
    """A regular, non-empty file owned by `owner` that group and others
    cannot write: a process we pin for cannot grow it into our RAM."""
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return False
    return st.st_uid == owner and not st.st_mode & 0o022


class Pinner:
    """Keeps a set of files mmapped and mlocked; refresh() reconciles."""

    def __init__(self, libc, owner=0, limit=PIN_MAX_BYTES):
        self.libc = libc
        self.owner = owner
        self.limit = limit
        self.pinned = {}  # (st_dev, st_ino) -> (addr, size, path)

    def total(self):
        return sum(size for _, size, _ in self.pinned.values())

    def refresh(self, paths):
        wanted = {}
        for path in sorted(paths):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if pinnable(st, self.owner):
                wanted.setdefault((st.st_dev, st.st_ino), (path, st.st_size))
        for key in [k for k in self.pinned if k not in wanted]:
            addr, size, _ = self.pinned.pop(key)
            self.libc.munmap(ctypes.c_void_p(addr), ctypes.c_size_t(size))
        skipped = 0
        for key, (path, size) in wanted.items():
            if key in self.pinned:
                continue
            if self.total() + size > self.limit:
                skipped += 1
                continue
            addr = self.map(path, size)
            if addr is not None:
                self.pinned[key] = (addr, size, path)
        return skipped

    def map(self, path, size):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            addr = self.libc.mmap(None, size, PROT_READ, MAP_SHARED, fd, 0)
        finally:
            os.close(fd)
        if addr in (None, ctypes.c_void_p(-1).value):
            return None
        if self.libc.mlock(ctypes.c_void_p(addr), ctypes.c_size_t(size)):
            self.libc.munmap(ctypes.c_void_p(addr), ctypes.c_size_t(size))
            return None
        return addr


def load_libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_long]
    return libc


def run_pinner(units):
    """The root child: pin the front door's mapped files, forever."""
    lock_memory()
    pinner = Pinner(load_libc())
    last = None
    while True:
        try:
            skipped = pinner.refresh(mapped_files(unit_pids(units)))
            now = (len(pinner.pinned), pinner.total() >> 20, skipped)
            if now != last:
                sys.stderr.write("agent-box-reboot-helper: pinned %d files "
                                 "(%d MiB), %d over the limit\n" % now)
                last = now
        except Exception as exc:  # keep pinning through one bad refresh
            sys.stderr.write("agent-box-reboot-helper: pin: %r\n" % exc)
        time.sleep(PIN_REFRESH_S)


def lock_memory():
    libc = ctypes.CDLL(None, use_errno=True)
    # MCL_CURRENT | MCL_FUTURE
    if libc.mlockall(1 | 2) != 0:
        sys.stderr.write("agent-box-reboot-helper: mlockall failed: errno %d\n"
                         % ctypes.get_errno())


def drop_privileges():
    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)


def main():
    user = os.environ.get("AGENT_BOX_REBOOT_USER", "")
    path = os.environ.get("AGENT_BOX_REBOOT_SOCKET", "")
    if not user or not path:
        sys.stderr.write("agent-box-reboot-helper: AGENT_BOX_REBOOT_USER and "
                         "AGENT_BOX_REBOOT_SOCKET are required\n")
        return 2
    entry = pwd.getpwnam(user)
    # Preload everything a later lookup could need, before the disk can fail.
    grp.getgrall()
    trigger_fd = os.open(SYSRQ_TRIGGER, os.O_WRONLY)
    mask_fd = os.open(SYSRQ_MASK, os.O_RDONLY)
    if os.path.lexists(path):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    os.umask(0o177)
    sock.bind(path)
    os.chown(path, entry.pw_uid, -1)
    os.chmod(path, 0o600)
    sock.listen(4)
    units = os.environ.get("AGENT_BOX_REBOOT_PIN_UNITS", "").split()
    if units:
        # Auto-reap: a pinner that dies must not leave a zombie, and the
        # reboot path does not depend on it.
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        if os.fork() == 0:
            # The child keeps root (reading another user's maps needs it)
            # and so holds nothing a client can reach.
            sock.close()
            os.close(trigger_fd)
            os.close(mask_fd)
            run_pinner(set(units))
            os._exit(1)
    lock_memory()
    drop_privileges()
    sys.stderr.write("agent-box-reboot-helper: ready for uid %d on %s\n"
                     % (entry.pw_uid, path))
    serve(sock, entry.pw_uid, trigger_fd, mask_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
