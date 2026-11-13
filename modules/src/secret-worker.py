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
import json
import os
import struct
import sys


def read_frame(stream):
    header = stream.read(4)
    if len(header) != 4:
        sys.stderr.write("agent-box-secret-worker: no request frame\n")
        raise SystemExit(2)
    (length,) = struct.unpack(">I", header)
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def main():
    frame = read_frame(sys.stdin.buffer)

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
        shell = "/bin/bash" if frame["shell"] == "bash" else "/bin/sh"
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
