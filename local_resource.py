"""Cooperative per-user Ollama lease shared by CLI, dashboard and other clients."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time


def directory():
    path = Path(os.environ.get('CODING_HUB_RESOURCE_DIR', str(Path.home() / '.local/state/local-ai-resource')))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def status():
    try:
        import fcntl
        with (directory() / 'ollama.lock').open('a+') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.seek(0)
                try: return dict(json.load(stream), busy=True)
                except ValueError: return {'busy': True}
            return {'busy': False}
    except (ImportError, OSError):
        return {'busy': None, 'note': 'Shared local scheduling unavailable on this platform.'}


@contextmanager
def lease(owner='Coding Hub', cancel_event=None, timeout=1200):
    import fcntl
    path = directory() / 'ollama.lock'
    with path.open('a+') as stream:
        path.chmod(0o600)
        start, announced = time.monotonic(), False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Canceled while waiting for the local model.')
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    print('[Local queue] Waiting for the shared Ollama slot.', flush=True)
                    announced = True
                if time.monotonic() - start >= timeout:
                    raise TimeoutError('Timed out waiting for the shared Ollama slot.')
                time.sleep(.2)
        stream.seek(0); stream.truncate()
        json.dump({'owner': owner, 'pid': os.getpid(), 'started_at': time.time()}, stream)
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
