"""Private, synthetic dashboard for browser regression tests. Never calls a model."""
import contextlib
import io
import json
import os
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
import browser_checks


class Cache:
    def __init__(self, value): self.value = value
    def get(self, *args): return self.value


with tempfile.TemporaryDirectory(prefix='hub-web-test-') as temporary:
    root=Path(temporary).resolve();hub.STATE=root/'state';project=root/'project';project.mkdir()
    studio=root/'studio';(studio/'docs').mkdir(parents=True)
    (studio/'circuitctl.py').write_text('')
    (studio/'launch.py').write_text('import sys;from pathlib import Path;Path(__file__).with_name("opened").write_text(sys.argv[-1])')
    (studio/'docs/AUTOMATION.md').write_text('Circuit test API reference')
    os.environ['CIRCUIT_STUDIO_HOME']=str(studio);os.environ['XDG_DATA_HOME']=str(root/'data')
    (project/'source.py').write_text('answer = 1\n')
    html='<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>Browser fixture</title><h1>Ready to test</h1><a href="/about.html">About</a>'
    (project/'index.html').write_text(html)
    (project/'about.html').write_text(html.replace('Ready to test','About this fixture'))
    memory=ProjectMemory(project);conversation=memory.conversation(goal='Synthetic preview')
    folder=hub.private_dir(hub.STATE/'tasks'/'preview-task')
    with contextlib.redirect_stdout(io.StringIO()):
        with workspace_changes.session(project, folder, True, True) as working:
            (working/'source.py').write_text('answer = 2\n')
    memory.record(conversation,folder.name,'Improve the answer','Preview ready.','completed')
    import reply_files
    (project/'frontend'/'cypress'/'screenshots').mkdir(parents=True)
    (project/'frontend'/'cypress'/'screenshots'/'passed.png').write_bytes((hub.ROOT/'docs'/'desktop-workspace.png').read_bytes())
    (project/'frontend'/'.cypress-e2e.log').write_text('All 12 browser checks passed\n')
    (project/'lines.py').write_text('\n'.join('line '+str(i) for i in range(1,101)))
    (project/'backend').mkdir()
    for name in ['frontend','backend']:(project/name/'worker.log').write_text(name+' worker finished')
    memory.record(conversation,'files-task','Review test artifacts',
        'Next.js served `/auth/login` and `/`. Screenshots/logs are in cypress/screenshots/ and `.cypress-e2e.log`. See lines.py:42 and missing.log. Also worker.log. Other names: https://example.com/worker.log and worker.log.bak.', 'completed')
    # Exercise authenticated reveal routing without opening the host file manager in CI.
    reply_files.reveal=lambda project,reference,task=None: {'opened':str(reply_files.resolve(project,reference,task)[0]),'selected':True}
    manager=dashboard.TaskManager(command_factory=lambda t: dashboard.TaskManager.command(t) if browser_checks.chat_request(t['prompt']) is not None else [sys.executable,'-u','-c','import time;print("Synthetic task");time.sleep(.4)'])
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
