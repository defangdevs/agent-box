#!/usr/bin/env python3
"""Verify the third-party assets vendored under docs/vendor (issue #25).

This is docs/vendor's own twin of scripts/check_vendor.py, not an extension
of it: that script's "reachable from the module" check looks for an
`@@include:vendor/...@@` marker inside modules/src, which is how a payload
gets embedded into the single-file modules/agent-box.nix (issue #51) — a
constraint that has nothing to do with docs/, a plain static site that can
(and does) just link a second file with a <script src> tag. Sharing one
script over two differently-shaped conventions would either bend this one to
fit that reachability check or silently skip it; a second, smaller script
is the honest way to say "same idea, different rules".

What it checks (no network):

  * every vendored file hashes to the `sha256` its pin records — so a local
    edit, a truncated download or a swapped file fails here rather than
    shipping to the public launch page;
  * every file under docs/vendor/ is pinned, and every pin has a file;
  * each pin's `used_by` file exists and actually references the vendored
    file by its path, so a dead vendored asset is not silently carried.

Bumping is manual: re-run the same `curl -sSL <url> | sha256sum` the pin's
own url records, diff the file, and update both the file and its `sha256`
and `version` fields together. There is no --update here (unlike
scripts/check_vendor.py) because docs/vendor only holds one file today and
a bump tool for one pin is not worth the code paths it would need to get
right.
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "docs" / "vendor"
MANIFEST = VENDOR / "vendor.json"

NOT_AN_ASSET = {"vendor.json"}


def load_manifest() -> list:
    with MANIFEST.open() as fh:
        return json.load(fh)["packages"]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_integrity(packages: list) -> list:
    """Return a list of human-readable problems; empty means everything holds."""
    problems = []
    pinned = set()
    for pkg in packages:
        name, rel = pkg["name"], pkg["path"]
        path = VENDOR / rel
        pinned.add(rel)
        if not path.exists():
            problems.append(f"{name}: pinned file is missing: {path.relative_to(REPO)}")
            continue
        got = sha256_of(path)
        if got != pkg["sha256"]:
            problems.append(
                f"{name}: {rel} does not match its pin\n"
                f"    pinned {pkg['sha256']}\n"
                f"    actual {got}\n"
                f"    A vendored file is never edited in place — re-download it from\n"
                f"    {pkg['url']} and update both the file and this pin together."
            )
        used_by = pkg.get("used_by", "").split(" ")[0].split(",")[0]
        used_by_path = REPO / used_by
        if not used_by:
            continue
        if not used_by_path.exists():
            problems.append(f"{name}: used_by names a file that does not exist: {used_by}")
        elif f"vendor/{rel}" not in used_by_path.read_text(errors="ignore"):
            problems.append(
                f"{name}: {used_by} does not reference vendor/{rel} — "
                f"nothing loads this pin. Wire it up or drop the pin."
            )

    for path in sorted(VENDOR.rglob("*")):
        rel = path.relative_to(VENDOR).as_posix()
        if path.is_file() and rel not in NOT_AN_ASSET and rel not in pinned:
            problems.append(
                f"{rel}: vendored but not pinned — add it to "
                f"{MANIFEST.relative_to(REPO)} so it gets verified and tracked."
            )
    return problems


def main() -> int:
    packages = load_manifest()
    print(f"vendored assets: {len(packages)}")
    for pkg in packages:
        print(f"  {pkg['name']} {pkg['version']} ({pkg['license']}) — {pkg['path']}")
    problems = check_integrity(packages)
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"\n{len(problems)} problem(s)")
        return 1
    print("all vendored files match their pins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
