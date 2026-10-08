import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import hub
import terminal_ui


class TerminalTests(unittest.TestCase):
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
