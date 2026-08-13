# (Run via pkgs.writers.writePython3, which supplies the interpreter line.)
"""Read and edit a user's ~/.config/agent-box/env from shell.

The session-spawn wrapper (src/env-exec.sh) and `agent-box-session env`
(src/session-cli.sh) both used to parse this file themselves, in shell. A
value may be quoted and span lines — a PEM certificate or key, issue 212 —
so each of them carried a copy of systemd's env-file grammar, and a drift
between the copies truncates a stored secret without raising anything. They
call this instead: one grammar, in src/env-file.py, shared with the settings
page that writes the file.

    env-cli read PATH        every pair, NUL-separated: KEY \0 VALUE \0 ...
    env-cli ls PATH          key names only, one per line (never a value)
    env-cli set PATH KEY VAL replace KEY, atomically, mode 0600
    env-cli rm PATH KEY      drop KEY

`read` is NUL-separated because a value may hold newlines; the caller
exports the pairs literally and must never eval them.
"""
import os
import re
import sys
import tempfile

@@include:env-file.py@@


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    command, path = argv[1], argv[2]
    args = argv[3:]
    if command == "read":
        out = sys.stdout.buffer
        for key, value in read_pairs(path):
            out.write(key.encode() + b"\0" + value.encode() + b"\0")
        out.flush()
        return 0
    if command == "ls":
        # Names only, sorted — mirrors the settings page, which never
        # surfaces a stored value.
        for key in sorted({key for (key, _) in read_pairs(path)}):
            print(key)
        return 0
    if command in ("set", "rm"):
        if not args or not KEY_RE.match(args[0]):
            sys.stderr.write(f"env: invalid key name {args[0] if args else ''!r}\n")
            return 2
        if command == "rm":
            delete_key(path, args[0])
        else:
            if len(args) < 2:
                sys.stderr.write("env: set needs a value\n")
                return 2
            set_key(path, args[0], args[1])
        return 0
    sys.stderr.write(f"env: unknown command {command!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
