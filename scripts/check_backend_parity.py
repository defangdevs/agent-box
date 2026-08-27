#!/usr/bin/env python3
"""Guard the two backends against silent divergence.

agent-box renders the same box two ways. `modules/agent-box.nix.in` is the
NixOS module; `bin/agentbox` is the native renderer for a distro host. They
ship the SAME payloads out of `modules/src/` — one supervisor, one settings
daemon, one session CLI — and each is responsible for handing those payloads
their configuration. Nothing made them agree about what that configuration is.

Issue #392 is what that costs: the settings daemon hides its whole Connections
section when it is passed no AGENT_BOX_CONNECT_BINS, the module set it, the
native renderer never did, and every Lightsail box shipped without guided
sign-in until a user asked where the tab had gone. The payload was identical
on both boxes. Only the configuration differed, and no test looks there:

  - tests/connect.nix and its siblings set these variables BY HAND on the
    unit, so they prove the payload works GIVEN a value — never that a
    renderer supplies one.
  - tests/native/expected/ is regenerated with `--update`, so it ratifies
    whatever the renderer currently emits. Absence and intent look identical.
  - the runtime-profile-payloads-match-modules-src flake check compares
    payload BYTES: right idea, one level too low.

So this check reads the contract instead, out of three sets already in the
repo (no VM, no build):

  1. every AGENT_BOX_* name a shared payload READS,
  2. every one the MODULE supplies, from tests/golden/,
  3. every one the NATIVE renderer supplies, from tests/native/expected/.

A name supplied by exactly one backend is a divergence. It fails the check
unless it is listed in KNOWN_GAPS (a real gap someone owns, with its issue) or
BY_DESIGN (a difference that is correct, with the reason). Both tables are
checked for staleness: an entry that no longer describes a divergence fails
too, so a fixed gap cannot sit here pretending to still be one.

The same comparison runs over the rendered UNIT sets, which is where a
difference like "the module jails brute-force logins and the native box does
not" shows up — a divergence with no environment variable to its name.

Run it directly (`python3 scripts/check_backend_parity.py`), or through the
`backend-parity` flake check.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARED = REPO / "modules" / "src"
GOLDEN = REPO / "tests" / "golden"          # what the module renders
NATIVE = REPO / "tests" / "native" / "expected"  # what bin/agentbox renders

VAR = re.compile(r"AGENT_BOX_[A-Z0-9_]+")
# A backend SUPPLIES a variable when a rendered artifact assigns it: a unit's
# Environment=, a generated env file's KEY=value, or a generated wrapper's
# export. All three end up as "<NAME>=" at a word boundary.
SUPPLIED = re.compile(r"(?:^|[\s\"'])(AGENT_BOX_[A-Z0-9_]+)=", re.MULTILINE)
# Names one payload hands another at runtime. Nothing on the host supplies
# these, so they are not part of either renderer's contract. Listed rather
# than detected, because "a payload assigns it" is also true of a name a
# payload merely defaults (AGENT_BOX_HOOK_SESSION_ARGS), and dropping those
# would hide exactly the divergences this check exists to find.
INTERNAL = {
    "AGENT_BOX_SESSION_ID": "supervisor tells the session's own hook which "
                            "session it is in",
    "AGENT_BOX_PROFILE": "the session CLI hands the chosen profile to the "
                         "supervisor it spawns",
    "AGENT_BOX_REGISTRY_LOCK_FD": "registry.sh passes its open lock fd down",
    "AGENT_BOX_CODEX_UTS": "codex-remote-control.sh re-execs itself",
}

# Divergences that are CORRECT. Each needs a reason a reader can check.
BY_DESIGN = {
    "AGENT_BOX_USERS": "spot monitor: EC2-only, and the native path's own "
                       "deployment (Lightsail) has no Spot to monitor",
    "AGENT_BOX_GRACE": "spot monitor, see AGENT_BOX_USERS",
    "AGENT_BOX_MSG": "spot monitor, see AGENT_BOX_USERS",
    "AGENT_BOX_POLL": "spot monitor, see AGENT_BOX_USERS",
}

# Divergences that are BUGS, each owned by an issue. This table must only ever
# shrink: fixing a gap means deleting its line, and the staleness check below
# makes that mandatory rather than optional.
KNOWN_GAPS = {
    "AGENT_BOX_ENVSTORE_BIN": "#394: `agent-box-session env` fails on a "
                              "native box; the profile ships no envstore CLI",
    "AGENT_BOX_PROFILE_BIN": "#394: agent profiles are unavailable natively",
    "AGENT_BOX_GUIDE_TARGET": "#394: native boxes never symlink the platform "
                              "guide into ~/.claude/CLAUDE.md",
    "AGENT_BOX_CLAUDE_SETTINGS": "#394: claude starts without the settings "
                                 "file, so its SessionStart hook never fires",
    "AGENT_BOX_CODEX_FULL_ACCESS": "#394: codex takes the non-full-access "
                                   "branch on a native box",
    "AGENT_BOX_HOOK_SPAWN_CMD": "#394: standing watches cannot spawn a "
                                "session natively",
    "AGENT_BOX_HOOK_SESSION_ARGS": "#394: see AGENT_BOX_HOOK_SPAWN_CMD",
    "AGENT_BOX_WEBHOOK_PYTHON": "#394: see AGENT_BOX_HOOK_SPAWN_CMD",
    "AGENT_BOX_WEBHOOK_PINNED_SCRIPT": "#394: see AGENT_BOX_HOOK_SPAWN_CMD",
    "AGENT_BOX_WEB_USERS": "#394: the settings daemon's multi-user branches "
                           "never engage natively",
}

# Units, by family (instance and template spellings normalized away).
UNITS_BY_DESIGN = {
    "agent-box-spot-monitor.service":
        "EC2-only, see AGENT_BOX_USERS above",
}
UNITS_KNOWN_GAPS = {
    "fail2ban.service": "#394: the module jails Caddy's 401s; a native box "
                        "has no brute-force backstop on the terminal",
}


def read_files(root):
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, path.read_text(errors="ignore")


def names(root, pattern):
    found = set()
    for _, text in read_files(root):
        for match in pattern.finditer(text):
            found.update(g for g in match.groups() if g)
    return found


def read_by_payloads():
    """Every AGENT_BOX_* name a shared payload mentions.

    The payloads under modules/src/ are the shared half of the box — both
    backends install these bytes — so what they read IS the contract. A name
    no payload mentions is not one: a stack parameter, or a fixture's own
    test data, which neither backend owes the other.
    """
    found = set()
    for _, text in read_files(SHARED):
        found.update(m.group(0) for m in VAR.finditer(text))
    return found


def unit_families(paths):
    """Unit names with the instance stripped: agent-box@agent.service and
    agent-box@.service are the same unit, spelled differently by the two
    backends. Drop-in directories name their unit too.

    Services only. The golden fixture snapshots systemd's rendered services
    and targets but no .socket units, though the module installs two — so
    comparing sockets here would report a difference the fixture invented.
    The services those sockets activate ARE compared, and they are the units
    that carry configuration.
    """
    out = set()
    for path in paths:
        name = path.name
        if name.endswith(".d"):
            name = name[:-2]
        if not name.endswith(".service"):
            continue
        out.add(re.sub(r"@[^.]*\.", "@.", name))
    return out


def report(kind, only_module, only_native, by_design, known):
    """Print one section; return (violations, stale entries)."""
    violations, stale = [], []
    for name in sorted(only_module | only_native):
        where = "module only" if name in only_module else "native only"
        if name in by_design:
            print(f"  ok (by design)  {name:34} {where} — {by_design[name]}")
        elif name in known:
            print(f"  known gap       {name:34} {where} — {known[name]}")
        else:
            violations.append((name, where))
    for name in sorted(set(by_design) | set(known)):
        if name not in only_module and name not in only_native:
            stale.append(name)
    if violations:
        print(f"\nFAIL: {len(violations)} undeclared {kind} divergence(s):",
              file=sys.stderr)
        for name, where in violations:
            print(f"       {name} — supplied {where}.\n"
                  f"       Supply it from both backends, or declare it in "
                  f"scripts/check_backend_parity.py with a reason.",
                  file=sys.stderr)
    if stale:
        print(f"\nFAIL: {len(stale)} stale {kind} entr(y/ies) — no longer a "
              f"divergence:", file=sys.stderr)
        for name in stale:
            print(f"       {name} — delete its line; the tables record "
                  f"today's divergences, not yesterday's.", file=sys.stderr)
    return violations, stale


def main():
    for root in (SHARED, GOLDEN, NATIVE):
        if not root.exists():
            print(f"FAIL: {root} is missing — this check would pass "
                  f"vacuously.", file=sys.stderr)
            return 1

    all_read = read_by_payloads()
    stale_internal = sorted(set(INTERNAL) - all_read)
    if stale_internal:
        print("FAIL: INTERNAL names no payload mentions any more: "
              + ", ".join(stale_internal), file=sys.stderr)
        return 1
    module = names(GOLDEN, SUPPLIED)
    native = names(NATIVE, SUPPLIED)
    contract = all_read - set(INTERNAL)

    print(f"payload variables in the contract: {len(contract)} "
          f"({len(INTERNAL)} internal to the payloads, not compared)")
    only_module = (module - native) & contract
    only_native = (native - module) & contract
    v1, s1 = report("variable", only_module, only_native,
                    BY_DESIGN, KNOWN_GAPS)

    print("\nrendered units:")
    mod_units = unit_families((GOLDEN / "web" / "units").iterdir())
    nat_units = unit_families(
        (NATIVE / "etc" / "systemd" / "system").iterdir())
    v2, s2 = report("unit", mod_units - nat_units, nat_units - mod_units,
                    UNITS_BY_DESIGN, UNITS_KNOWN_GAPS)

    if v1 or v2 or s1 or s2:
        return 1
    print("\nOK: every divergence between the two backends is declared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
