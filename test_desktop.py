import os
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
