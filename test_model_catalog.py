import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
import dashboard
import hub


class ModelCatalogTests(unittest.TestCase):
    def choices(self, *ids):
        return [{'id': mid, 'name': mid} for mid in ids]

    def test_native_catalog_includes_future_models_and_retirement_notice(self):
        result = subprocess.CompletedProcess([], 0,
            'gemini-9-flash-high\tGemini future\nclaude-sonnet-4-6\tClaude Sonnet\nnoise\n', '')
        with patch.object(hub, 'executable', return_value='agy'), patch.object(accounts.subprocess, 'run', return_value=result):
            models = accounts.antigravity_models()
        self.assertEqual([m['id'] for m in models], ['gemini-9-flash-high', 'claude-sonnet-4-6'])
        self.assertIn('Nov 2, 2026', models[1]['name'])

    def test_native_failure_cannot_masquerade_as_available_models(self):
        for result in (subprocess.CompletedProcess([], 1, 'gemini-x\tModel\n', 'private detail'),
                       subprocess.CompletedProcess([], 0, 'Authentication required', '')):
            with patch.object(hub, 'executable', return_value='agy'), patch.object(accounts.subprocess, 'run', return_value=result):
                with self.assertRaises(ValueError) as error:
                    accounts.antigravity_models()
                self.assertNotIn('private detail', str(error.exception))

    def test_defaults_adapt_to_the_current_account_and_future_versions(self):
        models = self.choices('gemini-3.8-flash-high', 'gemini-3.8-flash-medium', 'claude-opus-4-6-thinking')
        with patch.object(accounts, 'antigravity_models', return_value=models):
            self.assertEqual(accounts.resolve_antigravity_model('fast'), 'gemini-3.8-flash-medium')
            self.assertEqual(accounts.resolve_antigravity_model('deep'), 'gemini-3.8-flash-high')
            models.extend(self.choices('gemini-10-flash-medium', 'gemini-10-pro-high'))
            self.assertEqual(accounts.resolve_antigravity_model('fast'), 'gemini-10-flash-medium')
            self.assertEqual(accounts.resolve_antigravity_model('deep'), 'gemini-10-pro-high')

    def test_explicit_account_model_is_kept_or_rejected_never_substituted(self):
        with patch.object(accounts, 'antigravity_models', return_value=self.choices('claude-future', 'gemini-flash')):
            self.assertEqual(accounts.resolve_antigravity_model('deep', 'claude-future'), 'claude-future')
            with self.assertRaisesRegex(ValueError, 'no longer available'):
                accounts.resolve_antigravity_model('deep', 'retired-model')

    def test_catalog_failure_skips_automatic_antigravity_without_fixed_default(self):
        with patch.object(hub, 'executable', return_value='agy'), \
             patch.object(accounts, 'antigravity_models', side_effect=ValueError('offline')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub.candidates('antigravity', 'deep'), [])

    def test_forced_refresh_reloads_recent_catalog(self):
        cache = accounts.AccessCache()
        cache.catalog_updated = time.monotonic()
        with patch.object(accounts, 'connection_status', return_value={}), \
             patch.object(accounts, 'model_catalog', return_value={'antigravity': self.choices('new-model')}) as fetch:
            cache.refresh()
            fetch.assert_not_called()
            cache.refresh(force=True)
            fetch.assert_called_once()
        self.assertEqual(cache.value['provider_models']['antigravity'][0]['id'], 'new-model')
        self.assertFalse(cache.value['antigravity_catalog_error'])

    def test_price_refresh_failure_hides_old_free_options(self):
        cache = dashboard.PricingCache()
        cache.value.update(free_models=['was-free'], pricing_checked_at=100)
        with patch.object(hub, 'refresh_free_models', side_effect=OSError('offline')):
            cache.refresh()
        self.assertEqual(cache.value['free_models'], [])
        self.assertTrue(cache.value['pricing_error'])
        self.assertIn('could not be verified', accounts.catalog_notes(cache.value)['free'])

    def test_new_coding_models_are_price_gated_and_paid_kimi_stays_excluded(self):
        good = {'tool_call': True, 'cost': {'input': 0, 'output': 0}}
        models = {name: good for name in ('glm-5.3-flash-free', 'mimo-v2.6-flash-free', 'nemotron-3-ultra-free')}
        models['kimi-k3.1-pro'] = dict(good, cost={'input': 2, 'output': 10})
        models['kimi-k2.5-free'] = dict(good, status='deprecated')
        self.assertEqual(hub.free_models({'opencode': {'models': models}}),
                         ['glm-5.3-flash-free', 'mimo-v2.6-flash-free'])
        models['glm-5.3-flash-free'] = dict(good, cost={'input': 0, 'output': 0, 'cache_write': 1})
        self.assertNotIn('glm-5.3-flash-free', hub.free_models({'opencode': {'models': models}}))

    def test_removed_explicit_antigravity_model_stops_before_creating_task(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(hub, 'STATE', Path(tmp) / 'state'), \
             patch.object(accounts, 'antigravity_models', return_value=self.choices('available-model')):
            with self.assertRaisesRegex(ValueError, 'no longer available'):
                hub.run_task(Path(tmp), 'Inspect this project', backend='antigravity', model='retired-model')
            self.assertFalse((Path(tmp) / 'state/tasks').exists())


if __name__ == '__main__':
    unittest.main()
