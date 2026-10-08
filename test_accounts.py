import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
import hub


class AccountTests(unittest.TestCase):
    def test_fixed_login_commands_and_provider_links(self):
        with patch.object(hub, 'executable', side_effect=lambda name: '/bin/' + name):
            self.assertEqual(accounts.login_command('claude'), ['/bin/claude', 'auth', 'login', '--claudeai'])
            self.assertEqual(accounts.login_command('antigravity', True)[-1], '/logout')
            self.assertIn('openai', accounts.login_command('openai'))
            for invalid in ('shell', 'claude;id', None):
                with self.assertRaises(ValueError): accounts.login_command(invalid)
        self.assertTrue(accounts.safe_link('https://accounts.google.com/o/oauth2/auth?state=example'))
        for link in ('https://accounts.google.com.evil.test/', 'javascript:alert(1)', 'file:///tmp/key',
                     'https://user@claude.ai/', 'http://auth.openai.com/'):
            self.assertFalse(accounts.safe_link(link))

    @unittest.skipIf(os.name == 'nt', 'PTY is Unix-only')
    def test_signin_pty_input_lifecycle_and_no_persistent_transcript(self):
        original = subprocess.Popen
        code = "import sys,time;print('Choose an account',flush=True);line=input();print('Response received',flush=True);time.sleep(30)"
        def spawn(args, **kw):
            self.assertEqual(args[2:4], ['--child', 'claude'])
            return original([sys.executable, '-u', '-c', code], **kw)
        with tempfile.TemporaryDirectory() as temp, patch.object(hub, 'STATE', Path(temp)), \
             patch.object(hub, 'executable', return_value='/fake/cli'), \
             patch.object(accounts.subprocess, 'Popen', side_effect=spawn):
            session = accounts.SignInSession('claude')
            try:
                deadline = time.monotonic()+3
                while 'Choose an account' not in session.snapshot()['screen'] and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertIn('Choose an account', session.snapshot()['screen'])
                for payload in ({'key':'shell'}, {'text':'a\nb'}, {'text':'\x1b[2J'}):
                    with self.assertRaises(ValueError): session.send(payload)
                session.send({'text': 'test-response'})
                deadline = time.monotonic()+3
                while 'Response received' not in session.snapshot()['screen'] and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertIn('Response received', session.snapshot()['screen'])
                self.assertEqual(list(Path(temp).rglob('*.log')), [])
            finally:
                session.close()
            self.assertFalse(session.snapshot()['running'])
            self.assertEqual(session.snapshot()['screen'], '')
            with self.assertRaises(ValueError): session.send({'key':'enter'})

    def test_subscription_environment_avoids_api_billing_and_route_isolation(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'example', 'OPENAI_API_KEY':'example',
                                     'CLAUDE_CODE_USE_BEDROCK':'1'}):
            self.assertNotIn('ANTHROPIC_API_KEY', hub.clean_environment('claude'))
            self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', hub.clean_environment('claude'))
            env = hub.clean_environment('openai','test-model')
            self.assertNotIn('OPENAI_API_KEY',env)
            config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            self.assertEqual(config['enabled_providers'], ['openai'])
            self.assertEqual(config['permission']['bash'], 'deny')
            self.assertNotIn('OPENCODE_DISABLE_DEFAULT_PLUGINS',env)
        with patch.object(hub, 'executable', return_value='/bin/claude'):
            read = hub.command('claude','sonnet','Inspect only')
            edit = hub.command('claude','sonnet','Implement',apply=True)
            self.assertEqual(read[read.index('--tools')+1], 'Read,Glob,Grep')
            self.assertIn('Bash', edit[edit.index('--tools')+1])
            self.assertIn('--strict-mcp-config', read)
        self.assertEqual(hub.event_text('{"type":"result","result":"Done","is_error":false}'), ('Done',False))
        self.assertTrue(hub.event_text('{"type":"result","errors":["Not signed in"],"is_error":true}')[1])

    def test_model_selection_is_explicit_and_never_shell_input(self):
        with self.assertRaises(ValueError): hub.validate_model('openai', None)
        for backend, model in [('auto','gpt-x'),('smart','x;echo SECRET'),('openai','x;echo SECRET'),('claude','--flag')]:
            with self.assertRaises(ValueError): hub.validate_model(backend,model)
        self.assertEqual(hub.validate_model('antigravity','claude-sonnet-4-6'),'claude-sonnet-4-6')
        self.assertEqual(hub.validate_model('smart','claude-sonnet-4-6'),'claude-sonnet-4-6')

    def test_smart_uses_the_entire_live_antigravity_catalog(self):
        catalog = [{'id': mid, 'name': mid} for mid in ('claude-opus-4-6-thinking', 'claude-sonnet-4-6',
            'gemini-3.1-pro-high', 'gemini-3.1-pro-low', 'gemini-3.8-flash-high',
            'gemini-3.8-flash-medium', 'gemini-3.8-flash-low', 'gpt-oss-120b-medium', 'future-account-model')]
        with patch.object(accounts, 'model_catalog', return_value={'antigravity': catalog}):
            self.assertEqual(hub.model_choices('smart'), catalog)
            for choice in catalog:
                self.assertEqual(hub.validate_model('smart', choice['id']), choice['id'])

    def test_credential_listing_exposes_only_status(self):
        with patch.object(hub,'executable',return_value='cli'), patch.object(accounts.subprocess,'run',side_effect=[
            subprocess.CompletedProcess([],0,json.dumps({'loggedIn':True,'authMethod':'claude.ai','email':'private@example.test'}),''),
            subprocess.CompletedProcess([],0,'OpenAI oauth\n','')]):
            status = accounts.connection_status()
        self.assertEqual(status, {'claude':'saved','openai':'saved'})
        self.assertNotIn('private', json.dumps(status))
