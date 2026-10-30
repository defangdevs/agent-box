# Secret custody: the wire protocol and the record store (issue #922).
#
# One owner for the two things the root broker and the unprivileged CLI must
# agree on: how a request is framed and which names are legal. The broker
# also uses the Store below; the CLI never touches it, because the whole
# point is that the agent user cannot read the records.
#
# Spliced ABOVE secret-broker.py and secret-cli.py by the generated module,
# so the names bind in both; this file therefore has no `if __name__` block.
import fcntl
import json
import os
import re
import socket
import struct

SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SECRET_MAX_FRAME = 64 * 1024
SECRET_MAX_VALUE = 32 * 1024
SECRET_MAX_NAMES = 256
SECRET_OPS = ("list", "set", "remove")


class SecretError(Exception):
    """A refusal the caller may be told about; never carries a value."""


def valid_secret_name(name):
    return isinstance(name, str) and SECRET_NAME_RE.match(name) is not None


def valid_secret_value(value):
    if not isinstance(value, str) or value == "" or "\0" in value:
        return False
    return len(value.encode("utf-8")) <= SECRET_MAX_VALUE


def recv_exact(conn, count):
    chunks = []
    while count:
        chunk = conn.recv(count)
        if not chunk:
            raise SecretError("connection closed mid-frame")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def send_frame(conn, obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > SECRET_MAX_FRAME:
        raise SecretError("response too large")
    conn.sendall(struct.pack(">I", len(body)) + body)


def recv_frame(conn):
    (length,) = struct.unpack(">I", recv_exact(conn, 4))
    # The length is checked BEFORE any allocation: a peer must not be able to
    # make the root process reserve memory by lying in four bytes.
    if length == 0 or length > SECRET_MAX_FRAME:
        raise SecretError("bad frame length")
    try:
        obj = json.loads(recv_exact(conn, length).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SecretError("malformed request")
    if not isinstance(obj, dict):
        raise SecretError("malformed request")
    return obj


def validate_request(req):
    """Strict schema: unknown fields are refused, not ignored, so a caller
    cannot smuggle a uid, a path or a unit property past a future reader."""
    op = req.get("op")
    if op not in SECRET_OPS:
        raise SecretError("unknown operation")
    allowed = {"list": {"op"}, "set": {"op", "name", "value"},
               "remove": {"op", "name"}}[op]
    if set(req) - allowed:
        raise SecretError("unexpected field")
    if op != "list" and not valid_secret_name(req.get("name")):
        raise SecretError("invalid name")
    if op == "set" and not valid_secret_value(req.get("value")):
        raise SecretError("invalid value")
    return op


class Store:
    """Root-only records for ONE namespace.

    Layout: <root>/<namespace>/{meta.json,.lock,<NAME>.rec}. Every access goes
    through a directory fd with O_NOFOLLOW, so a planted symlink is an error
    and never a redirect; every write is temp-file + fsync + rename, so a
    crash leaves the old record or the new one.
    """

    def __init__(self, root, namespace, user, uid):
        if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", namespace) or namespace in (".", ".."):
            raise SecretError("invalid namespace")
        os.makedirs(root, mode=0o700, exist_ok=True)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.mkdir(namespace, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            self.dir_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=root_fd)
        finally:
            os.close(root_fd)
        self._bind(user, uid)

    def close(self):
        os.close(self.dir_fd)

    def _read(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.dir_fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise SecretError("store entry unreadable")
        with os.fdopen(fd, "rb") as handle:
            try:
                return json.loads(handle.read(SECRET_MAX_FRAME * 2))
            except ValueError:
                raise SecretError("store entry corrupt")

    def _write(self, name, obj):
        tmp = f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     0o600, dir_fd=self.dir_fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(obj).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(tmp, name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)

    def _bind(self, user, uid):
        # A namespace is bound to the identity it was created for. A uid that
        # was recycled for a different account must not inherit the records.
        meta = self._read("meta.json")
        if meta is None:
            self._write("meta.json", {"user": user, "uid": uid})
        elif meta.get("user") != user or meta.get("uid") != uid:
            raise SecretError("namespace belongs to another identity")

    def _locked(self):
        fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                     dir_fd=self.dir_fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def list(self):
        """Names and metadata only; no value is ever loaded into the answer."""
        out = []
        for entry in sorted(os.listdir(self.dir_fd)):
            if not entry.endswith(".rec") or not valid_secret_name(entry[:-4]):
                continue
            rec = self._read(entry) or {}
            out.append({"name": entry[:-4], "version": rec.get("version", 0),
                        "configured": not rec.get("tombstone", False)})
        return out

    def set(self, name, value):
        lock = self._locked()
        try:
            old = self._read(name + ".rec") or {}
            version = int(old.get("version", 0)) + 1
            self._write(name + ".rec", {"version": version, "value": value})
            return version
        finally:
            os.close(lock)

    def remove(self, name):
        """Delete the value but keep a tombstone, so a retired name stays
        protected rather than reappearing from a stale legacy copy."""
        lock = self._locked()
        try:
            old = self._read(name + ".rec")
            if old is None:
                raise SecretError("no such secret")
            version = int(old.get("version", 0)) + 1
            self._write(name + ".rec", {"version": version, "tombstone": True})
            return version
        finally:
            os.close(lock)


def handle_request(store, req):
    op = validate_request(req)
    if op == "list":
        return {"ok": True, "secrets": store.list()[:SECRET_MAX_NAMES]}
    if op == "set":
        return {"ok": True, "version": store.set(req["name"], req["value"])}
    return {"ok": True, "version": store.remove(req["name"])}
