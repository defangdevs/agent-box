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

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
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
def profile_payload_names(webhook=True):
    """Every agent-box binary nix/runtime.nix puts in the profile.

    Read out of the expression rather than listed here: a hand-maintained
    copy of a list that already exists is how the settings page lost its
    Connections section (issue #392) and how `agent-box-session env` was
    promised on a box that could not run it (#394). The fake profile the
    render tests build has to be the real profile's shape, or a test can
    only prove the renderer agrees with the test.
    """
    text = (REPO / "nix" / "runtime.nix").read_text()

    def declared(chunk):
        return set(re.findall(
            r'(?:payload|writePython3Bin|writeShellScriptBin|runCommand)\s+"'
            r'(agent-box[a-z0-9-]*|agentbox)"', chunk)) - {"agent-box-assets"}

    names = declared(text)
    if not webhook:
        # Payloads runtime.nix adds only under `lib.optional(s)
        # webhookEnabled`. Including them unconditionally would let this
        # list vouch for a command a webhook-less box does not install —
        # the list would be right about the file and wrong about the box.
        for chunk in re.split(r"lib\.optionals?\s+webhookEnabled", text)[1:]:
            names -= declared(chunk.split("];")[0].split(");")[0])
    return names


# Third-party binaries the renderer names by path. Not derived: these are
# nixpkgs packages, not payloads, and the profile lists them as attributes
# (`with pkgs; [ tmux ttyd ... ]`) whose binary names are not the attribute
# names in every case.
FAKE_TOOLS = [
    "claude", "codex", "tmux", "ttyd", "caddy", "grep", "find", "flock",
    "hostname", "fail2ban-server", "fail2ban-client", "nft",
]
FAKE_BINS = sorted(profile_payload_names() | set(FAKE_TOOLS))


# What the fake profile's manifest claims it was installed from. The rev is
# deliberately NOT the one tests/native/config.json carries, so the fixture
# shows which of the two the renderer believes (issue #358: the profile is
# the ground truth, the config field is a fallback that can be stale).
FAKE_REPO = "defangdevs/agent-box"
FAKE_REV = "1" * 40


def load_agentbox():
    """Import bin/agentbox as a module, for unit tests of its internals.

    It has no .py suffix (it is a command), so the import machinery needs
    to be told what it is. Its `if __name__ == "__main__"` guard keeps the
    import from running main().
    """
    loader = SourceFileLoader("agentbox_cli", str(AGENTBOX))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def build_fake_profile(root):
    """A stand-in for the Nix runtime profile, with the real shared assets."""
    prof = Path(root) / "profile"
    (prof / "bin").mkdir(parents=True)
    (prof / "manifest.json").write_text(json.dumps({
        "version": 3,
        "elements": {
            "runtime": {
                "active": True,
                "attrPath": "packages.x86_64-linux.runtime",
                "originalUrl": f"github:{FAKE_REPO}",
                "url": f"github:{FAKE_REPO}/{FAKE_REV}",
                "storePaths": [str(prof)],
            },
        },
    }, indent=2) + "\n")
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
    for g in ("default-agents.md", "default-agents-webhook.md",
              "default-agents-host-native.md"):
        shutil.copy(SRC / g, share / "guides" / g)
    (prof / "libexec" / "agent-box").mkdir(parents=True)
    shutil.copy(SRC / "password-helper.py",
                prof / "libexec" / "agent-box" / "password-helper.py")
    return prof


def render(tmp, config):
    """Run `agentbox apply --root`, then normalize the paths that move.

    The profile lives in a temp dir, and so does nothing else — except the
    config path, which agent-box-update.service's ExecStart now carries so
    an update re-applies the same config (and which is tests/native/
    config.json or .yaml here, i.e. a checkout-dependent absolute path).
    Both are replaced with tokens so the fixture is a property of the
    renderer rather than of where the test happened to run.
    """
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
        if path.is_symlink():
            # A link into the profile is as much a rendered decision as a
            # file is (the fail2ban jail's action.d, issue #394), so it is
            # normalized and fixed the same way rather than left as a path
            # that only existed inside one test run.
            target = (os.readlink(path)
                      .replace(str(prof), "@PROFILE@")
                      .replace(str(config), "@CONFIG@"))
            if target != os.readlink(path):
                path.unlink()
                os.symlink(target, path)
            continue
        if path.is_file():
            text = (path.read_text()
                    .replace(str(prof), "@PROFILE@")
                    .replace(str(config), "@CONFIG@"))
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
        if path.is_symlink():
            # Recorded as its own kind of entry: a link that silently
            # became a file (or moved) has to show up in the fixture diff
            # like any other change to what the box gets.
            manifest[path.relative_to(root).as_posix()] = (
                "symlink -> " + os.readlink(path))
        elif path.is_file():
            manifest[path.relative_to(root).as_posix()] = path.read_text()
    return manifest


def tree_modes(root):
    """path -> mode string, read from the render (never from the fixture)."""
    return {
        path.relative_to(root).as_posix(): oct(path.stat().st_mode & 0o7777)
        for path in sorted(Path(root).rglob("*"))
        if path.is_file() and not path.is_symlink()
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

    def test_instances_come_back_after_a_reboot(self):
        """The %i instances must be wanted by a target, not just started.

        They carry no [Install] section, so `systemctl enable` does nothing
        for them: on the live box every instance ran until the reboot and
        then never came back — caddy alone, / answering 502, no terminal and
        no agent session. Enablement therefore has to be a Wants= on the
        target, the same mechanism the NixOS backend uses.
        """
        mu = (FIXTURE / "etc/systemd/system/multi-user.target.d"
                        "/10-agent-box.conf").read_text()
        sk = (FIXTURE / "etc/systemd/system/sockets.target.d"
                        "/10-agent-box.conf").read_text()
        for user in ("agent", "robot"):
            self.assertIn(f"Wants=agent-box@{user}.service", mu)
            self.assertIn(f"Wants=agent-web-terminal@{user}.service", mu)
            self.assertIn(f"Wants=agent-box-settings@{user}.service", mu)
            self.assertIn(f"Wants=agent-box-settings@{user}.socket", sk)
        # Every template instance the renderer starts must also be named in
        # one of the two drop-ins, or it is a unit that does not survive a
        # reboot.
        wanted = set()
        for text in (mu, sk):
            for line in text.splitlines():
                if line.startswith("Wants="):
                    wanted.add(line.split("=", 1)[1].strip())
        for unit in self._rendered_units():
            if "@" in unit:
                self.assertIn(
                    unit, wanted,
                    f"{unit} is started by apply but nothing wants it at "
                    "boot — it would vanish on the first reboot")

    def _rendered_units(self):
        """The unit list `apply` would act on, for this fixture's config."""
        import importlib.util
        spec = importlib.util.spec_from_loader(
            "agentbox", importlib.machinery.SourceFileLoader(
                "agentbox", str(AGENTBOX)))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            spec_obj = mod.Spec(json.loads(CONFIG_JSON.read_text()), prof)
            rend = mod.Renderer(spec_obj, prof, root=Path(tmp) / "o")
            return list(rend.render().units)

    def test_the_shipped_guide_describes_THIS_host(self):
        """The guide is shared; the host it describes is not.

        Both backends install the same default-agents.md and bind
        @HOST_SECTION@ to their own half — NixOS on the module side, a
        distro box with agent-box in a Nix profile here. Shipping the NixOS
        text on a native box is not a cosmetic slip: an agent that believes
        it runs NixOS reaches for nixos-rebuild, reads /etc/nixos, and
        mistakes what it is able to change.
        """
        guide = (FIXTURE / "etc/agent-box-guides/AGENTS.agent.md").read_text()
        self.assertIn("Your host: a distro box", guide)
        self.assertNotIn("NixOS host", guide)
        self.assertNotIn("nixos-rebuild", guide)
        # An unbound token ships as literal "@NAME@" in the text an agent
        # reads — which is exactly how @WEBHOOK_SECTION@ shipped before the
        # native side ever bound it.
        leaked = re.findall(r"@[A-Z][A-Z_]*@", guide)
        self.assertEqual([], leaked, f"unbound guide token(s): {leaked}")
    def test_the_store_has_a_janitor(self):
        """Issue #394: a full root wedges the box — no journal, no profile
        swap, no working agent — and on a native box nobody is around to
        run nix-collect-garbage by hand. The NixOS deployments get this
        from their host config; here the renderer owns it, so assert the
        three pieces exist rather than trusting the byte fixture, which a
        --update would happily regenerate without them.
        """
        units = FIXTURE / "etc/systemd/system"
        timer = (units / "agent-box-nix-gc.timer").read_text()
        self.assertIn("OnCalendar=daily", timer)
        # A box that was off at the scheduled hour must still collect:
        # stop/start and Spot boxes are off a lot.
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)
        service = (units / "agent-box-nix-gc.service").read_text()
        self.assertIn("nix-collect-garbage --delete-older-than 7d", service)
        # nix is NOT in the runtime profile — it comes from the Determinate
        # installer's default profile — and a unit inherits no PATH.
        self.assertIn("/nix/var/nix/profiles/default/bin/nix-collect-garbage",
                      service)
        journald = (FIXTURE / "etc/systemd/journald.conf.d/agent-box.conf")
        self.assertIn("SystemMaxUse=200M", journald.read_text())
        nixconf = (FIXTURE / "etc/nix/nix.custom.conf").read_text()
        self.assertIn("min-free = 1073741824", nixconf)
        self.assertIn("max-free = 5368709120", nixconf)

    def test_a_foreign_nix_custom_conf_is_left_alone(self):
        """/etc/nix/nix.custom.conf is what Determinate tells a HUMAN to
        edit, so it is not ours to overwrite. A box that already has one
        keeps it, and the render says so — a silently skipped setting looks
        exactly like one that was never implemented.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "out"
            (out / "etc/nix").mkdir(parents=True)
            mine = "min-free = 42\n"
            (out / "etc/nix/nix.custom.conf").write_text(mine)
            mod = load_agentbox()
            spec_obj = mod.Spec(json.loads(CONFIG_JSON.read_text()), prof)
            rend = mod.Renderer(spec_obj, prof, root=out)
            tree = rend.render()
            self.assertNotIn(str(out / "etc/nix/nix.custom.conf"), tree.files)
            self.assertEqual(mine,
                             (out / "etc/nix/nix.custom.conf").read_text())
            self.assertTrue(any("nix.custom.conf" in n for n in rend.notes),
                            "skipped the file without saying so")

    def test_a_foreign_action_d_without_nftables_multiport_is_left_alone(self):
        """action.d is normally agentbox's own symlink into the profile. A
        pre-existing real directory there instead is left alone the same
        way — but if it lacks nftables-multiport.conf, banaction =
        nftables-multiport can never fire, so the jail would run and log
        bans it never enforces. Refuse to enable the unit rather than ship
        that, and say why in a note.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "out"
            action_dir = out / "etc/agent-box/fail2ban/action.d"
            action_dir.mkdir(parents=True)
            (action_dir / "some-other-action.conf").write_text("")
            mod = load_agentbox()
            spec_obj = mod.Spec(json.loads(CONFIG_JSON.read_text()), prof)
            rend = mod.Renderer(spec_obj, prof, root=out)
            tree = rend.render()
            self.assertNotIn(mod.FAIL2BAN_UNIT, tree.units)
            self.assertTrue(
                any("nftables-multiport" in n for n in rend.notes),
                "skipped the jail without saying so")

    def test_the_terminal_has_a_jail_in_front_of_it(self):
        """Issue #394: the browser terminal is one password on the open
        internet. The module has always jailed repeated 401s; the native
        backend shipped the fail2ban BINARY in its runtime profile and
        never configured or started it, which is the worst of the three
        states — the tool is there, so nothing looks missing.

        The jail's policy is asserted here; that it PARSES and that its
        regex matches a real Caddy 401 line is the `fail2ban-jail` flake
        check, which needs the real package.
        """
        jail = (FIXTURE / "etc/agent-box/fail2ban/jail.conf").read_text()
        for setting in ("maxretry = 5", "findtime = 10m", "bantime = 1h",
                        "backend = systemd", "[agent-web-auth]",
                        "enabled = true"):
            self.assertIn(setting, jail)
        filt = (FIXTURE / "etc/agent-box/fail2ban/filter.d"
                / "agent-web-auth.conf").read_text()
        self.assertIn("_SYSTEMD_UNIT=caddy.service", filt)
        # One jail, one regex, whichever backend rendered it: the module's
        # failregex and this one must stay the same string.
        module = (REPO / "modules/agent-box.nix.in").read_text()
        regex = [x for x in filt.splitlines() if x.startswith("failregex")]
        self.assertTrue(regex, "no failregex in the shipped filter")
        body = regex[0].split("=", 1)[1].strip()
        self.assertIn(body, module,
                      "the native failregex has drifted from the module's")
        # action.d is a link into the profile rather than 40 copied files.
        link = FIXTURE / "etc/agent-box/fail2ban/action.d"
        self.assertTrue(link.is_symlink(), "action.d is not a symlink")
        self.assertEqual("@PROFILE@/etc/fail2ban/action.d",
                         os.readlink(link))
        unit = (FIXTURE / "etc/systemd/system"
                / "agent-box-fail2ban.service").read_text()
        self.assertIn("-c /etc/agent-box/fail2ban", unit)

    def test_the_guide_promises_only_commands_this_box_has(self):
        """The guide is shipped to every box and names commands by hand.

        `agent-box-session env set` and `agent-box-profile` were promised
        to agents on a box where neither worked: the profile shipped no
        envstore CLI, and the generated wrapper pinned neither, so the
        first verb read ${AGENT_BOX_ENVSTORE_BIN:?} and died (issue #394).
        A promise in the shipped guide is a contract with the agent, so
        check it the same way the rest of the render is checked.
        """
        guide = (FIXTURE / "etc/agent-box-guides/AGENTS.agent.md").read_text()
        promised = set(re.findall(r"`(agent-box-[a-z-]+)", guide))
        self.assertTrue(promised, "no agent-box-* commands found in the guide")
        wrappers = {p.name for p
                    in (FIXTURE / "usr/local/bin").iterdir()}
        # Anything the guide names is either a generated wrapper or a
        # payload the profile installs under its own name.
        config = json.loads(CONFIG_JSON.read_text())
        webhook = bool(config.get("webhook", {}).get("enable", False))
        payloads = profile_payload_names(webhook) | set(FAKE_TOOLS)
        for cmd in sorted(promised):
            self.assertTrue(cmd in wrappers or cmd in payloads,
                            f"the guide promises `{cmd}`, which this box "
                            f"does not have")

    def test_the_session_cli_can_reach_the_env_store(self):
        """session-cli.sh reads AGENT_BOX_ENVSTORE_BIN with ${VAR:?}, so an
        unset value is not a degraded `env` verb — it is an error message
        instead of a secret being stored."""
        wrapper = (FIXTURE / "usr/local/bin/agent-box-session").read_text()
        self.assertIn("AGENT_BOX_ENVSTORE_BIN=", wrapper)
        self.assertIn("AGENT_BOX_PROFILE_BIN=", wrapper)
        profile = (FIXTURE / "usr/local/bin/agent-box-profile").read_text()
        self.assertIn("AGENT_BOX_ENVSTORE_BIN=", profile)

    def test_wrappers_generated_without_web(self):
        """agent-box-session and agent-box-profile must exist even with
        web.enable: false — the shipped guide tells every agent to run
        them regardless, and the module's own agentRuntimePackages carries
        both unconditionally. Their generation used to live inside
        self.caddy(), which a web-disabled box never calls, so a native
        box with web.enable: false shipped neither (review on #403)."""
        data = json.loads(CONFIG_JSON.read_text())
        data["web"] = {"enable": False}
        del data["webhook"]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "no-web.json"
            cfg.write_text(json.dumps(data))
            out = render(tmp, cfg)
            self.assertFalse((out / "etc/agent-box/Caddyfile").exists(),
                             "web.enable: false should render no Caddyfile")
            session = (out / "usr/local/bin/agent-box-session").read_text()
            self.assertIn("AGENT_BOX_ENVSTORE_BIN=", session)
            self.assertIn("AGENT_BOX_PROFILE_BIN=", session)
            profile = (out / "usr/local/bin/agent-box-profile").read_text()
            self.assertIn("AGENT_BOX_ENVSTORE_BIN=", profile)

    def test_units_are_installed_verbatim(self):
        """The %i template units must be the shared asset, byte for byte."""
        for unit in (SRC / "units").iterdir():
            got = FIXTURE / "etc/systemd/system" / unit.name
            if not got.exists():
                continue
            self.assertEqual(unit.read_text(), got.read_text(),
                             f"{unit.name} was rewritten, not installed")

    def test_settings_gets_connect_bins_for_every_agent(self):
        """Issue #392: the settings page hides its whole Connections
        section when AGENT_BOX_CONNECT_BINS is empty, so a renderer that
        forgets it silently costs the box its guided sign-in — which is
        exactly how native boxes shipped without one. Asserted by name,
        not left to the byte fixture: a `--update` regenerates the fixture
        around whatever the renderer currently emits, so the fixture alone
        would ratify the variable's removal.
        """
        conf = (FIXTURE / "etc/systemd/system"
                / "agent-box-settings@agent.service.d" / "10-host.conf")
        line = next((x for x in conf.read_text().splitlines()
                     if "AGENT_BOX_CONNECT_BINS=" in x), "")
        self.assertTrue(line, "no AGENT_BOX_CONNECT_BINS in the settings "
                              "drop-in: the Connections section would be "
                              "hidden on every native box")
        cards = dict(
            pair.split("=", 1)
            for pair in line.split("AGENT_BOX_CONNECT_BINS=", 1)[1]
                            .rstrip('"').split())
        agents = json.loads(CONFIG_JSON.read_text())["agents"]
        # The path matters as much as the id: the daemon execs these
        # verbatim, so an id mapped to a relative or empty path is a card
        # that fails the moment it is clicked.
        want = {a: f"@PROFILE@/bin/{a}" for a in agents}
        want["github"] = "@PROFILE@/bin/gh"
        self.assertEqual(want, cards)


class SelfUpdateRenderTest(unittest.TestCase):
    """What `apply` puts on the box so an update can be triggered (#358)."""

    def setUp(self):
        self.mod = load_agentbox()
        self.unit = (FIXTURE / "etc/systemd/system"
                     / self.mod.UPDATE_UNIT).read_text()

    def test_update_unit_is_rendered_but_never_enabled(self):
        """On-demand only: enabling it would update on every apply.

        `cmd_apply` runs `systemctl enable --now` over Tree.units and
        `start` over the %i instances, so an update unit that appeared in
        either list would fire a fast-forward every single time the host
        configuration is applied — including the apply that an update
        itself runs, which is a loop.
        """
        self.assertIn("[Service]", self.unit)
        self.assertNotIn("[Install]", self.unit)
        self.assertIn("ExecStart=@PROFILE@/bin/agentbox update "
                      "--config @CONFIG@", self.unit)
        units = RenderTest()._rendered_units()
        self.assertNotIn(self.mod.UPDATE_UNIT, units)
        for target in ("multi-user", "sockets"):
            drop = (FIXTURE / "etc/systemd/system"
                    / f"{target}.target.d" / "10-agent-box.conf").read_text()
            self.assertNotIn(self.mod.UPDATE_UNIT, drop)

    def test_the_trigger_is_spelled_identically_everywhere(self):
        """Three places have to agree on the command, or sudo prompts.

        The sudoers grant matches on the exact command line, so the
        settings page's Update button and the command the shipped guide
        tells the agent to run must both be that same string. When they
        drifted on the NixOS side the symptom was not an error but a
        password prompt no agent can answer (issue #353).
        """
        trigger = self.mod.UPDATE_TRIGGER
        sudoers = (FIXTURE / "etc/sudoers.d/agent-box").read_text()
        guide = (FIXTURE / "etc/agent-box-guides/AGENTS.agent.md").read_text()
        for user in ("agent", "robot"):
            line = [x for x in sudoers.splitlines()
                    if x.startswith(f"{user} ALL=")]
            self.assertTrue(line, f"no sudoers line for {user}")
            self.assertIn(trigger, line[0])
        settings = (FIXTURE / "etc/systemd/system"
                    / "agent-box-settings@agent.service.d"
                    / "10-host.conf").read_text()
        self.assertIn(f'Environment="AGENT_BOX_UPDATE_CMD=/usr/bin/sudo -n '
                      f'{trigger}"', settings)
        self.assertIn(
            f'Environment="AGENT_BOX_UPDATE_UNIT={self.mod.UPDATE_UNIT}"',
            settings)
        self.assertIn("## Updating", guide)
        self.assertIn(trigger, guide)

    def test_repo_and_rev_come_from_the_profile_not_the_config(self):
        """The profile records what is installed; the config can be stale.

        tests/native/config.json still carries the placeholder `repo:`/
        `rev:` pair, and the fake profile's manifest disagrees with it —
        the manifest has to win, or a native box goes on advertising a rev
        of forty zeroes on its settings page (issue #358's second open
        question).
        """
        settings = (FIXTURE / "etc/systemd/system"
                    / "agent-box-settings@agent.service.d"
                    / "10-host.conf").read_text()
        config = json.loads(CONFIG_JSON.read_text())
        self.assertEqual(config["rev"], "0" * 40)
        self.assertIn(f'Environment="AGENT_BOX_REV={FAKE_REV}"', settings)
        self.assertIn(f'Environment="AGENT_BOX_REPO={FAKE_REPO}"', settings)


class SelfUpdateLogicTest(unittest.TestCase):
    """The refusals and the verification, without a network or a nix."""

    def setUp(self):
        self.mod = load_agentbox()
        self.calls = []
        self.mod.run = self._record

    @contextlib.contextmanager
    def quiet(self):
        """An update narrates itself to the journal; a test need not.

        Without this the progress lines are the last thing in the build
        log, which is exactly what the flake check shows on a failure.
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yield buf

    def _record(self, cmd, check=True, capture=False):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _api(self, mapping):
        def call(url, timeout=30):
            for fragment, payload in mapping.items():
                if fragment in url:
                    return payload
            raise AssertionError(f"unexpected API call: {url}")
        self.mod.github_json = call

    def _args(self, prof, **over):
        import argparse
        base = dict(profile=str(prof), post_switch=False, repo=None, rev=None,
                    force=False, check=False, config="/etc/agent-box/x.json",
                    no_restart_sessions=False, from_generation=None,
                    from_rev=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_flake_url_parsing(self):
        parse = self.mod.parse_flake_url
        self.assertEqual(parse(f"github:{FAKE_REPO}/{FAKE_REV}"),
                         (FAKE_REPO, FAKE_REV))
        # An unlocked entry: a repo, but no rev to compare against.
        self.assertEqual(parse("github:defangdevs/agent-box"),
                         ("defangdevs/agent-box", None))
        # A branch is a ref, not a rev — it must not be mistaken for one.
        self.assertEqual(parse("github:defangdevs/agent-box/master"),
                         ("defangdevs/agent-box", None))
        self.assertEqual(parse("path:/etc/agent-box"), (None, None))
        self.assertEqual(parse(None), (None, None))

    def test_manifest_element_handles_both_nix_shapes(self):
        """nix >= 2.20 keys elements by name; older nix uses a list."""
        element = {"attrPath": "packages.x86_64-linux.runtime",
                   "url": f"github:{FAKE_REPO}/{FAKE_REV}"}
        name, got = self.mod.manifest_element(
            {"version": 3, "elements": {"runtime": element}})
        self.assertEqual((name, got), ("runtime", element))
        name, got = self.mod.manifest_element(
            {"version": 2, "elements": [element]})
        self.assertEqual((name, got), ("0", element),
                         "an indexed element must yield the index `nix "
                         "profile remove` takes")
        self.assertEqual(self.mod.manifest_element({}), (None, None))

    def test_already_current_is_not_an_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            self._api({"/commits/": {"sha": FAKE_REV}})
            with self.quiet():
                self.assertEqual(self.mod.cmd_update(self._args(prof)), 0)
        self.assertEqual(self.calls, [],
                         "an already-current box must not touch its profile")

    def test_non_fast_forward_is_refused(self):
        """The one part of the NixOS payload worth porting verbatim.

        A target that is not strictly ahead means rewritten history or a
        replay of an older, possibly vulnerable rev.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            self._api({"/commits/": {"sha": "2" * 40},
                       "/compare/": {"status": "behind"}})
            with self.quiet(), self.assertRaises(self.mod.UpdateError) as cm:
                self.mod.cmd_update(self._args(prof))
        self.assertIn("fast-forward", str(cm.exception))
        self.assertEqual(self.calls, [],
                         "a refused update must not touch the profile")

    def test_force_skips_the_check_but_check_mode_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            self._api({"/commits/": {"sha": "2" * 40},
                       "/compare/": {"status": "ahead"}})
            with self.quiet():
                self.assertEqual(
                    self.mod.cmd_update(self._args(prof, check=True)), 0)
            self.assertEqual(self.calls, [])
            # --force reaches the switch without asking about ancestry at
            # all: the compare endpoint is never called (it would raise).
            self._api({"/commits/": {"sha": "2" * 40}})
            with self.quiet(), self.assertRaises(self.mod.UpdateError):
                self.mod.cmd_update(self._args(prof, force=True))
            self.assertTrue(any("build" in c for c in self.calls))

    def test_a_no_op_install_is_reported_as_a_failure(self):
        """"Still on the old rev" must never be reported as success.

        Installing over an existing entry can exit non-fatally and leave
        the old profile in place; the following `apply` then says "0
        change(s)" and the box looks updated while running the previous
        release (observed by hand on a live native box, issue #358). The
        profile is therefore re-read afterwards and disagreement is fatal.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)   # manifest keeps saying FAKE_REV
            self._api({"/commits/": {"sha": "2" * 40},
                       "/compare/": {"status": "ahead"}})
            with self.quiet(), self.assertRaises(self.mod.UpdateError) as cm:
                self.mod.cmd_update(self._args(prof))
        self.assertIn("refusing to call that an update", str(cm.exception))
        installs = [c for c in self.calls if "install" in c]
        removes = [c for c in self.calls if "remove" in c]
        self.assertTrue(removes and installs,
                        "the switch is remove-then-install, not a bare "
                        "install")
        self.assertLess(self.calls.index(removes[0]),
                        self.calls.index(installs[0]))

    def test_a_failed_build_is_reported_and_leaves_the_profile_alone(self):
        """`main` catches ConfigError and UpdateError — nothing else.

        A substitution or evaluation failure is the likeliest way an update
        stops, and it must read as a reason in the journal rather than a
        traceback. The profile is untouched at that point, so the "services
        restart" notice must not have been sent yet either.
        """
        def fail_on_build(cmd, check=True, capture=False):
            self.calls.append(list(cmd))
            if "build" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.mod.run = fail_on_build
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            self._api({"/commits/": {"sha": "2" * 40},
                       "/compare/": {"status": "ahead"}})
            with self.quiet() as out, \
                    self.assertRaises(self.mod.UpdateError) as cm:
                self.mod.cmd_update(self._args(prof))
        self.assertIn("failed", str(cm.exception))
        self.assertNotIn("services restart", out.getvalue())
        for call in self.calls:
            self.assertNotIn("remove", call)
            self.assertNotIn("install", call)

    def test_restart_covers_every_daemon_the_profile_swap_invalidated(self):
        """apply reports no change after a swap, so this list is the fix.

        Unit text names <profile>/bin/..., a path that does not move, so
        nothing looks changed while every daemon still runs the old code.
        What is live is read from systemd, not derived from the config —
        the recovery path has to work when the config is the very thing the
        new release choked on.
        """
        live = {
            "agent-web-auth-secrets.service": ["agent-web-auth-secrets.service"],
            "agent-web-terminal@*.service": ["agent-web-terminal@agent.service"],
            "agent-box-settings@*.socket": ["agent-box-settings@agent.socket"],
            "agent-box-settings@*.service": ["agent-box-settings@agent.service"],
            "agent-box-webhook@*.socket": ["agent-box-webhook@agent.socket"],
            # Deliberately stopped by the operator: not listed, not restarted.
            "agent-box-webhook@*.service": [],
            "caddy.service": ["caddy.service"],
            "agent-box@*.service": ["agent-box@agent.service",
                                    "agent-box@robot.service"],
        }
        self.mod.active_units = lambda pattern: live[pattern]
        with self.quiet():
            restarted = self.mod.restart_units()
        self.assertNotIn("agent-box-webhook@agent.service", restarted)
        for unit in ("agent-web-auth-secrets.service",
                     "agent-web-terminal@agent.service",
                     "agent-box-settings@agent.socket",
                     "agent-box-webhook@agent.socket",
                     "caddy.service"):
            self.assertIn(unit, restarted)
        # Sessions last: the agent that asked for the update is in one.
        self.assertEqual(
            restarted[-2:],
            ["agent-box@agent.service", "agent-box@robot.service"])
        with self.quiet():
            skipped = self.mod.restart_units(restart_sessions=False)
        self.assertNotIn("agent-box@agent.service", skipped)

    def test_a_profile_with_no_generation_still_recovers(self):
        """`--from-generation None` must not reach phase two.

        profile_generation() is None when the profile path is not a
        generation symlink. Passing str(None) would make phase two's
        rollback raise ValueError inside the except handler that exists to
        recover the box — no rollback, no re-apply, no wall notice, and a
        traceback where an operator wanted a working box.

        The flag is built at the very end of phase one, so the switch has
        to VERIFY for the assertion to see anything: a fake install that
        lands the target rev gets us there, and the handover is captured
        from subprocess.run, which is what phase one starts the child with.
        """
        target = "2" * 40
        child = []
        real_run = subprocess.run
        self.addCleanup(setattr, subprocess, "run", real_run)
        subprocess.run = lambda cmd, *a, **k: (
            child.extend(cmd) or subprocess.CompletedProcess(cmd, 0))
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)          # a plain dir, no symlink
            self.assertIsNone(self.mod.profile_generation(prof))
            self._api({"/commits/": {"sha": target},
                       "/compare/": {"status": "ahead"}})

            def install(cmd, check=True, capture=False):
                self.calls.append(list(cmd))
                if "install" in cmd:
                    manifest = prof / "manifest.json"
                    data = json.loads(manifest.read_text())
                    data["elements"]["runtime"]["url"] = (
                        f"github:{FAKE_REPO}/{target}")
                    manifest.write_text(json.dumps(data))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            self.mod.run = install
            with self.quiet():
                self.assertEqual(self.mod.cmd_update(self._args(prof)), 0)
        self.assertTrue(child, "phase one never handed over to phase two")
        self.assertIn("--post-switch", child)
        self.assertNotIn("--from-generation", child)
        for arg in child:
            self.assertNotEqual(arg, "None")
        for call in self.calls:
            self.assertNotIn("None", call)

    def test_github_failures_are_reported_not_raised(self):
        """A rate limit is 60/hour per IP, so the journal must read well.

        `main` catches ConfigError and UpdateError; anything else is a
        traceback in the journal where an operator wanted a reason.
        """
        import urllib.error
        import urllib.request

        def boom(req, timeout=30):
            raise urllib.error.HTTPError(
                req.full_url, 403, "rate limit exceeded", {}, None)

        real = urllib.request.urlopen
        urllib.request.urlopen = boom
        self.addCleanup(setattr, urllib.request, "urlopen", real)
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            with self.quiet(), self.assertRaises(self.mod.UpdateError) as cm:
                self.mod.cmd_update(self._args(prof))
        self.assertIn("GitHub request failed", str(cm.exception))
        self.assertEqual(self.calls, [],
                         "a failed lookup must not touch the profile")


def update_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        out = render(tmp, CONFIG_JSON)
        modes = tree_modes(out)
        if FIXTURE.exists():
            shutil.rmtree(FIXTURE)
        shutil.copytree(out, FIXTURE, symlinks=True)
    MODES.write_text(json.dumps(modes, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE} and {MODES.name} — review the diff and commit")


if __name__ == "__main__":
    if "--update" in sys.argv:
        update_fixture()
    else:
        unittest.main()
