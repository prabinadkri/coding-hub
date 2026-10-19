import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import free_quota
import hub


class FreeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(hub, 'STATE', Path(self.temp.name) / 'state'))
        self.stack.enter_context(patch.object(hub, 'executable', return_value='agent'))
        self.stack.enter_context(patch.object(free_quota, 'snapshot', return_value={'available': False}))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_free_auto_is_lazy_and_cannot_select_subscription_routes(self):
        with patch.object(hub, 'candidates', side_effect=lambda route, quality: [(route, 'model')]) as available:
            choices = hub.route_options('free-auto', 'deep')
            self.assertEqual(next(choices), ('free', 'model'))
            available.assert_called_once_with('free', 'deep')
            self.assertEqual(list(choices), [('local', 'model')])
            self.assertEqual([c.args[0] for c in available.call_args_list], ['free', 'local'])

    def test_unknown_prices_fall_back_to_local_without_using_a_cloud_account(self):
        with patch.object(hub, 'refresh_free_models', side_effect=OSError('offline')), \
             patch.object(hub, 'local_models', return_value=[hub.LOCAL_AGENT_MODEL]):
            self.assertEqual(list(hub.route_options('free-auto', 'fast')), [('local', hub.LOCAL_AGENT_MODEL)])

    def test_default_task_carries_partial_work_from_free_to_local(self):
        prompts = []

        def process(args, project, env, log):
            prompts.append(args[-1])
            if len(prompts) == 1:
                (project / 'partial.py').write_text('started = True\n')
                return 1, '429 rate limit after partial.py', True
            self.assertTrue((project / 'partial.py').is_file())
            return 0, 'Completed and checked.', False

        with patch.object(hub, 'candidates', side_effect=lambda route, quality:
                          [('free', 'beam-free')] if route == 'free' else [('local', hub.LOCAL_AGENT_MODEL)]) as candidates, \
             patch.object(hub, 'run_process', side_effect=process):
            self.assertEqual(hub.run_task(self.project, 'Build a parser', apply=True), 0)
            self.assertEqual([call.args[0] for call in candidates.call_args_list], ['free', 'local'])
        self.assertIn('Build a parser', prompts[1])
        self.assertIn('partial.py', prompts[1])

    def test_cooldown_avoids_cloud_execution(self):
        with patch.object(hub, 'refresh_free_models', return_value=['beam-free']), \
             patch.object(free_quota, 'routing_cooldowns', return_value={
                 'beam-free': {'retry_at': 9999999999, 'basis': 'provider Retry-After'}}), \
             patch.object(hub, 'local_models', return_value=[hub.LOCAL_AGENT_MODEL]):
            self.assertEqual(list(hub.route_options('free-auto', 'fast')), [('local', hub.LOCAL_AGENT_MODEL)])

    def test_new_shared_limit_stops_the_rest_of_the_cloud_chain(self):
        with patch.object(hub, 'candidates', side_effect=lambda route, quality:
                          [('free', 'beam-free'), ('free', 'space-bunny-free')] if route == 'free'
                          else [('local', hub.LOCAL_AGENT_MODEL)]), \
             patch.object(free_quota, 'routing_cooldowns', return_value={'space-bunny-free': {}}):
            self.assertEqual(list(hub.route_options('free-auto', 'fast')),
                             [('free', 'beam-free'), ('local', hub.LOCAL_AGENT_MODEL)])

    def test_coding_allowlist_excludes_retired_paid_and_arbitrary_models(self):
        free = {'tool_call': True, 'cost': {'input': 0, 'output': 0}}
        models = {name: dict(free) for name in hub.FREE_MODELS}
        models['beam-free'] = dict(free, status='deprecated')
        models['deepseek-v4.1-flash-free'] = dict(free, cost={'input': 0, 'output': 1})
        models['unknown-free'] = free
        self.assertEqual(hub.free_models({'opencode': {'models': models}}),
                         ['space-bunny-free', 'longcat-2.5-preview-free', 'big-pickle'])

    def test_automatic_route_cannot_override_its_model_to_a_paid_one(self):
        with self.assertRaises(ValueError):
            hub.validate_model('free-auto', 'paid-model')


class CooldownTests(unittest.TestCase):
    def payload(self, kind='rate_limit', retry=1200, success=0):
        return {'available': True, 'models': [
            {'id': 'beam-free', 'observation': {'observed_at': 950, 'retry_at': retry, 'kind': kind}},
            {'id': 'space-bunny-free', 'last_success_at': success}]}

    def test_model_limit_only_skips_that_model(self):
        result = free_quota.routing_cooldowns(self.payload(), now=1000)
        self.assertEqual(set(result), {'beam-free'})
        self.assertEqual(result['beam-free']['retry_at'], 1200)

    def test_shared_free_limit_skips_all_until_retry_time(self):
        self.assertEqual(set(free_quota.routing_cooldowns(self.payload('free_limit'), now=1000)), set(hub.FREE_MODELS))
        self.assertEqual(free_quota.routing_cooldowns(self.payload('free_limit'), now=1201), {})

    def test_later_success_clears_a_shared_observation(self):
        self.assertEqual(free_quota.routing_cooldowns(self.payload('free_limit', success=980), now=1000), {})
        self.assertEqual(free_quota.routing_cooldowns(self.payload('free_limit', success=950.5), now=1000), {})

    def test_missing_reset_uses_bounded_local_backoff_not_a_claimed_quota_reset(self):
        result = free_quota.routing_cooldowns(self.payload(retry=None), now=1000)
        self.assertEqual(result['beam-free']['retry_at'], 1250)
        self.assertIn('reset unknown', result['beam-free']['basis'])
        self.assertEqual(free_quota.routing_cooldowns(self.payload(retry=None), now=1251), {})

    def test_future_or_unavailable_observations_do_not_block_routes(self):
        self.assertEqual(free_quota.routing_cooldowns(self.payload(), now=900), {})
        self.assertEqual(free_quota.routing_cooldowns({'available': False}, now=1000), {})


if __name__ == '__main__':
    unittest.main()
