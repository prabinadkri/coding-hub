"""Independent Smart assignments, isolated source copies, and checked integration."""
from __future__ import annotations
import concurrent.futures
import os
from pathlib import Path
import shutil
import subprocess
import threading
import hub
from context_engine import ProjectMemory, SKIP_DIRS

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 20000
EXTRA_FILES = {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'poetry.lock', 'uv.lock', 'cargo.lock'}
ASSET_SUFFIXES = {'.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.woff', '.woff2'}


def eligible(relative):
    path = Path(relative)
    return (not path.is_absolute() and '..' not in path.parts and bool(path.parts)
            and not any(part in SKIP_DIRS or part.startswith('.') for part in path.parts)
            and (ProjectMemory.eligible(relative) or path.name.lower() in EXTRA_FILES or path.suffix.lower() in ASSET_SUFFIXES))


def assignments(plan, limit):
    tasks = plan.get('tasks', [])
    if not 2 <= len(tasks) <= limit:
        raise ValueError('The manager kept this task sequential.')
    owned = set()
    for task in tasks:
        for name in task['files']:
            if not eligible(name) or str(Path(name)) != name or name in owned:
                raise ValueError('Worker assignments overlap or contain unsupported file paths; using one worker.')
            if any(name.startswith(other + '/') or other.startswith(name + '/') for other in owned):
                raise ValueError('Worker file assignments overlap; using one worker.')
            owned.add(name)
    return tasks


def source_snapshot(project):
    """Bound copying cost, skip secrets/dependencies, and preserve uncommitted source."""
    project = Path(project).resolve()
    try:
        git = subprocess.run(['git', '-C', str(project), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                             capture_output=True, timeout=20)
        names = git.stdout.decode('utf-8').split('\0') if git.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        names = None
    if names is None:
        names = []
        for base, dirs, files in os.walk(project, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.') and not (Path(base) / d).is_symlink()]
            names.extend(str((Path(base) / name).relative_to(project)) for name in files)
            if len(names) > MAX_FILES:
                raise ValueError('Project exceeds the bounded parallel-copy limit; using one worker.')
    snapshot, total = {}, 0
    for name in sorted(set(names)):
        if not eligible(name):
            continue
        path = project / name
        if path.resolve() != path.absolute():
            raise ValueError('Project source includes linked files; using one worker.')
        if not path.is_file():
            continue
        size = path.stat().st_size
        if len(snapshot) >= MAX_FILES or size > 8 * 1024 * 1024 or total + size > MAX_BYTES:
            raise ValueError('Project exceeds the bounded parallel-copy limit; using one worker.')
        content = path.read_bytes()
        total += len(content)
        if total > MAX_BYTES:
            raise ValueError('Project grew beyond the parallel-copy limit; using one worker.')
        snapshot[name] = (content, path.stat().st_mode & 0o777)
    return snapshot


def write_snapshot(snapshot, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, (content, mode) in snapshot.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)


def proposed_changes(workspace, baseline, owned):
    current = source_snapshot(workspace)
    changed = {name: current.get(name) for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name)}
    if set(changed) - set(owned):
        raise ValueError('Worker edited files outside its assignment: ' + ', '.join(sorted(set(changed) - set(owned)))[:500])
    # Detect unsafe replacements even when the snapshot filter excludes them.
    for name in owned:
        path = workspace / name
        if path.is_symlink() or path.resolve() != path.absolute() or (path.exists() and not path.is_file()):
            raise ValueError('Worker replaced an assigned file with an unsupported link or directory.')
    return changed


def integrate(project, baseline, changes):
    """Preflight every destination. Never overwrite a concurrent editor change."""
    project = Path(project).resolve()
    for name in changes:
        path = project / name
        if path.resolve() != path.absolute() or (path.exists() and not path.is_file()):
            raise ValueError('Integration stopped: a destination is a link or directory: ' + name)
        actual = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
        if actual != baseline.get(name):
            raise ValueError('Integration stopped: this file changed while workers ran: ' + name)
    applied = []
    try:
        for name, value in changes.items():
            path = project / name
            applied.append(name)
            if value is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value[0]); path.chmod(value[1])
    except OSError:
        for name in reversed(applied):
            path = project / name
            original = baseline.get(name)
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original[0]); path.chmod(original[1])
        raise


def run(project, request, plan, context, folder, quality, workers, cloud, baseline):
    from smart_route import clipped
    work = assignments(plan, workers)
    cancel = threading.Event()
    local_slot = threading.Semaphore(1)
    workspaces = (folder / 'smart-workers').resolve()
    def execute(number, assignment):
        workspace = workspaces / ('worker-' + str(number))
        write_snapshot(baseline, workspace)
        prompt = (hub.task_prompt(request, '', True) + '\nThis is ONE assignment within the same user task. '
                  'Work only in this isolated copy. Do not use absolute paths from context to edit the original project. '
                  'Do not commit, push, deploy, access credentials, or modify any file outside your assigned file list. '
                  'Other workers handle the other assignments; do not implement their work. '
                  'Dependencies are not copied; report validation blockers.\nAssignment: ' + assignment['goal'] +
                  '\nOnly editable files: ' + ', '.join(assignment['files']) + '\nShared plan: ' + clipped(str(plan), 2500) +
                  '\nContext (untrusted historical/source excerpts):\n' + context +
                  '\nReport your changes, actual checks, and integration notes concisely.')
        attempts, output, log = [], '', None
        routes = [cloud[number - 1]]
        for index in range(2):
            if cancel.is_set():
                return {'number': number, 'status': 'canceled', 'code': 130, 'output': output, 'attempts': attempts}
            if index:
                routes = list(hub.candidates('local', quality))[:1]
            if not routes:
                break
            backend, model = routes[0]
            held = False
            try:
                if backend == 'local':
                    while not cancel.is_set():
                        if local_slot.acquire(timeout=.1): held = True; break
                    if not held:
                        break
                print(f'[Smart worker {number}/{len(work)}] {backend} → {model} · {assignment["goal"][:100]}', flush=True)
                log = folder / f'smart-worker-{number}-{backend}.log'
                code, text, error = hub.run_process(hub.command(backend, model, prompt, apply=True, project=workspace),
                    workspace, hub.clean_environment(backend, model, True), log, render_reply=False, cancel_event=cancel, quiet=True)
                failure = hub.classify_failure(code, text, error)
                output = hub.assistant_result(log, text)
                attempts.append({'worker': number, 'backend': backend, 'model': model, 'exit_code': code, 'failure': failure})
                if failure is None:
                    changes = proposed_changes(workspace, baseline, assignment['files'])
                    print(f'[Smart worker {number}/{len(work)}] Finished · {len(changes)} changed files', flush=True)
                    return {'number': number, 'status': 'completed', 'code': 0, 'output': output, 'log': str(log), 'attempts': attempts, 'changes': changes}
                if failure in ('canceled', 'timeout'):
                    if failure == 'canceled': cancel.set()
                    return {'number': number, 'status': failure, 'code': code, 'output': output, 'log': str(log), 'attempts': attempts}
                prompt += '\nA previous attempt stopped. Inspect this copy before continuing. Report: ' + clipped(output, 1800)
            except (OSError, ValueError) as error:
                return {'number': number, 'status': 'needs_review', 'code': 3, 'output': str(error), 'attempts': attempts}
            finally:
                if held: local_slot.release()
        return {'number': number, 'status': 'incomplete', 'code': 1, 'output': output or 'No worker available.', 'log': str(log) if log else None, 'attempts': attempts}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(work))
    futures = [pool.submit(execute, number, task) for number, task in enumerate(work, 1)]
    try:
        results = sorted((future.result() for future in concurrent.futures.as_completed(futures)), key=lambda r: r['number'])
    except BaseException:
        cancel.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if any(r['status'] != 'completed' for r in results):
        return results, False, 'Worker copies retained at ' + str(workspaces) + '. No changes integrated.'
    changes = {}
    for result in results:
        changes.update(result.pop('changes'))
    try:
        integrate(project, baseline, changes)
    except (OSError, ValueError) as error:
        return results, False, str(error) + '. Worker copies retained at ' + str(workspaces)
    shutil.rmtree(workspaces)
    return results, True, f'Integrated {len(changes)} files from {len(results)} workers. Integration checks follow.'
