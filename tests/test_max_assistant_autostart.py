"""Offline tests for local Station Max assistant autostart."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class TestMaxAssistantAutostart(unittest.TestCase):
    def test_delayed_files_trigger_retry_then_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            events = root / "startup.log"
            ticks = []

            def sleep(_delay):
                ticks.append(1)
                if len(ticks) == 3:
                    (directory / "config.json").write_text("{}", encoding="utf-8")
                    launcher = directory / "assistantd"
                    launcher.write_text("#!/bin/sh\n", encoding="ascii")
                    launcher.chmod(0o755)

            start = Mock(return_value=123)
            with patch.object(server, "_max_assistant_running", side_effect=[False, True]):
                ok = server._max_assistant_autostart_worker(
                    assistant_dir=directory,
                    log_path=events,
                    attempts=4,
                    delay=0,
                    sleep=sleep,
                    launch=start,
                )
            self.assertTrue(ok)
            self.assertEqual(start.call_count, 1)
            log = events.read_text(encoding="utf-8")
            self.assertIn("WAIT_FILES attempt=1", log)
            self.assertIn("START_ATTEMPT attempt=3", log)
            self.assertIn("START_OK", log)
            self.assertNotIn("RETRIES_EXHAUSTED", log)

    def test_does_not_restart_existing_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            (directory / "config.json").write_text("{}", encoding="utf-8")
            launcher = directory / "assistantd"
            launcher.write_text("#!/bin/sh\n", encoding="ascii")
            launcher.chmod(0o755)
            start = Mock()
            with patch.object(server, "_max_assistant_running", return_value=True):
                ok = server._max_assistant_autostart_worker(
                    assistant_dir=directory, log_path=root / "startup.log",
                    attempts=1, delay=0, sleep=lambda _: None, launch=start,
                )
            self.assertTrue(ok)
            start.assert_not_called()
            self.assertIn("ALREADY_RUNNING",
                          (root / "startup.log").read_text(encoding="utf-8"))

    def test_no_files_leaves_clear_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            start = Mock()
            ok = server._max_assistant_autostart_worker(
                assistant_dir=directory, log_path=root / "startup.log",
                attempts=3, delay=0, sleep=lambda _: None, launch=start,
            )
            self.assertFalse(ok)
            start.assert_not_called()
            log = (root / "startup.log").read_text(encoding="utf-8")
            self.assertIn("WAIT_FILES attempt=1", log)
            self.assertIn("RETRIES_EXHAUSTED", log)

    def test_direct_launcher_writes_pid_without_start_stop_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "assistant"
            directory.mkdir()
            (directory / "config.json").write_text("{}", encoding="utf-8")
            launcher = directory / "assistantd"
            launcher.write_text("#!/bin/sh\n", encoding="ascii")
            launcher.chmod(0o755)
            pid_path = root / "assistant.pid"
            child = Mock()
            child.pid = 23456
            child.poll.return_value = None
            with patch.object(server, "_max_assistant_running", return_value=False):
                with patch.object(server.subprocess, "Popen", return_value=child) as popen:
                    with patch.object(server.time, "sleep"):
                        pid = server._max_assistant_launch_local(directory, pid_path)
            self.assertEqual(pid, 23456)
            self.assertEqual(pid_path.read_text(encoding="ascii").strip(), "23456")
            args, kwargs = popen.call_args
            self.assertEqual(args[0][:1], [str(launcher)])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stderr"], server.subprocess.STDOUT)


if __name__ == "__main__":
    unittest.main()
