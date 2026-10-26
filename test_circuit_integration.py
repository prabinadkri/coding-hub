import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from circuit_integration import command, initialize, workspace, workspace_info, open_workspace, open_circuit


class CircuitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.studio = self.root / 'Circuit Studio'
        self.project.mkdir()
        (self.studio / 'docs').mkdir(parents=True)
        (self.studio / 'circuitctl.py').write_text('')
        (self.studio / 'launch.py').write_text('')
        (self.studio / 'docs/AUTOMATION.md').write_text('Local API reference')

    def test_init_preserves_guidance_and_refuses_overwrite(self):
        rules = self.project / 'CODING_HUB.md'
        rules.write_text('User requirements must remain verbatim.\n')
        initialize(self.project, self.studio)
        self.assertTrue(rules.read_text().startswith('User requirements must remain verbatim.\n'))
        self.assertIn('CIRCUITS.md', rules.read_text())
        self.assertEqual((self.project / 'CIRCUIT_API.md').read_text(), 'Local API reference')
        self.assertIn('CIRCUIT_API.md', (self.project / 'CIRCUITS.md').read_text())
        before = rules.read_bytes()
        with self.assertRaises(ValueError):
            initialize(self.project, self.studio)
        self.assertEqual(rules.read_bytes(), before)
        self.assertEqual(json.loads((self.project / 'circuit-studio.json').read_text())['studio'], str(self.studio))

    def test_literal_arguments_and_project_boundaries(self):
        initialize(self.project, self.studio)
        args = command(self.project, 'apply', 'a;echo hello.circuit', 'edit plan.json', 'deadbeef')
        self.assertEqual(args[1], str(self.studio / 'circuitctl.py'))
        self.assertIn(str(self.project / 'a;echo hello.circuit'), args)
        self.assertIn(str(self.project / 'edit plan.json'), args)
        self.assertEqual(args[-2:], ['--expected-revision', 'deadbeef'])
        for key in ('../escape.circuit', str(self.root / 'absolute.circuit')):
            with self.assertRaises(ValueError):
                command(self.project, 'inspect', key)
        (self.project / 'linked.circuit').symlink_to(self.root / 'outside.circuit')
        with self.assertRaises(ValueError):
            command(self.project, 'inspect', 'linked.circuit')
        with self.assertRaises(ValueError):
            command(self.project, 'new', 'a.circuit', '../plan.json')
        with self.assertRaises(ValueError):
            command(self.project, 'svg', 'a.circuit', output='../export.svg')

    def test_agent_context_receives_circuit_guidance(self):
        import hub
        from context_engine import ProjectMemory
        initialize(self.project, self.studio)
        with patch.object(hub, 'STATE', self.root / 'private-state'):
            memory = ProjectMemory(self.project)
            conversation = memory.conversation(goal='Make a circuit')
            context = memory.context(conversation, 'Make a circuit')
            self.assertIn('CIRCUITS.md', context)
            self.assertIn('structured circuit commands', context)

    def test_simulation_arguments_and_unknown_commands(self):
        initialize(self.project, self.studio)
        args = command(self.project, 'simulate', 'a.circuit', steps=200, time_step=1e-6)
        self.assertEqual(args[-4:], ['--steps', '200', '--time-step', '1e-06'])
        with self.assertRaises(ValueError):
            command(self.project, 'shell', 'a.circuit')
        with self.assertRaises(ValueError):
            command(self.project, 'inspect')

    def test_one_click_workspace_and_no_overwrite(self):
        with patch.dict('os.environ',{'XDG_DATA_HOME':str(self.root/'data'),'CIRCUIT_STUDIO_HOME':str(self.studio)}):
            first=workspace();second=workspace()
        self.assertNotEqual(first['project'],second['project'])
        self.assertEqual(first['prompt'],'Create a circuit: ')
        project=Path(first['project']);rules=(project/'CODING_HUB.md').read_text()
        (project/'design.circuit').write_text('{}')
        self.assertEqual(workspace(project,self.studio)['prompt'],'Improve this circuit: ')
        self.assertEqual((project/'CODING_HUB.md').read_text(),rules)
        self.assertIn('design.circuit',(project/'CIRCUITS.md').read_text())
        (project/'design.circuit').unlink();(project/'design.circuit').symlink_to(self.root/'outside')
        with self.assertRaises(ValueError):workspace_info(project)

    def test_open_launches_literal_project_and_no_model(self):
        import hub
        initialize(self.project,self.studio)
        with patch.dict('os.environ',{'CIRCUIT_STUDIO_HOME':str(self.studio)}),patch.object(hub,'STATE',self.root/'private'),patch('circuit_integration.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError,'No design.circuit'):open_circuit(self.project)
            launch.assert_not_called()
            value={'format':'circuit-studio','version':2,'schematic':'$ 1'}
            (self.project/'design.circuit').write_text(json.dumps(value))
            self.assertEqual(open_circuit(self.project),{'opened':True})
            self.assertEqual(launch.call_args.args[0][-2:],['--project',str(self.project/'design.circuit')])
            self.assertNotIn('shell',launch.call_args.kwargs)
        with patch('desktop.ensure_server',return_value='http://127.0.0.1:1234/#fake-session'),patch('circuit_integration.webbrowser.open',return_value=True) as browser:
            open_workspace(self.project)
            self.assertIn('&circuit=',browser.call_args.args[0])
            self.assertNotIn(str(self.project),browser.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
