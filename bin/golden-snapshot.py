#!/usr/bin/env python3
"""Render the agent-box golden snapshot (issue #154, Phase 0).

Runs inside the `golden-snapshot` derivation (see flake.nix). Input is a JSON
manifest evaluated from the golden NixOS configurations:

    { "<config>": { "units":    { "<unit>": {"text", "wantedBy", "requiredBy"} },
                    "etc":      { "<relpath>": "<store path>" },
                    "tmpfiles": [ "<rule>", ... ] } }

For each config this writes, under <out>/<config>/:
  units/<unit>            the unit text, plus X-Golden-* comment trailers for
                          wantedBy/requiredBy (NixOS realizes those as .wants/
                          symlinks, so they are invisible in the text itself)
  etc/<relpath>           published /etc files (sudoers, guides, Caddyfile,
                          fail2ban locals)
  tmpfiles.d/agent-box.conf
  payloads/<name>[/...]   every /nix/store object whose NAME mentions
                          agent-box/agent-web that is reachable from the texts
                          above — followed recursively, so the supervisor pulls
                          in the codex wrapper, the webhook spawner pulls in
                          the session CLI, and so on. This is what makes the
                          snapshot catch a one-byte change in any generated
                          script, not just in the unit that points at it.

Every emitted byte has store hashes normalized to a fixed placeholder, so the
fixture is identical across build systems (x86_64 vs aarch64 store paths differ
only in hash for the same nixpkgs pin) and diffs stay readable. Content changes
in generated payloads are still caught: their bytes are in the snapshot
directly, not just behind a hash.

Identical content is stored ONCE (issue #312). The web config is the vm config
plus the web overlay, so most payloads render byte-for-byte the same in both,
and a two-line edit to one generated script used to land twice in the fixture
diff — the golden churn a reviewer saw was a multiple of the real change. Now
the first path in sorted order owns the bytes and every other path that renders
them is one line in <out>/DUPLICATES. Nothing is lost: the check still diffs the
whole rendered snapshot, and a copy that stops matching drops out of DUPLICATES
and reappears as its own file in the same diff.
"""
import hashlib
import json
import os
import re
import sys

DUPLICATES_HEADER = """\
# Paths whose rendered bytes are identical to an earlier path in this snapshot
# (issue #312). The owner holds the content; these are `<path> -> <owner>`, in
# sorted order, so one edit to a shared payload is one hunk in the fixture diff
# instead of one per config that renders it.
#
# The web config is the vm config plus the web overlay, and a user's units and
# payloads are rendered per user, so most sharing is vm<->web or agent<->robot.
# A line DISAPPEARING is a behavior change, not a cleanup: those two paths no
# longer render the same bytes, and the copy that diverged shows up as its own
# file in the same diff.
"""

STORE_HASH = re.compile(rb"/nix/store/[0-9a-df-np-sv-z]{32}-")
NORMALIZED = b"/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-"
# A store object whose NAME mentions agent-box/agent-web is one of ours
# (module-generated scripts, seeds, helper packages, the NixOS unit-script
# wrappers around them); everything else (tmux, jq, ...) is upstream and
# pinned well enough by its normalized name.
PAYLOAD = re.compile(
    rb"/nix/store/[0-9a-df-np-sv-z]{32}-"
    rb"([0-9A-Za-z+._?=-]*(?:agent-box|agent-web)[0-9A-Za-z+._?=-]*)"
)


def payload_roots(data: bytes):
    """Store roots (path, name) of agent-box payloads referenced in data."""
    for m in PAYLOAD.finditer(data):
        yield m.group(0).decode(), m.group(1).decode()


def snapshot_config(name: str, manifest: dict, rendered: dict) -> None:
    """Render one config into `rendered` as {snapshot-relative path: bytes}."""
    scan = []  # bytes still to be searched for payload references

    def collect(*parts: str, data: bytes) -> None:
        # Normalize on the way in, so the dedup below compares the bytes the
        # fixture actually stores rather than build-specific store hashes.
        rendered[os.path.join(name, *parts)] = STORE_HASH.sub(NORMALIZED, data)

    for unit, info in sorted(manifest["units"].items()):
        text = info["text"]
        for key in ("wantedBy", "requiredBy"):
            if info[key]:
                text += "\n# X-Golden-%s: %s" % (key, " ".join(info[key]))
        # newline-terminate so git/diff treat fixture files as clean text
        data = text.rstrip("\n").encode() + b"\n"
        collect("units", unit, data=data)
        scan.append(data)

    for rel, source in sorted(manifest["etc"].items()):
        if os.path.isdir(source):
            sys.exit(f"golden-snapshot: etc/{rel} is a directory; "
                     "extend the script if that is intentional")
        data = open(source, "rb").read()
        collect("etc", rel, data=data)
        scan.append(data)

    if manifest["tmpfiles"]:
        data = ("\n".join(manifest["tmpfiles"]) + "\n").encode()
        collect("tmpfiles.d", "agent-box.conf", data=data)
        scan.append(data)

    # Follow /nix/store references to module-generated payloads, recursively.
    seen = {}  # payload name -> store root it came from
    queue = [r for data in scan for r in payload_roots(data)]
    while queue:
        root, pname = queue.pop()
        if pname in seen:
            if seen[pname] != root:
                sys.exit(f"golden-snapshot: payload name collision: {pname} "
                         f"is both {seen[pname]} and {root}")
            continue
        seen[pname] = root
        files = []
        if os.path.isdir(root):
            for dirpath, _dirnames, filenames in os.walk(root):
                rel = os.path.relpath(dirpath, root)
                files += [(os.path.join(dirpath, f),
                           os.path.join(pname, rel, f) if rel != "."
                           else os.path.join(pname, f))
                          for f in sorted(filenames)]
        else:
            files = [(root, pname)]
        for src, rel in files:
            data = open(src, "rb").read()
            collect("payloads", rel, data=data)
            queue.extend(payload_roots(data))


def write_deduplicated(rendered: dict, out: str) -> None:
    """Write each distinct blob once; list the repeats in <out>/DUPLICATES.

    Ownership goes to the first path in sorted order, which is stable against
    anything but adding or removing a config: it does not depend on the order
    the payload walk happened to discover things in.
    """
    owner_of = {}  # content digest -> path holding those bytes
    duplicates = []

    for path in sorted(rendered):
        data = rendered[path]
        digest = hashlib.sha256(data).hexdigest()
        owner = owner_of.get(digest)
        if owner is not None:
            duplicates.append(f"{path} -> {owner}\n")
            continue
        owner_of[digest] = path
        dest = os.path.join(out, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)

    # Always written, even when empty: an absent file would make "nothing is
    # shared" indistinguishable from "this snapshot predates the index".
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "DUPLICATES"), "w") as fh:
        fh.write(DUPLICATES_HEADER)
        fh.writelines(duplicates)


def main() -> None:
    manifest_path, out = sys.argv[1], sys.argv[2]
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    rendered = {}
    for name in sorted(manifest):
        snapshot_config(name, manifest[name], rendered)
    write_deduplicated(rendered, out)


if __name__ == "__main__":
    main()
