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
from unittest import mock

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
    # The pinned local-webhook the profile ships (issue #425), and the pin it
    # came from. A box needs no webhook config because these are here — so a
    # fake profile without them would test the pre-#425 world.
    (share / "local-webhook").mkdir()
    (share / "local-webhook" / "webhook.py").write_text(
        "#!/usr/bin/env python3\n# stand-in for the pinned local-webhook\n")
    (share / "local-webhook" / "pin.json").write_text(json.dumps({
        "repo": "defangdevs/local-channels",
        "rev": "f" * 40,
        "sha256": "sha256-" + "A" * 43 + "=",
    }) + "\n")
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

    def test_first_boot_carries_a_derived_label_to_the_resolved_domain(self):
        """A derived host label must move with an auto-resolved domain.

        host_label defaults from domain at spec-build time, but first_boot
        resolves an "auto" domain AFTER the spec is built. Leaving the label
        frozen ships the placeholder into the pointer seeded once into $HOME
        (~/AGENTS.md), which then reads "agent@auto" forever — the /etc
        pointer is re-rendered on the next apply, but the supervisor seeds
        the $HOME copy exactly once.
        """
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(
                {"domain": "auto", "users": {"agent": {}}}))
            spec = mod.Spec(json.loads(cfg.read_text()), prof)
            self.assertEqual("auto", spec.host_label)  # frozen at build time
            args = type("A", (), {"settle_delay": 0, "config": str(cfg)})()
            orig = mod.settle_public_ip
            mod.settle_public_ip = lambda **k: "203.0.113.7"
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    mod.first_boot(spec, args)
            finally:
                mod.settle_public_ip = orig
            self.assertEqual("203-0-113-7.sslip.io", spec.domain)
            self.assertEqual(spec.domain, spec.host_label,
                             "a derived label must move with the domain")
            self.assertEqual("203-0-113-7.sslip.io",
                             json.loads(cfg.read_text())["domain"])

    def test_first_boot_leaves_an_explicit_label_alone(self):
        """A hostLabel pinned in config is not a placeholder — keep it."""
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(
                {"domain": "auto", "hostLabel": "my-box",
                 "users": {"agent": {}}}))
            spec = mod.Spec(json.loads(cfg.read_text()), prof)
            args = type("A", (), {"settle_delay": 0, "config": str(cfg)})()
            orig = mod.settle_public_ip
            mod.settle_public_ip = lambda **k: "203.0.113.7"
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    mod.first_boot(spec, args)
            finally:
                mod.settle_public_ip = orig
            self.assertEqual("my-box", spec.host_label)
            self.assertEqual("203-0-113-7.sslip.io", spec.domain)

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

    def test_webhooks_need_no_config_at_all(self):
        """A box with no `webhook:` key still gets webhooks (issue #425).

        The Lightsail template writes exactly such a config, and
        webhook_enable used to be `bool(repo and script)` — both
        hand-declared — so every box it launched had the receiver, the
        Caddy route, the CLI and the whole settings panel silently absent,
        while the NixOS template (webhook.enable default true) had them.

        The profile now ships the pinned script and its pin, so both values
        have a source. What must NOT come back is a path from the config
        that nothing installs: the rendered script is the profile's own.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "bare.json"
            cfg.write_text(json.dumps({
                "domain": "bare.example.org",
                "agents": ["claude"],
                "web": {"enable": True},
                "users": {"agent": {"root": True}},
            }) + "\n")
            out = render(tmp, cfg)
            units = out / "etc" / "systemd" / "system"
            assert (units / "agent-box-webhook@agent.service.d").is_dir(), \
                "no receiver drop-in: webhooks off on a config that says nothing"
            settings = (units / "agent-box-settings@agent.service.d"
                        / "10-host.conf").read_text()
            want = ('Environment="AGENT_BOX_WEBHOOK_SCRIPT=@PROFILE@'
                    '/share/agent-box/local-webhook/webhook.py"')
            assert want in settings, settings
            env = (out / "etc" / "agent-box" / "units"
                   / "agent-box-settings-agent.env").read_text()
            assert "AGENT_BOX_WEBHOOK_STATE_DIR=" in env, env
            # The marketplace repo the supervisor pins claude's plugin to
            # comes from the pin beside the script, not from a config key.
            agent_unit = (units / "agent-box@agent.service.d"
                          / "10-host.conf").read_text()
            assert ("AGENT_BOX_WEBHOOK_REPO=defangdevs/local-channels"
                    in agent_unit), agent_unit

    def test_webhooks_can_still_be_turned_off(self):
        """`webhook.enable = false` is the opt-out, matching the module."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "off.json"
            cfg.write_text(json.dumps({
                "domain": "off.example.org",
                "agents": ["claude"],
                "web": {"enable": True},
                "webhook": {"enable": False},
                "users": {"agent": {"root": True}},
            }) + "\n")
            out = render(tmp, cfg)
            units = out / "etc" / "systemd" / "system"
            assert not (units / "agent-box-webhook@agent.service.d").exists(), \
                "webhook.enable = false still rendered the receiver"
            settings = (units / "agent-box-settings@agent.service.d"
                        / "10-host.conf").read_text()
            assert "AGENT_BOX_WEBHOOK_SCRIPT" not in settings, settings

    def test_web_off_takes_webhooks_with_it(self):
        """web.enable = false must gate webhook_enable too (CodeRabbit
        finding on PR #431): the receiver, CLI and settings panel are all
        native-web features, so `_webhook_enable` alone is not enough."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "noweb.json"
            cfg.write_text(json.dumps({
                "domain": "noweb.example.org",
                "agents": ["claude"],
                "web": {"enable": False},
                "users": {"agent": {"root": True}},
            }) + "\n")
            out = render(tmp, cfg)
            units = out / "etc" / "systemd" / "system"
            assert not (units / "agent-box-webhook@agent.service.d").exists(), \
                "web.enable = false still rendered the webhook receiver"

    def test_a_quoted_false_does_not_turn_webhooks_loose(self):
        """`bool("false")` is `True` — same trap as codexFullAccess
        (issue #404 review), now caught for webhook.enable too
        (CodeRabbit finding on PR #431). null is kept meaning off."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in ("false", "true", 0, 1, [], {}):
                config["webhook"] = {"enable": bad}
                with self.assertRaises(mod.ConfigError):
                    mod.Spec(config, prof)
            for ok in (True, False, None):
                config["webhook"] = {"enable": ok}
                mod.Spec(config, prof)  # must not raise
            config["webhook"] = {"enable": None}
            spec = mod.Spec(config, prof)
            self.assertFalse(spec.webhook_enable)

    def test_a_quoted_false_does_not_turn_the_web_front_door_loose(self):
        """Same trap, same fix, for `web.enable` (CodeRabbit finding on PR
        #431): it now gates webhook_enable too, so a stringly-typed
        "false" must not slip past bool() and enable both."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in ("false", "true", 0, 1, [], {}):
                config["web"] = {"enable": bad}
                with self.assertRaises(mod.ConfigError):
                    mod.Spec(config, prof)
            for ok in (True, False, None):
                config["web"] = {"enable": ok}
                mod.Spec(config, prof)  # must not raise
            config["web"] = {"enable": None}
            spec = mod.Spec(config, prof)
            self.assertFalse(spec.web_enable)
            self.assertFalse(spec.webhook_enable)

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

    def test_the_base_os_patches_itself_without_a_reboot(self):
        """The distro still owns its packages, but nobody here can answer
        the two questions an unattended patch run asks: may I restart this
        service, and may I reboot? Both answers are rendered, because
        leaving them to the defaults is the silent failure — an
        interactive needrestart run non-interactively (which is how
        unattended-upgrades runs it) falls back to list-only, so the patch
        lands and the vulnerable process keeps running until a reboot
        this box has no way to perform.
        """
        apt = (FIXTURE / "etc/apt/apt.conf.d/52-agent-box-unattended"
               ).read_text()
        # A box whose periodic jobs were never turned on patches nothing
        # and looks exactly like one that is up to date.
        self.assertIn('APT::Periodic::Update-Package-Lists "1";', apt)
        self.assertIn('APT::Periodic::Unattended-Upgrade "1";', apt)
        self.assertIn('Unattended-Upgrade::Automatic-Reboot "false";', apt)
        # Which pockets count as security pockets is the distro's call,
        # and an apt list assignment APPENDS — restating it here could
        # only duplicate the distro's list or, with a #clear, narrow it.
        self.assertNotIn("Allowed-Origins", apt)
        # `#` is a comment in apt.conf too (only #include and #clear are
        # directives), so the file keeps the header that makes it ours to
        # remove again.
        self.assertTrue(apt.startswith("# Generated by `agentbox apply`"))

        nr = (FIXTURE / "etc/needrestart/conf.d/50-agent-box.conf").read_text()
        self.assertIn("$nrconf{restart} = 'a';", nr)
        # Merged into needrestart's hash, never assigned over it: conf.d
        # is eval'd after the shipped config, so `$nrconf{override_rc} =
        # {...}` here would drop its own exclusions (dbus, the display
        # managers, the network stack) on the way past.
        self.assertIn("$nrconf{override_rc}->{qr(^agent-box@)} = 0;", nr)
        self.assertNotIn("$nrconf{override_rc} =", nr)

    def test_an_apt_run_can_never_kill_a_session(self):
        """The one unit automatic restarts must not touch.

        `agent-box@%i.service` stops with `tmux -L agent-box kill-server`,
        so needrestart deciding it is outdated because libc moved is every
        session on the box dying mid-task at whatever hour apt woke up.
        The exclusion is asserted against the unit's OWN ExecStop, so a
        future unit that stops some gentler way still has to come past
        this test.
        """
        unit = (FIXTURE / "etc/systemd/system/agent-box@.service").read_text()
        self.assertIn("ExecStop=tmux -L agent-box kill-server", unit)
        nr = (FIXTURE / "etc/needrestart/conf.d/50-agent-box.conf").read_text()
        self.assertIn("qr(^agent-box@)", nr)

    def test_the_guide_says_a_pending_reboot_is_not_the_agents_to_take(self):
        """A kernel patch only takes effect at a boot, and this box does
        not reboot itself. The guide has to name the file that says one is
        waiting AND say who can act on it — an agent that reads "reboot
        required" with no such grant otherwise goes looking for a way.
        """
        guide = (FIXTURE / "etc/agent-box-guides/AGENTS.agent.md").read_text()
        self.assertIn("/var/run/reboot-required", guide)
        self.assertIn("52-agent-box-unattended", guide)

    def test_a_reboot_time_is_the_only_way_to_get_an_automatic_reboot(self):
        """`automaticReboot: true` is unattended-upgrades' "now" — a
        reboot the moment a kernel patch lands, at an hour nobody chose.
        A deployment that wants the trade has to name the hour.
        """
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in (True, "now", "3:00", "25:00", "03:60"):
                config["osUpdates"] = {"automaticReboot": bad}
                with self.assertRaises(mod.ConfigError, msg=repr(bad)):
                    mod.Spec(config, prof)
            config["osUpdates"] = {"automaticReboot": "03:00"}
            spec = mod.Spec(config, prof)
            out = Path(tmp) / "rebooting"
            mod.Renderer(spec, prof, root=out).render()
            tree = mod.Renderer(spec, prof, root=out).render()
            apt = tree.files[str(out / "etc/apt/apt.conf.d"
                                 / "52-agent-box-unattended")][0]
            self.assertIn('Unattended-Upgrade::Automatic-Reboot "true";', apt)
            self.assertIn('Unattended-Upgrade::Automatic-Reboot-Time '
                          '"03:00";', apt)
            # An agent session IS a logged-in user, so without this the
            # reboot is skipped for as long as anyone is attached — which
            # on this box is always, making the knob look broken.
            self.assertIn('Unattended-Upgrade::Automatic-Reboot-WithUsers '
                          '"true";', apt)

    def test_a_quoted_false_does_not_decide_the_patch_policy(self):
        """The trap the other booleans here already guard: a string is
        truthy, so `enable: "false"` would render the policy the config
        was trying to turn off. A quoted value is a typo in either
        direction, and the config must not guess which.
        """
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in ("false", "true", 1):
                config["osUpdates"] = {"enable": bad}
                with self.assertRaises(mod.ConfigError, msg=repr(bad)):
                    mod.Spec(config, prof)
            # A FALSY non-mapping is the dangerous half: `osUpdates:
            # false` is the obvious way to write "off", and folding it
            # into an empty mapping (`data.get(...) or {}`) would default
            # `enable` back to true — the one spelling meaning "leave my
            # box's patching alone" turning it on. Only an absent key is
            # absent.
            for bad in ("yes", False, 0, [], ""):
                config["osUpdates"] = bad
                with self.assertRaises(mod.ConfigError, msg=repr(bad)):
                    mod.Spec(config, prof)

    def test_turning_os_updates_off_takes_the_policy_with_it(self):
        """apply never deletes what a render merely stops emitting, so
        the off switch has to name both files. Otherwise a box applied
        once with osUpdates on and then off keeps patching — and keeps
        restarting daemons — under rules its configuration no longer
        mentions. One root, two renders: two output trees cannot see it.
        """
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "one-root"
            owned = [out / "etc/apt/apt.conf.d/52-agent-box-unattended",
                     out / "etc/needrestart/conf.d/50-agent-box.conf"]

            def render(enable):
                config["osUpdates"] = {"enable": enable}
                cfg = Path(tmp) / "config.json"
                cfg.write_text(json.dumps(config))
                proc = subprocess.run(
                    [sys.executable, str(AGENTBOX), "apply",
                     "--config", str(cfg), "--profile", str(prof),
                     "--root", str(out)], capture_output=True, text=True)
                self.assertEqual(0, proc.returncode, proc.stderr)

            render(True)
            for f in owned:
                self.assertTrue(f.exists(), f"{f.name} not rendered")
            render(False)
            for f in owned:
                self.assertFalse(f.exists(),
                                 f"{f.name} survived osUpdates.enable: false")

    def test_a_patch_policy_we_did_not_write_is_never_removed(self):
        """An administrator's own file at either name keeps it: the
        generated header is what makes a file ours, not the path (the
        rule remove_if_ours already enforces for every other rendered
        file).
        """
        config = json.loads(CONFIG_JSON.read_text())
        config["osUpdates"] = {"enable": False}
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "one-root"
            mine = "// mine, thanks\n"
            for rel in ("etc/apt/apt.conf.d/52-agent-box-unattended",
                        "etc/needrestart/conf.d/50-agent-box.conf"):
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_text(mine)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(config))
            proc = subprocess.run(
                [sys.executable, str(AGENTBOX), "apply",
                 "--config", str(cfg), "--profile", str(prof),
                 "--root", str(out)], capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, proc.stderr)
            for rel in ("etc/apt/apt.conf.d/52-agent-box-unattended",
                        "etc/needrestart/conf.d/50-agent-box.conf"):
                self.assertEqual(mine, (out / rel).read_text())

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

    def test_every_user_gets_their_own_canonical_guide(self):
        """Issue #394. Two things were wrong at once here.

        The guide was written to a hardcoded AGENTS.agent.md from inside
        the per-user loop, so a second user overwrote the first's copy and
        both pointers named a file describing one of them. And nothing set
        AGENT_BOX_GUIDE_TARGET, so supervisor.sh never symlinked the guide
        into ~/.claude/CLAUDE.md or $CODEX_HOME/AGENTS.md — which is the
        scope that survives an agent working inside a checkout BELOW $HOME,
        i.e. what a session doing real work does.
        """
        for user in ("agent", "robot"):
            canonical = FIXTURE / f"etc/agent-box-guides/AGENTS.{user}.md"
            self.assertTrue(canonical.is_file(),
                            f"{user} has no canonical guide")
            pointer = (FIXTURE / "etc/agent-box/guides"
                       / f"{user}-agents-pointer.md").read_text()
            self.assertIn(f"@/etc/agent-box-guides/AGENTS.{user}.md", pointer)
            env = (FIXTURE / "etc/agent-box/units" / f"{user}.env").read_text()
            self.assertIn(
                f"AGENT_BOX_GUIDE_TARGET=/etc/agent-box-guides/"
                f"AGENTS.{user}.md", env)
        # The host label names the user, so the two guides' pointers must
        # not be byte-identical — that was the symptom of the overwrite.
        self.assertNotEqual(
            (FIXTURE / "etc/agent-box/guides/agent-agents-pointer.md").read_text(),
            (FIXTURE / "etc/agent-box/guides/robot-agents-pointer.md").read_text())

    def test_the_agent_clis_get_their_own_config(self):
        """Issue #394, gaps 4 and 5.

        Both are files the agent CLIs read for themselves, and both were
        missing natively:

          claude  supervisor.sh passes --settings only when
                  AGENT_BOX_CLAUDE_SETTINGS names a file, so the
                  SessionStart hook (issue #223) never fired on a native
                  box — silently, since a missing hook looks like a hook
                  with nothing to say.
          codex   /etc/codex/config.toml is codex's system layer and the
                  ONLY way to reach the app-server daemon behind a
                  remote-controlled session, which is spawned with a fixed
                  argv. Without it a native codex box asked for approvals
                  where a NixOS one did not.

        The env var and the file go together on purpose: supervisor.sh
        treats the var as "the box wrote a system-wide codex config", and
        a session opting OUT of full access pins the restricted values back
        because of it.
        """
        settings = json.loads(
            (FIXTURE / "etc/agent-box/claude-hook-settings.json").read_text())
        hook = (settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertTrue(hook.endswith("agent-box-claude-session-start-hook"),
                        f"SessionStart hook is {hook!r}")
        self.assertTrue(hook.startswith("@PROFILE@/bin/"),
                        "the hook must be named by absolute path — the "
                        "agent CLI does not inherit the unit's PATH")
        toml = (FIXTURE / "etc/codex/config.toml").read_text()
        self.assertIn('approval_policy = "never"', toml)
        # Both keys, or the box opens the filesystem and still stops to ask.
        self.assertIn('sandbox_mode = "danger-full-access"', toml)
        for user in ("agent", "robot"):
            env = (FIXTURE / "etc/agent-box/units" / f"{user}.env").read_text()
            self.assertIn("AGENT_BOX_CLAUDE_SETTINGS="
                          "/etc/agent-box/claude-hook-settings.json", env)
            self.assertIn("AGENT_BOX_CODEX_FULL_ACCESS=1", env)

    def test_turning_codex_full_access_off_removes_the_file(self):
        """The dangerous transition, applied to ONE root.

        A box rendered with codexFullAccess: true and later false kept
        sandbox_mode = "danger-full-access" on disk — apply never deletes
        what a render stops emitting — while AGENT_BOX_CODEX_FULL_ACCESS
        disappeared. Full access still in force, and the flag a session
        uses to pin the restricted values back gone with it: the contract
        the file/flag pair exists to keep, inverted. Two separate output
        roots cannot see this, which is why this test reuses one.
        """
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "one-root"
            conf = out / "etc/codex/config.toml"

            def apply(full_access):
                config["codexFullAccess"] = full_access
                args = [sys.executable, str(AGENTBOX), "apply",
                        "--config", str(CONFIG_JSON), "--profile", str(prof),
                        "--root", str(out)]
                cfg = Path(tmp) / "config.json"
                cfg.write_text(json.dumps(config))
                args[args.index("--config") + 1] = str(cfg)
                proc = subprocess.run(args, capture_output=True, text=True)
                self.assertEqual(0, proc.returncode, proc.stderr)

            def env_text(user):
                return (out / "etc/agent-box/units" / f"{user}.env").read_text()

            apply(True)
            self.assertIn("danger-full-access", conf.read_text())
            for user in ("agent", "robot"):
                self.assertIn("AGENT_BOX_CODEX_FULL_ACCESS=1", env_text(user))
            apply(False)
            self.assertFalse(conf.exists(),
                             "stale full-access config survived the flip")
            for user in ("agent", "robot"):
                self.assertNotIn("AGENT_BOX_CODEX_FULL_ACCESS", env_text(user),
                                 "stale full-access flag survived the flip")

    def test_a_file_we_did_not_write_is_never_removed(self):
        """Removal is scoped to the generated header. A human who takes a
        rendered file over keeps it — a renderer that deletes files it did
        not write is a footgun aimed at exactly the person least likely to
        expect it."""
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            mine = Path(tmp) / "config.toml"
            mine.write_text('approval_policy = "on-request"\n')
            self.assertFalse(mod.remove_if_ours(mine))
            self.assertTrue(mine.exists())
            ours = Path(tmp) / "ours.toml"
            ours.write_text(mod.GENERATED_HEADER + "x = 1\n")
            self.assertTrue(mod.remove_if_ours(ours))
            self.assertFalse(ours.exists())
            # A generated SCRIPT carries the header on line TWO — the
            # shebang has to be first or the kernel will not run it. Held
            # by name because the strict first-line test read every
            # rendered helper as someone else's file, and the first one
            # that ever needed removing (the zram helper, protectMemory
            # flipped off) was silently left running.
            script = Path(tmp) / "ours.sh"
            script.write_text("#!/bin/sh\n" + mod.GENERATED_HEADER + "true\n")
            self.assertTrue(mod.remove_if_ours(script))
            self.assertFalse(script.exists())
            theirs = Path(tmp) / "theirs.sh"
            theirs.write_text("#!/bin/sh\necho mine\n")
            self.assertFalse(mod.remove_if_ours(theirs))
            self.assertTrue(theirs.exists())
            # A symlink at the name is never ours, no matter what it
            # points at: read_text() follows it, so without a dedicated
            # check an administrator's OWN symlink into a file that
            # happens to carry our header would read as generated and get
            # unlinked out from under them (issue #404 review).
            elsewhere = Path(tmp) / "elsewhere.toml"
            elsewhere.write_text(mod.GENERATED_HEADER + "x = 1\n")
            link = Path(tmp) / "linked.toml"
            link.symlink_to(elsewhere)
            self.assertFalse(mod.remove_if_ours(link))
            self.assertTrue(link.is_symlink())
            # The target is an administrator's file too, reached only
            # through their own symlink — leaving the link alone is not
            # enough if the target got touched on the way.
            self.assertTrue(elsewhere.exists())
            self.assertEqual(mod.GENERATED_HEADER + "x = 1\n",
                             elsewhere.read_text())

    def test_codex_full_access_is_file_and_flag_together(self):
        """Never the flag without the file. supervisor.sh reads the flag as
        evidence the file exists, so setting one alone would make a
        session's skipPermissions = false opt-out fight a default nobody
        wrote."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for want in (False, True):
                config["codexFullAccess"] = want
                spec = mod.Spec(config, prof)
                rend = mod.Renderer(spec, prof, root=Path(tmp) / f"o{want}")
                tree = rend.render()
                toml = [f for f in tree.files if f.endswith("codex/config.toml")]
                env = [text for path, (text, _) in tree.files.items()
                       if path.endswith("/agent.env")][0]
                self.assertEqual(want, bool(toml))
                self.assertEqual(want, "AGENT_BOX_CODEX_FULL_ACCESS" in env)

    def test_a_quoted_false_does_not_turn_codex_loose(self):
        """`bool("false")` is True — a quoted string here would silently
        grant danger-full-access instead of the false the author wrote
        (issue #404 review). null is kept meaning off, same as bool(None)
        always made it; only a type this knob has no business carrying
        is rejected."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in ("false", "true", 0, 1, [], {}):
                config["codexFullAccess"] = bad
                with self.assertRaises(mod.ConfigError):
                    mod.Spec(config, prof)
            for ok in (True, False, None):
                config["codexFullAccess"] = ok
                mod.Spec(config, prof)  # must not raise
            # null has to actually BEHAVE like off, not just be accepted:
            # bool(None) is False, but only a render proves nothing reads
            # it the other way.
            config["codexFullAccess"] = None
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=Path(tmp) / "null").render()
            self.assertFalse(
                [f for f in tree.files if f.endswith("codex/config.toml")],
                "codexFullAccess: null rendered the full-access file")
            env = [text for path, (text, _) in tree.files.items()
                   if path.endswith("/agent.env")][0]
            self.assertNotIn("AGENT_BOX_CODEX_FULL_ACCESS", env)

    def test_a_web_box_seeds_no_session_and_a_console_box_still_does(self):
        """The front door (issue #416). A user with a browser terminal and
        no declared sessions gets an EMPTY seed — the settings page is
        where a session is started, which is what lets the agent CLIs be
        fetched on demand instead of shipped in the image. A box with no
        web has no front door, so it keeps the "main" it always seeded:
        neither a settings page nor a session leaves no way in but the
        console. This must match seedMain in modules/agent-box.nix.in — two
        backends answering it differently is the same box behaving two
        ways."""
        mod = load_agentbox()
        base = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for web, want in ((True, []), (False, ["main"])):
                config = json.loads(json.dumps(base))
                config["web"] = {"enable": web}
                # No "sessions" key at all: this is the defaulting path.
                config["users"] = {"agent": {}}
                spec = mod.Spec(config, prof)
                rend = mod.Renderer(spec, prof, root=Path(tmp) / f"w{web}")
                tree = rend.render()
                seed = [text for path, (text, _) in tree.files.items()
                        if path.endswith("agent-sessions.json")][0]
                self.assertEqual(
                    want, sorted(json.loads(seed)["sessions"]),
                    f"web.enable={web} seeded the wrong sessions")

    def test_seed_main_session_overrides_the_front_door_either_way(self):
        """The escape hatch, and the type check on it. `seedMainSession:
        true` keeps the pre-#416 session on a web box; false removes it
        from a console box. A quoted "false" is truthy, so it would
        silently seed the session the author asked not to have — the same
        trap codexFullAccess had."""
        mod = load_agentbox()
        base = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for web, seed_main, want in ((True, True, ["main"]),
                                         (False, False, [])):
                config = json.loads(json.dumps(base))
                config["web"] = {"enable": web}
                config["users"] = {"agent": {"seedMainSession": seed_main}}
                spec = mod.Spec(config, prof)
                tree = mod.Renderer(
                    spec, prof,
                    root=Path(tmp) / f"o{web}{seed_main}").render()
                seed = [text for path, (text, _) in tree.files.items()
                        if path.endswith("agent-sessions.json")][0]
                self.assertEqual(want, sorted(json.loads(seed)["sessions"]))
            for bad in ("false", "true", 0, 1, []):
                config = json.loads(json.dumps(base))
                config["users"] = {"agent": {"seedMainSession": bad}}
                with self.assertRaises(mod.ConfigError):
                    mod.Spec(config, prof)

    def test_the_jit_source_is_pinned_and_carries_no_host_store_path(self):
        """Lazy harnesses fetch from the box's OWN pinned nixpkgs, not the
        flake registry — that one follows nixpkgs-unstable and was measured
        resolving gh 2.98.0 on a box shipping 2.97.0. And nothing here may
        name a /nix/store path: resolving `nix` at apply time baked the
        BUILDING host's store path into a generated file, so the fixture
        differed per machine and would break on the next nix upgrade.
        agents.sh resolves it at runtime instead."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=Path(tmp) / "jit").render()
            env = [text for path, (text, _) in tree.files.items()
                   if path.endswith("/agent.env")][0]
            self.assertIn("AGENT_BOX_NIXPKGS=", env)
            self.assertNotIn("AGENT_BOX_NIX_BIN", env)
            line = [ln for ln in env.splitlines()
                    if ln.startswith("AGENT_BOX_NIXPKGS=")][0]
            self.assertNotIn("/nix/store/", line)
            # An override has to actually reach the unit.
            config["jitNixpkgs"] = "https://example.invalid/pinned.tar.xz"
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=Path(tmp) / "jit2").render()
            env = [text for path, (text, _) in tree.files.items()
                   if path.endswith("/agent.env")][0]
            self.assertIn(
                "AGENT_BOX_NIXPKGS=https://example.invalid/pinned.tar.xz", env)

    def test_both_backends_expose_the_same_jit_pin_knob(self):
        """`jitNixpkgs` exists on BOTH backends (issue #416). The default
        fallback is the mutable nixos-unstable channel, which is the right
        default but not reproducible — so an operator who needs the lazy
        path pinned must be able to pin it whichever backend they deploy.
        Native having the knob and the module not would have been an
        asymmetry this PR introduced."""
        mod = load_agentbox()
        module_src = (REPO / "modules" / "agent-box.nix.in").read_text()
        self.assertIn("jitNixpkgs = lib.mkOption", module_src)
        self.assertIn("cfg.jitNixpkgs", module_src)
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            self.assertEqual(
                "https://channels.nixos.org/nixos-unstable/nixexprs.tar.xz",
                mod.Spec(config, prof).jit_nixpkgs)
            config["jitNixpkgs"] = "https://example.invalid/p.tar.xz"
            self.assertEqual("https://example.invalid/p.tar.xz",
                             mod.Spec(config, prof).jit_nixpkgs)

    def test_hook_session_args_rejects_a_bare_string(self):
        """A bare string is iterable, so list() would silently turn
        "--model foo" into one argument per CHARACTER instead of raising —
        exactly the shape a YAML author reaches for by mistake when they
        mean a single-element list (issue #404 review)."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in ("--model foo", [1, 2], {"a": "b"}, 5):
                config["webhook"] = {"hookSessionArgs": bad}
                with self.assertRaises(mod.ConfigError):
                    mod.Spec(config, prof)
            for ok in (["--model", "foo"], [], None):
                config["webhook"] = {"hookSessionArgs": ok}
                mod.Spec(config, prof)  # must not raise

    def test_the_webhook_dispatcher_is_fully_wired(self):
        """Issue #394, gap 6. Per-session delivery worked natively; the
        pieces around it did not, each failing quietly in its own way:

          HOOK_SPAWN_CMD          the standing-watch panel could not print
                                  the prompt the next match would launch.
          WEBHOOK_PYTHON          the panel fell back to the daemon's own
                                  interpreter, which need not carry
                                  local-webhook's dependencies.
          WEBHOOK_PINNED_SCRIPT   claude sessions tracked the marketplace's
                                  default branch instead of the plugin
                                  version the box pins.
          HOOK_SESSION_ARGS       no box-wide default for the hook-*
                                  sessions a watch spawns (issue #216's
                                  cheaper triage model).
        """
        settings = (FIXTURE / "etc/systemd/system"
                    / "agent-box-settings@agent.service.d"
                    / "10-host.conf").read_text()
        self.assertIn("AGENT_BOX_HOOK_SPAWN_CMD=", settings)
        self.assertIn("AGENT_BOX_WEBHOOK_PYTHON=", settings)
        env = (FIXTURE / "etc/agent-box/units/agent.env").read_text()
        self.assertIn("AGENT_BOX_WEBHOOK_PINNED_SCRIPT=", env)
        wrapper = (FIXTURE / "usr/local/bin/agent-box-webhook").read_text()
        self.assertIn("AGENT_BOX_HOOK_SESSION_ARGS=", wrapper)
        # JSON, not a shell list: webhook-spawn.sh parses it with jq
        # precisely so an argument may contain spaces.
        args = wrapper.split("AGENT_BOX_HOOK_SESSION_ARGS=", 1)[1]
        json.loads(args.split("\n")[0].strip().strip("'"))

    def test_a_hook_arg_with_an_apostrophe_survives_the_wrapper(self):
        """The wrapper is generated shell. JSON does not escape an
        apostrophe, so `don't` closed the quoted value and broke
        agent-box-webhook for every caller — not just the one who set it.
        Also covers webhook: null and hookSessionArgs: null, either of
        which used to raise in Spec before anything was rendered."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        config["webhook"]["hookSessionArgs"] = [
            "--append-system-prompt", "don't guess; ask"]
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=Path(tmp) / "o").render()
            wrapper = [text for path, (text, _) in tree.files.items()
                       if path.endswith("bin/agent-box-webhook")][0]
            line = [x for x in wrapper.splitlines()
                    if "AGENT_BOX_HOOK_SESSION_ARGS" in x][0]
            # What the shell would actually assign, per the shell.
            out = subprocess.run(["sh", "-c", line + "\n"
                                  "printf %s \"$AGENT_BOX_HOOK_SESSION_ARGS\""],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(config["webhook"]["hookSessionArgs"],
                             json.loads(out.stdout))
        for empty in ({"webhook": None},
                      {"webhook": {"enable": True, "script": "/x",
                                   "hookSessionArgs": None}}):
            cfg = dict(json.loads(CONFIG_JSON.read_text()), **empty)
            with tempfile.TemporaryDirectory() as tmp:
                prof = build_fake_profile(tmp)
                self.assertEqual([], mod.Spec(cfg, prof).hook_session_args)

    def test_the_root_daemon_knows_every_terminal_user(self):
        """AGENT_BOX_WEB_USERS is what GET / offers. Only the root daemon
        serves that page, so only it gets the list."""
        root = (FIXTURE / "etc/agent-box/units"
                / "agent-box-settings-agent.env").read_text()
        self.assertIn("AGENT_BOX_WEB_USERS=agent,robot", root)
        other = (FIXTURE / "etc/agent-box/units"
                 / "agent-box-settings-robot.env").read_text()
        self.assertNotIn("AGENT_BOX_WEB_USERS", other)

    def test_memory_pressure_has_all_three_answers(self):
        """Issue #62, and #394 gap 9.

        The incident: a swapless box under agent memory pressure never
        OOM-killed. It thrashed the page cache instead — every fault
        reading pages straight back off disk — and userspace froze for
        hours while the health checks stayed green. Nothing was out of
        memory; everything was too slow to say so.

        Natively this was ONE of the three knobs: OOMScoreAdjust told the
        kernel which process to kill in a situation the kernel was never
        going to notice, while the earlyoom binary sat unused in the
        runtime profile.
        """
        units = FIXTURE / "etc/systemd/system"
        zram = (units / "agent-box-zram.service").read_text()
        self.assertIn("RemainAfterExit=yes", zram)
        helper = (FIXTURE / "etc/agent-box/bin/agent-box-zram").read_text()
        self.assertIn("--algorithm zstd", helper)
        # Above any disk swap the deployment made (the Lightsail template
        # writes a 2 GiB file): compressed RAM first, disk when it fills.
        self.assertIn("--priority 100", helper)
        early = (units / "agent-box-earlyoom.service").read_text()
        for arg in ("-m 10", "-s 10", "-r 3600", "--avoid", "--prefer"):
            self.assertIn(arg, early)
        # It must outlive what it arbitrates, and start after the swap it
        # measures.
        self.assertIn("OOMScoreAdjust=-100", early)
        self.assertIn("After=agent-box-zram.service", early)
        sysctl = (FIXTURE / "etc/sysctl.d/60-agent-box-memory.conf").read_text()
        self.assertIn("vm.swappiness = 180", sysctl)
        self.assertIn("vm.page-cluster = 0", sysctl)

    def test_zram_module_available(self):
        """Issue #435: Azure's linux-azure kernel ships no zram module, so
        `modprobe zram` fails and agent-box-zram.service dies at boot with
        nothing else to say why. `apply` has to ask first, with a --dry-run
        that never actually loads a module a caller may not want loaded."""
        mod = load_agentbox()
        with mock.patch.object(mod.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertTrue(mod.zram_module_available())
            self.assertIn("--dry-run", run.call_args.args[0])

            run.return_value = subprocess.CompletedProcess([], 1)
            self.assertFalse(mod.zram_module_available())

            run.side_effect = FileNotFoundError("no modprobe")
            self.assertTrue(mod.zram_module_available())

            # A different OSError (e.g. permissions) is not proof the
            # module is fine — staying quiet there is exactly the
            # silent-degradation this check exists to catch.
            run.side_effect = PermissionError("denied")
            self.assertFalse(mod.zram_module_available())

    def test_protect_memory_false_renders_none_of_it(self):
        """The knob has to actually be a knob: a host that turns it off
        gets no units, not units that are installed and idle."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        config["protectMemory"] = False
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=Path(tmp) / "o").render()
            for name in ("agent-box-zram.service",
                         "agent-box-earlyoom.service",
                         "60-agent-box-memory.conf"):
                self.assertFalse([f for f in tree.files if f.endswith(name)],
                                 f"{name} rendered with protectMemory off")

    def test_turning_protect_memory_off_takes_the_daemon_with_it(self):
        """The dangerous transition, applied to ONE root.

        The knob rendering nothing is only half of it. A box applied with
        protectMemory: true and later false kept the zram device swapped
        in and earlyoom RUNNING — apply never deletes what a render stops
        emitting — so a daemon whose whole job is to kill the agent CLIs
        went on doing it with nothing left in the config that named it.
        Two separate output roots cannot see this, which is why this test
        reuses one, exactly as the codexFullAccess test above does.
        """
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "one-root"
            owned = [out / "etc/systemd/system/agent-box-zram.service",
                     out / "etc/systemd/system/agent-box-earlyoom.service",
                     out / "etc/agent-box/bin/agent-box-zram",
                     out / "etc/sysctl.d/60-agent-box-memory.conf"]

            def render(protect):
                config["protectMemory"] = protect
                cfg = Path(tmp) / "config.json"
                cfg.write_text(json.dumps(config))
                spec = mod.Spec(config, prof)
                tree = mod.Renderer(spec, prof, root=out).render()
                proc = subprocess.run(
                    [sys.executable, str(AGENTBOX), "apply",
                     "--config", str(cfg), "--profile", str(prof),
                     "--root", str(out)], capture_output=True, text=True)
                self.assertEqual(0, proc.returncode, proc.stderr)
                return tree

            render(True)
            for f in owned:
                self.assertTrue(f.exists(), f"{f.name} not rendered")
            tree = render(False)
            for f in owned:
                self.assertFalse(f.exists(),
                                 f"{f.name} survived protectMemory: false")
            # Removing the unit FILE is not stopping the daemon. The units
            # have to be named for disabling too, or earlyoom keeps running
            # from the copy systemd already loaded — and named in THIS
            # order: earlyoom is what kills, so it has to be the one
            # already stopped when zram's swapoff creates a memory spike
            # bringing the compressed pages back to RAM, not the one still
            # running to react to it.
            self.assertEqual(["agent-box-earlyoom.service",
                              "agent-box-zram.service"],
                             tree.disable)

    def test_a_unit_this_box_never_had_is_not_disabled(self):
        """The teardown must be quiet on a box that always had the knob
        off: `systemctl disable` on a name that was never installed is an
        error on every apply, and output nobody reads is output that hides
        the next real problem."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        config["protectMemory"] = False
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=Path(tmp) / "fresh").render()
            self.assertEqual([], tree.disable)

    def test_an_administrator_edited_unit_is_not_disabled(self):
        """Existence alone is not ownership.

        A unit file present at the name we generate is not necessarily
        one we generated — an administrator may have taken it over, the
        same case remove_if_ours already respects. Queuing that unit for
        disable-and-stop because a file merely exists at its path would
        stop a service whose file remove_if_ours goes on to leave in
        place: this box's render deciding a customized unit is torn down,
        which nothing about protectMemory: false authorizes.
        """
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "one-root"
            config["protectMemory"] = True
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps(config))
            spec = mod.Spec(config, prof)
            mod.Renderer(spec, prof, root=out).render()
            proc = subprocess.run(
                [sys.executable, str(AGENTBOX), "apply",
                 "--config", str(cfg), "--profile", str(prof),
                 "--root", str(out)], capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, proc.stderr)

            unit = out / "etc/systemd/system/agent-box-earlyoom.service"
            unit.write_text("# hand-edited by an administrator\n")

            config["protectMemory"] = False
            spec = mod.Spec(config, prof)
            tree = mod.Renderer(spec, prof, root=out).render()
            self.assertNotIn("agent-box-earlyoom.service", tree.disable)

    def test_only_an_oomd_we_disabled_is_ever_re_enabled(self):
        """systemd-oomd is the one piece of memory protection that is the
        DISTRO's, not ours: apply turns it off so it does not race
        earlyoom. Giving it back on the way down is only correct for a box
        where we were the one who took it — `is-enabled` reporting
        "masked", "disabled" or nothing at all means an admin decided
        this, and apply leaves their decision alone."""
        mod = load_agentbox()

        def fake_run(cmd, check=True, capture=False):
            return subprocess.CompletedProcess(cmd, 0, stdout=reply, stderr="")

        with contextlib.ExitStack() as stack:
            stack.callback(setattr, mod, "run", mod.run)
            mod.run = fake_run
            for reply, want in (("enabled\n", True),
                                ("disabled\n", False),
                                ("masked\n", False),
                                ("static\n", False),
                                ("", False)):
                self.assertIs(want, mod.unit_is_enabled(mod.OOMD_UNIT),
                              f"is-enabled said {reply!r}")

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

    def test_ttyd_override_keeps_every_flag_the_template_sets(self):
        """The terminal drop-in overrides ExecStart only to absolutize two
        binaries — `ttyd` and `agent-box-attach` are bare in the shared
        template and this unit carries no agent PATH — so every `-t` client
        option is a RESTATEMENT of
        modules/src/units/agent-web-terminal@.service and must match it.

        Restating drops things: macOptionClickForcesSelection (#327, the Mac
        Option-drag selection the shipped guide tells every user to use) was
        in the template and in the module's override, and missing from this
        one, for as long as the three spellings were maintained by hand. The
        backend-parity check cannot see that class — it is keyed on
        AGENT_BOX_* names and these are bare ttyd flags — and neither can the
        byte fixture, which `--update` regenerates around whatever the
        renderer currently emits. Hence a test that reads the template.
        """
        def options(execstart):
            # A LIST, not a dict: a restatement can also gain a duplicate
            # `-t`, and ttyd takes the last one — which a dict would hide by
            # collapsing the pair silently (CodeRabbit, PR #454).
            return re.findall(r"-t ([\w-]+)=(\S+)", execstart)

        template = next(
            x for x in (SRC / "units" / "agent-web-terminal@.service")
            .read_text().splitlines() if x.startswith("ExecStart="))
        for user in ("agent", "robot"):
            conf = (FIXTURE / "etc/systemd/system"
                    / f"agent-web-terminal@{user}.service.d" / "10-host.conf")
            # The LAST ExecStart= line: the first is the empty reset that
            # systemd requires before a template's own value can be replaced.
            override = [x for x in conf.read_text().splitlines()
                        if x.startswith("ExecStart=")][-1]
            got, want = options(override), options(template)
            self.assertEqual(
                sorted(n for n, _ in want), sorted(n for n, _ in got),
                f"agent-web-terminal@{user} drops or invents a ttyd option "
                f"the shared template does not have")
            # Values too, wherever the template's is a literal. Only the
            # interpolating ones (%i, ${VAR}) may differ — the renderer
            # substitutes those — so comparing names alone would accept
            # `macOptionClickForcesSelection=false`, which is the same bug
            # this test exists for with one word changed.
            literal = {n: v for n, v in want
                       if "%i" not in v and "${" not in v}
            self.assertEqual(
                literal, {n: v for n, v in got if n in literal},
                f"agent-web-terminal@{user} changes the VALUE of a ttyd "
                f"option the shared template pins")


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

    def test_only_the_root_user_may_reboot_the_box(self):
        """The Danger zone's "Reboot box", and the boundary it respects.

        "Restart all" bounces the caller's OWN unit and needs no sudo at
        all; a reboot takes every user's sessions down with it. So the
        grant and the variable that unlocks the card go to the root user
        alone — otherwise a second terminal user's page is a button that
        kills the first user's work, which is the same thing the per-user
        password helper grant exists to prevent.
        """
        trigger = self.mod.REBOOT_TRIGGER
        sudoers = (FIXTURE / "etc/sudoers.d/agent-box").read_text()
        lines = {line.split()[0]: line for line in sudoers.splitlines()
                 if " ALL=" in line}
        self.assertIn(trigger, lines["agent"])
        self.assertNotIn(trigger, lines["robot"],
                         "a non-root terminal user was granted the reboot")
        # Same string on both sides of sudo, or the grant does not match
        # the command and sudo asks for a password instead (issue #353).
        want = f'Environment="AGENT_BOX_REBOOT_CMD=/usr/bin/sudo -n {trigger}"'
        for user, present in (("agent", True), ("robot", False)):
            conf = (FIXTURE / "etc/systemd/system"
                    / f"agent-box-settings@{user}.service.d"
                    / "10-host.conf").read_text()
            self.assertEqual(present, want in conf,
                             f"AGENT_BOX_REBOOT_CMD for {user}")
        # --no-block is not decoration: the daemon has to answer the
        # request before systemd starts stopping units, and it is one of
        # the units being stopped.
        self.assertIn("--no-block", trigger)

    def test_turning_the_reboot_button_off_takes_the_grant_with_it(self):
        """The knob is not just cosmetic. A box that hides the card while
        keeping `sudo systemctl reboot` in sudoers would have given away
        the whole point of turning it off — the button is the visible
        half, the grant is the half that matters."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        config["web"] = dict(config.get("web") or {}, rebootButton=False)
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "no-reboot"
            spec = mod.Spec(config, prof)
            self.assertFalse(spec.reboot_button)
            tree = mod.Renderer(spec, prof, root=out).render()
            sudoers = tree.files[str(out / "etc/sudoers.d/agent-box")][0]
            self.assertNotIn(mod.REBOOT_TRIGGER, sudoers)
            conf = tree.files[str(out / "etc/systemd/system"
                                  / "agent-box-settings@agent.service.d"
                                  / "10-host.conf")][0]
            self.assertNotIn("AGENT_BOX_REBOOT_CMD", conf)

    def test_a_quoted_false_does_not_hand_out_the_reboot(self):
        """The trap the other booleans here guard: a string is truthy, so
        `rebootButton: "false"` would grant the very thing it was written
        to withhold. null still means off."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            for bad in ("false", "true", 1):
                config["web"] = dict(config.get("web") or {},
                                     rebootButton=bad)
                with self.assertRaises(mod.ConfigError, msg=repr(bad)):
                    mod.Spec(config, prof)

    def test_a_console_box_has_no_reboot_button_to_gate(self):
        """No web, no settings page, no card — and therefore no reason to
        hand anyone a root reboot. The grant follows the button, not the
        default."""
        mod = load_agentbox()
        config = json.loads(CONFIG_JSON.read_text())
        config["web"] = {"enable": False}
        with tempfile.TemporaryDirectory() as tmp:
            prof = build_fake_profile(tmp)
            out = Path(tmp) / "console"
            spec = mod.Spec(config, prof)
            self.assertFalse(spec.reboot_button)
            tree = mod.Renderer(spec, prof, root=out).render()
            self.assertNotIn(
                mod.REBOOT_TRIGGER,
                tree.files[str(out / "etc/sudoers.d/agent-box")][0])

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
