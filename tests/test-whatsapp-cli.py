#!/usr/bin/env python3
"""Optional WhatsApp runtime upgrades preserve the working installation."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parent.parent / "modules/src/whatsapp-cli.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("whatsapp_cli_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WhatsAppInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = load_cli()
        self.cli.HOME = self.root
        self.cli.RUNTIME = self.root / ".local/share/local-whatsapp"
        self.cli.STATE = self.root / ".local/state/local-whatsapp"
        self.cli.NODE = self.root / ".nix-profile/bin/node"
        self.cli.NPM = self.root / ".nix-profile/bin/npm"
        self.cli.NODE.parent.mkdir(parents=True)
        self.cli.NODE.touch()
        self.cli.NPM.touch()
        self.files = {
            name: ("updated " + name).encode()
            for name in ("bridge.mjs", "state.mjs", "package.json", "package-lock.json")
        }
        self.cli.FILES = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in self.files.items()
        }
        self.cli.RUNTIME.mkdir(parents=True)
        (self.cli.RUNTIME / "bridge.mjs").write_text("old bridge")
        (self.cli.RUNTIME / "node_modules").mkdir()
        self.cli.STATE.mkdir(parents=True)
        (self.cli.STATE / "auth-marker").write_text("linked")

    def run_install(self, corrupt=False):
        calls = []

        def urlopen(url, timeout):
            name = url.rsplit("/", 1)[-1]
            content = b"wrong" if corrupt else self.files[name]
            calls.append(name)
            return io.BytesIO(content)

        def npm(argv, **kwargs):
            self.assertEqual(str(self.cli.NPM), argv[0])
            (kwargs["cwd"] / "node_modules").mkdir()

        with mock.patch.object(self.cli, "urlopen", side_effect=urlopen), \
                mock.patch.object(self.cli.subprocess, "run", side_effect=npm):
            if corrupt:
                with self.assertRaisesRegex(RuntimeError, "integrity check failed"):
                    self.cli.install()
            else:
                self.cli.install()
        return calls

    def test_upgrade_replaces_runtime_and_keeps_device_link(self):
        self.assertEqual(set(self.files), set(self.run_install()))
        self.assertEqual(self.files["bridge.mjs"],
                         (self.cli.RUNTIME / "bridge.mjs").read_bytes())
        self.assertEqual("linked", (self.cli.STATE / "auth-marker").read_text())
        self.assertFalse(self.cli.RUNTIME.with_name("local-whatsapp.previous").exists())
        self.assertEqual([], self.run_install())

    def test_bad_download_leaves_old_runtime_in_place(self):
        self.run_install(corrupt=True)
        self.assertEqual("old bridge", (self.cli.RUNTIME / "bridge.mjs").read_text())
        self.assertEqual("linked", (self.cli.STATE / "auth-marker").read_text())

    def test_interrupted_upgrade_restores_legacy_backup_before_download(self):
        backup = self.cli.RUNTIME.with_name("local-whatsapp.previous")
        self.cli.RUNTIME.rename(backup)
        self.run_install(corrupt=True)
        self.assertEqual("old bridge", (self.cli.RUNTIME / "bridge.mjs").read_text())
        self.assertFalse(backup.exists())

    def test_interrupted_upgrade_restores_pending_runtime(self):
        pending = self.cli.RUNTIME.with_name("local-whatsapp.pending")
        self.cli.RUNTIME.rename(pending)
        self.run_install(corrupt=True)
        self.assertEqual("old bridge", (self.cli.RUNTIME / "bridge.mjs").read_text())
        self.assertFalse(pending.exists())

    def test_profile_is_private_and_defaults_when_cleared(self):
        self.assertIsNone(self.cli.profile())
        self.assertEqual("phone-agent", self.cli.profile("phone-agent"))
        config = self.cli.STATE / "config.json"
        self.assertEqual(0o600, config.stat().st_mode & 0o777)
        self.assertEqual("phone-agent", self.cli.profile())
        self.assertIsNone(self.cli.profile("default"))
        with self.assertRaisesRegex(RuntimeError, "profile"):
            self.cli.profile("not a profile")
        with self.assertRaisesRegex(RuntimeError, "profile"):
            self.cli.profile("dotted.name")

    def test_pair_rejects_non_ascii_digits(self):
        for phone in ("+1 415 555 1234\u0663", "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668", "12345abc678"):
            with mock.patch.dict("os.environ", {"LOCAL_WHATSAPP_PHONE": phone}):
                with self.assertRaisesRegex(RuntimeError, "LOCAL_WHATSAPP_PHONE"):
                    self.cli.pair()


if __name__ == "__main__":
    unittest.main()
