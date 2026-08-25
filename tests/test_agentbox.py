#!/usr/bin/env python3
"""Render tests for bin/agentbox — the native backend (issue #154 Phase 4).

`agentbox apply --root DIR` renders the whole host state into a tree without
touching the host, which makes the native backend testable the same way the
NixOS one is: a committed fixture (tests/native/expected/) is the reviewable
record of what a box actually gets, and any change to it shows up as a diff
in the pull request rather than on a live box.

Hermetic on purpose: the shared assets come from a fake profile built out of
modules/src (the same files the real profile ships), and the profile path is
normalized to @PROFILE@, so this runs under a plain python3 with no Nix and
no network. Regenerate the fixture after an intended change with:

    python3 tests/test_agentbox.py --update
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTBOX = REPO / "bin" / "agentbox"
FIXTURE = REPO / "tests" / "native" / "expected"
CONFIG_JSON = REPO / "tests" / "native" / "config.json"
CONFIG_YAML = REPO / "tests" / "native" / "config.yaml"
# File modes are recorded as data, not as the fixture's own permissions: the
# fixture reaches CI through the Nix store, which normalizes every file to
# 0444, so an on-disk mode could not survive the trip.
MODES = REPO / "tests" / "native" / "expected-modes.json"
SRC = REPO / "modules" / "src"

# Binaries the renderer names in units, wrappers and env files. Present as
# empty executables in the fake profile so a rendered path is real.
FAKE_BINS = [
    "agent-box-supervisor", "agent-box-attach", "agent-box-settings",
    "agent-box-mark-stopped", "agent-box-env-exec",
    "agent-box-codex-remote-control", "agent-box-webhook-receiver",
    "agent-box-webhook-spawn", "agent-box-session-bare",
    "agent-box-webhook-bare", "claude", "codex", "tmux", "ttyd", "caddy",
    "grep", "find", "flock", "hostname",
]


def build_fake_profile(root):
    """A stand-in for the Nix runtime profile, with the real shared assets."""
    prof = Path(root) / "profile"
    (prof / "bin").mkdir(parents=True)
    for b in FAKE_BINS:
        p = prof / "bin" / b
        p.write_text("#!/bin/sh\n:\n")
        os.chmod(p, 0o755)
    share = prof / "share" / "agent-box"
    (share / "units").mkdir(parents=True)
    for u in (SRC / "units").iterdir():
        shutil.copy(u, share / "units" / u.name)
    (share / "caddy").mkdir()
    for c in SRC.glob("caddyfile-*.caddy"):
        shutil.copy(c, share / "caddy" / c.name)
    (share / "guides").mkdir()
    for g in ("default-agents.md", "default-agents-webhook.md"):
        shutil.copy(SRC / g, share / "guides" / g)
    (prof / "libexec" / "agent-box").mkdir(parents=True)
    shutil.copy(SRC / "password-helper.py",
                prof / "libexec" / "agent-box" / "password-helper.py")
    return prof


def render(tmp, config):
    """Run `agentbox apply --root`, then normalize the profile path away."""
    prof = build_fake_profile(tmp)
    out = Path(tmp) / "out"
    proc = subprocess.run(
        [sys.executable, str(AGENTBOX), "apply", "--config", str(config),
         "--profile", str(prof), "--root", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"agentbox apply failed ({proc.returncode}):\n{proc.stderr}")
    for path in sorted(out.rglob("*")):
        if path.is_file():
            text = path.read_text().replace(str(prof), "@PROFILE@")
            # Read-only assets (the verbatim units, 0444) have to be made
            # writable for the normalization pass, then set back — the mode
            # is part of what the fixture locks.
            mode = path.stat().st_mode & 0o7777
            os.chmod(path, 0o644)
            path.write_text(text)
            os.chmod(path, mode)
    return out


def tree_manifest(root):
    """path -> text."""
    manifest = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = path.read_text()
    return manifest


def tree_modes(root):
    """path -> mode string, read from the render (never from the fixture)."""
    return {
        path.relative_to(root).as_posix(): oct(path.stat().st_mode & 0o7777)
        for path in sorted(Path(root).rglob("*")) if path.is_file()
    }


class RenderTest(unittest.TestCase):
    def test_matches_committed_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = render(tmp, CONFIG_JSON)
            got = tree_manifest(out)
            modes = tree_modes(out)
        want = tree_manifest(FIXTURE)
        self.assertEqual(json.loads(MODES.read_text()), modes,
                         "a rendered file's mode changed — regenerate with "
                         "`python3 tests/test_agentbox.py --update`")
        self.assertEqual(
            sorted(want), sorted(got),
            "the set of rendered files changed — regenerate with "
            "`python3 tests/test_agentbox.py --update` and review the diff")
        for rel in sorted(want):
            self.assertEqual(want[rel], got[rel], f"{rel} changed")

    def test_yaml_and_json_agree(self):
        """The two config dialects must describe the same box."""
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not importable")
        with tempfile.TemporaryDirectory() as tmp:
            from_json = tree_manifest(render(tmp, CONFIG_JSON))
        with tempfile.TemporaryDirectory() as tmp:
            from_yaml = tree_manifest(render(tmp, CONFIG_YAML))
        self.assertEqual(from_json, from_yaml)

    def test_apply_is_idempotent(self):
        """A second apply over the same tree must report no changes."""
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "out"
            args = [sys.executable, str(AGENTBOX), "apply",
                    "--config", str(CONFIG_JSON), "--profile", str(prof),
                    "--root", str(out)]
            subprocess.run(args, check=True, capture_output=True, text=True)
            second = subprocess.run(args + ["--dry-run"], check=True,
                                    capture_output=True, text=True)
            self.assertNotIn("would write", second.stdout,
                             "a re-apply wanted to rewrite files:\n"
                             + second.stdout)

    def test_rejects_bad_config(self):
        cases = [
            ({"users": {}}, "at least one user"),
            ({"agents": ["claude"], "defaultAgent": "codex",
              "users": {"a": {}}}, "not in agents"),
            ({"users": {"a": {"root": True}, "b": {"root": True}}},
             "at most one user"),
            ({"users": {"Bad Name": {}}}, "invalid user name"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for i, (data, expect) in enumerate(cases):
                cfg = Path(tmp) / f"bad{i}.json"
                cfg.write_text(json.dumps(data))
                proc = subprocess.run(
                    [sys.executable, str(AGENTBOX), "apply", "--config",
                     str(cfg), "--profile", str(prof), "--root",
                     str(Path(tmp) / f"o{i}")],
                    capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn(expect, proc.stderr)

    def test_caddy_cannot_start_without_the_secrets_oneshot(self):
        """Reboot safety, locked as a property of the rendered units.

        /run/agent-box-web/env is on tmpfs and caddy.service reads it with an
        unprefixed EnvironmentFile=, so something has to rewrite it on every
        boot. Before agent-web-auth-secrets.service existed, only
        `apply --first-boot` did — and cloud-init runs that once, so the
        first reboot left caddy in an auto-restart loop and the box with no
        terminal, settings page or downloads. Verified on live hardware.
        """
        caddy = (FIXTURE / "etc/systemd/system/caddy.service").read_text()
        oneshot = FIXTURE / "etc/systemd/system/agent-web-auth-secrets.service"
        self.assertIn("EnvironmentFile=/run/agent-box-web/env", caddy)
        self.assertTrue(
            oneshot.exists(),
            "caddy.service requires /run/agent-box-web/env, which is on "
            "tmpfs — something must rebuild it every boot")
        unit = oneshot.read_text()
        self.assertIn("RequiredBy=caddy.service", unit)
        self.assertIn("Before=caddy.service", unit)
        self.assertIn("web-secrets", unit)

    def test_web_secrets_rebuilds_the_env_file(self):
        """`agentbox web-secrets` reprojects /run from what persisted.

        Run against a fake root: the persistent inputs are a hash file and
        (optionally) a cookie secret, and the output has to be the three
        keys the Caddyfile binds, with the algorithm sniffed from the hash
        rather than assumed.
        """
        import importlib.util
        spec = importlib.util.spec_from_loader(
            "agentbox", importlib.machinery.SourceFileLoader(
                "agentbox", str(AGENTBOX)))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mod.AUTH_ENV_DIR = str(root / "run")
            mod.AUTH_ENV_FILE = str(root / "run" / "env")
            mod.COOKIE_DIR = str(root / "var")
            argon = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
            bcrypt = "$2a$14$abcdefghijklmnopqrstuv"
            for name, hashed in (("agent", argon), ("web-two", bcrypt)):
                (root / f"{name}.hash").write_text(hashed + "\n")
            users = [(n, str(root / f"{n}.hash"))
                     for n in ("agent", "web-two")]
            mod.write_auth_env(users)
            env = dict(
                line.split("=", 1)
                for line in Path(mod.AUTH_ENV_FILE).read_text().splitlines())
            self.assertEqual(env["WEB_PASSWORD_HASH_AGENT"], argon)
            self.assertEqual(env["WEB_PASSWORD_ALGORITHM_AGENT"], "argon2id")
            # "-" is not legal in an env var name; it becomes "_".
            self.assertEqual(env["WEB_PASSWORD_ALGORITHM_WEB_TWO"], "bcrypt")
            self.assertEqual(len(env["WEB_COOKIE_SECRET_AGENT"]), 64)
            self.assertEqual(oct(Path(mod.AUTH_ENV_FILE).stat().st_mode
                                 & 0o777), "0o640")

            # A cookie secret is minted once and then kept: rotating it on
            # every boot would sign every viewer out at each reboot.
            first = env["WEB_COOKIE_SECRET_AGENT"]
            mod.write_auth_env(users)
            again = dict(
                line.split("=", 1)
                for line in Path(mod.AUTH_ENV_FILE).read_text().splitlines())
            self.assertEqual(first, again["WEB_COOKIE_SECRET_AGENT"])

            # An unrecognized hash is refused, not written out as if caddy
            # could use it.
            (root / "agent.hash").write_text("plaintext-oops\n")
            with self.assertRaises(mod.ConfigError):
                mod.write_auth_env(users)

    def test_units_are_installed_verbatim(self):
        """The %i template units must be the shared asset, byte for byte."""
        for unit in (SRC / "units").iterdir():
            got = FIXTURE / "etc/systemd/system" / unit.name
            if not got.exists():
                continue
            self.assertEqual(unit.read_text(), got.read_text(),
                             f"{unit.name} was rewritten, not installed")


def update_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        out = render(tmp, CONFIG_JSON)
        modes = tree_modes(out)
        if FIXTURE.exists():
            shutil.rmtree(FIXTURE)
        shutil.copytree(out, FIXTURE)
    MODES.write_text(json.dumps(modes, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE} and {MODES.name} — review the diff and commit")


if __name__ == "__main__":
    if "--update" in sys.argv:
        update_fixture()
    else:
        unittest.main()
