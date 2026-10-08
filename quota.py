"""Read live limits through Antigravity's own supported /usage command."""
from __future__ import annotations
import json
import math
import subprocess
import threading
import time
from datetime import datetime
import hub


def parse_usage(payload):
    if payload.get('status') != 'SUCCESS' or payload.get('command', {}).get('name') != 'usage':
        raise ValueError('Antigravity did not return a quota report. Open agy and sign in if needed.')
    groups = []
    for group in payload['command'].get('data', {}).get('groups', []):
        buckets = []
        for bucket in group.get('buckets', []):
            fraction = bucket.get('remaining_fraction')
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
                continue
            reset = bucket.get('reset_time')
            try:
                reset_at = datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp() if isinstance(reset, str) else None
            except ValueError:
                reset_at = None
            buckets.append({'id': str(bucket.get('id', ''))[:100], 'name': str(bucket.get('name', 'Quota'))[:150],
                            'remaining_percent': round(fraction * 100, 2), 'reset_at': reset_at,
                            'window': str(bucket.get('window', ''))[:30]})
        if buckets:
            groups.append({'name': str(group.get('name', 'Models'))[:150], 'buckets': buckets})
    if not groups:
        raise ValueError('No quota amounts were reported by this CLI version.')
    return {'available': True, 'groups': groups, 'checked_at': time.time(), 'source': 'Antigravity /usage', 'error': None}


def refresh():
    binary = hub.executable('agy')
    if not binary:
        raise RuntimeError('Install Antigravity CLI to see its quota.')
    # /usage is handled by the CLI itself: no model turn, token extraction or private API.
    result = subprocess.run([binary, '-p', '/usage', '--output-format', 'json', '--print-timeout', '15s'],
                            env=hub.clean_environment('antigravity'), capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError('Antigravity quota check failed. Open agy and run /usage to check sign-in.')
    snapshot = parse_usage(json.loads(result.stdout))
    hub.save_json(hub.STATE / 'quota.json', snapshot)
    return snapshot


class QuotaCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.refreshing = False
        self.attempted_at = 0
        try:
            self.value = json.loads((hub.STATE / 'quota.json').read_text())
        except (OSError, ValueError):
            self.value = {'available': False, 'groups': [], 'checked_at': None, 'error': None}

    def get(self, force=False):
        with self.lock:
            if not self.refreshing and (force or time.monotonic() - self.attempted_at > 300):
                self.refreshing = True
                self.attempted_at = time.monotonic()
                threading.Thread(target=self.update, daemon=True).start()
            return dict(self.value, refreshing=self.refreshing,
                        stale=bool(self.value.get('error')) or not self.value.get('checked_at') or not 0 <= time.time() - self.value['checked_at'] <= 360)

    def update(self):
        try:
            value = refresh()
            with self.lock:
                self.value = value
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            with self.lock:
                self.value = dict(self.value, error=str(error), stale=True)
        finally:
            with self.lock:
                self.refreshing = False
