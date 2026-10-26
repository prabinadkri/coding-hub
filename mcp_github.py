"""Small read-only GitHub MCP adapter. gh retains ownership of authentication."""
import base64
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


def schema(name, description, properties=None, required=None):
    return {'name': name, 'description': description,
        'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False},
        'annotations': {'readOnlyHint': True, 'destructiveHint': False}}


REPO = {'type': 'string', 'description': 'owner/repo; omit to use the selected project Git remote'}
NUMBER = {'type': 'integer', 'minimum': 1}
TOOLS = [
    schema('repository', 'Read repository description, default branch and permissions.', {'repo': REPO}),
    schema('issues', 'List the latest 20 open issues and pull requests.', {'repo': REPO}),
    schema('issue', 'Read one issue or pull request, including its body.', {'repo': REPO, 'number': NUMBER}, ['number']),
    schema('pull_requests', 'List the latest 20 open pull requests.', {'repo': REPO}),
    schema('pull_request_files', 'Read the first 20 changed files and available patch excerpts of a pull request.', {'repo': REPO, 'number': NUMBER}, ['number']),
    schema('checks', 'Read the first 20 check runs for a commit or branch.', {'repo': REPO, 'ref': {'type': 'string'}}, ['ref']),
    schema('file', 'Read a repository text file at an optional branch/ref (bounded output).', {'repo': REPO, 'path': {'type': 'string'}, 'ref': {'type': 'string'}}, ['path'])]


class Github:
    def __init__(self, project): self.project = Path(project)
    def tools(self): return TOOLS
    def close(self): pass

    def run(self, args):
        import hub
        executable = hub.executable('gh')
        if not executable: raise ValueError('Install GitHub CLI and run gh auth login first.')
        try:
            result = subprocess.run([executable, *args], cwd=self.project, capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired: raise ValueError('GitHub request timed out.') from None
        if result.returncode: raise ValueError('GitHub request failed. Check gh auth status in this project, repository access and GitHub rate limits.')
        return result.stdout

    def identity(self): return self.run(['api', '--method', 'GET', 'user', '--jq', '.login']).strip()

    def call(self, name, arguments):
        tool = next((t for t in TOOLS if t['name'] == name), None)
        if not tool or not isinstance(arguments, dict): raise ValueError('Unknown GitHub tool.')
        spec = tool['inputSchema']
        if set(arguments) - set(spec['properties']) or any(k not in arguments for k in spec['required']): raise ValueError('Invalid GitHub arguments.')
        for key, value in arguments.items():
            if key == 'number':
                if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 100000000: raise ValueError('Use a positive issue or PR number.')
            elif not isinstance(value, str) or not value or len(value) > 500: raise ValueError('Invalid GitHub argument.')
        repo = arguments.get('repo') or self.run(['repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner']).strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or any(part in ('.', '..') for part in repo.split('/')):
            raise ValueError('Specify a repository as owner/repo.')
        endpoint, fields = 'repos/' + repo, []
        if name == 'issues': endpoint += '/issues'; fields = ['state=open', 'per_page=20']
        elif name == 'issue': endpoint += '/issues/' + str(arguments['number'])
        elif name == 'pull_requests': endpoint += '/pulls'; fields = ['state=open', 'per_page=20']
        elif name == 'pull_request_files': endpoint += '/pulls/' + str(arguments['number']) + '/files'; fields = ['per_page=20']
        elif name == 'checks': endpoint += '/commits/' + quote(arguments['ref'], safe='') + '/check-runs'; fields = ['per_page=20']
        elif name == 'file':
            path = arguments['path']
            if any(p in ('', '.', '..') for p in path.split('/')): raise ValueError('Use a repository-relative file path.')
            endpoint += '/contents/' + quote(path, safe='/')
            if arguments.get('ref'): fields = ['ref=' + arguments['ref']]
        args = ['api', '--method', 'GET', endpoint]
        for field in fields: args.extend(['-f', field])
        value = json.loads(self.run(args))
        if name == 'file':
            if not isinstance(value, dict) or value.get('type') != 'file' or value.get('encoding') != 'base64':
                raise ValueError('This endpoint is not a small text file.')
            value = base64.b64decode(value.get('content', '')).decode('utf-8', errors='replace')
        return {'content': [{'type': 'text', 'text': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}]}
