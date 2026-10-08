"""Search saved run metadata on demand, without loading logs or provider data."""
import json

ACTIVE = {'queued', 'running', 'stopping'}
ATTENTION = {'failed', 'needs_review', 'incomplete', 'timeout', 'interrupted'}


class HistoryIndex:
    def __init__(self, directory):
        self.directory = directory
        self.cache = {}

    def page(self, query='', status='', route='', offset=0, limit=20):
        if len(query) > 500 or status not in ('', 'active', 'completed', 'attention', 'canceled'):
            raise ValueError('Invalid history filter.')
        if route not in ('', 'auto', 'smart', 'antigravity', 'free', 'local', 'claude', 'openai'):
            raise ValueError('Invalid history route.')
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError('Invalid history page.')
        query = query.strip()
        paths = set(self.directory.glob('*.json'))
        for path in self.cache.keys() - paths:
            del self.cache[path]
        rows = []
        for path in paths:
            try:
                stat = path.stat()
                if path.is_symlink() or stat.st_size > 256 * 1024:
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self.cache.get(path)
                if not cached or cached[0] != stamp:
                    item = json.loads(path.read_text())
                    if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in ('id', 'prompt', 'project', 'backend', 'status')) or not isinstance(item.get('created_at'), (int, float)) or item['id'] != path.stem:
                        continue
                    self.cache[path] = (stamp, item)
                item = self.cache[path][1]
                if route and item['backend'] != route:
                    continue
                states = ACTIVE if status == 'active' else ATTENTION if status == 'attention' else {status}
                if status and item['status'] not in states:
                    continue
                if query.casefold() not in (' '.join(item[k] for k in ('prompt', 'project', 'backend'))).casefold():
                    continue
                # Logs, diagnostics, and account payloads never belong in a listing.
                rows.append({k: item[k] for k in ('id', 'prompt', 'project', 'backend', 'status', 'created_at', 'started_at', 'ended_at', 'conversation') if k in item})
            except (OSError, ValueError, TypeError):
                continue
        rows.sort(key=lambda t: (t['created_at'], t['id']), reverse=True)
        total = len(rows)
        offset = min(offset, max(0, (total - 1) // limit) * limit)
        return {'tasks': rows[offset:offset + limit], 'total': total, 'offset': offset, 'limit': limit,
                'has_more': offset + limit < total}
