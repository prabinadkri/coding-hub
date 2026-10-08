import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import hub
import parallel_workers as parallel
import smart_route as smart


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'; self.project.mkdir()
        self.folder = self.root / 'task'; self.folder.mkdir()
        self.state = patch.object(hub, 'STATE', self.root / 'state'); self.state.start()
        self.plan = {'steps': ['Agree API /hello returning text, then integrate.'], 'checks': ['Test the full app'], 'tasks': [
            {'goal': 'Implement frontend calling /hello', 'files': ['frontend.js']},
            {'goal': 'Implement backend /hello', 'files': ['backend.py']}]}

    def tearDown(self):
        self.state.stop(); self.temp.cleanup()

    def test_real_process_workers_overlap_and_integrate_then_review_once(self):
        (self.project / 'notes.txt').write_text('Keep uncommitted user work')
        (self.project / '.env').write_text('must not be copied')
        barrier = threading.Barrier(2)
        observed = []
        real_run = hub.run_process
        def command(backend, model, prompt, apply, project):
            if 'smart-workers' in str(project):
                self.assertFalse((Path(project) / '.env').exists())
                self.assertEqual((Path(project) / 'notes.txt').read_text(), 'Keep uncommitted user work')
                barrier.wait(timeout=3)
                name = 'frontend.js' if model == 'free-a' else 'backend.py'
                script = 'from pathlib import Path; import time,json; time.sleep(.15); Path(' + repr(name) + ').write_text("implemented"); print(json.dumps({"type":"text","part":{"text":"Implemented owned file"}}))'
                observed.append((model, str(project)))
                return [sys.executable, '-c', script]
            self.assertEqual((self.project / 'frontend.js').read_text(), 'implemented')
            self.assertEqual((self.project / 'backend.py').read_text(), 'implemented')
            self.assertIn('integration worker', prompt)
            return [sys.executable, '-c', 'print("Integrated and checked")']
        responses = [{'ok': True, 'code': 0, 'usage': {'total_tokens': 80}, 'response': self.plan},
                     {'ok': True, 'code': 0, 'usage': {'total_tokens': 120}, 'response': {'verdict': 'pass', 'summary': 'Checked', 'next_steps': ''}}]
        with patch.object(smart, 'manager', side_effect=responses) as manager, patch.object(smart, 'evidence', return_value='Combined diff'), patch.object(hub, 'candidates', return_value=[('free','free-a'), ('free','free-b')]), patch.object(hub, 'command', side_effect=command), patch.object(hub, 'run_process', side_effect=real_run), contextlib.redirect_stdout(io.StringIO()):
            result = smart.execute(self.project, 'Build a frontend and backend', 'deep', True, 'API contract: /hello', self.folder, model='claude-manager', workers=2)
        self.assertEqual(result['code'], 0)
        self.assertEqual(len(observed), 2)
        self.assertNotEqual(observed[0][1], observed[1][1])
        self.assertEqual(manager.call_count, 2)
        self.assertTrue(all(c.kwargs['model'] == 'claude-manager' for c in manager.call_args_list))
        self.assertEqual(result['report']['parallel_workers'], 2)
        self.assertEqual(result['report']['manager_total_tokens'], 200)
        self.assertFalse((self.folder / 'smart-workers').exists())
        self.assertEqual((self.project / 'notes.txt').read_text(), 'Keep uncommitted user work')

    def test_overlapping_and_unsafe_assignments_are_rejected(self):
        for name in ('frontend.js', '../outside.py', '/tmp/outside.py', '.env', 'src/../app.py'):
            plan = dict(self.plan, tasks=[self.plan['tasks'][0], {'goal': 'Other', 'files': [name]}])
            with self.assertRaises(ValueError): parallel.assignments(plan, 2)
        with self.assertRaises(ValueError): parallel.assignments(self.plan, 1)

    def test_integration_preserves_concurrent_edits_before_touching_any_file(self):
        first = self.project / 'frontend.js'; first.write_text('old')
        second = self.project / 'backend.py'; second.write_text('old')
        baseline = parallel.source_snapshot(self.project)
        second.write_text('user edit while workers run')
        with self.assertRaisesRegex(ValueError, 'changed while workers'):
            parallel.integrate(self.project, baseline, {'frontend.js': (b'new', 0o644), 'backend.py': (b'new', 0o644)})
        self.assertEqual(first.read_text(), 'old')
        self.assertEqual(second.read_text(), 'user edit while workers run')

    def test_worker_scope_violation_never_reaches_original_project(self):
        baseline = parallel.source_snapshot(self.project)
        def fake(args, cwd, env, log, **kwargs):
            (Path(cwd) / 'unassigned.py').write_text('unexpected')
            return 0, 'Done', False
        with patch.object(hub, 'command', return_value=['fake']), patch.object(hub, 'run_process', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            results, integrated, reason = parallel.run(self.project, 'Build', self.plan, '', self.folder, 'fast', 2, [('free','a'),('free','b')], baseline)
        self.assertFalse(integrated)
        self.assertTrue(all(r['status'] == 'needs_review' for r in results))
        self.assertFalse(list(self.project.iterdir()))
        self.assertTrue((self.folder / 'smart-workers').exists())

    def test_failed_cloud_workers_use_only_one_local_worker_at_a_time(self):
        active = peak = 0
        lock = threading.Lock()
        def fake(args, cwd, env, log, **kwargs):
            nonlocal active, peak
            if 'free' in Path(log).name: return 1, '429 quota', True
            with lock: active += 1; peak = max(peak, active)
            time.sleep(.1)
            name = 'frontend.js' if str(cwd).endswith('worker-1') else 'backend.py'
            (Path(cwd) / name).write_text('done')
            with lock: active -= 1
            return 0, 'Completed locally', False
        with patch.object(hub, 'command', return_value=['fake']), patch.object(hub, 'candidates', return_value=[('local','qwen')]), patch.object(hub, 'run_process', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            results, integrated, _ = parallel.run(self.project, 'Build', self.plan, '', self.folder, 'fast', 2, [('free','a'),('free','b')], {})
        self.assertTrue(integrated)
        self.assertEqual(peak, 1)
        self.assertEqual([r['attempts'][-1]['backend'] for r in results], ['local','local'])

    def test_cancel_event_terminates_real_worker_process(self):
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set); timer.start()
        started = time.monotonic()
        try:
            result = hub.run_process([sys.executable, '-c', 'import time; time.sleep(30)'], self.project, dict(os.environ), self.folder / 'cancel.log', cancel_event=cancel, quiet=True)
        finally: timer.cancel()
        self.assertEqual(result[0], 130)
        self.assertLess(time.monotonic() - started, 4)

    @unittest.skipIf(os.name == 'nt', 'POSIX signal behavior')
    def test_quiet_cancel_force_stops_a_worker_ignoring_termination(self):
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set); timer.start()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                result = hub.run_process([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("private tool detail",flush=True); time.sleep(30)'], self.project, dict(os.environ), self.folder / 'stubborn.log', cancel_event=cancel, quiet=True)
        finally:
            timer.cancel()
        self.assertEqual(result[0], 130)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('private tool detail', (self.folder / 'stubborn.log').read_text())

    def test_selected_manager_model_and_prompt_budget_reach_cli(self):
        def fake(args, cwd, env, log, **kwargs):
            self.assertEqual(args[args.index('--model') + 1], 'claude-top')
            self.assertLessEqual(len(args[args.index('-p') + 1].encode()), smart.MANAGER_PROMPT_LIMIT)
            Path(log).write_text(json.dumps({'status':'SUCCESS','structured_output':{'verdict':'pass','summary':'Checked','next_steps':''},'usage':{'total_tokens':100}}))
            return 0, '', False
        with patch.object(hub, 'executable', return_value='agy'), patch.object(hub, 'run_process', side_effect=fake):
            self.assertTrue(smart.manager(self.project, 'long context ' * 5000, 'fast', smart.REVIEW_SCHEMA, self.folder / 'manager.log', model='claude-top')['ok'])

    def test_worker_count_validation(self):
        for count in (0, 4, True, '2', 1.5):
            with self.assertRaises(ValueError): hub.validate_workers('smart', count)
        with self.assertRaises(ValueError): hub.validate_workers('local', 2)
        self.assertEqual(hub.validate_workers('smart', 3), 3)
