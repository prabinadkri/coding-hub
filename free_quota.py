"""Read-only local OpenCode usage and provider-reported rate-limit observations.

Local message/token counts are not the provider's quota counter. Free limits can
be shared by public IP; production allowances are not publicly reported.
"""
from __future__ import annotations
import contextlib
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import hub

NAMES = {'space-bunny-free': 'Space Bunny', 'longcat-2.5-preview-free': 'LongCat 2.5 Preview', 'big-pickle': 'Big Pickle'}
LIMITER_SOURCE = 'https://github.com/anomalyco/opencode/blob/5d9cd9b259f0456522f318a7435501d03cfbee79/packages/console/app/src/routes/zen/util/ipRateLimiter.ts'
COVERAGE = ('Local OpenCode observations on this laptop, across projects. These are AI response and token counts, '
            'not remaining requests. Retries, other devices and shared public-IP usage may be missing. Free models can share a quota.')
TOKEN_FIELDS = ('input', 'output', 'reasoning', 'cache_read', 'cache_write')


def database_path():
    override = os.environ.get('CODING_HUB_OPENCODE_DB')
    return Path(override).expanduser() if override else Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'opencode' / 'opencode.db'


def number(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else 0


def retry_at(value, observed):
    """RFC Retry-After: delay seconds or an HTTP date, never a guessed timestamp."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0 or seconds > 366 * 86400:
            return None
        return observed + seconds
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                return None
            stamp = date.timestamp()
            return stamp if observed - 60 <= stamp <= observed + 366 * 86400 else None
        except (ValueError, TypeError, OverflowError):
            return None


def limit_observation(error, observed):
    """Keep only status and Retry-After; never expose raw bodies, headers or keys."""
    if isinstance(error, str):
        try:
            error = json.loads(error)
        except ValueError:
            return None
    if not isinstance(error, dict) or not isinstance(error.get('data'), dict):
        return None
    data = error['data']
    if data.get('statusCode') != 429:
        return None
    headers = data.get('responseHeaders', {})
    retry = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None) if isinstance(headers, dict) else None
    free_limit = False
    try:
        body = json.loads(data.get('responseBody', ''))
        free_limit = isinstance(body, dict) and isinstance(body.get('error'), dict) and body['error'].get('type') == 'FreeUsageLimitError'
    except (ValueError, TypeError):
        pass
    return {'observed_at': observed, 'retry_at': retry_at(retry, observed),
            'kind': 'free_limit' if free_limit else 'rate_limit', 'source': 'OpenCode provider response (HTTP 429)'}


def snapshot(path=None, now=None):
    now = time.time() if now is None else now
    start = int(now // 86400) * 86400
    rows = [{'id': name, 'name': NAMES.get(name, name), 'remaining': None, 'limit': None,
             'status': 'not_reported', 'usage': None, 'observation': None} for name in hub.FREE_MODELS]
    local = {'id': hub.LOCAL_AGENT_MODEL, 'name': 'Local Qwen 8B', 'status': 'no_provider_quota', 'usage': None}
    result = {'available': False, 'checked_at': now, 'day_start': start, 'expected_reset_at': start + 86400,
              'reset_basis': 'Expected daily reset: 00:00 UTC, based on published limiter code. Not a live provider guarantee.',
              'source_url': LIMITER_SOURCE, 'coverage': COVERAGE, 'models': rows, 'local': local, 'error': None, 'clock_warning': None}
    path = Path(path) if path is not None else database_path()
    if not path.is_file():
        result['error'] = 'No local OpenCode usage database found. Usage is unavailable; remaining allowance is not reported.'
        return result
    models = tuple(hub.FREE_MODELS) + (hub.LOCAL_AGENT_MODEL,)
    placeholders = ','.join('?' for _ in models)
    model_filter = f"((json_extract(data,'$.providerID')='opencode' AND json_extract(data,'$.modelID') IN ({','.join('?' for _ in hub.FREE_MODELS)})) OR (json_extract(data,'$.providerID')='ollama' AND json_extract(data,'$.modelID')=?))"
    fields = ('input', 'output', 'reasoning', 'cache.read', 'cache.write')
    columns = ','.join(f"SUM(CASE WHEN json_type(data,'$.tokens.{f}') IN ('integer','real') AND json_extract(data,'$.tokens.{f}')>=0 THEN json_extract(data,'$.tokens.{f}') ELSE 0 END)" for f in fields)
    deadline = time.monotonic() + 3
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            totals = db.execute(f"""SELECT json_extract(data,'$.modelID'),COUNT(*),{columns},
                SUM(CASE WHEN json_type(data,'$.tokens')='object' THEN 1 ELSE 0 END),MAX(time_created)
                FROM message WHERE time_created>=? AND time_created<=? AND json_valid(data)
                AND json_extract(data,'$.role')='assistant' AND {model_filter}
                GROUP BY json_extract(data,'$.modelID')""", (start * 1000, (start + 86400) * 1000 - 1, *models)).fetchall()
            usages = {}
            for row in totals:
                if row[8] > (now + 300) * 1000:
                    result['clock_warning'] = "Some recorded timestamps are ahead of this computer's clock. Daily totals follow the recorded UTC date."
                tokens = dict(zip(TOKEN_FIELDS, [number(v) for v in row[2:7]]))
                usages[row[0]] = dict(responses=row[1], tokens=tokens,
                                      total_tokens=sum(tokens.values()) if row[7] or not row[1] else None)
            for row in [*rows, local]:
                row['usage'] = usages.get(row['id'], {'responses': 0, 'tokens': dict.fromkeys(TOKEN_FIELDS, 0), 'total_tokens': 0})
            # Only select provider/model/error metadata. No message text, paths,
            # titles, credentials, or tool output is returned by these queries.
            recent = db.execute(f"""SELECT json_extract(data,'$.modelID'), time_updated,
                json_extract(data,'$.error') FROM message WHERE time_updated>=? AND time_updated<=?
                AND json_valid(data) AND json_extract(data,'$.role')='assistant'
                AND json_extract(data,'$.error.data.statusCode')=429 AND {model_filter}
                ORDER BY time_updated DESC LIMIT 100""", ((now - 7 * 86400) * 1000, now * 1000, *models)).fetchall()
            # Some retry errors are stored as parts before the final response.
            retries = db.execute(f"""SELECT json_extract(m.data,'$.modelID'),p.time_created,json_extract(p.data,'$.error')
                FROM part p JOIN message m ON p.message_id=m.id WHERE p.time_created>=? AND p.time_created<=?
                AND json_valid(p.data) AND json_valid(m.data) AND json_extract(p.data,'$.type')='retry'
                AND json_extract(p.data,'$.error.data.statusCode')=429
                AND json_extract(m.data,'$.providerID')='opencode'
                AND json_extract(m.data,'$.modelID') IN ({placeholders})
                ORDER BY p.time_created DESC LIMIT 100""", ((now - 7 * 86400) * 1000, now * 1000, *models)).fetchall()
            successes = dict(db.execute(f"""SELECT json_extract(data,'$.modelID'),MAX(time_updated) FROM message
                WHERE time_updated>=? AND time_updated<=? AND json_valid(data)
                AND json_extract(data,'$.role')='assistant' AND json_extract(data,'$.finish') IS NOT NULL
                AND json_extract(data,'$.error') IS NULL AND {model_filter}
                GROUP BY json_extract(data,'$.modelID')""", ((now - 7 * 86400) * 1000, now * 1000, *models)).fetchall())
            observed = {}
            for model, timestamp, error in sorted(recent + retries, key=lambda row: row[1], reverse=True):
                if model in observed or timestamp <= successes.get(model, 0):
                    continue
                observation = limit_observation(error, timestamp / 1000)
                if observation:
                    observed[model] = observation
            for row in rows:
                observation = observed.get(row['id'])
                if observation:
                    row['observation'] = observation
                    row['status'] = ('limit_observed' if observation['retry_at'] is None else
                                     'retry_elapsed' if observation['retry_at'] <= now else 'rate_limited')
        result['available'] = True
    except (sqlite3.Error, OSError, ValueError):
        for row in [*rows, local]:
            row.update(usage=None, observation=None)
        result['error'] = 'Local usage could not be read (database busy, unsupported schema, or query time limit). Remaining allowance is unknown.'
    return result


class FreeQuotaCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.refreshing = False
        self.attempted_at = 0

    def get(self, force=False):
        with self.lock:
            if not self.refreshing and (force or time.monotonic() - self.attempted_at >= 20):
                self.refreshing = True
                self.attempted_at = time.monotonic()
                threading.Thread(target=self.update, daemon=True).start()
            return dict(self.value or {'models': [], 'local': {}, 'checked_at': None}, refreshing=self.refreshing)

    def update(self):
        try:
            value = snapshot()
            with self.lock:
                self.value = value
        finally:
            with self.lock:
                self.refreshing = False
