"""TDD regression tests for Task P3 deep runtime fixes (assistant_max, party, extmusic/*)."""
from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Record original socket.getaddrinfo BEFORE importing extmusic modules so we can
# prove none of them monkey-patch socket.getaddrinfo globally.
_ORIG_GETADDRINFO = socket.getaddrinfo

import assistant_max
import extmusic
from extmusic import fast_yt, google_oauth, gstbridge, vk, vkauth, ytaccount, ytmusic
from extmusic.google_login import GoogleLogin, GoogleLoginError
from extmusic.native_yt import NativeYT
from extmusic.player import PlayerClient
from extmusic.service import ExtMusicService
from extmusic.store import ExtMusicStore
import party


# ---------------------------------------------------------------------------
# 1. assistant_max.py bugs
# ---------------------------------------------------------------------------


def test_glagol_client_thread_safe_fragmented_101_drain_and_token_reload(tmp_path, monkeypatch):
    token_file = tmp_path / ".yandex_auth.json"
    token_file.write_text(json.dumps({"music_token": "tok-v1"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    received_frames = []
    connections = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    stop_srv = threading.Event()

    def _read_ws_frame(conn):
        hdr = conn.recv(2)
        if len(hdr) < 2:
            return None
        b1, b2 = hdr[0], hdr[1]
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(conn.recv(2), "big")
        elif length == 127:
            length = int.from_bytes(conn.recv(8), "big")
        mask = conn.recv(4) if masked else b""
        payload = bytearray()
        while len(payload) < length:
            chunk = conn.recv(length - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if masked:
            payload = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(payload)

    def serve():
        idx = 0
        while not stop_srv.is_set():
            srv.settimeout(0.2)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            idx += 1
            connections.append(conn)
            conn.settimeout(2.0)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                req += chunk
            # Connection 1: reject non-101 even if " 101 " appears inside a header
            if idx == 1:
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nX-Note: 101 \r\n\r\n")
                conn.close()
                continue
            # Connection 2+: fragmented >1024-byte HTTP 101 response + unsolicited server frame
            pad = b"X-Pad: " + (b"A" * 1200) + b"\r\n"
            conn.sendall(b"HTTP/1.1 101 ")
            time.sleep(0.02)
            conn.sendall(b"Switching Protocols\r\n" + pad)
            time.sleep(0.02)
            conn.sendall(b"Upgrade: websocket\r\n\r\n")
            # Send unsolicited server text frame to test draining
            server_msg = b'{"ping":1}'
            conn.sendall(bytes([0x81, len(server_msg)]) + server_msg)
            while not stop_srv.is_set():
                try:
                    data = _read_ws_frame(conn)
                    if not data:
                        break
                    received_frames.append(json.loads(data.decode("utf-8")))
                except Exception:
                    break
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        client = assistant_max.GlagolClient(host="127.0.0.1", port=port)
        assert client.token == "tok-v1"
        assert hasattr(client, "_lock")
        assert hasattr(client, "close")

        # 1. Non-101 response with " 101 " in header must be rejected & socket closed
        assert client.connect(timeout=2.0) is False
        assert client.sock is None

        # 2. Fragmented >1024-byte 101 response must succeed
        assert client.connect(timeout=2.0) is True
        assert client.sock is not None

        # 3. Rotate token on disk, force reconnect via dead socket -> reload token
        token_file.write_text(json.dumps({"music_token": "tok-v2"}), encoding="utf-8")
        client.close()
        assert client.sock is None
        assert client.send_command({"command": "ping"}) is True
        assert client.token == "tok-v2"

        # 4. Concurrent sends must not corrupt frames or race on sock
        def worker(i):
            for j in range(5):
                assert client.send_command({"command": "test", "i": i, "j": j}) is True

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)

        time.sleep(0.1)
        assert len(received_frames) == 21
        assert all(frame["conversationToken"] == "tok-v2" for frame in received_frames)
        client.close()
        assert client.sock is None
    finally:
        stop_srv.set()
        srv.close()
        t.join(timeout=2.0)


def test_assistant_ours_tts_deaf_window_non_alice_vs_alice(tmp_path, monkeypatch):
    d = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    d.status_path = tmp_path / "status.json"

    # 1. Non-Alice custom TTS clears self.ours immediately once speech finishes
    d.cfg["tts"] = {"provider": "openai", "base_url": "http://localhost:11434/v1"}
    entered = threading.Event()
    release = threading.Event()

    def slow_custom(text, tts=None):
        entered.set()
        release.wait(2.0)
        return True

    monkeypatch.setattr(d, "speak_custom", slow_custom)
    t = threading.Thread(target=d.speak, args=("Привет",), daemon=True)
    t.start()
    assert entered.wait(1.0)
    with d.lock:
        assert d.ours is True
        # Non-Alice suppression must be tightly bounded (~<=4.5s), never 20s
        assert 0 < (d.say_until - d.say_at) <= 4.5
    release.set()
    t.join(timeout=2.0)
    with d.lock:
        assert d.ours is False
    assert d._should_suppress_phrase() is False

    # 2. Alice provider failing glagol.say and falling back to speak_google also clears ours
    d.cfg["tts"] = {"provider": "alice"}
    monkeypatch.setattr(d.glagol, "say", lambda _text: False)
    monkeypatch.setattr(d, "speak_google", lambda _text: True)
    d.speak("Фолбэк")
    with d.lock:
        assert d.ours is False
    assert d._should_suppress_phrase() is False

    # 3. Alice provider succeeding keeps ours=True until logcat IDLE (or 20s cap)
    monkeypatch.setattr(d.glagol, "say", lambda _text: True)
    d.speak("Ответ Алисы")
    with d.lock:
        assert d.ours is True
        assert d.say_until >= d.say_at + 19.0
    assert d._should_suppress_phrase() is True


def test_assistant_logcat_cleanup_and_eof_backoff(tmp_path, monkeypatch):
    d = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    d.status_path = tmp_path / "status.json"
    d.glagol.close = Mock()

    sleeps = []
    procs = []
    eof_count = 0

    class FakeLogcatProc:
        def __init__(self, alive_on_wait=False):
            self.stdout = io.StringIO("")
            self.terminated = 0
            self.killed = 0
            self.waits = 0
            self._alive = True
            self._alive_on_wait = alive_on_wait

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated += 1

        def wait(self, timeout=None):
            self.waits += 1
            if self._alive_on_wait:
                raise subprocess.TimeoutExpired("logcat", timeout)
            self._alive = False
            return 0

        def kill(self):
            self.killed += 1
            self._alive = False

    def fake_popen(cmd, **kwargs):
        nonlocal eof_count
        eof_count += 1
        p = FakeLogcatProc()
        procs.append(p)
        if eof_count >= 2:
            d.running = False
        return p

    monkeypatch.setattr(assistant_max.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(assistant_max.time, "sleep", lambda s: sleeps.append(s))

    d.run_logcat_listener()
    assert eof_count == 2
    # First EOF while running=True must back off (~1s) instead of spinning 100% CPU
    assert any(s >= 0.5 for s in sleeps), f"Expected EOF backoff sleep, got {sleeps}"

    # Verify stop() terminates/kills lingering _logcat_proc and closes glagol
    stuck_proc = FakeLogcatProc(alive_on_wait=True)
    d._logcat_proc = stuck_proc
    d.running = True
    d.stop()
    assert d.running is False
    assert stuck_proc.terminated == 1
    assert stuck_proc.killed == 1
    assert d._logcat_proc is None
    d.glagol.close.assert_called_once()


def test_ext_play_youtube_default_unauthenticated_400_fallback(tmp_path, monkeypatch):
    d = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    spoken = []
    calls = []
    monkeypatch.setattr(d, "speak", lambda text: spoken.append(text))

    def fake_ext_call(path, method="GET", body=None, timeout=30):
        calls.append((method, path, body))
        if path.startswith("/api/extmusic/yt/account/liked"):
            raise urllib.error.HTTPError(path, 400, "Bad Request", {}, None)
        if path.startswith("/api/extmusic/yt/account/home"):
            raise urllib.error.HTTPError(path, 400, "Bad Request", {}, None)
        if path.startswith("/api/extmusic/yt/home"):
            return {
                "tracks": [],
                "quick_picks": [{"source": "yt", "id": "qp1", "title": "Pick", "artist": "Art"}],
            }
        if path == "/api/extmusic/play":
            return {"ok": True}
        raise AssertionError(f"Unexpected path: {path}")

    monkeypatch.setattr(d, "_ext_call", fake_ext_call)
    d._ext_play_youtube_default()

    assert spoken == []
    assert [c[1] for c in calls] == [
        "/api/extmusic/yt/account/liked?count=50",
        "/api/extmusic/yt/account/home?count=10",
        "/api/extmusic/yt/home?count=10",
        "/api/extmusic/play",
    ]
    assert calls[-1][2]["track"]["id"] == "qp1"


# ---------------------------------------------------------------------------
# 2. party.py bugs
# ---------------------------------------------------------------------------


def _make_party_manager(tmp_path):
    return party.PartyManager(
        bulbs_config_path=str(tmp_path / "bulbs.json"),
        settings_path=str(tmp_path / "settings.json"),
        strip_config_path=str(tmp_path / "strip.json"),
    )


@pytest.mark.parametrize("mode", ["fireplace", "aurora", "ocean", "lava", "orbit", "police", "cinema"])
def test_audio_loop_silence_timeout_ignored_in_non_party_modes(tmp_path, monkeypatch, mode):
    pm = _make_party_manager(tmp_path)
    pm.running = True
    pm._run_stop.clear()
    pm.settings["active_mode"] = mode
    pm.settings["silence_timeout"] = 5.0
    pm.last_sound_time = time.time() - 100.0
    pm._white_hold_until = 0.0

    restored = []
    monkeypatch.setattr(pm, "restore_white", lambda: restored.append(True))

    chunk = b"\x00" * 2048

    class FakeAudioProc:
        def __init__(self):
            self.calls = 0

            class Out:
                def read(_, n):
                    self.calls += 1
                    if self.calls >= 3:
                        pm._run_stop.set()
                    return chunk

            self.stdout = Out()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(party.subprocess, "Popen", lambda *a, **kw: FakeAudioProc())
    pm._audio_loop(pm._run_generation, pm._run_stop)

    assert restored == []
    assert pm._white_hold_until == 0.0


def test_audio_loop_silence_timeout_applies_in_party_mode_and_handles_odd_pcm(tmp_path, monkeypatch):
    pm = _make_party_manager(tmp_path)
    pm.running = True
    pm._run_stop.clear()
    pm.settings["active_mode"] = "party"
    pm.settings["silence_timeout"] = 5.0
    pm.last_sound_time = time.time() - 100.0
    pm._white_hold_until = 0.0

    restored = []
    monkeypatch.setattr(pm, "restore_white", lambda: restored.append(True))

    odd_chunk = b"\x01\x00\x02\x00" * 512 + b"\x7f"  # 2049 bytes (odd length)

    class FakeAudioProc:
        def __init__(self):
            self.calls = 0

            class Out:
                def read(_, n):
                    self.calls += 1
                    if self.calls >= 2:
                        pm._run_stop.set()
                    return odd_chunk

            self.stdout = Out()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(party.subprocess, "Popen", lambda *a, **kw: FakeAudioProc())
    pm._audio_loop(pm._run_generation, pm._run_stop)

    assert restored == [True]
    assert pm._white_hold_until > time.time() + 3000.0


def test_run_android_cmd_no_preexec_and_uses_selinux_shell(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(party.subprocess, "run", fake_run)
    monkeypatch.setattr(party.os.path, "exists", lambda p: p == "/system/bin/sh")

    party.run_android_cmd(["dumpsys", "media_session"], ctx="u:r:shell:s0", timeout=2.0)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs.get("preexec_fn") is None
    assert args[:2] == ["/system/bin/sh", "-c"]
    assert "u:r:shell:s0" in args[2]
    assert "/proc/self/attr/current" in args[2]
    assert "exec dumpsys media_session" in args[2]

    calls.clear()
    monkeypatch.setattr(party.os.path, "exists", lambda p: False)
    party.run_android_cmd(["dumpsys", "audio"])
    args2, kwargs2 = calls[0]
    assert kwargs2.get("preexec_fn") is None
    assert args2 == ["dumpsys", "audio"]


def test_flash_role_timers_tracked_and_cancelled_on_stop(tmp_path, monkeypatch):
    pm = _make_party_manager(tmp_path)
    pm.running = True
    pm.workers["k1"] = SimpleNamespace(role="kick", name="K", connected=True, device=SimpleNamespace(socket=object()), send_color_val=lambda *a, **k: True, send_white=lambda *a, **k: True, stop=lambda *a, **k: None)

    sent = []
    monkeypatch.setattr(pm, "broadcast_color", lambda workers, val, timeout=0.060, wait=True: sent.append(val))

    pm._flash_role("kick", "HIGH", "LOW", delay=0.15)
    assert sent == ["HIGH"]
    assert len(pm._flash_timers) == 1

    pm.stop()
    assert len(pm._flash_timers) == 0
    time.sleep(0.22)
    # Timer was cancelled on stop, so LOW must never fire afterwards
    assert sent == ["HIGH"]


def test_party_tinycap2_kill_fallback_workers_dict_and_dynamic_site_packages(tmp_path, monkeypatch):
    # 1. tinycap2 proc.kill() fallback when wait() times out
    pm = _make_party_manager(tmp_path)

    class HungProc:
        def __init__(self):
            self.terminated = 0
            self.killed = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated += 1

        def wait(self, timeout=None):
            if self.killed == 0:
                raise subprocess.TimeoutExpired("tinycap2", timeout)
            return -9

        def kill(self):
            self.killed += 1

    hp = HungProc()
    pm.audio_proc = hp
    pm.stop(persist=False)
    assert hp.terminated == 1
    assert hp.killed == 1
    assert pm.audio_proc is None

    # 2. WorkersDict standard dict key semantics
    wd = party.WorkersDict({"b1": "W1", "b2": "W2"})
    assert list(wd) == ["b1", "b2"]
    assert dict(wd) == {"b1": "W1", "b2": "W2"}
    assert list(wd.values()) == ["W1", "W2"]

    # 3. Dynamic python3.* site-packages discovery (supports 3.12, 3.13, etc.)
    sp312 = tmp_path / "data" / "python" / "lib" / "python3.12" / "site-packages"
    sp312.mkdir(parents=True)
    monkeypatch.setattr(party.glob, "glob", lambda pat: [str(sp312)] if "python3.*" in pat else [])
    cands = party._site_packages_candidates()
    assert str(sp312) in cands


# ---------------------------------------------------------------------------
# 3. extmusic/* bugs
# ---------------------------------------------------------------------------


def test_extmusic_service_no_dead_eq_and_status_unlocked_on_radio_next(tmp_path, monkeypatch):
    from extmusic import service as svc_mod, ytdlp

    assert not hasattr(svc_mod, "_EQUALIZER_PATH")
    assert not hasattr(svc_mod, "_DISPLAY_DEV")

    panel = SimpleNamespace(glagol=None, adb=None, music_cmd=lambda d: {"ok": True})
    svc = ExtMusicService(panel)
    svc.store = ExtMusicStore(tmp_path / "extmusic.json")
    svc.yt_account.store = svc.store
    for dead_attr in (
        "_eq_frames", "_eq_was_active", "_eq_stop", "_eq_thread",
        "_eq_pause_clock", "_eq_resume_clock", "_start_eq", "_stop_eq_render", "_stop_eq", "_eq_anim_loop",
    ):
        assert not hasattr(svc, dead_attr), f"Dead attribute still present: {dead_attr}"

    # Verify self.status() is called OUTSIDE self.lock on radio_gen mismatch & radio_error paths
    monkeypatch.setattr(svc.gst, "available", lambda: True)
    monkeypatch.setattr(svc.gst, "ensure_injected", lambda: True)
    monkeypatch.setattr(svc.gst, "play", lambda *a, **k: 1)
    monkeypatch.setattr(svc.gst, "stop", lambda: 1)
    monkeypatch.setattr(svc.gst, "release", lambda: 1)
    monkeypatch.setattr(svc.gst, "player", lambda: {"ok": True, "state": "stopped", "playing": False, "position": 0, "duration": 0, "levels": [], "error": ""})
    monkeypatch.setattr(svc.native, "available", lambda: False)
    monkeypatch.setattr(svc.native, "warmup", lambda: None)
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr("extmusic.fast_yt.fast_resolve", lambda vid, cookies=None: {"url": f"https://rr.example/{vid}.m4a", "duration": 10})

    def assert_lock_unlocked():
        acquired = [False]
        def probe():
            got = svc.lock.acquire(blocking=False)
            acquired[0] = got
            if got:
                svc.lock.release()
        th = threading.Thread(target=probe)
        th.start()
        th.join(timeout=1.0)
        assert acquired[0] is True, "ExtMusicService.lock must NOT be held inside status()"

    orig_status = svc.status
    status_lock_states = []

    def spy_status():
        acquired = [False]
        def probe():
            got = svc.lock.acquire(blocking=False)
            acquired[0] = got
            if got:
                svc.lock.release()
        th = threading.Thread(target=probe)
        th.start()
        th.join(timeout=1.0)
        status_lock_states.append(acquired[0])
        return orig_status()

    try:
        svc.play({"source": "yt", "id": "t1", "title": "T1", "artist": "A"})
        monkeypatch.setattr(svc, "status", spy_status)

        # Path 1: radio_error
        status_lock_states.clear()
        monkeypatch.setattr("extmusic.ytmusic.radio", lambda *a, **k: (_ for _ in ()).throw(ytmusic.YTMusicError("net fail")))
        svc.next(auto=True)
        assert status_lock_states == [True]

        # Path 2: radio_gen changed mid-flight
        svc.play({"source": "yt", "id": "t2", "title": "T2", "artist": "A"})
        status_lock_states.clear()
        entered = threading.Event()
        release = threading.Event()

        def slow_radio(*a, **k):
            entered.set()
            release.wait(2.0)
            return [{"source": "yt", "id": "rad", "title": "R", "artist": "A"}]

        monkeypatch.setattr("extmusic.ytmusic.radio", slow_radio)
        t = threading.Thread(target=svc.next, kwargs={"auto": True})
        t.start()
        assert entered.wait(1.0)
        svc.play({"source": "yt", "id": "t3", "title": "T3", "artist": "A"})
        release.set()
        t.join(timeout=2.0)
        assert all(status_lock_states)
    finally:
        svc.stop()


def test_ytmusic_is_allowed_thumb_url_rejects_kelvin_sign():
    # U+212A (Kelvin sign 'K') lowercases to ASCII 'k' in Python .lower()!
    assert ytmusic.is_allowed_thumb_url("https://\u212a.ytimg.com/vi/abc/0.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://i.ytimg.com\u212a/vi/abc/0.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://foo\u212a.googleusercontent.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://I.YTIMG.COM/vi/abc/0.jpg") is True


def test_gstbridge_no_global_getaddrinfo_patch_locking_close_and_ensure_injected(tmp_path, monkeypatch):
    # 1. Process-wide socket.getaddrinfo must NOT be monkey-patched
    assert socket.getaddrinfo is _ORIG_GETADDRINFO
    assert getattr(socket, "_ipv4_patched", False) is False
    assert getattr(socket, "_ipv4_patched_gst", False) is False

    # 2. GstBridge.close() shuts down proxy server cleanly
    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(tmp_path / "override.txt"))

    class FakeAdb:
        def __init__(self):
            self.calls = []
            self.rc = 0
            self.has_frida = True
            self.has_hook = True
            self.in_mem = False

        def shell(self, cmd, check=False, timeout=10):
            self.calls.append(cmd)
            if "pidof com.yandex.io.sdk" in cmd:
                return "1234\n", "", 0
            if "grep -q frida-agent" in cmd:
                return ("1\n" if self.in_mem else "0\n"), "", 0
            if "frida-inject" in cmd and "-p 1234" in cmd:
                if self.rc != 0:
                    return "", "frida-inject: error", self.rc
                return "injected", "", 0
            if "frida-inject" in cmd:
                return ("/data/local/tmp/frida-inject\n" if self.has_frida else ""), "", (0 if self.has_frida else 1)
            if "hook_location.js" in cmd:
                return ("ok\n" if self.has_hook else ""), "", (0 if self.has_hook else 1)
            return "", "", 0

    panel = SimpleNamespace(adb=FakeAdb(), glagol=SimpleNamespace(send=lambda p: {"ok": True}))
    bridge = gstbridge.GstBridge(panel)
    try:
        assert bridge._server is not None
        bound_port = bridge._server.server_address[1]
        assert bound_port > 0

        # 3. ensure_injected() must NOT set _last_injected_pid or return True if frida-inject missing or rc != 0
        panel.adb.has_frida = False
        assert bridge.ensure_injected() is False
        assert bridge._last_injected_pid is None

        panel.adb.has_frida = True
        panel.adb.rc = 1
        assert bridge.ensure_injected() is False
        assert bridge._last_injected_pid is None

        panel.adb.rc = 0
        monkeypatch.setattr(gstbridge.time, "sleep", lambda _s: None)
        assert bridge.ensure_injected() is True
        assert bridge._last_injected_pid == 1234

        # 4. Stale _init_streamer must not overwrite streamer after release()
        entered = threading.Event()
        release_evt = threading.Event()

        class FakeStreamer:
            def __init__(self, url):
                entered.set()
                release_evt.wait(2.0)
                self.url = url
                self.ready = True
                self.hdr = b""
                self.segments = []
                self.total_size = 100
                self.duration_sec = 10.0

        monkeypatch.setattr(gstbridge, "MP4Streamer", FakeStreamer)
        monkeypatch.setattr(bridge, "_write_override", lambda _u: None)
        bridge.play("https://example.test/stream.m4a", "T", "A", 10)
        assert entered.wait(1.0)
        bridge.release()
        release_evt.set()
        time.sleep(0.05)
        assert bridge.streamer is None
        assert bridge._last_url == ""

        # 5. close() stops the TCP server so the port can be re-bound or refuses connections
        bridge.close()
        assert bridge._server is None
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", bound_port), timeout=0.5):
                pass
    finally:
        bridge.close()


def test_native_yt_and_player_lock_and_close_previous_forward(monkeypatch):
    # 1. PlayerClient closes previous forward when resetting / reconnecting
    closed_ports = []
    next_port = iter([10001, 10002])

    class FakeAdb:
        is_local = False

        def shell(self, cmd, check=False, timeout=10):
            return "", "", 0

        def forward(self, remote_port):
            return next(next_port)

        def close_forward(self, port):
            closed_ports.append(port)
            return True

    panel = SimpleNamespace(adb=FakeAdb())
    player = PlayerClient(panel)
    assert hasattr(player, "_lock")
    player.port = 10000

    monkeypatch.setattr(player, "ensure_assets", lambda: None)
    monkeypatch.setattr(player, "_stop_existing", lambda: None)
    monkeypatch.setattr(player, "_send_once", lambda line, timeout=45.0: {"ok": True})
    player.start()
    assert closed_ports == [10000]

    # When _send fails, it closes the previous port before clearing/reconnecting
    player.port = 10001
    attempts = {"n": 0}

    def flaky_send_once(line, timeout=45.0):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("broken pipe")
        return {"ok": True}

    monkeypatch.setattr(player, "_send_once", flaky_send_once)
    player._started_at = time.time()  # skip restart, just test port reset
    assert player._send("STATUS") == {"ok": True}
    assert closed_ports == [10000, 10001]

    # 2. NativeYT has _lock and closes previous port forward on reconnect/reset
    nat_closed = []

    class FakeNativeAdb(FakeAdb):
        def close_forward(self, port):
            nat_closed.append(port)
            return True

    native = NativeYT(SimpleNamespace(adb=FakeNativeAdb()))
    assert hasattr(native, "_lock")
    native._port = 20001
    monkeypatch.setattr(native, "_send_once", lambda line, timeout=2.0: (_ for _ in ()).throw(OSError("eof")))
    monkeypatch.setattr(native, "_start_daemon", lambda: False)
    with pytest.raises(Exception):
        native._send("STATUS", start=False)
    assert nat_closed == [20001]
    assert native._port is None


def test_google_login_submit_code_thread_guard_and_atomic_account(monkeypatch):
    class Screen:
        def dump(self):
            return "<hierarchy/>"

        def input_foreground(self):
            return True

    class Panel:
        class Adb:
            @staticmethod
            def shell(cmd, timeout=10):
                return "Account {name=user@example.com, type=app.revanced}", "", 0

        adb = Adb()

    login = GoogleLogin(Panel(), Screen())

    # 1. submit_code must reject when _thread is alive
    login.step = "code"
    login._thread = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(GoogleLoginError):
        login.submit_code("123456")

    # 2. _finish updates step='done' and account atomically under lock
    observed = []
    orig_set = login._set

    def spy_set(step, message="", *args, **kwargs):
        orig_set(step, message, *args, **kwargs)
        observed.append(login.status())

    monkeypatch.setattr(login, "_set", spy_set)
    login._finish("user@example.com")
    done_snapshots = [s for s in observed if s["step"] == "done"]
    assert done_snapshots and done_snapshots[0]["account"] == "user@example.com"


def test_gstbridge_proxy_streams_via_ipv4_opener_end_to_end(monkeypatch):
    import http.server
    import struct

    # Build a minimal valid MP4 header + sidx box + media payload
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x00\x00"
    # sidx v0, 1 segment of 12 bytes starting immediately after sidx
    sidx_body = struct.pack(">IIQQHH", 0, 1, 1000, 0, 0, 0)  # placeholder
    sidx_payload = struct.pack(">I", 0) + struct.pack(">II", 1, 1000) + struct.pack(">II", 0, 0) + struct.pack(">HH", 0, 1) + struct.pack(">III", 12, 1000, 0)
    sidx = struct.pack(">I4s", 8 + len(sidx_payload), b"sidx") + sidx_payload
    media = b"MDEADBEEF123"
    blob = ftyp + sidx + media

    class UpstreamHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            rng = self.headers.get("Range", "")
            start, end = 0, len(blob) - 1
            if rng.startswith("bytes="):
                p = rng[6:].split("-")
                if p[0].isdigit():
                    start = int(p[0])
                if len(p) > 1 and p[1].isdigit():
                    end = min(int(p[1]), len(blob) - 1)
            chunk = blob[start:end + 1]
            self.send_response(206 if rng else 200)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(blob)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    up = http.server.HTTPServer(("127.0.0.1", 0), UpstreamHandler)
    up_port = up.server_address[1]
    up_thread = threading.Thread(target=up.serve_forever, daemon=True)
    up_thread.start()

    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda p: {"ok": True}))
    bridge = gstbridge.GstBridge(panel)
    try:
        monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
        monkeypatch.setattr(bridge, "_write_override", lambda _u: None)
        monkeypatch.setattr(bridge, "_init_streamer", lambda _u: None)
        bridge.play(f"http://127.0.0.1:{up_port}/audio.m4a", "T", "A", 10)
        bridge.streamer = gstbridge.MP4Streamer(f"http://127.0.0.1:{up_port}/audio.m4a")
        assert bridge.streamer.ready is True
        proxy_port = bridge._server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{proxy_port}/stream.mp4?seek=0")
        with extmusic.ipv4_urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.read() == blob
    finally:
        bridge.close()
        up.shutdown()
        up.server_close()


def test_native_yt_stale_status_does_not_clobber_play_and_close_forward_modes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class FakeAdb:
        adb = "pyadb"
        is_local = False

        @staticmethod
        def shell(cmd, check=False, timeout=10):
            return "package:app.rvx.android.apps.youtube.music\n", "", 0

    native = NativeYT(SimpleNamespace(adb=FakeAdb()))
    monkeypatch.setattr(native, "warmup", lambda: None)
    monkeypatch.setattr(native, "_ensure_playing", lambda: None)
    monkeypatch.setattr(native, "_send", lambda line, timeout=2.0, start=False: (entered.set(), release.wait(2.0), {"ok": True, "state": 3, "playing": True, "title": "Old"})[2])

    res_holder = {}

    def bg_status():
        res_holder["st"] = native.status(force=True)

    t = threading.Thread(target=bg_status)
    t.start()
    assert entered.wait(1.0)
    native.play("dQw4w9WgXcQ")
    release.set()
    t.join(timeout=2.0)
    # In-flight status that started before play() must NOT repopulate self._status cache
    assert native._status == {}

    # close_adb_forward delegates to pyadb.close_forward when adb == "pyadb"
    import pyadb
    closed = []
    monkeypatch.setattr(pyadb, "close_forward", lambda port: closed.append(port) or True)
    assert extmusic.close_adb_forward(FakeAdb(), 12345) is True
    assert closed == [12345]


def test_glagol_drain_sets_nonblocking_and_restores_timeout(monkeypatch):
    import select as _select

    class FakeSock:
        def __init__(self, timeout=2.0, behavior=None, raise_on_restore=False):
            self.timeout = timeout
            self.calls = []
            self.closed = False
            self.behavior = list(behavior or [])
            self.raise_on_restore = raise_on_restore

        def fileno(self):
            return 42

        def gettimeout(self):
            return self.timeout

        def settimeout(self, val):
            self.calls.append(val)
            if self.closed and self.raise_on_restore:
                raise OSError("bad file descriptor")
            self.timeout = val

        def recv(self, n):
            if self.behavior:
                nxt = self.behavior.pop(0)
                if isinstance(nxt, BaseException):
                    raise nxt
                return nxt
            return b""

        def close(self):
            self.closed = True

    client = assistant_max.GlagolClient(host="127.0.0.1", port=1961)

    # 1. Normal drain switches to 0.0 and restores original timeout (2.0)
    sock_ok = FakeSock(timeout=2.0, behavior=[b"ping"])
    client.sock = sock_ok
    seq_ok = [[sock_ok], []]
    monkeypatch.setattr(_select, "select", lambda r, w, x, t: (seq_ok.pop(0) if seq_ok else [], [], []))
    client._drain_locked(raise_on_eof=True)
    assert sock_ok.calls == [0.0, 2.0]
    assert sock_ok.timeout == 2.0
    assert sock_ok.closed is False

    # 2. TimeoutError / BlockingIOError during recv exits cleanly and restores timeout
    for exc in (TimeoutError("timed out"), BlockingIOError(11, "EAGAIN")):
        sock_err = FakeSock(timeout=2.0, behavior=[exc])
        client.sock = sock_err
        monkeypatch.setattr(_select, "select", lambda r, w, x, t, s=sock_err: ([s], [], []))
        client._drain_locked(raise_on_eof=True)
        assert sock_err.calls == [0.0, 2.0]
        assert sock_err.timeout == 2.0
        assert sock_err.closed is False

    # 3. EOF on closed socket where settimeout restore raises OSError is handled safely
    sock_eof = FakeSock(timeout=1.5, behavior=[b""], raise_on_restore=True)
    client.sock = sock_eof
    monkeypatch.setattr(_select, "select", lambda r, w, x, t, s=sock_eof: ([s], [], []))
    with pytest.raises(ConnectionError):
        client._drain_locked(raise_on_eof=True)
    assert sock_eof.closed is True
    assert sock_eof.calls == [0.0, 1.5]


def test_gstbridge_release_on_inject_failure_and_service_switch_away(tmp_path, monkeypatch):
    class SpyGlagol:
        def __init__(self):
            self.state = None
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)
            return {"status": "SUCCESS"}

    class FakePlayer:
        def __init__(self):
            self.port = 9999
            self.played = []
            self.stopped = 0

        def play(self, url):
            self.played.append(url)
            return {"state": "playing"}

        def stop(self):
            self.stopped += 1
            return {"state": "stopped"}

        def status(self):
            return {"ok": True, "state": "playing"}

    panel = SimpleNamespace(glagol=SpyGlagol())
    overrides = []
    injected = {"ok": True}
    monkeypatch.setattr(gstbridge.GstBridge, "_ensure_server", lambda self: None)
    monkeypatch.setattr(gstbridge.GstBridge, "available", lambda self, max_age=8.0: injected["ok"])
    monkeypatch.setattr(gstbridge.GstBridge, "ensure_injected", lambda self: injected["ok"])
    monkeypatch.setattr(gstbridge.GstBridge, "_write_override", lambda self, url: overrides.append(url))
    monkeypatch.setattr(gstbridge.GstBridge, "_init_streamer", lambda self, url: None)

    # 1. Direct GstBridge.play() failure on ensure_injected clears stale override_url & _is_active
    bridge = gstbridge.GstBridge(panel)
    bridge.play("https://rr.example/t1.m4a", "T1", "A1", 120)
    assert bridge._is_active is True
    assert overrides[-1] == "https://rr.example/t1.m4a"

    injected["ok"] = False
    with pytest.raises(gstbridge.GstBridgeError):
        bridge.play("https://rr.example/t2.m4a", "T2", "A2", 120)
    assert bridge._is_active is False
    assert bridge._last_url == ""
    assert overrides[-1] == ""

    with pytest.raises(gstbridge.GstBridgeError):
        bridge.seek(5000)

    # 2. Service switching from active GST -> VK clears override & stops Glagol
    from extmusic import ytdlp
    injected["ok"] = True
    svc = ExtMusicService(panel)
    svc.store = ExtMusicStore(tmp_path / "extmusic.json")
    svc.store.save_vk("vk-tok", "VKAndroidApp/1.0", user_id="1", name="Tester")
    svc.yt_account.store = svc.store
    svc.player = FakePlayer()
    monkeypatch.setattr(svc.native, "available", lambda: False)
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(
        fast_yt,
        "fast_resolve",
        lambda video_id, cookies=None: {"url": f"https://rr.example/{video_id}.m4a", "duration": 180},
    )
    monkeypatch.setattr(
        ytdlp,
        "resolve",
        lambda video_id, **kwargs: {"url": f"https://rr.example/{video_id}.m4a", "duration": 180},
    )

    try:
        svc.play({"source": "yt", "id": "yt1", "title": "YT1", "artist": "A"})
        assert svc.gst._is_active is True
        assert overrides[-1] == "https://rr.example/yt1.m4a"
        panel.glagol.sent.clear()

        svc.play({"source": "vk", "id": "42", "owner_id": "10", "title": "VK1", "artist": "A", "url": "https://vk.example/42.mp3"})
        assert svc.gst._is_active is False
        assert overrides[-1] == ""
        assert {"command": "stop"} in panel.glagol.sent
        assert svc.player.played[-1] == "https://vk.example/42.mp3"

        # 3. Active GST -> fallback to PlayerClient on GST failure clears override & releases GST
        svc.play({"source": "yt", "id": "yt2", "title": "YT2", "artist": "A"})
        assert svc.gst._is_active is True
        assert overrides[-1] == "https://rr.example/yt2.m4a"
        panel.glagol.sent.clear()

        monkeypatch.setattr(
            fast_yt,
            "fast_resolve",
            lambda video_id, cookies=None: (_ for _ in ()).throw(fast_yt.FastYTError("gst err")),
        )

        def resolve_yt_fallback(video_id, **kwargs):
            if kwargs.get("fast"):
                raise ytdlp.YTDLPError("fast failed")
            return {"url": f"https://rr.example/{video_id}_fallback.m4a", "duration": 180}

        monkeypatch.setattr(ytdlp, "resolve", resolve_yt_fallback)
        svc.play({"source": "yt", "id": "yt3", "title": "YT3", "artist": "A"})
        assert svc.gst._is_active is False
        assert overrides[-1] == ""
        assert {"command": "stop"} in panel.glagol.sent
        assert svc.player.played[-1] == "https://rr.example/yt3_fallback.m4a"

        # 4. Active GST -> fallback to PlayerClient when gst.available() turns False clears override & stops Glagol
        monkeypatch.setattr(
            fast_yt,
            "fast_resolve",
            lambda video_id, cookies=None: {"url": f"https://rr.example/{video_id}.m4a", "duration": 180},
        )
        monkeypatch.setattr(
            ytdlp,
            "resolve",
            lambda video_id, **kwargs: {"url": f"https://rr.example/{video_id}.m4a", "duration": 180},
        )
        svc.play({"source": "yt", "id": "yt4", "title": "YT4", "artist": "A"})
        assert svc.gst._is_active is True
        assert overrides[-1] == "https://rr.example/yt4.m4a"
        panel.glagol.sent.clear()

        injected["ok"] = False
        svc.play({"source": "yt", "id": "yt5", "title": "YT5", "artist": "A"})
        assert svc.gst._is_active is False
        assert overrides[-1] == ""
        assert {"command": "stop"} in panel.glagol.sent
        assert svc.player.played[-1] == "https://rr.example/yt5.m4a"

        # 5. ExtMusicService.prev() releases self.lock before calling status() / seek()
        svc.queue = []
        other_acquired = []
        orig_status = svc.status

        def spy_status():
            th = threading.Thread(
                target=lambda: other_acquired.append(svc.lock.acquire(timeout=0.2) and (svc.lock.release() or True))
            )
            th.start()
            th.join(timeout=1.0)
            return orig_status()

        monkeypatch.setattr(svc, "status", spy_status)
        svc.prev()
        assert other_acquired == [True]
    finally:
        svc.stop()


def test_ytmusic_vk_vkauth_use_ipv4_opener_and_honor_urlopen_mocks(monkeypatch):
    # Ensure required IPv4 opener helpers are exported
    assert hasattr(extmusic, "_IPv4HTTPHandler")
    assert hasattr(extmusic, "_IPv4HTTPSHandler")
    assert callable(getattr(extmusic, "build_ipv4_opener", None))

    class FakeResp:
        def __init__(self, body, status=200, headers=None, url="https://i.ytimg.com/vi/abc/hq.jpg"):
            self._body = body
            self.status = status
            self.headers = headers or {"Content-Type": "application/json"}
            self._url = url

        def read(self, n=-1):
            return self._body

        def geturl(self):
            return self._url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    # 1. Unmocked urllib.request.urlopen -> routes through scoped IPv4 opener(s)
    opener_calls = []
    build_calls = []

    class SpyOpener:
        def __init__(self, *handlers):
            self.handlers = handlers

        def open(self, req, timeout=10.0):
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            build_calls.append((url, tuple(self.handlers)))
            return FakeResp(b"IMGDATA", headers={"Content-Type": "image/jpeg"}, url=url)

    def fake_ipv4_open(req, timeout=10.0):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        opener_calls.append(url)
        if "music.youtube.com" in url:
            return FakeResp(b'{"contents":{}}')
        if "api.vk.com/method/" in url:
            if "auth.validatePhone" in url:
                return FakeResp(b'{"ok":true}')
            return FakeResp(b'{"response":{"items":[]}}')
        return FakeResp(b"{}")

    monkeypatch.setattr(extmusic._IPV4_OPENER, "open", fake_ipv4_open)
    orig_build_ipv4 = extmusic.build_ipv4_opener

    def spy_build_ipv4(*handlers):
        # Also validate real handler assembly includes _IPv4HTTPHandler/_IPv4HTTPSHandler + custom handlers
        real = orig_build_ipv4(*handlers)
        types = tuple(type(h) for h in real.handlers)
        assert extmusic._IPv4HTTPHandler in types
        assert extmusic._IPv4HTTPSHandler in types
        for h in handlers:
            assert h in types
        return SpyOpener(*handlers)

    monkeypatch.setattr(extmusic, "build_ipv4_opener", spy_build_ipv4)

    assert ytmusic._post({"browseId": "FEmusic_home"}, endpoint="browse") == {"contents": {}}
    body, ctype = ytmusic.fetch_thumb("https://i.ytimg.com/vi/abc/hq.jpg")
    assert body == b"IMGDATA" and ctype == "image/jpeg"
    assert vk.VKClient("tok")._call("audio.search", {"q": "rock"}) == {"items": []}
    assert vkauth._post_form("https://api.vk.com/method/auth.validatePhone", {"sid": "1"}, vkauth.USER_AGENT) == {"ok": True}

    assert len(opener_calls) == 3
    assert any("music.youtube.com" in u for u in opener_calls)
    assert sum("api.vk.com" in u for u in opener_calls) == 2
    assert len(build_calls) == 1 and ytmusic._SafeThumbRedirectHandler in build_calls[0][1]

    # 2. Mocked urllib.request.urlopen -> honored across all 4 call paths
    mock_calls = []

    def mock_urlopen(req, timeout=10.0):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        mock_calls.append(url)
        if "music.youtube.com" in url:
            return FakeResp(b'{"mocked":true}')
        if "ytimg.com" in url:
            return FakeResp(b"MOCKIMG", headers={"Content-Type": "image/png"}, url=url)
        if "auth.validatePhone" in url:
            return FakeResp(b'{"mock_auth":true}')
        return FakeResp(b'{"response":{"mock_vk":true}}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    assert ytmusic._post({"browseId": "FEmusic_home"}, endpoint="browse") == {"mocked": True}
    body2, ctype2 = ytmusic.fetch_thumb("https://i.ytimg.com/vi/abc/hq.jpg")
    assert body2 == b"MOCKIMG" and ctype2 == "image/png"
    assert vk.VKClient("tok")._call("audio.search", {"q": "rock"}) == {"mock_vk": True}
    assert vkauth._post_form("https://api.vk.com/method/auth.validatePhone", {"sid": "1"}, vkauth.USER_AGENT) == {"mock_auth": True}
    assert len(mock_calls) == 4
