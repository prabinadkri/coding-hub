import contextlib
import io
import json
import os
import select
import subprocess
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import hub
import terminal_ui


class TerminalTests(unittest.TestCase):
    def test_smart_model_and_workers_commands_apply_to_the_same_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project'; project.mkdir()
            commands = iter(['/route smart', '/model claude-manager', '/workers 2', 'Build a system', 'Add tests', '/quit'])
            with patch.object(hub, 'STATE', Path(temporary) / 'state'), patch('builtins.input', side_effect=lambda _: next(commands)), patch.object(hub, 'run_task', return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
                hub.chat(project)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs['model'], 'claude-manager')
            self.assertEqual(run.call_args.kwargs['workers'], 2)
            self.assertEqual(run.call_args_list[0].kwargs['conversation'], run.call_args_list[1].kwargs['conversation'])

    def test_delete_requires_confirmation_then_starts_a_fresh_chat(self):
        from context_engine import ProjectMemory
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); project = root / 'project'; project.mkdir()
            commands = iter(['/delete', 'First chat', '/delete', '', 'Follow up', '/delete', 'delete', 'Fresh chat', '/quit'])
            out = io.StringIO()
            with patch.object(hub, 'STATE', root / 'state'), patch('builtins.input', side_effect=lambda _: next(commands)), patch.object(hub, 'run_task', return_value=0) as run, contextlib.redirect_stdout(out):
                self.assertEqual(hub.chat(project), 0)
                ids = [call.kwargs['conversation'] for call in run.call_args_list]
                self.assertEqual(ids[0], ids[1])
                self.assertNotEqual(ids[1], ids[2])
                self.assertEqual(ProjectMemory(project).info()['conversations'], 1)
                self.assertEqual(ProjectMemory(project).messages(ids[2])['goal'], 'Fresh chat')
            self.assertIn('Chat kept.', out.getvalue())
            self.assertIn('Chat deleted.', out.getvalue())

    def test_commands_keep_multiline_text_mode_and_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); project = root/'project'; project.mkdir()
            commands = iter(['/help','/route local','/model qwen3:8b','/edit on','/paste','Create hello.py','Print OK','.','Follow up','/new','Third task',':memory','/quit'])
            out = io.StringIO()
            with patch.object(hub,'STATE',root/'state'), patch('builtins.input',side_effect=lambda _:next(commands)), patch.object(hub,'run_task',return_value=0) as run, contextlib.redirect_stdout(out):
                self.assertEqual(hub.chat(project),0)
            calls = run.call_args_list
            self.assertEqual(len(calls),3)
            self.assertEqual(calls[0].args[1], 'Create hello.py\nPrint OK')
            self.assertEqual(calls[0].args[2:5], ('local','fast',True))
            self.assertEqual(calls[0].kwargs['model'],'qwen3:8b')
            self.assertEqual(calls[0].kwargs['conversation'],calls[1].kwargs['conversation'])
            self.assertNotEqual(calls[0].kwargs['conversation'],calls[2].kwargs['conversation'])
            self.assertNotIn('\x1b', out.getvalue())
            self.assertIn('Project memory', out.getvalue())

    def test_general_local_prompt_allows_system_tasks_without_project_scope(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(hub,'STATE',Path(temporary)):
            project=hub.general_workspace()
            self.assertTrue(hub.is_general(project))
            self.assertFalse(hub.is_general(Path(temporary)/'other'))
            config=json.loads(hub.clean_environment('local',hub.LOCAL_AGENT_MODEL,True,general=True)['OPENCODE_CONFIG_CONTENT'])
            self.assertIn('general task',config['agent']['build']['prompt'])
            self.assertEqual(config['permission']['bash'],'allow')
            self.assertNotIn('Implement the requested change in this project',hub.task_prompt('Check disk space','',True,general=True))

    @unittest.skipIf(os.name == 'nt', 'PTY interaction requires Unix')
    def test_real_terminal_completes_slash_commands_and_exits(self):
        import pty
        master, slave = pty.openpty()
        with tempfile.TemporaryDirectory() as temporary:
            process = subprocess.Popen([sys.executable,str(hub.ROOT/'hub.py')],
                stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,CODING_HUB_STATE=temporary,TERM='xterm'))
            os.close(slave)
            output = b''
            try:
                deadline=time.monotonic()+5
                while b'You' not in output and time.monotonic()<deadline:
                    if select.select([master],[],[],.1)[0]: output+=os.read(master,65536)
                self.assertIn(b'You',output)
                self.assertNotIn(b'Choose:',output)
                os.write(master,b'/he\t\n/quit\n')
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if select.select([master],[],[],.1)[0]:
                        try: chunk=os.read(master,65536)
                        except OSError: break
                        if not chunk: break
                        output+=chunk
                    elif process.poll() is not None: break
                self.assertEqual(process.wait(timeout=2),0)
                self.assertIn(b'Conversation',output)
                self.assertNotIn(b'Unknown command',output)
            finally:
                if process.poll() is None: process.kill(); process.wait()
                os.close(master)

    def test_menu_default_starts_general_chat_then_redraws_and_accepts_quit(self):
        with tempfile.TemporaryDirectory() as temporary:
            commands=iter(['','','auto','q'])
            output=io.StringIO()
            with patch.object(hub,'STATE',Path(temporary)), patch('builtins.input',side_effect=lambda _:next(commands)), patch.object(hub,'chat',return_value=0) as chat, contextlib.redirect_stdout(output):
                self.assertEqual(hub.menu(),0)
            self.assertEqual(chat.call_args.args[0],Path(temporary)/'workspaces/general')
            self.assertEqual(output.getvalue().count('8  Conversation'),2)

    def test_mode_and_workspace_commands_apply_to_the_next_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);project=root/'project';project.mkdir()
            commands=iter(['/mode build','/project '+str(project),'Check this project','/general','/mode analysis','Explain disk space','/quit'])
            with patch.object(hub,'STATE',root/'state'), patch('builtins.input',side_effect=lambda _:next(commands)), patch.object(hub,'run_task',return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hub.chat(hub.general_workspace()),0)
            self.assertEqual(run.call_args_list[0].args[0],project.resolve())
            self.assertTrue(run.call_args_list[0].args[4])
            self.assertEqual(run.call_args_list[1].args[0],(root/'state/workspaces/general').resolve())
            self.assertFalse(run.call_args_list[1].args[4])

    def test_cancel_paste_and_invalid_command_never_start_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            commands = iter(['/paste','Discard this','/cancel','/edit maybe','/unknown','/quit'])
            with patch.object(hub,'STATE',Path(temporary)/'state'), patch('builtins.input',side_effect=lambda _:next(commands)), patch.object(hub,'run_task') as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hub.chat(temporary),0)
            run.assert_not_called()

    def test_terminal_control_sequences_are_removed_from_untrusted_replies(self):
        value = 'Hello\x1b[2J\x1b]52;c;dangerous\x07there\r\x00\nCode\tOK'
        cleaned = terminal_ui.safe_text(value)
        self.assertEqual(cleaned, 'Hellothere\nCode\tOK')

    def test_interactive_process_shows_progress_and_only_clean_answer(self):
        class Terminal(io.StringIO):
            def isatty(self): return True
        out = Terminal()
        script = 'import json,time; print(json.dumps({"type":"tool_use","part":{"tool":"write","state":{"status":"completed","output":"PRIVATE TOOL OUTPUT"}}}),flush=True); time.sleep(.3); print(json.dumps({"type":"text","part":{"text":"## Result\\nCreated hello.py."}}))'
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(out), patch.dict(os.environ,{'TERM':'xterm-256color'}):
            code, result, _ = hub.run_process([sys.executable,'-c',script],temporary,os.environ.copy(),Path(temporary)/'task.log',5)
        self.assertEqual(code,0)
        self.assertIn('Ctrl+C to stop',out.getvalue())
        self.assertIn('Created hello.py.',out.getvalue())
        self.assertNotIn('PRIVATE TOOL OUTPUT',out.getvalue())
        self.assertIn('PRIVATE TOOL OUTPUT',result)
