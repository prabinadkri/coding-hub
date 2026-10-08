import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import task_lock

class TaskLockTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.project=self.root/'project';self.project.mkdir()
        self.state=self.root/'state';(self.state/'locks').mkdir(parents=True)
        self.path=self.state/'locks'/(hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()+'.lock')
    def tearDown(self):self.temp.cleanup()

    def test_recovers_dead_legacy_owner_and_can_reuse_workspace(self):
        self.path.write_text(json.dumps({'pid':12345678,'project':str(self.project)}))
        with patch.object(task_lock,'owner_alive',return_value=False):
            with task_lock.workspace_lock(self.state,self.project): pass
        self.assertEqual(json.loads(self.path.read_text())['format'],2)
        self.assertIsNone(json.loads(self.path.read_text())['pid'])
        with task_lock.workspace_lock(self.state,self.project): pass

    def test_respects_live_legacy_owner_without_changing_its_lock(self):
        original=json.dumps({'pid':os.getpid(),'project':str(self.project)})
        self.path.write_text(original)
        with self.assertRaisesRegex(RuntimeError,'still using'):
            with task_lock.workspace_lock(self.state,self.project): self.fail('Acquired live legacy lock')
        self.assertEqual(self.path.read_text(),original)

    def test_second_process_cannot_enter_until_crashed_owner_exits(self):
        script='import task_lock,sys,time\nwith task_lock.workspace_lock(sys.argv[1],sys.argv[2]):\n print("locked",flush=True)\n time.sleep(30)'
        process=subprocess.Popen([sys.executable,'-c',script,str(self.state),str(self.project)],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(RuntimeError,'already running'):
                with task_lock.workspace_lock(self.state,self.project): self.fail('Concurrent acquisition')
            process.kill();process.wait(timeout=3)
            with task_lock.workspace_lock(self.state,self.project): pass
        finally:
            if process.poll() is None: process.kill();process.wait()
            process.stdout.close()
