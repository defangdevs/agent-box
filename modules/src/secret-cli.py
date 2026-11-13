"""agent-box-secret - manage this user's protected secrets (#922).

  list                       names and versions; never a value
  set NAME --stdin           write-only: the value comes from stdin, never argv
  remove NAME                delete the value, keeping a protected-name tombstone
  exec (--argv ... | --command CMD --shell bash|sh) --secret NAME...
                             run a command with only the named secrets injected
  status JOBID               poll a job started by exec
  cancel JOBID               stop a running job

Talks to the root broker over its Unix socket. There is deliberately no verb
that prints a value.

--profile NAME puts a value in one agent-box profile's own secret bucket
instead of the GLOBAL one every profile's sessions can request by default -
narrower by default, not a security boundary: the broker only checks which
Linux user is asking, so any process of this user can still pass a
different --profile explicitly. Omitting --profile prints a one-line
deprecation warning and uses the global bucket. Prefer a profile:
`agent-box-profile ls` lists the ones you have.
"""
import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(prog="agent-box-secret")
    parser.add_argument("--socket", default=os.environ.get("AGENT_BOX_SECRET_SOCKET"))
    parser.add_argument("--profile",
                        help="scope to this agent-box profile's own secrets; omit for the "
                             "deprecated global bucket shared by every profile")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("list")
    set_p = sub.add_parser("set")
    set_p.add_argument("name")
    set_p.add_argument("--stdin", action="store_true", required=True)
    sub.add_parser("remove").add_argument("name")

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
    if args.profile is None and args.verb in ("list", "set", "remove", "exec"):
        sys.stderr.write(
            "agent-box-secret: no --profile given; using the deprecated global secret "
            "bucket, which every profile's sessions can request. Pass --profile NAME "
            "(see `agent-box-profile ls`).\n")

    if args.verb == "list":
        req = {"op": "list", "profile": args.profile}
    elif args.verb == "set":
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        req = {"op": "set", "profile": args.profile, "name": args.name, "value": value}
    elif args.verb == "remove":
        req = {"op": "remove", "profile": args.profile, "name": args.name}
    elif args.verb == "exec":
        if bool(args.command) == bool(args.argv):
            sys.stderr.write("agent-box-secret: give either a literal argv or --command, not both\n")
            return 2
        req = {"op": "exec", "profile": args.profile, "secretNames": args.secrets}
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
        req = {"op": "status", "profile": args.profile, "jobId": args.job_id}
    else:
        req = {"op": "cancel", "profile": args.profile, "jobId": args.job_id}

    resp = connect_and_call(args.socket, req)
    if not resp.get("ok"):
        sys.stderr.write(f"agent-box-secret: {resp.get('error', 'refused')}\n")
        return 1
    if args.json:
        print(json.dumps(resp))
    elif args.verb == "list":
        for item in resp["secrets"]:
            state = "configured" if item["configured"] else "removed"
            print(f"{item['name']}\tv{item['version']}\t{state}")
    elif args.verb in ("set", "remove"):
        print(f"{args.verb} ok (v{resp['version']})")
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
