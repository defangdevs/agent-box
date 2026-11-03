"""agent-box-secret — manage this user's protected secrets (#922).

  list                 names and versions; never a value
  set NAME --stdin     write-only: the value comes from stdin, never argv
  remove NAME          delete the value, keeping a protected-name tombstone

Talks to the root broker over its Unix socket. There is deliberately no verb
that prints a value.
"""
import argparse
import sys


def call(sock_path, req):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(15)
    try:
        conn.connect(sock_path)
        send_frame(conn, req)
        return recv_frame(conn)
    except (OSError, SecretError) as exc:
        sys.stderr.write(f"agent-box-secret: broker unavailable: {exc}\n")
        raise SystemExit(1)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(prog="agent-box-secret")
    parser.add_argument("--socket", default=os.environ.get("AGENT_BOX_SECRET_SOCKET"))
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("list")
    set_p = sub.add_parser("set")
    set_p.add_argument("name")
    set_p.add_argument("--stdin", action="store_true", required=True)
    sub.add_parser("remove").add_argument("name")
    args = parser.parse_args()
    if not args.socket:
        sys.stderr.write("agent-box-secret: secrets are not enabled on this box\n")
        return 2
    if args.verb == "list":
        req = {"op": "list"}
    elif args.verb == "set":
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        req = {"op": "set", "name": args.name, "value": value}
    else:
        req = {"op": "remove", "name": args.name}
    resp = call(args.socket, req)
    if not resp.get("ok"):
        sys.stderr.write(f"agent-box-secret: {resp.get('error', 'refused')}\n")
        return 1
    if args.verb == "list":
        for item in resp["secrets"]:
            state = "configured" if item["configured"] else "removed"
            print(f"{item['name']}\tv{item['version']}\t{state}")
    else:
        print(f"{args.verb} ok (v{resp['version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
