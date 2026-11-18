"""TDD regression tests for Task P2 hardware, concurrency, and API bugfixes."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import io
import json
import os
import shlex
import socket
import sys
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_profiles import fresh_server
import agent
import config
import dsp_max
import glagol
import max_profile
import profiles
import pyadb


def _fresh_max_server():
    s = fresh_server()
    max_profile.install(s, profiles.PROFILES["max"])
    return s


# ---------------------------------------------------------------------------
# 1. server.py & api/extmusic.py bugs
# ---------------------------------------------------------------------------


def test_max_system_quasar_restart_uses_am_force_stop_not_q_restart(monkeypatch):
    s = _fresh_max_server()
    monkeypatch.setattr(max_profile.time, "sleep", lambda _secs: None)

    # 1. Remote ADB: force-stop -> startservice WakeUp -> verify pidof -> ok
    adb = Mock()
    adb.is_local = False
    calls = []

    def remote_shell(cmd, **kw):
        calls.append(cmd)
        if "pidof com.yandex.io.sdk" in cmd:
            return "4321\n", "", 0
        if "am startservice" in cmd:
            return "Starting service: Intent { cmp=com.yandex.io.sdk/com.yandex.tv.alice.app.KeepAliveService }\n", "", 0
        return "", "", 0

    adb.shell.side_effect = remote_shell
    p = s.Panel(adb)

    res = p.system("quasar_restart")
    assert res == {"ok": True, "pid": 4321}
    assert any("am force-stop com.yandex.io.sdk" in c for c in calls)
    assert any(
        "am startservice" in c
        and "com.yandex.io.sdk/com.yandex.tv.alice.app.KeepAliveService" in c
        and "com.yandex.tv.alice.app.KeepAliveService.WakeUp" in c
        for c in calls
    )
    assert any("pidof com.yandex.io.sdk" in c for c in calls)
    assert all("/q restart" not in c for c in calls)

    # 2. Local Android: am runs via max_android_cmd (u:r:shell:s0)
    local_adb = Mock()
    local_adb.is_local = True
    local_shell_calls = []

    def fake_run(args, **kwargs):
        local_shell_calls.append(args)
        return SimpleNamespace(stdout="Starting service: Intent {}\n", stderr="", returncode=0)

    p_local = s.Panel(local_adb)
    monkeypatch.setattr(max_profile, "_android_shell_available", lambda: True)
    monkeypatch.setattr(max_profile.subprocess, "run", fake_run)
    monkeypatch.setattr(max_profile, "find_quasar_pid", lambda: 7788)

    local_res = p_local.system("quasar_restart")
    assert local_res == {"ok": True, "pid": 7788}
    assert len(local_shell_calls) == 2
    assert local_shell_calls[0][:2] == ["/system/bin/sh", "-c"]
    assert "u:r:shell:s0" in local_shell_calls[0][2]
    assert "am force-stop com.yandex.io.sdk" in local_shell_calls[0][2]
    assert "u:r:shell:s0" in local_shell_calls[1][2]
    assert "am startservice" in local_shell_calls[1][2]
    assert "KeepAliveService.WakeUp" in local_shell_calls[1][2]

    # 3. Error / missing PID must raise AdbError
    adb_err = Mock()
    adb_err.is_local = False
    adb_err.shell.side_effect = lambda cmd, **kw: ("", "SecurityException: not allowed", 1)
    with pytest.raises(s.AdbError):
        s.Panel(adb_err).system("quasar_restart")

    adb_nopid = Mock()
    adb_nopid.is_local = False
    adb_nopid.shell.side_effect = lambda cmd, **kw: ("", "", 1) if "pidof" in cmd else ("Starting service\n", "", 0)
    with pytest.raises(s.AdbError):
        s.Panel(adb_nopid).system("quasar_restart")

    # 4. Base server.Panel.system("quasar_restart") also raises AdbError on am failure
    s_base = fresh_server()
    base_adb = Mock()
    base_adb.shell.return_value = ("Error: Unknown service", "", 1)
    with pytest.raises(s_base.AdbError):
        s_base.Panel(base_adb).system("quasar_restart")


def test_max_logs_list_and_log_allowlist_and_traversal():
    s = fresh_server()
    max_logs = [
        ("yapanel.log", "/data/yapanel/yapanel.log"),
        ("assistant.log", f"{s.ASSIST_DIR}/assistant.log"),
        ("bt-memes.log", f"{s.WEB_DIR}/bt-memes/bt-memes.log"),
        ("wakeword.log", s.WAKE_LOG),
        ("alice_frida.log", s.WAKE_FRIDA_LOG),
    ]
    cmds = []

    def fake_shell(cmd, **kw):
        cmds.append(cmd)
        if "which logcat" in cmd:
            return "/system/bin/logcat\n", "", 0
        if "yapanel.log" in cmd and "[ -f" in cmd:
            return "\n".join(name for name, _ in max_logs) + "\n", "", 0
        if "tail -n" in cmd:
            return "line1\n", "", 0
        return "", "", 0

    adb = Mock()
    adb.shell.side_effect = fake_shell
    p = s.Panel(adb)

    listed = p.logs_list()
    for name, _ in max_logs:
        assert name in listed, f"Missing Max log {name!r} in {listed}"

    cmds.clear()
    for name, expected_path in max_logs:
        out = p.log(name, 25)
        assert "line1" in out["text"]
        assert any(f"tail -n 25 {expected_path}" in c for c in cmds), (name, expected_path, cmds)

    cmds.clear()
    for bad in ("../etc/passwd", "/etc/passwd", "yapanel.log/../../etc/shadow", "tmp:../etc/passwd", "wakeword.log;id"):
        with pytest.raises(s.AdbError):
            p.log(bad, 50)
    assert not cmds


def test_backup_and_restore_supports_max_dirs_and_drops_oknix_alarm():
    s = _fresh_max_server()
    fullmatch = s.Panel.BACKUP_FILES.fullmatch

    # Accepted Max paths
    for good in (
        "boot.binscled.zst",
        "boot_sound.wav",
        "boot_every_time",
        "hosts",
        "override/sounds/common/timer.wav",
        "override/ledpatterns/ring/alice.led",
        "override/ledpatterns/clock/blink.binscled.zst",
        "patterns/ring/pulse.led",
        "patterns/clock/boot.binscled.zst",
        "anims/ring/my_ring.led",
        "anims/clock/my_clock.binscled",
        "agent/tokens",
        "assistant/config.json",
    ):
        assert fullmatch(good), f"Expected BACKUP_FILES to accept {good!r}"

    # Dead Buildroot & traversal paths must be rejected
    for bad in (
        "alarm/alarms.conf",
        "alarm/scenes.conf",
        "override/ledpatterns/oknix/timer.led",
        "yamd_autostart",
        "patterns/ring/../etc/passwd",
        "patterns/clock/.hidden",
        "agent/../etc/passwd",
        "assistant/.hidden",
        "override/sounds/a/../b",
    ):
        assert not fullmatch(bad), f"Expected BACKUP_FILES to reject {bad!r}"

    # Build backup tarball and restore through Max profile
    buf = io.BytesIO()
    manifest = json.dumps({"format": 1, "created": "2026-11-18T00:00:00+0000", "ui": {}}).encode("utf-8")
    payloads = {
        "webpanel/override/ledpatterns/ring/alice.led": b"loop\nff0000 100\n",
        "webpanel/patterns/ring/pulse.led": b"loop\n00ff00 100\n",
        "webpanel/patterns/clock/custom.binscled.zst": b"clock-zst",
        "webpanel/agent/tokens": b"digest1 panel\n",
        "webpanel/assistant/config.json": b'{"enabled": true}',
    }
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(manifest)
        tar.addfile(ti, io.BytesIO(manifest))
        for name, data in payloads.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

    adb = Mock()
    adb.is_local = False
    pushes = []
    adb.shell.return_value = ("", "", 0)
    adb.push.side_effect = lambda local, remote, **kw: pushes.append((Path(local).read_bytes(), remote))

    p = s.Panel(adb)
    p.patterns = lambda: {"ring": ["alice.led"], "clock": []}
    p.footprint = lambda: {"items": [], "dir": s.WEB_DIR, "size_kb": 0, "any": False}
    p.footprint_reset = lambda: None
    p.hook_status = lambda: {"hook": False}
    p.boot_status = lambda: {"supported": False, "sound_supported": False}
    p._snapshot_web = lambda td: {}
    installed_overrides = []
    p._install_override = lambda data, custom, target, orig=None: installed_overrides.append((custom, target, data))

    res = p.backup_restore(buf.getvalue())
    assert res["restored"]
    assert (f"{s.OVR_DIR}/ledpatterns/ring/alice.led", f"{s.PATTERNS['ring']}/alice.led", b"loop\nff0000 100\n") in installed_overrides
    pushed_remotes = {remote: data for data, remote in pushes}
    assert pushed_remotes.get(f"{s.WEB_DIR}/patterns/ring/pulse.led") == b"loop\n00ff00 100\n"
    assert pushed_remotes.get(f"{s.WEB_DIR}/patterns/clock/custom.binscled.zst") == b"clock-zst"
    assert pushed_remotes.get(f"{s.WEB_DIR}/agent/tokens") == b"digest1 panel\n"
    assert pushed_remotes.get(f"{s.WEB_DIR}/assistant/config.json") == b'{"enabled": true}'


def test_panel_allow_networks_allows_loopback_extmusic_and_enforces_remote(monkeypatch):
    s = fresh_server()
    orig_cfg = config.get_config()
    cfg = config.PanelConfig()
    cfg.server.allow_networks = ("10.20.0.0/24",)  # intentionally NO 127.0.0.0/8
    config.set_config(cfg)
    try:
        acc = Mock()
        acc.session_ok.return_value = False

        def make_handler(ip, command="GET", headers=None):
            h = s.Handler.__new__(s.Handler)
            h.client_address = (ip, 51234)
            h.command = command
            h.headers = headers or {}
            h.access = acc
            h.panel = Mock(config=cfg)
            h.sent = []
            h.close_connection = False
            h._send = lambda code, body, ctype="application/json", headers=(): h.sent.append((code, body))
            return h

        for lo_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            h_get = make_handler(lo_ip, "GET")
            assert h_get._guard("/api/extmusic/status") is False, f"{lo_ip} GET /api/extmusic/* blocked: {h_get.sent}"

            h_post = make_handler(lo_ip, "POST", {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": "2"})
            assert h_post._guard("/api/extmusic/shuffle") is False, f"{lo_ip} POST /api/extmusic/* blocked: {h_post.sent}"

            # CSRF origin check must still enforce on loopback
            h_csrf = make_handler(lo_ip, "POST", {"Host": "127.0.0.1:8765", "Origin": "https://evil.example"})
            assert h_csrf._guard("/api/extmusic/shuffle") is True
            assert h_csrf.sent[-1][0] == 403

        # Non-loopback outside allow_networks -> 403
        h_denied = make_handler("192.168.99.10", "GET")
        assert h_denied._guard("/api/extmusic/status") is True
        assert h_denied.sent[-1][0] == 403

        # Non-loopback inside allow_networks without session cookie -> 401
        h_allowed_unauth = make_handler("10.20.0.5", "GET")
        assert h_allowed_unauth._guard("/api/extmusic/status") is True
        assert h_allowed_unauth.sent[-1][0] == 401
    finally:
        config.set_config(orig_cfg)


def test_music_has_next_requires_repeat_none_for_yt_autoplay():
    s = _fresh_max_server()
    p = s.Panel(Mock())
    p.glagol = SimpleNamespace(
        ws=True,
        state={"playing": False, "playerState": None},
        info=lambda: {"connected": True, "state": p.glagol.state, "updated": time.time()},
    )
    p.auth = SimpleNamespace(music_token="", info=lambda: {"logged_in": False})
    svc = SimpleNamespace(
        lock=threading.RLock(),
        current={"source": "yt", "id": "vid1", "title": "Song 1", "artist": "Art", "duration": 180},
        queue=[{"source": "yt", "id": "vid1", "title": "Song 1", "artist": "Art", "duration": 180}],
        index=0,
        playing=True,
        position=1.0,
        duration=180.0,
        shuffle=False,
        repeat="one",
        autoplay_radio=True,
        yt_account=None,
        maybe_auto_stop_from_glagol=lambda st: None,
        status=lambda: {
            "active": True,
            "current": svc.current,
            "queue": svc.queue,
            "index": 0,
            "playing": True,
            "position": 1.0,
            "duration": 180.0,
            "shuffle": False,
            "repeat": "one",
            "autoplay_radio": True,
        },
    )
    p._extmusic_service = svc

    ps = p.music()["glagol"]["state"]["playerState"]
    assert ps["hasNext"] is False


def test_music_play_reads_queue_under_svc_lock():
    s = fresh_server()
    p = s.Panel(Mock())
    p.glagol = Mock()
    p.auth = SimpleNamespace(music_token="", info=lambda: {"logged_in": False})

    lock_events = []

    class TrackingLock:
        def __enter__(self):
            lock_events.append("enter")
            return self

        def __exit__(self, *a):
            lock_events.append("exit")
            return False

    svc = SimpleNamespace(
        lock=TrackingLock(),
        current={"source": "yt", "id": "vid1", "title": "Song 1"},
        queue=[{"source": "yt", "id": "vid1", "title": "Song 1"}],
        play=lambda t, q=None: {"ok": True, "current": t, "queue_len": len(q or [])},
    )
    p._extmusic_service = svc

    res = p.music_play("ext:yt:vid1", "track")
    assert res["ok"] is True
    assert lock_events == ["enter", "exit"]


def test_api_extmusic_queue_rejects_float_indices_with_400():
    import api

    class BadSvc:
        def queue_remove(self, idx):
            raise AssertionError(f"queue_remove should not receive float index {idx!r}")

        def queue_move(self, fi, ti):
            raise AssertionError(f"queue_move should not receive float indices {fi!r}, {ti!r}")

    panel = SimpleNamespace(_extmusic_service=BadSvc())
    rm_route = api.ROUTER.match("POST", "/api/extmusic/queue/remove")
    mv_route = api.ROUTER.match("POST", "/api/extmusic/queue/move")
    assert rm_route and mv_route

    for bad in (1.5, "1.5", True, None):
        with pytest.raises((ValueError, TypeError)):
            rm_route(api.ApiRequest(panel, {}, {"index": bad}))
        with pytest.raises((ValueError, TypeError)):
            mv_route(api.ApiRequest(panel, {}, {"from": bad, "to": 0}))
        with pytest.raises((ValueError, TypeError)):
            mv_route(api.ApiRequest(panel, {}, {"from": 0, "to": bad}))


def test_assistant_launch_local_checks_system_cert_pem_fallback(tmp_path, monkeypatch):
    s = fresh_server()
    assist_dir = tmp_path / "assistant"
    assist_dir.mkdir()
    (assist_dir / "assistant_max.py").write_text("import time\n", encoding="utf-8")
    (assist_dir / "config.json").write_text("{}", encoding="utf-8")
    pid_path = tmp_path / "assistant.pid"
    fallback_pem = tmp_path / "etc_ssl_cert.pem"
    fallback_pem.write_text("CERT", encoding="utf-8")
    missing_primary = tmp_path / "missing_cacert.pem"

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(s, "Path", lambda p="": (
        missing_primary if str(p) == "/data/yapanel/certs/cacert.pem"
        else fallback_pem if str(p) == "/data/etc/ssl/cert.pem"
        else Path(p)
    ))

    captured_env = {}

    class DummyChild:
        def poll(self):
            return None

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured_env.update(kw.get("env") or {})
        pid_path.write_text("4242\n", encoding="ascii")
        return DummyChild()

    monkeypatch.setattr(s.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(s, "_max_assistant_running", lambda p: p.exists())

    pid = s._max_assistant_launch_local(assistant_dir=assist_dir, pid_path=pid_path)
    assert pid == 4242
    assert captured_env.get("SSL_CERT_FILE") == str(fallback_pem)


def test_zero_mtime_file_cache_computes_and_caches_md5(tmp_path, monkeypatch):
    s = fresh_server()
    assist_py = tmp_path / "assistant_max.py"
    assist_py_bytes = b"print('assistant max v2')\n"
    assist_py.write_bytes(assist_py_bytes)
    os.utime(assist_py, (0, 0))
    assert assist_py.stat().st_mtime == 0.0
    expected_py_md5 = hashlib.md5(assist_py_bytes).hexdigest()

    assist_bin = tmp_path / "assistantd"
    assist_bin_bytes = b"\x7fELFassist"
    assist_bin.write_bytes(assist_bin_bytes)
    os.utime(assist_bin, (0, 0))
    assert assist_bin.stat().st_mtime == 0.0
    expected_bin_md5 = hashlib.md5(assist_bin_bytes).hexdigest()

    viz_bin = tmp_path / "vizd"
    viz_bin_bytes = b"\x7fELFvizz"
    viz_bin.write_bytes(viz_bin_bytes)
    os.utime(viz_bin, (0, 0))
    assert viz_bin.stat().st_mtime == 0.0
    expected_viz_md5 = hashlib.md5(viz_bin_bytes).hexdigest()

    monkeypatch.setattr(s, "__file__", str(tmp_path / "server.py"))
    monkeypatch.setattr(s, "ASSIST_SRC", tmp_path / "missing_assist_src")

    adb = Mock()
    adb.shell.return_value = (
        f"@installed\n{expected_bin_md5}  /data/quasar/webpanel/assistant/assistantd\n"
        f"@pymd5\n{'0' * 32}  /data/quasar/webpanel/assistant/assistant_max.py\n"
        "@status\n{}\n",
        "",
        0,
    )
    p = s.Panel(adb)
    p._assist_binary = lambda download=False: assist_bin
    p._viz_binary = lambda download=False: viz_bin

    read_counts = {assist_py: 0, assist_bin: 0, viz_bin: 0}
    orig_read_bytes = Path.read_bytes

    def counting_read_bytes(self_path):
        if self_path in read_counts:
            read_counts[self_path] += 1
        return orig_read_bytes(self_path)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    first_py = p._assist_py_local_md5()
    second_py = p._assist_py_local_md5()
    assert first_py == expected_py_md5
    assert len(first_py) == 32
    assert second_py == expected_py_md5
    assert read_counts[assist_py] == 1

    assert p._assist_local_md5() == expected_bin_md5
    assert p._assist_local_md5() == expected_bin_md5
    assert read_counts[assist_bin] == 1

    assert p._viz_local_md5() == expected_viz_md5
    assert p._viz_local_md5() == expected_viz_md5
    assert read_counts[viz_bin] == 1

    status = p.assistant_status()
    assert status["outdated"] is True


def test_run_sh_and_installer_guard_against_duplicate_server_launch():
    root = Path(__file__).resolve().parents[1]
    run_sh_bytes = (root / "run.sh").read_bytes()
    assert b"\r\n" not in run_sh_bytes, "run.sh must use LF line endings"
    run_sh = run_sh_bytes.decode("utf-8")
    assert "ps -A -o ARGS" in run_sh
    assert "[s]erver\\.py --host" in run_sh
    assert "exit 0" in run_sh

    ps1_bytes = (root / "install_to_max.ps1").read_bytes()
    ps1_tail = ps1_bytes.rsplit(b'"@', 1)[-1].decode("utf-8")
    idx = ps1_tail.find("nohup /data/yapanel/run.sh")
    assert idx != -1
    launch_line = ps1_tail[ps1_tail.rfind("\n", 0, idx) + 1 : ps1_tail.find("\n", idx)]
    assert "ps -A -o ARGS" in launch_line
    assert "'[s]erver\\.py --host'" in launch_line


def test_installer_cleans_stale_assets_and_preserves_nonzero_mtimes():
    import base64
    import io
    import re
    import struct
    import tarfile

    root = Path(__file__).resolve().parents[1]
    ps1_bytes = (root / "install_to_max.ps1").read_bytes()
    ps1_tail = ps1_bytes.rsplit(b'"@', 1)[-1].decode("utf-8")

    # Stale asset/script cleanup must happen before tar -xzf and never touch runtime state files
    unpack_idx = ps1_tail.find("tar -xzf /data/local/tmp/yapanel_max.tar.gz -C /data/yapanel/")
    assert unpack_idx != -1
    before_unpack = ps1_tail[:unpack_idx]
    assert "rm -rf /data/yapanel/static" in before_unpack
    for stale in (
        "/data/yapanel/device/alarmd.sh",
        "/data/yapanel/device/probe.sh",
        "/data/yapanel/device/assistant/assistant_max.py",
        "/data/yapanel/device/input/RemoveGoogle.java",
    ):
        assert stale in before_unpack, f"missing stale cleanup for {stale}"
    for state_file in (
        ".extmusic.json",
        ".panel_settings.json",
        ".yandex_auth.json",
        ".metrics.json",
        ".panel_journal.jsonl",
        ".radio_logos",
        ".tls",
        "max_bt_sync.json",
    ):
        assert state_file not in ps1_tail, f"installer must not reference/wipe runtime {state_file}"

    # Embedded tar.gz must have non-zero gzip & tar member mtimes so HTTP If-Modified-Since never misfires
    m = re.search(rb'\$EmbeddedTarB64 = @"\r?\n(.*?)\r?\n"@', ps1_bytes, re.S)
    assert m, "embedded tar marker not found"
    tar_gz = base64.b64decode(re.sub(rb"\s+", b"", m.group(1)))
    gzip_mtime = struct.unpack("<I", tar_gz[4:8])[0]
    assert gzip_mtime > 0, f"embedded gzip header mtime must be > 0, got {gzip_mtime}"
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tf:
        members = tf.getmembers()
        assert members
        assert all(mem.mtime > 0 for mem in members), "every embedded tar member must have mtime > 0"


def test_build_installer_uses_nonzero_mtime_when_present():
    import importlib.util
    import io
    import struct
    import tarfile

    root = Path(__file__).resolve().parents[1]
    script = root / "tools" / "build_installer.py"
    if not script.is_file():
        pytest.skip("tools/build_installer.py is not tracked in CI")
    spec = importlib.util.spec_from_file_location("build_installer", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tar_bytes, _ = mod.build_tar({"static/app.js": b"console.log('ok');\n"})
    gzip_mtime = struct.unpack("<I", tar_bytes[4:8])[0]
    assert gzip_mtime > 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        members = tf.getmembers()
        assert members
        assert all(mem.mtime > 0 for mem in members)


def test_install_and_uninstall_scripts_compat_root_device_check_and_port():
    import re
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[1]
    for name in ("install_to_max.ps1", "uninstall_to_max.ps1"):
        raw = (root / name).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{name} must keep UTF-8 BOM for PowerShell 5.1"

    ps1_bytes = (root / "install_to_max.ps1").read_bytes()
    m_tar = re.search(rb'\$EmbeddedTarB64 = @"\r?\n.*?\r?\n"@', ps1_bytes, re.S)
    assert m_tar
    ps1_head = ps1_bytes[:m_tar.start()].decode("utf-8-sig")
    ps1_tail = ps1_bytes[m_tar.end():].decode("utf-8")
    un_text = (root / "uninstall_to_max.ps1").read_text(encoding="utf-8-sig")

    # 1. Version banner regex required by tools/build_installer.py stays intact
    assert re.search(r"YapAnel Hi-Fi & Party Mode v\d+(?:\.\d+){1,3}", ps1_tail)

    # 2. Device model & architecture guard with -Force escape hatch
    assert "[switch]$Force" in ps1_head
    assert "uname -m" in ps1_tail
    assert "ro.product.device" in ps1_tail
    assert "yandexstation_2" in ps1_tail

    # 3. Custom ADB port parsing (never blindly appends :5555 or builds http://host:port:8765)
    assert "Parse-StationEndpoint" in ps1_tail
    assert ':5555$",""' not in ps1_tail
    assert "Parse-StationEndpoint" in un_text

    # 4. Extended su candidates, su 0 support, rootd rootres verification, no broken awk \$1, base64 direct shell
    for script_body, label in ((ps1_tail, "install_to_max.ps1"), (un_text, "uninstall_to_max.ps1")):
        assert "/odm/bin/su" in script_body, label
        assert "/vendor/xbin/su" in script_body, label
        assert "/sbin/su" in script_body, label
        assert 'awk "{print \\$1}"' not in script_body, label
        assert "shell $normCmd" not in script_body, f"{label} must encode direct commands via base64"
        assert "rootres." in script_body, label

    # 5. Fast Python import pre-check before slow online pip install + /data space check
    assert "import tinytuya, requests, cryptography" in ps1_tail
    assert "df -k /data" in ps1_tail
    # Uninstall must not delete active Hybrid rootd channel files while running via rootd
    assert "/data/local/tmp/rootout" not in un_text
    assert "/data/local/tmp/rootcmd-" not in un_text

    # 6. PowerShell syntax check when powershell.exe is present (Windows host)
    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    if ps_exe:
        check_cmd = (
            "$e=$null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile('install_to_max.ps1',[ref]$null,[ref]$e); "
            "if($e.Count){$e|ForEach-Object{Write-Error $_};exit 1}; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile('uninstall_to_max.ps1',[ref]$null,[ref]$e); "
            "if($e.Count){$e|ForEach-Object{Write-Error $_};exit 1}"
        )
        res = subprocess.run(
            [ps_exe, "-NoProfile", "-Command", check_cmd],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr or res.stdout


# ---------------------------------------------------------------------------
# 2. max_profile.py & dsp_max.py bugs
# ---------------------------------------------------------------------------


def test_max_profile_boot_dsp_uses_local_panel_not_module(tmp_path, monkeypatch):
    woofer = tmp_path / "woofer"
    tweeter = tmp_path / "tweeter"
    woofer.write_text("", encoding="utf-8")
    tweeter.write_text("", encoding="utf-8")
    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(woofer))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tweeter))

    seen_panels = []
    orig_init = dsp_max.MaxDSP.__init__

    def spy_init(self, panel):
        seen_panels.append(panel)
        orig_init(self, panel)

    monkeypatch.setattr(dsp_max.MaxDSP, "__init__", spy_init)
    monkeypatch.setattr(dsp_max.MaxDSP, "restore_hardware_state", lambda self: {"ok": True})

    s = fresh_server()
    max_profile.install(s, profiles.PROFILES["max"])

    assert seen_panels, "Boot DSP should initialize when sysfs paths exist"
    assert seen_panels[0] is not s, "MaxDSP must not receive the server module directly"
    assert getattr(getattr(seen_panels[0], "adb", None), "is_local", False) is True


def test_max_set_quasar_clock_paused_remote_autodetects_python_and_returns_real_status():
    adb = Mock()
    adb.is_local = False
    cmds = []

    def fake_shell(cmd, **kw):
        cmds.append(cmd)
        return "WP_OK=1\n", "", 0

    adb.shell.side_effect = fake_shell
    with patch.object(max_profile.os.path, "exists", return_value=False):
        ok = max_profile.max_set_quasar_clock_paused(adb, pause=True)
    assert ok is True
    assert cmds and "command -v python3" in cmds[0]
    tokens = shlex.split(cmds[0])
    assert "-c" in tokens
    py_src = tokens[tokens.index("-c") + 1]
    ast.parse(py_src)
    compile(py_src, "<remote_quasar_clock>", "exec")

    adb.shell.side_effect = lambda cmd, **kw: ("WP_OK=0\n", "", 0)
    with patch.object(max_profile.os.path, "exists", return_value=False):
        fail = max_profile.max_set_quasar_clock_paused(adb, pause=False)
    assert fail is False


def test_max_display_manager_remote_stop_clears_fb_and_eq_frame_uses_lock():
    s = _fresh_max_server()
    adb = Mock()
    adb.is_local = False
    cmds = []
    adb.shell.side_effect = lambda cmd, **kw: (cmds.append(cmd), ("", "", 0))[1]
    p = s.Panel(adb)

    res = p.display_eq_frame([128] * 400)
    assert res == {"ok": True}
    cmds.clear()

    stopped = p.display_stop()
    assert stopped == {"mode": None}
    assert any(max_profile.MAX_SCREEN_DEV in c for c in cmds), f"Remote stop must update {max_profile.MAX_SCREEN_DEV}: {cmds}"
    assert any("base64 -d" in c for c in cmds)


def test_max_display_eq_frame_reads_running_mode_under_lock():
    # Direct behavioral verification with a tracking RLock on the cached display manager
    s = _fresh_max_server()
    adb = Mock()
    adb.is_local = False
    adb.shell.return_value = ("", "", 0)
    p = s.Panel(adb)

    # First call initializes the manager
    p.display_eq_frame([10] * 400)
    # Trigger a stop to grab/reset, then inspect via display_status lock usage isn't mutated
    # Re-create fresh manager cleanly by re-installing profile on clean server module
    s2 = _fresh_max_server()
    p2 = s2.Panel(adb)
    lock_acquires = []

    orig_rlock = max_profile.threading.RLock

    class TrackingLock:
        def __init__(self):
            self._rlock = orig_rlock()

        def __enter__(self):
            lock_acquires.append("enter")
            return self._rlock.__enter__()

        def __exit__(self, *a):
            lock_acquires.append("exit")
            return self._rlock.__exit__(*a)

    try:
        max_profile.threading.RLock = TrackingLock
        p2.display_eq_frame([0] * 400)
        p2.display_eq_frame([255] * 400)
    finally:
        max_profile.threading.RLock = orig_rlock
        p2.display_stop()

    # On the second eq_frame when already running in mode=="eq", lock is entered cleanly
    assert len(lock_acquires) >= 4


def test_max_install_no_host_makedirs_remote_patterns_and_my_ring_pattern(monkeypatch):
    mkdir_calls = []
    monkeypatch.setattr(max_profile.os, "makedirs", lambda path, exist_ok=False: mkdir_calls.append(path))
    # Simulate non-Android host environment explicitly
    s = _fresh_max_server()
    assert not mkdir_calls, f"install() ran os.makedirs on host PC: {mkdir_calls}"

    # Remote patterns() must NOT be masked by host os.path.exists()
    adb = Mock()
    adb.is_local = False
    adb.shell.side_effect = lambda cmd, **kw: (
        ("stock.led\n", "", 0) if s.PATTERNS["ring"] in cmd
        else ("custom.led\n", "", 0) if f"{s.MY_ANIM_DIR}/ring" in cmd
        else ("", "", 0)
    )
    adb.read_bytes.return_value = b"loop\nff0000 100\n"
    p = s.Panel(adb)

    with patch.object(max_profile.os.path, "exists", return_value=True), \
         patch.object(max_profile.os, "listdir", return_value=["host_ghost.led"]):
        pats = p.patterns()
    assert pats["ring"] == ["stock.led", "my/custom.led"]

    # Play my/... ring pattern over remote ADB -> must read MY_ANIM_DIR/ring/...
    with patch.object(max_profile.os.path, "exists", return_value=False):
        res = p.play_pattern("ring", "my/custom.led", seconds=0.05)
    assert res["ok"] is True
    adb.read_bytes.assert_called_with(f"{s.MY_ANIM_DIR}/ring/custom.led")
    p.stop_pattern("ring")


def test_max_bt_action_power_off_sets_bt_off_true():
    s = _fresh_max_server()
    adb = Mock()
    adb.is_local = False
    adb.shell.return_value = ("", "", 0)
    p = s.Panel(adb)
    p.glagol = SimpleNamespace(ws=True, send=Mock())
    p._bt_set_alice_state = Mock()
    p.bt_state = Mock(return_value={"powered": True})

    seen_flags = []
    with patch.object(max_profile, "max_set_bt_off", side_effect=lambda a, path, on: seen_flags.append(on)):
        p.bt_action({"action": "power_off"})

    assert seen_flags == [True]


def test_dsp_max_profile_resets_and_unified_is_local(tmp_path, monkeypatch):
    woofer = tmp_path / "woofer"
    tweeter = tmp_path / "tweeter"
    woofer.write_text("", encoding="utf-8")
    tweeter.write_text("", encoding="utf-8")
    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(woofer))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tweeter))

    panel = SimpleNamespace(adb=None, _load_settings=lambda: {})
    dsp = dsp_max.MaxDSP(panel)
    dsp._write_i2c_batch = lambda cmds: True
    dsp._save_state = lambda: True
    dsp._update_boot_script = lambda: True

    assert dsp.apply_profile("club", save=False)["ok"] is True
    assert dsp.params["bass_gain"] == 6.5
    assert dsp.params["subsonic_freq"] == 40

    assert dsp.apply_profile("night", save=False)["ok"] is True
    assert dsp.params["bass_gain"] == 0.0
    assert dsp.params["subsonic_freq"] == 65

    assert dsp.apply_profile("bass", save=False)["ok"] is True
    assert dsp.params["subsonic_freq"] == 45

    assert dsp.apply_profile("stock", save=False)["ok"] is True
    assert dsp.params["bass_gain"] == 0.0
    assert dsp.params["subsonic_freq"] == 48

    # Unified _is_local check between verify_registers and _write_i2c_batch
    monkeypatch.setattr(dsp_max, "WOOFER_SYS", str(tmp_path / "missing_w"))
    monkeypatch.setattr(dsp_max, "TWEETER_SYS", str(tmp_path / "missing_t"))
    remote_adb = Mock(is_local=False)
    local_adb = Mock(is_local=True)
    dsp.adb = remote_adb
    assert dsp._is_local() is False
    dsp.adb = local_adb
    assert dsp._is_local() is True
    dsp._expected_regs = {(str(tmp_path / "missing_w"), "3A"): "3A 00 80 00 00"}
    assert dsp.verify_registers() is None
    remote_adb.shell.assert_not_called()
    local_adb.shell.assert_not_called()


# ---------------------------------------------------------------------------
# 3. pyadb.py, glagol.py, agent.py bugs
# ---------------------------------------------------------------------------


def test_pyadb_reader_locks_streams_and_close_closes_forwards():
    # 1. Verify _reader acquires self.lock around self.streams lookup
    dev = pyadb.Device.__new__(pyadb.Device)
    lock_events = []

    class SpyRLock:
        def __init__(self):
            self._lock = threading.RLock()

        def __enter__(self):
            lock_events.append("enter")
            return self._lock.__enter__()

        def __exit__(self, *a):
            lock_events.append("exit")
            return self._lock.__exit__(*a)

    dev.lock = SpyRLock()
    dev.streams = {}
    dev.alive = True
    dev.sock = Mock()
    packets = iter([(pyadb.A_CLSE, 1, 99, b""), OSError("eof")])

    def next_pkt():
        v = next(packets)
        if isinstance(v, Exception):
            raise v
        return v

    dev._read_packet = next_pkt
    dev._reader()
    assert "enter" in lock_events and "exit" in lock_events

    # 2. Verify forward() sockets are tracked by dev and closed on dev.close()
    dev2 = pyadb.Device.__new__(pyadb.Device)
    dev2.lock = threading.RLock()
    dev2.streams = {}
    dev2.alive = True
    dev2.sock = Mock()
    dev2._forwards = set()
    dev2.open = lambda dest: SimpleNamespace(read=lambda n=-1: b"", write=lambda d: None, close=lambda: None)

    port = pyadb.forward(dev2, 1961)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        dev2.close()

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)


def test_glagol_locks_identity_and_ymusic_invalidates_on_token_change():
    # 1. Glagol identity properties exist & synchronize concurrent mutations cleanly
    g = glagol.Glagol(SimpleNamespace(music_token=""), "192.0.2.1", "dev1", "yandexstation_2")
    assert hasattr(g, "_id_lock")
    observed = []
    errors = []

    def writer():
        try:
            for i in range(100):
                g.host = f"192.0.2.{1 + (i % 5)}"
                g.device_id = f"dev-{i}"
                g.platform = f"plat-{i % 2}"
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                observed.append((g.host, g.device_id, g.platform))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not errors
    assert len(observed) == 100

    # 2. YandexMusic invalidates _uid, _likes, cache when auth token changes
    auth = SimpleNamespace(music_token="token-A")
    ym = glagol.YandexMusic(auth)
    ym._uid = 111
    ym._likes = (time.time(), {"t-old"})
    ym.cache["t-old"] = {"id": "t-old", "title": "Cached"}
    ym._last_token = "token-A"

    seen_tokens = []

    def fake_req(path, query=None, form=None, timeout=15):
        seen_tokens.append(auth.music_token)
        if "/account/status" in path:
            return {"account": {"uid": 222}}
        if "/likes/tracks" in path:
            return {"library": {"tracks": [{"id": "t-new"}]}}
        if "/tracks/" in path:
            return [{"id": "t-new", "title": "Fresh", "artists": [], "albums": [], "available": True}]
        return {}

    ym._req = lambda path, query=None, form=None, timeout=15: (ym._sync_token(), fake_req(path, query, form, timeout))[1]
    auth.music_token = "token-B"

    assert ym.uid() == 222
    assert "t-old" not in ym.cache
    assert ym.liked() == {"t-new"}
    assert seen_tokens == ["token-B", "token-B"]


def test_dead_btout_and_yam_v01_routes_return_404_not_500():
    import api

    for method, route in (
        ("GET", "/api/btout/eq"),
        ("POST", "/api/btout/scan"),
        ("POST", "/api/btout/use"),
        ("POST", "/api/btout/off"),
        ("POST", "/api/btout/eq"),
        ("GET", "/api/yam/status"),
        ("GET", "/api/yam/registry"),
        ("GET", "/api/yam/plugins"),
        ("GET", "/api/yam/logs"),
        ("GET", "/api/yam/settings"),
        ("GET", "/api/yam/version"),
        ("POST", "/api/yam/start"),
        ("POST", "/api/yam/install-daemon"),
        ("POST", "/api/yam/autostart"),
        ("POST", "/api/yam/install"),
        ("POST", "/api/yam/action"),
        ("POST", "/api/yam/settings"),
    ):
        assert api.ROUTER.match(method, route) is None, f"Dangling dead route still registered: {method} {route}"

    assert api.ROUTER.match("GET", "/api/yam2/status") is not None
    assert api.ROUTER.match("POST", "/api/yam2/pkg") is not None

    s = fresh_server()
    acc = Mock()
    acc.session_ok.return_value = True

    def dispatch(method, path):
        h = s.Handler.__new__(s.Handler)
        h.client_address = ("127.0.0.1", 50000)
        h.command = method
        h.path = path
        h.headers = {"Host": "127.0.0.1:8765"} if method == "GET" else {
            "Host": "127.0.0.1:8765",
            "Content-Type": "application/json",
            "Content-Length": "2",
        }
        h.rfile = io.BytesIO(b"{}" if method == "POST" else b"")
        h.access = acc
        h.panel = s.Panel(Mock())
        h.panel.conn = SimpleNamespace(online=True)
        h.sent = []
        h.close_connection = False
        h._send = lambda code, body, ctype="application/json", headers=(): h.sent.append((code, body))
        if method == "GET":
            h.do_GET()
        else:
            h.do_POST()
        return h.sent[-1]

    for method, route in (("GET", "/api/btout/eq"), ("GET", "/api/yam/status"), ("POST", "/api/btout/scan"), ("POST", "/api/yam/start")):
        code, body = dispatch(method, route)
        assert code == 404, f"{method} {route} returned {code} {body}"


def test_agent_proc_closes_ssl_socket_when_handshake_fails():
    for scenario in ("send_oserror", "eof", "http_401", "http_500"):
        raw_sock = Mock()
        ssl_sock = Mock()
        if scenario == "send_oserror":
            ssl_sock.sendall.side_effect = OSError("handshake send failed")
        elif scenario == "eof":
            ssl_sock.recv.side_effect = [b"HTTP/1.1 101 Switching", b""]
        elif scenario == "http_401":
            ssl_sock.recv.side_effect = [b"HTTP/1.1 401 Unauthorized\r\n\r\n"]
        elif scenario == "http_500":
            ssl_sock.recv.side_effect = [b"HTTP/1.1 500 Internal Server Error\r\n\r\n"]

        link = SimpleNamespace(
            host="192.0.2.50",
            port=9090,
            token="tok",
            ctx=SimpleNamespace(wrap_socket=lambda raw: ssl_sock),
        )

        with patch.object(agent.socket, "create_connection", return_value=raw_sock):
            with pytest.raises(agent.AgentError):
                agent.AgentProc(link, "echo hi", False, 80, 24, False)

        ssl_sock.close.assert_called_once()
        raw_sock.close.assert_called_once()
