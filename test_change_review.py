import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import hub
import change_review as review
from context_engine import ProjectMemory


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.state = patch.object(hub, 'STATE', self.root / 'state')
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def test_compares_task_changes_preserving_existing_edits_and_missing_newlines(self):
        subprocess.run(['git','init','-q',str(self.project)],check=True)
        for name, text in [('old.py','already edited\n'),('same.py','existing user edit\n'),('delete.py','remove me\n')]:
            (self.project/name).write_text(text)
        subprocess.run(['git','-C',str(self.project),'add','.'],check=True)
        before=review.snapshot(self.project)
        (self.project/'old.py').write_text('task edit')
        (self.project/'delete.py').unlink()
        (self.project/'new.py').write_text('new\n')
        after=review.snapshot(self.project)
        result=review.compare(before,after)
        files={f['path']:f for f in result['files']}
        self.assertEqual(set(files),{'old.py','delete.py','new.py'})
        self.assertEqual(files['delete.py']['status'],'deleted')
        self.assertIn('-already edited\n+task edit\n',files['old.py']['diff'])
        self.assertEqual((files['old.py']['added'],files['old.py']['removed']),(1,1))

    def test_ignored_binary_large_and_symlink_files_are_excluded(self):
        (self.project/'.env').write_text('secret')
        (self.project/'credentials.json').write_text('secret')
        (self.project/'binary.py').write_bytes(b'\0data')
        (self.project/'large.py').write_text('x'*(review.MAX_FILE+1))
        outside=self.root/'outside.py';outside.write_text('private')
        (self.project/'link.py').symlink_to(outside)
        snapshot=review.snapshot(self.project)
        self.assertEqual(snapshot['files'],{})
        self.assertTrue(snapshot['limited'])
        before={'files':{'large.py':'previous content'},'paths':{'large.py'},'limited':False,'covered':1}
        self.assertEqual(review.compare(before,snapshot)['files'],[])

    def test_task_capture_review_and_project_isolation(self):
        (self.project/'main.py').write_text('before\n')
        def run(args, project, env, log):
            (project/'main.py').write_text('after\n')
            log.write_text(json.dumps({'type':'text','part':{'text':'Updated main.py.'}})+'\n')
            return 0,'Updated main.py.',False
        with patch.object(hub,'route_options',return_value=[('local','test')]), patch.object(hub,'command',return_value=['test']), patch.object(hub,'clean_environment',return_value={}), patch.object(hub,'run_process',side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub.run_task(self.project,'Update main.py',backend='local',apply=True),0)
        memory=ProjectMemory(self.project)
        with memory.connect() as db: row=dict(db.execute('SELECT * FROM turns').fetchone())
        messages=memory.messages(row['conversation'])
        self.assertTrue(messages['turns'][0]['has_review'])
        result=review.load(self.project,row['task'])
        self.assertIn('+after',result['files'][0]['diff'])
        other=self.root/'other';other.mkdir()
        with self.assertRaises(ValueError): review.load(other,row['task'])
        with self.assertRaises(ValueError): review.load(self.project,'../../outside')
        self.assertEqual((self.project/'main.py').read_text(),'after\n')

    def test_large_reviews_report_omissions(self):
        before={'files':{},'paths':set(),'limited':False,'covered':0}
        after={'files':{f'f{i}.py':'new\n' for i in range(70)},'paths':set(),'limited':False,'covered':70}
        result=review.compare(before,after)
        self.assertEqual(len(result['files']),64)
        self.assertEqual(result['omitted_files'],6)
        self.assertTrue(result['limited'])

    def test_provider_errors_are_readable_without_http_headers(self):
        event={'type':'error','error':{'name':'APIError','data':{'message':"OpenCode's free tier can only be used from within OpenCode",'responseHeaders':{'test':'private details'}}}}
        text,error=hub.event_text(json.dumps(event))
        self.assertTrue(error)
        self.assertIn('Choose Antigravity or Local',text)
        self.assertNotIn('responseHeaders',text)
        self.assertIn('Open Accounts',hub.provider_error('Failed to authenticate: OAuth session expired'))
