#!/usr/bin/env python3
"""The SMTP adapter must preserve envelope ownership and retry identity."""
import base64
import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import dkim
import dns.exception
import dns.resolver
import nacl.signing

from test_agentbox import load_agentbox, build_fake_profile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("email_adapter", ROOT / "modules/src/email-cli.py")
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


class EmailAdapter(unittest.TestCase):
    def setUp(self):
        marker = mock.patch.object(mail, 'verification_marker', return_value=mail.VERIFIED_POLICY)
        marker.start()
        self.addCleanup(marker.stop)
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.cfg = {"domains": ["40-70-78-11.defangstation.com"], "users": [self.user],
                    "certificateFile": "/etc/mail/cert.pem", "keyFile": "/etc/mail/key.pem",
                    "maxMessageBytes": 2097152, "webhookScript": "/store/webhook.py",
                    "deliveryCommand": "/store/email", "smtpd": "/store/smtpd"}
        self.recipient = self.user + '@' + self.cfg["domains"][0]
        self.raw = (b'Received: from sender by box id abc123; Mon, 26 Oct 2026 12:00:00 +0000\r\n'
                    b'X-Agent-Box-Email-Verified: exact-domain-v1\r\n'
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
        self.assertEqual(event['baseRecipient'], self.recipient)
        self.assertEqual(event['recipientTag'], '')
        self.assertEqual(event['headers']['to'], ['wrong@example.org'])
        self.assertTrue(event['senderAuthenticated'])
        self.assertEqual(event['authenticationPolicy'], 'exact-domain-v1')
        self.assertEqual(event['title'], 'Task')
        self.assertIn('Do the work', event['text'])
        self.assertEqual(event['attachments'][0]['filename'], '../../bad.sh')
        self.assertEqual(base64.b64decode(event['rawMimeBase64']), self.raw)

    def test_unverified_legacy_queue_and_duplicate_markers_never_reach_bus(self):
        marker = b'X-Agent-Box-Email-Verified: exact-domain-v1\r\n'
        for raw in (self.raw.replace(marker, b''), self.raw.replace(marker, marker * 2)):
            with self.assertRaises(mail.PermanentMessageError):
                mail.normalized_message(raw, self.recipient, 'envelope@example.org')
        with mock.patch.object(mail, 'verification_marker', return_value=mail.VERIFIED_POLICY + ' server-value'):
            with self.assertRaises(mail.PermanentMessageError):
                mail.normalized_message(self.raw, self.recipient, 'envelope@example.org')

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

    def test_tagged_delivery_preserves_envelope_routing_and_retry_identity(self):
        calls = []
        for tag in ('GitHub', 'build+linux', "a!#$%&'*-/=?^_`{|}~.z"):
            recipient = self.recipient.replace('@', '+' + tag + '@')
            with mock.patch.dict(os.environ, {'ORIGINAL_RECIPIENT': recipient,
                                             'EXTENSION': 'forged', 'SENDER': 'sender@example.org'}):
                with mock.patch.object(mail.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                    mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))
                    mail.deliver(self.cfg, self.user, io.BytesIO(self.raw))
            first, retry = run.call_args_list
            self.assertEqual(first, retry)
            event = json.loads(first.kwargs['input'])
            self.assertEqual(event['recipient'], recipient)
            self.assertEqual(event['baseRecipient'], self.recipient)
            self.assertEqual(event['recipientTag'], tag)
            self.assertEqual(event['headers']['to'], ['wrong@example.org'])
            self.assertEqual(base64.b64decode(event['rawMimeBase64']), self.raw)
            calls.append(first.args[0][-1])
        self.assertEqual(len(set(calls)), 3)

    def test_other_user_domain_invalid_tag_and_oversize_are_refused(self):
        for recipient in ('other@' + self.cfg['domains'][0], self.user + '@elsewhere.example',
                          'other+github@' + self.cfg['domains'][0],
                          self.user + '+github@elsewhere.example',
                          self.user + '+@' + self.cfg['domains'][0],
                          self.user + '+bad tag@' + self.cfg['domains'][0],
                          self.user + '+bad:tag@' + self.cfg['domains'][0],
                          self.user + '+tag@evil@' + self.cfg['domains'][0], ''):
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
        raw = (b'X-Agent-Box-Email-Verified: exact-domain-v1\r\nFrom: a@example.org\r\n'
               b'Content-Type: multipart/mixed; boundary=x\r\n\r\n'
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
        self.assertIn('listen on socket filter email_auth', cfg)
        self.assertEqual(cfg.count('filter email_auth'), 4)
        with self.assertRaises(ValueError):
            mail.validate_config(dict(self.cfg, keyFile='/tmp/key"; evil'))

class VerificationMarker(unittest.TestCase):
    def test_failed_atomic_publication_leaves_no_partial_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'token'
            with mock.patch.object(mail, 'VERIFIED_TOKEN', str(path)), mock.patch.object(mail.os, 'replace', side_effect=OSError('injected failure')):
                with self.assertRaises(OSError):
                    mail.verification_marker(create=True)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_token_is_atomic_persistent_and_rejects_unsafe_files(self):
        fstat = os.fstat

        def root_stat(fd):
            return SimpleNamespace(st_uid=0, st_mode=fstat(fd).st_mode)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'token'
            with mock.patch.object(mail, 'VERIFIED_TOKEN', str(path)), mock.patch.object(mail.os, 'fstat', side_effect=root_stat):
                first = mail.verification_marker(create=True)
                self.assertEqual(mail.verification_marker(create=True), first)
                self.assertEqual(mail.verification_marker(), first)
                self.assertEqual(len(first.split()[1]), 64)
                path.chmod(0o666)
                with self.assertRaises(ValueError):
                    mail.verification_marker()
                path.chmod(0o644)
                path.write_text('invalid\n')
                with self.assertRaises(ValueError):
                    mail.verification_marker()
                path.unlink()
                path.symlink_to(Path(tmp) / 'missing')
                with self.assertRaises(OSError):
                    mail.verification_marker()


class SenderAuthentication(unittest.TestCase):
    raw = b'From: Author <author@example.org>\r\nSubject: Signed task\r\n\r\nPrivate body\r\n.dot line\r\n'
    private_key = (ROOT / 'tests/email/dkim-test.key').read_bytes()
    public_key = b''.join((ROOT / 'tests/email/dkim-test.pub').read_bytes().splitlines()[1:-1])

    def signed(self, domain=b'example.org', raw=None, **kw):
        raw = raw or self.raw
        return dkim.sign(raw, b'test', domain, self.private_key, **kw) + raw

    def dns_answer(self, name, qtype, **kw):
        if name.lower() == 'test._domainkey.example.org.':
            return [SimpleNamespace(strings=[b'v=DKIM1; k=rsa; p=' + self.public_key])]
        raise dns.resolver.NXDOMAIN()

    def verdict(self, raw=None, sender='forwarder@other.example', result='fail', ip='192.0.2.1'):
        with mock.patch.object(mail.spf, 'check2', return_value=(result, 'test')):
            with mock.patch.object(mail.dns.resolver, 'resolve', side_effect=self.dns_answer):
                return mail.authenticate_message(raw or self.raw, ip, sender, 'mx.example.org')

    def test_spf_pass_requires_exact_from_alignment(self):
        self.assertEqual(self.verdict(sender='author@example.org', result='pass'), 'proceed')
        for sender in ('author@evil.example', 'author@sub.example.org', ''):
            self.assertIn('550', self.verdict(sender=sender, result='pass'))
        self.assertIn('550', self.verdict())
        for result in ('fail', 'softfail', 'neutral', 'none', 'permerror'):
            self.assertIn('550', self.verdict(sender='author@example.org', result=result))
        self.assertIn('451', self.verdict(sender='author@example.org', result='temperror'))

    def test_temporary_spf_failure_is_preserved_at_the_signature_cap(self):
        raw = b'DKIM-Signature: invalid\r\n' * 9 + self.raw
        self.assertIn('451', self.verdict(raw, sender='author@example.org', result='temperror'))
        self.assertIn('550', self.verdict(raw, sender='author@example.org', result='fail'))

    def test_local_enqueue_requires_dkim_even_with_an_aligned_envelope(self):
        forged = b'X-Agent-Box-Email-Verified: exact-domain-v1 public-host-value\r\n' + self.raw
        self.assertIn('550', self.verdict(forged, sender='author@example.org', result='pass', ip='local'))
        self.assertEqual(self.verdict(self.signed(), sender='author@example.org', ip='local'), 'proceed')

    def test_headers_are_bounded_before_parsing_but_body_is_independent(self):
        for separator in (b'\r\n', b'\n'):
            raw = separator.join([b'From: a@example.org'] + [b'X: x'] * 17000) + separator * 2 + b'body'
            with mock.patch.object(mail, 'BytesParser') as parser:
                self.assertIn('550', self.verdict(raw, sender='a@example.org', result='pass'))
                parser.assert_not_called()
        prefix = b'From: a@example.org\r\nX-Pad: '
        headers = prefix + b'x' * (mail.MAX_HEADER_BYTES - len(prefix) - 4) + b'\r\n\r\n'
        self.assertEqual(mail.author_domain(headers + b'body' * 500000), 'example.org')
        with self.assertRaises(mail.PermanentMessageError):
            mail.author_domain(headers.replace(b'X-Pad: ', b'X-Pad: x'))

    def test_hostile_rsa_parameters_never_reach_the_rsa_operation(self):
        def der(tag, value):
            length = len(value)
            size = length.to_bytes((length.bit_length() + 7) // 8, 'big')
            encoded_size = bytes([length]) if length < 128 else bytes([0x80 | len(size)]) + size
            return bytes([tag]) + encoded_size + value

        def integer(value):
            return der(2, b'\0' + value.to_bytes((value.bit_length() + 7) // 8, 'big'))

        for bits, exponent in ((1023, 65537), (4097, 65537), (8192, 65537), (2048, 2**65 + 1), (2048, 2)):
            modulus = 2**(bits - 1) + 1
            record = b'v=DKIM1; k=rsa; p=' + base64.b64encode(der(0x30, integer(modulus) + integer(exponent)))
            answer = [SimpleNamespace(strings=[record])]
            with mock.patch.object(mail.dns.resolver, 'resolve', return_value=answer):
                with mock.patch.object(mail.dkim, 'RSASSA_PKCS1_v1_5_verify') as rsa:
                    self.assertIn('550', mail.authenticate_message(self.signed(), '192.0.2.1', '', 'mx.example.org'))
                    rsa.assert_not_called()

    def test_actual_spf_library_ipv4_and_ipv6(self):
        def lookup(name, qtype, *args):
            return [((name, 'TXT'), [b'v=spf1 ip4:192.0.2.1 ip6:2001:db8::1 -all'])] if qtype == 'TXT' else []
        with mock.patch.object(mail.spf, 'DNSLookup', side_effect=lookup):
            for ip in ('192.0.2.1', '2001:db8::1'):
                self.assertEqual(mail.authenticate_message(self.raw, ip, 'author@example.org', 'mx.example.org'), 'proceed')
            self.assertIn('550', mail.authenticate_message(self.raw, '192.0.2.2', 'author@example.org', 'mx.example.org'))

    def test_real_dkim_survives_spf_failure_and_null_sender(self):
        for sender in ('forwarder@other.example', '', 'author@example.org'):
            self.assertEqual(self.verdict(self.signed(), sender=sender), 'proceed')
        # A failed first signature must not hide a later valid signature.
        self.assertEqual(self.verdict(self.signed(b'evil.example', raw=self.signed())), 'proceed')

    def test_tampered_unaligned_partial_and_sha1_signatures_are_rejected(self):
        signed = self.signed()
        for raw in (signed.replace(b'Private body', b'Forged task'),
                    self.signed(b'evil.example'), self.signed(length=True),
                    self.signed(signature_algorithm=b'rsa-sha1'),
                    b'DKIM-Signature: invalid\r\n' + self.raw,
                    b'DKIM-Signature: d=example.org; d=example.org\r\n' + self.raw,
                    b'DKIM-Signature: invalid\r\n' * 9 + signed):
            self.assertIn('550', self.verdict(raw))

    def test_ed25519_signature(self):
        key = nacl.signing.SigningKey(bytes(range(32)))
        raw = dkim.sign(self.raw, b'test', b'example.org', base64.b64encode(bytes(key)),
                        signature_algorithm=b'ed25519-sha256') + self.raw
        answer = [SimpleNamespace(strings=[b'v=DKIM1; k=ed25519; p=' + base64.b64encode(bytes(key.verify_key))])]
        with mock.patch.object(mail.dns.resolver, 'resolve', return_value=answer):
            self.assertEqual(mail.authenticate_message(raw, '192.0.2.1', '', 'mx.example.org'), 'proceed')

    def test_missing_ambiguous_malformed_and_group_authors_are_rejected(self):
        for prefix in (b'', b'From: missing-domain\r\n', b'From: a@example.org, b@example.org\r\n',
                       b'From: a@example.org\r\nFrom: b@example.org\r\n',
                       b'From: List: a@example.org;\r\n', b'From: a@bad..example.org\r\n'):
            self.assertIn('550', self.verdict(prefix + b'\r\nbody', sender='a@example.org', result='pass'))

    def test_forged_authentication_headers_do_not_help(self):
        raw = (b'Authentication-Results: box; dkim=pass; spf=pass\r\n'
               b'X-Agent-Box-Email-Verified: exact-domain-v1\r\n' + self.raw)
        self.assertIn('550', self.verdict(raw))

    def test_dns_outage_defers_but_missing_key_rejects(self):
        with mock.patch.object(mail.dns.resolver, 'resolve', side_effect=dns.exception.Timeout):
            self.assertIn('451', mail.authenticate_message(self.signed(), '192.0.2.1', '', 'mx.example.org'))
        with mock.patch.object(mail.dns.resolver, 'resolve', side_effect=dns.resolver.NXDOMAIN):
            self.assertIn('550', mail.authenticate_message(self.signed(), '192.0.2.1', '', 'mx.example.org'))


class FilterProtocol(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        marker = mock.patch.object(mail, 'verification_marker', return_value=mail.VERIFIED_POLICY)
        marker.start()
        self.addCleanup(marker.stop)
        self.output = io.BytesIO()
        self.auth = mail.EmailAuthFilter({'maxMessageBytes': 1024}, self.output)
        self.auth.feed(b'config|ready')

    def request(self, phase, data=b'', session=b'one'):
        self.auth.feed(b'filter|0.7|0|smtp-in|' + phase + b'|' + session + b'|token|' + data)

    def report(self, event, data=b'', session=b'one'):
        self.auth.feed(b'report|0.7|0|smtp-in|' + event + b'|' + session + b'|' + data)

    def message(self, raw=SenderAuthentication.raw, sender=b'author@example.org'):
        self.request(b'connect', b'rdns|[2001:db8::1]')
        self.report(b'link-identify', b'EHLO|mx.example.org')
        self.report(b'tx-begin', b'msg')
        self.report(b'tx-mail', b'msg|ok|' + sender)
        for line in raw.split(b'\r\n')[:-1]:
            self.request(b'data-line', b'.' + line if line.startswith(b'.') else line)
        self.request(b'data-line', b'.')
        self.request(b'commit')

    async def complete(self):
        if self.auth.tasks:
            await asyncio.gather(*self.auth.tasks)

    async def test_protocol_preserves_original_bytes_and_trusted_envelope(self):
        with mock.patch.object(mail, 'authenticate_message', return_value='proceed') as verify:
            self.message(sender=b'quoted|local@example.org')
            await self.complete()
        verify.assert_called_once_with(SenderAuthentication.raw, '2001:db8::1',
                                       'quoted|local@example.org', 'mx.example.org')
        self.assertIn(b'filter-result|one|token|proceed\n', self.output.getvalue())
        self.assertIn(b'filter-dataline|one|token|..dot line\n', self.output.getvalue())

    async def test_client_markers_and_folded_continuations_are_removed(self):
        raw = (b'x-agent-box-email-verified: forged\r\n folded forgery\r\n'
               b'X-Agent-Box-Email-Verified: another\r\n' + SenderAuthentication.raw)
        with mock.patch.object(mail, 'authenticate_message', return_value='proceed'):
            self.message(raw)
            await self.complete()
        data = self.output.getvalue()
        self.assertNotIn(b'forged', data)
        self.assertNotIn(b'another', data)
        self.assertEqual(data.count(b'X-Agent-Box-Email-Verified: exact-domain-v1'), 1)

    async def test_negative_verdict_and_verifier_crash_fail_closed(self):
        for verdict in ('reject|550 5.7.1 Failed', 'reject|451 4.7.1 Retry'):
            with mock.patch.object(mail, 'authenticate_message', return_value=verdict):
                self.message()
                await self.complete()
            self.assertIn(verdict.encode(), self.output.getvalue())
        with mock.patch.object(mail, 'authenticate_message', side_effect=RuntimeError('private mail')):
            self.message()
            await self.complete()
        self.assertIn(b'451', self.output.getvalue())
        self.assertNotIn(b'private mail', self.output.getvalue())

    async def test_reset_overflow_and_connection_limit(self):
        self.auth.limit = 50
        self.message()
        self.assertIn(b'552', self.output.getvalue())
        self.report(b'tx-reset', b'msg')
        self.assertEqual(self.auth.sessions[b'one']['message']['raw'], b'')
        self.auth.MAX_SESSIONS = 1
        self.request(b'connect', b'rdns|192.0.2.2', session=b'two')
        self.assertIn(b'421', self.output.getvalue())

    async def test_line_limit_boundary_and_next_transaction(self):
        for size in (mail.MAX_DATA_LINE_BYTES, mail.MAX_DATA_LINE_BYTES + 1):
            self.output.seek(0)
            self.output.truncate()
            self.auth = mail.EmailAuthFilter({'maxMessageBytes': 2097152}, self.output)
            with mock.patch.object(mail, 'authenticate_message', return_value='proceed') as verify:
                self.message(SenderAuthentication.raw + b'x' * size + b'\r\n')
                await self.complete()
                if size == mail.MAX_DATA_LINE_BYTES:
                    verify.assert_called_once()
                    self.assertIn(b'filter-result|one|token|proceed', self.output.getvalue())
                else:
                    verify.assert_not_called()
                    self.assertIn(b'filter-result|one|token|reject|550 5.6.0', self.output.getvalue())
                self.assertTrue(all(len(line) < 65536 for line in self.output.getvalue().splitlines()))
                self.message()
                await self.complete()
                self.assertTrue(self.output.getvalue().endswith(b'filter-result|one|token|proceed\n'))

    async def test_busy_verifier_defers_and_disconnect_discards_late_result(self):
        await self.auth.workers.acquire()
        await self.auth.workers.acquire()
        self.message()
        await self.complete()
        self.assertIn(b'451', self.output.getvalue())
        self.auth.workers.release()
        self.auth.workers.release()
        started, release = threading.Event(), threading.Event()

        def blocked_verify(*args):
            started.set()
            if not release.wait(5):
                raise TimeoutError('test verifier was not released')
            return 'proceed'

        with mock.patch.object(mail, 'authenticate_message', side_effect=blocked_verify):
            try:
                self.message()
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                self.report(b'link-disconnect')
                size = len(self.output.getvalue())
            finally:
                release.set()
                await self.complete()
        self.assertEqual(len(self.output.getvalue()), size)

    async def test_reader_drains_oversized_line_without_echoing_an_oversized_reply(self):
        frames = [b'config|ready', b'filter|0.7|0|smtp-in|connect|one|token|rdns|192.0.2.1',
                  b'report|0.7|0|smtp-in|tx-begin|one|message',
                  b'report|0.7|0|smtp-in|tx-mail|one|message|ok|a@example.org']
        for line in (b'From: a@example.org', b'', b'x' * 65534, b'.'):
            frames.append(b'filter|0.7|0|smtp-in|data-line|one|token|' + line)
        frames.append(b'filter|0.7|0|smtp-in|commit|one|token')
        data = b'\n'.join(frames) + b'\n'
        read_fd, write_fd = os.pipe()

        def produce():
            with os.fdopen(write_fd, 'wb') as output:
                output.write(data)

        with os.fdopen(read_fd, 'rb') as source, tempfile.TemporaryFile() as output:
            producer = asyncio.create_task(asyncio.to_thread(produce))
            with mock.patch.object(mail.sys, 'stdin', SimpleNamespace(buffer=source)), mock.patch.object(mail.sys, 'stdout', SimpleNamespace(buffer=output)):
                with mock.patch.object(mail, 'authenticate_message', return_value='proceed') as verify:
                    await mail.run_auth_filter({'maxMessageBytes': 2097152})
            await producer
            output.seek(0)
            replies = output.read()
        self.assertIn(b'filter-result|one|token|reject|550 5.6.0', replies)
        self.assertTrue(all(len(line) < 65536 for line in replies.splitlines()))
        verify.assert_not_called()

    async def test_nonblocking_output_preserves_partial_writes_under_backpressure(self):
        read_fd, write_fd = os.pipe()
        output = mail.FilterOutput(write_fd)
        data = b'filter-dataline|one|token|X-Pad: x\n' * 16000
        release = threading.Event()
        write = os.write

        def consume():
            with os.fdopen(read_fd, 'rb') as source:
                if not release.wait(10):
                    raise TimeoutError('writer never encountered backpressure')
                return source.read()

        def pressured_write(fd, value):
            try:
                return write(fd, value)
            except BlockingIOError:
                release.set()
                raise

        consumer = asyncio.create_task(asyncio.to_thread(consume))
        try:
            with mock.patch.object(mail.os, 'write', side_effect=pressured_write):
                output.write(data)
                await asyncio.wait_for(output.drain(), 10)
            self.assertTrue(release.is_set())
        finally:
            release.set()
            os.close(write_fd)
        self.assertEqual(await consumer, data)


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
            self.assertIn('StateDirectory=agent-box-email', text('/etc/systemd/system/agent-box-email.service'))
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
