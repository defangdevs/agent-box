#!/usr/bin/env python3
"""Round-trip check for the env-file format (issue 212).

The per-user secrets file (~/.config/agent-box/env) holds systemd env-file
syntax, so a value may be quoted and span lines — that is how a PEM (an x509
certificate, a private key) is stored. Its one implementation is
modules/src/env-file.py: the settings daemon splices it in, and so does
modules/src/env-cli.py, the helper the session-spawn wrapper and
`agent-box-session env` call.

Nothing here raises on a bad round trip in production — a value that does not
survive its own encoding comes back truncated — so this asserts it directly:
every shape a stored secret can take is written, read back, and compared.
The CLI is exercised too, since NUL-framed output is the contract the shell
callers depend on.

Run: python3 tests/env-file-format.py DIR
where DIR is modules/src. Prints a summary and exits non-zero on the first
divergence.
"""
import os
import re
import subprocess
import sys
import tempfile

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBogIBAAKCAQEAq1sT8QVw==\n"
    "aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk=\n"
    "-----END RSA PRIVATE KEY-----\n"
)

# One entry per shape a stored secret can take. The "=" padding, the quote
# and the backslash are the three the pre-212 line-based reader got wrong.
VALUES = [
    "ghp_supersecret",
    "sk-ant-api03-AAAA/BBBB+CCCC=",
    "",
    " ",
    "  leading and trailing  ",
    "a b c",
    'has "double" quotes',
    "has 'single' quotes",
    "back\\slash and \\\" both",
    "trailing backslash \\",
    "dollar $HOME and `backtick`",
    "hash # not a comment",
    "semi ; colon",
    PEM,
    PEM.rstrip("\n"),
    "token-with-trailing-newline\n",
    "line1\nline2\n\n\nline5",
    "tab\tseparated\t",
    "ünïcödé ✓",
    "=starts with equals",
    "#starts with hash",
]

# Files a human (or an older agent-box) may have written by hand.
HANDWRITTEN = [
    ("A=1\nB=2\n", [("A", "1"), ("B", "2")]),
    ("A=1", [("A", "1")]),                        # no trailing newline
    ("# comment\nA=1\n", [("A", "1")]),
    ("; comment\nA=1\n", [("A", "1")]),
    ("  A = 1 \n", []),                           # space in key: not a name
    ("9BAD=x\nGOOD=y\n", [("GOOD", "y")]),
    ("A='single quoted'\n", [("A", "single quoted")]),
    ('A="multi\nline"\nB=2\n', [("A", "multi\nline"), ("B", "2")]),
    # Unbalanced quote: only a pre-212 file can hold one inside a value, so
    # the reader falls back to the old line format rather than losing keys.
    ('A="unterminated\nB=2\n', [("A", '"unterminated'), ("B", "2")]),
    ('A=a"b\nB=2\n', [("A", 'a"b'), ("B", "2")]),
    ("A=don't\nB=2\n", [("A", "don't"), ("B", "2")]),
    ('A="legacy quoted"\nB=a"b\n', [("A", "legacy quoted"), ("B", 'a"b')]),
    ("A=va\\ lue\n", [("A", "va lue")]),
    ("A=one\\\ntwo\n", [("A", "onetwo")]),         # backslash-newline continues
    ("A=trailing   \n", [("A", "trailing")]),
    ('A="  kept  "\n', [("A", "  kept  ")]),
    ("A=\n", [("A", "")]),
    ("A=x\r\nB=y\r\n", [("A", "x"), ("B", "y")]),
    ('A="a"b"c"\n', [("A", "abc")]),               # adjacent segments join
]

MARKER = re.compile(r"^\s*@@include:(?P<path>[^@]+)@@\s*$", re.M)


def load_lib(directory):
    """Exec modules/src/env-file.py, which is a spliced-in fragment."""
    namespace = {"re": re, "os": os, "tempfile": tempfile}
    with open(os.path.join(directory, "env-file.py")) as fh:
        exec(fh.read(), namespace)  # noqa: S102 — the point of the check
    return namespace


def build_cli(directory, target):
    """Resolve env-cli.py's include the way bin/assemble-module.py does."""
    with open(os.path.join(directory, "env-cli.py")) as fh:
        text = fh.read()

    def splice(match):
        with open(os.path.join(directory, match.group("path"))) as fh:
            return fh.read().rstrip("\n")

    with open(target, "w") as fh:
        fh.write(MARKER.sub(splice, text))
    return target


def cli(script, *args):
    return subprocess.run([sys.executable, script, *args],
                          capture_output=True, check=True).stdout


def cli_pairs(script, path):
    """Read pairs back through the CLI's NUL framing (the shell contract)."""
    parts = cli(script, "read", path).decode().split("\0")[:-1]
    return list(zip(parts[0::2], parts[1::2]))


def main():
    directory = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    lib = load_lib(directory)
    tmpdir = tempfile.mkdtemp()
    env_path = os.path.join(tmpdir, "env")
    script = build_cli(directory, os.path.join(tmpdir, "env-cli.py"))
    failures = []

    def check(name, want, got):
        if want != got:
            failures.append(f"{name}\n  want {want!r}\n  got  {got!r}")

    for i, value in enumerate(VALUES):
        # Surrounded by ordinary pairs: a value that runs off its own record
        # eats them, which is exactly what used to happen.
        pairs = [("BEFORE", "before"), (f"K{i}", value), ("AFTER", "after")]
        encoded = lib["encode_value"](value)
        lib["write_pairs"](env_path, pairs)
        check(f"round trip, value {i} ({encoded!r})", pairs, lib["read_pairs"](env_path))
        check(f"CLI read, value {i} ({encoded!r})", pairs, cli_pairs(script, env_path))
        # Rewriting one key must leave every other value intact — the save
        # that used to make the truncation permanent.
        lib["set_key"](env_path, "AFTER", "rewritten")
        check(f"survives a later save, value {i}", value, dict(lib["read_pairs"](env_path))[f"K{i}"])
        check(f"CLI ls, value {i}", ["AFTER", "BEFORE", f"K{i}"],
              cli(script, "ls", env_path).decode().split())

    for text, want in HANDWRITTEN:
        with open(env_path, "w") as fh:
            fh.write(text)
        check(f"hand-written {text!r}", want, lib["parse_env"](text))
        check(f"CLI read, hand-written {text!r}", want, cli_pairs(script, env_path))

    # The CLI's own edits go through the same writer the page uses.
    os.unlink(env_path)
    cli(script, "set", env_path, "CERT", PEM)
    cli(script, "set", env_path, "TOKEN", "plain")
    cli(script, "rm", env_path, "TOKEN")
    check("CLI set/rm", [("CERT", PEM)], lib["read_pairs"](env_path))
    check("CLI writes 0600", 0o600, os.stat(env_path).st_mode & 0o777)

    if failures:
        print("env-file format divergence:\n\n" + "\n\n".join(failures), file=sys.stderr)
        return 1
    print(f"env-file format: {len(VALUES)} values + {len(HANDWRITTEN)} hand-written "
          "files round-trip through the library and the CLI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
