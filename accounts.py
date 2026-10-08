"""Bounded, in-memory native sign-in sessions; credentials stay with provider CLIs."""
from __future__ import annotations
import codecs
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import hub

PROVIDERS = {'antigravity': ('Antigravity', 'agy'), 'claude': ('Claude', 'claude'), 'openai': ('ChatGPT', 'opencode')}
KEYS = {'up': '\x1b[A', 'down': '\x1b[B', 'enter': '\r', 'escape': '\x1b', 'tab': '\t', 'backspace': '\x7f'}
AUTH_HOSTS = {'accounts.google.com', 'antigravity.google', 'antigravity.google.com',
              'auth.openai.com', 'chatgpt.com', 'claude.ai', 'platform.claude.com', 'console.anthropic.com'}


def login_command(provider, reconnect=False):
    if provider not in PROVIDERS:
        raise ValueError('Choose Antigravity, Claude or ChatGPT.')
    binary = hub.executable(PROVIDERS[provider][1])
    if not binary:
        raise ValueError(f'Install {PROVIDERS[provider][1]} before connecting this account.')
    if provider == 'antigravity':
        return [binary, '-i', '/logout' if reconnect else '/usage']
    if reconnect:
        raise ValueError('Use the normal sign-in button to reconnect this provider.')
    if provider == 'claude':
        return [binary, 'auth', 'login', '--claudeai']
    return [binary, '--pure', 'auth', 'login', '--provider', 'openai']


def environment(provider):
    env = hub.clean_environment('antigravity' if provider == 'antigravity' else 'claude')
    for key in tuple(env):
        if key.startswith('OPENCODE_'):
            env.pop(key)
    env.update(TERM='xterm-256color', COLUMNS='100', LINES='28', NO_COLOR='1')
    return env


def available():
    # Report presence without reading token files or disclosing account identifiers.
    return [{'id': key, 'name': title, 'installed': bool(hub.executable(binary))}
            for key, (title, binary) in PROVIDERS.items()]


def connection_status():
    result = {}
    binary = hub.executable('claude')
    if binary:
        try:
            data = json.loads(subprocess.run([binary, 'auth', 'status'], env=environment('claude'),
                capture_output=True, text=True, timeout=6).stdout)
            result['claude'] = 'saved' if data.get('loggedIn') and data.get('authMethod') in ('claude.ai', 'oauth_token') else 'sign_in'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result['claude'] = 'unknown'
    binary = hub.executable('opencode')
    if binary:
        try:
            data = subprocess.run([binary, '--pure', 'auth', 'list'], env=environment('openai'),
                                  capture_output=True, text=True, timeout=6)
            text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', data.stdout + data.stderr)
            # CLI lists credential types, never access tokens. No raw output is exposed.
            result['openai'] = 'saved' if re.search(r'\bOpenAI\b.*\boauth\b', text, re.I) else 'sign_in'
        except (OSError, subprocess.TimeoutExpired):
            result['openai'] = 'unknown'
    return result


def safe_link(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and parsed.hostname in AUTH_HOSTS and not parsed.username and not parsed.password
    except ValueError:
        return False


class SignInSession:
    def __init__(self, provider, reconnect=False):
        login_command(provider, reconnect)  # Validate before allocating any resources.
        if os.name == 'nt':
            raise ValueError('Embedded sign-in currently requires Linux or macOS.')
        # Bundled, unmodified renderer; no installation or network loading at runtime.
        sys.path.insert(0, str(hub.ROOT / 'vendor'))
        import pyte
        import fcntl
        import pty
        import struct
        import termios
        self.id, self.provider = uuid.uuid4().hex, provider
        self.lock = threading.RLock()
        self.screen = pyte.Screen(100, 28)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.links = []
        self.raw_tail = ''
        self.created = self.seen = time.monotonic()
        self.closed = False
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 28, 100, 0, 0))
        folder = hub.private_dir(hub.STATE / 'account-sign-in')
        try:
            self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', provider,
                'reconnect' if reconnect else 'login'], stdin=slave, stdout=slave, stderr=slave,
                cwd=folder, env=environment(provider), start_new_session=True)
        except Exception:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            while not self.closed:
                if time.monotonic() - self.created > 600 or time.monotonic() - self.seen > 300:
                    self.close()
                    break
                ready, _, _ = select.select([self.master], [], [], .3)
                if ready:
                    data = os.read(self.master, 8192)
                    if not data:
                        break
                    text = self.decoder.decode(data)
                    with self.lock:
                        self.stream.feed(text)
                        self.raw_tail = (self.raw_tail + text)[-24000:]
                        # Strip styles before finding links; preserve URLs across reads.
                        clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.raw_tail)
                        for url in re.findall(r'https://[^\s\x00-\x20\x7f\x1b<>"\\]+', clean):
                            url = url.rstrip(').,;')
                            if safe_link(url) and url not in self.links:
                                self.links = (self.links + [url])[-6:]
                        # Answer cursor-position queries used by native terminal UIs.
                        if '\x1b[6n' in text:
                            os.write(self.master, f'\x1b[{self.screen.cursor.y+1};{self.screen.cursor.x+1}R'.encode())
                if self.process.poll() is not None and not ready:
                    break
        except (OSError, ValueError):
            pass
        finally:
            if self.process.poll() is None:
                self.close()

    def snapshot(self):
        with self.lock:
            self.seen = time.monotonic()
            code = self.process.poll()
            return {'id': self.id, 'provider': self.provider, 'screen': '\n'.join(self.screen.display).rstrip(),
                    'links': list(self.links), 'running': code is None and not self.closed, 'exit_code': code}

    def send(self, data):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise ValueError('This sign-in session has ended. Start a new one.')
            key = data.get('key')
            if key is not None:
                if key not in KEYS:
                    raise ValueError('Unsupported sign-in control.')
                value = KEYS[key]
            else:
                value = data.get('text')
                if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 32 for c in value):
                    raise ValueError('Enter a single line of at most 8,192 characters.')
                value += '\r'
            os.write(self.master, value.encode())
            self.seen = time.monotonic()
        return {'ok': True}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=2)
                except ProcessLookupError:
                    pass
            os.close(self.master)
            self.raw_tail = ''
            self.links = []
            self.screen.reset()


class AccountManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.session = None

    def start(self, provider, reconnect=False):
        with self.lock:
            if self.session and self.session.snapshot()['running']:
                raise RuntimeError('Finish or close the current sign-in session first.')
            if self.session:
                self.session.close()
            self.session = SignInSession(provider, reconnect)
            return self.session.snapshot()

    def get(self, identifier):
        with self.lock:
            if not self.session or self.session.id != identifier:
                raise ValueError('Sign-in session expired. Start again from Accounts.')
            return self.session

    def close(self):
        with self.lock:
            if self.session:
                self.session.close()
                self.session = None


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--child':
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    args = login_command(sys.argv[2], sys.argv[3] == 'reconnect')
    os.execvpe(args[0], args, environment(sys.argv[2]))


def model_catalog():
    models = {'claude': [{'id': 'sonnet', 'name': 'Claude Sonnet'}, {'id': 'opus', 'name': 'Claude Opus'}]}
    binary = hub.executable('agy')
    if binary:
        try:
            data = subprocess.run([binary, 'models'], env=environment('antigravity'), capture_output=True,
                                  text=True, timeout=12)
            models['antigravity'] = [{'id': line.split('\t')[0], 'name': line.split('\t')[-1]}
                for line in data.stdout.splitlines() if re.match(r'^[a-z0-9][a-z0-9._-]+\t', line)]
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        catalog = hub.get_json('https://models.dev/api.json', 10).get('openai', {}).get('models', {})
        choices = [{'id': key, 'name': value.get('name', key)} for key, value in catalog.items()
                   if value.get('tool_call') and key.startswith('gpt-') and not value.get('deprecated')]
        models['openai'] = sorted(choices, key=lambda m: m['id'], reverse=True)[:80]
    except Exception:
        pass
    return models


class AccessCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.updating = False
        self.updated = 0
        self.catalog_updated = 0
        self.value = {'accounts': available(), 'account_status': {}, 'provider_models': {}}

    def get(self, force=False):
        with self.lock:
            if not self.updating and (force or time.monotonic() - self.updated > 30):
                self.updating = True
                threading.Thread(target=self.refresh, daemon=True).start()
            return dict(self.value)

    def refresh(self):
        try:
            value = dict(self.value, accounts=available(), account_status=connection_status())
            if time.monotonic() - self.catalog_updated > 300:
                value['provider_models'] = model_catalog()
                self.catalog_updated = time.monotonic()
            with self.lock:
                self.value, self.updated = value, time.monotonic()
        finally:
            with self.lock:
                self.updating = False
