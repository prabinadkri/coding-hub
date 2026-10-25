"""Observed outcomes only; no background model calls or synthetic rankings."""
import json
import sqlite3
import time
import hub


def connect():
    path = hub.private_dir(hub.STATE) / 'model-metrics.sqlite3'
    db = sqlite3.connect(path, timeout=10)
    path.chmod(0o600)
    db.execute("CREATE TABLE IF NOT EXISTS attempts(model TEXT, backend TEXT, finished REAL, seconds REAL, success INTEGER, verified TEXT, task TEXT, role TEXT DEFAULT 'worker')")
    if 'role' not in {r[1] for r in db.execute('PRAGMA table_info(attempts)')}:
        db.execute("ALTER TABLE attempts ADD COLUMN role TEXT DEFAULT 'worker'")
    return db


def record(backend, model, seconds, success, task='', role='worker'):
    with connect() as db:
        db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)', (model, backend, time.time(), seconds, int(success), None, task, role))
    db.close()


def verified(task, status):
    with connect() as db:
        # Credit only the final successful attempt, not every fallback/manager call.
        db.execute("UPDATE attempts SET verified=? WHERE rowid=(SELECT max(rowid) FROM attempts WHERE task=? AND success=1 AND role='worker')", (status, task))
    db.close()


def snapshot():
    import free_quota
    import local_resource
    with connect() as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('''SELECT backend,model,count(*) AS samples,
            sum(success) AS successful,round(avg(seconds),2) AS average_seconds,
            max(CASE WHEN success=1 THEN finished END) AS last_success,
            sum(CASE WHEN verified='passed' THEN 1 ELSE 0 END) AS checks_passed,
            sum(CASE WHEN verified='failed' THEN 1 ELSE 0 END) AS checks_failed
            FROM attempts GROUP BY backend,model''')]
    db.close()
    try: catalog = json.loads((hub.STATE / 'free-models.json').read_text())
    except (OSError, ValueError): catalog = {}
    return {'observations': rows, 'catalog': catalog, 'cooldowns': free_quota.routing_cooldowns(),
            'local_resource': local_resource.status(),
            'note': 'Observed attempts, not a benchmark. Latency depends on task size and server load. Prices are rechecked before free cloud execution.'}
