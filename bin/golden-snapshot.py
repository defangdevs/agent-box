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
"""
import json
import os
import re
import sys

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


def write(dest: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(STORE_HASH.sub(NORMALIZED, data))


def payload_roots(data: bytes):
    """Store roots (path, name) of agent-box payloads referenced in data."""
    for m in PAYLOAD.finditer(data):
        yield m.group(0).decode(), m.group(1).decode()


def snapshot_config(name: str, manifest: dict, out: str) -> None:
    base = os.path.join(out, name)
    scan = []  # bytes still to be searched for payload references

    for unit, info in sorted(manifest["units"].items()):
        text = info["text"]
        for key in ("wantedBy", "requiredBy"):
            if info[key]:
                text += "\n# X-Golden-%s: %s" % (key, " ".join(info[key]))
        # newline-terminate so git/diff treat fixture files as clean text
        data = text.rstrip("\n").encode() + b"\n"
        write(os.path.join(base, "units", unit), data)
        scan.append(data)

    for rel, source in sorted(manifest["etc"].items()):
        if os.path.isdir(source):
            sys.exit(f"golden-snapshot: etc/{rel} is a directory; "
                     "extend the script if that is intentional")
        data = open(source, "rb").read()
        write(os.path.join(base, "etc", rel), data)
        scan.append(data)

    if manifest["tmpfiles"]:
        data = ("\n".join(manifest["tmpfiles"]) + "\n").encode()
        write(os.path.join(base, "tmpfiles.d", "agent-box.conf"), data)
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
            write(os.path.join(base, "payloads", rel), data)
            queue.extend(payload_roots(data))


def main() -> None:
    manifest_path, out = sys.argv[1], sys.argv[2]
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    for name in sorted(manifest):
        snapshot_config(name, manifest[name], out)


if __name__ == "__main__":
    main()
