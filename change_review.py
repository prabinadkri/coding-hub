"""Bounded, read-only before/after reviews stored outside the user's project."""
from contextlib import contextmanager
import difflib
import json
from pathlib import Path
import re
import time

import hub

MAX_FILE = 262144
MAX_BYTES = 32 * 1024 * 1024
MAX_DIFF = 200000


def snapshot(project):
    from context_engine import ProjectMemory
    memory = ProjectMemory(project)
    paths = memory.paths()
    present = set(paths)
    values, skipped, used = {}, 0, 0
    started = time.monotonic()
    for name in paths:
        if used >= MAX_BYTES or time.monotonic() - started > 5:
            break
        path = memory.project / name
        try:
            if not path.exists() and not path.is_symlink():
                present.discard(name)
                continue
            if path.resolve() != path.absolute() or not path.is_file():
                skipped += 1
                continue
            if path.stat().st_size > MAX_FILE:
                skipped += 1
                continue
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE + 1)
            used += len(raw)
            if len(raw) > MAX_FILE or b'\0' in raw:
                skipped += 1
                continue
            values[name] = raw.decode('utf-8')
        except (OSError, UnicodeError):
            skipped += 1
    return {'files': values, 'paths': present, 'covered': len(values), 'eligible': len(paths),
            'limited': len(values) + skipped < len(paths) or skipped > 0}


def compare(before, after):
    files, size, omitted = [], 0, 0
    names = sorted(set(before['files']) | set(after['files']))
    for name in names:
        # A file skipped by either scan must not look like an addition/deletion.
        if any(name in scan['paths'] and name not in scan['files'] for scan in (before, after)):
            continue
        old, new = before['files'].get(name, ''), after['files'].get(name, '')
        if old == new and (name in before['files']) == (name in after['files']):
            continue
        status = 'added' if name not in before['files'] else 'deleted' if name not in after['files'] else 'modified'
        # autojunk avoids quadratic work on large repetitive generated files.
        old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
        diff = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                      for line in difflib.unified_diff(old_lines, new_lines,
                        fromfile='a/' + name if status != 'added' else '/dev/null',
                        tofile='b/' + name if status != 'deleted' else '/dev/null'))
        lines = diff.splitlines()
        additions = sum(line.startswith('+') and not line.startswith('+++') for line in lines)
        deletions = sum(line.startswith('-') and not line.startswith('---') for line in lines)
        if len(files) >= 64 or size + len(diff.encode()) > MAX_DIFF:
            omitted += 1
            continue
        size += len(diff.encode())
        files.append({'path': name, 'status': status, 'added': additions, 'removed': deletions,
                      'diff': diff or 'Empty file ' + status + '.'})
    return {'files': files, 'omitted_files': omitted,
            'limited': before['limited'] or after['limited'] or omitted > 0,
            'covered_before': before['covered'], 'covered_after': after['covered'],
            'note': 'Source files changed during this task. Concurrent editor changes may also appear. '
                    'Ignored, binary, sensitive-name and oversized files are excluded.'}


@contextmanager
def capture(project, folder, enabled):
    before = None
    if enabled:
        try:
            before = snapshot(project)
        except (OSError, ValueError):
            pass
    try:
        yield
    finally:
        if enabled:
            try:
                result = compare(before, snapshot(project)) if before else {
                    'files': [], 'limited': True, 'note': 'The before-task snapshot was unavailable.'}
                result.update(project=str(project), task=folder.name, saved_at=time.time())
                hub.save_json(folder / 'changes.json', result)
                if result['files']:
                    print(f"\nReview: {len(result['files'])} changed source file(s). Use View changes or :changes.", flush=True)
            except (OSError, ValueError) as error:
                print('Change review unavailable: ' + str(error), flush=True)


def load(project, task=None):
    from context_engine import ProjectMemory
    memory = ProjectMemory(project)
    with memory.connect() as db:
        if task:
            if not isinstance(task, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task):
                raise ValueError('Choose a saved task.')
            row = db.execute('SELECT task FROM turns WHERE task=? LIMIT 1', (task,)).fetchone()
        else:
            row = db.execute('SELECT task FROM turns ORDER BY rowid DESC LIMIT 1').fetchone()
        if not row:
            raise ValueError('No saved task was found in this project.')
    path = hub.STATE / 'tasks' / row['task'] / 'changes.json'
    if not path.is_file():
        return {'files': [], 'limited': False, 'note': 'No change snapshot for this task. Reviews are captured for new tasks with edits enabled.'}
    value = json.loads(path.read_text())
    if value.get('project') != str(memory.project):
        raise ValueError('This review belongs to a different project.')
    return value


def show(project, task=None):
    result = load(project, task)
    print('\nChanges · ' + result['note'])
    if result.get('limited'):
        print('Partial review: some files or diff content exceeded the review limits.')
    if not result['files']:
        print('No captured source changes.')
    for item in result['files']:
        print(f"\n{item['path']} · {item['status']} · +{item['added']} −{item['removed']}\n")
        print(item['diff'])
