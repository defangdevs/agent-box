#!/usr/bin/env python3
"""Install an unmerged ref on a native test box, then restore its baseline.

Operator-only: run as root, outside an agent session, from a trusted copy.
Uses the installed updater; adds no service, sudo grant or settings control.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    for command in ("install", "restore", "status"):
        sub = commands.add_parser(command)
        sub.add_argument("--profile", type=Path,
                         default=Path("/nix/var/nix/profiles/agent-box"))
        sub.add_argument("--state-dir", type=Path,
                         default=Path("/var/lib/agent-box/test-update"))
        if command == "install":
            target = sub.add_mutually_exclusive_group(required=True)
            target.add_argument("--pr", type=int)
            target.add_argument("--ref", help="branch or tag to resolve once")
            sub.add_argument("--config", default="/etc/agent-box/config.yaml")
            sub.add_argument("--check", action="store_true",
                             help="resolve and print the target; change nothing")
    return ap


def sync_directory(path):
    """Make published records and cleanup transitions durable before proceeding."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked_state(args, create=False):
    """Only inspect or change trial records under the private state lock."""
    state = args.state_dir.absolute()
    if create:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state.lstat()
    if state.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("state directory must be owned by root with mode 0700")
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield state


def saved_record(path, profile):
    """Bind recovery metadata to the operator-selected profile."""
    saved = json.loads(path.read_text())
    if saved.get("profile") != str(profile.absolute()):
        raise ValueError("recorded trial belongs to another profile")
    return saved


def current_revision(profile):
    """Read locked origin metadata without executing a possibly broken CLI."""
    try:
        elements = json.loads((profile / "manifest.json").read_text())["elements"]
        items = list(elements.items()) if isinstance(elements, dict) else list(enumerate(elements))
        for name, entry in items:
            if not isinstance(entry, dict):
                continue
            if name != "runtime" and not str(entry.get("attrPath", "")).endswith(".runtime") and len(items) != 1:
                continue
            origin = entry.get("url") or entry.get("uri") or ""
            if origin.startswith("github:"):
                parts = origin[len("github:"):].split("/")
                rev = parts[2] if len(parts) == 3 else ""
            else:
                rev = parse_qs(urlsplit(origin).query).get("rev", [""])[0]
            return rev if re.fullmatch(r"[0-9a-f]{40}", rev) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def complete_cleanup(state):
    """Keep the cleanup marker until removing the helper's GC root is durable."""
    (state / "baseline").unlink(missing_ok=True)
    sync_directory(state)
    (state / "cleanup.json").unlink()
    sync_directory(state)


def state_command(args):
    """Report recovery state or resume cleanup without requiring a baseline CLI."""
    with locked_state(args) as state:
        record = state / "trial.json"
        cleanup = state / "cleanup.json"
        if record.exists() and cleanup.exists():
            raise ValueError("conflicting recovery records; both retained")
        path = cleanup if cleanup.exists() else record
        if not path.exists():
            raise ValueError("no trial is recorded")
        saved = saved_record(path, args.profile)
        if args.command == "status":
            print(json.dumps(dict(saved, current=current_revision(args.profile),
                                  baselineAvailable=(state / "baseline/bin/agentbox").is_file(),
                                  cleanupPending=cleanup.exists()), indent=2))
            return 0
        if cleanup.exists():
            complete_cleanup(state)
            print(f"completed restore cleanup for {saved['repo']}@{saved['rev']}")
            return 0
    return None


def operate(args, cli):
    """Keep the recovery record until restoration has actually succeeded."""
    profile = args.profile.absolute()
    state_dir = args.state_dir.absolute()
    cli_path = profile / "bin/agentbox"
    record = state_dir / "trial.json"
    baseline = state_dir / "baseline"

    if args.command in ("status", "restore"):
        result = state_command(args)
        if result is not None:
            return result

    repo, current = cli["profile_origin"](profile)
    if not repo or not current:
        raise ValueError("cannot identify the installed native runtime")
    if args.command == "install":
        if args.pr is not None and args.pr <= 0:
            raise ValueError("--pr must be a positive number")
        ref = f"refs/pull/{args.pr}/head" if args.pr else args.ref
        if not ref or ref.startswith("-") or any(c.isspace() for c in ref):
            raise ValueError("invalid ref")
        target = cli["source_tree"](
            profile, "check", url=f"https://github.com/{repo}.git", ref=ref)
        if not cli["REV_RE"].fullmatch(target):
            raise ValueError("remote did not return a full commit ID")
        print(f"{repo}: {current} -> {target} ({ref})", flush=True)
        if args.check:
            return 0

    with locked_state(args, create=True):
        if args.command == "install":
            if record.exists() or (state_dir / "cleanup.json").exists():
                raise ValueError("a trial is already recorded; restore it first")
            if target == current:
                print("already running that revision; no trial started")
                return 0
            # An indirect GC root keeps the original executable and closure
            # available even when a candidate is older or GC runs mid-trial.
            subprocess.run(["nix-store", "--add-root", str(baseline),
                            "--indirect", "--realise", str(profile.resolve())],
                           check=True)
            saved = {"repo": repo, "rev": current, "target": target,
                     "ref": ref, "profile": str(profile),
                     "config": str(Path(args.config).absolute())}
            temporary = record.with_suffix(".tmp")
            try:
                with temporary.open("w") as stream:
                    json.dump(saved, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(record)
                sync_directory(state_dir)
            except OSError:
                if not record.exists():
                    baseline.unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
                    sync_directory(state_dir)
                raise
            executable, revision = cli_path, target
        else:
            if not record.exists():
                raise ValueError("no trial is recorded")
            saved = saved_record(record, profile)
            executable, revision = baseline / "bin/agentbox", saved["rev"]
            if not executable.is_file():
                raise ValueError("baseline closure is missing; recovery record retained")

        # A candidate can diverge from main, and returning can be a downgrade.
        # Only this explicit root-operated helper bypasses the ancestry guard.
        result = subprocess.run([
            str(executable), "update", "--profile", str(profile),
            "--config", saved["config"], "--repo", saved["repo"],
            "--rev", revision, "--force"])
        if result.returncode:
            print("update failed; baseline and recovery record retained", file=sys.stderr)
            return result.returncode
        _, installed = cli["profile_origin"](profile)
        if installed != revision:
            raise ValueError("installed revision did not match; recovery record retained")
        if args.command == "restore":
            record.replace(state_dir / "cleanup.json")
            sync_directory(state_dir)
            complete_cleanup(state_dir)
            print(f"restored {saved['repo']}@{revision}")
        else:
            print("trial installed; use this helper's restore command when finished")
        return 0


def main(argv=None):
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        print("test-box-update: requires an operator root shell on a test box",
              file=sys.stderr)
        return 1
    try:
        if args.command in ("status", "restore"):
            result = state_command(args)
            if result is not None:
                return result
        # The immutable installed CLI owns profile parsing and ref resolution.
        # The helper itself stays outside the profile the candidate replaces.
        executable = args.profile / "bin/agentbox"
        if args.command == "restore":
            executable = args.state_dir / "baseline/bin/agentbox"
        cli = runpy.run_path(str(executable))
        try:
            return operate(args, cli)
        except (cli["ConfigError"], cli["UpdateError"]) as exc:
            print(f"test-box-update: {exc}", file=sys.stderr)
            return 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"test-box-update: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
