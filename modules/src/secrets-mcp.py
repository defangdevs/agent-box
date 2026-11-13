"""agent-box-secrets-mcp - the box-local MCP adapter (#922, PR 3 slice).

An unprivileged stdio server. It never sees a value except as it flows back
from a job's own (already redacted) stdout/stderr - it talks to the root
broker over its per-user Unix socket exactly like the CLI does, using the
same request schema (lib/secret-protocol.py). Four tools only, matching
list_secret_names / exec_with_secrets / exec_with_secrets_status /
exec_with_secrets_cancel from the issue; there is deliberately no tool that
can read or export a value.

Scope: this process's AGENT_BOX_PROFILE (the supervisor already exports it
into every session - see env-exec.py). No tool argument can select a
different profile or user; that would defeat the whole per-profile
boundary this adapter exists to keep.

Spliced ABOVE this file by the generated module, after secret-protocol.py,
so SecretError/send_frame/recv_frame/SECRET_* already exist when this runs.
"""
import os
import socket
import sys
import time

from mcp.server.fastmcp import FastMCP

SOCKET_PATH = os.environ.get("AGENT_BOX_SECRET_SOCKET")
PROFILE = os.environ.get("AGENT_BOX_PROFILE") or None
POLL_INTERVAL_S = 0.1
INLINE_WAIT_BUDGET_S = 3.0

mcp = FastMCP("agent-box-secrets")


def _call(req):
    if not SOCKET_PATH:
        return {"ok": False, "error": "secrets are not enabled on this box"}
    req = dict(req, profile=PROFILE)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(15)
    try:
        conn.connect(SOCKET_PATH)
        send_frame(conn, req)
        return recv_frame(conn)
    except (OSError, SecretError) as exc:
        return {"ok": False, "error": f"broker unavailable: {exc}"}
    finally:
        conn.close()


@mcp.tool()
def list_secret_names() -> dict:
    """List this profile's configured secret names and versions. Never a
    value - use exec_with_secrets to run a command with one injected."""
    return _call({"op": "list"})


@mcp.tool()
def exec_with_secrets(
    secretNames: list[str],
    argv: list[str] | None = None,
    command: str | None = None,
    shell: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeoutMs: int | None = None,
    idempotencyKey: str | None = None,
) -> dict:
    """Run a command with ONLY the named secrets injected into its
    environment. Give either argv (a literal argument list, preferred) or
    command+shell ("bash" or "sh"). secretNames must come from
    list_secret_names. The command's own environment never contains a
    secret this call did not name. Output is captured with every selected
    value's exact text redacted. Returns before the job may have finished
    for a long-running command; poll with exec_with_secrets_status using
    the returned jobId. idempotencyKey makes a retried call with the exact
    same request return the original job instead of starting a second one."""
    req = {"op": "exec", "secretNames": secretNames}
    if argv is not None:
        req["argv"] = argv
    if command is not None:
        req["command"] = command
    if shell is not None:
        req["shell"] = shell
    if cwd is not None:
        req["cwd"] = cwd
    if env is not None:
        req["env"] = env
    if timeoutMs is not None:
        req["timeoutMs"] = timeoutMs
    if idempotencyKey is not None:
        req["idempotencyKey"] = idempotencyKey
    resp = _call(req)
    if not resp.get("ok"):
        return resp
    job_id = resp["jobId"]
    deadline = time.time() + INLINE_WAIT_BUDGET_S
    status = _call({"op": "status", "jobId": job_id})
    while status.get("ok") and status.get("state") in ("pending", "running") and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        status = _call({"op": "status", "jobId": job_id})
    status["jobId"] = job_id
    return status


@mcp.tool()
def exec_with_secrets_status(jobId: str) -> dict:
    """Poll a job started by exec_with_secrets."""
    return _call({"op": "status", "jobId": jobId})


@mcp.tool()
def exec_with_secrets_cancel(jobId: str) -> dict:
    """Stop a running job started by exec_with_secrets. A job that already
    finished is returned unchanged; cancellation cannot undo an effect the
    command already caused outside this box (e.g. a detached deployment)."""
    return _call({"op": "cancel", "jobId": jobId})


def main():
    if not SOCKET_PATH:
        sys.stderr.write(
            "agent-box-secrets-mcp: AGENT_BOX_SECRET_SOCKET is not set; "
            "every tool call will report secrets as disabled\n")
    mcp.run()


if __name__ == "__main__":
    main()
