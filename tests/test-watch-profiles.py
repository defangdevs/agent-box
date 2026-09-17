#!/usr/bin/env python3
"""Named watches through the real CLI, settings routes, and signed ingress."""
import hashlib
import hmac
import http.client
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

REPO = Path(__file__).resolve().parent.parent
WEBHOOK = Path(sys.argv.pop(1)).resolve()
DAEMON = REPO / 'tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings'


class Watches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.profiles = self.root / '.config/agent-box/profiles'
        self.profiles.mkdir(parents=True)
        for name in ('triage', 'debugger'):
            (self.profiles / (name + '.env')).write_text('HARNESS=claude\n')
        self.env = dict(os.environ, HOME=str(self.root),
                        LOCAL_WEBHOOK_STATE_DIR=str(self.state),
                        LOCAL_WEBHOOK_SESSION='test', LOCAL_WEBHOOK_PORT='0',
                        AGENT_BOX_WEBHOOK_SCRIPT=str(WEBHOOK),
                        AGENT_BOX_ENVSTORE_BIN='false',
                        AGENT_BOX_SESSION_BIN='false',
                        AGENT_BOX_SETTINGS_ENV_FILE=str(self.root / 'env'),
                        AGENT_BOX_SETTINGS_USER='agent',
                        AGENT_BOX_SETTINGS_PORT='0',
                        AGENT_BOX_WEBHOOK_STATE_DIR=str(self.state),
                        AGENT_BOX_WEBHOOK_PYTHON=sys.executable,
                        AGENT_BOX_PROFILES_DIR=str(self.profiles))
        for key in ('CODEX_THREAD_ID', 'LOCAL_WEBHOOK_SPAWN_CONFIG',
                    'AGENT_BOX_HOOK_PROFILE', 'LOCAL_WEBHOOK_SELF'):
            self.env.pop(key, None)
        src = REPO / 'modules/src'
        spawn = (src / 'webhook-spawn.sh').read_text()
        spawn = re.sub(r'@@include:([^@]+)@@',
                       lambda m: (src / m[1]).read_text(), spawn)
        self.spawn = self.root / 'spawn'
        self.spawn.write_text('#!' + shutil.which('bash') + '\n' + spawn)
        self.spawn.chmod(0o700)
        self.env['AGENT_BOX_HOOK_SPAWN_CMD'] = str(self.spawn)
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        try:
            loader = importlib.machinery.SourceFileLoader('watch_settings', str(DAEMON))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            self.d = importlib.util.module_from_spec(spec)
            loader.exec_module(self.d)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.d.HOOK_SPAWN_CMD = str(self.spawn)
        self.d.PROFILES_DIR = str(self.profiles)
        self.d.WEBHOOK_PYTHON = sys.executable
        self.d.WEBHOOK_SCRIPT = str(WEBHOOK)
        self.server = self.d.make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def cli(self, *args):
        return subprocess.run(['bash', str(REPO / 'modules/src/webhook-cli.sh'), *args],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def subscribe(self, name, profile, events):
        # Name before topic catches the wrapper mistaking its value for a topic.
        p = self.cli('subscribe', '--name', name, 'owner/repo', '--deliver-to',
                     'subagent', '--profile', profile, '--when', json.dumps(events))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def entries(self):
        return json.loads((self.state / 'filter.dispatch.json').read_text())['topics']

    def post(self, route, **fields):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        try:
            c.request('POST', self.d.BASE + '/webhooks/' + route,
                      urllib.parse.urlencode(fields),
                      {'Content-Type': 'application/x-www-form-urlencoded'})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 303)
            return r.getheader('Location')
        finally:
            c.close()

    def test_cli_identity_preview_and_policy(self):
        self.subscribe('issues', 'triage', self.d.WATCH_EVENTS['issues'][1])
        self.subscribe('ci', 'debugger', self.d.WATCH_EVENTS['ci'][1])
        self.assertEqual(len(self.entries()), 2)
        for name, profile in [('issues', 'triage'), ('ci', 'debugger')]:
            p = subprocess.run([str(self.spawn), '--resolved-profile',
                                'github:owner/repo', '', name], env=self.env,
                               capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(p.stdout)['profile'], profile)
        with patch.dict(os.environ, self.env, clear=True):
            stamp = self.d.hook_args_stamp()
            for name, profile in [('issues', 'triage'), ('ci', 'debugger')]:
                self.assertEqual(self.d.hook_resolved_profile(
                    'github:owner/repo', '', stamp, name), (profile, ()))
            (self.profiles / 'debugger.env').unlink()
            self.assertEqual(self.d.hook_resolved_profile(
                'github:owner/repo', '', self.d.hook_args_stamp(), 'ci'),
                ('', ('debugger',)))
        p = self.cli('ls')
        names = [e['name'] for e in json.loads(p.stdout)['dispatch']['topics']]
        self.assertEqual(names, ['issues', 'ci'])
        self.subscribe('', 'triage', self.d.WATCH_EVENTS['issues'][1])
        before = self.entries()[:2]
        policy = self.root / 'policy.json'
        rule = {'path': 'event', 'in': ['pull_request']}
        policy.write_text(json.dumps({'github:owner/repo': {'when': rule}}))
        subprocess.run(['bash', str(REPO / 'modules/src/webhook-policy-apply.sh')],
                       env=dict(self.env, AGENT_BOX_WEBHOOK_POLICY_FILE=str(policy)),
                       capture_output=True, check=True)
        self.assertEqual(self.entries()[:2], before)
        self.assertEqual(self.entries()[2]['include'], rule)
        p = self.cli('unsubscribe', 'owner/repo', '--deliver-to', 'subagent')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.entries()), 2)
        p = self.cli('unsubscribe', 'owner/repo', '--name', 'issues',
                     '--deliver-to', 'subagent')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([e['name'] for e in self.entries()], ['ci'])

    def test_http_create_edit_delete_and_invalid_inputs(self):
        for name, events, profile in [('issues', 'issues', 'triage'),
                                      ('ci', 'ci', 'debugger')]:
            self.assertIn('webhook_saved', self.post(
                'save', mode='create', topic='owner/repo', watch_name=name,
                profile=profile, events=events))
        first = self.entries()[0]
        self.assertIn('webhook_saved', self.post(
            'save', mode='edit', topic='owner/repo', watch_name='ci',
            profile='triage', events='reviews', note='review this'))
        self.assertEqual(self.entries()[0], first)
        self.assertEqual(self.entries()[1]['spawnConfig']['profile'], 'triage')
        before = self.entries()
        for fields in [dict(profile='missing'), dict(watch_name='../bad'),
                       dict(events='custom', include='{}'),
                       dict(events='custom', include='{bad'),
                       dict(events='custom', include='{"path":"event","nope":[]}'),
                       dict(mode='create')]:
            data = dict(mode='edit', topic='owner/repo', watch_name='ci',
                        profile='debugger', events='ci')
            data.update(fields)
            self.assertIn('webhook_invalid', self.post('save', **data))
            self.assertEqual(self.entries(), before)
        self.assertIn('webhook_deleted', self.post(
            'unsubscribe', topic='github:owner/repo', watch_name='ci',
            key='agent', dispatch='1'))
        self.assertEqual(self.entries(), [first])

    def test_unnamed_profile_edit_preserves_policy_and_other_spawn_config(self):
        self.subscribe('', 'triage', self.d.WATCH_EVENTS['issues'][1])
        p = self.cli('subscribe', 'owner/repo', '--deliver-to', 'subagent',
                     '--spawn-config', 'profile=triage', '--spawn-config', 'extra=keep',
                     '--when', json.dumps(self.d.WATCH_EVENTS['issues'][1]))
        self.assertEqual(p.returncode, 0, p.stderr)
        before = self.entries()[0]
        self.assertIn('webhook_saved', self.post(
            'save', mode='profile', topic='owner/repo', watch_name='', profile='debugger'))
        entry = self.entries()[0]
        self.assertEqual(entry['include'], before['include'])
        self.assertEqual(entry['spawnConfig'], {'profile': 'debugger', 'extra': 'keep'})
        self.assertIn('webhook_saved', self.post(
            'save', mode='profile', topic='owner/repo', watch_name='', profile=''))
        self.assertEqual(self.entries()[0]['spawnConfig'], {'extra': 'keep'})

    def test_cross_origin_save_is_rejected(self):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        try:
            c.request('POST', self.d.BASE + '/webhooks/save',
                      urllib.parse.urlencode(dict(mode='create', topic='owner/repo',
                                                 watch_name='bad', events='issues')),
                      {'Content-Type': 'application/x-www-form-urlencoded',
                       'Sec-Fetch-Site': 'cross-site'})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 403)
            self.assertFalse((self.state / 'filter.dispatch.json').exists())
        finally:
            c.close()

    def test_panel_identity_and_missing_profile(self):
        self.subscribe('issues', 'triage', self.d.WATCH_EVENTS['issues'][1])
        self.subscribe('ci', 'debugger', self.d.WATCH_EVENTS['ci'][1])
        calls = []
        def preview(topic, note, stamp, name=''):
            calls.append(name)
            return ('triage', ()) if name == 'issues' else ('', ('debugger',))
        self.d.hook_resolved_profile = preview
        self.d.hook_preamble = lambda *args: 'fixture prompt'
        markup = self.d.render_webhooks(self.entries())
        self.assertIn('github:owner/repo / issues', markup)
        self.assertIn('github:owner/repo / ci', markup)
        self.assertIn('name="watch_name" value="ci"', markup)
        self.assertIn('watch-github:owner/repo#issues', markup)
        self.assertIn('watch-github:owner/repo#ci', markup)
        self.assertIn('debugger&#x27; not found', markup)
        self.assertEqual(calls, ['issues', 'ci'])
        self.assertEqual(self.d.profile_usage(self.entries()),
                         {'triage': ['github:owner/repo / issues']})
        markup = self.d.render_watch_editor(dict(self.entries()[0], note='</textarea><script>'))
        self.assertNotIn('</textarea><script>', markup)

    def test_signed_events_select_distinct_profiles(self):
        self.subscribe('issues', 'triage', self.d.WATCH_EVENTS['issues'][1])
        self.subscribe('ci', 'debugger', self.d.WATCH_EVENTS['ci'][1])
        # Real receiver + signed HTTP + real dispatch, with only session launch
        # replaced by a recorder. No live watches or agent sessions are touched.
        (self.state / 'github.secret').write_text('fixture-secret')
        (self.state / 'sources.json').write_text(json.dumps({
            'defaultSource': 'github', 'sources': {'github': {'secretFile': 'github.secret'}}}))
        output = self.root / 'launches'
        recorder = self.root / 'record.py'
        recorder.write_text('#!' + sys.executable + '\nimport os,sys\n'
                            'sys.stdin.read()\n'
                            'with open(' + repr(str(output)) + ', "a") as f:\n'
                            ' f.write(os.environ["LOCAL_WEBHOOK_SPAWN_CONFIG"] + "\\n")\n')
        recorder.chmod(0o700)
        sock = self.root / 'ingress.sock'
        log = open(self.root / 'receiver.log', 'w+')
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, str(WEBHOOK)],
                                env=dict(self.env, LOCAL_WEBHOOK_RECEIVER_ONLY='1',
                                         LOCAL_WEBHOOK_HTTP_SOCK=str(sock),
                                         LOCAL_WEBHOOK_SPAWN_CMD=str(recorder)),
                                stdout=log, stderr=log)
        def stop():
            proc.terminate()
            proc.wait(timeout=10)
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(sock.exists())
        for event, more in [('issues', {'action': 'opened', 'issue': {'number': 1, 'title': 'new'}}),
                            ('workflow_run', {'action': 'completed', 'workflow_run': {
                                'id': 2, 'conclusion': 'failure', 'head_branch': 'fix'}})]:
            payload = {'repository': {'full_name': 'owner/repo'}, 'sender': {'login': 'human'}}
            payload.update(more)
            body = json.dumps(payload).encode()
            conn = http.client.HTTPConnection('localhost')
            conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.sock.connect(str(sock))
            conn.request('POST', '/github', body, {
                'Content-Type': 'application/json', 'X-GitHub-Event': event,
                'X-Hub-Signature-256': 'sha256=' + hmac.new(
                    b'fixture-secret', body, hashlib.sha256).hexdigest()})
            response = conn.getresponse()
            self.assertEqual(response.status, 200, response.read())
            conn.close()
        deadline = time.monotonic() + 15
        launches = []
        while time.monotonic() < deadline:
            if output.exists():
                launches = [json.loads(s)['profile'] for s in output.read_text().splitlines()]
            if len(launches) == 2:
                break
            time.sleep(.1)
        log.flush()
        self.assertCountEqual(launches, ['triage', 'debugger'],
                              (self.root / 'receiver.log').read_text())


if __name__ == '__main__':
    unittest.main()
