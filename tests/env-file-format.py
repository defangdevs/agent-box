#!/usr/bin/env python3
"""Pin the two implementations of the env-file format to each other (issue 212).

The per-user secrets file (~/.config/agent-box/env) is written and read by
the settings daemon in Python (parse_env / encode_value in
modules/src/settings-daemon.py) and read — and, through the session CLI,
rewritten — by the shell in modules/src/env-file.sh. The file is systemd
env-file syntax, so a value may be quoted and span lines; a divergence
between the two readers does not raise anything, it silently truncates a
stored secret at its first newline.

This check writes each value with the Python writer, then asserts that both
readers give the value back byte-exact, and that both writers agree on the
encoding. It also parses a set of hand-written files (the shapes a human or
an older agent-box wrote) that the readers must not choke on.

Run: python3 tests/env-file-format.py DIR
where DIR holds settings-daemon.py and env-file.sh. Prints a summary and
exits non-zero on the first divergence.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

HEADER = (
    "# Managed by agent-box settings page. KEY=value, systemd env-file\n"
    "# syntax: a quoted value may span lines (PEM keys, certificates).\n"
    "# Do not add secrets by hand here unless you know what you are doing.\n"
)

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
    # Unbalanced quote: a pre-212 file could hold one inside a value, so
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


def load_daemon(directory):
    """Import modules/src/settings-daemon.py without starting a server."""
    os.environ.setdefault("AGENT_BOX_SETTINGS_ENV_FILE", "/dev/null")
    path = os.path.join(directory, "settings-daemon.py")
    spec = importlib.util.spec_from_file_location("settings_daemon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shell(directory, script, *args):
    return subprocess.run(
        ["bash", "-c", f". {directory}/env-file.sh\n{script}", "bash", *args],
        capture_output=True, check=True,
    ).stdout.decode()


def shell_parse(directory, path):
    """Parse `path` with env-file.sh; NUL-delimited so values keep newlines."""
    out = shell(
        directory,
        f"emit() {{ printf '%s\\0%s\\0' \"$1\" \"$2\"; }}\nenv_parse {path!r} emit",
    )
    parts = out.split("\0")[:-1]
    return list(zip(parts[0::2], parts[1::2]))


def shell_quote(directory, value):
    return shell(directory, 'env_quote "$1"\nprintf %s "$ENV_QUOTED"', value)


def main():
    directory = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    daemon = load_daemon(directory)
    failures = []
    tmpdir = tempfile.mkdtemp()
    env_path = os.path.join(tmpdir, "env")

    def check(name, want, got):
        if want != got:
            failures.append(f"{name}\n  want {want!r}\n  got  {got!r}")

    for i, value in enumerate(VALUES):
        # Surrounded by ordinary pairs: a value that runs off its own record
        # eats them, which is exactly what used to happen.
        pairs = [("BEFORE", "before"), (f"K{i}", value), ("AFTER", "after")]
        encoded = daemon.encode_value(value)
        text = HEADER + "".join(f"{k}={daemon.encode_value(v)}\n" for k, v in pairs)
        with open(env_path, "w") as fh:
            fh.write(text)
        check(f"python reader, value {i} ({encoded!r})", pairs, daemon.parse_env(text))
        check(f"shell reader, value {i} ({encoded!r})", pairs, shell_parse(directory, env_path))
        check(f"writers disagree, value {i}", encoded, shell_quote(directory, value))

    for text, want in HANDWRITTEN:
        with open(env_path, "w") as fh:
            fh.write(text)
        check(f"python reader, hand-written {text!r}", want, daemon.parse_env(text))
        check(f"shell reader, hand-written {text!r}", want, shell_parse(directory, env_path))

    if failures:
        print("env-file format divergence:\n\n" + "\n\n".join(failures), file=sys.stderr)
        return 1
    print(f"env-file format: {len(VALUES)} values + {len(HANDWRITTEN)} hand-written "
          "files agree across the Python and shell implementations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
