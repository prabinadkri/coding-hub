"""Process-lifetime workspace locks, including recovery of older lock files."""
from __future__ import annotations
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import time


def owner_alive(pid):
    if not isinstance(pid,int) or isinstance(pid,bool) or pid <= 0: return False
    # Windows does not support the POSIX signal-zero liveness probe. Keep old
    # owners protected there; modern OS locks still release automatically.
    if os.name == 'nt': return True
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    except PermissionError: return True
    if os.name != 'nt':
        try:
            if (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[0]=='Z': return False
        except (OSError,IndexError): pass
    return True


def write_owner(fd, data):
    raw=json.dumps(data).encode()
    os.lseek(fd,0,os.SEEK_SET)
    os.write(fd,raw)
    os.ftruncate(fd,len(raw))


def acquire(fd):
    if os.name == 'nt':
        import msvcrt
        if os.fstat(fd).st_size == 0: os.write(fd,b' ')
        os.lseek(fd,0,os.SEEK_SET)
        msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)


def release(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd,0,os.SEEK_SET)
        msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
    else:
        import fcntl
        fcntl.flock(fd,fcntl.LOCK_UN)


@contextmanager
def workspace_lock(state, project):
    project=Path(project).expanduser().resolve()
    directory=Path(state)/'locks';directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
    path=directory/(hashlib.sha256(str(project).encode()).hexdigest()+'.lock')
    flags=os.O_RDWR|getattr(os,'O_NOFOLLOW',0)
    try:
        fd=os.open(path,flags|os.O_CREAT|os.O_EXCL,0o600);created=True
    except FileExistsError:
        fd=os.open(path,flags);created=False
    held=False
    try:
        try: acquire(fd);held=True
        except OSError as error:
            if error.errno not in (errno.EACCES,errno.EAGAIN): raise
            raise RuntimeError('A task is already running for '+str(project)+'. Wait for it to finish, stop that task, or use a different workspace.') from None
        os.lseek(fd,0,os.SEEK_SET)
        try: previous=json.loads(os.read(fd,16384))
        except (ValueError,UnicodeError): previous={}
        if not isinstance(previous,dict): previous={}
        # Older releases used file existence as the lock. Respect a live legacy
        # owner, but recover abandoned files once that process has exited.
        if not created and previous.get('format') != 2:
            if owner_alive(previous.get('pid')):
                raise RuntimeError('An existing task is still using '+str(project)+' (PID '+str(previous['pid'])+'). Wait for it to finish or stop it in the terminal where it started.')
            if not previous and time.time()-os.fstat(fd).st_mtime < 5:
                raise RuntimeError('A task is starting for this workspace. Try again in a moment.')
        write_owner(fd,{'format':2,'pid':os.getpid(),'project':str(project)})
        try: yield
        finally: write_owner(fd,{'format':2,'pid':None,'project':str(project)})
    finally:
        if held: release(fd)
        os.close(fd)
