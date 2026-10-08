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
from context_engine import ProjectMemory, project_tree

VERSION = "2.7.1"
ACTIVE = {"queued", "running", "stopping"}
ASSETS = Path(__file__).resolve().parent / "assets"


def task_progress(task, output=''):
    state = task.get('status')
    if state == 'queued': return {'label':'Preparing task…', 'detail':'Starting the agent in your project'}
    if state == 'stopping': return {'label':'Stopping…', 'detail':'Waiting for the agent to stop safely'}
    if state not in ACTIVE: return {'label':str(state or 'Ready').replace('_',' ').capitalize(), 'detail':'Task ended'}
    label, detail = 'Thinking / waiting for model…', 'Waiting for the next agent update'
    names = {'read':'Read file', 'glob':'Find files', 'grep':'Search source', 'edit':'Edit file', 'write':'Write file', 'bash':'Run command'}
    for line in output.splitlines():
        if line.startswith('[Smart 1]'):
            label, detail = 'Planning…', 'Antigravity is preparing the worker plan'
        elif line.startswith('[Smart review]'):
            label, detail = 'Reviewing changes…', 'Antigravity is checking the worker’s evidence'
        elif line.startswith('[Smart worker]') or re.match(r'^\[\d+\]',line):
            label, detail = 'Thinking / waiting for model…', line.split(']',1)[-1].strip()[:120]
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
        self.tasks = {}
        self.processes = {}
        self.command_factory = command_factory or self.command
        for path in sorted(self.directory.glob("*.json"), reverse=True)[:100]:
            try:
                item = json.loads(path.read_text())
                if item["status"] in ACTIVE:
                    item.update(status="interrupted", ended_at=time.time())
                    hub.save_json(path, item)
                self.tasks[item["id"]] = item
            except (OSError, ValueError, KeyError, TypeError):
                continue

    @staticmethod
    def command(task):
        args = [sys.executable, "-u", str(hub.ROOT / "hub.py"), "run", "--project", task["project"],
                "--backend", task["backend"], "--quality", task["quality"]]
        if task.get('scope') == 'general':
            args[args.index('--project'):args.index('--project')+2] = ['--general']
        if task["mode"] == "build":
            args.append("--apply")
        if task.get("conversation"):
            args.extend(["--conversation", task["conversation"]])
        if task.get('model'):
            args.extend(['--model', task['model']])
        return args + ["--", task["prompt"]]

    def persist(self, task):
        hub.save_json(self.directory / (task["id"] + ".json"), task)

    def list(self):
        with self.lock:
            # CLI chat deletion also removes the corresponding saved task records.
            self.tasks = {key: task for key, task in self.tasks.items()
                          if task['status'] in ACTIVE or (self.directory / (key + '.json')).is_file()}
            return [dict(t) for t in sorted(self.tasks.values(), key=lambda t: t["created_at"], reverse=True)[:50]]

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
        return next((t for t in self.list() if t["status"] in ACTIVE), None)

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
        backend, quality, mode = data.get("backend", "auto"), data.get("quality", "fast"), data.get("mode", "analysis")
        if backend not in ("auto", "smart", "antigravity", "free", "local", "claude", "openai") or quality not in ("fast", "deep") or mode not in ("analysis", "build"):
            raise ValueError("Choose a valid route, quality and task mode.")
        model = hub.validate_model(backend, data.get('model'))
        if backend == "smart" and len(prompt.encode()) > 6000:
            raise ValueError("Smart requests are limited to 6,000 UTF-8 bytes. Split the task or choose a direct route.")
        with self.lock, hub.project_lock(project):
            if self.active():
                raise RuntimeError("A task is already running. Stop it or wait for it to finish.")
            memory = ProjectMemory(project)
            conversation = memory.conversation(data.get("conversation"), prompt.strip())
            task = {"id": uuid.uuid4().hex, "prompt": prompt.strip(), "project": str(project), "scope": "general" if hub.is_general(project) else "project",
                    "conversation": conversation,
                    "backend": backend, "quality": quality, "mode": mode, "created_at": time.time(),
                    "model": model,
                    "started_at": None, "ended_at": None, "status": "queued", "exit_code": None}
            self.tasks[task["id"]] = task
            self.persist(task)
            threading.Thread(target=self._run, args=(task,), daemon=True).start()
            return dict(task)

    def _run(self, task):
        process = None
        path = self.directory / (task["id"] + ".log")
        try:
            with self.lock:
                if task["status"] == "stopping":
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
                task["status"] = "canceled" if task["status"] == "stopping" or code in (130, 143, -2, -15) else "completed" if code == 0 else "needs_review" if task["backend"] == "smart" and code == 3 else "failed"
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

    def detail(self, identifier):
        with self.lock:
            self.list()
            if identifier not in self.tasks:
                raise KeyError("Task not found.")
            task = dict(self.tasks[identifier])
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
            task["status"] = "stopping"
            self.persist(task)
            process = self.processes.get(identifier)
            if process and process.poll() is None:
                process.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
            return dict(task)

    def close(self):
        for item in self.list():
            if item["status"] in ACTIVE:
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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
            static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/icon.svg": "icon.svg"}
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
