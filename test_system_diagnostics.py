import subprocess
import unittest
from unittest.mock import patch
import system_diagnostics as diagnostics

class DiagnosticsTests(unittest.TestCase):
    def test_commands_are_read_only_bounded_and_never_use_a_shell(self):
        with patch.object(diagnostics.shutil,'which',return_value='/usr/bin/tool'), patch.object(diagnostics.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'ok','')) as run:
            for args in diagnostics.CHECKS.values():
                self.assertTrue(diagnostics.command(args)['ok'])
                self.assertNotIn('sudo',args)
                self.assertNotIn('shell',run.call_args.kwargs)
                self.assertEqual(run.call_args.kwargs['timeout'],4)
        with patch.object(diagnostics.shutil,'which',return_value=None):
            self.assertFalse(diagnostics.command(('not-installed',))['ok'])

    def test_failed_services_and_unsynchronized_clock_are_reported_honestly(self):
        def result(args):
            if args[0]=='timedatectl':return {'ok':True,'text':'NTP=no\nNTPSynchronized=no'}
            if args[0]=='systemctl':return {'ok':True,'text':'example.service loaded failed failed'}
            return {'ok':False,'text':'Unavailable in this test'}
        with patch.object(diagnostics.platform,'system',return_value='Linux'), patch.object(diagnostics,'command',side_effect=result):
            report=diagnostics.report()
        states={item['name']:item['state'] for item in report['checks']}
        self.assertEqual(states['Clock synchronization'],'attention')
        self.assertEqual(states['System services'],'attention')
        self.assertEqual(states['NVIDIA GPU'],'unavailable')
        self.assertIn('no settings changed',report['text'])
        self.assertIn('propose exact fixes for review',diagnostics.prompt(report['text']))
