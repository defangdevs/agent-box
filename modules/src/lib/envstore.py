# The env store: ONE implementation of the KEY=value file format that
# ~/.config/agent-box/env and ~/.config/agent-box/profiles/<name>.env are
# written in (issue #212).
#
# Why this is a library and not another copy of the loop
# -----------------------------------------------------
# Six places used to parse this format independently — the env-exec wrapper,
# `agent-box-session env`, `agent-box-profile`, the settings daemon, the
# webhook spawner, and the profile reader inside the wrapper. Every one of
# them re-derived the same three rules (skip a comment, validate the key,
# strip one pair of quotes), and a format CHANGE therefore had to land in six
# dialects at once or the file would mean different things to different
# readers. Multi-line values are exactly such a change: a PEM written by the
# settings page and read by a line-per-pair shell loop does not come back as a
# PEM, it comes back as its first line plus a handful of junk exports.
#
# So the format has one owner. Python, because the readers that cannot be
# Python (a systemd ExecStart, a hook) go through the agent-box-envstore CLI
# instead, and because a quote-and-continuation scanner in POSIX sh is how the
# next bug gets written.
#
# The file format
# ---------------
#   * A line whose first non-blank character is `#`, or that is blank, is a
#     comment. A line with no `=` is skipped, as is one whose key is not
#     [A-Za-z_][A-Za-z0-9_]* — a hand-edited file must never be able to make a
#     reader do something other than set a variable.
#   * An UNQUOTED value ends at the end of its line, with trailing whitespace
#     removed. Use quotes to keep whitespace. `KEY = value` is not an
#     assignment: the key is `KEY ` and the line is skipped.
#   * A DOUBLE-QUOTED value may span lines: it ends at the next unescaped `"`,
#     however many newlines away that is. `\"` is a literal quote and `\\` a
#     literal backslash; every other backslash is itself, and a newline is a
#     newline. `\n` is NOT an escape — this is systemd's env-file rule
#     (env-file.c), which is what an operator moving a PEM off a systemd box
#     expects, and it means a value can never smuggle in a character the file
#     does not literally contain.
#   * A SINGLE-QUOTED value is legacy: one pair of quotes is stripped when
#     both sit on the same line. It does not continue across lines. Kept
#     because the pre-#212 readers stripped it and files in the wild have it.
#   * An unterminated `"` does not swallow the rest of the file. The line is
#     taken as a legacy raw value and parsing resumes with the next one, so
#     one corrupt entry costs one entry.
#   * A value may not contain NUL, and an entry that does is skipped on read
#     and refused on write. execve cannot carry one anyway — the variable
#     would silently truncate at it — and NUL is what frames the argument
#     vectors this store feeds (see session-cli.sh's profile decode), so a
#     value holding one could turn itself into two arguments.
#
# Writing is the exact inverse: a value is quoted only when it has to be, so a
# file of ordinary tokens stays the plain KEY=value text that `grep` and a
# human both expect.
#
# A write is a read-modify-write, so it takes a lock. os.replace makes a
# READER see a whole document and nothing more, but it does not stop two
# writers that both started from the same base: the second one publishes a
# file that never contained the first one's key, and a secret disappears with
# no error anywhere. That is agent-box#254 in another file. One lock covers
# every writer here because every writer now goes through this function —
# in-process for the settings daemon, through agent-box-envstore for the
# shell CLIs — which is the point of the format having one owner.
import contextlib
import fcntl
import json
import os
import re
import tempfile

# The key charset every reader already agreed on (the settings daemon's
# KEY_RE, the shell CLIs' valid_key).
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ENV_HEADER = (
    "# Managed by agent-box settings page. KEY=value, one per line.\n"
    "# Do not add secrets by hand here unless you know what you are doing.\n"
    "# A value may span lines when it is double-quoted (issue #212).\n"
)

# Launch config, not environment: agent-box-profile turns these into harness
# arguments and env-exec must not export them (a SYSTEM_PROMPT in the
# environment of everything the agent runs is a footgun, not a feature).
PROFILE_RESERVED = ("HARNESS", "MODEL", "EFFORT", "SYSTEM_PROMPT")


def profile_header(name):
    return (
        f'# agent-box agent profile "{name}" — managed by agent-box-profile.\n'
        "# KEY=value, one per line. HARNESS/MODEL/EFFORT/SYSTEM_PROMPT become\n"
        "# harness arguments; any other key becomes session environment, which\n"
        "# every session of this user can read (issue #135).\n"
        "# A value may span lines when it is double-quoted (issue #212).\n"
    )


def valid_key(key):
    return bool(KEY_RE.match(key))


def valid_value(value):
    """NUL disqualifies a value. See the format notes at the top."""
    return "\0" not in value


def _unescape(text):
    """Resolve `\\"` and `\\\\`; leave every other backslash literal."""
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and text[i + 1:i + 2] in ('"', "\\"):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _scan_quoted(rest, lines, index):
    """Read a double-quoted value whose opening quote is already consumed.

    `rest` is what followed that quote on its own line, `lines[index:]` the
    lines after it. Returns (value, next_index), or None when the quote is
    never closed — the caller then falls back to the legacy single-line
    reading rather than losing every entry below it.
    """
    chunks = []
    buf = rest
    while True:
        piece = []
        pos = 0
        while pos < len(buf):
            char = buf[pos]
            # Keep the escape pair intact here and resolve it once, after the
            # lines are joined; that way a `\"` cannot be mistaken for the
            # terminator and a `\\` cannot hide one.
            if char == "\\" and buf[pos + 1:pos + 2] in ('"', "\\"):
                piece.append(buf[pos:pos + 2])
                pos += 2
                continue
            if char == '"':
                chunks.append("".join(piece))
                return _unescape("\n".join(chunks)), index
            piece.append(char)
            pos += 1
        chunks.append("".join(piece))
        if index >= len(lines):
            return None
        buf = lines[index]
        index += 1


def parse(text):
    """Return the file's [(key, value)] in file order, junk lines skipped.

    Later duplicates are kept as duplicates; callers that want one value per
    key take the last (see `as_dict`), which is what every reader of this
    format has always done.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pairs = []
    index = 0
    while index < len(lines):
        # Left-stripped, never fully stripped: a quoted value owns every byte
        # after its opening quote, trailing blanks on a continued line
        # included.
        line = lines[index].lstrip()
        index += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # No strip on the key: `KEY = value` is not this format, and a reader
        # that quietly accepted it disagreed with the four that did not. The
        # line's own leading indentation is already gone (lstrip above).
        if not valid_key(key):
            continue
        if value.startswith('"'):
            scanned = _scan_quoted(value[1:], lines, index)
            if scanned is not None:
                value, index = scanned
                if valid_value(value):
                    pairs.append((key, value))
                continue
            # Unterminated quote: fall through and read THIS line the legacy
            # way, leaving the lines below it to be parsed on their own.
        value = value.rstrip()
        if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if not valid_value(value):
            continue
        pairs.append((key, value))
    return pairs


def as_dict(pairs):
    """Collapse [(key, value)] to {key: value}, last occurrence winning."""
    return dict(pairs)


def load(path):
    """`parse` the file at `path`; a missing or unreadable file is empty.

    Unreadable is deliberately not an error: this runs on the session-spawn
    path, where a permission problem on the secrets file must cost the
    secrets, not the session.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return parse(handle.read())
    except OSError:
        return []


def needs_quoting(value):
    if value != value.strip():
        return True
    if any(char in value for char in '\n\r"\\'):
        return True
    return value[:1] in ("'", '"')


def quote(value):
    """Render one value, quoting it only when the format requires it."""
    if not needs_quoting(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(pairs, header=""):
    return header + "".join(f"{key}={quote(value)}\n" for key, value in pairs)


class EnvStoreError(ValueError):
    """A value the format cannot carry."""


def save(path, pairs, header="", mode=0o600, dir_mode=0o700):
    """Atomically publish `pairs` to `path`.

    Same rename-over-a-temp-file-in-the-same-directory shape the settings
    daemon and both shell CLIs already used, so a reader either sees the old
    file or the new one. 0600 because the value beside the key is a secret.
    """
    for key, value in pairs:
        if not valid_value(value):
            raise EnvStoreError(f"{key}: a value may not contain NUL")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=dir_mode, exist_ok=True)
    # makedirs applies `mode` only when it CREATES the directory, and a
    # directory made by an older `mkdir -p` under umask 022 is 0755. The old
    # shell writer chmod'ed it on every write for that reason; keep doing so,
    # because the file inside is a secret.
    try:
        os.chmod(directory, dir_mode)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".envstore.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render(pairs, header))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def locked(path):
    """Hold an exclusive advisory lock on `path` for one read-modify-write.

    The lock file sits beside the store, the convention sessions.json already
    uses. fcntl rather than util-linux's flock(1) because every writer of this
    format is python now; the shell CLIs reach it through agent-box-envstore
    and inherit the lock without knowing it exists.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle = open(path + ".lock", "a", encoding="utf-8")
    try:
        try:
            os.fchmod(handle.fileno(), 0o600)
        except OSError:
            pass
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def update(path, assignments, header="", drop=()):
    """Rewrite `path` with `drop` and the assigned keys removed, then append.

    One read-modify-write under the lock, so `set` keeps the file's order for
    untouched keys and moves a re-set key to the end — the behavior the shell
    `env_rewrite` and the daemon's `set_key` both had — and a concurrent
    writer cannot drop the key this one did not touch.
    """
    with locked(path):
        doomed = set(drop) | {key for key, _ in assignments}
        kept = [(k, v) for (k, v) in load(path) if k not in doomed]
        save(path, kept + list(assignments), header)


def keys(path):
    """Sorted, de-duplicated key NAMES — never the values.

    The settings page and both CLIs list keys and refuse to show values; that
    rule belongs to the format's owner, not to each caller.
    """
    return sorted({key for key, _ in load(path)})


def dumps(path):
    """The store as a JSON object, for the shell callers that hold jq."""
    return json.dumps(as_dict(load(path)), ensure_ascii=False)
