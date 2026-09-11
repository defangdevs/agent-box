#!/usr/bin/env python3
"""Build, show and verify a release candidate's manifest (issue #632).

Until this existed, "a release" was a commit sha and nothing else, and the
two things that install agent-box for the public disagreed about what they
were shipping:

  * deploy-test.yml pinned AgentBoxRev/AgentBoxSha256 to the commit that
    triggered it and left AgentNixpkgsUrl/AgentNixpkgsSha256 EMPTY, so the
    box it booted tracked whatever the nixos-unstable channel was at boot;
  * publish-template.yml re-resolved that channel at publish time and
    injected the pair it happened to get into the S3 templates.

So the box that passed the fresh-boot test and the box a 1-click launch
creates were never pinned to the same dependency set, and neither identity
was written down anywhere afterwards. An immutable source commit is not a
complete release manifest.

This is that manifest. It is built ONCE per candidate, from one commit,
and everything downstream - the deployment test, the published templates,
an operator asking what a box is running - reads it instead of resolving
anything again:

    rev                  the commit; the source identity
    module_sha256        SRI hash of modules/agent-box.nix at that rev,
                         which is how template.yaml fetches it (issue #51)
    flake_ref            github:OWNER/REPO/<rev>, which is how
                         lightsail-template.yaml installs the runtime
    flake_lock_sha256    the flake's own pinned input set at that rev
    agent_nixpkgs        the ONE mutable external input: the resolved
                         channel SNAPSHOT url plus its unpacked hash
    templates            hash per deployment template at that rev

Three verbs:

    build    resolve every identity and write the manifest
    show     print it the way a workflow log and a PR body want it
    verify   recompute every identity from the recorded rev and refuse any
             difference - the reproducibility proof, and what says whether
             an installed box is running the candidate that was tested

Runnable without Nix for everything but the channel hash, which is
`nix-prefetch-url`'s answer and nothing else's.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

MANIFEST_VERSION = 1

# A channel tarball is tens of megabytes, so this is generous for the
# download - but it has to stay BELOW the 15-minute timeout of promote.yml's
# `candidate` job, which spends time on checkout, rev resolution and the
# CI-gate check before ever reaching this call. At 900s (== the job's own
# timeout) a stalled prefetch would be killed by the JOB timeout first,
# which GitHub reports as `cancelled` - indistinguishable from a supersede,
# and invisible to a standing webhook watch that only spawns on
# `failure`/`timed_out` (see AGENTS.md, "Give a long job a STEP-level
# timeout-minutes"). 600s leaves the preceding steps headroom and still
# fails as a reported ManifestError rather than a silent cancellation.
PREFETCH_TIMEOUT = 600

# The channel the templates' AgentNixpkgsUrl pair pins. Kept here rather
# than in the workflow because `verify` has to resolve the same one.
DEFAULT_CHANNEL = "https://channels.nixos.org/nixos-unstable"

# Hashed into the manifest because the published template IS these files
# with defaults injected: a template edited after the deployment test is a
# different artifact, whatever the rev says.
TEMPLATES = (
    "deploy/aws/template.yaml",
    "deploy/aws/lightsail-template.yaml",
)

# The module a box fetches as a single file (issue #51).
MODULE = "modules/agent-box.nix"

RAW = "https://raw.githubusercontent.com/{repo}/{rev}/{path}"

# Fields that describe when the manifest was made rather than what it
# describes. `verify` ignores them; everything else must match exactly.
PROVENANCE = ("created", "created_by", "manifest_version")


class ManifestError(Exception):
    """A candidate whose identities could not be resolved or did not match."""


def sri(data):
    """`sha256-<base64>`, the form Nix's fetchurl and the templates want."""
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def hexsum(data):
    return hashlib.sha256(data).hexdigest()


def read_remote(repo, rev, path, timeout=60):
    """The bytes GitHub serves for one path at one commit.

    By rev, never by branch: this is the same immutable URL a launching box
    fetches the module from, so what is hashed here is what a box gets.
    """
    url = RAW.format(repo=repo, rev=rev, path=path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            return fh.read()
    except Exception as exc:                     # noqa: BLE001 - reported
        raise ManifestError(f"cannot read {url}: {exc}") from exc


def read_source(repo, rev, path, source_dir=None, check_remote=True):
    """One candidate file, and the assurance the rev really serves it.

    A local checkout is the cheap and obvious source, and it is also the
    one that can lie: a workflow with an uncommitted edit, or a checkout
    at another rev, would hash bytes no launching box will ever see. So
    the remote copy at that exact rev is fetched and compared unless the
    caller says not to.
    """
    if source_dir is None:
        return read_remote(repo, rev, path)
    local = os.path.join(source_dir, path)
    try:
        with open(local, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ManifestError(f"cannot read {local}: {exc}") from exc
    if check_remote:
        remote = read_remote(repo, rev, path)
        if remote != data:
            raise ManifestError(
                f"{path} in {source_dir} differs from {repo}@{rev[:12]} - "
                "the tree is not the candidate it claims to be")
    return data


def resolve_channel(channel=DEFAULT_CHANNEL, timeout=60):
    """The channel's current SNAPSHOT url - immutable once resolved.

    channels.nixos.org/nixos-unstable is a redirect to a dated release
    directory. The redirect target is a fixed artifact; the redirector is
    not. Recording the target is what turns "we built against unstable"
    into a dependency identity.
    """
    override = os.environ.get("AGENT_BOX_CHANNEL_URL")
    if override:
        return override
    req = urllib.request.Request(channel, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            resolved = fh.geturl()
    except Exception as exc:                     # noqa: BLE001 - reported
        raise ManifestError(f"cannot resolve {channel}: {exc}") from exc
    return resolved.rstrip("/") + "/nixexprs.tar.xz"


def prefetch(url):
    """`nix-prefetch-url --unpack`, which is the only source of this hash."""
    tool = shutil.which("nix-prefetch-url")
    if not tool:
        raise ManifestError(
            "nix-prefetch-url is not on PATH, so the channel hash cannot be "
            "computed - a manifest without it is not a release manifest")
    try:
        # Bounded, because an unbounded stall here would run the job out of
        # its own timeout - and a job that exceeds its timeout is reported
        # `cancelled`, which this repo has already learned is
        # indistinguishable from a routine supersede and so reaches nobody.
        proc = subprocess.run([tool, "--unpack", url], capture_output=True,
                              text=True, timeout=PREFETCH_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ManifestError(
            f"nix-prefetch-url {url} did not finish within "
            f"{PREFETCH_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise ManifestError(f"nix-prefetch-url {url} failed: {proc.stderr}")
    out = proc.stdout.strip().splitlines()
    if not out:
        raise ManifestError(f"nix-prefetch-url {url} printed nothing")
    return out[-1].strip()


def build(repo, rev, source_dir=None, check_remote=True,
          channel=DEFAULT_CHANNEL, created_by=None):
    """Every identity of one candidate, resolved exactly once."""
    if len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise ManifestError(
            f"rev must be a full 40-character commit sha, got {rev!r} - a "
            "branch or short sha is not an immutable identity")

    module = read_source(repo, rev, MODULE, source_dir, check_remote)
    lock = read_source(repo, rev, "flake.lock", source_dir, check_remote)
    templates = {
        path: hexsum(read_source(repo, rev, path, source_dir, check_remote))
        for path in TEMPLATES
    }
    url = resolve_channel(channel)
    return {
        "manifest_version": MANIFEST_VERSION,
        "created": datetime.datetime.now(datetime.timezone.utc)
                           .replace(microsecond=0).isoformat(),
        "created_by": created_by or "release_manifest.py",
        "repo": repo,
        "rev": rev,
        "module_path": MODULE,
        "module_sha256": sri(module),
        "flake_ref": f"github:{repo}/{rev}",
        "flake_lock_sha256": hexsum(lock),
        "agent_nixpkgs": {
            "channel": channel,
            "url": url,
            "sha256": prefetch(url),
        },
        "templates": templates,
    }


def verify(manifest, source_dir=None, check_remote=True, expect_rev=None):
    """Recompute the manifest from its own rev and report every difference.

    The channel entry is re-hashed from the URL the manifest RECORDED, not
    from the channel: re-resolving the redirect would compare the candidate
    against whatever unstable moved to since, which is the exact confusion
    this file exists to end.
    """
    problems = []
    repo, rev = manifest.get("repo"), manifest.get("rev")
    if not repo or not rev:
        raise ManifestError("manifest has no repo/rev - nothing to verify")
    if expect_rev and expect_rev != rev:
        problems.append(
            f"rev: manifest describes {rev}, expected {expect_rev}")

    try:
        module = read_source(repo, rev, MODULE, source_dir, check_remote)
        if sri(module) != manifest.get("module_sha256"):
            problems.append(
                f"module_sha256: {MODULE} at {rev[:12]} hashes to "
                f"{sri(module)}, manifest says {manifest.get('module_sha256')}")
    except ManifestError as exc:
        problems.append(str(exc))

    try:
        lock = read_source(repo, rev, "flake.lock", source_dir, check_remote)
        if hexsum(lock) != manifest.get("flake_lock_sha256"):
            problems.append(
                f"flake_lock_sha256: flake.lock at {rev[:12]} hashes to "
                f"{hexsum(lock)}, manifest says "
                f"{manifest.get('flake_lock_sha256')}")
    except ManifestError as exc:
        problems.append(str(exc))

    recorded = manifest.get("templates") or {}
    if set(recorded) != set(TEMPLATES):
        problems.append(
            f"templates: manifest records {sorted(recorded)}, this release "
            f"ships {sorted(TEMPLATES)}")
    for path in sorted(set(recorded) & set(TEMPLATES)):
        try:
            got = hexsum(read_source(repo, rev, path, source_dir,
                                     check_remote))
        except ManifestError as exc:
            problems.append(str(exc))
            continue
        if got != recorded[path]:
            problems.append(
                f"templates[{path}]: hashes to {got}, manifest says "
                f"{recorded[path]}")

    pins = manifest.get("agent_nixpkgs") or {}
    if not pins.get("url") or not pins.get("sha256"):
        problems.append(
            "agent_nixpkgs: no url/sha256 pair - this is the mutable "
            "external input, so a manifest without it promotes something "
            "that was never pinned")
    elif not os.environ.get("AGENT_BOX_SKIP_PREFETCH"):
        try:
            got = prefetch(pins["url"])
        except ManifestError as exc:
            problems.append(str(exc))
        else:
            if got != pins["sha256"]:
                problems.append(
                    f"agent_nixpkgs.sha256: {pins['url']} now hashes to "
                    f"{got}, manifest says {pins['sha256']}")

    expected = {"repo", "rev", "module_path", "module_sha256", "flake_ref",
                "flake_lock_sha256", "agent_nixpkgs", "templates"}
    missing = expected - set(manifest)
    if missing:
        problems.append(f"missing field(s): {', '.join(sorted(missing))}")
    if manifest.get("flake_ref") != f"github:{repo}/{rev}":
        problems.append(
            f"flake_ref: {manifest.get('flake_ref')} does not name "
            f"{repo}@{rev}")
    return problems


def summary(manifest):
    pins = manifest.get("agent_nixpkgs") or {}
    lines = [
        f"repo               {manifest.get('repo')}",
        f"rev                {manifest.get('rev')}",
        f"flake_ref          {manifest.get('flake_ref')}",
        f"module_sha256      {manifest.get('module_sha256')}",
        f"flake_lock_sha256  {manifest.get('flake_lock_sha256')}",
        f"agent_nixpkgs url  {pins.get('url')}",
        f"agent_nixpkgs sha  {pins.get('sha256')}",
    ]
    for path, digest in sorted((manifest.get("templates") or {}).items()):
        lines.append(f"template           {digest}  {path}")
    lines.append(f"built              {manifest.get('created')} by "
                 f"{manifest.get('created_by')}")
    return "\n".join(lines)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump(manifest, path):
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(text)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="verb", required=True)

    def shared(p):
        p.add_argument("--source-dir", default=None, metavar="DIR",
                       help="read the candidate's files from a local "
                            "checkout instead of fetching them")
        p.add_argument("--no-remote-check", action="store_true",
                       help="trust --source-dir without comparing it "
                            "against the rev GitHub serves")
        return p

    b = shared(sub.add_parser("build", help="resolve and write a manifest"))
    b.add_argument("--repo", required=True, metavar="OWNER/REPO")
    b.add_argument("--rev", required=True, metavar="SHA",
                   help="the candidate commit, in full")
    b.add_argument("--channel", default=DEFAULT_CHANNEL)
    b.add_argument("--created-by", default=None)
    b.add_argument("--out", default="-", metavar="FILE")

    v = shared(sub.add_parser("verify", help="recompute and compare"))
    v.add_argument("manifest")
    v.add_argument("--rev", default=None, metavar="SHA",
                   help="also require the manifest to describe this rev - "
                        "how a box's reported rev is checked against the "
                        "candidate that was tested")

    s = sub.add_parser("show", help="print a manifest readably")
    s.add_argument("manifest")

    f = sub.add_parser("field", help="print one recorded value")
    f.add_argument("manifest")
    f.add_argument("path", metavar="a.dotted.path",
                   help="e.g. rev, module_sha256, agent_nixpkgs.url")

    args = ap.parse_args(argv)
    try:
        if args.verb == "build":
            manifest = build(args.repo, args.rev,
                             source_dir=args.source_dir,
                             check_remote=not args.no_remote_check,
                             channel=args.channel,
                             created_by=args.created_by)
            dump(manifest, args.out)
            if args.out != "-":
                print(summary(manifest))
            return 0
        if args.verb == "show":
            print(summary(load(args.manifest)))
            return 0
        if args.verb == "field":
            # For the workflows, which need single values on stdout and
            # must not silently inject an empty Default: when a field is
            # missing.
            value = load(args.manifest)
            for part in args.path.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ManifestError(
                        f"{args.manifest} has no {args.path}")
                value = value[part]
            if value in (None, "", {}, []):
                raise ManifestError(
                    f"{args.manifest}: {args.path} is empty")
            print(value)
            return 0
        problems = verify(load(args.manifest),
                          source_dir=args.source_dir,
                          check_remote=not args.no_remote_check,
                          expect_rev=args.rev)
        if problems:
            for problem in problems:
                print(f"::error::{problem}", file=sys.stderr)
            print(f"{args.manifest}: {len(problems)} difference(s) - this is "
                  "not the candidate it claims to be", file=sys.stderr)
            return 1
        print(f"{args.manifest}: every recorded identity still matches")
        print(summary(load(args.manifest)))
        return 0
    except ManifestError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
