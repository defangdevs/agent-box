#!/usr/bin/env python3
"""The webhook panel says WHICH kind of nothing it is (issue #425).

A box with no webhook panel used to render an empty string, and that is
how #425 arrived: "fresh box, no webhook panel". The page could not tell
its operator whether the feature was off on purpose or wired up wrong, so
answering the report had to start with a bug hunt through two renderers.

These tests pin the three answers webhook_unavailable() gives, including
the two real incidents that produced them: #426 (the settings unit was
given no AGENT_BOX_WEBHOOK_SCRIPT while the CLI wrapper had one) and
#425's own second half (the declared webhook.py had no owner, so the path
named a file nothing installed).

The subject is tests/golden/web/payloads/.../agent-box-settings, not
modules/src/settings-daemon.py: the daemon is shipped with the env-store
library prepended, so the source file alone does not import. The golden
payload is that assembled article, and the golden-snapshot check fails if
it ever stops matching what the module builds.
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


def daemon_with(**env):
    """Import the settings daemon under exactly this environment.

    Its webhook constants are read at import, which is the whole point:
    what the unit hands the daemon is what the panel has to work with.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "/usr/bin:/bin")
    # The two the daemon refuses to start without, and neither has
    # anything to do with webhooks.
    os.environ["AGENT_BOX_SETTINGS_ENV_FILE"] = os.path.join(
        tempfile.gettempdir(), "agent-box-settings-under-test.env")
    os.environ["AGENT_BOX_SETTINGS_USER"] = "agent"
    os.environ.update(env)
    try:
        # An explicit SourceFileLoader because the payload is installed
        # under its command name, with no .py for importlib to recognise.
        loader = importlib.machinery.SourceFileLoader(
            "agent_box_settings_under_test", str(DAEMON))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class WebhookPanelState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = os.path.join(self.tmp.name, "webhook.py")
        with open(self.script, "w"):
            pass
        self.state = self.tmp.name

    def test_no_webhook_variables_at_all_reads_as_off(self):
        """The box renders no receiver. Nothing is broken; say so."""
        text = daemon_with().webhook_unavailable()
        self.assertIn("Automations are not enabled", text)
        self.assertIn("administrator", text)

    def test_state_dir_without_script_names_the_missing_variable(self):
        """Issue #426 exactly: the settings unit got the state dir and the
        URL, and AGENT_BOX_WEBHOOK_SCRIPT went to the CLI wrapper instead.
        The panel vanished on every native box and said nothing."""
        text = daemon_with(
            AGENT_BOX_WEBHOOK_STATE_DIR=self.state,
            AGENT_BOX_WEBHOOK_URL="https://box.example/agent/webhook",
        ).webhook_unavailable()
        self.assertIn("AGENT_BOX_WEBHOOK_SCRIPT", text)
        self.assertNotIn("Automations are not enabled", text)

    def test_script_without_state_dir_names_the_missing_variable(self):
        text = daemon_with(
            AGENT_BOX_WEBHOOK_SCRIPT=self.script).webhook_unavailable()
        self.assertIn("AGENT_BOX_WEBHOOK_STATE_DIR", text)

    def test_a_pin_that_is_not_on_disk_names_the_path(self):
        """Issue #425's second half: webhook.script named /etc/agent-box/
        webhook.py and nothing installed it, so every button here forked a
        file that was not there."""
        missing = os.path.join(self.tmp.name, "gone.py")
        text = daemon_with(
            AGENT_BOX_WEBHOOK_SCRIPT=missing,
            AGENT_BOX_WEBHOOK_STATE_DIR=self.state,
        ).webhook_unavailable()
        self.assertIn(missing, text)
        self.assertIn("is not there", text)

    def test_a_working_pin_leaves_the_real_panel_alone(self):
        module = daemon_with(
            AGENT_BOX_WEBHOOK_SCRIPT=self.script,
            AGENT_BOX_WEBHOOK_STATE_DIR=self.state,
        )
        self.assertIsNone(module.webhook_unavailable())
        self.assertTrue(module.WEBHOOKS)

    def test_event_expiry_labels_explain_their_meaning(self):
        module = daemon_with()
        self.assertEqual(module.display_event_expiry("45 minutes"),
                         "Expires in 45 minutes")
        self.assertEqual(module.display_event_expiry("never (pinned)"),
                         "Always active")

    def test_no_sender_yet_is_prose_and_not_a_one_row_table(self):
        """The table lists the senders that exist. With none, it is a
        header over a paragraph of documentation — and `.tbl li` is a flex
        row, so that paragraph came out laid across the row as columns.
        The explanation belongs in the note above the table instead."""
        module = daemon_with(
            AGENT_BOX_WEBHOOK_SCRIPT=self.script,
            AGENT_BOX_WEBHOOK_STATE_DIR=self.state,
            AGENT_BOX_WEBHOOK_URL="https://box.example/agent/webhook",
        )
        html = module.render_webhook_endpoint()
        self.assertNotIn("Webhook URL", html)
        self.assertNotIn("<ul", html)
        self.assertIn("No service is connected yet", html)
        # The command names its source: the panel is not GitHub-only, and
        # `setup` leaning on its default hid the argument entirely.
        self.assertIn("agent-box-webhook setup SOURCE", html)
        self.assertIn("replacing SOURCE", html)
        self.assertIn("<code>github</code>", html)

    def test_a_configured_sender_gets_the_table_back(self):
        """And the same paragraph still says what a webhook is, without
        making the reader take GitHub's word for it."""
        with open(os.path.join(self.state, "sources.json"), "w") as fh:
            fh.write('{"defaultSource": "stripe",'
                     ' "sources": {"stripe": {"secretFile": "stripe.secret"}}}')
        module = daemon_with(
            AGENT_BOX_WEBHOOK_SCRIPT=self.script,
            AGENT_BOX_WEBHOOK_STATE_DIR=self.state,
            AGENT_BOX_WEBHOOK_URL="https://box.example/agent/webhook",
        )
        html = module.render_webhook_endpoint()
        self.assertIn("Webhook URL", html)
        self.assertIn("https://box.example/agent/webhook/stripe", html)
        self.assertNotIn("No sender is set up yet", html)
        self.assertIn("Connect GitHub or another service", html)

    def test_the_explanation_is_rendered_as_the_webhook_section(self):
        """Under its own heading, so an operator looking for the panel
        finds the reason where the panel would have been."""
        module = daemon_with()
        html = module.WEBHOOK_UNAVAILABLE_TPL.format(
            text=module.webhook_unavailable())
        self.assertIn("<h2>Automations</h2>", html)
        self.assertIn("Automations are not enabled", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
