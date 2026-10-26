"""Bounded MCP tools client: stdio and Streamable HTTP, without an OAuth proxy."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

LIMIT = 1024 * 1024
PROTOCOL = '2025-06-18'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class Client:
    def __init__(self, config, cwd, timeout=20):
        self.config, self.cwd, self.timeout = config, Path(cwd), timeout
        self.process = None
        self.session = None
        self.initialized = False
        self.number = 0
        self.protocol = PROTOCOL
        self.closed = threading.Event()
        self.responses = queue.Queue(maxsize=8)

    def variables(self, mapping):
        values = {}
        for key, source in mapping.items():
            value = os.environ.get(source)
            if not value: raise ValueError('Set environment variable ' + source + ' before starting Coding Hub.')
            if '\r' in value or '\n' in value: raise ValueError('Environment value contains an invalid newline.')
            values[key] = value
        return values

    def start(self):
        if self.initialized: return
        if self.config['type'] == 'stdio':
            env = dict(os.environ)
            # A custom child is not another authorized Hub agent.
            for key in ('CODING_HUB_MCP_PROJECT', 'CODING_HUB_MCP_APPLY'): env.pop(key, None)
            env.update(self.variables(self.config.get('env', {})))
            try:
                self.process = subprocess.Popen(self.config['command'], cwd=self.cwd, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=(os.name != 'nt'))
            except OSError:
                raise ValueError('Cannot start MCP command. Check that the executable is installed and its arguments are correct.') from None
            threading.Thread(target=self.read_stdout, daemon=True).start()
        result = self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'coding-hub', 'version': '2.15.0'}}, initialize=True)
        if not isinstance(result, dict) or result.get('protocolVersion') not in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
            raise ValueError('The MCP server returned an unsupported protocol version.')
        self.protocol = result['protocolVersion']
        self.notify('notifications/initialized')
        self.initialized = True

    def read_stdout(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(LIMIT + 1)
                if not line or len(line) > LIMIT:
                    value = {'transport_error': 'MCP server disconnected or exceeded the response limit.'}
                else:
                    try: value = json.loads(line)
                    except ValueError: value = {'transport_error': 'MCP stdout must contain JSON-RPC messages only.'}
                while not self.closed.is_set():
                    try: self.responses.put(value, timeout=.1); break
                    except queue.Full: pass
                if isinstance(value, dict) and 'transport_error' in value: break
        except (OSError, ValueError): pass

    def http(self, payload=None, method='POST'):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': self.protocol}
        headers.update(self.variables(self.config.get('headers', {})))
        if self.session: headers['Mcp-Session-Id'] = self.session
        request = urllib.request.Request(self.config['url'], data=json.dumps(payload).encode() if payload is not None else None,
                                         headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                session = response.headers.get('Mcp-Session-Id')
                if session:
                    if len(session) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in session): raise ValueError('Invalid MCP session header.')
                    self.session = session
                if response.status == 202 or method == 'DELETE': return None
                if response.headers.get_content_type() == 'text/event-stream':
                    total, data, deadline = 0, [], time.monotonic() + self.timeout
                    while time.monotonic() < deadline:
                        line = response.readline(LIMIT + 1); total += len(line)
                        if total > LIMIT: raise ValueError('MCP response exceeded 1 MiB.')
                        if not line: break
                        text = line.decode('utf-8').rstrip('\r\n')
                        if text.startswith('data:'): data.append(text[5:].lstrip())
                        elif not text and data:
                            value = json.loads('\n'.join(data)); data = []
                            if isinstance(value, dict) and value.get('id') == payload.get('id'): return value
                    raise ValueError('MCP stream ended or timed out before returning a result.')
                body = response.read(LIMIT + 1)
                if len(body) > LIMIT: raise ValueError('MCP response exceeded 1 MiB.')
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise ValueError('MCP authentication required. Use an environment header or a vendor-authenticated local MCP command; interactive OAuth is not supported by this bridge.') from None
            raise ValueError('MCP HTTP request failed (status ' + str(error.code) + '). Redirects are not followed.') from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError('MCP endpoint could not be reached within its timeout.') from None

    def send(self, payload):
        try:
            self.process.stdin.write((json.dumps(payload) + '\n').encode()); self.process.stdin.flush()
        except (OSError, ValueError): raise ValueError('MCP command disconnected.') from None

    def notify(self, method):
        payload = {'jsonrpc': '2.0', 'method': method}
        if self.config['type'] == 'stdio': self.send(payload)
        else: self.http(payload)

    def request(self, method, params, initialize=False):
        if not initialize: self.start()
        self.number += 1
        payload = {'jsonrpc': '2.0', 'id': self.number, 'method': method, 'params': params}
        if self.config['type'] == 'http': value = self.http(payload)
        else:
            self.send(payload)
            deadline = time.monotonic() + self.timeout
            for _ in range(200):
                try: value = self.responses.get(timeout=max(.001, deadline - time.monotonic()))
                except queue.Empty: raise ValueError('MCP command timed out.') from None
                if not isinstance(value, dict): raise ValueError('Invalid MCP response.')
                if 'transport_error' in value: raise ValueError(value['transport_error'])
                if 'method' in value and 'id' in value:
                    self.send({'jsonrpc': '2.0', 'id': value['id'], 'error': {'code': -32601, 'message': 'Client requests are unsupported.'}})
                elif value.get('id') == payload['id']: break
                if time.monotonic() >= deadline: raise ValueError('MCP command timed out.')
            else: raise ValueError('MCP server sent too many unrelated messages.')
        if not isinstance(value, dict) or value.get('id') != payload['id']: raise ValueError('Invalid MCP response identifier.')
        if 'error' in value: raise ValueError('MCP server rejected ' + method + '. Check its native configuration and logs.')
        if 'result' not in value: raise ValueError('MCP response has no result.')
        return value['result']

    def tools(self):
        tools, cursor = [], None
        for _ in range(4):
            result = self.request('tools/list', {'cursor': cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get('tools'), list): raise ValueError('Invalid MCP tools response.')
            for tool in result['tools']:
                if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not isinstance(tool.get('inputSchema'), dict):
                    raise ValueError('Invalid MCP tool schema.')
                if len(json.dumps(tool)) > 32000: raise ValueError('MCP tool schema is too large for this connection.')
                tools.append(tool)
                if len(tools) > 200: raise ValueError('MCP server has more than 200 tools. Configure a smaller server toolset.')
            cursor = result.get('nextCursor')
            if not cursor: return tools
        raise ValueError('MCP discovery exceeded four pages. Configure a smaller server toolset.')

    def call(self, name, arguments): return self.request('tools/call', {'name': name, 'arguments': arguments})

    def close(self):
        self.closed.set()
        if self.process:
            # Kill the entire owned server group, including npx/uvx children.
            try:
                if os.name == 'nt': self.process.terminate()
                else: os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired): pass
            try:
                if os.name == 'nt':
                    if self.process.poll() is None: self.process.kill()
                else: os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout): stream.close()
            self.process = None
        elif self.session:
            try: self.http(method='DELETE')
            except (OSError, ValueError): pass
            self.session = None
