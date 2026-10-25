"""Local reply references, bounded previews, and explicit file-manager actions."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from collections import deque
from urllib.parse import unquote, urlsplit
import hub

EXTENSIONS = r'(?:log|txt|md|rst|json|jsonl|csv|tsv|yaml|yml|toml|ini|cfg|conf|xml|html|css|scss|js|jsx|ts|tsx|py|go|rs|java|c|h|cpp|cs|sh|sql|vue|svelte|png|jpe?g|gif|webp|svg|pdf|zip|tgz|gz|mp4|mov)'
LOCATION = r'(?::\d+(?::\d+)?|#L\d+(?:-L\d+)?)?'
# Start only at a token boundary so long dotted output cannot cause quadratic rescans.
BOUNDARY = r'(?<![\w.@+~/-])'
PLAIN_PATH = BOUNDARY + r'(?:~/|\.\.?/|/|[\w.@+\-]+/)[\w.@+\-/]*' + LOCATION
PLAIN_FILE = BOUNDARY + r'\.?[\w@+\-][\w.@+\-]*\.' + EXTENSIONS + LOCATION + r'(?![\w:#-]|\.[\w])'
TOKEN = re.compile(r"\[[^\]\n]{1,200}\]\((?:<[^>\n]+>|[^)\n]+)\)|`[^`\n]+`|https?://[^\s<>`]+|file://[^\s<>`]+|[\"'][^\"'\n]+[\"']|" + PLAIN_PATH + '|' + PLAIN_FILE, re.I)
SENSITIVE_DIRS = {'.ssh', '.aws', '.azure', '.gnupg', '.config', '.codex', '.claude', '.git'}
SENSITIVE_NAMES = {'credentials', 'credentials.json', 'auth.json', 'oauth_creds.json', 'id_rsa', 'id_ed25519', 'key4.db', 'logins.json', 'cookies', 'login data'}
TEXT_LIMIT = 128 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
DOWNLOAD_LIMIT = 20 * 1024 * 1024
PRODUCT_NAMES = {'next.js', 'node.js', 'vue.js', 'nuxt.js', 'socket.io', 'react.js', 'express.js', 'd3.js', 'three.js'}
SEARCH_SKIP = SENSITIVE_DIRS | {'.hg', '.svn', 'node_modules', 'vendor', '.venv', 'venv', '__pycache__', '.next', '.nuxt', '.cache', 'dist', 'build', 'coverage'}
SEARCH_DIRECTORIES = 600
SEARCH_ENTRIES = 20000
SEARCH_SECONDS = .8


def split_location(value):
    match = re.search(r'(?::(\d+)(?::\d+)?|#L(\d+)(?:-L\d+)?)$', value)
    if match:
        return value[:match.start()], min(1000000, max(1, int(match[1] or match[2])))
    return value, None


def looks_like_path(value, explicit=False):
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value): return False
    if value.startswith('file://'): return True
    if re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', value) and not re.search(r':\d+(?::\d+)?$', value): return False
    path, _ = split_location(value)
    if not path.strip('/') or path.lower() in PRODUCT_NAMES: return False
    # Slash-prefixed web routes in test reports are not filesystem locations.
    if path.startswith('/') and Path(path).parts[1:2] in [(p,) for p in ('auth', 'api', 'login', 'logout', 'dashboard', 'settings')]:
        return False
    if re.match(r'^(?:python\d*|node|npm|npx|git|bash|sh|cat|cd)\s', path) or any(c in path for c in (';', '|', '$', '\n')): return False
    return bool(path.startswith(('/', './', '../', '~/')) or path.endswith('/') or re.search(r'\.'+EXTENSIONS+r'$', path, re.I) or (explicit and '/' in path))


def references(text):
    """No filesystem reads while rendering chat; never link code fences or web URLs."""
    result = []
    seen = set()
    for piece in re.split(r'(```[\s\S]*?(?:```|$))', str(text)[:60000]):
        if piece.startswith('```'): continue
        for match in TOKEN.finditer(piece):
            raw = match[0]
            if raw.lower().startswith(('http://', 'https://')): continue
            label, target = raw, raw
            explicit = raw.startswith(('`', '[', '"', "'"))
            if raw.startswith('['):
                label, target = raw[1:].split('](', 1)
                target = target[:-1].strip().removeprefix('<').removesuffix('>')
            elif raw[0] in ('`', '"', "'"):
                label = target = raw[1:-1]
            else:
                target = raw.rstrip('.,;')
                # A closing parenthesis commonly belongs to surrounding prose.
                while target.endswith(')') and target.count(')') > target.count('('): target = target[:-1]
                raw = label = target
            if looks_like_path(target, explicit) and raw not in seen:
                result.append({'raw': raw, 'label': label, 'path': target})
                seen.add(raw)
                if len(result) >= 40: return result
    return result


def allowed_name(path):
    parts = [p.lower() for p in Path(path).parts]
    return not (any(p in SENSITIVE_DIRS for p in parts) or any(p.startswith('.env') for p in parts)
                or any(p in SENSITIVE_NAMES for p in parts) or Path(path).suffix.lower() in ('.pem', '.key', '.p12', '.pfx'))


def roots_for(project, task=None):
    from context_engine import ProjectMemory
    memory = ProjectMemory(project)
    base, roots = memory.project, [memory.project]
    if task:
        if not isinstance(task, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task): raise ValueError('Choose a saved reply.')
        with memory.connect() as db:
            if not db.execute('SELECT 1 FROM turns WHERE task=? LIMIT 1', (task,)).fetchone():
                raise ValueError('This reply does not belong to the selected project.')
        checkpoint = hub.STATE / 'tasks' / task / 'checkpoint.json'
        try:
            if not checkpoint.is_symlink() and checkpoint.stat().st_size < 16 * 1024 * 1024:
                value = json.loads(checkpoint.read_text())
                if value.get('preview') and value.get('project') == str(memory.project):
                    base = checkpoint.parent / 'workspace'
                    roots.append(base)
        except FileNotFoundError: pass
        # Only this turn's browser artifacts, not all private Hub state.
        record = hub.STATE / 'tasks' / task / 'task.json'
        try:
            if not record.is_symlink() and record.stat().st_size < 512 * 1024:
                value = json.loads(record.read_text())
                browser = value.get('browser') or (value.get('verification') or {}).get('browser') or {}
                if re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}', browser.get('id', '')):
                    roots.append(memory.directory / 'browser-runs' / browser['id'])
        except FileNotFoundError: pass
    return base, roots


class ReferenceChoices(ValueError):
    def __init__(self, reference, matches, root, line, limited=False):
        self.reference, self.matches, self.root, self.line, self.limited = reference, matches, root, line, limited
        super().__init__('Choose the intended file or folder using its full path:\n' + '\n'.join(str(p) for p in matches))


def nested_matches(base, relative):
    """Bounded lookup only after an exact relative path is missing; never follow links."""
    pending = deque([(base, 0)])
    matches = []
    directories = entries = 0
    limited = False
    deadline = time.monotonic() + SEARCH_SECONDS
    while pending:
        if directories >= SEARCH_DIRECTORIES or entries >= SEARCH_ENTRIES or time.monotonic() >= deadline:
            return matches, True
        directory, depth = pending.popleft()
        directories += 1
        if directory != base:
            candidate = directory / relative
            try:
                if candidate.exists() and candidate.resolve() == candidate and allowed_name(candidate):
                    if candidate.is_file() or candidate.is_dir(): matches.append(candidate)
            except OSError: pass
            if len(matches) >= 20: return matches, True
        try:
            # Protect against a directory being replaced by a link during the lookup.
            descriptor = open_fd(directory)
            try:
                with os.scandir(descriptor) as items:
                    for item in items:
                        entries += 1
                        if entries >= SEARCH_ENTRIES or time.monotonic() >= deadline: return matches, True
                        if item.name in SEARCH_SKIP or not allowed_name(item.name) or item.is_symlink(): continue
                        try:
                            if item.is_dir(follow_symlinks=False):
                                if depth < 6: pending.append((directory / item.name, depth + 1))
                                else: limited = True
                        except OSError: continue
            finally: os.close(descriptor)
        except OSError: limited = True
    return matches, limited


def resolve(project, reference, task=None):
    if not isinstance(reference, str) or not reference or len(reference) > 4096 or any(ord(c) < 32 for c in reference):
        raise ValueError('Choose a local file or folder reference.')
    value, line = split_location(reference)
    if value.startswith('file:'):
        url = urlsplit(value)
        if url.scheme != 'file' or url.netloc not in ('', 'localhost') or url.query or url.fragment:
            raise ValueError('Only local file references are supported.')
        value = unquote(url.path)
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Choose a local file or folder reference.')
    elif re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', value):
        raise ValueError('Choose a local path, not a URL or command.')
    base, roots = roots_for(project, task)
    path = Path(value).expanduser()
    relative = not path.is_absolute()
    if relative: path = base / path
    path = Path(os.path.abspath(path))
    permitted = next((r for r in roots if path.is_relative_to(r)), None)
    if permitted is None: raise ValueError('This path is outside the project and this reply’s saved artifacts. Copy the path to open it manually.')
    if not allowed_name(path): raise ValueError('Credential and private configuration paths are not available in reply previews.')
    # Do not follow links into other projects or private files.
    if path.resolve() != path: raise ValueError('Symbolic links are not opened from replies. Open the original path in your file manager.')
    if not path.exists():
        # A tool can run from a package inside a monorepo and report package-relative paths.
        # Explicit absolute paths and parent traversals must never be reinterpreted.
        if relative and '..' not in Path(value).parts:
            matches, limited = nested_matches(base, Path(value))
            matches.sort(key=lambda p: str(p).casefold())
            if len(matches) == 1 and not limited: path, permitted = matches[0], base
            elif matches: raise ReferenceChoices(reference, matches, base, line, limited)
            elif limited: raise ValueError('File not found in the bounded project search. Open the project folder or use the full path.')
        if not path.exists(): raise ValueError('This file or folder no longer exists. It may have been moved or removed.')
    return path, line, permitted


def open_fd(path):
    """Open each component without following symlinks, including during a rename race."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(path.parts) - 2: flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def describe(project, reference, task=None, offset=0):
    try: path, line, root = resolve(project, reference, task)
    except ReferenceChoices as choices:
        return {'kind': 'choices', 'name': 'Choose a file or folder', 'path': reference,
                'note': 'Several project locations match this reference.' if not choices.limited else 'Project search was limited. Choose a matching location or open the project folder.',
                'entries': [{'name': str(p.relative_to(choices.root)), 'path': str(p) + (':'+str(choices.line) if choices.line else '')} for p in choices.matches]}
    if type(offset) is not int or offset < 0 or offset > 2000: raise ValueError('Invalid folder page.')
    descriptor = open_fd(path)
    try:
        info = os.fstat(descriptor)
        result = {'path': str(path), 'name': path.name, 'line': line, 'size': info.st_size,
                  'parent': str(path.parent) if path != root else None, 'modified': info.st_mtime}
        if stat.S_ISDIR(info.st_mode):
            entries = []
            scanned = 0
            with os.scandir(descriptor) as iterator:
                for item in iterator:
                    scanned += 1
                    if scanned > 2000: break
                    if item.is_symlink() or not allowed_name(path / item.name): continue
                    try:
                        is_dir = item.is_dir(follow_symlinks=False)
                        if not is_dir and not item.is_file(follow_symlinks=False): continue
                        entries.append({'name': item.name, 'path': str(path / item.name), 'kind': 'folder' if is_dir else 'file'})
                    except OSError: continue
            entries.sort(key=lambda e: (e['kind'] != 'folder', e['name'].casefold()))
            return dict(result, kind='folder', entries=entries[offset:offset+100], offset=offset, has_more=len(entries)>offset+100, scan_limited=scanned>2000)
        if not stat.S_ISREG(info.st_mode): raise ValueError('Only ordinary files and folders can be viewed.')
        suffix = path.suffix.lower()
        if suffix in ('.png', '.jpg', '.jpeg', '.gif', '.webp') and info.st_size <= IMAGE_LIMIT:
            with os.fdopen(os.dup(descriptor), 'rb') as stream: raw = stream.read(IMAGE_LIMIT+1)
            if len(raw) > IMAGE_LIMIT: raise ValueError('Image grew beyond the preview limit. Use Show in Files.')
            mime = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.webp':'image/webp'}[suffix]
            return dict(result, kind='image', image='data:'+mime+';base64,'+base64.b64encode(raw).decode())
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            tail = suffix in ('.log', '.jsonl') and not line and info.st_size > TEXT_LIMIT
            if tail: stream.seek(-TEXT_LIMIT, os.SEEK_END)
            raw = stream.read(TEXT_LIMIT+1)
        if b'\0' in raw: return dict(result, kind='binary', note='No text preview. Use Show in Files or Download.')
        truncated = info.st_size > TEXT_LIMIT
        text = raw[:TEXT_LIMIT].decode('utf-8', errors='replace')
        if tail: text = text.partition('\n')[2]
        lines = text.splitlines()
        first = max(1, line - 15) if line and line <= len(lines) else 1
        selected = lines[first-1:first+79] if line else lines[-600:] if tail else lines[:600]
        return dict(result, kind='text', text='\n'.join(selected), first_line=None if tail else first,
                    truncated=truncated or len(lines)>len(selected), tail=tail,
                    note='Latest log output (bounded preview).' if tail else 'Requested line is outside the preview limit.' if line and line>len(lines) else '')
    finally: os.close(descriptor)


def download(project, reference, task=None):
    path, _, _ = resolve(project, reference, task)
    descriptor = open_fd(path)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > DOWNLOAD_LIMIT:
            raise ValueError('Downloads are limited to ordinary files up to 20 MiB. Use Show in Files for larger files.')
        with os.fdopen(os.dup(descriptor), 'rb') as stream: data = stream.read(DOWNLOAD_LIMIT+1)
        if len(data) > DOWNLOAD_LIMIT: raise ValueError('File grew beyond the download limit.')
        return data
    finally: os.close(descriptor)


def reveal(project, reference, task=None):
    path, _, _ = resolve(project, reference, task)
    if not path.is_file() and not path.is_dir(): raise ValueError('Choose an ordinary file or folder.')
    # Always reveal in the file manager. Never execute the file or a model-supplied command.
    if path.is_file() and shutil.which('gdbus'):
        try:
            result = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.FileManager1',
                '--object-path', '/org/freedesktop/FileManager1', '--method', 'org.freedesktop.FileManager1.ShowItems',
                json.dumps([path.as_uri()]), ''], capture_output=True, timeout=5)
            if result.returncode == 0: return {'opened': str(path), 'selected': True}
        except (OSError, subprocess.TimeoutExpired): pass
    folder = path if path.is_dir() else path.parent
    command = ['gio', 'open', folder.as_uri()] if shutil.which('gio') else ['open', str(folder)] if hub.sys.platform == 'darwin' else ['xdg-open', str(folder)]
    try: result = subprocess.run(command, capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as error: raise ValueError('Could not open the file manager. Copy the path instead.') from error
    if result.returncode: raise ValueError('The desktop file manager could not open. Copy the path instead.')
    return {'opened': str(folder), 'selected': False}
