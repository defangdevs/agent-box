#!/usr/bin/env python3
"""Decide whether a commit range touched any of a workflow's build paths.

Why this exists (issue #632). CI's expensive jobs are path-filtered, and
until now the filter lived on the workflow TRIGGER (`on: pull_request:
paths:`). A trigger-level filter means the whole workflow never starts on a
PR that misses it -- and a workflow that never starts reports NO check run
at all. That is fine while nothing depends on the check, and fatal the
moment a branch ruleset requires it: a required check that is never
reported leaves the PR pending forever, so requiring green CI would have
blocked every docs-only change permanently.

So the filter moved DOWN a level. The workflow now always starts, a cheap
`changes` job runs this script, the expensive job is `if:`-guarded on its
answer, and a terminal `gate` job reports success/failure unconditionally.
That gate is the check a ruleset can require: it is reported on every pull
request and every push, and it says "skipped, correctly" rather than
saying nothing at all.

The pattern dialect is GitHub's own (`on.<event>.paths`), because these
pattern files ARE the lists that used to sit in those trigger blocks:

    *   zero or more characters, but never `/`
    **  zero or more characters, `/` included
    ?   exactly one character, but never `/`

Anything else is literal. Blank lines and `#` comments are ignored, which
is the reason the lists are plain text and not JSON: every entry in them
carries a comment saying which bug put it there, and those comments are
the most valuable part of the file.

Usage:

    changed_paths.py FILTER --base SHA --head SHA     # asks git
    changed_paths.py FILTER --files-from FILE         # or a given list
    changed_paths.py FILTER --files-from -            # ... on stdin

It prints `true` or `false` and exits 0 either way; a non-zero exit means
the question could not be answered. With no usable base (a force-push, a
brand-new branch, a manual dispatch) it prints `true`: the fail-safe
direction for a gate is to RUN the checks, never to skip them.
"""

import argparse
import re
import subprocess
import sys


def load_patterns(path):
    """The non-comment, non-blank lines of a filter file, in order."""
    patterns = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    if not patterns:
        raise SystemExit(f"{path}: no patterns -- an empty filter would "
                         "silently skip every build")
    return patterns


def to_regex(pattern):
    """GitHub's `paths` glob, as an anchored regex.

    `**` has to be consumed before `*`, or `**.nix` compiles to
    `[^/]*[^/]*\\.nix` and stops matching `modules/foo.nix` -- which is the
    whole point of that entry.
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(patterns, files):
    """Every (file, pattern) pair that fired, so the log can say why."""
    compiled = [(p, to_regex(p)) for p in patterns]
    hits = []
    for name in files:
        for pattern, rx in compiled:
            if rx.match(name):
                hits.append((name, pattern))
                break
    return hits


def changed_files(base, head):
    """The paths git reports between two revs, or None if it cannot."""
    if not base or not head:
        return None
    # An all-zero base is how GitHub spells "there was no previous commit"
    # in a push payload (a new branch, or the first push to one).
    if set(base) == {"0"}:
        return None
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        # A shallow clone, or a base the force-push took away. Either way
        # the range is not answerable here.
        sys.stderr.write(proc.stderr)
        return None
    return [line for line in proc.stdout.splitlines() if line]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("filter", help="a .paths file")
    ap.add_argument("--base", default="", help="the rev to diff from")
    ap.add_argument("--head", default="", help="the rev to diff to")
    ap.add_argument("--files-from", default=None,
                    help="read the changed paths from a file, or - for stdin")
    args = ap.parse_args(argv)

    patterns = load_patterns(args.filter)

    if args.files_from:
        source = sys.stdin if args.files_from == "-" else \
            open(args.files_from, encoding="utf-8")
        with source:
            files = [line.strip() for line in source if line.strip()]
    else:
        files = changed_files(args.base, args.head)
        if files is None:
            print("::notice::no usable commit range "
                  f"({args.base or '(none)'}...{args.head or '(none)'}) -- "
                  "running the checks", file=sys.stderr)
            print("true")
            return 0

    hits = matches(patterns, files)
    for name, pattern in hits[:20]:
        print(f"{name}  <-  {pattern}", file=sys.stderr)
    if len(hits) > 20:
        print(f"... and {len(hits) - 20} more", file=sys.stderr)
    print(f"{len(files)} changed path(s), {len(hits)} matched "
          f"{args.filter}", file=sys.stderr)
    print("true" if hits else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
