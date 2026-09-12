# Shared admission policy for the single-user v1 box (issue #662).
# Callers hold the sessions.json sidecar lock through this check AND their
# registry write / tmux spawn. Non-stopped entries reserve pending starts.
import os as capacity_os
import subprocess as capacity_subprocess


CAPACITY_KIB_PER_GIB = 1024 * 1024


class SessionCapacityError(Exception):
    pass


def capacity_memory_limit():
    """Return roughly one session per GiB of physical RAM, minimum one."""
    path = capacity_os.environ.get("AGENT_BOX_MEMINFO_FILE", "/proc/meminfo")
    with open(path, encoding="ascii") as handle:
        for line in handle:
            fields = line.split()
            if fields[:1] != ["MemTotal:"]:
                continue
            if len(fields) != 3:
                break
            if not fields[1].isascii() or not fields[1].isdecimal():
                break
            if fields[2] != "kB":
                break
            memory_kib = int(fields[1])
            if memory_kib < 1:
                break
            # MemTotal excludes the kernel's own reservations, so it is a
            # little below the host's nominal RAM. Round up rather than turn
            # a nominal 4 GiB box into three slots for that bookkeeping.
            rounded_kib = memory_kib + CAPACITY_KIB_PER_GIB - 1
            return max(1, rounded_kib // CAPACITY_KIB_PER_GIB)
    raise ValueError("cannot determine MemTotal from %s" % path)


def capacity_limit():
    path = capacity_os.environ.get("AGENT_BOX_SESSION_LIMIT_FILE",
                                   "/etc/agent-box/session-limit")
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
    except FileNotFoundError:
        value = "auto"
    if value == "auto":
        return capacity_memory_limit()
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError("sessionLimit must be 'auto' or a positive integer")
    return int(value)


def capacity_live():
    command = [capacity_os.environ.get("AGENT_BOX_TMUX_BIN", "tmux"),
               "-L", capacity_os.environ.get("AGENT_BOX_TMUX_SOCKET", "agent-box")]
    env = dict(capacity_os.environ)
    if env.get("AGENT_BOX_TMUX_TMPDIR"):
        env["TMUX_TMPDIR"] = env["AGENT_BOX_TMUX_TMPDIR"]
    proc = capacity_subprocess.run(
        command + ["list-sessions", "-F", "#S"], capture_output=True,
        text=True, timeout=5, env=env)
    if proc.returncode:
        # A missing server is empty; arbitrary tmux errors are not. Do not
        # turn a permissions/socket failure into permission to overcommit.
        if not any(s in proc.stderr for s in (
                "no server running", "no sessions", "No such file or directory")):
            raise OSError("cannot determine session capacity: " + proc.stderr.strip())
        return set()
    # The settings page's sign-in flows (settings-daemon.py's CONNECT_PREFIX)
    # run on this same tmux socket as a "_connect-<flow>" pane, but they are
    # not an agent session and were never registered — counting them would
    # let an in-progress sign-in consume a slot a real session needs.
    return {s for s in proc.stdout.splitlines() if not s.startswith("_connect-")}


def capacity_check(sessions, targets=(), spawning=False, live=None, limit=None):
    """Admit new/revived targets, or one supervisor spawn.

    On boot or after a limit reduction, an overfull registry is a queue:
    keep live panes and admit pending names in sorted order up to the limit.
    Existing panes are never killed. Ordinary adds cannot jump that queue.
    """
    try:
        limit = capacity_limit() if limit is None else limit
        live = capacity_live() if live is None else set(live)
    except (OSError, ValueError, capacity_subprocess.TimeoutExpired) as exc:
        raise SessionCapacityError("Cannot check session capacity: %s" % exc) from exc
    pending = {name for name, entry in sessions.items()
               if isinstance(entry, dict) and entry.get("stopped") is not True}
    # A crash is flagged `died`, not `stopped` (issue #516), so a died entry
    # stays in `pending` -- it must remain its own candidate for revival by
    # `agent-box-session restart`, or a stale flag on a session that already
    # respawned fine (a race the pane epilogue and the supervisor both write
    # `died`/`stopped` into) can never be admitted again to clear it. But its
    # pane is a post-mortem shell doing no real work, so unlike a genuinely
    # running session it must not cost anyone ELSE a slot: before this fix a
    # died session's pane counted as real, running capacity forever, and
    # enough of them stalled every OTHER pending session too, with nothing to
    # clear it but `agent-box-session rm` (issue #523).
    died = {name for name, entry in sessions.items()
            if isinstance(entry, dict) and entry.get("died") is not None}
    used = (live | pending) - died
    targets = set(targets)
    if spawning:
        available = max(0, limit - len(live - died))
        admitted = live | set(sorted(pending - live)[:available])
        allowed = targets <= admitted
    else:
        # An already-admitted session retains its slot during restart, even
        # if an operator has since lowered the limit below the running count.
        added = targets - used
        allowed = not added or len(used | targets) <= limit
    if not allowed:
        raise SessionCapacityError(
            "Session limit reached (%d running or queued, limit %d). "
            "Stop a session before starting another." % (len(used), limit))
    return {"used": len(used), "max": limit}
