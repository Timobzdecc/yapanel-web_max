"""Timed animation runner regressions; no speaker required."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

import server


class AnimationErrors(unittest.TestCase):
    def test_failed_preview_reports_utility_output(self):
        p = server.Panel(Mock())
        p.stop_pattern = Mock()
        p.detach = Mock()
        p.adb.shell.side_effect = [('', '', 0), ('led_util: invalid frame layout', '', 0)]
        with self.assertRaisesRegex(server.AdbError, 'invalid frame layout'):
            p._run_timed('ring', 'led_util demo', 5, verify=True)
        self.assertIn('/tmp/webpanel_ring.log', p.detach.call_args.args[1])

    def test_running_preview_is_accepted(self):
        p = server.Panel(Mock())
        p.stop_pattern = Mock()
        p.detach = Mock()
        p.adb.shell.side_effect = [('', '', 0), ('@running\n', '', 0)]
        p._run_timed('ring', 'led_util demo', 5, verify=True)

    @unittest.skipUnless(shutil.which('sh'), 'requires sh')
    def test_real_shell_start_check_detects_success_and_utility_failure(self):
        for command, succeeds in [('sleep 2', True), ("sh -c 'echo bad-layout >&2; exit 9'", False)]:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as td:
                p = server.Panel(Mock())
                p.stop_pattern = Mock()
                processes = []
                def local(script):
                    return script.replace('/tmp/webpanel_ring', (Path(td) / 'ring').as_posix())
                def shell(script, **kwargs):
                    result = subprocess.run(['sh', '-c', local(script)], capture_output=True, text=True, timeout=5)
                    return result.stdout, result.stderr, result.returncode
                p.adb.shell = shell
                p.detach = lambda kind, script: processes.append(subprocess.Popen(['sh', '-c', local(script)]))
                try:
                    if succeeds:
                        p._run_timed('ring', command, 1, verify=True)
                    else:
                        with self.assertRaisesRegex(server.AdbError, 'bad-layout'):
                            p._run_timed('ring', command, 1, verify=True)
                finally:
                    for proc in processes:
                        proc.wait(timeout=5)
