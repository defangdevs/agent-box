#!/usr/bin/env python3
"""The settings page exposes only the managed armored public key.

The subject is the assembled golden payload, like the other settings-panel
checks: that is the daemon as it ships, with the shared env-store library
prepended. These tests pin the missing-key state, bounded file read, HTML
escaping, disclosure controls, and the copy button's text target.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (REPO / "tests" / "golden" / "web" / "payloads"
          / "agent-box-settings" / "bin" / "agent-box-settings")
_loaded = 0


def load_daemon(state):
    global _loaded
    _loaded += 1
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = str(state / "env")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    try:
        name = "agent_box_settings_gpg_panel_%d" % _loaded
        loader = importlib.machinery.SourceFileLoader(name, str(DAEMON))
        spec = importlib.util.spec_from_loader(name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class GpgKeyPanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = pathlib.Path(self.tmp.name) / "agent-box"
        self.state.mkdir()

    def write_key(self, body="mDMEfixture+/="):
        text = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
                "Comment: %s\n\n"
                "mDMEfixture+/=\n"
                "-----END PGP PUBLIC KEY BLOCK-----\n") % body
        (self.state / "gpg-public-key.asc").write_text(text, encoding="ascii")
        return text

    def test_missing_key_explains_that_it_is_not_ready(self):
        panel = load_daemon(self.state).render_gpg_section()
        self.assertIn("Encrypted handoff", panel)
        self.assertIn("public key is not ready", panel)
        self.assertNotIn("gpg-key-pane", panel)
        self.assertNotIn("data-copy-target", panel)

    def test_armored_key_is_in_a_toggle_pane_with_copy_target(self):
        key = self.write_key()
        module = load_daemon(self.state)
        self.assertEqual(module.read_gpg_public_key(), key)
        panel = module.render_gpg_section()
        self.assertIn('data-toggle="gpg-key-pane"', panel)
        self.assertIn('aria-controls="gpg-key-pane"', panel)
        self.assertIn('aria-expanded="true"', panel)
        self.assertIn('id="gpg-public-key"', panel)
        self.assertIn('data-copy-target="gpg-public-key"', panel)
        self.assertIn('aria-label="Copy armored public key"', panel)
        self.assertIn("-----BEGIN PGP PUBLIC KEY BLOCK-----", panel)
        self.assertIn("-----END PGP PUBLIC KEY BLOCK-----", panel)
        self.assertNotIn("PRIVATE KEY", panel)

    def test_key_text_is_html_escaped(self):
        self.write_key("&lt;script&gt;")
        # Write literal markup, not entities, to prove the renderer owns the
        # escaping boundary. An armor Comment header may contain text.
        path = self.state / "gpg-public-key.asc"
        path.write_text(path.read_text(encoding="ascii").replace(
            "&lt;script&gt;", "<script>"), encoding="ascii")
        panel = load_daemon(self.state).render_gpg_section()
        self.assertNotIn("Comment: <script>", panel)
        self.assertIn("Comment: &lt;script&gt;", panel)

    def test_non_armor_and_oversized_files_are_refused(self):
        path = self.state / "gpg-public-key.asc"
        path.write_text("not a key\n", encoding="ascii")
        module = load_daemon(self.state)
        self.assertEqual(module.read_gpg_public_key(), "")

        path.write_bytes(b"x" * (module.GPG_PUBLIC_KEY_MAX + 1))
        module = load_daemon(self.state)
        self.assertEqual(module.read_gpg_public_key(), "")


if __name__ == "__main__":
    unittest.main()
