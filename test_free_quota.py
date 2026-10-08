import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import free_quota
import hub


class FreeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'opencode.db'
        self.now = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc).timestamp()
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE message(id TEXT PRIMARY KEY,time_created INTEGER,time_updated INTEGER,data TEXT); CREATE TABLE part(id TEXT,message_id TEXT,time_created INTEGER,data TEXT);')

    def tearDown(self):
        self.temp.cleanup()

    def message(self, ident, model='space-bunny-free', provider='opencode', at=None, **extra):
        at = self.now - 120 if at is None else at
        data = {'role': 'assistant', 'modelID': model, 'providerID': provider,
                'finish': 'stop', 'tokens': {'input': 10, 'output': 5, 'reasoning': 2, 'cache': {'read': 3, 'write': 1}}}
        data.update(extra)
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO message VALUES(?,?,?,?)', (ident, int(at * 1000), int(at * 1000), json.dumps(data)))

    def error(self, retry='300'):
        return {'name': 'APIError', 'data': {'statusCode': 429, 'message': 'private prompt',
                'responseHeaders': {'Retry-After': retry, 'Authorization': 'secret-key'},
                'responseBody': json.dumps({'error': {'type': 'FreeUsageLimitError'}, 'private': 'secret-key'})}}

    def test_counts_are_scoped_to_model_provider_role_and_utc_day(self):
        self.message('a')
        self.message('b')
        self.message('user', role='user')
        self.message('paid', model='paid-model')
        self.message('other-provider', provider='other')
        self.message('previous-day', at=self.now - 86400)
        self.message('future-day', at=(self.now // 86400 + 1) * 86400 + 10)
        self.message('local', model=hub.LOCAL_AGENT_MODEL, provider='ollama')
        before = self.path.read_bytes()
        with patch.object(hub, 'executable', side_effect=AssertionError('No agent calls allowed')):
            result = free_quota.snapshot(self.path, self.now)
        self.assertTrue(result['available'])
        row = result['models'][0]
        self.assertEqual(row['usage']['responses'], 2)
        self.assertEqual(row['usage']['total_tokens'], 42)
        self.assertIsNone(row['remaining'])
        self.assertIsNone(row['limit'])
        self.assertEqual(result['local']['usage']['responses'], 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result['expected_reset_at'], datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp())

    def test_provider_retry_time_is_observed_and_secrets_are_not_returned(self):
        self.message('limited', error=self.error(), finish=None)
        result = free_quota.snapshot(self.path, self.now)
        row = result['models'][0]
        self.assertEqual(row['status'], 'rate_limited')
        self.assertEqual(row['observation']['retry_at'], self.now + 180)
        self.assertEqual(row['observation']['kind'], 'free_limit')
        self.assertNotIn('secret-key', json.dumps(result))
        self.assertNotIn('private prompt', json.dumps(result))
        self.assertIsNone(row['remaining'])

    def test_retry_parts_are_seen_and_later_success_clears_the_observation(self):
        self.message('a', finish=None)
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO part VALUES(?,?,?,?)', ('retry', 'a', (self.now - 60) * 1000,
                json.dumps({'type': 'retry', 'error': self.error('600')})))
        self.assertEqual(free_quota.snapshot(self.path, self.now)['models'][0]['status'], 'rate_limited')
        self.message('success', at=self.now - 20)
        row = free_quota.snapshot(self.path, self.now)['models'][0]
        self.assertIsNone(row['observation'])
        self.assertEqual(row['status'], 'not_reported')

    def test_expired_retry_does_not_claim_restored_access(self):
        self.message('limited', error=self.error('30'), finish=None)
        row = free_quota.snapshot(self.path, self.now)['models'][0]
        self.assertEqual(row['status'], 'retry_elapsed')
        self.assertIsNone(row['remaining'])

    def test_missing_retry_is_unknown_and_http_date_is_supported(self):
        observed = free_quota.limit_observation(self.error(None), self.now)
        self.assertIsNone(observed['retry_at'])
        self.assertEqual(free_quota.retry_at('Thu, 08 Oct 2026 18:05:00 GMT', self.now), self.now + 300)
        for value in ('NaN', 'Infinity', '-10', 'tomorrow', 9999999999, True):
            self.assertIsNone(free_quota.retry_at(value, self.now))
        error = self.error();error['data']['statusCode'] = 401
        self.assertIsNone(free_quota.limit_observation(error, self.now))

    def test_missing_database_and_unsupported_schema_do_not_show_zero_usage(self):
        missing = self.path.parent / 'missing.db'
        result = free_quota.snapshot(missing, self.now)
        self.assertFalse(missing.exists())
        self.assertFalse(result['available'])
        self.assertIsNone(result['models'][0]['usage'])
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE part')
        result = free_quota.snapshot(self.path, self.now)
        self.assertFalse(result['available'])
        self.assertIsNone(result['models'][0]['usage'])

    def test_usage_is_zero_only_when_a_readable_database_has_no_observations(self):
        result = free_quota.snapshot(self.path, self.now)
        self.assertTrue(result['available'])
        self.assertEqual(result['models'][0]['usage']['responses'], 0)
        self.assertIsNone(result['models'][0]['remaining'])
        self.message('missing-tokens', tokens=None)
        result = free_quota.snapshot(self.path, self.now)
        self.assertIsNone(result['models'][0]['usage']['total_tokens'])

    def test_clock_correction_preserves_same_day_observations_and_flags_timestamps(self):
        self.message('before-clock-fix', at=self.now + 3600)
        result = free_quota.snapshot(self.path, self.now)
        self.assertEqual(result['models'][0]['usage']['responses'], 1)
        self.assertIn('timestamps', result['clock_warning'])

    def test_cache_refresh_uses_elapsed_time_after_wall_clock_changes(self):
        cache = free_quota.FreeQuotaCache()
        cache.attempted_at = 100
        with patch.object(free_quota.time, 'monotonic', return_value=125), \
             patch.object(free_quota.time, 'time', return_value=1), \
             patch.object(free_quota.threading, 'Thread') as thread:
            cache.get()
        thread.return_value.start.assert_called_once()
        self.assertEqual(cache.attempted_at, 125)

    def test_malformed_errors_are_ignored(self):
        for value in (None, 'not json', [], {}, {'data': []}, {'data': {'statusCode': 500}}):
            self.assertIsNone(free_quota.limit_observation(value, self.now))


if __name__ == '__main__':
    unittest.main()
