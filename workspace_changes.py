"""Private source checkpoints and conflict-checked, file-selective apply/undo."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import time
import hub
from parallel_workers import source_snapshot, write_snapshot, integrate, eligible
from change_review import compare


def store(folder, snapshot):
    blobs = hub.private_dir(folder / 'blobs')
    manifest = {}
    for name, (raw, mode) in snapshot.items():
        digest = hashlib.sha256(raw).hexdigest()
        path = blobs / digest
        if not path.exists():
            path.write_bytes(raw)
            path.chmod(0o600)
        manifest[name] = {'blob': digest, 'mode': mode}
    return manifest


def restore(folder, manifest):
    result = {}
    for name, item in manifest.items():
        if not eligible(name) or not re.fullmatch('[a-f0-9]{64}', item['blob']):
            raise ValueError('Invalid checkpoint contents.')
        raw = (folder / 'blobs' / item['blob']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['blob']:
            raise ValueError('Checkpoint contents are damaged.')
        result[name] = (raw, item['mode'])
    return result


def text_snapshot(snapshot):
    files = {}
    for name, (raw, _) in snapshot.items():
        if len(raw) <= 262144 and b'\0' not in raw:
            try: files[name] = raw.decode('utf-8')
            except UnicodeError: pass
    return {'files': files, 'paths': set(snapshot), 'covered': len(files), 'eligible': len(snapshot), 'limited': len(files) != len(snapshot)}


@contextmanager
def session(project, folder, enabled, preview=False):
    project = Path(project).expanduser().resolve()
    if not enabled:
        yield project
        return
    before = source_snapshot(project)  # Refuse an incomplete checkpoint before editing.
    checkpoint = {'project': str(project), 'task': folder.name, 'preview': preview,
                  'before': store(folder, before), 'after': {}, 'applied': [], 'ready': False}
    hub.save_json(folder / 'checkpoint.json', checkpoint)
    working = folder / 'workspace' if preview else project
    if preview:
        write_snapshot(before, working)
        print('[Preview] Working in a source copy. Project edits require Apply in View changes.', flush=True)
    try:
        yield working
    finally:
        try:
            after = source_snapshot(working)
            names = sorted(n for n in before.keys() | after.keys() if before.get(n) != after.get(n))
            checkpoint.update(after=store(folder, after), changed=names, ready=True,
                              applied=[] if preview else names)
            hub.save_json(folder / 'checkpoint.json', checkpoint)
            review = compare(text_snapshot(before), text_snapshot(after))
            review.update(project=str(project), task=folder.name, saved_at=time.time(), recoverable=names,
                          preview=preview, applied=checkpoint['applied'],
                          note='Captured source/assets only; dependencies, hidden files, ignored files and sensitive names are excluded. '
                               'This is a source copy, not an OS sandbox. Commands and external side effects cannot be undone.' +
                               (' Direct-mode snapshots may include concurrent editor changes; inspect before undo.' if not preview else ''))
            hub.save_json(folder / 'changes.json', review)
            print(f'[Changes] {len(names)} recoverable file changes · /changes to review.', flush=True)
        except (OSError, ValueError) as error:
            print('[Changes] Checkpoint could not be finalized: ' + str(error), flush=True)


def change(project, task, action, files):
    from change_review import load
    project = Path(project).expanduser().resolve()
    if action not in ('apply', 'undo') or not isinstance(files, list) or not files or any(not isinstance(n, str) for n in files):
        raise ValueError('Choose Apply or Undo and at least one captured file.')
    with hub.project_lock(project):
        review = load(project, task)  # Validate ownership through project conversation history.
        folder = hub.STATE / 'tasks' / task
        value = json.loads((folder / 'checkpoint.json').read_text())
        if value['project'] != str(project) or not value['ready'] or set(files) - set(value['changed']):
            raise ValueError('Choose files from a finished checkpoint for this project.')
        applied = set(value['applied'])
        if action == 'apply' and (not value['preview'] or set(files) & applied):
            raise ValueError('These files are already applied.')
        if action == 'undo' and not set(files) <= applied:
            raise ValueError('Only applied files can be undone.')
        before, after = restore(folder, value['before']), restore(folder, value['after'])
        expected, desired = (before, after) if action == 'apply' else (after, before)
        integrate(project, expected, {n: desired.get(n) for n in files})
        applied = applied | set(files) if action == 'apply' else applied - set(files)
        value['applied'] = sorted(applied)
        hub.save_json(folder / 'checkpoint.json', value)
        review['applied'] = value['applied']
        hub.save_json(folder / 'changes.json', review)
        return review
