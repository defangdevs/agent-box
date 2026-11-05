"""OpenSMTPD configuration and per-user MIME-to-bus delivery (issue #231)."""

import argparse
import asyncio
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
import secrets
import subprocess
import stat
import sys
import tempfile
import time

import dkim
import dns.exception
import dns.resolver
import spf


CONFIG = "/etc/agent-box/email.json"
COMMAND = "/etc/agent-box/bin/agent-box-email"
SMTPD = "/etc/agent-box/bin/agent-box-email-smtpd"
WEBHOOK = "/etc/agent-box/email-webhook.py"
VERIFIED_HEADER = "X-Agent-Box-Email-Verified"
VERIFIED_POLICY = "exact-domain-v1"
VERIFIED_TOKEN = "/var/lib/agent-box-email/verification-token"


USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DOMAIN = re.compile(r"(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?")


class PermanentMessageError(ValueError):
    """Message content or recipient cannot succeed on a later attempt."""


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


def verification_marker(create=False):
    # Persistent across MTA restarts, but unknown to mail queued before the
    # filter was installed. The root-owned directory is created by systemd.
    path = Path(VERIFIED_TOKEN)
    if create and not path.exists():
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(secrets.token_hex(32) + '\n')
                os.fchmod(output.fileno(), 0o644)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="ascii") as source:
        st = os.fstat(source.fileno())
        token = source.read(66)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or not re.fullmatch(r"[a-f0-9]{64}\n", token):
            raise ValueError("email verification token must be valid, root-owned and non-writable")
    return VERIFIED_POLICY + " " + token.strip()


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
        'filter email_auth proc-exec "' + COMMAND + ' filter" user smtpd group smtpd',
        'listen on 0.0.0.0 port 25 tls pki mail hostname "' + cfg["domains"][0] + '" filter email_auth',
        'listen on "::" port 25 tls pki mail hostname "' + cfg["domains"][0] + '" filter email_auth',
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


def author_domain(raw):
    # One unambiguous author is required, including when SPF alone passes.
    msg = BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True)
    fields = msg.get_all("From", [])
    if msg.defects or len(fields) != 1 or fields[0].defects:
        raise PermanentMessageError("email requires one valid From mailbox")
    addresses = fields[0].addresses
    if len(addresses) != 1 or not addresses[0].username or any(group.display_name is not None for group in fields[0].groups):
        raise PermanentMessageError("email requires one valid From mailbox")
    domain = addresses[0].domain.lower()
    if not DOMAIN.fullmatch(domain) or domain.endswith('.') or '..' in domain:
        raise PermanentMessageError("email requires a DNS From domain")
    return domain


def authenticate_message(raw, ip, sender, helo):
    """Return a fixed SMTP verdict, never a score for the agent to interpret."""
    try:
        domain = author_domain(raw)
    except (ValueError, IndexError):
        return "reject|550 5.7.1 One valid From mailbox is required"
    temporary = False
    # Null reverse paths have no MAIL FROM domain to align. They need DKIM.
    if sender and sender.rpartition('@')[2].lower() == domain:
        try:
            result, _ = spf.check2(i=ip, s=sender, h=helo, timeout=2, querytime=10)
            if result == "pass":
                return "proceed"
            temporary = result == "temperror"
        except (dns.exception.DNSException, OSError):
            temporary = True
    deadline = time.monotonic() + 10
    lookup_failed = False

    def lookup(name, timeout=2):
        nonlocal lookup_failed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            lookup_failed = True
            raise dkim.DnsTimeoutError("verification DNS deadline exceeded")
        try:
            answer = dns.resolver.resolve(name.decode("ascii"), "TXT", lifetime=min(2, remaining))
            records = [b''.join(record.strings) for record in answer]
            return records[0] if len(records) == 1 and len(records[0]) <= 4096 else b''
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return b''
        except dns.exception.DNSException:
            lookup_failed = True
            raise dkim.DnsTimeoutError("verification DNS unavailable")

    try:
        verifier = dkim.DKIM(raw, minkey=1024)
    except dkim.DKIMException:
        return "reject|550 5.7.1 Malformed email headers"
    signatures = [value for name, value in verifier.headers if name.lower() == b'dkim-signature']
    # Bound attacker-selected crypto and DNS work. Ignore partial-body and
    # obsolete SHA-1 signatures even if a library would validate them.
    if len(signatures) > 8:
        return "reject|550 5.7.1 Too many DKIM signatures"
    for index, signature in enumerate(signatures):
        try:
            tags = dkim.util.parse_tag_value(signature)
            if tags.get(b'd', b'').lower() != domain.encode("ascii") or b'l' in tags or tags.get(b'a') not in (b'rsa-sha256', b'ed25519-sha256'):
                continue
            if verifier.verify(idx=index, dnsfunc=lookup):
                return "proceed"
        except (dkim.DKIMException, dkim.util.InvalidTagValueList, ValueError):
            continue
    if temporary or lookup_failed:
        return "reject|451 4.7.1 Sender verification temporarily unavailable"
    return "reject|550 5.7.1 Aligned SPF or DKIM authentication is required"


class EmailAuthFilter:
    """OpenSMTPD 0.7 protocol. DNS/crypto run off the protocol reader."""

    MAX_SESSIONS = 8

    def __init__(self, cfg, output):
        self.limit = cfg["maxMessageBytes"] + 65536
        self.marker = verification_marker()
        self.output = output
        self.sessions = {}
        self.workers = asyncio.Semaphore(2)
        self.tasks = set()

    def emit(self, *fields):
        self.output.write(b'|'.join(f.encode("ascii") if isinstance(f, str) else f for f in fields) + b'\n')
        self.output.flush()

    async def verdict(self, session, token, state, message):
        # Do not build an unbounded queue of messages waiting for DNS.
        if self.workers.locked():
            decision = "reject|451 4.7.1 Sender verifier busy; retry later"
        else:
            async with self.workers:
                try:
                    decision = await asyncio.to_thread(authenticate_message, bytes(message["raw"]),
                                                       state["ip"], message["sender"], state["helo"])
                except Exception:
                    # Unexpected verifier failures must never fail open or
                    # print attacker-controlled mail to the service journal.
                    decision = "reject|451 4.7.1 Sender verification temporarily unavailable"
        if self.sessions.get(session) is state and state.get("message") is message:
            self.emit("filter-result", session, token, decision)
            message["raw"].clear()

    def feed(self, line):
        if line == b'config|ready':
            for event in ("link-identify", "tx-begin", "tx-mail", "tx-reset", "tx-rollback", "link-disconnect"):
                self.emit("register", "report", "smtp-in", event)
            for phase in ("connect", "data-line", "commit"):
                self.emit("register", "filter", "smtp-in", phase)
            self.emit("register", "ready")
            return
        if line.startswith(b'report|'):
            _, version, _, subsystem, event, session, *data = line.split(b'|', 6)
            if version != b'0.7' or subsystem != b'smtp-in':
                raise ValueError("unsupported email filter protocol")
            state = self.sessions.get(session)
            if state is None:
                return
            if event == b'link-disconnect':
                del self.sessions[session]
            elif event == b'link-identify':
                state["helo"] = data[0].split(b'|', 1)[1].decode("utf-8", errors="replace")
            elif event in (b'tx-begin', b'tx-reset', b'tx-rollback'):
                state["message"] = {"raw": bytearray(), "sender": "", "headers": True,
                                    "skip": False, "started": False, "complete": False, "pending": False}
            elif event == b'tx-mail':
                _, result, sender = data[0].split(b'|', 2)
                if result == b'ok':
                    state["message"]["sender"] = sender.decode("utf-8", errors="replace")
            return
        if not line.startswith(b'filter|'):
            return
        _, version, _, subsystem, phase, session, token, *data = line.split(b'|', 7)
        if version != b'0.7' or subsystem != b'smtp-in':
            raise ValueError("unsupported email filter protocol")
        if phase == b'connect':
            if len(self.sessions) >= self.MAX_SESSIONS:
                self.emit("filter-result", session, token, "reject|421 4.7.1 Sender verifier busy; retry later")
            else:
                ip = data[0].rsplit(b'|', 1)[1].strip(b'[]').decode("ascii")
                self.sessions[session] = {"ip": ip, "helo": ""}
                self.emit("filter-result", session, token, "proceed")
            return
        state = self.sessions[session]
        message = state["message"]
        if phase == b'data-line':
            value = data[0]
            if not message["started"]:
                # Remove all client copies (including folded fields) below.
                # This marker is only delivered if commit verification passes.
                self.emit("filter-dataline", session, token, VERIFIED_HEADER + ": " + self.marker)
                message["started"] = True
            if value == b'.':
                message["complete"] = True
            else:
                unstuffed = value[1:] if value.startswith(b'..') else value
                if len(message["raw"]) <= self.limit:
                    message["raw"].extend(unstuffed + b'\r\n')
                if message["headers"]:
                    if value[:1] not in (b' ', b'\t'):
                        message["skip"] = value.split(b':', 1)[0].strip().lower() == VERIFIED_HEADER.lower().encode()
                    if not value:
                        message["headers"] = False
                        message["skip"] = False
                    if message["skip"]:
                        return
            self.emit("filter-dataline", session, token, value)
        elif phase == b'commit':
            if message["pending"] or not message["complete"]:
                raise ValueError("unexpected email filter commit")
            message["pending"] = True
            if len(message["raw"]) > self.limit:
                self.emit("filter-result", session, token, "reject|552 5.3.4 Message too large")
            else:
                task = asyncio.create_task(self.verdict(session, token, state, message))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)


async def run_auth_filter(cfg):
    reader = asyncio.StreamReader(limit=65536)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    auth = EmailAuthFilter(cfg, sys.stdout.buffer)
    while line := await reader.readline():
        auth.feed(line.rstrip(b'\n'))
    if auth.tasks:
        await asyncio.gather(*auth.tasks)


def normalized_message(raw, recipient, sender):
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    if [str(value) for value in msg.get_all(VERIFIED_HEADER, [])] != [verification_marker()]:
        raise PermanentMessageError("email did not pass the SMTP authentication filter")
    author_domain(raw)
    parts = list(msg.walk())
    if len(parts) > 100:
        raise PermanentMessageError("email has more than 100 MIME parts")
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
        "senderAuthenticated": True,
        "authenticationPolicy": VERIFIED_POLICY,
    }


def deliver(cfg, user, stream):
    if user not in cfg["users"] or pwd.getpwuid(os.getuid()).pw_name != user:
        raise ValueError("email delivery must run as its configured recipient Linux user")
    recipient = os.environ.get("ORIGINAL_RECIPIENT", "").lower()
    if recipient not in [user + '@' + d.lower() for d in cfg["domains"]]:
        raise PermanentMessageError("email envelope recipient is not enabled for this Linux user")
    # OpenSMTPD enforces the wire limit before accepting. Its own transport
    # headers are additional bytes when the MDA reads the queued message.
    limit = cfg["maxMessageBytes"] + 65536
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise PermanentMessageError("email exceeds maxMessageBytes")
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
        # Handoff failures leave transport responsibility with OpenSMTPD.
        # Avoid logging arbitrary mail text or tokens from helper output.
        raise OSError("email bus handoff failed; transport must retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("server", "deliver", "check", "filter"))
    parser.add_argument("user", nargs="?")
    args = parser.parse_args()
    if args.command != "deliver" and args.user is not None:
        parser.error("only deliver accepts a Linux user argument")
    if args.command == "deliver" and args.user is None:
        parser.error("deliver requires a Linux user")
    cfg = load_config()
    if args.command == "filter":
        asyncio.run(run_auth_filter(cfg))
    elif args.command == "deliver":
        deliver(cfg, args.user, sys.stdin.buffer)
    else:
        path = Path("/run/agent-box-email/smtpd.conf")
        if args.command == "check":
            print(server_config(cfg), end="")
            return
        if os.geteuid() != 0:
            raise ValueError("email server must be started by its root service")
        verification_marker(create=True)
        with open("/run/agent-box-email/smtpd.conf", "w", encoding="ascii") as output:
            output.write(server_config(cfg))
        os.execv(SMTPD, [SMTPD, "-d", "-f", str(path)])


def cli():
    try:
        main()
    except PermanentMessageError as error:
        print("agent-box-email: " + str(error), file=sys.stderr)
        return 65
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError) as error:
        print("agent-box-email: " + str(error), file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    sys.exit(cli())
