#!/usr/bin/env python3
"""The SMTP adapter must preserve envelope ownership and retry identity."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest import mock

from test_agentbox import load_agentbox, build_fake_profile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("email_adapter", ROOT / "modules/src/email-cli.py")
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


class EmailAdapter(unittest.TestCase):
    def setUp(self):
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.cfg = {"domains": ["40-70-78-11.defangstation.com"], "users": [self.user],
                    "certificateFile": "/etc/mail/cert.pem", "keyFile": "/etc/mail/key.pem",
                    "maxMessageBytes": 2097152, "webhookScript": "/store/webhook.py",
                    "deliveryCommand": "/store/email", "smtpd": "/store/smtpd"}
        self.recipient = self.user + '@' + self.cfg["domains"][0]
        self.raw = (b'Received: from sender by box id abc123; Mon, 26 Oct 2026 12:00:00 +0000\r\n'
                    b'From: Claimed <someone@example.org>\r\nTo: wrong@example.org\r\n'
                    b'Authentication-Results: forged; dkim=pass\r\n'
                    b'Subject: =?utf-8?q?Task?=\r\nMessage-ID: <duplicate@example.org>\r\n'
                    b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n'
                    b'--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nDo the work\r\n'
                    b'--x\r\nContent-Type: application/octet-stream\r\n'
                    b'Content-Disposition: attachment; filename="../../bad.sh"\r\n'
                    b'Content-Transfer-Encoding: base64\r\n\r\nZWNobyBubw==\r\n--x--\r\n')

    def test_mime_and_claimed_identity_are_preserved_as_data(self):
        event = mail.normalized_message(self.raw, self.recipient, 'envelope@example.org')
        self.assertEqual(event['recipient'], self.recipient)
        self.assertEqual(event['headers']['to'], ['wrong@example.org'])
        self.assertFalse(event['senderAuthenticated'])
        self.assertEqual(event['title'], 'Task')
        self.assertIn('Do the work', event['text'])
        self.assertEqual(event['attachments'][0]['filename'], '../../bad.sh')
        self.assertEqual(base64.b64decode(event['rawMimeBase64']), self.raw)

    def test_retry_is_identical_but_new_smtp_transaction_is_distinct(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw['input']))
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {'ORIGINAL_RECIPIENT': self.recipient, 'SENDER': 'claim@example.org'}):
            with mock.patch.object(mail.subprocess, 'run', side_effect=run):
                mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))
                mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))
                mail.deliver(self.cfg, self.user, io.BytesIO(self.raw.replace(b'id abc123', b'id def456')))
        self.assertEqual(calls[0], calls[1])
        self.assertNotEqual(calls[0][0][-1], calls[2][0][-1])
        self.assertEqual(json.loads(calls[0][1])['recipient'], self.recipient)

    def test_other_user_domain_subaddress_and_oversize_are_refused(self):
        for recipient in ('other@' + self.cfg['domains'][0], self.user + '@elsewhere.example',
                          self.user + '+session@' + self.cfg['domains'][0], ''):
            with mock.patch.dict(os.environ, {'ORIGINAL_RECIPIENT': recipient}):
                with self.assertRaises(ValueError):
                    mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))
        with mock.patch.dict(os.environ, {'ORIGINAL_RECIPIENT': self.recipient}):
            with self.assertRaises(ValueError):
                mail.deliver(dict(self.cfg, maxMessageBytes=4), self.user, io.BytesIO(b'x' * 70000))
            with mock.patch.object(mail.subprocess, 'run', return_value=mock.Mock(returncode=1)):
                with self.assertRaises(OSError):
                    mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))

    def test_agent_owned_or_writable_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'email.json'
            p.write_text(json.dumps(self.cfg))
            with mock.patch.object(mail, 'CONFIG', str(p)):
                for uid, mode in ((1000, 0o100644), (0, 0o100666)):
                    with mock.patch.object(mail.os, 'fstat', return_value=mock.Mock(st_uid=uid, st_mode=mode)):
                        with self.assertRaisesRegex(ValueError, 'root-owned'):
                            mail.load_config()

    def test_no_envelope_data_is_a_shell_command(self):
        cfg = mail.server_config(self.cfg)
        self.assertNotIn('%{', cfg)
        self.assertNotIn(' relay', cfg)
        self.assertIn('match from any for any reject', cfg)
        self.assertIn('tls pki mail', cfg)
        with self.assertRaises(ValueError):
            mail.validate_config(dict(self.cfg, keyFile='/tmp/key"; evil'))


class NativeEmail(unittest.TestCase):
    def test_opt_in_render_and_disable(self):
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_fake_profile(tmp)
            data = {'domain': 'box.example', 'users': {'alice': {'email': True}, 'bob': {}},
                    'email': {'enable': True, 'certificateFile': '/etc/mail/cert.pem',
                              'keyFile': '/etc/mail/key.pem'}}
            spec = mod.Spec(data, profile=profile)
            tree = mod.Renderer(spec, profile).render()
            def text(path):
                return tree.files[path][0]
            cfg = json.loads(text('/etc/agent-box/email.json'))
            self.assertEqual(cfg['users'], ['alice'])
            self.assertEqual(cfg['domains'], ['box.example'])
            links = dict(tree.links)
            self.assertEqual(links['/etc/agent-box/bin/agent-box-email-smtpd'], str(profile / 'sbin/smtpd'))
            self.assertIn('agent-box-email.service', tree.units)
            self.assertIn('smtpq', text('/etc/tmpfiles.d/agent-box.conf'))
            self.assertIn('User=alice', text('/etc/systemd/system/agent-box-email-source@alice.service'))
            self.assertIn('AGENT_BOX_WEBHOOK_URL=https://box.example/alice/webhook',
                          text('/etc/systemd/system/agent-box-email-source@alice.service'))
            data['email']['enable'] = False
            off = mod.Renderer(mod.Spec(data, profile=profile), profile).render()
            self.assertNotIn('agent-box-email.service', off.units)
            self.assertNotIn('/etc/agent-box/email.json', off.files)
            data['email']['enable'] = True
            data['users']['alice']['email'] = False
            with self.assertRaises(mod.ConfigError):
                mod.Spec(data, profile=profile)


if __name__ == '__main__':
    unittest.main()
