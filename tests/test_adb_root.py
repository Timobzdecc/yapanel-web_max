"""Авто-детект root-транспорта для удалённой панели (Max/Lite 2)."""
import subprocess
from unittest.mock import patch

from server import Adb


def completed(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


def test_probe_direct_root():
    adb = Adb("adb", "192.0.2.10:5555", needs_su=None)
    with patch.object(adb, "_raw_shell", return_value=completed(b"WP_MODE=direct\n")):
        assert adb.probe_needs_su() is False
    assert adb._root_mode == "direct"
    assert adb._root_argv is None


def test_probe_su_absolute_path():
    adb = Adb("adb", "192.0.2.10:5555", needs_su=None)
    with patch.object(adb, "_raw_shell", return_value=completed(b"WP_MODE=su /odm/bin/su\n")):
        assert adb.probe_needs_su() is True
    assert adb._root_mode == "su"
    assert adb._root_argv == ["/odm/bin/su"]


def test_shell_uses_detected_su_path():
    adb = Adb("adb", "192.0.2.10:5555", needs_su=None)
    with patch.object(adb, "_raw_shell", return_value=completed(b"WP_MODE=su /odm/bin/su\n")):
        adb.probe_needs_su()
    with patch("subprocess.run", return_value=completed(b"uid=0(root)\n@@webpanel-rc=0\n")) as run:
        out, err, rc = adb.shell("id -u")
    assert rc == 0
    assert "uid=0" in out
    argv = run.call_args.args[0]
    assert "/odm/bin/su" in argv
    assert argv[-1] == "/odm/bin/su"


def test_probe_rootd_and_shell_uses_queue():
    adb = Adb("adb", "192.0.2.10:5555", needs_su=None)
    with patch.object(adb, "_probe_root_mode", return_value=("rootd", None)), \
         patch.object(adb, "_rootd_exec", return_value=("0\n", "", 0)):
        assert adb.probe_needs_su() is True
    assert adb._root_mode == "rootd"
    with patch.object(adb, "_rootd_exec", return_value=("uid=0(root)\n", "", 0)) as rootd:
        out, err, rc = adb.shell("id")
    assert rc == 0
    assert "uid=0" in out
    rootd.assert_called_once()
