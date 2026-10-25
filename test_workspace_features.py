import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import hub
import project_checks
import workspace_changes
from context_engine import ProjectMemory


class WorkspaceFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'; self.project.mkdir()
        self.state = patch.object(hub, 'STATE', self.root / 'state'); self.state.start()
        self.output = contextlib.redirect_stdout(io.StringIO()); self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None,None,None); self.state.stop(); self.temp.cleanup()

    def checkpoint(self, preview=True):
        memory = ProjectMemory(self.project)
        conversation = memory.conversation(goal='Change source')
        folder = hub.private_dir(hub.STATE / 'tasks' / 'test-task')
        memory.record(conversation, folder.name, 'Change source', 'Done', 'completed')
        return folder

    def test_preview_selected_apply_and_undo_preserve_preexisting_edits(self):
        (self.project / 'main.py').write_text('user_uncommitted = 1\n')
        folder = self.checkpoint()
        with workspace_changes.session(self.project, folder, True, True) as working:
            (working / 'main.py').write_text('user_uncommitted = 2\n')
            (working / 'new.py').write_text('created = True\n')
            self.assertEqual((self.project / 'main.py').read_text(), 'user_uncommitted = 1\n')
        review = workspace_changes.change(self.project, folder.name, 'apply', ['main.py'])
        self.assertEqual(review['applied'], ['main.py'])
        self.assertFalse((self.project / 'new.py').exists())
        workspace_changes.change(self.project, folder.name, 'undo', ['main.py'])
        self.assertEqual((self.project / 'main.py').read_text(), 'user_uncommitted = 1\n')

    def test_conflicting_apply_is_all_or_nothing_and_undo_refuses_later_edits(self):
        (self.project / 'a.py').write_text('a=1'); (self.project / 'b.py').write_text('b=1')
        folder = self.checkpoint()
        with workspace_changes.session(self.project, folder, True, True) as working:
            (working / 'a.py').write_text('a=2'); (working / 'b.py').write_text('b=2')
        (self.project / 'b.py').write_text('b=user_edit')
        with self.assertRaises(ValueError): workspace_changes.change(self.project, folder.name, 'apply', ['a.py','b.py'])
        self.assertEqual((self.project / 'a.py').read_text(), 'a=1')
        workspace_changes.change(self.project, folder.name, 'apply', ['a.py'])
        (self.project / 'a.py').write_text('a=later_edit')
        with self.assertRaises(ValueError): workspace_changes.change(self.project, folder.name, 'undo', ['a.py'])
        self.assertEqual((self.project / 'a.py').read_text(), 'a=later_edit')

    def test_symlink_destination_and_cross_project_recovery_rejected(self):
        folder = self.checkpoint()
        with workspace_changes.session(self.project, folder, True, True) as working:
            (working / 'new.py').write_text('new=True')
        outside = self.root / 'outside.py'; outside.write_text('keep')
        (self.project / 'new.py').symlink_to(outside)
        with self.assertRaises(ValueError): workspace_changes.change(self.project, folder.name, 'apply', ['new.py'])
        self.assertEqual(outside.read_text(), 'keep')
        other=self.root/'other';other.mkdir()
        with self.assertRaises(ValueError): workspace_changes.change(other, folder.name, 'apply', ['new.py'])

    def test_direct_mode_recovery_and_added_deleted_binary_files(self):
        (self.project / 'old.py').write_text('old=True')
        (self.project / 'image.png').write_bytes(b'\x00old')
        folder=self.checkpoint()
        with workspace_changes.session(self.project, folder, True) as working:
            (working/'old.py').unlink();(working/'new.py').write_text('new=True')
            (working/'image.png').write_bytes(b'\x00new')
        workspace_changes.change(self.project, folder.name, 'undo', ['old.py','new.py','image.png'])
        self.assertEqual((self.project/'old.py').read_text(),'old=True')
        self.assertFalse((self.project/'new.py').exists())
        self.assertEqual((self.project/'image.png').read_bytes(),b'\x00old')

    def test_no_commands_is_not_verified_and_shell_operators_are_literal(self):
        folder=hub.private_dir(self.root/'checks')
        self.assertEqual(project_checks.run(self.project, project_checks.DEFAULT, folder)['status'],'not_verified')
        code="import sys; assert sys.argv[1:] == [';', 'touch', 'BAD']"
        config=dict(project_checks.DEFAULT, commands=[shlex.join([sys.executable,'-c',code,';','touch','BAD'])])
        self.assertEqual(project_checks.run(self.project,config,folder)['status'],'passed')
        self.assertFalse((self.project/'BAD').exists())

    def test_failed_check_exit_code_captured_and_repairs_are_bounded_free(self):
        folder=hub.private_dir(self.root/'checks')
        config=dict(project_checks.DEFAULT,commands=[shlex.join([sys.executable,'-c','raise SystemExit(7)'])],repairs=2)
        result=project_checks.run(self.project,config,folder)
        self.assertEqual((result['status'],result['checks'][0]['exit_code']),('failed',7))
        with patch.object(project_checks,'run',return_value=result),patch.object(hub,'route_options',return_value=[('free','beam-free')]) as routes,patch.object(hub,'run_process',return_value=(0,'done',False)) as process:
            result=project_checks.finish(self.project,'fix',config,folder,True)
        self.assertEqual(len(result['repair_attempts']),2)
        self.assertEqual(process.call_count,2)
        self.assertEqual(routes.call_args.args[0],'free-auto')
        self.assertEqual(result['status'],'failed')

    def test_failing_independent_checks_change_completed_task_to_needs_review(self):
        config=dict(project_checks.DEFAULT,commands=[shlex.join([sys.executable,'-c','raise SystemExit(2)'])])
        project_checks.profile(self.project,config)
        real=hub.run_process
        def run(args,cwd,env,log,**kwargs):
            if args[0]=='fake-agent':
                (Path(cwd)/'result.py').write_text('result=True')
                return 0,'Implemented successfully',False
            return real(args,cwd,env,log,**kwargs)
        with patch.object(hub,'route_options',return_value=[('free','beam-free')]),patch.object(hub,'command',return_value=['fake-agent']),patch.object(hub,'run_process',side_effect=run):
            code=hub.run_task(self.project,'Implement',apply=True,preview=True)
        self.assertEqual(code,3)
        self.assertFalse((self.project/'result.py').exists())
        task=next((hub.STATE/'tasks').glob('*/task.json'))
        record=json.loads(task.read_text())
        self.assertEqual(record['verification']['status'],'failed')
        self.assertEqual(record['verification']['baseline']['status'],'failed')

    def test_python_symbol_definitions_imports_calls_and_incremental_removal(self):
        source=self.project/'service.py'
        source.write_text('from pathlib import Path\nclass Service:\n    def refresh(self, count: int):\n        return Path("x")\n\ndef test_refresh():\n    return Service().refresh(1)\n')
        memory=ProjectMemory(self.project);memory.index()
        hits=memory.symbols('refresh')
        self.assertTrue(any(r['name']=='Service.refresh' and r['line']==3 for r in hits))
        self.assertTrue(any(r['kind']=='call' for r in hits))
        self.assertTrue(any(r['kind']=='import' for r in memory.symbols('pathlib')))
        context=memory.context(memory.conversation(), 'refresh')
        self.assertIn('Python symbol map',context)
        source.unlink();memory.index();self.assertEqual(memory.symbols('refresh'),[])

    def test_free_manager_has_no_tools_rechecks_prices_and_never_uses_subscription(self):
        import smart_route
        folder=hub.private_dir(self.root/'manager')
        response=json.dumps({'steps':['Inspect source'],'checks':['Run tests']})
        with patch.object(hub,'refresh_free_models',return_value=['beam-free']),patch('free_quota.routing_cooldowns',return_value={}),patch.object(hub,'executable',return_value='fake-agent'),patch.object(hub,'run_process',return_value=(0,response,False)) as run:
            result=smart_route.free_manager(self.project,'plan','fast',smart_route.PLAN_SCHEMA,folder/'plan.log')
        self.assertTrue(result['ok'])
        args=run.call_args.args
        self.assertIn('opencode/beam-free',args[0])
        self.assertEqual(json.loads(args[2]['OPENCODE_CONFIG_CONTENT'])['permission'],{'*':'deny'})
        self.assertEqual(run.call_args.kwargs['timeout'],120)
        with patch.object(hub,'refresh_free_models',return_value=[]),patch.object(hub,'run_process') as run:
            self.assertFalse(smart_route.free_manager(self.project,'plan','fast',smart_route.PLAN_SCHEMA,folder/'denied.log',model='paid')['ok'])
            run.assert_not_called()

    def test_browser_configuration_rejects_remote_and_arbitrary_scripts(self):
        import browser_checks
        for value in ({'url':'https://example.com','steps':[]},{'url':'http://localhost:3000','steps':[{'action':'eval','value':'steal()'}]}):
            with self.assertRaises(ValueError): browser_checks.validate(value)
        self.assertEqual(browser_checks.validate({'url':'http://127.0.0.1:3000','steps':[{'action':'text','selector':'h1','value':'Ready'}]})['steps'][0]['action'],'text')

    def test_silent_provider_retry_is_stopped_and_shared_backoff_recorded(self):
        import free_quota
        folder=hub.private_dir(self.root/'stream')
        code="import time; print('timestamp=test level=ERROR message=stream_error error=Free usage limit reached',flush=True); time.sleep(30)"
        start=time.monotonic()
        result=hub.run_process([sys.executable,'-u','-c',code,'--print-logs'],self.project,
            hub.clean_environment('free','beam-free'),folder/'stream.log',quiet=True)
        self.assertLess(time.monotonic()-start,5)
        self.assertEqual(hub.classify_failure(*result),'quota')
        value={'available':False,'models':[{'id':name,'observation':None,'last_success_at':0} for name in hub.FREE_MODELS]}
        with patch.object(free_quota,'_snapshot',return_value=value):
            cooling=free_quota.routing_cooldowns()
        self.assertEqual(set(cooling),set(hub.FREE_MODELS))
        self.assertTrue(all(item['shared'] and 'reset unknown' in item['basis'] for item in cooling.values()))

    def test_metrics_verify_worker_not_manager_and_preserve_unknown(self):
        import model_metrics
        model_metrics.record('free','worker-model',1.5,True,'task')
        model_metrics.record('free','manager-model',2,True,'task','manager')
        model_metrics.verified('task','passed')
        with patch('free_quota.routing_cooldowns',return_value={}):
            rows={r['model']:r for r in model_metrics.snapshot()['observations']}
        self.assertEqual(rows['worker-model']['checks_passed'],1)
        self.assertEqual(rows['manager-model']['checks_passed'],0)

    @unittest.skipIf(os.name=='nt','POSIX local lease')
    def test_interrupt_while_waiting_for_local_slot_becomes_canceled_task(self):
        with patch('local_resource.lease', side_effect=KeyboardInterrupt), patch.object(hub, '_run_process') as child:
            result=hub.run_process(['unused'],self.project,hub.clean_environment('local',hub.LOCAL_AGENT_MODEL),self.root/'log')
        self.assertEqual(result[0],130)
        child.assert_not_called()

    @unittest.skipIf(os.name=='nt','POSIX local lease')
    def test_shared_local_lease_serializes_processes_and_releases_after_crash(self):
        import local_resource
        with patch.dict(os.environ,{'CODING_HUB_RESOURCE_DIR':str(self.root/'resource')}):
            child=subprocess.Popen([sys.executable,'-u','-c',"from local_resource import lease; import time\nwith lease('test client'):\n print('locked',flush=True)\n time.sleep(30)"],cwd=hub.ROOT,stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'locked')
                self.assertTrue(local_resource.status()['busy'])
                canceled=threading.Event();canceled.set()
                with self.assertRaises(InterruptedError):
                    with local_resource.lease(cancel_event=canceled): pass
            finally:
                child.kill();child.wait();child.stdout.close()
            with local_resource.lease('test recovery'):
                self.assertTrue(local_resource.status()['busy'])
            self.assertFalse(local_resource.status()['busy'])


if __name__=='__main__': unittest.main()
