#!/usr/bin/env python3
"""One spec, two witnesses (issue #451).

`scripts/check_backend_parity.py` guards the CONFIGURATION each backend hands
the shared payloads: which unit gets which AGENT_BOX_* variable. It cannot see
anything with no variable name in it — a tmpfiles rule, a sudoers command, the
seed JSON two hand-written producers emit. Those are #356's actual bugs.

The reason no check compared them is upstream of any check: the two fixtures
were rendered from two HAND-MIRRORED configurations that had drifted into
describing different boxes. tests/golden declared no sessions; tests/native
declared two per user and a different sudoAllowlist. Comparing their output
would have reported dozens of differences that mean nothing.

So this check does two things, in order:

1. **One spec.** tests/native/config.json must equal the config that
   tests/spec.nix evaluates out of the golden web configuration's own options
   (`nix run .#update-native-config` writes it). After this, both fixtures
   describe the same box, and a module option the native schema cannot express
   is a red CI job.

2. **Two witnesses.** For that one box, compare what the two renderers
   produced for the artifacts the parity check is blind to — the seed JSON,
   the tmpfiles rules and the sudoers grants — modulo the substrate paths that
   are SUPPOSED to differ (`/run/current-system/sw/bin` vs `/usr/bin`, a store
   path vs `/etc/agent-box/bin`).

Divergences that are correct live in the tables below, each with a reason a
reader can check, and each checked for staleness: an entry that no longer
describes a divergence fails, so a fix must delete its line.

Usage:
    python3 scripts/check_one_spec.py [--spec GENERATED.json]

Without --spec, step 1 is skipped with a notice (it needs Nix to evaluate the
module); the flake check `one-spec-both-backends` always passes it.
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden" / "web"
NATIVE = REPO / "tests" / "native" / "expected"
CONFIG = REPO / "tests" / "native" / "config.json"
CONFIG_YAML = REPO / "tests" / "native" / "config.yaml"

# Substrate spellings of the same thing. Applied to both sides before any
# comparison, so a rule that differs ONLY in where the binary lives compares
# equal — and one that differs in what it grants does not.
SUBSTRATE = [
    # systemctl: NixOS's system profile vs a distro's /usr/bin.
    (re.compile(r"/run/current-system/sw/bin/systemctl|/usr/bin/systemctl"),
     "systemctl"),
    # The per-user password helper: a store path (hash already normalized to
    # e's by the golden snapshot) vs the profile copy `agentbox apply` links.
    (re.compile(r"(/nix/store/e+-agent-box-password-(\w+)/bin/|"
                r"/etc/agent-box/bin/)agent-box-password-"),
     "agent-box-password-"),
    # agent-box-candidate, same story: the module grants its store script
    # (writeShellScript, so a file and not a dir with bin/), native grants
    # the copy `agentbox apply` renders into /etc. Both are the SAME grant
    # of the same one-argument wrapper — a substrate spelling, not a
    # difference in what the box allows.
    (re.compile(r"/nix/store/e+-agent-box-candidate|"
                r"/etc/agent-box/bin/agent-box-candidate"),
     "agent-box-candidate"),
]

# Divergences that are CORRECT. Each needs a reason a reader can check.
#
# `d /home/@USER@/.config 0755 …` used to sit here, reasoned as native-only.
# It never was: #370 gave the module the same rule a week before this check
# was written, and the golden fixture could not show it because the snapshot
# recovered the module's rules by grepping the whole system's for the
# substring "agent-box" — which a path named after the USER does not contain.
# The exception described the blind spot, not the box. The manifest now asks
# the module for its own inventory (flake.nix -> internal.tmpfilesRules), so
# both sides render both rules and there is nothing to declare.
TMPFILES_BY_DESIGN = {}

SUDOERS_BY_DESIGN = {}

# Divergences that are BUGS, each owned by an issue. Only ever shrinks.
TMPFILES_KNOWN_GAPS = {}
SUDOERS_KNOWN_GAPS = {}


# What this run could not check. A skip that prints and returns 0 reads
# exactly like a pass — the failure mode this file has now hit twice (a
# silently-skipped test, then a check that could not fail at all) — so
# anything skipped is named here and the closing line refuses to say "OK".
SKIPPED = []


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def normalize(text):
    for pattern, replacement in SUBSTRATE:
        text = pattern.sub(replacement, text)
    return text


def users():
    """Every user the one spec declares."""
    return sorted(json.loads(CONFIG.read_text())["users"])


# --------------------------------------------------------------------------
# 1. One spec
# --------------------------------------------------------------------------

def check_spec(generated):
    if generated is None:
        # Without Nix there is no module evaluation to compare against, but
        # the two DIALECTS of the committed config can still be held to each
        # other — that half needs no module, and it is the half that failed
        # in CI when this file was written.
        SKIPPED.append("the module evaluation (no --spec)")
        print("spec: SKIPPED (no --spec; the flake check does this half)")
        return check_spec_yaml(json.loads(CONFIG.read_text()), required=False)
    want = json.loads(Path(generated).read_text())
    got = json.loads(CONFIG.read_text())
    if want == got:
        print("spec: tests/native/config.json matches the module's options")
        # required: --spec means the flake check, whose derivation carries
        # pyyaml. If it cannot import there, the dependency was dropped and
        # the check is broken — not partial.
        return check_spec_yaml(want, required=True)
    lines = []
    for key in sorted(set(want) | set(got)):
        if want.get(key) != got.get(key):
            lines.append(f"  {key}:\n"
                         f"    module:    {json.dumps(want.get(key))}\n"
                         f"    committed: {json.dumps(got.get(key))}")
    return fail(
        "tests/native/config.json is not what the module's options evaluate "
        "to. The two fixtures would describe different boxes, and every "
        "comparison below would be meaningless.\n"
        + "\n".join(lines)
        + "\n\nRegenerate: nix run .#update-native-config"
          "\nThen re-render the native fixture: "
          "python3 tests/test_agentbox.py --update")



def check_spec_yaml(want, required):
    """The YAML dialect is the same box, not a third mirror.

    tests/test_agentbox.py renders both dialects and compares the trees, so a
    drifted config.yaml fails there too — as a 269k-character assertion diff
    (that is how this file's own drift surfaced in CI). Comparing the parsed
    config here says which KEY moved instead.
    """
    try:
        import yaml
    except ImportError:
        if required:
            return fail(
                "PyYAML is not importable, so tests/native/config.yaml was "
                "not checked — and this run was asked to check it. The "
                "one-spec-both-backends derivation carries pyyaml, so this "
                "means the dependency was dropped: the check is broken, not "
                "partial.")
        # Standalone, on a box without pyyaml (this is the common case —
        # hard-failing here would make the no-Nix path unusable for the
        # comparisons that DO run). Named in the closing line instead.
        SKIPPED.append("config.yaml (no PyYAML)")
        print("spec: config.yaml SKIPPED (no PyYAML)")
        return 0
    got = yaml.safe_load(CONFIG_YAML.read_text())
    if want == got:
        print("spec: config.yaml describes the same box")
        return 0
    keys = sorted(k for k in set(want) | set(got) if want.get(k) != got.get(k))
    return fail("tests/native/config.yaml has drifted from config.json: "
                + ", ".join(keys)
                + "\n  Regenerate: nix run .#update-native-config")

# --------------------------------------------------------------------------
# 2. Two witnesses
# --------------------------------------------------------------------------

def check_seed():
    """The seed JSON, whose two producers are #356's twin-schema bug.

    Compared as DATA, not bytes: the module's builtins.toJSON and the native
    renderer's json.dumps disagree about whitespace and key order, and neither
    is what the supervisor reads it with (jq).
    """
    bad = 0
    for user in users():
        left = GOLDEN / "payloads" / f"agent-box-{user}-sessions.json"
        right = NATIVE / "etc/agent-box/seed" / f"{user}-sessions.json"
        if not left.exists() or not right.exists():
            bad += fail(f"seed: {user} has a seed on only one backend "
                        f"(module={left.exists()}, native={right.exists()})")
            continue
        want, got = json.loads(left.read_text()), json.loads(right.read_text())
        if want != got:
            bad += fail(
                f"seed: {user}'s seeded sessions differ between backends.\n"
                f"  module: {json.dumps(want, sort_keys=True)}\n"
                f"  native: {json.dumps(got, sort_keys=True)}\n"
                "  The supervisor reads this file with jq and expects every "
                "runtime field to be present — a shape difference here is a "
                "session that never starts (#356).")
    if not bad:
        print(f"seed: {len(users())} user(s), byte-identical session state")
    return bad


def _report(kind, only_left, only_right, by_design, known_gaps):
    """Print one artifact's divergences; return (violations, stale)."""
    violations, seen = 0, set()
    for side, items in (("module only", only_left), ("native only", only_right)):
        for item in sorted(items):
            if item in by_design:
                seen.add(item)
                print(f"  ok (by design)  {kind}: {item}\n"
                      f"                  {side} — {by_design[item]}")
            elif item in known_gaps:
                seen.add(item)
                print(f"  known gap       {kind}: {item}\n"
                      f"                  {side} — {known_gaps[item]}")
            else:
                violations += fail(f"{kind}: {side} — {item}")
    stale = sorted((set(by_design) | set(known_gaps)) - seen)
    for item in stale:
        violations += fail(
            f"{kind}: stale exception — {item!r} no longer diverges. "
            "Delete its line.")
    return violations


def check_tmpfiles():
    """The directory set, #356's other twin."""
    left = {normalize(x) for x in
            (GOLDEN / "tmpfiles.d" / "agent-box.conf").read_text().splitlines()
            if x.strip()}
    right = set()
    for path in sorted((NATIVE / "etc/tmpfiles.d").glob("*.conf")):
        right |= {normalize(x) for x in path.read_text().splitlines()
                  if x.strip() and not x.startswith("#")}
    # Per-user rules collapse to one entry so an exception is written once
    # rather than once per user of the test config.
    def fold(rules):
        out = set()
        for rule in rules:
            for user in users():
                rule = re.sub(rf"(?<![\w-]){re.escape(user)}(?![\w-])",
                              "@USER@", rule)
            out.add(rule)
        return out
    left, right = fold(left), fold(right)
    bad = _report("tmpfiles", left - right, right - left,
                  TMPFILES_BY_DESIGN, TMPFILES_KNOWN_GAPS)
    if not bad:
        print(f"tmpfiles: {len(left & right)} rule(s) agree, "
              f"{len(left ^ right)} declared")
    return bad


def check_sudoers():
    """What each backend lets an agent user do as root.

    Compared per user as a SET of commands: the two renderers order them
    differently and neither order means anything to sudo.
    """
    def grants(text):
        out = {}
        for line in text.splitlines():
            match = re.match(r"(\w[\w-]*)\s+ALL=\([^)]*\)\s+(.*)", line.strip())
            if not match:
                continue
            user, rest = match.groups()
            cmds = {normalize(c.strip()).removeprefix("NOPASSWD: ").strip()
                    for c in rest.split(",")}
            out.setdefault(user, set()).update(c for c in cmds if c)
        return out

    left = grants((GOLDEN / "etc" / "sudoers").read_text())
    right = grants((NATIVE / "etc/sudoers.d/agent-box").read_text())
    bad = 0
    # Accumulated over ALL users, then reported once: an exception is about a
    # COMMAND, not about who holds it, and reporting per user would call every
    # other user's pass stale for an entry that legitimately covers one.
    only_left, only_right = set(), set()
    for user in users():
        if user not in left or user not in right:
            bad += fail(f"sudoers: {user} has grants on only one backend")
            continue
        # The helper grant names the user it belongs to; fold it so the
        # comparison is about WHAT is granted, not to whom.
        def fold(cmds, user=user):
            return {c.replace(f"agent-box-password-{user}",
                              "agent-box-password-@USER@") for c in cmds}
        mine, theirs = fold(left[user]), fold(right[user])
        only_left |= mine - theirs
        only_right |= theirs - mine
    bad += _report("sudoers", only_left, only_right,
                   SUDOERS_BY_DESIGN, SUDOERS_KNOWN_GAPS)
    if not bad:
        print(f"sudoers: {len(users())} user(s) compared")
    return bad


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", help="module-evaluated config JSON")
    args = parser.parse_args()

    for path in (GOLDEN, NATIVE, CONFIG, CONFIG_YAML):
        if not path.exists():
            return fail(f"missing fixture {path}")

    bad = check_spec(args.spec)
    if bad:
        # Every comparison below assumes one box. Running them against two
        # different ones prints noise, not findings.
        return bad
    bad += check_seed()
    bad += check_tmpfiles()
    bad += check_sudoers()
    if bad:
        print(f"\n{bad} divergence(s) between the two backends for ONE spec.",
              file=sys.stderr)
        return 1
    if SKIPPED:
        print("\nPARTIAL: the comparisons that ran agree, but this run did "
              "not check " + ", ".join(SKIPPED) + ".")
        print("Not a pass — for that, run "
              "`nix build .#checks.<system>.one-spec-both-backends`.")
        return 0
    print("\nOK: one spec, and both backends render it the same.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
