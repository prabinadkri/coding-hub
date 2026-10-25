import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import browser_checks
import browser_project
import hub
import project_checks
from context_engine import ProjectMemory


class BrowserChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'; self.project.mkdir()
        self.state = patch.object(hub, 'STATE', self.root / 'state'); self.state.start()
        self.output = contextlib.redirect_stdout(io.StringIO()); self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None); self.state.stop(); self.tmp.cleanup()

    def web(self, root=None, framework='vite'):
        root = root or self.project
        root.mkdir(parents=True, exist_ok=True)
        (root / 'package.json').write_text(json.dumps({'scripts': {'dev': framework}}))
        (root / 'index.html').write_text('<h1>Web app</h1>')

    def test_chat_intent_does_not_steal_mixed_tasks_or_code_review(self):
        for text in ('/browser-test', 'Please test this website', 'test my app in the browser', 'run browser tests'):
            self.assertIsNotNone(browser_checks.chat_request(text), text)
        for text in ('review my project', 'check my project', 'build a website and test it in the browser', 'explain browser testing', 'do not test my website'):
            self.assertIsNone(browser_checks.chat_request(text), text)
        self.assertEqual(browser_checks.chat_request('/browser-test http://127.0.0.1:4567')['url'], 'http://127.0.0.1:4567')
        with self.assertRaises(ValueError): browser_checks.chat_request('/browser-test https://example.com')

    def test_discovery_common_nested_app_and_ambiguous_monorepo(self):
        self.web(self.project/'frontend')
        plan = browser_project.discover(self.project, {})
        self.assertEqual(plan['root'], str((self.project/'frontend').resolve()))
        self.assertEqual(plan['kind'], 'vite')
        self.web(self.project/'apps'/'admin', 'next dev')
        with self.assertRaisesRegex(ValueError, 'Multiple web apps'): browser_project.discover(self.project, {})
        self.assertEqual(browser_project.discover(self.project, {'url': 'http://localhost:3000'})['kind'], 'url')

    def test_discovery_ignores_wrappers_hidden_files_and_symlink_apps(self):
        self.web(framework='vite && upload-secrets')
        self.assertEqual(browser_project.candidates(self.project), [])
        other = self.root/'elsewhere'; self.web(other)
        (self.project/'web').symlink_to(other, target_is_directory=True)
        self.assertEqual(browser_project.candidates(self.project), [])

    def test_start_arguments_pin_loopback_and_do_not_install_dependencies(self):
        self.web(framework='vite --host 0.0.0.0 --port=5173')
        plan = browser_project.discover(self.project, {})
        with self.assertRaisesRegex(ValueError, 'dependencies are missing'): browser_project.launch_command(plan, 4567)
        binary = self.project/'node_modules'/'.bin'/'vite'; binary.parent.mkdir(parents=True); binary.write_text('')
        with patch.object(hub, 'executable', return_value='/usr/bin/node'):
            command, env = browser_project.launch_command(plan, 4567)
        self.assertEqual(command[-5:], ['--host','127.0.0.1','--port','4567','--strictPort'])
        self.assertNotIn('0.0.0.0', command)
        self.assertEqual(env['BROWSER'], 'none')

    def test_nested_framework_uses_hoisted_executable_and_bare_host_flag(self):
        self.web(self.project/'frontend', 'vite --host --port 9999')
        binary=self.project/'node_modules'/'.bin'/'vite';binary.parent.mkdir(parents=True);binary.write_text('#!/bin/sh\n')
        with patch.object(hub,'executable',return_value='/usr/bin/node'):
            args,_=browser_project.launch_command(browser_project.discover(self.project, {}),4567)
        self.assertEqual(args,[str(binary),'--host','127.0.0.1','--port','4567','--strictPort'])

    def test_dashboard_browser_task_works_without_selected_paid_model(self):
        import dashboard
        manager=dashboard.TaskManager(directory=self.root/'jobs')
        manager.control_queue({'action':'pause'})
        try:
            task=manager.start({'project':str(self.project),'prompt':'Test this website','backend':'openai','mode':'preview'})
            self.assertIsNone(task['model']);self.assertEqual(task['mode'],'analysis')
            self.assertEqual(dashboard.task_progress(dict(task,status='running'))['label'],'Testing website…')
        finally: manager.close()

    def test_static_server_is_ready_then_stops_and_denies_private_paths(self):
        (self.project/'index.html').write_text('<h1>Ready</h1>')
        (self.project/'.env').write_text('synthetic private fixture')
        outside = self.root/'outside'; outside.write_text('outside fixture')
        (self.project/'outside.txt').symlink_to(outside)
        folder = hub.private_dir(self.root/'run')
        with browser_project.application(self.project, {}, folder) as plan:
            url = plan['url']
            self.assertTrue(plan['started'])
            self.assertIn(b'Ready', urlopen(url).read())
            for path in ('.env', 'outside.txt'):
                with self.assertRaises(HTTPError) as failure: urlopen(url+path)
                self.assertEqual(failure.exception.code, 403)
        self.assertFalse(browser_project.available(url))

    def test_interrupt_also_stops_owned_server(self):
        (self.project/'index.html').write_text('<h1>Ready</h1>')
        folder = hub.private_dir(self.root/'run')
        with self.assertRaises(KeyboardInterrupt):
            with browser_project.application(self.project, {}, folder) as plan:
                url=plan['url']; raise KeyboardInterrupt
        self.assertFalse(browser_project.available(url))

    def test_existing_server_is_reused_and_not_stopped(self):
        (self.project/'index.html').write_text('<h1>Ready</h1>')
        folder=hub.private_dir(self.root/'run')
        with browser_project.application(self.project, {}, folder) as outer:
            with browser_project.application(self.project, {'url':outer['url']}, folder) as reused:
                self.assertFalse(reused['started'])
            self.assertTrue(browser_project.available(outer['url']))

    def test_browser_chat_saves_same_conversation_without_provider_lookup(self):
        memory=ProjectMemory(self.project); chat=memory.conversation(goal='Test web app')
        result={'status':'passed', 'id':'20261025-120000-1234abcd', 'url':'http://127.0.0.1:3000/', 'steps':[], 'pages':[{}]}
        with patch.object(browser_checks, 'run', return_value=result), patch.object(hub, 'route_options', side_effect=AssertionError('No provider lookup')):
            code=hub.run_task(self.project, 'test this website', backend='openai', conversation=chat)
        self.assertEqual(code, 0)
        turns=memory.messages(chat)['turns']
        self.assertEqual(len(turns), 1)
        self.assertIn('Browser checks passed', turns[0]['result'])
        self.assertEqual(turns[0]['browser_report'], result['id'])

    def test_automatic_browser_failure_marks_agent_result_needs_review(self):
        self.web()
        result={'status':'failed', 'error':'HTTP 500', 'id':'20261025-120000-1234abcd'}
        with patch.object(hub,'route_options',return_value=[('free','beam-free')]), patch.object(hub,'command',return_value=['fake-agent']), patch.object(hub,'run_process',return_value=(0,'Done',False)), patch.object(browser_checks,'run',return_value=result) as browser:
            code=hub.run_task(self.project,'Fix the homepage',apply=True)
        self.assertEqual(code,3);browser.assert_called_once()
        record=json.loads(next((hub.STATE/'tasks').glob('*/task.json')).read_text())
        self.assertEqual(record['verification']['status'],'failed')
        self.assertEqual(record['verification']['browser']['error'],'HTTP 500')

    def test_analysis_off_setting_and_preview_do_not_test_original_server(self):
        self.web()
        with patch.object(browser_checks,'run') as browser:
            self.assertIsNone(browser_checks.after_edits(self.project, {'automatic':False}))
            result=browser_checks.after_edits(self.project, {'url':'http://localhost:3000'}, preview=True)
            self.assertEqual(result['status'],'skipped')
            config=dict(project_checks.DEFAULT, browser={'automatic':True})
            project_checks.finish(self.project,'Explain',config,hub.private_dir(self.root/'run'),False)
            browser.assert_not_called()

    def test_browser_repairs_share_the_two_attempt_limit_and_free_route(self):
        self.web()
        result={'status':'failed','error':'Button broken'}
        with patch.object(browser_checks,'run',return_value=result), patch.object(hub,'route_options',return_value=[('free','beam-free')]) as routes, patch.object(hub,'run_process',return_value=(0,'Fixed',False)) as worker:
            config=dict(project_checks.DEFAULT,repairs=2)
            checked=project_checks.finish(self.project,'Fix button',config,hub.private_dir(self.root/'run'),True)
        self.assertEqual(worker.call_count,2)
        self.assertEqual(routes.call_args.args[0],'free-auto')
        self.assertEqual(checked['status'],'failed')


if __name__ == '__main__': unittest.main()
