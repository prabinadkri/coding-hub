import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import dashboard
import hub


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = patch.object(hub, "STATE", self.root / "state")
        self.state.start()
        self.manager = dashboard.TaskManager(self.root / "tasks", lambda t: [sys.executable, "-c", "import sys; print(sys.argv[1])", t["prompt"]])
        self.server = dashboard.DashboardServer(("127.0.0.1", 0), manager=self.manager, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.accounts.close()
        self.manager.close()
        deadline = time.monotonic() + 5
        while self.manager.active() and time.monotonic() < deadline:
            time.sleep(.02)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.state.stop()
        self.temp.cleanup()

    def request(self, path, body=None, token="test-token", origin=None, host=None):
        headers = {"X-CodeHub-Token": token}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=raw, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def wait_task(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.manager.detail(identifier)
            if item["status"] not in dashboard.ACTIVE:
                return item
            time.sleep(.02)
        self.fail("Task did not finish")

    def test_unauthenticated_requests_cannot_read_history_or_start_tasks(self):
        self.assertEqual(self.request("/api/tasks", token="")[0], 401)
        self.assertEqual(self.request("/api/tasks", {"project": str(self.project), "prompt": "hello"}, token="wrong")[0], 401)
        self.assertEqual(self.request('/api/free-quota/refresh', {}, token='')[0], 401)
        self.assertEqual(self.manager.list(), [])
        for endpoint in ('/api/projects', '/api/project', '/api/conversation', '/api/changes'):
            self.assertEqual(self.request(endpoint, token='')[0], 401)
        self.assertEqual(self.request('/api/conversation/delete', {}, token='')[0], 401)

    def test_smart_manager_and_worker_choices_reach_the_task_command(self):
        code, task = self.request('/api/tasks', {'project': str(self.project), 'prompt': 'Build the app', 'backend': 'smart', 'model': 'claude-opus-4-6-thinking', 'workers': 2})
        self.assertEqual(code, 201)
        self.wait_task(task['id'])
        command = self.manager.command(task)
        self.assertEqual(command[command.index('--model') + 1], 'claude-opus-4-6-thinking')
        self.assertEqual(command[command.index('--workers') + 1], '2')
        self.assertEqual(task['workers'], 2)
        self.assertEqual(self.request('/api/tasks', {'project': str(self.project), 'prompt': 'Build', 'backend': 'smart', 'workers': 9})[0], 400)

    def test_delete_endpoint_removes_history_and_rejects_active_chat(self):
        code, task = self.request('/api/tasks', {'project': str(self.project), 'prompt': 'Disposable chat'})
        self.assertEqual(code, 201)
        self.wait_task(task['id'])
        body = {'project': str(self.project), 'id': task['conversation']}
        self.assertEqual(self.request('/api/conversation/delete', body, origin='https://outside.example')[0], 403)
        with self.manager.lock:
            self.manager.tasks[task['id']]['status'] = 'queued'
            self.manager.persist(self.manager.tasks[task['id']])
        self.assertEqual(self.request('/api/conversation/delete', body)[0], 409)
        with self.manager.lock:
            self.manager.tasks[task['id']]['status'] = 'completed'
            self.manager.persist(self.manager.tasks[task['id']])
        self.assertEqual(self.request('/api/conversation/delete', body)[0], 200)
        self.assertEqual(self.request('/api/tasks')[1]['tasks'], [])
        self.assertEqual(self.request('/api/tasks/' + task['id'])[0], 404)
        self.assertEqual(self.request('/api/projects')[1]['projects'][0]['conversations'], [])
        self.assertFalse((self.manager.directory / (task['id'] + '.log')).exists())
        restarted = dashboard.TaskManager(self.manager.directory)
        self.assertEqual(restarted.list(), [])

    def test_cli_deletion_is_reflected_by_running_dashboard(self):
        _, task = self.request('/api/tasks', {'project': str(self.project), 'prompt': 'Delete elsewhere'})
        self.wait_task(task['id'])
        dashboard.ProjectMemory(self.project).delete_conversation(task['conversation'], self.manager.directory)
        self.assertEqual(self.request('/api/tasks')[1]['tasks'], [])
        self.assertEqual(self.request('/api/tasks/' + task['id'])[0], 404)

    def test_progress_uses_observed_stages_and_keeps_completed_tools_in_the_past(self):
        task = {'status':'running'}
        self.assertIn('Thinking', dashboard.task_progress(task)['label'])
        self.assertEqual(dashboard.task_progress(task, '[Smart 1] Antigravity manager · concise plan')['label'], 'Planning…')
        writing = dashboard.task_progress(task, '[tool] write · running')
        self.assertEqual(writing['label'], 'Write file…')
        completed = dashboard.task_progress(task, '[tool] write · completed')
        self.assertIn('Last action', completed['detail'])
        self.assertIn('Thinking', completed['label'])
        reviewing = dashboard.task_progress(task, '[tool] write · completed\n[Smart review] Antigravity manager · bounded evidence review')
        self.assertEqual(reviewing['label'], 'Reviewing changes…')
        self.assertEqual(dashboard.task_progress({'status':'stopping'})['label'], 'Stopping…')
        self.assertIn('parallel', dashboard.task_progress(task, '[Smart worker 1/2] Finished')['label'])
        self.assertIn('Integrating', dashboard.task_progress(task, '[Smart integration] Combined edits\n[Smart worker] free → worker')['label'])

    def test_general_task_needs_no_project_and_cannot_mix_project_chats(self):
        code, task = self.request('/api/tasks', {'scope':'general','prompt':'Explain Linux memory'})
        self.assertEqual(code, 201)
        self.assertEqual(task['scope'],'general')
        self.assertEqual(Path(task['project']), hub.general_workspace().resolve())
        self.wait_task(task['id'])
        self.assertIn('--general',self.manager.command(task))
        self.assertNotIn('--project',self.manager.command(task))
        code, data = self.request('/api/conversation?project='+task['project']+'&id='+task['conversation'])
        self.assertEqual(data['scope'],'general')
        code, _ = self.request('/api/tasks', {'project':str(self.project),'conversation':task['conversation'],'prompt':'Cross project'})
        self.assertEqual(code,400)
        tree=self.request('/api/projects')[1]['projects']
        self.assertEqual(tree[0]['name'],'General chats')
        self.assertEqual(self.request('/api/diagnostics',token='')[0],401)

    def test_account_endpoints_require_token_and_reject_arbitrary_provider(self):
        for endpoint in ('/api/accounts/start', '/api/accounts/input', '/api/accounts/close'):
            self.assertEqual(self.request(endpoint, {}, token='')[0], 401)
        self.assertEqual(self.request('/api/accounts/session?id=example', token='')[0], 401)
        self.assertEqual(self.request('/api/accounts/start', {'provider':'shell'})[0], 400)
        self.assertEqual(self.request('/api/accounts/session?id=missing')[0], 400)
        self.assertIsNone(self.server.accounts.session)

    def test_project_memory_and_conversations_are_scoped_to_the_selected_project(self):
        _, task = self.request('/api/tasks', {'project': str(self.project), 'prompt': 'Initial question'})
        self.wait_task(task['id'])
        conversation = task['conversation']
        code, followup = self.request('/api/tasks', {'project': str(self.project), 'prompt': 'Follow-up', 'conversation': conversation})
        self.assertEqual(code, 201)
        self.assertEqual(followup['conversation'], conversation)
        self.wait_task(followup['id'])
        other = self.root / 'other'
        other.mkdir()
        self.assertEqual(self.request('/api/tasks', {'project': str(other), 'prompt': 'wrong project', 'conversation': conversation})[0], 400)
        self.assertEqual(self.request('/api/project/notes', {'project': str(self.project), 'requirements': 'Preserve APIs.'})[0], 200)
        self.assertEqual(self.request('/api/project/init', {'project': str(self.project)})[0], 200)
        self.assertTrue((self.project / 'CODING_HUB.md').is_file())
        self.assertTrue(self.request('/api/projects')[1]['projects'])

    def test_foreign_origin_and_dns_rebinding_hosts_are_rejected(self):
        self.assertEqual(self.request("/api/health", origin="https://outside.example")[0], 403)
        self.assertEqual(self.request("/api/health", host=f"attacker.example:{self.server.server_port}")[0], 421)
        self.assertEqual(self.request("/api/health", origin=self.url)[0], 200)

    def test_real_task_execution_keeps_prompt_literal_and_project_files_intact(self):
        (self.project / "keep.txt").write_text("user content")
        prompt = "$(touch BAD); `whoami`; <script>alert(1)</script>"
        code, task = self.request("/api/tasks", {"project": str(self.project), "prompt": prompt, "backend": "local"})
        self.assertEqual(code, 201)
        result = self.wait_task(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn(prompt, result["output"])
        self.assertFalse((self.project / "BAD").exists())
        self.assertEqual((self.project / "keep.txt").read_text(), "user content")
        self.assertEqual(self.request("/api/tasks")[1]["tasks"][0]["mode"], "analysis")

    def test_stop_cancels_actual_child_and_blocks_overlapping_tasks(self):
        self.manager.command_factory = lambda t: [sys.executable, "-u", "-c", "import time;print('started');time.sleep(30)"]
        body = {"project": str(self.project), "prompt": "wait"}
        _, task = self.request("/api/tasks", body)
        self.assertEqual(self.request("/api/tasks", body)[0], 409)
        self.assertEqual(self.request("/api/stop", {"id": task["id"]})[0], 200)
        self.assertEqual(self.wait_task(task["id"])["status"], "canceled")

    def test_invalid_task_parameters_do_not_launch_a_process(self):
        for extra in ({"backend": "paid"}, {"mode": "shell"}, {"quality": "unknown"}, {"prompt": ["bad"]}, {"project": "/no-such-directory-abc"}):
            self.assertEqual(self.request("/api/tasks", dict({"project": str(self.project), "prompt": "task"}, **extra))[0], 400)
        self.assertEqual(self.manager.list(), [])

    def test_smart_review_status_and_byte_limit(self):
        data = {'project': str(self.project), 'prompt': 'अ' * 2100, 'backend': 'smart'}
        self.assertEqual(self.request('/api/tasks', data)[0], 400)
        self.assertEqual(self.manager.list(), [])
        self.manager.command_factory = lambda t: [sys.executable, '-c', 'raise SystemExit(3)']
        data['prompt'] = 'Implement checkout validation'
        code, task = self.request('/api/tasks', data)
        self.assertEqual(code, 201)
        self.assertEqual(self.wait_task(task['id'])['status'], 'needs_review')

    def test_unknown_task_cannot_read_arbitrary_files(self):
        self.assertEqual(self.request("/api/tasks/../../private")[0], 404)

    def test_folder_picker_returns_only_visible_directories(self):
        (self.project / "src").mkdir()
        (self.project / ".hidden").mkdir()
        (self.project / "private.txt").write_text("do not return this content")
        from urllib.parse import quote
        code, data = self.request("/api/folders?path=" + quote(str(self.project)))
        self.assertEqual(code, 200)
        self.assertEqual([p["name"] for p in data["folders"]], ["src"])

    def test_restart_marks_unfinished_tasks_interrupted(self):
        task = {"id": "example", "status": "running", "created_at": 1}
        hub.save_json(self.root / "tasks" / "example.json", task)
        restored = dashboard.TaskManager(self.root / "tasks")
        self.assertEqual(restored.detail("example")["status"], "interrupted")

    def test_static_page_has_security_headers_and_no_external_dependencies(self):
        with urllib.request.urlopen(self.url) as response:
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            content = response.read().decode()
            self.assertNotIn('<script src="http', content)
            self.assertIn('href="/style.css"', content)


class RoutingTests(unittest.TestCase):
    def test_auto_does_not_check_free_prices_when_first_route_succeeds(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(hub, "STATE", Path(folder) / "state"), \
             patch.object(hub, "candidates", return_value=[("antigravity", hub.AGY_MODELS["fast"])]) as candidates, \
             patch.object(hub, "executable", return_value="agent"), \
             patch.object(hub, "run_process", return_value=(0, "done", False)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub.run_task(folder, "test"), 0)
            candidates.assert_called_once_with("antigravity", "fast")


if __name__ == "__main__":
    unittest.main()
