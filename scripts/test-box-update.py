#!/usr/bin/env python3
"""Install an unmerged ref on a native test box, then restore its baseline.

Operator-only: run as root, outside an agent session, from a trusted copy.
Uses the installed updater; adds no service, sudo grant or settings control.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys


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


def operate(args, cli):
    """Keep the recovery record until restoration has actually succeeded."""
    profile = args.profile.absolute()
    state_dir = args.state_dir.absolute()
    cli_path = profile / "bin/agentbox"
    record = state_dir / "trial.json"
    baseline = state_dir / "baseline"

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

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state_dir.lstat()
    if state_dir.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("state directory must be owned by root with mode 0700")
    with (state_dir / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "install":
            if record.exists():
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
            with temporary.open("w") as stream:
                json.dump(saved, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(record)
            directory_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            executable, revision = cli_path, target
        else:
            if not record.exists():
                raise ValueError("no trial is recorded")
            saved = json.loads(record.read_text())
            if saved["profile"] != str(profile):
                raise ValueError("recorded trial belongs to another profile")
            if args.command == "status":
                print(json.dumps(dict(saved, current=current), indent=2))
                return 0
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
            record.unlink()
            baseline.unlink()
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
        # The immutable installed CLI owns profile parsing and ref resolution.
        # The helper itself stays outside the profile the candidate replaces.
        executable = args.profile / "bin/agentbox"
        if args.command in ("restore", "status"):
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
