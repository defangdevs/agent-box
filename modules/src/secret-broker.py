"""agent-box-secret-broker — root custodian for ONE agent user's secrets (#922).

Runs as root from a socket-activated unit, one instance per configured user.
The socket's directory is root-owned and only that user can connect; on top
of that every connection is checked against the kernel's SO_PEERCRED uid, so a
mis-set mode cannot widen access. The request cannot name a uid, a path or a
namespace: all three come from this process's own arguments.

The protocol (src/lib/secret-protocol.py) has no operation that returns a
value. This first slice only stores, lists and removes; job execution is a
later slice.
"""
import argparse
import sys


def peer_uid(conn):
    cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", cred)[1]


def serve_one(listener, store, expect_uid):
    conn, _ = listener.accept()
    with conn:
        conn.settimeout(10)
        try:
            if peer_uid(conn) != expect_uid:
                send_frame(conn, {"ok": False, "error": "forbidden"})
                return
            send_frame(conn, handle_request(store, recv_frame(conn)))
        except SecretError as exc:
            send_frame(conn, {"ok": False, "error": str(exc)})
        except (OSError, socket.timeout):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--socket", help="bind this path instead of using systemd's fd 3")
    args = parser.parse_args()
    if args.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(args.socket)
        os.chmod(args.socket, 0o660)
        listener.listen(8)
    elif os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        listener = socket.socket(fileno=3)
    else:
        sys.stderr.write("agent-box-secret-broker: no listening socket\n")
        return 2
    store = Store(args.store_root, args.user, args.user, args.uid)
    while True:
        serve_one(listener, store, args.uid)


if __name__ == "__main__":
    sys.exit(main())
