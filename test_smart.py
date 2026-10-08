import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hub
import smart_route as smart


class SmartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.folder = self.root / 'task'
        self.folder.mkdir()
        self.state = patch.object(hub, 'STATE', self.root / 'state')
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def run_route(self, replies, apply=True, worker=(0, 'Implemented and checked.', False)):
        with patch.object(smart, 'manager', side_effect=replies) as manager, \
             patch.object(smart, 'evidence', return_value='actual source excerpt'), \
             patch.object(hub, 'candidates', side_effect=lambda route, quality: [(route, 'test-model')]), \
             patch.object(hub, 'command', return_value=['agent']), \
             patch.object(hub, 'run_process', return_value=worker) as process, \
             contextlib.redirect_stdout(io.StringIO()):
            result = smart.execute(self.project, 'Add a parser', 'fast', apply, 'Pinned: preserve API.', self.folder)
        return result, manager, process

    def plan(self):
        return {'ok': True, 'code': 0, 'usage': {'total_tokens': 120},
                'response': {'steps': ['Implement parser'], 'checks': ['Run parser tests']}}

    def review(self, verdict='pass', usage=None):
        return {'ok': True, 'code': 0, 'usage': {'total_tokens': 180} if usage is None else usage,
                'response': {'verdict': verdict, 'summary': 'Reviewed evidence.', 'next_steps': ''}}

    def test_plan_worker_review_caps_manager_calls_and_records_measured_usage(self):
        result, manager, worker = self.run_route([self.plan(), self.review()])
        self.assertEqual(result['code'], 0)
        self.assertEqual(manager.call_count, 2)
        self.assertEqual(worker.call_count, 1)
        self.assertIn('Pinned: preserve API.', manager.call_args.args[1])
        report = json.loads((self.folder / 'smart.json').read_text())
        self.assertEqual(report['manager_total_tokens'], 300)
        self.assertFalse(report['savings_verified'])
        self.assertIsNone(report['direct_baseline_tokens'])
        self.assertIn('unmeasured', result['output'])

    def test_short_analysis_skips_planning(self):
        result, manager, worker = self.run_route([self.review()], apply=False)
        self.assertEqual(manager.call_count, 1)
        self.assertEqual(result['report']['manager_total_tokens'], 180)

    def test_rejected_review_stops_without_recursive_repairs(self):
        result, manager, worker = self.run_route([self.plan(), self.review('revise')])
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['code'], 3)
        self.assertEqual(manager.call_count, 2)
        self.assertEqual(worker.call_count, 1)

    def test_cancellation_does_not_start_review_or_fallback(self):
        result, manager, worker = self.run_route([self.plan()], worker=(130, 'Stopped', True))
        self.assertEqual(result['status'], 'canceled')
        self.assertEqual(manager.call_count, 1)
        self.assertEqual(worker.call_count, 1)

    def test_review_unavailable_is_incomplete_and_unknown_usage_is_not_zero(self):
        failure = {'ok': False, 'code': 1, 'error': 'quota', 'usage': {}}
        result, manager, worker = self.run_route([self.plan(), failure])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['report']['manager_total_tokens'])

    def test_oversized_requests_do_not_invoke_any_provider(self):
        with patch.object(smart, 'manager') as manager, self.assertRaises(ValueError):
            smart.execute(self.project, 'अ' * 2100, 'fast', True, '', self.folder)
        manager.assert_not_called()

    def test_manager_uses_separate_workspace_and_validates_provider_schema(self):
        payload = {'status': 'SUCCESS', 'structured_output': self.review()['response'],
                   'usage': {'total_tokens': 123, 'private_field': 999, 'input_tokens': float('nan')}}
        def execute(args, cwd, env, log, timeout, render_reply):
            self.assertFalse(render_reply)
            Path(log).write_text(json.dumps(payload))
            self.assertNotEqual(cwd, self.project)
            self.assertIn('coding-hub-manager', args)
            self.assertIn('tools: []', (cwd / '.agents/agents/coding-hub-manager.md').read_text())
            self.assertNotIn('--dangerously-skip-permissions', args)
            return 0, '', False
        with patch.object(hub, 'executable', return_value='agy'), patch.object(hub, 'run_process', side_effect=execute):
            result = smart.manager(self.project, 'Review', 'fast', smart.REVIEW_SCHEMA, self.folder / 'review.log')
            self.assertTrue(result['ok'])
            self.assertEqual(result['usage'], {'total_tokens': 123})
            payload['structured_output']['verdict'] = 'probably'
            self.assertFalse(smart.manager(self.project, 'Review', 'fast', smart.REVIEW_SCHEMA, self.folder / 'bad.log')['ok'])

    def test_local_worker_follows_free_failure(self):
        with patch.object(smart, 'manager', return_value=self.review()), \
             patch.object(smart, 'evidence', return_value='source'), \
             patch.object(hub, 'candidates', side_effect=lambda route, quality: [(route, 'model')]), \
             patch.object(hub, 'command', return_value=['agent']), \
             patch.object(hub, 'run_process', side_effect=[(1, '429 quota', True), (0, 'Answer', False)]), \
             contextlib.redirect_stdout(io.StringIO()):
            result = smart.execute(self.project, 'Explain parser', 'fast', False, '', self.folder)
        self.assertEqual([a['backend'] for a in result['report']['worker_attempts']], ['free', 'local'])
        self.assertEqual(result['code'], 0)

    def test_review_receipts_come_from_tool_results_and_are_bounded(self):
        log = self.folder / 'worker.log'
        event = {'type': 'tool_use', 'part': {'tool': 'bash', 'state': {
            'status': 'completed', 'input': {'command': 'python3 -m unittest'},
            'metadata': {'exit': 1}, 'output': 'FAILED: expected ValueError\n' * 1000}}}
        log.write_text(json.dumps(event) + '\n' + json.dumps({'type': 'text', 'part': {'text': 'All tests passed!'}}))
        receipt = smart.command_receipts(log)
        self.assertIn('"exit": 1', receipt)
        self.assertIn('FAILED', receipt)
        self.assertNotIn('All tests passed!', receipt)
        self.assertLess(len(receipt.encode()), 3600)


if __name__ == '__main__':
    unittest.main()
