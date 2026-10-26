"""Private, project-scoped MCP connections and native agent configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

NAME = 'coding_hub_tools'
NOTE = ('Connections belong to this workspace. GitHub uses your existing gh sign-in and is read-only. '
        'Custom servers run trusted code or contact their endpoint; they are available only with edits & commands enabled. '
        'Tool discovery is on demand. Smart managers never receive these connections. '
        'HTTP supports anonymous access or headers from environment variables; interactive OAuth and legacy SSE are not supported here.')


def location(project):
    import hub
    if not project or not Path(project).expanduser().is_dir():
        raise ValueError('Choose an existing workspace first.')
    key = hashlib.sha256(str(Path(project).expanduser().resolve()).encode()).hexdigest()
    return hub.STATE / 'projects' / key / 'mcp-connections.json'


def validate(value):
    if not isinstance(value, dict): raise ValueError('Connection must be a JSON object.')
    name = value.get('name', '')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', name):
        raise ValueError('Use a short lowercase connection name, with letters, numbers, - or _.')
    kind = value.get('type')
    if kind not in ('github', 'stdio', 'http'): raise ValueError('Choose github, stdio or http.')
    if not isinstance(value.get('enabled', True), bool): raise ValueError('enabled must be true or false.')
    result = {'name': name, 'type': kind, 'enabled': value.get('enabled', True)}
    allowed = value.get('tools', [])
    if not isinstance(allowed, list) or len(allowed) > 40 or any(not isinstance(t, str) or not re.fullmatch(r'[\w.:-]{1,100}', t) for t in allowed):
        raise ValueError('tools must be a list of at most 40 exact tool names.')
    result['tools'] = list(dict.fromkeys(allowed))
    if kind == 'stdio':
        command = value.get('command')
        if not isinstance(command, list) or not 1 <= len(command) <= 40 or any(not isinstance(s, str) or not s or len(s) > 2000 or '\x00' in s for s in command):
            raise ValueError('command must be a nonempty JSON argument array, without shell expansion.')
        result['command'] = command
        result['env'] = env_names(value.get('env', {}))
    elif kind == 'http':
        url = value.get('url', '')
        if not isinstance(url, str) or len(url) > 2000: raise ValueError('Enter an HTTP MCP endpoint.')
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not (
            parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1'))):
            raise ValueError('Use HTTPS (HTTP only on localhost), without credentials, query parameters or fragments.')
        result['url'] = url
        result['headers'] = env_names(value.get('headers', {}), headers=True)
    return result


def env_names(value, headers=False):
    if not isinstance(value, dict) or len(value) > 16: raise ValueError('Environment mapping must be a small JSON object.')
    for key, source in value.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,79}', key):
            raise ValueError('Invalid environment variable or header name.')
        if headers and key.lower() in ('host', 'content-length', 'content-type', 'accept', 'mcp-session-id', 'mcp-protocol-version'):
            raise ValueError('Transport headers are managed automatically.')
        if not isinstance(source, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', source):
            raise ValueError('Map to environment variable names, never paste secret values.')
    return dict(value)


def connections(project):
    path = location(project)
    if not path.exists(): return []
    data = json.loads(path.read_text())
    if not isinstance(data, list) or len(data) > 8: raise ValueError('Invalid saved MCP connections.')
    return [validate(item) for item in data]


def snapshot(project):
    return {'project': str(Path(project).expanduser().resolve()), 'connections': connections(project), 'note': NOTE}


def action(project, operation, connection=None, name=None):
    import hub
    # A separate lock permits safe settings reads while a task is running.
    from task_lock import workspace_lock
    with workspace_lock(hub.STATE / 'mcp-settings', Path(project).expanduser().resolve()):
        items = connections(project)
        if operation == 'save':
            item = validate(connection)
            items = [v for v in items if v['name'] != item['name']] + [item]
            if len(items) > 8: raise ValueError('Keep at most 8 connections per workspace.')
        elif operation in ('enable', 'disable', 'delete'):
            if not any(v['name'] == name for v in items): raise ValueError('Connection not found.')
            if operation == 'delete': items = [v for v in items if v['name'] != name]
            else:
                for item in items:
                    if item['name'] == name: item['enabled'] = operation == 'enable'
        elif operation == 'test':
            item = next((v for v in items if v['name'] == name), None)
            if not item: raise ValueError('Connection not found.')
            from mcp_transport import Client
            from mcp_github import Github
            client = Github(project) if item['type'] == 'github' else Client(item, Path(project))
            try:
                tools = client.tools()
                if item['tools']: tools = [t for t in tools if t['name'] in item['tools']]
                identity = client.identity() if item['type'] == 'github' else None
                return {'ok': True, 'name': name, 'tools': [{'name': t['name'], 'description': str(t.get('description', ''))[:300]} for t in tools], 'identity': identity}
            finally: client.close()
        else: raise ValueError('Unknown MCP operation.')
        hub.save_json(location(project), items)
    return snapshot(project)


def native_entry():
    return {'command': sys.executable, 'args': [str(Path(__file__).with_name('mcp_gateway.py'))]}


def install_antigravity():
    """Merge one dormant, environment-gated bridge; never rewrite unrelated entries."""
    import hub
    from task_lock import workspace_lock
    path = Path.home() / '.gemini/config/mcp_config.json'
    entry = native_entry()
    with workspace_lock(hub.STATE / 'mcp-settings', path):
        raw = path.read_text() if path.exists() else ''
        try: data = json.loads(raw) if raw.strip() else {}
        except ValueError: raise ValueError('Antigravity MCP config is invalid; repair it in its native /mcp menu.') from None
        if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
            raise ValueError('Antigravity MCP config is invalid; repair it in its native /mcp menu.')
        servers = data.setdefault('mcpServers', {})
        existing = servers.get(NAME)
        if existing and existing != entry:
            # The executable can change after a Python upgrade; only replace our exact script entry.
            if existing.get('args') != entry['args']:
                raise ValueError('An unrelated Antigravity server already uses ' + NAME + '.')
        if existing != entry:
            servers[NAME] = entry
            hub.save_json(path, data)
        # Headless analysis cannot display the native MCP permission prompt.
        # Authorize only our gated bridge; leave all existing ask/deny rules intact.
        settings_path = Path.home() / '.gemini/antigravity-cli/settings.json'
        raw = settings_path.read_text() if settings_path.exists() else ''
        try: settings = json.loads(raw) if raw.strip() else {}
        except ValueError: raise ValueError('Antigravity CLI settings are invalid; repair them in its native settings.') from None
        if not isinstance(settings, dict) or not isinstance(settings.get('permissions', {}), dict):
            raise ValueError('Invalid Antigravity CLI permissions settings.')
        permissions = settings.setdefault('permissions', {})
        allowed = permissions.setdefault('allow', [])
        if not isinstance(allowed, list) or any(not isinstance(v, str) for v in allowed):
            raise ValueError('Invalid Antigravity CLI allow rules.')
        rule = 'mcp(' + NAME + '/*)'
        if rule not in allowed:
            allowed.append(rule)
            hub.save_json(settings_path, settings)


def prepare(env, backend, project, apply=False):
    """An explicit project is required. Manager calls never pass one."""
    for key in ('CODING_HUB_MCP_PROJECT', 'CODING_HUB_MCP_APPLY'):
        env.pop(key, None)
    if not project: return False
    eligible = [v for v in connections(project) if v['enabled'] and (apply or v['type'] == 'github')]
    if not eligible: return False
    import hub
    env.update(CODING_HUB_STATE=str(hub.STATE), CODING_HUB_MCP_PROJECT=str(Path(project).resolve()), CODING_HUB_MCP_APPLY='1' if apply else '0')
    if backend == 'antigravity': install_antigravity()
    return True


def cli(argv=None, project=None):
    parser = argparse.ArgumentParser(prog='codehub mcp')
    parser.add_argument('operation', nargs='?', default='list', choices=('list', 'github', 'add', 'test', 'enable', 'disable', 'delete'))
    parser.add_argument('name', nargs='?')
    parser.add_argument('--project', default=str(project or Path.cwd()))
    parser.add_argument('--general', action='store_true', help='Configure the standalone General tasks workspace')
    parser.add_argument('--config', help='JSON file containing a single connection; environment names only for secrets')
    args = parser.parse_args(argv)
    if args.general:
        import hub
        args.project = hub.general_workspace()
    if args.operation == 'list': result = snapshot(args.project)
    elif args.operation == 'github': result = action(args.project, 'save', {'name': args.name or 'github', 'type': 'github'})
    elif args.operation == 'add':
        if not args.config: raise ValueError('Use --config FILE for a custom connection.')
        result = action(args.project, 'save', json.loads(Path(args.config).read_text()))
    else: result = action(args.project, args.operation, name=args.name)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
