#!/usr/bin/env python3
"""Verify — and bump — the third-party assets vendored under modules/src/vendor.

Why vendoring at all
--------------------
A deployed box fetches modules/agent-box.nix as ONE file (issue #51), and the
settings daemon serves pages that load no external asset. So a CDN reference is
not an option even in principle: anything the page needs has to be inside the
module. That makes the repo the distribution channel for somebody else's code,
and this script is what keeps that honest.

What it checks (no network, no Nix, no third-party module):

  * every vendored file hashes to the `sha256` its pin records — so a local
    edit, a truncated download or a swapped file fails here rather than
    shipping to every box;
  * that hash is UPSTREAM's own, because the file is byte-identical to the
    artifact at `url` — so anyone can re-derive it from the source;
  * every file under vendor/ is pinned, and every pin has a file. A vendored
    asset nobody declared is exactly the thing that later gets bumped by
    nobody;
  * each pin is actually reachable from the module — some modules/src file
    includes it — so a dead vendored asset is not silently carried.

What it does with `--upstream` (needs network): asks each pin's forge for the
current release and reports which pins are behind. That is the "tell me an
update exists" half; .github/workflows/vendor-updates.yml runs it weekly and
opens an issue, so nothing depends on a human remembering to look.

Bumping: `--update idiomorph` (optionally `--version 0.8.0`) downloads, checks
it parses as the kind of asset it claims to be, rewrites the file and the pin,
and tells you what to run next. Always run `nix run .#assemble` afterwards —
the generated module carries a copy, and CI fails until it matches.
"""
import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "modules" / "src" / "vendor"
MANIFEST = VENDOR / "vendor.json"
SRC = REPO / "modules" / "src"

# Anything in vendor/ that is not a vendored asset. The manifest describes the
# others, so it cannot describe itself.
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
                f"    {pkg['url']} , or bump the pin with --update {name}."
            )
        # A pin whose asset nothing includes is dead weight that still gets
        # bumped, reviewed and audited. Cheap to catch, so catch it.
        marker = f"@@include:vendor/{rel}@@"
        if not any(marker in f.read_text(errors="ignore")
                   for f in SRC.rglob("*") if f.is_file() and f.suffix in (".py", ".nix", ".in")):
            problems.append(
                f"{name}: nothing includes it — no modules/src file carries "
                f"`{marker}`. Wire it up or drop the pin."
            )
        used_by = pkg.get("used_by", "").split(" ")[0]
        if used_by and not (REPO / used_by).exists():
            problems.append(f"{name}: used_by names a file that does not exist: {used_by}")

    # rglob, not iterdir: a pin's `path` may name a subdirectory, and an asset
    # dropped into one would otherwise be embedded while escaping this check
    # entirely — which is the single thing it exists to prevent.
    for path in sorted(VENDOR.rglob("*")):
        rel = path.relative_to(VENDOR).as_posix()
        if path.is_file() and rel not in NOT_AN_ASSET and rel not in pinned:
            problems.append(
                f"{rel}: vendored but not pinned — add it to "
                f"{MANIFEST.relative_to(REPO)} so it gets verified and tracked."
            )
    return problems


def latest_release(repo: str) -> str:
    """The newest release tag on a GitHub repo, or '' when it cannot be read."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp).get("tag_name", "")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  ! could not reach GitHub for {repo}: {exc}", file=sys.stderr)
        return ""


def check_upstream(packages: list) -> list:
    """Return the pins that are behind, as (name, pinned_tag, latest_tag)."""
    behind = []
    for pkg in packages:
        latest = latest_release(pkg["repo"])
        if not latest:
            continue
        if latest != pkg["tag"]:
            behind.append((pkg["name"], pkg["tag"], latest))
            print(f"  {pkg['name']}: pinned {pkg['tag']}, upstream {latest}  BEHIND")
        else:
            print(f"  {pkg['name']}: {pkg['tag']} (current)")
    return behind


def update(packages: list, name: str, version: str) -> int:
    pkg = next((p for p in packages if p["name"] == name), None)
    if pkg is None:
        print(f"no such pin: {name}", file=sys.stderr)
        return 1
    tag = version if version else latest_release(pkg["repo"])
    if not tag:
        return 1
    if not tag.startswith("v"):
        tag = "v" + tag
    url = pkg["url_template"].format(repo=pkg["repo"], tag=tag)
    print(f"{name}: {pkg['tag']} -> {tag}\n  {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  download failed: {exc}", file=sys.stderr)
        return 1
    # A 404 from raw.githubusercontent is a 404, but a moved dist path could
    # still return an HTML error page with a 200 from some other forge. Refuse
    # anything that is not plausibly the asset.
    text = body.decode("utf-8", errors="replace")
    if pkg["path"].endswith(".js") and ("<html" in text[:200].lower() or not text.strip()):
        print("  that does not look like JavaScript — check url_template", file=sys.stderr)
        return 1
    path = VENDOR / pkg["path"]
    path.write_bytes(body)
    digest = sha256_of(path)
    # Rewritten through the parser, not by substitution: a regex over the raw
    # text would rewrite the FIRST "version"/"sha256" in the file regardless of
    # which package it belongs to, which is correct only while there is exactly
    # one pin. Dumping preserves key order, so the diff stays a four-line one.
    doc = json.loads(MANIFEST.read_text())
    entry = next(p for p in doc["packages"] if p["name"] == name)
    entry.update(version=tag.lstrip("v"), tag=tag, url=url, sha256=digest)
    MANIFEST.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {path.relative_to(REPO)} ({len(body)} bytes, sha256 {digest[:16]}…)")
    print("  next: nix run .#assemble && python3 tests/test-assemble-module.py")
    print("        then nix run .#update-golden, and review both diffs.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", action="store_true",
                    help="also ask each forge whether the pin is behind (needs network)")
    ap.add_argument("--update", metavar="NAME", help="bump NAME to the latest release")
    ap.add_argument("--version", default="",
                    help="with --update: pin this version instead of the latest")
    args = ap.parse_args()

    packages = load_manifest()
    if args.update:
        return update(packages, args.update, args.version)

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

    if args.upstream:
        print("\nupstream:")
        behind = check_upstream(packages)
        if behind:
            names = ", ".join(f"{n} ({old} -> {new})" for n, old, new in behind)
            print(f"\n{len(behind)} pin(s) behind: {names}")
            return 2
        print("\nevery pin is at the latest release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
