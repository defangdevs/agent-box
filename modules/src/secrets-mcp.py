"""agent-box-secrets-mcp - the box-local MCP adapter (#922, PR 3 slice).

An unprivileged stdio server. It never sees a value except as it flows back
from a job's own (already redacted) stdout/stderr - it talks to the root
broker over its per-user Unix socket exactly like the CLI does, using the
same request schema (lib/secret-protocol.py). The four tools from the issue
(list_secret_names / exec_with_secrets / exec_with_secrets_status /
exec_with_secrets_cancel), plus the website-login tools: generate_secret,
browser_fill, browser_save_cookies and browser_load_cookies. There is
deliberately no tool that can read, export or SET a value - a value typed
into a tool call would land in the transcript; `agent-box-secret set
--stdin` is how a person stores one they already have.

Scope: this process's AGENT_BOX_PROFILE (the supervisor already exports it
into every session - see env-exec.py), or the box's current default
profile when that is unset (resolve_profile, lib/secret-protocol.py) -
there is no separate global scope. No tool argument can select a
different profile; that is routing, not a security boundary, exactly like
every other per-profile value in this codebase (see env-exec.py's own
"Convenience, NOT isolation" note on profile env) - the broker only ever
checks SO_PEERCRED uid, so any process of this user could ask for another
profile's secrets by naming it explicitly. What scoping buys is that an
ordinary tool call never reaches across profiles by accident.

Spliced ABOVE this file by the generated module, after secret-protocol.py,
so SecretError/connect_and_call/SECRET_* already exist when this runs.
"""
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

SOCKET_PATH = os.environ.get("AGENT_BOX_SECRET_SOCKET")
POLL_INTERVAL_S = 0.1
INLINE_WAIT_BUDGET_S = 3.0
BROWSER_WAIT_BUDGET_S = 20.0

mcp = FastMCP("agent-box-secrets")


def _call(req, profile=None):
    # Resolved per call, not once at import: a box with no default profile
    # and no AGENT_BOX_PROFILE must not fail to even START this server over
    # it - each tool call reports the refusal instead. A job's own follow-up
    # calls pass the profile it was started under, so a default that changes
    # meanwhile cannot make status/cancel look in another namespace.
    try:
        profile = resolve_profile(profile or os.environ.get("AGENT_BOX_PROFILE") or None)
    except SecretError as exc:
        return {"ok": False, "error": str(exc)}
    return connect_and_call(SOCKET_PATH, dict(req, profile=profile))


@mcp.tool()
def list_secret_names() -> dict:
    """List this profile's configured secret names, versions, kinds
    (value/totp/cookies), origins and whether each is fill-only. Never a
    value - use exec_with_secrets to run a command with one injected, or
    browser_fill to type one into a login form."""
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
    try:
        profile = resolve_profile(os.environ.get("AGENT_BOX_PROFILE") or None)
    except SecretError as exc:
        return {"ok": False, "error": str(exc)}
    resp = _call(req, profile)
    if not resp.get("ok"):
        return resp
    job_id = resp["jobId"]
    deadline = time.time() + INLINE_WAIT_BUDGET_S
    status = _call({"op": "status", "jobId": job_id}, profile)
    # Not lib/secret-jobs.py's ACTIVE_STATES: this file only splices with
    # secret-protocol.py (see the header), to keep the unprivileged adapter's
    # dependency closure small - a second literal here is cheaper than
    # pulling in the whole job-manager module for one tuple.
    while status.get("ok") and status.get("state") in ("pending", "running") and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        status = _call({"op": "status", "jobId": job_id}, profile)
    status["jobId"] = job_id
    status["profile"] = profile
    return status


def _run_browser_job(req):
    try:
        profile = resolve_profile(os.environ.get("AGENT_BOX_PROFILE") or None)
    except SecretError as exc:
        return {"ok": False, "error": str(exc)}
    resp = _call(req, profile)
    if not resp.get("ok"):
        return resp
    job_id = resp["jobId"]
    deadline = time.time() + BROWSER_WAIT_BUDGET_S
    status = _call({"op": "status", "jobId": job_id}, profile)
    while status.get("ok") and status.get("state") in ("pending", "running") and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        status = _call({"op": "status", "jobId": job_id}, profile)
    status["jobId"] = job_id
    status["profile"] = profile
    return status


def _origins(origins):
    return [normalize_origin(o) for o in origins or []]


@mcp.tool()
def generate_secret(
    name: str,
    origins: list[str] | None = None,
    length: int = SECRET_GENERATE_DEFAULT_LENGTH,
    symbols: bool = True,
    allowExec: bool = False,
) -> dict:
    """Create a new random password and store it as `name`, without anyone
    (you included) ever seeing it. Use it when you sign up for a website:
    give the site's URL in origins, then type it into the sign-up form with
    browser_fill. With origins the secret is fill-only (exec refuses it)
    unless allowExec is true. Set symbols=false for a site that refuses
    punctuation. Replaces an existing secret of the same name."""
    try:
        normalized = _origins(origins)
    except SecretError as exc:
        return {"ok": False, "error": str(exc)}
    req = {"op": "generate", "name": name, "length": length, "symbols": symbols}
    if normalized:
        req["origins"] = normalized
        req["fillOnly"] = not allowExec
    return _call(req)


@mcp.tool()
def browser_fill(name: str, cdpPort: int, selector: str, targetId: str | None = None) -> dict:
    """Type secret `name` into the input that CSS `selector` matches, in
    your Chromium on 127.0.0.1:cdpPort (start it with
    --remote-debugging-port). The broker first checks that the page's
    origin, as the browser reports it, is one of the secret's origins, and
    refuses otherwise - so never retry a refusal on another page: it means
    you are not on the site you think. A TOTP secret types its current
    code. targetId (from http://127.0.0.1:PORT/json/list) is needed only
    when several pages are open. Submit the form yourself afterwards."""
    req = {"op": "fill", "name": name, "cdpPort": cdpPort, "selector": selector}
    if targetId:
        req["targetId"] = targetId
    return _run_browser_job(req)


@mcp.tool()
def browser_save_cookies(name: str, cdpPort: int, origins: list[str],
                         targetId: str | None = None, allowExec: bool = False) -> dict:
    """After you log in, store the browser's cookies for `origins` as
    secret `name`, so a later session can skip the login with
    browser_load_cookies. The cookies go straight to the broker; they are
    not returned to you. Fill-only unless allowExec is true (then exec can
    inject them as a Playwright storageState JSON)."""
    try:
        normalized = _origins(origins)
    except SecretError as exc:
        return {"ok": False, "error": str(exc)}
    req = {"op": "save-cookies", "name": name, "cdpPort": cdpPort,
           "origins": normalized, "fillOnly": not allowExec}
    if targetId:
        req["targetId"] = targetId
    return _run_browser_job(req)


@mcp.tool()
def browser_load_cookies(name: str, cdpPort: int, targetId: str | None = None) -> dict:
    """Put the cookies stored as `name` back into your Chromium on
    127.0.0.1:cdpPort, limited to the secret's own origins. Reload or
    navigate the page afterwards to use the session."""
    req = {"op": "load-cookies", "name": name, "cdpPort": cdpPort}
    if targetId:
        req["targetId"] = targetId
    return _run_browser_job(req)


@mcp.tool()
def exec_with_secrets_status(jobId: str, profile: str | None = None) -> dict:
    """Poll a job started by exec_with_secrets. Pass the profile that call
    returned, if any, so the lookup uses the profile the job started under."""
    return _call({"op": "status", "jobId": jobId}, profile)


@mcp.tool()
def exec_with_secrets_cancel(jobId: str, profile: str | None = None) -> dict:
    """Stop a running job started by exec_with_secrets. A job that already
    finished is returned unchanged; cancellation cannot undo an effect the
    command already caused outside this box (e.g. a detached deployment)."""
    return _call({"op": "cancel", "jobId": jobId}, profile)


def main():
    if not SOCKET_PATH:
        sys.stderr.write(
            "agent-box-secrets-mcp: AGENT_BOX_SECRET_SOCKET is not set; "
            "every tool call will report secrets as disabled\n")
    mcp.run()


if __name__ == "__main__":
    main()
