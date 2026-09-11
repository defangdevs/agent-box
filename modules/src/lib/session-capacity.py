# Shared admission policy for the single-user v1 box (issue #662).
# Callers hold the sessions.json sidecar lock through this check AND their
# registry write / tmux spawn. Non-stopped entries reserve pending starts.
import os as capacity_os
import subprocess as capacity_subprocess


class SessionCapacityError(Exception):
    pass


def capacity_limit():
    path = capacity_os.environ.get("AGENT_BOX_SESSION_LIMIT_FILE",
                                   "/etc/agent-box/session-limit")
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
    except FileNotFoundError:
        value = "4"
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError("sessionLimit must be a positive integer")
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
    return set(proc.stdout.splitlines())


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
    used = live | pending
    targets = set(targets)
    if spawning:
        available = max(0, limit - len(live))
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
