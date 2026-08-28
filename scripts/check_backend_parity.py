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

Then it runs a THIRD time, keyed by (unit, variable), over the units both
backends render. Issue #426 is what that costs: the settings daemon hides its
whole webhook panel without AGENT_BOX_WEBHOOK_SCRIPT, the module set it on the
settings unit, the native renderer set it only in the `agent-box-webhook` CLI
wrapper — a different unit — and every native box shipped with the panel gone.
Box-wide, both backends supplied the name, so the comparison above saw nothing
at all. Supplying a variable somewhere is not supplying it to the payload that
reads it.

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
MODULE_UNITS = GOLDEN / "web" / "units"
DUPLICATES = GOLDEN / "DUPLICATES"
NATIVE_UNITS = NATIVE / "etc" / "systemd" / "system"
# The %i template units under here (agent-box-settings@.service and
# friends) are backend-neutral: both renderers install this exact file,
# byte-for-byte (see modules/agent-box.nix.in's agentBoxUnitsPackage). The
# module ships it via systemd.packages, which golden-snapshot.py never
# captures — it only records config.systemd.units, i.e. the Nix-eval
# drop-in each instance overlays on top. So GOLDEN alone under-reports what
# the module supplies for these; treat this directory as read by both.
SHARED_UNITS = SHARED / "units"

VAR = re.compile(r"AGENT_BOX_[A-Z0-9_]+")
# A backend SUPPLIES a variable when a rendered artifact assigns it: a unit's
# Environment=, a generated env file's KEY=value, or a generated wrapper's
# export. All three end up as "<NAME>=" at a word boundary — except systemd's
# own unquoted `Environment=AGENT_BOX_FOO=bar`, where the boundary is the
# literal "Environment=" prefix rather than whitespace or a quote.
SUPPLIED = re.compile(
    r"(?:^|[\s\"']|(?<=Environment=))(AGENT_BOX_[A-Z0-9_]+)=", re.MULTILINE)
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
    "AGENT_BOX_NIX_BIN": "lazy harnesses (#416): the module pins nix as a "
                         "store path because it HAS one. A native box does "
                         "not — resolving `nix` at apply time would bake "
                         "the BUILDING host's store path into a generated "
                         "file and break on the next nix upgrade — so "
                         "lib/agents.sh resolves it at USE from PATH and "
                         "the two standard install layouts. Both backends "
                         "reach the same binary; only one can name it "
                         "ahead of time",
}

# Divergences that are BUGS, each owned by an issue. This table must only ever
# shrink: fixing a gap means deleting its line, and the staleness check below
# makes that mandatory rather than optional.
KNOWN_GAPS = {
}

# Units, by family (instance and template spellings normalized away).
UNITS_BY_DESIGN = {
    "agent-box-spot-monitor.service":
        "EC2-only, see AGENT_BOX_USERS above",
    "agent-box-zram.service":
        "native only, and correct: on NixOS zram comes from the zramSwap "
        "option, which a generator turns into systemd-zram-setup@zram0 at "
        "boot — a unit no fixture records because it does not exist until "
        "the generator runs. Both backends give the box compressed swap; "
        "only one of them has a unit file to compare",
    "agent-box-nix-gc.service":
        "native only, and correct: on NixOS the DEPLOYMENT owns store "
        "housekeeping (aws/template.yaml sets nix.gc and min-free), a layer "
        "a native box does not have, so its renderer owns it instead. If the "
        "module ever grows nix.gc of its own, this line goes away",
}
UNITS_KNOWN_GAPS = {
    "agent-box-defang-cli.service": "#394: the module installs the Defang "
                                    "CLI in the background (#373); the "
                                    "native runtime profile has no defang "
                                    "at all, which is also why its settings "
                                    "page offers no defang sign-in card",
}

# Same job, different unit name. The native jail is agent-box-fail2ban so a
# distro fail2ban installed later keeps its own service and its own config;
# that is a deliberate naming choice, not a second implementation, so the
# comparison folds one onto the other instead of reporting both sides as
# one-sided divergences. fail2ban.service is no longer in UNITS_KNOWN_GAPS
# above for the same reason: the alias below makes it not a gap at all.
UNIT_ALIASES = {
    "fail2ban.service": "agent-box-fail2ban.service",
    # Same daemon, same thresholds; prefixed here because Ubuntu may also
    # carry a distro earlyoom, and this box's copy comes from the runtime
    # profile with the box's own arguments.
    "earlyoom.service": "agent-box-earlyoom.service",
}

# Per-(unit, variable) divergences, keyed "<unit family> <VARIABLE>". Only
# units BOTH backends render are compared: a unit one backend does not have
# is the unit comparison's business, and reporting its every variable here
# again would bury the finding this table exists for.
#
# A variable already declared in BY_DESIGN or KNOWN_GAPS above needs no entry:
# those names are one-sided box-wide, so they are one-sided in whatever unit
# carries them, and the reason is written once where it belongs.
UNIT_VARS_BY_DESIGN = {
}
UNIT_VARS_KNOWN_GAPS = {
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


def module_unit_names():
    """The web config's REAL unit set.

    tests/golden deduplicates a file whose bytes match one already in the
    snapshot, recording it as a line in DUPLICATES instead — so the web
    config's earlyoom.service lives under vm/units/ and reading web/units/
    alone silently under-reports what the module ships. That is how this
    check called earlyoom "absent from both backends" when it is absent
    from exactly one.
    """
    names = {p.name for p in MODULE_UNITS.iterdir()}
    for line in DUPLICATES.read_text().splitlines():
        if line.startswith("web/units/") and "->" in line:
            names.add(line.split("->")[0].strip().rsplit("/", 1)[-1])
    return names


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
        name = unit_family(path if isinstance(path, str) else path.name)
        if name is not None:
            out.add(name)
    return out


def unit_family(name):
    """One unit name, normalized — or None when it is not a service.

    agent-box@agent.service, agent-box@.service and the drop-in directory
    agent-box@agent.service.d are three spellings of one unit; the two
    backends do not agree on which they use, and nothing here should care.
    """
    if name.endswith(".d"):
        name = name[:-2]
    if not name.endswith(".service"):
        return None
    return re.sub(r"@[^.]*\.", "@.", name)


def env_file_units():
    """Which unit reads each /etc/agent-box/units/*.env file.

    Both backends write those files and both install the SAME %i template
    units, so the templates' own EnvironmentFile= lines are the mapping.
    Reading it out of them means a unit that grows an env file tomorrow is
    keyed correctly with no table here to update — and a file no unit reads
    is reported rather than silently attributed to the wrong unit.
    """
    found = []
    for path in sorted(SHARED_UNITS.iterdir()):
        unit = unit_family(path.name)
        if unit is None:
            continue
        for match in re.finditer(
                r"EnvironmentFile=-?/etc/agent-box/units/(\S+)",
                path.read_text()):
            name = match.group(1)
            # .local.env is the user's own override, which no renderer
            # writes; a path with no %i is not a per-instance env file.
            if name.endswith(".local.env") or "%i" not in name:
                continue
            found.append((len(name) - len("%i"), re.compile(
                "^" + "(?:.+)".join(
                    re.escape(part) for part in name.split("%i")) + "$"),
                unit))
    # Longest literal first: "%i.env" also matches
    # agent-box-settings-agent.env, and the specific unit is the answer.
    found.sort(key=lambda entry: entry[0], reverse=True)
    return [(pattern, unit) for _, pattern, unit in found]


ENV_FILE_UNITS = None   # built in main(), once SHARED_UNITS is known to exist


def unit_of(root, path):
    """The unit a rendered file configures, or None if it configures none.

    Three shapes, two backends: a unit file or drop-in under a systemd
    directory, and an env file under /etc/agent-box/units/.
    """
    parts = path.relative_to(root).parts
    if len(parts) >= 2 and parts[-2] == "units" and parts[-1].endswith(".env"):
        for pattern, unit in ENV_FILE_UNITS:
            if pattern.match(parts[-1]):
                return unit
        return None
    for anchor in ("system", "units"):
        if anchor in parts and parts.index(anchor) + 1 < len(parts):
            return unit_family(parts[parts.index(anchor) + 1])
    return None


def supplied_by_unit(root):
    """{unit: {variable, ...}} for one backend's rendered tree."""
    out = {}
    for path, text in read_files(root):
        unit = unit_of(root, path)
        if unit is None:
            continue
        found = {m.group(1) for m in SUPPLIED.finditer(text)}
        if found:
            out.setdefault(unit, set()).update(found)
    return out


def ambiguous(kind, by_design, known):
    """Names declared in BOTH exception tables — an unresolved contradiction:
    is the divergence correct, or a bug someone owns? Checked up front, like
    INTERNAL staleness below, rather than inside report(), whose by_design-
    first lookup would otherwise silently resolve the ambiguity for it."""
    dupes = sorted(set(by_design) & set(known))
    if dupes:
        print(f"FAIL: {len(dupes)} {kind} entr(y/ies) declared as both "
              f"by-design AND a known gap — pick one: "
              + ", ".join(dupes), file=sys.stderr)
    return dupes


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
    for root in (SHARED, GOLDEN, NATIVE, MODULE_UNITS, NATIVE_UNITS,
                 SHARED_UNITS, DUPLICATES):
        if not root.exists():
            print(f"FAIL: {root} is missing — this check would pass "
                  f"vacuously.", file=sys.stderr)
            return 1

    global ENV_FILE_UNITS
    ENV_FILE_UNITS = env_file_units()

    all_read = read_by_payloads()
    stale_internal = sorted(set(INTERNAL) - all_read)
    if stale_internal:
        print("FAIL: INTERNAL names no payload mentions any more: "
              + ", ".join(stale_internal), file=sys.stderr)
        return 1
    # Both, then decide: `or` would short-circuit, so a variable clash
    # would hide every unit clash until someone fixed the first one and
    # re-ran. One run, every contradiction.
    bad_vars = ambiguous("variable", BY_DESIGN, KNOWN_GAPS)
    bad_units = ambiguous("unit", UNITS_BY_DESIGN, UNITS_KNOWN_GAPS)
    bad_pairs = ambiguous("per-unit variable",
                          UNIT_VARS_BY_DESIGN, UNIT_VARS_KNOWN_GAPS)
    if bad_vars or bad_units or bad_pairs:
        return 1
    shared_units = names(SHARED_UNITS, SUPPLIED)
    module = names(GOLDEN, SUPPLIED) | shared_units
    native = names(NATIVE, SUPPLIED) | shared_units
    contract = all_read - set(INTERNAL)

    print(f"payload variables in the contract: {len(contract)} "
          f"({len(INTERNAL)} internal to the payloads, not compared)")
    only_module = (module - native) & contract
    only_native = (native - module) & contract
    v1, s1 = report("variable", only_module, only_native,
                    BY_DESIGN, KNOWN_GAPS)

    print("\nrendered units:")
    mod_units = {UNIT_ALIASES.get(u, u)
                 for u in unit_families(module_unit_names())}
    nat_units = unit_families(NATIVE_UNITS.iterdir())
    v2, s2 = report("unit", mod_units - nat_units, nat_units - mod_units,
                    UNITS_BY_DESIGN, UNITS_KNOWN_GAPS)

    # Per (unit, variable), over the units BOTH backends render. The
    # template units under modules/src/units/ are installed verbatim by
    # both, so what they set counts for each side — otherwise every
    # variable a template supplies would read as a divergence against the
    # backend whose fixture happens not to repeat it.
    shared_by_unit = {}
    for path, text in read_files(SHARED_UNITS):
        unit = unit_family(path.name)
        if unit is None:
            continue
        found = {m.group(1) for m in SUPPLIED.finditer(text)}
        if found:
            shared_by_unit.setdefault(unit, set()).update(found)
    mod_by_unit = {UNIT_ALIASES.get(u, u): v
                   for u, v in supplied_by_unit(GOLDEN).items()}
    nat_by_unit = supplied_by_unit(NATIVE)
    # Declared box-wide already: the reason lives in the tables above, and
    # a one-sided name is one-sided in whatever unit carries it.
    declared = set(BY_DESIGN) | set(KNOWN_GAPS)
    pair_module, pair_native = set(), set()
    for unit in sorted(mod_units & nat_units):
        shared = shared_by_unit.get(unit, set())
        supplies = (mod_by_unit.get(unit, set()) | shared,
                    nat_by_unit.get(unit, set()) | shared)
        for side, one_sided in zip((pair_module, pair_native),
                                   (supplies[0] - supplies[1],
                                    supplies[1] - supplies[0])):
            side.update(f"{unit} {name}"
                        for name in (one_sided & contract) - declared)

    print(f"\nper-unit variables, over the "
          f"{len(mod_units & nat_units)} unit(s) both backends render:")
    v3, s3 = report("per-unit variable", pair_module, pair_native,
                    UNIT_VARS_BY_DESIGN, UNIT_VARS_KNOWN_GAPS)

    if v1 or v2 or v3 or s1 or s2 or s3:
        return 1
    print("\nOK: every divergence between the two backends is declared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
