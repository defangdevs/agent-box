"""agent-box-secret - manage this user's protected secrets (#922).

  list                       names, versions, kinds and origins; never a value
  set NAME --stdin [--totp|--cookies] [--origin URL]... [--allow-exec]
                             write-only: the value comes from stdin, never argv
  generate NAME [--length N] [--no-symbols] [--origin URL]... [--allow-exec]
                             store a new random password nobody has seen
  remove NAME                delete the value, keeping a protected-name tombstone
  exec (--argv ... | --command CMD --shell bash|sh) --secret NAME...
                             run a command with only the named secrets injected
  fill NAME --cdp-port PORT --selector CSS [--target ID]
                             type the secret into a field of your browser's
                             page, only if the page is on one of its origins
  save-cookies NAME --cdp-port PORT --origin URL... [--target ID] [--allow-exec]
                             store the browser's cookies for those origins
  load-cookies NAME --cdp-port PORT [--target ID]
                             put stored cookies back into the browser
  status JOBID               poll a job started by exec
  cancel JOBID               stop a running job

A secret with --origin is FILL-ONLY unless --allow-exec is given: exec
refuses it, and fill types it only into a page whose origin the browser
reports as one of those. A --totp secret is a TOTP seed (base32 or an
otpauth:// URI); exec and fill get its current code, never the seed.
fill/save-cookies/load-cookies talk to a Chromium you started with
--remote-debugging-port=PORT, on 127.0.0.1 only.

Talks to the root broker over its Unix socket. There is deliberately no verb
that prints a value.

--profile NAME puts a value in one agent-box profile's own secret bucket.
There is no separate global bucket: omitting --profile resolves to the
box's current default profile (`agent-box-profile default`), the same one
a session started with neither --profile nor --harness gets - and refuses
if none is set. Scoping is routing, not a security boundary: the broker
only checks which Linux user is asking, so any process of this user can
still pass a different --profile explicitly. `agent-box-profile ls` lists
the ones you have.
"""
import argparse
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser(prog="agent-box-secret")
    parser.add_argument("--socket", default=os.environ.get("AGENT_BOX_SECRET_SOCKET"))
    parser.add_argument("--profile",
                        help="scope to this agent-box profile's own secrets; omit to use "
                             "the box's current default profile (agent-box-profile default)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("list")

    def add_origin_args(sub_parser, required=False):
        sub_parser.add_argument("--origin", dest="origins", action="append",
                                default=[], required=required,
                                help="a site this secret is for; repeat for more than one")
        sub_parser.add_argument("--allow-exec", action="store_true",
                                help="let exec inject it too (a secret with --origin is "
                                     "fill-only without this)")

    def add_browser_args(sub_parser):
        sub_parser.add_argument("--cdp-port", type=int, required=True,
                                help="the browser's --remote-debugging-port (127.0.0.1)")
        sub_parser.add_argument("--target",
                                help="DevTools target id of the page; needed only "
                                     "when more than one page is open")
        sub_parser.add_argument("--timeout-ms", type=int)

    set_p = sub.add_parser("set")
    set_p.add_argument("name")
    set_p.add_argument("--stdin", action="store_true", required=True)
    kind_group = set_p.add_mutually_exclusive_group()
    kind_group.add_argument("--totp", action="store_true",
                            help="the value is a TOTP seed (base32 or otpauth:// URI)")
    kind_group.add_argument("--cookies", action="store_true",
                            help="the value is a Playwright storageState JSON")
    add_origin_args(set_p)
    gen_p = sub.add_parser("generate")
    gen_p.add_argument("name")
    gen_p.add_argument("--length", type=int)
    gen_p.add_argument("--no-symbols", action="store_true")
    add_origin_args(gen_p)
    sub.add_parser("remove").add_argument("name")

    fill_p = sub.add_parser("fill")
    fill_p.add_argument("name")
    fill_p.add_argument("--selector", required=True)
    add_browser_args(fill_p)
    save_p = sub.add_parser("save-cookies")
    save_p.add_argument("name")
    add_origin_args(save_p, required=True)
    add_browser_args(save_p)
    load_p = sub.add_parser("load-cookies")
    load_p.add_argument("name")
    add_browser_args(load_p)

    exec_p = sub.add_parser("exec")
    exec_p.add_argument("--secret", dest="secrets", action="append", default=[],
                        help="a secret name to inject; repeat for more than one")
    exec_p.add_argument("--cwd")
    exec_p.add_argument("--timeout-ms", type=int)
    exec_p.add_argument("--idempotency-key")
    exec_p.add_argument("--command", help="run via a shell instead of a literal argv")
    exec_p.add_argument("--shell", choices=("bash", "sh"), default="sh")
    exec_p.add_argument("argv", nargs="*", help="literal argv, if --command is not given")

    sub.add_parser("status").add_argument("job_id")
    sub.add_parser("cancel").add_argument("job_id")

    args = parser.parse_args()
    if not args.socket:
        sys.stderr.write("agent-box-secret: secrets are not enabled on this box\n")
        return 2
    try:
        profile = resolve_profile(args.profile)
    except SecretError as exc:
        sys.stderr.write(f"agent-box-secret: {exc}\n")
        return 2

    meta = {}
    if args.verb in ("set", "generate", "save-cookies"):
        try:
            origins = [normalize_origin(o) for o in args.origins]
        except SecretError as exc:
            sys.stderr.write(f"agent-box-secret: {exc}\n")
            return 2
        if origins:
            meta["origins"] = origins
            meta["fillOnly"] = not args.allow_exec

    if args.verb == "list":
        req = {"op": "list", "profile": profile}
    elif args.verb == "set":
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        req = {"op": "set", "profile": profile, "name": args.name, "value": value, **meta}
        if args.totp:
            req["kind"] = "totp"
        elif args.cookies:
            req["kind"] = "cookies"
    elif args.verb == "generate":
        req = {"op": "generate", "profile": profile, "name": args.name, **meta}
        if args.length:
            req["length"] = args.length
        if args.no_symbols:
            req["symbols"] = False
    elif args.verb in ("fill", "save-cookies", "load-cookies"):
        req = {"op": args.verb, "profile": profile, "name": args.name,
               "cdpPort": args.cdp_port}
        if args.target:
            req["targetId"] = args.target
        if args.timeout_ms:
            req["timeoutMs"] = args.timeout_ms
        if args.verb == "fill":
            req["selector"] = args.selector
        if args.verb == "save-cookies":
            req.update(meta)
    elif args.verb == "remove":
        req = {"op": "remove", "profile": profile, "name": args.name}
    elif args.verb == "exec":
        if bool(args.command) == bool(args.argv):
            sys.stderr.write("agent-box-secret: give either a literal argv or --command, not both\n")
            return 2
        req = {"op": "exec", "profile": profile, "secretNames": args.secrets}
        if args.command:
            req["command"] = args.command
            req["shell"] = args.shell
        else:
            req["argv"] = args.argv
        if args.cwd:
            req["cwd"] = args.cwd
        if args.timeout_ms:
            req["timeoutMs"] = args.timeout_ms
        if args.idempotency_key:
            req["idempotencyKey"] = args.idempotency_key
    elif args.verb == "status":
        req = {"op": "status", "profile": profile, "jobId": args.job_id}
    else:
        req = {"op": "cancel", "profile": profile, "jobId": args.job_id}

    resp = connect_and_call(args.socket, req)
    if resp.get("ok") and args.verb in ("fill", "save-cookies", "load-cookies"):
        # A browser job takes a second or two; the caller wants its outcome,
        # not a job id to poll, so wait for it here (bounded by the job's
        # own timeout on the broker side).
        job_id = resp["jobId"]
        while True:
            resp = connect_and_call(args.socket, {"op": "status", "profile": profile,
                                                  "jobId": job_id})
            if not resp.get("ok") or resp.get("state") not in ("pending", "running"):
                break
            time.sleep(0.1)
        if resp.get("ok") and resp.get("state") != "done":
            sys.stderr.write(resp.get("stderr") or f"agent-box-secret: job {resp.get('state')}\n")
            return 1
    if not resp.get("ok"):
        sys.stderr.write(f"agent-box-secret: {resp.get('error', 'refused')}\n")
        return 1
    if args.json:
        print(json.dumps(resp))
    elif args.verb == "list":
        for item in resp["secrets"]:
            state = "configured" if item["configured"] else "removed"
            extra = []
            if item.get("kind", "value") != "value":
                extra.append(item["kind"])
            if item.get("fillOnly"):
                extra.append("fill-only")
            if item.get("origins"):
                extra.append(" ".join(item["origins"]))
            print("\t".join([item["name"], f"v{item['version']}", state, *extra]))
        # Advisory only (see lib/secret-candidates.py's header): this never
        # acts on what it finds, so it costs nothing to check on every plain
        # `list`. known_by_namespace[profile] is exactly what the broker
        # just answered; if --profile was never given, "profile" is also
        # what the shared env file's own candidates should be checked
        # against, since that is what "no --profile" now means for secrets.
        known = {item["name"] for item in resp["secrets"]}
        known_by_namespace = {profile: known}
        if args.profile is None:
            known_by_namespace[None] = known
        hint = format_hint(scan(os.path.expanduser("~"), known_by_namespace=known_by_namespace))
        if hint:
            sys.stderr.write("\n" + hint + "\n")
    elif args.verb in ("set", "remove", "generate"):
        print(f"{args.verb} ok (v{resp['version']})")
    elif args.verb in ("fill", "save-cookies", "load-cookies"):
        sys.stdout.write(resp.get("stdout") or "")
    elif args.verb == "exec":
        print(resp["jobId"])
    else:
        print(f"{resp['state']}\texit={resp.get('exitCode')}")
        if resp.get("stdout"):
            sys.stdout.write(resp["stdout"])
        if resp.get("stderr"):
            sys.stderr.write(resp["stderr"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
