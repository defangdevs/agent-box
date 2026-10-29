"""The reaction adapter never sends a live external request in these tests."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'modules' / 'src'
ACK = types.ModuleType('ack')
ACK.GH_BIN = 'gh'
exec(compile((SRC / 'lib/envstore.py').read_text() + '\n\n' +
             (SRC / 'webhook-ack.py').read_text(), 'webhook-ack.py', 'exec'), ACK.__dict__)


def envelope(event='issue_comment', action='created', **payload):
    return {'source': 'github', 'format': 'github', 'key': 'o/r', 'event': event,
            'payload': {'repository': {'full_name': 'o/r'}, 'action': action, **payload}}


class TestAcknowledgment(unittest.TestCase):
    def test_issue_and_review_comment_endpoints_ignore_payload_urls(self):
        for event, kind in [('issue_comment', 'issues'), ('pull_request_review_comment', 'pulls')]:
            calls = []
            e = envelope(event, comment={'id': 123, 'url': 'https://attacker.invalid/',
                                         'body': '$(touch /tmp/never) `echo never`'})
            ACK.acknowledge(e, api=lambda *args: calls.append(args), token='account-token')
            self.assertEqual(calls, [('account-token', 'POST', 'o/r/%s/comments/123/reactions' % kind)])

    def test_unsupported_and_deleted_events_do_not_read_credentials(self):
        events = [envelope('workflow_run'), envelope(action='deleted', comment={'id': 1}),
                  dict(envelope(), source='slack'), dict(envelope(), format='generic'),
                  dict(envelope(comment={'id': 1}), key='another/repo')]
        for e in events:
            with patch.object(ACK, 'github_token', side_effect=AssertionError('read token')):
                ACK.acknowledge(e)

    def test_target_ids_and_repo_are_strictly_validated(self):
        for ident in (True, -1, 0, '1/reactions', '$(echo 1)', 1.5, None):
            self.assertIsNone(ACK.github_target(envelope(comment={'id': ident})))
        for repo in ('../o/r', 'o/r?x=1', 'o/r\n', 'o/r/../other', 'o/%2e%2e'):
            e = envelope(comment={'id': 1})
            e['key'] = e['payload']['repository']['full_name'] = repo
            self.assertIsNone(ACK.github_target(e))

    def test_issue_and_pr_mentions_use_issue_reactions(self):
        for event, field in [('issues', 'issue'), ('pull_request', 'pull_request')]:
            self.assertEqual(ACK.github_target(envelope(event, 'edited', **{field: {'number': 42}})),
                             ('o/r', 'issues/42', None))

    def test_submitted_review_only_acknowledges_its_own_comments_across_pages(self):
        calls = []
        def api(token, method, path):
            calls.append((method, path))
            if method == 'POST':
                return {}
            if path.endswith('&page=1'):
                return [{'id': i + 1, 'pull_request_review_id': 99 if i == 0 else 100}
                        for i in range(100)]
            return [{'id': 101, 'pull_request_review_id': 99}]
        ACK.acknowledge(envelope('pull_request_review', 'submitted', review={'id': 99},
                                 pull_request={'number': 42}), api=api, token='token')
        self.assertEqual(calls, [('GET', 'o/r/pulls/42/reviews/99/comments?per_page=100&page=1'),
                                 ('POST', 'o/r/pulls/comments/1/reactions'),
                                 ('GET', 'o/r/pulls/42/reviews/99/comments?per_page=100&page=2'),
                                 ('POST', 'o/r/pulls/comments/101/reactions')])

    def test_replays_use_the_same_per_account_remote_reaction(self):
        reactions = set()
        def api(token, method, path):
            reactions.add((token, path, 'eyes'))
            return {'id': 1}
        for action in ('created', 'created', 'edited'):
            ACK.acknowledge(envelope(action=action, comment={'id': 1}), api, 'alice')
        self.assertEqual(len(reactions), 1)
        ACK.acknowledge(envelope(comment={'id': 1}), api, 'bob')
        self.assertEqual(len(reactions), 2)

    def test_token_store_uses_shared_parser_and_process_identity_takes_precedence(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HOME': home}, clear=True):
            directory = Path(home) / '.config' / 'agent-box'
            directory.mkdir(parents=True)
            (directory / 'env').write_text('GH_TOKEN="stored\\\\token"\n')
            self.assertEqual(ACK.github_token(), 'stored\\token')
            os.environ['GH_TOKEN'] = 'profile-token'
            self.assertEqual(ACK.github_token(), 'profile-token')

    def test_fallback_gh_auth_is_argv_and_errors_do_not_expose_token(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(ACK, 'load', return_value=[]):
            result = subprocess.CompletedProcess([], 0, b'token\n')
            with patch.object(ACK.subprocess, 'run', return_value=result) as run:
                self.assertEqual(ACK.github_token(), 'token')
                self.assertEqual(run.call_args.args[0], ['gh', 'auth', 'token', '--hostname', 'github.com'])

    def test_transport_pins_host_and_rejects_redirects(self):
        response = io.BytesIO(b'{"id":1}')
        with patch.object(ACK.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value = response
            ACK.github_api('secret', 'POST', 'o/r/issues/comments/1/reactions')
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.github.com/repos/o/r/issues/comments/1/reactions')
            self.assertEqual(json.loads(request.data), {'content': 'eyes'})
            self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
        self.assertIsNone(ACK.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_outcome_instruction_is_present_in_worker_prompt_and_guide(self):
        spawn = (SRC / 'webhook-spawn.sh').read_text()
        guide = (SRC / 'default-agents-webhook.md').read_text()
        for text in (spawn, guide):
            self.assertIn('reply on the originating', text)
            self.assertIn('verification', text)
            self.assertIn('live owner', text)


if __name__ == '__main__':
    unittest.main()
