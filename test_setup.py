import contextlib
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import setup

class SetupTests(unittest.TestCase):
    def test_linux_launcher_matches_application_id_and_preserves_old_shortcut(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,'home',return_value=Path(temp)), \
             patch.object(setup.sys,'platform','linux'), patch.object(setup.shutil,'which',return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            legacy = Path(temp)/'.local/share/applications/prabin-coding-hub.desktop'
            legacy.parent.mkdir(parents=True)
            legacy.write_text('[Desktop Entry]\nName=Coding Hub\n')
            setup.configure()
            app_id='io.github.prabinadkri.CodingHub'
            registered=legacy.with_name(app_id+'.desktop').read_text()
            self.assertIn('Icon='+app_id,registered)
            self.assertIn('StartupWMClass='+app_id,registered)
            self.assertNotIn('NoDisplay=true',registered)
            self.assertIn('NoDisplay=true',legacy.read_text())
            self.assertTrue((Path(temp)/('.local/share/icons/hicolor/scalable/apps/'+app_id+'.svg')).is_file())
