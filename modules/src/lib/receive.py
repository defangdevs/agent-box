# Shared state protocol for temporary browser upload capabilities (issue #898).
#
# This file is included into both receive-cli.py and settings-daemon.py. Keep
# every name receive_-prefixed: the daemon is a large stdlib-only program and
# an unprefixed helper silently colliding there is much harder to diagnose
# than a duplicate import.

RECEIVE_VERSION = 1
RECEIVE_SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}\Z")
RECEIVE_TOKEN_RE = re.compile(
    r"^Bearer ([A-Za-z0-9_-]{16,64})\.([A-Za-z0-9_-]{32,128})\Z"
)
RECEIVE_CONTENT_RE = re.compile(r"^sha256-tree-v1:[0-9a-f]{64}\Z")
RECEIVE_CONTENT_BLOCK = 8 << 20
RECEIVE_STATE_DIR = os.path.realpath(os.environ.get(
    "AGENT_BOX_RECEIVE_STATE_DIR",
    os.path.join(HOME_DIR, ".local", "state", "agent-box", "receive"),
))
RECEIVE_UPLOAD_DIR = os.path.realpath(os.environ.get(
    "AGENT_BOX_UPLOADS_DIR", os.path.join(HOME_DIR, "uploads"),
))
RECEIVE_PARTIAL_DIR = os.path.join(RECEIVE_UPLOAD_DIR, ".agent-box-partials")
RECEIVE_FREE_RESERVE = 256 << 20


def receive_fsync_dir(path):
    """Persist a directory entry when the filesystem supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def receive_ensure_dirs():
    """Create owner-only state, destination, and partial directories."""
    for path in (RECEIVE_STATE_DIR, RECEIVE_UPLOAD_DIR, RECEIVE_PARTIAL_DIR):
        os.makedirs(path, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)


def receive_record_path(slot):
    if not RECEIVE_SLOT_RE.fullmatch(slot or ""):
        raise ValueError("invalid upload slot")
    return os.path.join(RECEIVE_STATE_DIR, slot + ".json")


def receive_lock_path(slot):
    if not RECEIVE_SLOT_RE.fullmatch(slot or ""):
        raise ValueError("invalid upload slot")
    return os.path.join(RECEIVE_STATE_DIR, slot + ".lock")


@contextlib.contextmanager
def receive_capacity_lock():
    """Serialize new-file reservations across upload slots."""
    receive_ensure_dirs()
    path = os.path.join(RECEIVE_STATE_DIR, ".capacity.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def receive_slot_lock(slot, blocking=True):
    """Serialize every mutation of one slot across daemon threads/CLIs."""
    receive_ensure_dirs()
    fd = os.open(receive_lock_path(slot),
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                 0o600)
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        yield
    finally:
        os.close(fd)


def receive_read(slot):
    """Read one well-shaped record, or None for absent/corrupt state."""
    try:
        with open(receive_record_path(slot), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict) or record.get("version") != RECEIVE_VERSION:
        return None
    if record.get("slot") != slot:
        return None
    if not isinstance(record.get("secret_sha256"), str):
        return None
    for key in ("created", "expires", "max_bytes"):
        if not isinstance(record.get(key), int) or record[key] < 0:
            return None
    if record.get("status") not in (
            "waiting", "uploading", "complete", "failed"):
        return None
    if record["status"] != "waiting":
        length = record.get("length")
        valid_length = isinstance(length, int)
        if valid_length:
            valid_length = 0 <= length <= record["max_bytes"]
        if not valid_length:
            return None
        for key in ("name", "metadata", "fingerprint"):
            if not isinstance(record.get(key), str):
                return None
        content = record.get("content")
        if content is not None and not RECEIVE_CONTENT_RE.fullmatch(content):
            return None
    if record["status"] == "complete":
        if not isinstance(record.get("final_name"), str):
            return None
        sha256 = record.get("sha256")
        if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            return None
    return record


def receive_write(record):
    """Atomically publish one owner-only record."""
    receive_ensure_dirs()
    slot = record["slot"]
    path = receive_record_path(slot)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % slot,
                               suffix=".tmp", dir=RECEIVE_STATE_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        receive_fsync_dir(RECEIVE_STATE_DIR)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def receive_secret_digest(slot, secret):
    return hashlib.sha256((slot + "." + secret).encode("ascii")).hexdigest()


def receive_content_digests(path, cancel_path=None):
    """Return the bounded-memory browser fingerprint, SHA-256, and size."""
    leaves = bytearray()
    ordinary = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        while True:
            if cancel_path is not None and os.path.exists(cancel_path):
                raise InterruptedError("upload revoked")
            block = handle.read(RECEIVE_CONTENT_BLOCK)
            if not block:
                break
            total += len(block)
            ordinary.update(block)
            leaves.extend(hashlib.sha256(block).digest())
    prefix = ("agent-box-content-v1\n%d\n" % total).encode("ascii")
    tree = hashlib.sha256(prefix + leaves).hexdigest()
    return "sha256-tree-v1:" + tree, ordinary.hexdigest(), total


def receive_parse_authorization(value):
    match = RECEIVE_TOKEN_RE.fullmatch((value or "").strip())
    return match.groups() if match else (None, None)


def receive_authorized(slot, value, now=None):
    """Return a live matching record; never return the presented secret."""
    token_slot, secret = receive_parse_authorization(value)
    if token_slot != slot:
        return None
    if receive_is_revoked(slot):
        return None
    record = receive_read(slot)
    if record is None:
        return None
    expected = receive_secret_digest(slot, secret)
    if not secrets.compare_digest(record["secret_sha256"], expected):
        return None
    if record["expires"] <= int(time.time() if now is None else now):
        return None
    return record


def receive_partial_path(slot):
    return os.path.join(RECEIVE_PARTIAL_DIR, slot + ".part")


def receive_revoked_path(slot):
    if not RECEIVE_SLOT_RE.fullmatch(slot or ""):
        raise ValueError("invalid upload slot")
    return os.path.join(RECEIVE_STATE_DIR, slot + ".revoked")


def receive_is_revoked(slot):
    return os.path.exists(receive_revoked_path(slot))


def receive_mark_revoked(slot):
    """Make a capability unusable without waiting for its active request."""
    receive_ensure_dirs()
    path = receive_revoked_path(slot)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    os.close(fd)
    receive_fsync_dir(RECEIVE_STATE_DIR)


def receive_reserved_bytes(exclude=None, now=None):
    """Return bytes promised but not yet written by active uploads."""
    now = int(time.time() if now is None else now)
    try:
        names = os.listdir(RECEIVE_STATE_DIR)
    except OSError:
        return 0
    reserved = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        slot = name[:-5]
        if slot == exclude or not RECEIVE_SLOT_RE.fullmatch(slot):
            continue
        record = receive_read(slot)
        inactive = record is None or record.get("status") != "uploading"
        expired = record is not None and record["expires"] <= now
        if inactive or expired or receive_is_revoked(slot):
            continue
        try:
            written = os.path.getsize(receive_partial_path(slot))
        except OSError:
            written = 0
        reserved += max(0, record.get("length", 0) - written)
    return reserved


def receive_remove_locked(slot, record=None):
    """Remove a slot and any unfinished bytes while its lock is held."""
    # A completed partial is a second hard link to the published file.
    with contextlib.suppress(OSError):
        os.unlink(receive_partial_path(slot))
    with contextlib.suppress(OSError):
        os.unlink(receive_record_path(slot))
    with contextlib.suppress(OSError):
        os.unlink(receive_revoked_path(slot))
    receive_fsync_dir(RECEIVE_PARTIAL_DIR)
    receive_fsync_dir(RECEIVE_STATE_DIR)


def receive_create(ttl, max_bytes, now=None):
    """Mint a capability, persist only its digest, and return slot+token."""
    receive_ensure_dirs()
    now = int(time.time() if now is None else now)
    for _attempt in range(32):
        slot = secrets.token_urlsafe(12)
        # A leading "-" is URL-safe but argparse reads it as an option in
        # `agent-box-receive revoke SLOT`. Keep generated capabilities usable
        # as ordinary positional arguments without requiring a hidden `--`.
        if not slot[0].isalnum():
            continue
        secret = secrets.token_urlsafe(32)
        record = {
            "version": RECEIVE_VERSION,
            "slot": slot,
            "secret_sha256": receive_secret_digest(slot, secret),
            "created": now,
            "expires": now + ttl,
            "max_bytes": max_bytes,
            "status": "waiting",
        }
        path = receive_record_path(slot)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True,
                          separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            receive_fsync_dir(RECEIVE_STATE_DIR)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        return slot, slot + "." + secret, record
    raise RuntimeError("could not allocate an upload slot")


def receive_prune(now=None):
    """Opportunistically remove expired records and abandoned partials."""
    now = int(time.time() if now is None else now)
    try:
        names = os.listdir(RECEIVE_STATE_DIR)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        slot = name[:-5]
        if not RECEIVE_SLOT_RE.fullmatch(slot):
            continue
        try:
            with receive_slot_lock(slot, blocking=False):
                record = receive_read(slot)
                expired = record is not None and record["expires"] <= now
                if record is not None and (
                        expired or receive_is_revoked(slot)):
                    receive_remove_locked(slot, record)
                    removed += 1
        except BlockingIOError:
            continue
    return removed


def receive_safe_name(raw):
    """Turn browser metadata into a filename, never a filesystem path."""
    raw = (raw or "").replace("\\", "/").split("/")[-1]

    def safe_char(ch):
        category = unicodedata.category(ch)
        return not category.startswith("C") and category not in ("Zl", "Zp")
    raw = "".join(ch for ch in raw if safe_char(ch)).strip()
    raw = raw.lstrip(".-")
    if not raw or raw in (".", ".."):
        raw = "upload.bin"
    encoded = raw.encode("utf-8", "ignore")
    if len(encoded) > 200:
        encoded = encoded[:200]
        while True:
            try:
                raw = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
    return raw or "upload.bin"


def receive_publish(partial, wanted, slot):
    """Link a completed partial under a deterministic non-overwriting name.

    link(), rather than rename(), gives us RENAME_NOREPLACE semantics using
    only Python's stdlib. The slot suffix makes collision vanishingly rare;
    recognizing the same inode makes a crash after link but before the record
    update safely retryable instead of publishing a second copy.
    """
    stem, suffix = os.path.splitext(receive_safe_name(wanted))
    for discriminator in (slot[:8], slot):
        name = "%s-%s%s" % (stem, discriminator, suffix)
        target = os.path.join(RECEIVE_UPLOAD_DIR, name)
        try:
            os.link(partial, target, follow_symlinks=False)
        except FileExistsError:
            try:
                if os.path.samestat(os.stat(partial, follow_symlinks=False),
                                    os.stat(target, follow_symlinks=False)):
                    receive_fsync_dir(RECEIVE_UPLOAD_DIR)
                    return name
            except OSError:
                pass
            continue
        receive_fsync_dir(RECEIVE_UPLOAD_DIR)
        return name
    raise OSError(errno.EEXIST, "an upload already owns that name")
