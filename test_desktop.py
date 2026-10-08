import os
import json
import urllib.request
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dashboard
import desktop
import hub


class DesktopTests(unittest.TestCase):
    def test_invalid_descriptor_never_sends_its_token(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(hub, "STATE", Path(directory)):
            for value in ({"port": "https://outside.example", "token": "private"},
                          {"port": 8765, "token": "invalid\r\nHeader: value"},
                          {"port": 70000, "token": "a" * 32}):
                hub.save_json(hub.STATE / "dashboard" / "server.json", value)
                with patch("dashboard.urllib.request.urlopen") as request:
                    self.assertIsNone(dashboard.session_url())
                    request.assert_not_called()

    def test_browser_registration_paths_are_preserved_and_extended(self):
        with patch.dict(os.environ, {'XDG_DATA_DIRS':'/custom/share:/usr/share'}), patch.object(Path, 'is_dir', return_value=True):
            desktop.prepare_browser_environment()
            paths = os.environ['XDG_DATA_DIRS'].split(':')
            self.assertEqual(paths[:2], ['/custom/share','/usr/share'])
            self.assertIn('/var/lib/snapd/desktop', paths)
            desktop.prepare_browser_environment()
            self.assertEqual(os.environ['XDG_DATA_DIRS'].split(':'), paths)

    @unittest.skipIf(os.name == "nt", "Unix background server lifecycle")
    def test_saved_sidebar_survives_server_restart_without_a_new_task(self):
        from context_engine import ProjectMemory
        children = []
        original = subprocess.Popen
        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory() as temporary, patch.object(hub, 'STATE', Path(temporary) / 'state'):
            expected = set()
            for name in ('first', 'second'):
                folder = Path(temporary) / name; folder.mkdir()
                memory = ProjectMemory(folder)
                chat = memory.conversation(goal='Saved ' + name)
                memory.record(chat, name + '-task', 'Original question', 'Saved answer', 'completed')
                expected.add(chat)
            try:
                for _ in range(2):
                    with patch('desktop.subprocess.Popen', side_effect=capture):
                        url = desktop.ensure_server(0)
                    base, token = url.split('/#', 1)
                    request = urllib.request.Request(base + '/api/projects', headers={'X-CodeHub-Token': token})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        projects = json.load(response)['projects']
                    self.assertEqual({c['id'] for p in projects for c in p['conversations']}, expected)
                    self.assertFalse(list((hub.STATE / 'dashboard' / 'tasks').glob('*.json')))
                    children[-1].send_signal(signal.SIGINT)
                    children[-1].wait(timeout=10)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.send_signal(signal.SIGINT)
                    child.wait(timeout=10)

    @unittest.skipIf(os.name == "nt", "Unix background server lifecycle")
    def test_app_starts_a_real_server_then_reuses_it(self):
        children = []
        original = subprocess.Popen

        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with tempfile.TemporaryDirectory() as directory, patch.object(hub, "STATE", Path(directory)):
            try:
                with patch("desktop.subprocess.Popen", side_effect=capture):
                    url = desktop.ensure_server(0)
                    self.assertTrue(url.startswith("http://127.0.0.1:"))
                    self.assertIn("/#", url)
                    self.assertEqual(desktop.ensure_server(0), url)
                    self.assertEqual(len(children), 1)
                    self.assertEqual(dashboard.session_url(), url)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.send_signal(signal.SIGINT)
                    child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
