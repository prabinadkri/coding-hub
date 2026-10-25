"""Private, synthetic dashboard for browser regression tests. Never calls a model."""
import contextlib
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hub
import dashboard
from context_engine import ProjectMemory
import workspace_changes


class Cache:
    def __init__(self, value): self.value = value
    def get(self, *args): return self.value


with tempfile.TemporaryDirectory(prefix='hub-web-test-') as temporary:
    root=Path(temporary);hub.STATE=root/'state';project=root/'project';project.mkdir()
    (project/'source.py').write_text('answer = 1\n')
    memory=ProjectMemory(project);conversation=memory.conversation(goal='Synthetic preview')
    folder=hub.private_dir(hub.STATE/'tasks'/'preview-task')
    with contextlib.redirect_stdout(io.StringIO()):
        with workspace_changes.session(project, folder, True, True) as working:
            (working/'source.py').write_text('answer = 2\n')
    memory.record(conversation,folder.name,'Improve the answer','Preview ready.','completed')
    manager=dashboard.TaskManager(command_factory=lambda t:[sys.executable,'-u','-c','import time;print("Synthetic task");time.sleep(.4)'])
    status=Cache({'checking':False,'programs':{},'models':[hub.LOCAL_AGENT_MODEL],'free_models':['beam-free'],
                  'local_ready':True,'local_model':hub.LOCAL_AGENT_MODEL,'gpu':None,'ram':None,'loaded':[],
                  'accounts':[],'provider_models':{},'home':str(root),'default_project':str(project)})
    server=dashboard.DashboardServer(('127.0.0.1',0),manager=manager,status=status,token='fixture-token')
    server.quota=Cache({});server.free_quota=Cache({})
    signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=server.shutdown,daemon=True).start())
    print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/#fixture-token','project':str(project),'conversation':conversation}),flush=True)
    try: server.serve_forever()
    finally:
        manager.close();server.accounts.close();server.server_close()
