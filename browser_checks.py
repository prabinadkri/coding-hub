"""Optional Playwright checks against a user-selected local application."""
import json
import os
import re
import shlex
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
        if parsed.port == 0:
            raise ValueError('Enter a localhost URL with a usable port.')
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
    command, automatic = value.get('command', ''), value.get('automatic', True)
    if not isinstance(command, str) or len(command) > 2000 or (command and not shlex.split(command)):
        raise ValueError('Enter a start command with arguments, or leave it blank for discovery.')
    if type(automatic) is not bool:
        raise ValueError('Automatic browser checks must be on or off.')
    if command and not url:
        raise ValueError('Set a localhost URL alongside the custom start command.')
    return {'url': url, 'steps': steps, 'command': command, 'automatic': automatic}


def chat_request(text):
    """Recognize explicit test requests, without stealing mixed coding tasks from the agent."""
    text = text.strip()
    if text == '/browser-test': return {}
    if text.startswith('/browser-test '):
        return {'url': validate({'url': text.split(None, 1)[1].strip()})['url']}
    simple = re.sub(r'[.!?]+$', '', text.lower()).strip()
    simple = re.sub(r'^(?:can|could|would) you ', '', simple)
    if re.fullmatch(r'(?:please )?(?:test|check) (?:this |my |the )?(?:website|web app|webapp|app|project)(?: (?:in|with|using) (?:the |a )?browser)?(?: please)?', simple):
        # "Check my project" alone is a code review, not necessarily a browser request.
        if 'website' in simple or 'web app' in simple or 'webapp' in simple or 'browser' in simple:
            return {}
    if simple in ('run browser tests', 'run the browser tests', 'test website', 'browser test'):
        return {}
    return None


def summary(result, artifacts=True):
    status = result['status']
    if status == 'skipped': return 'Browser checks not run: ' + result['reason']
    lines = ['Browser checks ' + status + '.']
    if result.get('url'): lines.append('Tested: ' + result['url'])
    if result.get('pages'):
        lines.append(f"Checked {len(result['pages'])} page(s) at desktop and mobile sizes; {len(result.get('steps', []))} saved interaction step(s).")
    for key in ('error',):
        if result.get(key): lines.append(str(result[key])[:1500])
    problems = result.get('console_errors', []) + result.get('page_errors', []) + result.get('http_errors', []) + result.get('layout_errors', [])
    if problems: lines.extend('- ' + str(p)[:400] for p in problems[:6])
    if artifacts and result.get('directory'): lines.append('Screenshots, trace and report: ' + result['directory'])
    if result.get('started'): lines.append('The temporary server was stopped after testing.')
    lines.append('Automatic coverage checks loading, local navigation and layout. Saved steps cover specific user interactions.')
    return '\n\n'.join(lines)


def after_edits(project, config, memory_project=None, preview=False):
    from browser_project import candidates
    config = validate(config)
    if not config['automatic']: return None
    if preview and config['url']:
        return {'status': 'skipped', 'reason': 'A configured URL could point at the original project. Clear it to test an independently runnable preview copy.'}
    if not config['url'] and not candidates(project): return None
    return run(project, config, memory_project=memory_project)


def chat_task(project, request, conversation=None, resume=False):
    """A normal saved chat turn and queue job, with no provider or model invocation."""
    from context_engine import ProjectMemory
    from project_checks import profile
    if hub.is_general(project):
        raise ValueError('Choose a project chat before testing a website.')
    identifier = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    folder = hub.private_dir(hub.STATE / 'tasks' / identifier)
    with hub.project_lock(project):
        memory = ProjectMemory(project)
        conversation = memory.conversation(conversation, request, resume)
        config = dict(profile(project)['browser'], **(chat_request(request) or {}))
        print('Running browser checks · no model tokens used', flush=True)
        result = run(project, config)
        reply = summary(result, artifacts=hub.sys.stdout.isatty())
        status = 'completed' if result['status'] == 'passed' else 'canceled' if result['status'] == 'canceled' else 'needs_review'
        hub.save_json(folder / 'task.json', {'id': identifier, 'project': str(memory.project), 'conversation': conversation,
            'request': request, 'status': status, 'apply': False, 'attempts': [], 'browser': result})
        memory.record(conversation, identifier, request, reply, status)
        hub.terminal_reply(reply) if hub.sys.stdout.isatty() else print(reply, flush=True)
        return 0 if status == 'completed' else 130 if status == 'canceled' else 3


def run(project, config=None, memory_project=None):
    from project_checks import profile
    from context_engine import ProjectMemory
    memory = ProjectMemory(memory_project or project)
    config = validate(config if config is not None else profile(memory.project)['browser'])
    identifier = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    folder = hub.private_dir(memory.directory / 'browser-runs' / identifier)
    code, plan = 1, {}
    try:
        if not hub.executable('node'):
            raise ValueError('Install Node.js and the optional browser test dependencies described in README.')
        from browser_project import application
        with application(project, config, folder) as plan:
            hub.save_json(folder / 'config.json', dict(config, url=plan['url']))
            print('[Browser] Checking ' + plan['url'] + ' at desktop and mobile sizes…', flush=True)
            code, output, _ = hub.run_process([hub.executable('node'), str(hub.ROOT / 'tools/browser_check.mjs'), str(folder)],
                project, dict(os.environ), folder / 'runner.log', timeout=150, render_reply=False, quiet=True)
            try: result = json.loads((folder / 'result.json').read_text())
            except (OSError, ValueError):
                result = {'status': 'failed', 'error': output[-3000:] or 'Browser runner did not finish. Install optional Playwright dependencies.', 'steps': []}
            if code in (130, 143, -2, -15): result['status'] = 'canceled'
    except KeyboardInterrupt:
        code, result = 130, {'status': 'canceled', 'error': 'Browser testing canceled.'}
    except (OSError, ValueError) as error:
        result = {'status': 'failed', 'error': str(error), 'steps': []}
    result.update({key: plan[key] for key in ('url', 'kind', 'started') if key in plan})
    result.update(id=identifier, project=str(memory.project), exit_code=code, directory=str(folder))
    if (folder / 'server.log').is_file(): result['server_log'] = 'server.log'
    hub.save_json(folder / 'result.json', result)
    hub.save_json(memory.directory / 'browser-latest.json', result)
    print('[Browser] ' + result['status'] + '.', flush=True)
    return result
