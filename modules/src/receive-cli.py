# Mint and revoke temporary, upload-only browser capabilities (issue #898).

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import unicodedata
import urllib.parse

HOME_DIR = os.path.realpath(os.environ.get("HOME") or os.path.expanduser("~"))

@@include-verbatim:lib/receive.py@@


def parse_amount(raw, units, what):
    match = re.fullmatch(r"([1-9][0-9]*)([A-Za-z]*)", raw or "")
    if not match or match.group(2).lower() not in units:
        raise argparse.ArgumentTypeError("invalid %s: %s" % (what, raw))
    return int(match.group(1)) * units[match.group(2).lower()]


def parse_size(raw):
    return parse_amount(raw, {
        "": 1, "k": 1 << 10, "kb": 1 << 10,
        "m": 1 << 20, "mb": 1 << 20,
        "g": 1 << 30, "gb": 1 << 30,
        "t": 1 << 40, "tb": 1 << 40,
    }, "size")


def parse_ttl(raw):
    return parse_amount(raw, {
        "s": 1, "m": 60, "h": 3600, "d": 86400,
    }, "lifetime")


def parse_slot(raw):
    if not RECEIVE_SLOT_RE.fullmatch(raw or ""):
        raise argparse.ArgumentTypeError("invalid upload slot")
    return raw


def human_size(value):
    for suffix, divisor in (("TiB", 1 << 40), ("GiB", 1 << 30),
                            ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if value >= divisor:
            return "%.1f %s" % (value / divisor, suffix)
    return "%d bytes" % value


def cmd_create(args):
    base = (args.url or os.environ.get("AGENT_BOX_URL", "")).strip()
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise SystemExit("agent-box-receive: AGENT_BOX_URL must be an https URL")
    if args.ttl < 600 or args.ttl > 7 * 86400:
        raise SystemExit("agent-box-receive: --ttl must be between 10m and 7d")
    if args.max_size < 1 or args.max_size > (1 << 40):
        raise SystemExit("agent-box-receive: --max-size must be at most 1T")
    receive_prune()
    slot, token, record = receive_create(args.ttl, args.max_size)
    url = base.rstrip("/") + "/receive/" + slot + "/#" + token
    if args.json:
        print(json.dumps({"slot": slot, "url": url,
                          "expires": record["expires"],
                          "maxBytes": record["max_bytes"]},
                         separators=(",", ":")))
    else:
        print(url)
        print("agent-box-receive: slot %s accepts one file up to %s for %s"
              % (slot, human_size(args.max_size), args.ttl_text),
              file=sys.stderr)


def cmd_list(_args):
    receive_prune()
    try:
        names = sorted(os.listdir(RECEIVE_STATE_DIR))
    except OSError:
        names = []
    print("SLOT\tSTATUS\tOFFSET/TOTAL\tEXPIRES")
    for name in names:
        if not name.endswith(".json"):
            continue
        slot = name[:-5]
        record = receive_read(slot)
        if record is None:
            continue
        total = record.get("length", record["max_bytes"])
        try:
            offset = os.path.getsize(receive_partial_path(slot))
        except OSError:
            offset = total if record["status"] == "complete" else 0
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(record["expires"]))
        print("%s\t%s\t%d/%d\t%s" %
              (slot, record["status"], offset, total, expires))


def cmd_revoke(args):
    record = receive_read(args.slot)
    if record is None:
        raise SystemExit("agent-box-receive: no such slot: %s" % args.slot)
    receive_mark_revoked(args.slot)
    try:
        with receive_slot_lock(args.slot, blocking=False):
            receive_remove_locked(args.slot, receive_read(args.slot))
    except BlockingIOError:
        # The marker already makes authorization fail. An active PATCH checks
        # it between reads and performs the same cleanup while holding the
        # lock, so revocation itself never waits on a slow uploader.
        pass
    print("revoked %s" % args.slot)


def parser():
    result = argparse.ArgumentParser(
        prog="agent-box-receive",
        description="Create a temporary resumable browser upload link.")
    sub = result.add_subparsers(dest="command")
    create = sub.add_parser("create", help="create an upload link (default)")
    create.add_argument("--ttl", default="24h")
    create.add_argument("--max-size", default="2G")
    create.add_argument("--url", help=argparse.SUPPRESS)
    create.add_argument("--json", action="store_true")
    sub.add_parser("list", help="list live upload slots")
    revoke = sub.add_parser("revoke", help="revoke an upload slot")
    revoke.add_argument("slot", type=parse_slot)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv.insert(0, "create")
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv.insert(0, "create")
    args = parser().parse_args(argv)
    if args.command == "create":
        args.ttl_text = args.ttl
        args.ttl = parse_ttl(args.ttl)
        args.max_size = parse_size(args.max_size)
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        cmd_revoke(args)


if __name__ == "__main__":
    main()
