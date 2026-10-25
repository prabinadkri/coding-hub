"""A dependency-free, loopback-only desktop dashboard for Coding Hub."""
from __future__ import annotations

import argparse
import concurrent.futures
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request
import uuid
import webbrowser

import hub
import quota
import free_quota
import accounts
from context_engine import ProjectMemory, project_tree, delete_project

VERSION = "2.13.0"
ACTIVE = {"queued", "running", "stopping"}
ASSETS = Path(__file__).resolve().parent / "assets"


def task_progress(task, output=''):
    state = task.get('status')
    if state == 'queued': return {'label':'Queued', 'detail':'Waiting for an available task slot; manage order in Task queue'}
    if state == 'stopping': return {'label':'Stopping…', 'detail':'Waiting for the agent to stop safely'}
    if state not in ACTIVE: return {'label':str(state or 'Ready').replace('_',' ').capitalize(), 'detail':'Task ended'}
    label, detail = 'Thinking / waiting for model…', 'Waiting for the next agent update'
    from browser_checks import chat_request
    try:
        if chat_request(task.get('prompt', '')) is not None:
            label, detail = 'Testing website…', 'Discovering the local web app; no model tokens used'
    except ValueError:
        pass
    integrating = False
    names = {'read':'Read file', 'glob':'Find files', 'grep':'Search source', 'edit':'Edit file', 'write':'Write file', 'bash':'Run command'}
    for line in output.splitlines():
        if line.startswith('[Smart 1]'):
            label, detail = 'Planning…', 'The selected manager is preparing the worker plan'
        elif line.startswith('[Smart review]'):
            label, detail = 'Reviewing changes…', 'The selected manager is checking the worker’s evidence'
        elif line.startswith('[Smart parallel]') or re.match(r'^\[Smart worker \d+/\d+\]', line):
            label, detail = 'Workers running in parallel…', line.split(']', 1)[-1].strip()[:150]
        elif line.startswith('[Smart integration]'):
            integrating = True
            label, detail = 'Integrating and checking…', line.split(']', 1)[-1].strip()[:150]
        elif line.startswith('[Smart worker]') or re.match(r'^\[\d+\]',line):
            label, detail = 'Integrating and checking…' if integrating else 'Thinking / waiting for model…', line.split(']',1)[-1].strip()[:120]
        elif line.startswith('[Browser]'):
            label, detail = 'Testing website…', line.removeprefix('[Browser]').strip()[:180]
        elif line.startswith('[Verification') or line.startswith('[Local queue]'):
            label, detail = 'Checking results…' if line.startswith('[Verification') else 'Waiting for local model…', line[:180]
        elif line.startswith('[tool] '):
            match = re.match(r'\[tool\] ([\w-]+) · (\w+)', line)
            if match:
                tool, status = match.groups()
                action = names.get(tool, tool.replace('_',' ').capitalize())
                label = action + '…' if status in ('running','pending') else 'Thinking / waiting for model…'
                detail = ('Current action: ' if status in ('running','pending') else 'Last action: ') + action + ' · ' + status
    return {'label':label,'detail':detail}


class TaskManager:
    def __init__(self, directory=None, command_factory=None):
        self.directory = hub.private_dir(directory or hub.STATE / "dashboard" / "tasks")
        self.lock = threading.RLock()
        from task_history import HistoryIndex
        self.history_index = HistoryIndex(self.directory)
        self.tasks = {}
        self.processes = {}
        self.command_factory = command_factory or self.command
        self.dispatched = None
        self.paused = False
        self.pause_preference = False
        self.closing = False
        self.pending_path = self.directory.with_name(self.directory.name + '-queue.json')
        paths = list(self.directory.glob('*.json'))
        try:
            control = json.loads(self.pending_path.read_text())
            pending = {name for name in control['pending'] if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',name)}
            self.paused = self.pause_preference = control.get('paused') is True
            # History size must never evict waiting jobs from the startup cache.
            paths = list(set(sorted(paths, key=lambda p:p.stat().st_mtime, reverse=True)[:100]) |
                         {self.directory / (name + '.json') for name in pending})
        except (OSError, ValueError, TypeError, KeyError):
            pass  # One-time migration discovers pending jobs in older stores.
        for path in paths:
            try:
                if path.is_symlink() or path.stat().st_size > 256*1024:
                    continue
                item = json.loads(path.read_text())
                if not isinstance(item,dict) or item.get('id') != path.stem or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',path.stem):
                    continue
                if item["status"] in ("running", "stopping"):
                    item.update(status="interrupted", ended_at=time.time())
                    hub.save_json(path, item)
                self.tasks[item["id"]] = item
                if item["status"] == "queued": self.paused = True
            except (OSError, ValueError, KeyError, TypeError):
                continue
        recent = sorted(self.tasks.values(), key=lambda t:t.get('created_at',0), reverse=True)[:100]
        keep = {t['id'] for t in recent} | {t['id'] for t in self.tasks.values() if t['status'] in ACTIVE}
        self.tasks = {key:task for key,task in self.tasks.items() if key in keep}
        self.persist_queue()

    @staticmethod
    def command(task):
        args = [sys.executable, "-u", str(hub.ROOT / "hub.py"), "run", "--project", task["project"],
                "--backend", task["backend"], "--quality", task["quality"]]
        if task.get('scope') == 'general':
            args[args.index('--project'):args.index('--project')+2] = ['--general']
        if task["mode"] in ("build", "preview"):
            args.append("--preview" if task["mode"] == "preview" else "--apply")
        if task.get("conversation"):
            args.extend(["--conversation", task["conversation"]])
        if task.get('model'):
            args.extend(['--model', task['model']])
        if task.get('backend') in ('smart', 'smart-free'):
            args.extend(['--workers', str(task.get('workers', 1))])
        return args + ["--", task["prompt"]]

    def persist(self, task):
        hub.save_json(self.directory / (task["id"] + ".json"), task)
        self.persist_queue()

    def persist_queue(self):
        hub.save_json(self.pending_path, {'paused':self.pause_preference,
            'pending':[task['id'] for task in self.tasks.values() if task['status'] in ACTIVE]})

    def list(self):
        with self.lock:
            # CLI chat deletion also removes the corresponding saved task records.
            self.tasks = {key: task for key, task in self.tasks.items()
                          if task['status'] in ACTIVE or (self.directory / (key + '.json')).is_file()}
            return [dict(t) for t in sorted(self.tasks.values(), key=lambda t: t["created_at"], reverse=True)[:50]]

    def history(self, **filters):
        with self.lock:
            return self.history_index.page(**filters)

    def delete_conversation(self, data):
        project = data.get('project')
        if not isinstance(project, str) or not project.strip():
            raise ValueError('Choose the chat’s project folder.')
        with self.lock:
            result = ProjectMemory(project).delete_conversation(data.get('id'), self.directory)
            for identifier in result['task_ids']:
                self.tasks.pop(identifier, None)
            return result

    def active(self):
        with self.lock:
            return next((dict(t) for t in self.tasks.values() if t['status'] in ('running', 'stopping')),
                        next((dict(t) for t in self.tasks.values() if t['status'] == 'queued'), None))

    def delete_project(self, data):
        with self.lock:
            result = delete_project(data.get('project'), self.directory)
            self.tasks = {key: task for key, task in self.tasks.items() if task.get('project') != result['deleted']}
            return result

    def start(self, data):
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        prompt, project = data.get("prompt"), data.get("project")
        scope = data.get('scope', 'project')
        if scope not in ('project','general'): raise ValueError('Choose a general or project task.')
        if scope == 'general': project = str(hub.general_workspace())
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise ValueError("Enter a task of 1–12,000 characters.")
        if not isinstance(project, str) or not project.strip() or len(project) > 4096:
            raise ValueError("Choose a project folder.")
        project = Path(project).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("The project folder does not exist.")
        backend, quality, mode = data.get("backend", hub.DEFAULT_ROUTE), data.get("quality", "fast"), data.get("mode", "analysis")
        if backend not in hub.ROUTES or quality not in ("fast", "deep") or mode not in ("analysis", "build", "preview"):
            raise ValueError("Choose a valid route, quality and task mode.")
        from browser_checks import chat_request
        browser_test = chat_request(prompt) is not None
        if browser_test and hub.is_general(project):
            raise ValueError('Choose a project chat before testing a website.')
        model = None if browser_test else hub.validate_model(backend, data.get('model'))
        workers = 1 if browser_test else hub.validate_workers(backend, data.get('workers', 1))
        if browser_test: mode = 'analysis'
        if backend in ("smart", "smart-free") and len(prompt.encode()) > 6000:
            raise ValueError("Smart requests are limited to 6,000 UTF-8 bytes. Split the task or choose a direct route.")
        if mode == 'preview' and hub.is_general(project):
            raise ValueError('Preview mode requires a project folder.')
        with self.lock:
            if self.closing: raise RuntimeError('The server is shutting down.')
            if sum(t['status'] == 'queued' for t in self.tasks.values()) >= 100:
                raise ValueError('Queue limit reached. Finish or cancel waiting tasks first.')
            memory = ProjectMemory(project)
            conversation = memory.conversation(data.get("conversation"), prompt.strip())
            task = {"id": uuid.uuid4().hex, "prompt": prompt.strip(), "project": str(project), "scope": "general" if hub.is_general(project) else "project",
                    "conversation": conversation,
                    "backend": backend, "quality": quality, "mode": mode, "created_at": time.time(),
                    "model": model, "workers": workers,
                    "started_at": None, "ended_at": None, "status": "queued", "exit_code": None,
                    "queue_order": max((t.get("queue_order", 0) for t in self.tasks.values()), default=0) + 1}
            self.tasks[task["id"]] = task
            self.persist(task)
            self._schedule()
            return dict(task)

    def queue(self):
        with self.lock:
            return {'paused': self.paused, 'tasks': [dict(t) for t in sorted(self.tasks.values(), key=lambda t: t.get('queue_order', t['created_at'])) if t['status'] in ACTIVE]}

    def control_queue(self, data):
        with self.lock:
            action = data.get('action')
            if action in ('pause', 'resume'):
                self.paused = self.pause_preference = action == 'pause'
            elif action == 'move':
                waiting = sorted((t for t in self.tasks.values() if t['status'] == 'queued' and t['id'] != self.dispatched), key=lambda t: t.get('queue_order', t['created_at']))
                ids = [t['id'] for t in waiting]
                if data.get('id') not in ids or data.get('direction') not in ('up', 'down'):
                    raise ValueError('Choose a waiting task and up/down.')
                i = ids.index(data['id']); j = i + (-1 if data['direction'] == 'up' else 1)
                if 0 <= j < len(waiting):
                    a, b = waiting[i], waiting[j]
                    a['queue_order'], b['queue_order'] = b.get('queue_order', b['created_at']), a.get('queue_order', a['created_at'])
                    self.persist(a); self.persist(b)
            else:
                raise ValueError('Queue actions: pause, resume, move.')
            self.persist_queue()
            self._schedule()
            return self.queue()

    def _schedule(self):
        if self.paused or self.closing or self.dispatched is not None:
            return
        waiting = sorted((t for t in self.tasks.values() if t['status'] == 'queued'), key=lambda t: t.get('queue_order', t['created_at']))
        if waiting:
            self.dispatched = waiting[0]['id']
            threading.Thread(target=self._run, args=(waiting[0],), daemon=True).start()

    def _run(self, task):
        process = None
        path = self.directory / (task["id"] + ".log")
        try:
            with self.lock:
                if task["status"] in ("stopping", "canceled"):
                    task.update(status="canceled", ended_at=time.time())
                    self.persist(task)
                    return
                env = dict(os.environ, PYTHONUNBUFFERED="1", CODING_HUB_STATE=str(hub.STATE))
                process = subprocess.Popen(self.command_factory(task), cwd=task["project"], env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", start_new_session=(os.name != "nt"))
                self.processes[task["id"]] = process
                task.update(status="running", started_at=time.time())
                self.persist(task)
            with path.open("w") as log:
                path.chmod(0o600)
                for line in process.stdout:
                    log.write(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line))
                    log.flush()
            code = process.wait()
            with self.lock:
                task["status"] = "canceled" if task["status"] == "stopping" or code in (130, 143, -2, -15) else "completed" if code == 0 else "needs_review" if code == 3 else "failed"
                task["exit_code"] = code
        except Exception as error:
            with path.open("a") as log:
                path.chmod(0o600)
                log.write(f"\nCould not run task: {error}\n")
            with self.lock:
                task["status"] = "failed"
        finally:
            if process and process.stdout:
                process.stdout.close()
            with self.lock:
                task["ended_at"] = time.time()
                self.processes.pop(task["id"], None)
                self.persist(task)
                self.dispatched = None
                self._schedule()

    def detail(self, identifier):
        with self.lock:
            self.list()
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier):
                raise KeyError('Task not found.')
            if identifier in self.tasks:
                task = dict(self.tasks[identifier])
            else:
                path = self.directory / (identifier + '.json')
                try:
                    if path.is_symlink() or path.stat().st_size > 256 * 1024:
                        raise ValueError('Invalid task record.')
                    task = json.loads(path.read_text())
                    if not isinstance(task, dict) or task.get('id') != identifier:
                        raise ValueError('Invalid task record.')
                except (OSError, ValueError):
                    raise KeyError('Task not found.') from None
        path = self.directory / (identifier + ".log")
        output = ""
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 160000))
                output = stream.read().decode("utf-8", errors="replace")
        return dict(task, output=output, progress=task_progress(task, output))

    def stop(self, identifier):
        with self.lock:
            task = self.tasks.get(identifier)
            if not task:
                raise KeyError("Task not found.")
            if task["status"] not in ACTIVE:
                return dict(task)
            if task['status'] == 'queued' and task['id'] != self.dispatched:
                task.update(status='canceled', ended_at=time.time())
                self.persist(task)
                return dict(task)
            task["status"] = "stopping"
            self.persist(task)
            process = self.processes.get(identifier)
            if process and process.poll() is None:
                process.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
            return dict(task)

    def close(self):
        for item in self.list():
            if item["status"] in ("running", "stopping"):
                self.stop(item["id"])


class PricingCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.updating = False
        self.updated = 0
        self.value = {'free_models': [], 'pricing_checked_at': None, 'pricing_error': False}

    def get(self):
        with self.lock:
            if not self.updating and time.monotonic() - self.updated > 300:
                self.updating = True
                threading.Thread(target=self.refresh, daemon=True).start()
            return dict(self.value)

    def refresh(self):
        try:
            models = hub.refresh_free_models()
            with self.lock:
                self.value = {'free_models': models, 'pricing_checked_at': time.time(), 'pricing_error': False}
        except Exception:
            with self.lock:
                self.value['pricing_error'] = True
        finally:
            with self.lock:
                self.updated, self.updating = time.monotonic(), False


class StatusCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.updating = False
        self.updated = 0
        self.cpu_sample = None
        self.access = accounts.AccessCache()
        self.prices = PricingCache()
        self.value = {"checking": True, "home": str(Path.home()), "default_project": str(Path.home() / "Documents"),
                      "version": VERSION, "programs": {}, "models": [], "loaded": [], "gpu": None,
                      "free_models": [], "pricing_checked_at": None, "ram": None, "cpu": None,
                      "sampled_at": None, "accounts": [], "account_status": {}}

    @staticmethod
    def gpu():
        binary = hub.executable("nvidia-smi")
        if not binary:
            return None
        try:
            result = subprocess.run([binary, "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode:
                return None
            values = [v.strip() for v in result.stdout.splitlines()[0].split(",")]
            return {"name": values[0], "total_mb": int(values[1]), "used_mb": int(values[2]), "utilization": int(values[3])}
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def ram():
        try:
            values = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()}
            return {"total_gb": round(values["MemTotal"] / 1048576, 1), "available_gb": round(values["MemAvailable"] / 1048576, 1)}
        except (OSError, ValueError, KeyError):
            return None

    def cpu(self):
        try:
            values = [int(n) for n in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            sample = sum(values), values[3] + values[4]
            previous, self.cpu_sample = self.cpu_sample, sample
            if previous and sample[0] > previous[0]:
                return round(100 * (1 - (sample[1] - previous[1]) / (sample[0] - previous[0])), 1)
        except (OSError, ValueError, IndexError):
            pass
        return None

    def get(self, force=False):
        with self.lock:
            if not self.updating and (force or time.monotonic() - self.updated > 3):
                self.updating = True
                threading.Thread(target=self.refresh, daemon=True).start()
            return dict(self.value, refreshing=self.updating)

    def refresh(self):
        def safe(fn, default):
            try:
                return fn()
            except Exception:
                return default
        try:
            value = dict(self.value)
            value["programs"] = {name: bool(hub.executable(name)) for name in ("agy", "opencode", "ollama")}
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                tags = pool.submit(safe, hub.local_models, [])
                loaded = pool.submit(safe, lambda: hub.get_json("http://127.0.0.1:11434/api/ps", 3).get("models", []), [])
                gpu = pool.submit(self.gpu)
                value.update(models=tags.result(), loaded=loaded.result(), gpu=gpu.result(), ram=self.ram(),
                             cpu=self.cpu(), sampled_at=time.time(), accounts=accounts.available())
            value.update(self.access.get())
            # Cloud catalog calls never hold up live local hardware sampling.
            # Executing a selected free model still verifies its price afresh.
            value.update(self.prices.get())
            import local_resource
            value['local_resource'] = local_resource.status()
            value.update(checking=False, local_ready=hub.LOCAL_AGENT_MODEL in value["models"], local_model=hub.LOCAL_AGENT_MODEL)
            with self.lock:
                self.value, self.updated = value, time.monotonic()
        finally:
            with self.lock:
                self.updating = False


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # Loopback is known; avoid HTTPServer's reverse DNS lookup, which can
        # block startup for tens of seconds on offline hosts and hosted macOS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def __init__(self, address, manager=None, status=None, token=None):
        self.manager = manager or TaskManager()
        self.status = status or StatusCache()
        self.quota = quota.QuotaCache()
        self.free_quota = free_quota.FreeQuotaCache()
        self.accounts = accounts.AccountManager()
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json"):
        if kind == "application/json":
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, api=False):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.reply(421, {"error": "Use the local dashboard address."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.reply(403, {"error": "Cross-origin requests are not accepted."})
            return False
        if api and not hmac.compare_digest(self.headers.get("X-CodeHub-Token", ""), self.server.token):
            self.reply(401, {"error": "Reopen Coding Hub from its app icon to reconnect."})
            return False
        return True

    def do_GET(self):
        parsed = urlsplit(self.path)
        if not self.allowed(parsed.path.startswith("/api/")):
            return
        try:
            query = parse_qs(parsed.query)
            if parsed.path in ('/api/files/preview', '/api/files/download'):
                import reply_files
                project, reference = query.get('project', [''])[0], query.get('path', [''])[0]
                task = query.get('task', [None])[0]
                if parsed.path.endswith('/download'):
                    return self.reply(200, reply_files.download(project, reference, task), 'application/octet-stream')
                return self.reply(200, reply_files.describe(project, reference, task, int(query.get('offset', ['0'])[0])))
            if parsed.path == '/api/queue':
                return self.reply(200, self.server.manager.queue())
            if parsed.path == '/api/metrics':
                import model_metrics
                return self.reply(200, model_metrics.snapshot())
            if parsed.path == '/api/checks':
                from project_checks import profile
                memory = ProjectMemory(query.get('project', [''])[0])
                value = profile(memory.project)
                latest = memory.directory / 'browser-latest.json'
                return self.reply(200, dict(value, browser_latest=json.loads(latest.read_text()) if latest.is_file() else None))
            if parsed.path == '/api/browser/report':
                memory = ProjectMemory(query.get('project', [''])[0])
                identifier = query.get('id', [''])[0]
                if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}', identifier):
                    raise ValueError('Choose a browser report.')
                return self.reply(200, json.loads((memory.directory / 'browser-runs' / identifier / 'result.json').read_text()))
            if parsed.path == '/api/browser/artifact':
                memory = ProjectMemory(query.get('project', [''])[0])
                identifier, name = query.get('id', [''])[0], query.get('name', [''])[0]
                if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}', identifier) or not (name in ('trace.zip', 'final.png', 'failure.png', 'result.json', 'server.log') or re.fullmatch(r'page-[1-3]-(desktop|mobile)\.png', name)):
                    raise ValueError('Choose a browser test artifact.')
                path = memory.directory / 'browser-runs' / identifier / name
                return self.reply(200, path.read_bytes(), mimetypes.guess_type(name)[0] or 'application/octet-stream')
            if parsed.path == "/api/health":
                return self.reply(200, {"app": "coding-hub", "version": VERSION})
            if parsed.path == '/api/diagnostics':
                import system_diagnostics
                return self.reply(200, system_diagnostics.report())
            if parsed.path == "/api/status":
                return self.reply(200, dict(self.server.status.get(), quota=self.server.quota.get(), free_quota=self.server.free_quota.get()))
            if parsed.path == '/api/accounts/session':
                identifier = parse_qs(parsed.query).get('id', [''])[0]
                return self.reply(200, self.server.accounts.get(identifier).snapshot())
            if parsed.path == "/api/project":
                project = parse_qs(parsed.query).get("path", [""])[0]
                if not project:
                    raise ValueError("Choose a project folder first.")
                return self.reply(200, ProjectMemory(project).info())
            if parsed.path == '/api/history':
                query = parse_qs(parsed.query)
                return self.reply(200, self.server.manager.history(
                    query=query.get('q', [''])[0], status=query.get('status', [''])[0],
                    route=query.get('route', [''])[0], offset=int(query.get('offset', ['0'])[0])))
            if parsed.path == "/api/tasks":
                return self.reply(200, {"tasks": self.server.manager.list()})
            if parsed.path == "/api/projects":
                return self.reply(200, {"projects": project_tree()})
            if parsed.path == '/api/changes':
                from change_review import load
                query = parse_qs(parsed.query)
                project, task = query.get('project', [''])[0], query.get('task', [''])[0]
                if not project or not task:
                    raise ValueError('Choose a project and saved task.')
                return self.reply(200, load(project, task))
            if parsed.path == "/api/conversation":
                query = parse_qs(parsed.query)
                project, identifier = query.get("project", [""])[0], query.get("id", [""])[0]
                if not project or not identifier:
                    raise ValueError("Choose a saved conversation.")
                before = int(query.get("before", ["0"])[0]) or None
                return self.reply(200, ProjectMemory(project).messages(identifier, before=before))
            if parsed.path.startswith("/api/tasks/"):
                return self.reply(200, self.server.manager.detail(parsed.path.rsplit("/", 1)[1]))
            if parsed.path == "/api/folders":
                folder = Path(parse_qs(parsed.query).get("path", [str(Path.home())])[0]).expanduser().resolve()
                if not folder.is_dir():
                    raise ValueError("Folder not found.")
                children = sorted((p for p in folder.iterdir() if not p.name.startswith(".") and p.is_dir()), key=lambda p: p.name.lower())[:200]
                return self.reply(200, {"path": str(folder), "parent": str(folder.parent),
                                        "folders": [{"name": p.name, "path": str(p)} for p in children]})
            static = {"/": "index.html", "/app.js": "app.js", "/features.js": "features.js", "/style.css": "style.css", "/icon.svg": "icon.svg"}
            if parsed.path in static:
                path = ASSETS / static[parsed.path]
                return self.reply(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.reply(404, {"error": "Not found."})
        except KeyError:
            self.reply(404, {"error": "Task not found."})
        except (ValueError, OSError) as error:
            self.reply(400, {"error": str(error)})

    def do_POST(self):
        if not self.allowed(True):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                return self.reply(413, {"error": "Request must be 1–65,536 bytes."})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.reply(415, {"error": "Use application/json."})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            if self.path == '/api/files/reveal':
                from reply_files import reveal
                return self.reply(200, reveal(data.get('project', ''), data.get('path'), data.get('task')))
            if self.path == '/api/queue':
                return self.reply(200, self.server.manager.control_queue(data))
            if self.path == '/api/checks':
                from project_checks import profile
                project = Path(data.get('project', '')).expanduser().resolve()
                with hub.project_lock(project):
                    return self.reply(200, profile(project, data.get('config')))
            if self.path == '/api/verify':
                from project_checks import profile, run
                project = Path(data.get('project', '')).expanduser().resolve()
                with hub.project_lock(project):
                    return self.reply(200, run(project, profile(project), hub.private_dir(hub.STATE / 'manual-checks' / uuid.uuid4().hex)))
            if self.path == '/api/browser-test':
                from browser_checks import run
                project = Path(data.get('project', '')).expanduser().resolve()
                with hub.project_lock(project):
                    return self.reply(200, run(project))
            if self.path == '/api/changes/action':
                from workspace_changes import change
                return self.reply(200, change(data.get('project', ''), data.get('task'), data.get('action'), data.get('files')))
            if self.path == "/api/tasks":
                if self.server.accounts.session and self.server.accounts.session.snapshot()['running']:
                    raise RuntimeError('Finish or close account sign-in before sending a task.')
                return self.reply(201, self.server.manager.start(data))
            if self.path == '/api/accounts/start':
                if self.server.manager.active():
                    raise RuntimeError('Wait for the running task before changing account access.')
                return self.reply(201, self.server.accounts.start(data.get('provider'), data.get('reconnect') is True))
            if self.path == '/api/accounts/input':
                return self.reply(200, self.server.accounts.get(data.get('id')).send(data))
            if self.path == '/api/accounts/close':
                self.server.accounts.get(data.get('id')).close()
                self.server.quota.get(True)
                self.server.status.updated = 0
                self.server.status.access.get(True)
                self.server.status.get(True)
                return self.reply(200, {'ok': True})
            if self.path == "/api/stop":
                return self.reply(200, self.server.manager.stop(data.get("id", "")))
            if self.path == '/api/conversation/delete':
                return self.reply(200, self.server.manager.delete_conversation(data))
            if self.path == '/api/project/delete':
                return self.reply(200, self.server.manager.delete_project(data))
            if self.path == "/api/refresh":
                return self.reply(200, self.server.status.get(True))
            if self.path == "/api/quota/refresh":
                return self.reply(200, self.server.quota.get(True))
            if self.path == "/api/free-quota/refresh":
                return self.reply(200, self.server.free_quota.get(True))
            if self.path == "/api/project/notes":
                project = data.get("project")
                if not isinstance(project, str) or not project.strip():
                    raise ValueError("Choose a project folder first.")
                memory = ProjectMemory(project)
                with hub.project_lock(memory.project):
                    memory.remember(data.get("requirements"))
                return self.reply(200, memory.info())
            if self.path == "/api/project/init":
                project = data.get("project")
                if not isinstance(project, str) or not project.strip():
                    raise ValueError("Choose a project folder first.")
                memory = ProjectMemory(project)
                with hub.project_lock(memory.project):
                    result = memory.initialize_rules()
                return self.reply(200, result)
            if self.path == "/api/unload":
                import local_resource
                if local_resource.status().get("busy"):
                    raise RuntimeError("The shared local model is in use. Wait for its current request to finish.")
                if self.server.manager.active():
                    raise RuntimeError("Wait for the active task to finish before releasing memory.")
                model = data.get('model') or hub.LOCAL_AGENT_MODEL
                if not isinstance(model, str) or model not in hub.local_models():
                    raise ValueError('Choose an installed local model to release.')
                request = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                    data=json.dumps({"model": model, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    response.read()
                self.server.status.get(True)
                return self.reply(200, {"ok": True})
            self.reply(404, {"error": "Not found."})
        except RuntimeError as error:
            self.reply(409, {"error": str(error)})
        except KeyError:
            self.reply(404, {"error": "Task not found."})
        except (ValueError, OSError, TypeError) as error:
            self.reply(400, {"error": str(error)})


def session_url():
    """Return an authenticated local URL only after checking the running server."""
    try:
        existing = json.loads((hub.STATE / "dashboard" / "server.json").read_text())
        port, token = int(existing["port"]), existing["token"]
        if not 0 < port < 65536 or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
            return None
        address = f"http://127.0.0.1:{port}"
        request = urllib.request.Request(address + "/api/health", headers={"X-CodeHub-Token": token})
        with urllib.request.urlopen(request, timeout=2) as response:
            if json.load(response).get("app") == "coding-hub":
                return address + "/#" + token
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def serve(port=8765, open_browser=True):
    descriptor = hub.STATE / "dashboard" / "server.json"
    # Repeated launches reuse the server and its private session.
    existing = session_url()
    if existing:
        if open_browser:
            webbrowser.open(existing)
        print("Dashboard is already running at " + existing.split("/#")[0], flush=True)
        return 0
    try:
        server = DashboardServer(("127.0.0.1", port))
    except OSError:
        if not port:
            raise
        server = DashboardServer(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{server.server_port}"
    hub.save_json(descriptor, {"pid": os.getpid(), "port": server.server_port, "token": server.token})
    server.status.get()
    print("Coding Hub dashboard: " + address, flush=True)
    print("Open the app icon to connect securely. Press Ctrl+C to stop the server.", flush=True)
    if open_browser:
        webbrowser.open(address + "/#" + server.token)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.manager.close()
        server.accounts.close()
        server.server_close()
        try:
            if json.loads(descriptor.read_text()).get("pid") == os.getpid():
                descriptor.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    sys.exit(serve(args.port, not args.no_browser))
