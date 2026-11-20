#!/usr/bin/env python3
"""Prepare a reviewed local-webhook pin bump; upstream source is data only."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import urlopen

UPSTREAM = "defangdevs/local-channels"
PIN = "nix/webhook-pin.nix"
ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"^VERSION = ['\"]([0-9]+\.[0-9]+\.[0-9]+)['\"]$", re.MULTILINE)
TAG = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
SHA = re.compile(r"[0-9a-f]{40}")


def api(endpoint, paginate=False):
    """Read GitHub JSON, optionally retaining each paginated response."""
    command = ["gh", "api"]
    if paginate:
        command += ["--paginate", "--slurp"]
    return json.loads(subprocess.check_output(command + [endpoint], text=True))


def items(endpoint):
    """Flatten an API list endpoint across every page."""
    return [item for page in api(endpoint, paginate=True) for item in page]


def download(rev):
    """Read the webhook source at an immutable SHA without executing it."""
    if not SHA.fullmatch(rev):
        raise ValueError("Expected a full commit SHA")
    url = f"https://raw.githubusercontent.com/{UPSTREAM}/{rev}/local-webhook/webhook.py"
    with urlopen(url, timeout=30) as response:
        return response.read()


def digest(payload):
    """Return the flat file hash in Nix SRI format."""
    return "sha256-" + base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def version(payload):
    """Read a literal stable VERSION assignment as a sortable tuple."""
    match = VERSION.search(payload.decode("utf-8"))
    if not match:
        raise ValueError("Cannot read local-webhook VERSION")
    return tuple(int(part) for part in match[1].split("."))


def ci_runs(rev, branch):
    """The latest push run of upstream ci.yml for exactly this commit."""
    return api(
        f"repos/{UPSTREAM}/actions/workflows/ci.yml/runs"
        f"?branch={branch}&event=push&head_sha={rev}&per_page=1"
    )["workflow_runs"]


RELEASE_BOT = "github-actions[bot]"
COMMIT_FILES_PAGE_LIMIT = 300


def version_only_release_parent(rev, target_version):
    """The single parent of an automated release commit, or None.

    Only when the bot authored it, its subject starts with `release:`, it has
    one parent, and every changed line pair is the previous version string
    replaced by the target one (webhook.py VERSION, plugin.json, README row).
    Anything else, even one more changed character, keeps the strict rule.
    """
    commit = api(f"repos/{UPSTREAM}/commits/{rev}")
    author = (commit.get("author") or {}).get("login")
    message = (commit.get("commit") or {}).get("message", "")
    parents = commit.get("parents") or []
    files = commit.get("files") or []
    if author != RELEASE_BOT or not message.startswith("release:") or len(parents) != 1 or not files:
        return None
    # GitHub lists at most 300 files per commit page; a full page may hide more.
    if len(files) >= COMMIT_FILES_PAGE_LIMIT:
        return None
    new = ".".join(map(str, target_version))

    def pairs(file):
        patch = file.get("patch") or ""
        removed = [line[1:] for line in patch.splitlines()
                   if line.startswith("-")]
        added = [line[1:] for line in patch.splitlines()
                 if line.startswith("+")]
        return list(zip(removed, added)) if removed and len(removed) == len(added) else None

    source = [file for file in files if file.get("filename") == "local-webhook/webhook.py"]
    source_pairs = pairs(source[0]) if len(source) == 1 else None
    if not source_pairs or len(source_pairs) != 1:
        return None
    before, after = (VERSION.fullmatch(line) for line in source_pairs[0])
    if not before or not after or after[1] != new or before[1] == new:
        return None
    old = before[1]
    for file in files:
        changed = pairs(file)
        if not changed or any(b.replace(old, new) != a for b, a in changed):
            return None
    parent = parents[0].get("sha", "")
    return parent if SHA.fullmatch(parent) else None


def pin_fields(text):
    """Validate the pin format before any network access or replacement."""
    fields = {}
    for name in ("repo", "rev", "sha256"):
        matches = re.findall(rf'^  {name} = "([^"]+)";$', text, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError(f"Expected one literal {name} in {PIN}")
        fields[name] = matches[0]
    if fields["repo"] != UPSTREAM or not SHA.fullmatch(fields["rev"]):
        raise ValueError("Unexpected upstream repository or revision")
    return fields


def prepare(root, repository):
    """Return the update plan, proposed pin and PR body without writing files."""
    text = (root / PIN).read_text(encoding="utf-8")
    pin = pin_fields(text)
    # Include manually authored bumps, not just this workflow's branch names.
    for pull in items(f"repos/{repository}/pulls?state=open&per_page=100"):
        files = items(f"repos/{repository}/pulls/{pull['number']}/files?per_page=100")
        if any(file["filename"] == PIN or file.get("previous_filename") == PIN for file in files):
            return {"changed": False, "reason": f"PR #{pull['number']} already changes {PIN}"}, text, ""

    releases = []
    for tag in items(f"repos/{UPSTREAM}/tags?per_page=100"):
        match = TAG.fullmatch(tag["name"])
        if match:
            releases.append((tuple(map(int, match.groups())), tag))
    if not releases:
        raise ValueError("No stable local-webhook version tags found")
    target_version, tag = max(releases, key=lambda release: release[0])
    rev = tag["commit"]["sha"]
    if not SHA.fullmatch(rev):
        raise ValueError("Release does not resolve to a full commit SHA")
    if rev == pin["rev"]:
        return {"changed": False, "reason": "Already pinned to the latest release"}, text, ""

    current = download(pin["rev"])
    if digest(current) != pin["sha256"]:
        raise ValueError("Current upstream file does not match its pinned hash")
    if target_version <= version(current):
        return {"changed": False, "reason": "Latest release is not newer than the current pin"}, text, ""
    payload = download(rev)
    if version(payload) != target_version:
        raise ValueError("Release tag does not match webhook.py VERSION")

    branch = api(f"repos/{UPSTREAM}")["default_branch"]
    ci_rev = rev
    runs = ci_runs(rev, branch)
    if not runs:
        # Upstream's release job pushes `release: ...` with the workflow
        # token, and GitHub starts no workflow for such a push, so a release
        # commit never has a CI run of its own. That stalled every bump after
        # 0.38.0. Accept the parent's run only for a commit that changes
        # nothing but the version string.
        parent = version_only_release_parent(rev, target_version)
        if parent:
            ci_rev = parent
            runs = ci_runs(parent, branch)
    if not runs or any((
        runs[0]["head_sha"] != ci_rev,
        runs[0]["head_branch"] != branch,
        runs[0]["event"] != "push",
        runs[0]["path"] != ".github/workflows/ci.yml",
        runs[0]["status"] != "completed",
        runs[0]["conclusion"] != "success",
    )):
        raise ValueError("Latest upstream CI run for the release on the default branch is not green")

    comparison = api(
        f"repos/{UPSTREAM}/compare/{pin['rev']}...{rev}?per_page=100", paginate=True
    )
    status = comparison[0]["status"]
    if status not in ("ahead", "diverged"):
        raise ValueError(f"Release does not advance the current pin ({status})")
    commits = [commit for page in comparison for commit in page["commits"]]
    body = [
        f"Bump local-webhook to **{tag['name']}**.",
        f"\nPrevious pin: `{pin['rev']}`. New pin: `{rev}`.",
        f"\nUpstream CI: {runs[0]['html_url']}",
        f"\nCompare: https://github.com/{UPSTREAM}/compare/{pin['rev']}...{rev}",
        "\nReview watch/dispatch defaults and deployment assumptions before merging.",
        "The workflow updates only the pin and assembled module; it never merges.",
    ]
    if status == "diverged":
        body += [
            "\n**History diverged:** the old pin may precede a squash merge.",
            "The release version is newer; review both sides of the comparison for lost changes.",
        ]
    body += ["\nUpstream commits since the previous pin:\n"]
    for commit in commits:
        subject = commit["commit"]["message"].splitlines()[0].replace("`", "'")
        body.append(f"- [{commit['sha'][:12]}](https://github.com/{UPSTREAM}/commit/{commit['sha']}) `{subject}`")
    body += [
        "\nValidation: module assembly and assembler tests run before opening this PR.",
        "PR CI may need **Approve workflows to run** because this PR uses `GITHUB_TOKEN`.",
    ]
    updated = text.replace(f'  rev = "{pin["rev"]}";', f'  rev = "{rev}";')
    updated = updated.replace(f'  sha256 = "{pin["sha256"]}";', f'  sha256 = "{digest(payload)}";')
    return {"changed": True, "version": tag["name"], "rev": rev}, updated, "\n".join(body) + "\n"


def main():
    """Apply an explicitly requested write only after all release checks pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Write the pin after all checks pass")
    parser.add_argument("--body", type=Path, help="Write the proposed PR description here")
    args = parser.parse_args()
    try:
        plan, updated, body = prepare(ROOT, os.environ.get("GITHUB_REPOSITORY", "defangdevs/agent-box"))
        if args.body:
            args.body.write_text(body, encoding="utf-8")
        if args.write and plan["changed"]:
            (ROOT / PIN).write_text(updated, encoding="utf-8")
        print(json.dumps(plan))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Pin update refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
