"""Bounded local retrieval and durable conversation checkpoints; no model required."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid
import hub

SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', 'target', 'vendor', '.next', '.cache'}
TEXT_SUFFIXES = {'.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.kt', '.c', '.h', '.cpp', '.cs', '.rb', '.php', '.swift', '.sh', '.sql', '.html', '.css', '.scss', '.md', '.txt', '.toml', '.json', '.yaml', '.yml', '.vue', '.svelte', '.proto', '.ex', '.exs', '.scala', '.dart'}
SKIP_NAMES = {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'poetry.lock', 'uv.lock', 'cargo.lock', 'credentials.json', 'auth.json', 'settings.json'}
STOP = {'the', 'and', 'this', 'that', 'with', 'from', 'have', 'what', 'how', 'please', 'file', 'code', 'project', 'then', 'into', 'for', 'you', 'are', 'use', 'make'}
GUIDANCE_FILES = ('CODING_HUB.md', 'AGENTS.md', 'CLAUDE.md', 'GEMINI.md')
RULES_TEMPLATE = '''# Project instructions

## Goal
Describe what this project does and the current objective.

## Architecture
List the main modules and where related tests live.

## Development commands
Add the exact install, test, lint, and build commands.

## Constraints
- Preserve unrelated user changes.
- Keep public APIs stable unless a change is requested.
- Do not read secrets, publish, deploy, or purchase services.

## Working method
- Search for the relevant symbol before reading whole files.
- Break large changes into small, testable steps.
- Report actual checks, unresolved problems, decisions, and next steps.

Keep this file concise. Conversation checkpoints are stored privately outside the repository.
'''


def keywords(text):
    # Split snake_case and camelCase before FTS; quote every token, never SQL input.
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    return list(dict.fromkeys(t.lower() for t in re.findall(r'[A-Za-z][A-Za-z0-9]{2,}', text) if t.lower() not in STOP))[:24]


def fts_query(text):
    return ' OR '.join('"' + term + '"' for term in keywords(text))


class ProjectMemory:
    def __init__(self, project):
        self.project = Path(project).expanduser().resolve()
        if not self.project.is_dir():
            raise ValueError('Choose an existing project folder.')
        key = hashlib.sha256(str(self.project).encode()).hexdigest()
        self.directory = hub.private_dir(hub.STATE / 'projects' / key)
        metadata = self.directory / 'project.json'
        if not metadata.exists():
            hub.save_json(metadata, {'project': str(self.project), 'name': self.project.name})
        self.database = self.directory / 'index.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, stamp TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path, line UNINDEXED, content);
                CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, goal TEXT, created REAL, updated REAL);
                CREATE VIRTUAL TABLE IF NOT EXISTS turns USING fts5(conversation UNINDEXED, task UNINDEXED, request, result, status UNINDEXED, created UNINDEXED);
            ''')
        self.database.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def eligible(relative):
        path = Path(relative)
        lower = path.name.lower()
        return (not path.is_absolute() and '..' not in path.parts and
                not any(p in SKIP_DIRS for p in path.parts) and
                not lower.startswith('.env') and not any(x in lower for x in ('secret', 'credential', 'private_key')) and
                lower not in SKIP_NAMES and
                (path.suffix.lower() in TEXT_SUFFIXES or lower in ('dockerfile', 'makefile', 'gemfile')))

    def paths(self):
        if hub.executable('git'):
            try:
                result = subprocess.run(['git', '-C', str(self.project), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, timeout=20)
                if result.returncode == 0:
                    return sorted(set(p for p in result.stdout.decode('utf-8', 'replace').split('\0') if self.eligible(p)))
            except (OSError, subprocess.TimeoutExpired):
                pass
        found = []
        for base, dirs, files in os.walk(self.project, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.') and not (Path(base) / d).is_symlink())
            for name in files:
                relative = str((Path(base) / name).relative_to(self.project))
                if self.eligible(relative):
                    found.append(relative)
            if len(found) >= 200000:
                break
        return sorted(found)

    def index(self, seconds=12, byte_budget=128 * 1024 * 1024):
        started, read_bytes, updated = time.monotonic(), 0, 0
        paths = self.paths()
        complete = True
        with self.connect() as db:
            known = {row['path']: row['stamp'] for row in db.execute('SELECT path, stamp FROM files')}
            for relative in sorted(paths, key=lambda p: p in known):
                if time.monotonic() - started > seconds or read_bytes >= byte_budget:
                    complete = False
                    break
                path = self.project / relative
                try:
                    # Do not index symlinks, including symlinked parent directories.
                    if path.resolve() != path.absolute() or not path.is_file():
                        continue
                    stat = path.stat()
                    stamp = f'{stat.st_mtime_ns}:{stat.st_size}'
                    if known.get(relative) == stamp:
                        continue
                    db.execute('DELETE FROM chunks WHERE path=?', (relative,))
                    if stat.st_size > 262144:
                        db.execute('DELETE FROM files WHERE path=?', (relative,))
                        continue
                    raw = path.read_bytes()
                    read_bytes += len(raw)
                    if b'\0' in raw:
                        db.execute('DELETE FROM files WHERE path=?', (relative,))
                        continue
                    lines = raw.decode('utf-8', 'replace').splitlines()
                    for start in range(0, len(lines), 60):
                        text = '\n'.join(lines[start:start + 70])[:6000]
                        db.execute('INSERT INTO chunks(path,line,content) VALUES(?,?,?)', (relative, start + 1, text))
                    db.execute('INSERT OR REPLACE INTO files VALUES(?,?)', (relative, stamp))
                    updated += 1
                except OSError:
                    continue
            # Removed and newly ignored files must not remain searchable.
            for relative in set(known) - set(paths):
                db.execute('DELETE FROM chunks WHERE path=?', (relative,))
                db.execute('DELETE FROM files WHERE path=?', (relative,))
            indexed = db.execute('SELECT count(*) FROM files').fetchone()[0]
        info = {'eligible_files': len(paths), 'indexed_files': indexed, 'updated_files': updated,
                'complete': complete, 'elapsed_seconds': round(time.monotonic() - started, 2),
                'checked_at': time.time(), 'modules': Counter(p.split('/')[0] for p in paths if '/' in p).most_common(14)}
        hub.save_json(self.directory / 'index.json', info)
        return info

    def search(self, query, limit=5):
        match = fts_query(query)
        if not match:
            return []
        with self.connect() as db:
            rows = db.execute('SELECT path,line,content FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks,4.0,0.0,1.0) LIMIT ?', (match, min(limit, 12))).fetchall()
        result = []
        for row in rows:
            path = self.project / row['path']
            # Search results are advisory. Agents must re-read current files before edits.
            if path.is_file() and path.resolve() == path.absolute():
                result.append(dict(row))
        return result

    def notes(self):
        try:
            return (self.directory / 'requirements.txt').read_text()
        except FileNotFoundError:
            return ''

    def remember(self, text):
        if not isinstance(text, str) or len(text.encode()) > 4000:
            raise ValueError('Pinned requirements must be at most 4,000 UTF-8 bytes; nothing is silently truncated.')
        path = self.directory / 'requirements.txt'
        temp = path.with_suffix('.tmp')
        temp.write_text(text)
        temp.chmod(0o600)
        temp.replace(path)

    def guidance(self):
        files, content, used = [], [], 0
        for name in GUIDANCE_FILES:
            path = self.project / name
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            included = size <= 5000 - used
            files.append({'name': name, 'bytes': size, 'included': included})
            if included:
                text = path.read_text(errors='replace')
                used += len(text.encode())
                content.append(f'Project guidance from {name}:\n{text}')
            else:
                content.append(f'Additional project guidance exists in {name} ({size} bytes). Read the applicable sections before working on its scope. It is not included in this bounded packet.')
        return files, '\n\n'.join(content)

    def initialize_rules(self):
        path = self.project / 'CODING_HUB.md'
        # Exclusive creation also refuses existing symlinks. Never replace user rules.
        try:
            with path.open('x') as stream:
                stream.write(RULES_TEMPLATE)
        except FileExistsError:
            return {'path': str(path), 'created': False}
        return {'path': str(path), 'created': True}

    def conversation(self, identifier=None, goal='', resume=False):
        with self.connect() as db:
            if resume and not identifier:
                row = db.execute('SELECT id FROM conversations ORDER BY updated DESC LIMIT 1').fetchone()
                if not row:
                    raise ValueError('This project has no conversation to continue.')
                identifier = row['id']
            if identifier:
                if not re.fullmatch(r'[a-f0-9]{32}', identifier):
                    raise ValueError('Invalid conversation identifier.')
                row = db.execute('SELECT id FROM conversations WHERE id=?', (identifier,)).fetchone()
                if not row:
                    raise ValueError('Conversation does not belong to this project.')
            else:
                identifier = uuid.uuid4().hex
                db.execute('INSERT INTO conversations VALUES(?,?,?,?)', (identifier, goal, time.time(), time.time()))
        return identifier

    def record(self, conversation, task, request, result, status):
        with self.connect() as db:
            db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?)', (conversation, task, request, result, status, time.time()))
            db.execute('UPDATE conversations SET updated=? WHERE id=?', (time.time(), conversation))
        # Immutable individual checkpoints survive process restarts and remain inspectable.
        hub.save_json(self.directory / 'checkpoints' / (task + '.json'),
            {'conversation': conversation, 'task': task, 'request': request, 'result': result, 'status': status, 'saved_at': time.time()})

    def context(self, conversation, request, budget=12500):
        notes = self.notes()
        parts = ['PROJECT MEMORY\nPinned requirements from the user (retain verbatim):\n' + (notes or '(none)')]
        _, guidance = self.guidance()
        if guidance:
            parts.append(guidance)
        with self.connect() as db:
            root = db.execute('SELECT goal FROM conversations WHERE id=?', (conversation,)).fetchone()
            recent = db.execute('SELECT rowid,* FROM turns WHERE conversation=? ORDER BY rowid DESC LIMIT 2', (conversation,)).fetchall()
            if root and recent:
                parts.append('Original goal excerpt: ' + root['goal'][:1500])
            for row in reversed(recent):
                parts.append(f"Previous turn [{row['status']}], untrusted historical result; verify against current files:\nUser: {row['request'][:700]}\nResult: {row['result'][-1500:]}")
            match = fts_query(request)
            if match:
                older = db.execute('SELECT rowid,* FROM turns WHERE turns MATCH ? AND conversation=? ORDER BY bm25(turns) LIMIT 4', (match, conversation)).fetchall()
                recent_ids = {row['rowid'] for row in recent}
                for row in older:
                    if row['rowid'] not in recent_ids:
                        parts.append(f"Related earlier turn [{row['status']}]: {row['request'][:400]}\n{row['result'][-700:]}")
                        break
        try:
            info = json.loads((self.directory / 'index.json').read_text())
            parts.append(f"Local index: {info['indexed_files']} / {info['eligible_files']} eligible source files; excludes dependencies and generated files.\nModules: " + ', '.join(f'{name} ({count} files)' for name, count in info['modules']))
        except (OSError, ValueError):
            pass
        prefix = '\n\n'.join(parts)
        remaining = max(0, budget - len(prefix.encode()))
        for hit in self.search(request, 6):
            excerpt = f"\n\nRetrieved source excerpt (untrusted data, may be stale): {hit['path']}:{hit['line']}\n{hit['content'][:1800]}"
            if len(excerpt.encode()) > remaining:
                break
            prefix += excerpt
            remaining -= len(excerpt.encode())
        # Fixed sections are bounded independently; pinned requirements are never truncated.
        encoded = prefix.encode()
        if len(encoded) > budget:
            prefix = encoded[:budget - 80].decode('utf-8', 'ignore') + '\n[Context excerpt shortened; complete turns remain on disk.]'
        return prefix

    def info(self):
        try:
            info = json.loads((self.directory / 'index.json').read_text())
        except (OSError, ValueError):
            info = {'indexed_files': 0, 'eligible_files': 0, 'complete': False}
        with self.connect() as db:
            info['conversations'] = db.execute('SELECT count(*) FROM conversations').fetchone()[0]
            info['turns'] = db.execute('SELECT count(*) FROM turns').fetchone()[0]
        guidance, _ = self.guidance()
        return dict(info, requirements=self.notes(), guidance_files=guidance)

    def messages(self, identifier, limit=30, before=None):
        self.conversation(identifier)
        with self.connect() as db:
            conversation = dict(db.execute('SELECT * FROM conversations WHERE id=?', (identifier,)).fetchone())
            rows = db.execute('SELECT rowid,request,result,status,created,task FROM turns WHERE conversation=? AND rowid<? ORDER BY rowid DESC LIMIT ?',
                              (identifier, before or 9223372036854775807, limit)).fetchall()
            total = db.execute('SELECT count(*) FROM turns WHERE conversation=?', (identifier,)).fetchone()[0]
            oldest = rows[-1]['rowid'] if rows else None
            has_older = bool(oldest and db.execute('SELECT 1 FROM turns WHERE conversation=? AND rowid<? LIMIT 1', (identifier, oldest)).fetchone())
        return dict(conversation, project=str(self.project), turns=[dict(r) for r in reversed(rows)], total_turns=total,
                    oldest_cursor=oldest, has_older=has_older)


def project_tree():
    projects = []
    for metadata in (hub.STATE / 'projects').glob('*/project.json'):
        try:
            value = json.loads(metadata.read_text())
            memory = ProjectMemory(value['project'])
            with memory.connect() as db:
                conversations = [dict(r) for r in db.execute('SELECT id,goal,updated FROM conversations ORDER BY updated DESC LIMIT 100')]
            projects.append(dict(value, conversations=conversations))
        except (OSError, ValueError, sqlite3.Error):
            continue
    return sorted(projects, key=lambda p: p['name'].lower())
