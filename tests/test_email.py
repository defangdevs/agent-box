#!/usr/bin/env python3
"""The SMTP adapter must preserve envelope ownership and retry identity."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
from types import SimpleNamespace
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
                with self.assertRaises(mail.PermanentMessageError):
                    mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))
        with mock.patch.dict(os.environ, {'ORIGINAL_RECIPIENT': self.recipient}):
            with self.assertRaises(mail.PermanentMessageError):
                mail.deliver(dict(self.cfg, maxMessageBytes=4), self.user, io.BytesIO(b'x' * 70000))
            with mock.patch.object(mail.subprocess, 'run', return_value=mock.Mock(returncode=1)):
                with self.assertRaises(OSError):
                    mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))

    def test_permanent_message_refusal_and_temporary_service_exit_status(self):
        raw = (b'Content-Type: multipart/mixed; boundary=x\r\n\r\n'
               + b'--x\r\nContent-Type: text/plain\r\n\r\nx\r\n' * 101 + b'--x--\r\n')
        with self.assertRaises(mail.PermanentMessageError):
            mail.normalized_message(raw, self.recipient, '')
        for error, expected in ((mail.PermanentMessageError('invalid message'), 65),
                                (ValueError('invalid configuration'), 75),
                                (OSError('bus unavailable'), 75)):
            with mock.patch.object(mail, 'main', side_effect=error), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mail.cli(), expected)

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
    def email_config(self):
        return {'domain': 'box.example', 'users': {'alice': {'email': True}},
                'email': {'enable': True, 'certificateFile': '/etc/mail/cert.pem',
                          'keyFile': '/etc/mail/key.pem'}}

    def test_default_domains_follow_first_boot_but_explicit_domains_survive(self):
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_fake_profile(tmp)
            for domain in ('', 'auto', 'sslip.io'):
                for explicit in (None, ['mail.example']):
                    with self.subTest(domain=domain, explicit=explicit):
                        data = self.email_config()
                        data['domain'] = domain
                        if explicit is not None:
                            data['email']['domains'] = explicit
                        path = Path(tmp) / 'config.json'
                        path.write_text(json.dumps(data))
                        spec = mod.Spec(data, profile=profile)
                        args = SimpleNamespace(config=str(path), settle_delay=0)
                        with mock.patch.object(mod, 'settle_public_ip', return_value='203.0.113.7'), \
                                mock.patch.object(mod, 'resolve_web_password_source', return_value=(None, None)), \
                                contextlib.redirect_stdout(io.StringIO()):
                            mod.first_boot(spec, args)
                        self.assertEqual(spec.email_domains, explicit or ['203-0-113-7.sslip.io'])
                        tree = mod.Renderer(spec, profile).render()
                        self.assertEqual(json.loads(tree.files['/etc/agent-box/email.json'][0])['domains'],
                                         spec.email_domains)

    def test_runtime_upgrade_refreshes_intake_without_json_changes(self):
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_fake_profile(tmp)
            root = Path(tmp) / 'host'
            spec = mod.Spec(self.email_config(), profile=profile)
            executable = profile / 'bin/agent-box-email'
            (profile / 'sbin').mkdir()
            smtpd = profile / 'sbin/smtpd'
            for path in (executable, smtpd):
                path.unlink(missing_ok=True)
                target = Path(tmp) / (path.name + '-v1')
                target.write_text('v1')
                path.symlink_to(target)

            def render():
                return mod.Renderer(spec, profile, root=root).render()

            def save(tree):
                for name in ('email.json', 'email-runtime'):
                    path = root / 'etc/agent-box' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(tree.files[str(path)][0])

            save(render())
            self.assertFalse(render().email_restart)
            for path in (executable, smtpd):
                target = Path(tmp) / (path.name + '-v2')
                target.write_text('v2')
                path.unlink()
                path.symlink_to(target)
                updated = render()
                self.assertTrue(updated.email_restart)
                save(updated)
                self.assertFalse(render().email_restart)

    def test_existing_account_home_must_match_before_committing(self):
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_fake_profile(tmp)
            spec = mod.Spec(self.email_config(), profile=profile)
            for home, failures in (('/srv/old-home', 1), ('/home/alice', 0)):
                act = mod.Activation()
                with mock.patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=home)), \
                        contextlib.redirect_stderr(io.StringIO()):
                    mod.validate_render(spec, mod.Tree(), act, profile)
                self.assertEqual(len(act.failures), failures)

    def test_activation_keeps_enabled_sources_and_stops_removed_sources(self):
        mod = load_agentbox()
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_fake_profile(tmp)
            data = self.email_config()
            data.update(protectMemory=False, containers={'enable': False})
            data['users']['bob'] = {}
            spec = mod.Spec(data, profile=profile)
            tree = mod.Renderer(spec, profile, root=Path(tmp) / 'host').render()
            enabled = 'agent-box-email-source@alice.service'
            removed = 'agent-box-email-source@bob.service'
            live = lambda prefix: [enabled, removed] if prefix == 'agent-box-email-source@' else []
            act = mod.Activation()
            with mock.patch.object(mod, 'ensure_user'), \
                    mock.patch.object(mod, 'ensure_linger'), \
                    mock.patch.object(mod, 'ensure_group_user'), \
                    mock.patch.object(mod, 'unit_is_enabled', return_value=False), \
                    mock.patch.object(mod, 'active_units', return_value=[]), \
                    mock.patch.object(mod, 'systemd_units_live', side_effect=live), \
                    mock.patch.object(mod, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                failures = mod.activate(spec, tree, [], set(), act)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(failures, set())
            self.assertEqual(act.failures, [])
            self.assertNotIn(['systemctl', 'stop', enabled], commands)
            self.assertIn(['systemctl', 'start', enabled], commands)
            self.assertIn(['systemctl', 'stop', removed], commands)

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
            self.assertEqual(links['/etc/agent-box/email-tools/smtpctl'], str(profile / 'bin/smtpctl'))
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
