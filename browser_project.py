"""Bounded web-project discovery and temporary, loopback-only development servers."""
from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
import hub


def candidates(project):
    """Inspect common web roots, not dependencies, private files or an entire tree."""
    root = Path(project).resolve()
    roots = [root]
    for name in ('frontend', 'web', 'client', 'app', 'website', 'site', 'apps', 'packages'):
        child = root / name
        if child.is_dir() and not child.is_symlink():
            roots.append(child)
            if name in ('apps', 'packages'):
                roots.extend(p for p in sorted(child.iterdir())[:30] if p.is_dir() and not p.is_symlink() and not p.name.startswith('.'))
    found = []
    for folder in roots:
        manifest = folder / 'package.json'
        if manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size < 256 * 1024:
            try:
                package = json.loads(manifest.read_text())
                scripts = package.get('scripts', {})
                for name in ('dev', 'start', 'serve'):
                    script = scripts.get(name, '')
                    tokens = shlex.split(script)
                    # Do not guess arguments for shell pipelines, wrappers or production servers.
                    if not tokens or any(c in script for c in ('&&', '||', ';', '|', '`', '$(', '\n', '>', '<')):
                        continue
                    first = tokens[0]
                    framework = first if first in ('vite', 'next', 'astro', 'react-scripts') else None
                    if framework == 'vite' and len(tokens) > 1 and tokens[1] in ('build', 'preview'): continue
                    if framework in ('next', 'astro') and tokens[1:2] != ['dev']: continue
                    if framework == 'react-scripts' and tokens[1:2] != ['start']: continue
                    if framework:
                        found.append({'kind': framework, 'root': str(folder), 'project_root': str(root), 'script': name, 'tokens': tokens})
                        break
                else:
                    continue  # An index.html in an unknown JS app is not necessarily standalone HTML.
                continue
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        if (folder / 'index.html').is_file() and not (folder / 'index.html').is_symlink():
            found.append({'kind': 'static', 'root': str(folder)})
    return found


def discover(project, config):
    if config.get('url'):
        return {'kind': 'url', 'root': str(Path(project).resolve()), 'url': config['url']}
    if config.get('command'):
        raise ValueError('Set a localhost URL alongside the custom start command in Project checks.')
    found = candidates(project)
    if len(found) > 1:
        raise ValueError('Multiple web apps found. Select the web app folder or set its URL in Project checks: ' + ', '.join(p['root'] for p in found))
    if not found:
        raise ValueError('No supported web app found. Choose its folder, or set a localhost URL and optional start command in Project checks. Automatic startup supports Vite, Next.js, Astro, Create React App and static index.html sites.')
    return found[0]


def available(url):
    # Ignore proxy environment settings for a local application.
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=1) as response:
            return response.status < 500
    except HTTPError:
        return True  # Let the browser report the actual HTTP failure.
    except (URLError, OSError, ValueError):
        return False


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def launch_command(plan, port):
    root = Path(plan['root'])
    if plan['kind'] == 'static':
        return [sys.executable, str(hub.ROOT / 'tools/static_preview.py'), str(root), str(port)], {}
    binary = root / 'node_modules' / '.bin' / plan['kind']
    # A workspace may hoist dependencies. Only search ancestors inside the selected project.
    boundary = Path(plan.get('project_root', root))
    parent = root
    while not binary.is_file() and parent != boundary and parent.parent.is_relative_to(boundary):
        parent = parent.parent
        binary = parent / 'node_modules' / '.bin' / plan['kind']
    if not binary.is_file():
        raise ValueError('Web dependencies are missing in ' + str(root) + '. Install this project’s dependencies first; browser checks never install packages automatically. Preview copies do not share dependencies with the original project.')
    node = hub.executable('node')
    if not node: raise ValueError('Node.js is required to start this web app.')
    tokens = plan['tokens'][1:]
    # Replace conflicting host/port flags instead of depending on a CLI's duplicate flag behavior.
    args = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in ('--port', '-p', '--host', '--hostname', '-H'):
            if index < len(tokens) and not tokens[index].startswith('-'): index += 1
        elif not token.startswith(('--port=', '--host=', '--hostname=')):
            args.append(token)
    if plan['kind'] in ('vite', 'astro'):
        args += ['--host', '127.0.0.1', '--port', str(port)]
        if plan['kind'] == 'vite': args += ['--strictPort']
    elif plan['kind'] == 'next':
        args += ['--hostname', '127.0.0.1', '--port', str(port)]
    # Execute the installed shim: pnpm/Yarn shims can be shell scripts, not JavaScript.
    return [str(binary), *args], {'HOST': '127.0.0.1', 'PORT': str(port), 'BROWSER': 'none'}


def stop(process):
    if os.name == 'posix':
        with suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGTERM)
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: pass
        # A wrapper may have exited while a child still owns the socket.
        with suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill()
    process.wait()


@contextmanager
def application(project, config, folder):
    plan = discover(project, config)
    if plan['kind'] == 'url':
        if available(plan['url']):
            yield dict(plan, started=False)
            return
        if not config.get('command'):
            raise ValueError('The configured URL is not running. Clear the URL to discover and start a supported app automatically, or add its start command in Project checks.')
        command, extra = shlex.split(config['command']), {}
    else:
        port = free_port()
        plan['url'] = f'http://127.0.0.1:{port}/'
        command, extra = launch_command(plan, port)
    process = None
    print('[Browser] Starting local ' + plan['kind'] + ' app…', flush=True)
    with (folder / 'server.log').open('w') as log:
        (folder / 'server.log').chmod(0o600)
        try:
            process = subprocess.Popen(command, cwd=plan['root'], env=dict(os.environ, **extra),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=(os.name != 'nt'))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError('The web server exited during startup. See server.log in the browser report.')
                if available(plan['url']):
                    yield dict(plan, started=True)
                    return
                time.sleep(.25)
            raise ValueError('The web app did not become ready within 60 seconds. See server.log in the browser report.')
        finally:
            if process:
                stop(process)
                print('[Browser] Temporary web server stopped.', flush=True)
