"""Two probes tests/ttyd-isolation.nix (issue #628) needs and curl cannot do.

`connect` is the local-user half: open the terminal transport directly, as
whichever user runs this, and say whether the kernel allowed it. That is the
whole vulnerability in one syscall - before the fix it was a 127.0.0.1 port
every local user could open, and after it a unix socket only its own user
and caddy may reach.

`attach` is the browser half: complete ttyd's WebSocket handshake through
Caddy (basic auth, self-signed TLS, an Origin of the caller's choosing), send
real keystrokes, and report the terminal output that came back. curl can see
a 101, but it cannot type - and "--writable still works for the authenticated
owner" is exactly what a socket-permission change could break.

Exit codes: 0 attached/connected, 3 refused (the interesting negative), 1 for
anything else, so a caller can tell a refusal from a broken probe.
"""
import argparse
import json
import ssl
import socket
import sys
import time

# ttyd's wire protocol: the client's first message is a JSON init, after
# which a frame starting with '0' is INPUT and a server frame starting with
# '0' is terminal OUTPUT.
INPUT = "0"
OUTPUT = ord("0")


def connect(args):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(args.path)
    except OSError as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}")
        return 3
    sock.close()
    print("CONNECTED")
    return 0


def attach(args):
    from websocket import (ABNF, WebSocketConnectionClosedException,
                           WebSocketTimeoutException, create_connection)

    header = []
    if args.password:
        import base64
        raw = f"{args.user}:{args.password}".encode()
        header.append("Authorization: Basic "
                      + base64.b64encode(raw).decode())
    kwargs = {
        "subprotocols": ["tty"],
        "timeout": 20,
        "header": header,
        "sslopt": {"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
    }
    if args.origin:
        kwargs["origin"] = args.origin
    else:
        # websocket-client sends an Origin derived from the URL unless told
        # not to. A browser always sends one, so this case exists to pin
        # what ttyd does with a client that omits it: --check-origin refuses
        # it too.
        kwargs["suppress_origin"] = True

    try:
        conn = create_connection(args.url, **kwargs)
    except Exception as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}")
        return 3

    conn.send(json.dumps({"AuthToken": "", "columns": 120, "rows": 40}))
    typed = "" if (args.send is None and args.enter) else args.send
    if typed is not None:
        # Give the pane a moment to paint (and, when it is attaching, to
        # exec tmux) before typing into it. The trailing carriage return is
        # the Enter key -- typing a command without pressing it proves
        # nothing, and spelling a real newline through Nix, the shell and
        # argparse only invites it to arrive as two characters. --enter is
        # that Enter with nothing typed before it, which is what the attach
        # wrapper's "press Enter to start it here" offer waits for -- a flag
        # rather than an empty --send, because an empty '' argument inside a
        # Nix indented string TERMINATES the string (see AGENTS.md).
        time.sleep(2)
        conn.send(INPUT + typed + "\r")

    collected = bytearray()
    conn.settimeout(2)
    closed = False
    deadline = time.time() + args.read
    while time.time() < deadline:
        try:
            frame = conn.recv_frame()
        except WebSocketTimeoutException:
            # A quiet terminal, which is the ordinary case between frames.
            continue
        except WebSocketConnectionClosedException:
            # NOT quiet: the peer went away. Catching this with the timeout
            # would spin here until the deadline and then report ATTACHED
            # over a connection that no longer exists (CodeRabbit, PR #650).
            closed = True
            break
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 1
        if frame is None:
            continue
        if frame.opcode not in (ABNF.OPCODE_BINARY, ABNF.OPCODE_TEXT):
            continue
        data = frame.data
        if not isinstance(data, (bytes, bytearray)):
            data = data.encode()
        if data and data[0] == OUTPUT:
            collected += data[1:]
    try:
        conn.close()
    except Exception:
        pass

    text = collected.decode("utf-8", "replace")
    if closed and not collected:
        # An upgrade that carried nothing before the peer hung up is not an
        # attach: the caller asked whether the terminal is reachable, and
        # the answer here is no.
        print("CLOSED: peer closed the WebSocket before any terminal output")
        return 1
    print(f"ATTACHED {len(text)} bytes{' (peer closed)' if closed else ''}")
    print(text)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("connect")
    one.add_argument("path")
    one.set_defaults(func=connect)

    two = sub.add_parser("attach")
    two.add_argument("url")
    two.add_argument("--user", default="")
    two.add_argument("--password", default="")
    two.add_argument("--origin", default="")
    two.add_argument("--send", default=None,
                     help="type this line, then press Enter")
    two.add_argument("--enter", action="store_true",
                     help="press Enter with nothing typed")
    two.add_argument("--read", type=float, default=6.0)
    two.set_defaults(func=attach)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
