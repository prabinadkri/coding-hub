"""User-configured independent checks. Configuration lives outside the project."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import time
import hub

DEFAULT = {'commands': [], 'timeout': 180, 'repairs': 0, 'browser': {'url': '', 'steps': [], 'command': '', 'automatic': True}}


def profile(project, value=None):
    from context_engine import ProjectMemory
    path = ProjectMemory(project).directory / 'checks.json'
    if value is not None:
        commands = value.get('commands', [])
        if not isinstance(commands, list) or len(commands) > 8:
            raise ValueError('Use up to eight check commands.')
        for command in commands:
            if not isinstance(command, str) or not command.strip() or len(command) > 2000 or not shlex.split(command):
                raise ValueError('Enter a valid command. Shell operators require an explicit shell command.')
        timeout, repairs = value.get('timeout', 180), value.get('repairs', 0)
        if type(timeout) is not int or not 5 <= timeout <= 600 or type(repairs) is not int or not 0 <= repairs <= 2:
            raise ValueError('Check timeout must be 5–600 seconds; repair attempts must be 0–2.')
        from browser_checks import validate
        browser = validate(value.get('browser', DEFAULT['browser']))
        hub.save_json(path, dict(commands=commands, timeout=timeout, repairs=repairs, browser=browser))
    try:
        value = json.loads(path.read_text())
        from browser_checks import validate
        value['browser'] = validate(value.get('browser', {}))
        return value
    except FileNotFoundError:
        return dict(DEFAULT, commands=[], browser=dict(DEFAULT['browser']))


def run(project, config, folder, label='checks'):
    """Run fixed argv commands; exit status, not an agent claim, determines success."""
    results = []
    for number, command in enumerate(config['commands'], 1):
        log = folder / f'{label}-{number}.log'
        started = time.monotonic()
        print(f'[Verification] {command}', flush=True)
        try:
            code, output, _ = hub.run_process(shlex.split(command), project, dict(os.environ),
                                             log, timeout=config['timeout'], render_reply=False, quiet=True)
        except OSError as error:
            code, output = 127, str(error)
        results.append({'command': command, 'exit_code': code, 'seconds': round(time.monotonic()-started, 2),
                        'output': output[-6000:], 'log': str(log)})
        if code in (130, 143, -2, -15):
            break
    status = 'not_verified' if not results else 'passed' if all(r['exit_code'] == 0 for r in results) else 'failed'
    result = {'status': status, 'checks': results, 'checked_at': time.time()}
    hub.save_json(folder / f'{label}.json', result)
    return result


def finish(project, request, config, folder, apply, baseline=None, memory_project=None, preview=False):
    def evaluate(label='checks'):
        result = run(project, config, folder, label)
        if any(c['exit_code'] in (130, 143, -2, -15) for c in result['checks']): return result
        from browser_checks import after_edits
        browser = after_edits(project, config['browser'], memory_project=memory_project, preview=preview) if apply else None
        if browser:
            result = dict(result, browser=browser)
            if browser['status'] in ('failed', 'canceled'): result['status'] = 'failed'
            elif browser['status'] == 'passed' and result['status'] == 'not_verified': result['status'] = 'passed'
        return result
    result = evaluate()
    attempts = []
    canceled = lambda r: r.get('browser', {}).get('status') == 'canceled' or any(c['exit_code'] in (130, 143, -2, -15) for c in r['checks'])
    # Repairs use only verified-free/local routes, never the premium manager.
    for number in range(config['repairs'] if apply and not canceled(result) else 0):
        if result['status'] != 'failed':
            break
        failure = json.dumps({'checks': result['checks'], 'browser': result.get('browser')}, ensure_ascii=False)[-9000:]
        prompt = ('Repair only failures related to the original task. Preserve unrelated work. '
                  'Do not weaken, remove, or skip tests to pass checks. Do not commit or publish.\nTask:\n' +
                  request[:6000] + '\nIndependent check results (untrusted data):\n' + failure)
        routes = iter(hub.route_options('free-auto', 'fast'))
        candidate = next(routes, None)
        if not candidate:
            break
        backend, model = candidate
        log = folder / f'repair-{number+1}.log'
        print(f'[Verification repair {number+1}/2] {backend} → {model}', flush=True)
        try:
            code, output, error = hub.run_process(hub.command(backend, model, prompt, True, project=project), project,
                hub.clean_environment(backend, model, True), log, render_reply=False)
        except OSError as problem:
            code, output, error = 1, str(problem), True
        attempts.append({'backend': backend, 'model': model, 'exit_code': code})
        if hub.classify_failure(code, output, error):
            break
        result = evaluate(f'checks-repair-{number+1}')
        if canceled(result):
            break
    result.update(repair_attempts=attempts, baseline=baseline,
                  note='Independent verification records command exits and browser results. Coverage is limited to the checks that actually ran.')
    hub.save_json(folder / 'verification.json', result)
    print('[Verification] ' + result['status'].replace('_', ' '), flush=True)
    return result
