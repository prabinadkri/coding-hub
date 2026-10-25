"""Optional Playwright checks against a user-selected local application."""
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
import hub


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('Browser settings must be an object.')
    url, steps = value.get('url', ''), value.get('steps', [])
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError('Enter a local application URL.')
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password:
            raise ValueError('Browser tests support localhost URLs only.')
    if not isinstance(steps, list) or len(steps) > 40:
        raise ValueError('Use up to 40 browser steps.')
    for step in steps:
        if not isinstance(step, dict) or step.get('action') not in ('click', 'fill', 'text', 'visible', 'url'):
            raise ValueError('Browser actions: click, fill, text, visible, url.')
        if any(not isinstance(v, str) or len(v) > 2000 for v in step.values()) or set(step) - {'action', 'selector', 'value'}:
            raise ValueError('Invalid browser step.')
        if step['action'] != 'url' and not step.get('selector'):
            raise ValueError('Each browser step needs a selector.')
        if step['action'] in ('fill', 'text', 'url') and 'value' not in step:
            raise ValueError('This browser step needs a value.')
    return {'url': url, 'steps': steps}


def run(project, config=None):
    from project_checks import profile
    from context_engine import ProjectMemory
    memory = ProjectMemory(project)
    config = validate(config or profile(project)['browser'])
    if not config['url']:
        raise ValueError('Set a localhost browser URL in Project checks first and start that application.')
    if not hub.executable('node'):
        raise ValueError('Install Node.js and the optional browser test dependencies described in README.')
    identifier = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    folder = hub.private_dir(memory.directory / 'browser-runs' / identifier)
    hub.save_json(folder / 'config.json', config)
    code, output, _ = hub.run_process([hub.executable('node'), str(hub.ROOT / 'tools/browser_check.mjs'), str(folder)],
        project, dict(os.environ), folder / 'runner.log', timeout=180, render_reply=False, quiet=True)
    try: result = json.loads((folder / 'result.json').read_text())
    except (OSError, ValueError):
        result = {'status': 'failed', 'error': output[-3000:] or 'Browser runner did not finish. Install optional Playwright dependencies.', 'steps': []}
    result.update(id=identifier, project=str(memory.project), exit_code=code, directory=str(folder))
    hub.save_json(folder / 'result.json', result)
    hub.save_json(memory.directory / 'browser-latest.json', result)
    return result
