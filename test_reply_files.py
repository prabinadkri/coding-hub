import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import hub
import reply_files as files
from context_engine import ProjectMemory
from message_format import inline_markup

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8ioAAAAASUVORK5CYII=')

class ReplyFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root/'project';self.project.mkdir()
        self.state = patch.object(hub,'STATE',self.root/'state');self.state.start()
        self.memory = ProjectMemory(self.project)
        self.chat = self.memory.conversation(goal='Test browser')
        self.memory.record(self.chat,'files-task','Test browser','Screenshots/logs are in cypress/screenshots/ and .cypress-e2e.log.','completed')
        (self.project/'cypress'/'screenshots').mkdir(parents=True)
        (self.project/'cypress'/'screenshots'/'passed.png').write_bytes(PNG)
        (self.project/'.cypress-e2e.log').write_text('All 12 browser checks passed\n')

    def tearDown(self):
        self.state.stop();self.temp.cleanup()

    def test_existing_saved_replies_gain_references_without_file_scanning(self):
        with patch.object(files,'resolve',side_effect=AssertionError('No file reads during rendering')):
            refs=self.memory.messages(self.chat)['turns'][0]['file_references']
        self.assertEqual([r['path'] for r in refs],['cypress/screenshots/','.cypress-e2e.log'])

    def test_path_parsing_preserves_labels_spaces_locations_and_ignores_commands(self):
        text='See (/tmp/results/) and `my report.log`, [image](<cypress/my screenshot.png>), src/app.py:42:3, x.py#L12-L18.\n**.cypress-e2e.log**\nhttps://x.com/test.py\n```py\nsecret.py\n```\n`npm run test`'
        self.assertEqual([r['path'] for r in files.references(text)],['/tmp/results/','my report.log','cypress/my screenshot.png','src/app.py:42:3','x.py#L12-L18','.cypress-e2e.log'])
        self.assertEqual(files.references('no.pytest https://example.com/file.py [link](https://example.com/a.py)'),[])
        self.assertEqual(len(files.references('a.py '*100)),1)
        self.assertEqual(len(files.references(' '.join(f'a{i}.py' for i in range(80)))),40)

    def test_long_dotted_output_is_bounded_and_home_paths_keep_their_prefix(self):
        self.assertEqual(files.references('a.'*30000),[])
        self.assertEqual(files.references('a/'*30000),[])
        self.assertEqual([r['path'] for r in files.references('~/logs/run.log and /tmp and ./test.py')],['~/logs/run.log','/tmp','./test.py'])

    def test_native_markup_links_are_escaped_and_do_not_change_code(self):
        text='**See `my report.log`** and [<script>](x.py)'
        rendered=inline_markup(text,files.references(text))
        self.assertIn('<b>See <a href="codehub-file:0"><tt>my report.log</tt></a></b>',rendered)
        self.assertIn('&lt;script&gt;</a>',rendered)
        self.assertNotIn('<script>',rendered)

    def test_browse_folder_image_log_and_requested_line(self):
        folder=files.describe(self.project,'cypress/screenshots/','files-task')
        self.assertEqual(folder['entries'][0]['name'],'passed.png')
        image=files.describe(self.project,folder['entries'][0]['path'],'files-task')
        self.assertEqual(base64.b64decode(image['image'].split(',')[1]),PNG)
        self.assertEqual(files.download(self.project,'cypress/screenshots/passed.png'),PNG)
        self.assertIn('12 browser checks passed',files.describe(self.project,'.cypress-e2e.log')['text'])
        (self.project/'app.py').write_text('\n'.join('line '+str(i) for i in range(1,101)))
        value=files.describe(self.project,'app.py:42:3');self.assertEqual(value['first_line'],27)
        self.assertEqual(value['text'].splitlines()[15],'line 42')
        self.assertEqual(files.describe(self.project,(self.project/'app.py').as_uri())['kind'],'text')

    def test_logs_tail_and_large_text_and_directory_are_bounded(self):
        (self.project/'large.log').write_text('OLD\n'*40000+'LATEST\n')
        value=files.describe(self.project,'large.log')
        self.assertTrue(value['tail']);self.assertTrue(value['truncated'])
        self.assertTrue(value['text'].endswith('LATEST'));self.assertLessEqual(len(value['text'].splitlines()),600)
        (self.project/'large.txt').write_text('x'*200000)
        self.assertEqual(len(files.describe(self.project,'large.txt')['text']),files.TEXT_LIMIT)
        folder=self.project/'many';folder.mkdir()
        for i in range(101):(folder/f'{i:03}.txt').touch()
        self.assertEqual(len(files.describe(self.project,'many/')['entries']),100)
        self.assertEqual(len(files.describe(self.project,'many/',offset=100)['entries']),1)
        self.assertIsNone(files.describe(self.project,'.')['parent'])

    def test_refuses_foreign_projects_secrets_symlinks_special_files_and_missing_files(self):
        outside=self.root/'outside.txt';outside.write_text('private')
        (self.project/'alias.txt').symlink_to(outside)
        (self.project/'.env').write_text('private')
        os.mkfifo(self.project/'pipe')
        for path in ['../outside.txt',str(outside),'alias.txt','.env','missing.log','pipe','https://x.com/a.py','file:///tmp/%00x','file://remote/x']:
            with self.subTest(path=path), self.assertRaises((ValueError,OSError)):
                files.describe(self.project,path)
        other=self.root/'other';other.mkdir()
        with self.assertRaisesRegex(ValueError,'does not belong'):files.describe(other,'.','files-task')
        with self.assertRaises(ValueError):files.download(self.project,'.')
        (self.project/'large.bin').write_bytes(b'\0')
        self.assertEqual(files.describe(self.project,'large.bin')['kind'],'binary')
        with (self.project/'huge.bin').open('wb') as stream:stream.truncate(files.DOWNLOAD_LIMIT+1)
        with self.assertRaises(ValueError):files.download(self.project,'huge.bin')

    def test_preview_relative_references_use_preview_not_original(self):
        import workspace_changes
        folder=hub.private_dir(hub.STATE/'tasks'/'files-task')
        with contextlib.redirect_stdout(io.StringIO()),workspace_changes.session(self.project,folder,True,True) as working:
            (working/'new.log').write_text('Preview result')
        (self.project/'new.log').write_text('Original result')
        self.assertEqual(files.describe(self.project,'new.log','files-task')['text'],'Preview result')
        self.assertEqual(files.describe(self.project,str(self.project/'new.log'),'files-task')['text'],'Original result')

    def test_task_browser_artifacts_are_scoped_to_that_turn(self):
        folder=hub.private_dir(hub.STATE/'tasks'/'files-task');identifier='20261025-120000-1234abcd'
        artifact=self.memory.directory/'browser-runs'/identifier;artifact.mkdir(parents=True)
        (artifact/'desktop.png').write_bytes(PNG)
        (folder/'task.json').write_text(json.dumps({'browser':{'id':identifier}}))
        self.assertEqual(files.describe(self.project,str(artifact/'desktop.png'),'files-task')['kind'],'image')
        with self.assertRaises(ValueError):files.describe(self.project,str(artifact/'desktop.png'))

    def test_component_open_refuses_symlink_swap_after_resolution(self):
        path=self.project/'cypress'/'screenshots'/'passed.png'
        validated=files.resolve(self.project,str(path))
        path.unlink();path.symlink_to(self.root/'private')
        with patch.object(files,'resolve',return_value=validated),self.assertRaises(OSError):
            files.describe(self.project,str(path))

    def test_reveal_selects_file_without_shell_and_falls_back_to_parent(self):
        path=self.project/"report's special.log";path.write_text('ok')
        with patch.object(files.shutil,'which',return_value='/usr/bin/tool'),patch.object(files.subprocess,'run') as run:
            run.return_value.returncode=0
            value=files.reveal(self.project,path.name)
            self.assertTrue(value['selected']);self.assertEqual(run.call_args.args[0][-2],json.dumps([path.as_uri()]))
            self.assertNotIn('shell',run.call_args.kwargs)
            run.side_effect=[subprocess.TimeoutExpired('gdbus',5),subprocess.CompletedProcess([],0)]
            value=files.reveal(self.project,path.name)
            self.assertEqual(value['opened'],str(self.project));self.assertEqual(run.call_args.args[0],['gio','open',self.project.as_uri()])

    def test_terminal_file_commands_use_latest_reply_without_calling_a_model(self):
        commands=iter(['/files','/files .cypress-e2e.log','/reveal .cypress-e2e.log','/quit']);out=io.StringIO()
        with patch('builtins.input',side_effect=lambda _:next(commands)),patch.object(hub,'run_task') as model,patch.object(files,'reveal',return_value={'opened':str(self.project)}) as reveal,contextlib.redirect_stdout(out):
            self.assertEqual(hub.chat(self.project,resume=True),0)
        model.assert_not_called();self.assertIn('12 browser checks passed',out.getvalue())
        self.assertEqual(reveal.call_args.args[-1],'files-task')
