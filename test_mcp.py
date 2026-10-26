import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import hub
import mcp_connections as mcp
from mcp_gateway import Gateway
from mcp_github import Github
from mcp_transport import Client


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.project = self.root / 'project'; self.project.mkdir()
        self.other = self.root / 'other'; self.other.mkdir()
        patcher = patch.object(hub, 'STATE', self.root / 'state'); patcher.start(); self.addCleanup(patcher.stop)
        self.fixture = {'name': 'fixture', 'type': 'stdio', 'command': [sys.executable, str(hub.ROOT / 'tools/mcp_fixture.py')]}

    def save(self, item): return mcp.action(self.project, 'save', item)

    def test_private_registry_isolation_and_lifecycle(self):
        self.save({'name': 'github', 'type': 'github'})
        self.assertEqual(mcp.connections(self.other), [])
        self.assertFalse(any(self.project.iterdir()))
        if os.name != 'nt': self.assertEqual(mcp.location(self.project).stat().st_mode & 0o777, 0o600)
        self.assertFalse(mcp.action(self.project, 'disable', name='github')['connections'][0]['enabled'])
        self.assertTrue(mcp.action(self.project, 'enable', name='github')['connections'][0]['enabled'])
        self.assertEqual(mcp.action(self.project, 'delete', name='github')['connections'], [])

    def test_config_rejects_secret_literals_and_unsafe_endpoint_forms(self):
        for value in ({'name': 'Bad name', 'type': 'github'}, {'name': 'x', 'type': 'stdio', 'command': 'echo hi'},
                      {'name': 'x', 'type': 'http', 'url': 'https://user:password@example.com/mcp'},
                      {'name': 'x', 'type': 'http', 'url': 'http://example.com/mcp'},
                      {'name': 'x', 'type': 'http', 'url': 'https://example.com/mcp?token=secret'},
                      {'name': 'x', 'type': 'http', 'url': 'https://example.com/mcp', 'headers': {'Authorization': 'Bearer secret'}},
                      {'name': 'x', 'type': 'http', 'url': 'https://example.com/mcp', 'headers': {'Host': 'OVERRIDE'}}):
            with self.subTest(value=value), self.assertRaises(ValueError): mcp.validate(value)

    def test_stdio_handshake_discovery_call_and_shutdown(self):
        self.save(self.fixture)
        result = mcp.action(self.project, 'test', name='fixture')
        self.assertEqual([t['name'] for t in result['tools']], ['echo', 'where'])
        gateway = Gateway(self.project, True, cwd=self.other); self.addCleanup(gateway.close)
        self.assertEqual(gateway.invoke('connections', {})['connections'][0]['name'], 'fixture')
        self.assertEqual(len(gateway.invoke('discover', {'connection': 'fixture'})['tools']), 2)
        result = gateway.invoke('call', {'connection': 'fixture', 'tool': 'where', 'arguments': {}})
        self.assertEqual(result['text'], str(self.other))
        child = next(iter(gateway.clients.values())).process
        gateway.close(); self.assertIsNotNone(child.poll())

    def test_lazy_discovery_analysis_permissions_and_revocation(self):
        self.save(self.fixture); self.save({'name': 'github', 'type': 'github'})
        readonly = Gateway(self.project); self.addCleanup(readonly.close)
        self.assertEqual([v['name'] for v in readonly.invoke('connections', {})['connections']], ['github'])
        self.assertEqual(readonly.clients, {})
        with self.assertRaises(ValueError): readonly.invoke('discover', {'connection': 'fixture'})
        gateway = Gateway(self.project, True); self.addCleanup(gateway.close)
        gateway.invoke('discover', {'connection': 'fixture'})
        child = next(iter(gateway.clients.values())).process
        mcp.action(self.project, 'disable', name='fixture')
        with self.assertRaises(ValueError): gateway.invoke('call', {'connection': 'fixture', 'tool': 'echo', 'arguments': {}})
        self.assertIsNotNone(child.poll())

    def test_allowlist_and_output_budget(self):
        self.save({**self.fixture, 'tools': ['echo']})
        gateway = Gateway(self.project, True); self.addCleanup(gateway.close)
        self.assertEqual(len(gateway.invoke('discover', {'connection': 'fixture'})['tools']), 1)
        with self.assertRaises(ValueError): gateway.invoke('call', {'connection': 'fixture', 'tool': 'where', 'arguments': {}})
        value = gateway.invoke('call', {'connection': 'fixture', 'tool': 'echo', 'arguments': {'text': 'x' * 20000}})
        self.assertTrue(value['truncated']); self.assertEqual(len(value['text']), 16000)

    def test_stalled_stdio_times_out_and_exits(self):
        client = Client({'type': 'stdio', 'command': [sys.executable, '-c', 'import time;time.sleep(30)']}, self.project, timeout=.2)
        try:
            with self.assertRaisesRegex(ValueError, 'timed out'): client.tools()
        finally: client.close()
        self.assertIsNone(client.process)

    def test_opencode_native_config_and_manager_isolation(self):
        self.save({'name': 'github', 'type': 'github'})
        env = hub.clean_environment('local', hub.LOCAL_AGENT_MODEL, project=self.project)
        config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(env['CODING_HUB_MCP_PROJECT'], str(self.project))
        self.assertIn(mcp.NAME, config['mcp'])
        self.assertEqual(config['permission'][mcp.NAME + '_*'], 'allow')
        with patch.dict(os.environ, {k: v for k, v in env.items() if k.startswith('CODING_HUB_MCP_')}):
            manager = hub.clean_environment('free', 'beam-free')
            self.assertNotIn('CODING_HUB_MCP_PROJECT', manager)
            self.assertNotIn('mcp', json.loads(manager['OPENCODE_CONFIG_CONTENT']))
        self.assertNotIn('mcp', json.loads(hub.clean_environment('free', 'beam-free', project=self.other)['OPENCODE_CONFIG_CONTENT']))

    def test_preview_task_keeps_original_connection_scope(self):
        self.save({'name': 'github', 'type': 'github'})
        seen = {}
        def process(args, project, env, log, **kwargs):
            seen.update(project=project, env=env)
            return 0, 'Verified.', False
        with patch.object(hub, 'route_options', return_value=[('local', 'fixture-model')]), patch.object(hub, 'run_process', side_effect=process), contextlib.redirect_stdout(io.StringIO()):
            code = hub.run_task(self.project, 'Inspect this project.', backend='local', preview=True)
        self.assertEqual(code, 0)
        self.assertNotEqual(Path(seen['project']), self.project)
        self.assertEqual(seen['env']['CODING_HUB_MCP_PROJECT'], str(self.project))
        self.assertEqual(seen['env']['CODING_HUB_MCP_APPLY'], '1')

    def test_claude_strict_config_enabled_only_for_explicit_scope(self):
        self.save({'name': 'github', 'type': 'github'})
        args = hub.command('claude', 'sonnet', 'hello', project=self.other, mcp_project=self.project)
        self.assertIn(mcp.NAME, json.loads(args[args.index('--mcp-config') + 1])['mcpServers'])
        self.assertIn('mcp__coding_hub_tools__*', args[args.index('--allowedTools') + 1])
        manager = hub.command('claude', 'sonnet', 'hello', project=self.project)
        self.assertEqual(json.loads(manager[manager.index('--mcp-config') + 1]), {'mcpServers': {}})

    def test_antigravity_merge_preserves_unrelated_config_and_is_idempotent(self):
        path = self.root / '.gemini/config/mcp_config.json'; path.parent.mkdir(parents=True)
        settings = self.root / '.gemini/antigravity-cli/settings.json'; settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({'theme': 'dark', 'permissions': {'allow': ['command(git)'], 'deny': ['mcp(other/*)'], 'ask': ['command(*)']}}))
        original = {'mcpServers': {'existing': {'command': '/keep/me'}}, 'other': [1, 2]}
        path.write_text(json.dumps(original))
        with patch.object(Path, 'home', return_value=self.root):
            mcp.install_antigravity(); first = path.read_bytes(); mcp.install_antigravity()
        self.assertEqual(path.read_bytes(), first)
        value = json.loads(first); self.assertEqual(value['mcpServers']['existing'], original['mcpServers']['existing'])
        self.assertEqual(value['other'], [1, 2])
        policy = json.loads(settings.read_text())
        self.assertEqual(policy, {'theme': 'dark', 'permissions': {'allow': ['command(git)', 'mcp(coding_hub_tools/*)'], 'deny': ['mcp(other/*)'], 'ask': ['command(*)']}})

    def test_antigravity_empty_config_created_by_native_cli(self):
        path = self.root / '.gemini/config/mcp_config.json'; path.parent.mkdir(parents=True); path.touch()
        with patch.object(Path, 'home', return_value=self.root): mcp.install_antigravity()
        self.assertIn(mcp.NAME, json.loads(path.read_text())['mcpServers'])

    def test_github_readonly_and_original_project_account_context(self):
        github = Github(self.project)
        with patch.object(hub, 'executable', return_value='/wrapper/gh'), patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            github.call('issue', {'repo': 'owner/repo', 'number': 12})
        self.assertEqual(run.call_args.args[0], ['/wrapper/gh', 'api', '--method', 'GET', 'repos/owner/repo/issues/12'])
        self.assertEqual(run.call_args.kwargs['cwd'], self.project)
        for name, args in [('delete_repository', {}), ('file', {'repo': 'a/b', 'path': '../escape'}), ('issue', {'repo': 'a/b', 'number': '-XPOST'})]:
            with self.assertRaises(ValueError): github.call(name, args)

    def test_end_to_end_gateway_child_without_ai(self):
        self.save(self.fixture)
        env = {'CODING_HUB_STATE': str(hub.STATE), 'CODING_HUB_MCP_PROJECT': str(self.project), 'CODING_HUB_MCP_APPLY': '1'}
        with patch.dict(os.environ, env):
            client = Client({'type': 'stdio', 'command': [sys.executable, str(hub.ROOT / 'mcp_gateway.py')]}, self.other)
            # The normal transport deliberately strips delegation authority. Pass it only
            # in this native-client simulation, as the native agents inherit these values.
            client.config['env'] = {k: k for k in env}
            try:
                self.assertEqual([t['name'] for t in client.tools()], ['connections', 'discover', 'call'])
                value = client.call('call', {'connection': 'fixture', 'tool': 'where', 'arguments': {}})
                self.assertEqual(json.loads(value['content'][0]['text'])['text'], str(self.other))
            finally: client.close()

    def test_headless_permission_denial_is_not_success(self):
        self.assertEqual(hub.classify_failure(0, 'jetski: no output produced — a tool required the "mcp" permission that headless mode cannot prompt for, so it was auto-denied.'), 'permission')

    def test_cli_lifecycle_and_listing_without_models(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mcp.cli(['github'], project=self.project), 0)
            mcp.cli(['disable', 'github'], project=self.project)
        self.assertIn('github', output.getvalue())
        self.assertFalse(mcp.connections(self.project)[0]['enabled'])

    def test_http_sessions_json_sse_auth_and_no_redirect(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_DELETE(self):
                calls.append(('DELETE', self.headers.get('Mcp-Session-Id'))); self.send_response(204); self.end_headers()
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((request['method'], self.headers.get('Mcp-Session-Id')))
                if self.path in ('/auth', '/redirect'):
                    self.send_response(401 if self.path == '/auth' else 302); self.send_header('Location', '/mcp'); self.end_headers(); return
                if 'id' not in request: self.send_response(202); self.end_headers(); return
                result = {'protocolVersion': '2025-06-18'} if request['method'] == 'initialize' else {'tools': []}
                value = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
                self.send_response(200); self.send_header('Mcp-Session-Id', 'fixture-session')
                self.send_header('Content-Type', 'text/event-stream' if self.path == '/sse' else 'application/json'); self.end_headers()
                self.wfile.write((('data: ' + value + '\n\n') if self.path == '/sse' else value).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for path in ('/mcp', '/sse', '/auth', '/redirect'):
                client = Client({'type': 'http', 'url': f'http://127.0.0.1:{server.server_port}{path}'}, self.project)
                try:
                    if path in ('/auth', '/redirect'):
                        with self.assertRaises(ValueError): client.tools()
                    else: self.assertEqual(client.tools(), [])
                finally: client.close()
        finally: server.shutdown(); server.server_close(); thread.join()
        self.assertIn(('tools/list', 'fixture-session'), calls)
        self.assertIn(('DELETE', 'fixture-session'), calls)


if __name__ == '__main__': unittest.main()
