"""OpenSMTPD configuration and per-user MIME-to-bus delivery (issue #231)."""

import argparse
import base64
import email.policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import stat
import sys


CONFIG = "/etc/agent-box/email.json"
COMMAND = "/etc/agent-box/bin/agent-box-email"
SMTPD = "/etc/agent-box/bin/agent-box-email-smtpd"
WEBHOOK = "/etc/agent-box/email-webhook.py"


USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DOMAIN = re.compile(r"(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?")


def load_config():
    # Both backends own this fixed file and the fixed executable symlinks.
    # A caller cannot supply an alternate config or executable as CLI data.
    with open(CONFIG, encoding="ascii") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError("email configuration must be a root-owned non-writable regular file")
        cfg = json.load(fh)
    return validate_config(cfg)


def validate_config(cfg):
    if not cfg.get("domains") or not all(DOMAIN.fullmatch(d) for d in cfg["domains"]):
        raise ValueError("email domains must be DNS names")
    if not cfg.get("users") or not all(USER.fullmatch(u) for u in cfg["users"]):
        raise ValueError("email needs explicitly enabled Linux users")
    for key in ("certificateFile", "keyFile"):
        value = cfg.get(key, "")
        if not value.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9/_.+-]+", value):
            raise ValueError("email " + key + " must be an absolute path without whitespace or metacharacters")
    if type(cfg.get("maxMessageBytes")) is not int or not 1024 <= cfg["maxMessageBytes"] <= 8 * 1024 * 1024:
        raise ValueError("email maxMessageBytes must be between 1024 and 8388608")
    return cfg


def server_config(cfg):
    # No envelope fields are interpolated into an MDA command. OpenSMTPD sets
    # ORIGINAL_RECIPIENT/SENDER in the child's environment and drops uid first.
    lines = [
        'pki mail cert "' + cfg["certificateFile"] + '"',
        'pki mail key "' + cfg["keyFile"] + '"',
        'smtp max-message-size ' + str(cfg["maxMessageBytes"]),
        'smtp limit max-mails 10',
        'smtp limit max-rcpt 10',
        'queue ttl 1d',
        'listen on 0.0.0.0 port 25 tls pki mail hostname "' + cfg["domains"][0] + '"',
        'listen on "::" port 25 tls pki mail hostname "' + cfg["domains"][0] + '"',
    ]
    # An explicit userbase keeps .forward files out of this intake path.
    # Uid/gid are resolved on this host at service start, not baked into Nix.
    accounts = {u: pwd.getpwnam(u) for u in cfg["users"]}
    lines.append('table email_users { ' + ', '.join(
        '"' + u + '"="' + str(p.pw_uid) + ':' + str(p.pw_gid) + ':/var/empty"'
        for u, p in sorted(accounts.items())) + ' }')
    for user in sorted(cfg["users"]):
        # Literal mailaddr tables accept plus-tags; anchored regex matching
        # must reject those at RCPT time instead of queuing undeliverable mail.
        addresses = ', '.join('"^' + user + '@' + d.lower().replace('.', '[.]') + '$"' for d in cfg["domains"])
        command = " ".join((COMMAND, "deliver", user))
        lines += [
            'table rcpt_' + user + ' { ' + addresses + ' }',
            'action deliver_' + user + ' mda "' + command + '" userbase <email_users>',
            'match from any for rcpt-to regex <rcpt_' + user + '> action deliver_' + user,
        ]
    lines += ['match from any for any reject']
    return '\n'.join(lines) + '\n'


def normalized_message(raw, recipient, sender):
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    parts = list(msg.walk())
    if len(parts) > 100:
        raise ValueError("email has more than 100 MIME parts")
    text, html, attachments = [], [], []
    for part in parts:
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True) or b""
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments.append({"filename": str(part.get_filename() or "attachment"),
                                "contentType": part.get_content_type(),
                                "base64": base64.b64encode(content).decode("ascii")})
        elif part.get_content_type() in ("text/plain", "text/html"):
            try:
                decoded = content.decode(part.get_content_charset() or "utf-8", errors="replace")
            except LookupError:
                decoded = content.decode("utf-8", errors="replace")
            (text if part.get_content_type() == "text/plain" else html).append(decoded)
        else:
            attachments.append({"filename": "attachment", "contentType": part.get_content_type(),
                                "base64": base64.b64encode(content).decode("ascii")})
    # The first Received header is prepended by OpenSMTPD and remains stable
    # on transport retries, unlike a timestamp sampled afresh by this adapter.
    received = str(msg.get("Received", "")).rsplit(";", 1)[-1].strip()
    try:
        received = parsedate_to_datetime(received).isoformat()
    except (ValueError, TypeError, IndexError):
        received = ""
    return {
        "recipient": recipient, "envelopeSender": sender, "receivedAt": received,
        "title": str(msg.get("Subject", "")), "summary": '\n'.join(text)[:200],
        "headers": {key: [str(v) for v in msg.get_all(header, [])] for key, header in (
            ("from", "From"), ("to", "To"), ("cc", "Cc"), ("messageId", "Message-ID"),
            ("inReplyTo", "In-Reply-To"), ("references", "References"))},
        "text": '\n'.join(text), "html": '\n'.join(html), "attachments": attachments,
        "rawMimeBase64": base64.b64encode(raw).decode("ascii"),
        "senderAuthenticated": False,
    }


def deliver(cfg, user, stream):
    if user not in cfg["users"] or pwd.getpwuid(os.getuid()).pw_name != user:
        raise ValueError("email delivery must run as its configured recipient Linux user")
    recipient = os.environ.get("ORIGINAL_RECIPIENT", "").lower()
    if recipient not in [user + '@' + d.lower() for d in cfg["domains"]]:
        raise ValueError("email envelope recipient is not enabled for this Linux user")
    # OpenSMTPD enforces the wire limit before accepting. Its own transport
    # headers are additional bytes when the MDA reads the queued message.
    limit = cfg["maxMessageBytes"] + 65536
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("email exceeds maxMessageBytes")
    sender = os.environ.get("SENDER", "")
    payload = normalized_message(raw, recipient, sender)
    # The MTA's prepended Received header contains its own transaction id.
    # Hashing delivered bytes + envelope preserves retry identity without
    # trusting the sender's Message-ID or collapsing separate SMTP receives.
    ident = hashlib.sha256(raw + b'\0' + recipient.encode() + b'\0' + sender.encode()).hexdigest()
    body = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    env = dict(os.environ)
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    env["LOCAL_WEBHOOK_STATE_DIR"] = str(Path(env["HOME"]) / ".local/state/local-webhook")
    env["LOCAL_WEBHOOK_HTTP_SOCK"] = "/run/agent-box-webhook/" + user + ".sock"
    proc = subprocess.run([sys.executable, WEBHOOK, "emit", "email", "-",
                           "--event", "email.received", "--delivery-id", ident],
                          input=body.encode("ascii"), env=env, timeout=60,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode:
        # All adapter failures leave transport responsibility with OpenSMTPD.
        # Avoid logging arbitrary mail text or tokens from helper output.
        raise OSError("email bus handoff failed; transport must retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("server", "deliver", "check"))
    parser.add_argument("user", nargs="?")
    args = parser.parse_args()
    if args.command != "deliver" and args.user is not None:
        parser.error("only deliver accepts a Linux user argument")
    if args.command == "deliver" and args.user is None:
        parser.error("deliver requires a Linux user")
    cfg = load_config()
    if args.command == "deliver":
        deliver(cfg, args.user, sys.stdin.buffer)
    else:
        path = Path("/run/agent-box-email/smtpd.conf")
        if args.command == "check":
            print(server_config(cfg), end="")
            return
        if os.geteuid() != 0:
            raise ValueError("email server must be started by its root service")
        with open("/run/agent-box-email/smtpd.conf", "w", encoding="ascii") as output:
            output.write(server_config(cfg))
        os.execv(SMTPD, [SMTPD, "-d", "-f", str(path)])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError) as error:
        print("agent-box-email: " + str(error), file=sys.stderr)
        sys.exit(75)
