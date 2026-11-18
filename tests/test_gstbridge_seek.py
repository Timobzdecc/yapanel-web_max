"""Regression tests for YouTube Music seeking via GstBridge and hook_location.js."""
from __future__ import annotations

import http.server
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extmusic import gstbridge  # noqa: E402
from extmusic.service import ExtMusicService  # noqa: E402
from extmusic.store import ExtMusicStore  # noqa: E402


def _make_sidx(timescale: int, entries: list[tuple[int, int]], version: int = 0) -> bytes:
    body = bytearray()
    body += struct.pack(">I", version << 24)
    body += struct.pack(">II", 1, timescale)
    if version == 0:
        body += struct.pack(">II", 0, 0)
    else:
        body += struct.pack(">QQ", 0, 0)
    body += struct.pack(">HH", 0, len(entries))
    for seg_size, seg_dur in entries:
        body += struct.pack(">III", seg_size & 0x7FFFFFFF, seg_dur, 0)
    return struct.pack(">I4s", 8 + len(body), b"sidx") + bytes(body)


def _make_tfdt_box(decode_time: int, version: int = 0) -> bytes:
    if version == 0:
        return struct.pack(">I4sII", 16, b"tfdt", 0, decode_time)
    return struct.pack(">I4sIQ", 20, b"tfdt", 1 << 24, decode_time)


def _build_fragmented_mp4(seg_count: int = 2, seg_dur: int = 5000, timescale: int = 1000) -> tuple[bytes, bytes, list[bytes]]:
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\x00\x00\x00\x00"
    moov = struct.pack(">I4s", 24, b"moov") + (b"\x00" * 16)
    segs: list[bytes] = []
    entries: list[tuple[int, int]] = []
    for idx in range(seg_count):
        tfdt = _make_tfdt_box(idx * seg_dur, version=0 if idx == 0 else 1)
        payload = (b"A" * 19) + tfdt + (b"B" * 23)
        segs.append(payload)
        entries.append((len(payload), seg_dur))
    sidx = _make_sidx(timescale, entries)
    hdr = ftyp + moov
    return hdr + sidx + b"".join(segs), hdr, segs


def test_mp4streamer_retries_transient_errors_and_handles_large_sidx(monkeypatch):
    blob, hdr, _ = _build_fragmented_mp4(seg_count=850, seg_dur=1000, timescale=1000)
    assert len(hdr) + 850 * 12 > 8192

    attempts = {"n": 0}

    class FakeResp:
        def __init__(self, payload: bytes, start: int, end: int):
            self._payload = payload
            self.headers = {"Content-Range": f"bytes {start}-{end}/{len(blob)}"}

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=10):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise urllib.error.URLError(TimeoutError("_ssl.c:1001: The handshake operation timed out"))
        rng = req.headers.get("Range", "")
        assert rng.startswith("bytes=")
        lo_s, hi_s = rng[6:].split("-")
        lo = int(lo_s)
        hi = min(int(hi_s), len(blob) - 1)
        return FakeResp(blob[lo:hi + 1], lo, hi)

    monkeypatch.setattr(gstbridge, "ipv4_urlopen", fake_urlopen)
    streamer = gstbridge.MP4Streamer("https://rr6---sn-test.googlevideo.com/videoplayback?clen=" + str(len(blob)))
    assert streamer.ready is True
    assert len(streamer.segments) == 850
    byte_off, actual_sec, base_dt = streamer.get_seek_offset(600.0)
    assert actual_sec == 600.0
    assert byte_off > len(hdr)
    assert base_dt == 600000


def test_gstbridge_failed_init_does_not_poison_or_clobber_seek(monkeypatch, tmp_path):
    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(tmp_path / "override.txt"))
    monkeypatch.setattr(gstbridge, "USER_SEEK_FLAG", str(tmp_path / "user_seek.txt"))

    glagol_cmds = []
    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda p: glagol_cmds.append(dict(p)) or {"ok": True}, state={}))
    bridge = gstbridge.GstBridge(panel)
    try:
        monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
        monkeypatch.setattr(gstbridge.time, "sleep", lambda _s: None)

        # Case A: background _init_streamer fails; later seek() must retry and succeed
        outcomes = iter([False, True])

        class SequencedStreamer:
            def __init__(self, url):
                ok = next(outcomes)
                self.url = url
                self.ready = ok
                self.hdr = b"hdr" if ok else b""
                self.segments = [(0.0, 100, 50, 0), (30.0, 150, 50, 30000)] if ok else []
                self.total_size = 200 if ok else 0
                self.duration_sec = 60.0 if ok else 0.0

            def snapshot(self):
                return self.ready, self.hdr, self.total_size, self.duration_sec, list(self.segments)

            def get_seek_offset(self, target_sec):
                return 150, 30.0, 30000

        monkeypatch.setattr(gstbridge, "MP4Streamer", SequencedStreamer)
        bridge.play("https://example.test/videoplayback?itag=140", "T", "A", 60)
        assert bridge._streamer_done.wait(2.0)

        glagol_cmds.clear()
        bridge.seek(35000)
        assert bridge.streamer is not None and bridge.streamer.ready is True
        assert any(c.get("command") == "playMusic" for c in glagol_cmds)
        assert not any(c.get("command") == "rewind" for c in glagol_cmds)

        # Case B: slow stale _init_streamer returning ready=False must NOT overwrite ready streamer from seek()
        init_entered = threading.Event()
        init_release = threading.Event()

        class RacingStreamer:
            call_no = 0

            def __init__(self, url):
                RacingStreamer.call_no += 1
                self.url = url
                if RacingStreamer.call_no == 1:
                    init_entered.set()
                    init_release.wait(2.0)
                    self.ready = False
                    self.hdr = b""
                    self.segments = []
                    self.total_size = 0
                    self.duration_sec = 0.0
                else:
                    self.ready = True
                    self.hdr = b"hdr"
                    self.segments = [(0.0, 100, 50, 0), (40.0, 150, 50, 40000)]
                    self.total_size = 200
                    self.duration_sec = 80.0

            def snapshot(self):
                return self.ready, self.hdr, self.total_size, self.duration_sec, list(self.segments)

            def get_seek_offset(self, target_sec):
                return 150, 40.0, 40000

        monkeypatch.setattr(gstbridge, "MP4Streamer", RacingStreamer)
        bridge.play("https://example.test/videoplayback?itag=140", "T2", "A", 80)
        assert init_entered.wait(1.0)
        glagol_cmds.clear()

        # Run seek while background init is still in-flight; once init finishes ready=False, seek still works
        def release_soon():
            time.sleep(0.05)
            init_release.set()

        t_rel = threading.Thread(target=release_soon, daemon=True)
        t_rel.start()
        bridge.seek(45000)
        assert bridge._streamer_done.wait(2.0)
        assert bridge.streamer is not None and bridge.streamer.ready is True

        glagol_cmds.clear()
        bridge.seek(45000)
        assert any(c.get("command") == "playMusic" for c in glagol_cmds)
        assert not any(c.get("command") == "rewind" for c in glagol_cmds)
    finally:
        bridge.close()


def test_gstbridge_seek_clears_paused_state_and_repeated_pause_keeps_position(monkeypatch, tmp_path):
    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(tmp_path / "override.txt"))
    monkeypatch.setattr(gstbridge, "USER_SEEK_FLAG", str(tmp_path / "user_seek.txt"))

    now = {"t": 1000.0}
    monkeypatch.setattr(gstbridge.time, "time", lambda: now["t"])
    monkeypatch.setattr(gstbridge.time, "sleep", lambda _s: None)

    class ReadyStreamer:
        def __init__(self, url):
            self.url = url
            self.ready = True
            self.hdr = b"hdr"
            self.segments = [(0.0, 100, 50, 0), (60.0, 160, 50, 60000)]
            self.total_size = 210
            self.duration_sec = 120.0

        def snapshot(self):
            return self.ready, self.hdr, self.total_size, self.duration_sec, list(self.segments)

        def get_seek_offset(self, target_sec):
            return 160, 60.0, 60000

    monkeypatch.setattr(gstbridge, "MP4Streamer", ReadyStreamer)
    glagol_state = {"playing": False}

    def fake_send(payload):
        if payload.get("command") == "stop":
            glagol_state["playing"] = False
        return {"ok": True}

    panel = SimpleNamespace(glagol=SimpleNamespace(send=fake_send, state=glagol_state))
    bridge = gstbridge.GstBridge(panel)
    try:
        monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
        bridge.play("https://example.test/stream.m4a", "T", "A", 120)
        bridge._streamer_done.wait(1.0)
        p0 = bridge.player()
        assert p0["playing"] is True
        assert p0["state"] == "playing"

        now["t"] += 15.0
        bridge.pause()
        p1 = bridge.player()
        assert p1["playing"] is False
        assert p1["position"] == 15000

        # Calling pause() again 50s later must NOT advance paused_pos
        now["t"] += 50.0
        bridge.pause()
        p2 = bridge.player()
        assert p2["playing"] is False
        assert p2["position"] == 15000

        # Seeking while paused starts playMusic and must clear _is_paused
        bridge.seek(65000)
        p3 = bridge.player()
        assert p3["playing"] is True
        assert p3["state"] == "playing"
        assert p3["position"] == 60000

        # Seek back to 0 while paused also resumes playing cleanly
        bridge.pause()
        assert bridge.player()["playing"] is False
        bridge.seek(0)
        p4 = bridge.player()
        assert p4["playing"] is True
        assert p4["state"] == "playing"
        assert p4["position"] == 0
    finally:
        bridge.close()


def test_gstbridge_proxy_retries_upstream_and_patches_tfdt_across_chunk_boundary(monkeypatch, tmp_path):
    blob, hdr, segs = _build_fragmented_mp4(seg_count=2, seg_dur=5000, timescale=1000)
    seg1_off = len(hdr) + (len(blob) - len(hdr) - sum(len(s) for s in segs))

    # Align seg[1] in expected chunked upstream stream and split across small 7-byte reads so tfdt spans boundaries
    attempts = {"n": 0}

    class ChunkedStream:
        def __init__(self, data: bytes):
            self._data = data
            self._pos = 0

        def read(self, n: int = -1):
            if self._pos >= len(self._data):
                return b""
            step = 7 if n < 0 else min(7, n, len(self._data) - self._pos)
            chunk = self._data[self._pos:self._pos + step]
            self._pos += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class HeaderStream(ChunkedStream):
        def __init__(self, data: bytes, start: int, end: int):
            super().__init__(data)
            self.headers = {"Content-Range": f"bytes {start}-{end}/{len(blob)}"}

        def read(self, n: int = -1):
            if n < 0:
                rest = self._data[self._pos:]
                self._pos = len(self._data)
                return rest
            return super().read(n)

    def fake_urlopen(req, timeout=10):
        rng = req.headers.get("Range", "")
        lo_s, hi_s = rng[6:].split("-")
        lo = int(lo_s)
        req_hi = int(hi_s)
        hi = min(req_hi, len(blob) - 1)
        if lo == 0 and req_hi >= 8192:
            return HeaderStream(blob[lo:hi + 1], lo, hi)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.URLError(TimeoutError("upstream handshake stall"))
        return ChunkedStream(blob[lo:hi + 1])

    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(tmp_path / "override.txt"))
    monkeypatch.setattr(gstbridge, "USER_SEEK_FLAG", str(tmp_path / "user_seek.txt"))
    monkeypatch.setattr(gstbridge, "ipv4_urlopen", fake_urlopen)

    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda _p: {"ok": True}, state={"playing": True}))
    bridge = gstbridge.GstBridge(panel)
    try:
        monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
        bridge.play("https://example.test/videoplayback?clen=" + str(len(blob)), "T", "A", 10)
        bridge._streamer_done.wait(1.0)
        bridge.seek(5000)

        port = bridge._server.server_address[1]
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream.mp4?seek=5.0", timeout=5)
        body = resp.read()
        assert attempts["n"] == 2
        assert body.startswith(hdr)
        tail = body[len(hdr):]
        # Patched tfdt ver=1 decode time in segment 1 must be subtracted from 5000 -> 0
        idx = tail.find(b"tfdt")
        assert idx >= 4
        ver = tail[idx + 4]
        assert ver == 1
        patched_dt = struct.unpack(">Q", tail[idx + 8:idx + 16])[0]
        assert patched_dt == 0
        assert len(tail) == len(segs[1])
    finally:
        bridge.close()


def test_gstbridge_fallback_sets_user_seek_flag_and_service_updates_ext_started_at(monkeypatch, tmp_path):
    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(tmp_path / "override.txt"))
    user_seek = tmp_path / "user_seek.txt"
    monkeypatch.setattr(gstbridge, "USER_SEEK_FLAG", str(user_seek))

    class NoSidxStreamer:
        def __init__(self, url):
            self.url = url
            self.ready = False
            self.hdr = b""
            self.segments = []
            self.total_size = 0
            self.duration_sec = 0.0

        def snapshot(self):
            return False, b"", 0, 0.0, []

    glagol_cmds = []
    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda p: glagol_cmds.append(dict(p)) or {"ok": True}, state={"playing": True}))
    monkeypatch.setattr(gstbridge, "MP4Streamer", NoSidxStreamer)
    monkeypatch.setattr(gstbridge.time, "sleep", lambda _s: None)

    svc = ExtMusicService(panel)
    svc.store = ExtMusicStore(tmp_path / "extmusic.json")
    svc.yt_account.store = svc.store
    svc.gst.ensure_injected = lambda: True
    try:
        svc.current = {"source": "yt", "id": "abc", "url": "https://example.test/no_sidx", "gst": True, "duration": 180}
        svc.gst._last_url = "https://example.test/no_sidx"
        svc.gst._is_active = True
        svc._ext_started_at = 1.0

        svc.seek(42000)
        assert user_seek.read_text(encoding="utf-8") == "1"
        assert any(c == {"command": "rewind", "position": 42.0} for c in glagol_cmds)
        assert svc._ext_started_at > 1.0
    finally:
        svc.gst.close()


def test_hook_location_js_strips_aesctrdec_and_has_no_hardcoded_offset():
    js_path = ROOT / "device" / "extmusic" / "hook_location.js"
    text = js_path.read_text(encoding="utf-8")
    assert "gst_parse_launch" in text
    assert "aesctrdec" in text
    assert "user_seek.txt" in text
    assert "gst_element_seek" in text
    assert "0x2daeb3c" not in text
    assert "libquasar_daemons" not in text


def test_open_range_sends_visionos_user_agent_and_falls_back_on_403(monkeypatch):
    from extmusic.fast_yt import VISIONOS_USER_AGENT

    seen_uas = []

    class OkResp:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=3.5):
        seen_uas.append(req.headers.get("User-agent"))
        if len(seen_uas) == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return OkResp()

    monkeypatch.setattr(gstbridge, "ipv4_urlopen", fake_urlopen)
    with gstbridge._open_range("https://example.test/videoplayback", 0, 100, attempts=3) as resp:
        assert resp.read() == b"ok"
    assert seen_uas[0] == VISIONOS_USER_AGENT
    assert seen_uas[1] is None


def test_gstbridge_play_and_seek_zero_route_via_proxy_and_stream_exact_bytes(monkeypatch, tmp_path):
    payload = b"0123456789ABCDEF" * 77  # 1232 bytes, smaller than old hardcoded 5000000 fallback
    monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
    ov_file = tmp_path / "override.txt"
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(ov_file))
    monkeypatch.setattr(gstbridge, "USER_SEEK_FLAG", str(tmp_path / "user_seek.txt"))
    monkeypatch.setattr(gstbridge.time, "sleep", lambda _s: None)

    class UpstreamResp:
        def __init__(self, chunk: bytes, start: int, end: int, total: int):
            self._chunk = chunk
            self._pos = 0
            self.headers = {
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Length": str(len(chunk)),
            }

        def read(self, n: int = -1):
            if self._pos >= len(self._chunk):
                return b""
            if n < 0:
                res = self._chunk[self._pos:]
                self._pos = len(self._chunk)
                return res
            res = self._chunk[self._pos:self._pos + n]
            self._pos += len(res)
            return res

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=10):
        rng = req.headers.get("Range", "bytes=0-")
        lo_s, hi_s = rng[6:].split("-", 1)
        lo = int(lo_s) if lo_s.isdigit() else 0
        hi = min(int(hi_s), len(payload) - 1) if hi_s.isdigit() else len(payload) - 1
        if lo >= len(payload):
            raise urllib.error.HTTPError(req.full_url, 416, "Range Not Satisfiable", {}, None)
        return UpstreamResp(payload[lo:hi + 1], lo, hi, len(payload))

    monkeypatch.setattr(gstbridge, "ipv4_urlopen", fake_urlopen)
    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda _p: {"ok": True}, state={"playing": False}))
    bridge = gstbridge.GstBridge(panel)
    try:
        monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
        # Prevent background init from finishing first to verify seek=0 works before sidx is ready
        gate = threading.Event()
        monkeypatch.setattr(bridge, "_init_streamer", lambda _u: gate.wait(0.5))

        bridge.play("https://rr2.googlevideo.com/videoplayback?itag=140", "T", "A", 30)
        port = bridge._server.server_address[1]
        ov1 = ov_file.read_text(encoding="utf-8")
        assert ov1.startswith(f"http://127.0.0.1:{port}/stream.mp4?seek=0")

        # Simulate seek back to 0:00 via GstBridge
        bridge.seek(0)
        ov2 = ov_file.read_text(encoding="utf-8")
        assert ov2.startswith(f"http://127.0.0.1:{port}/stream.mp4?seek=0")
        gate.set()

        # Verify GET, open-ended Range, HEAD, and out-of-range 416 without sidx / clen
        bridge.streamer = None
        r1 = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream.mp4?seek=0", timeout=5)
        assert r1.headers.get("Content-Length") == str(len(payload))
        assert r1.read() == payload

        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/stream.mp4?seek=0",
            headers={"Range": "bytes=100-"},
        )
        r2 = urllib.request.urlopen(req2, timeout=5)
        assert r2.status == 206
        assert r2.headers.get("Content-Range") == f"bytes 100-{len(payload) - 1}/{len(payload)}"
        assert r2.headers.get("Content-Length") == str(len(payload) - 100)
        assert r2.read() == payload[100:]

        req_head = urllib.request.Request(f"http://127.0.0.1:{port}/stream.mp4?seek=0", method="HEAD")
        rh = urllib.request.urlopen(req_head, timeout=5)
        assert rh.headers.get("Content-Length") == str(len(payload))

        req_416 = urllib.request.Request(
            f"http://127.0.0.1:{port}/stream.mp4?seek=0",
            headers={"Range": "bytes=9999-"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_416, timeout=5)
        assert exc_info.value.code == 416
    finally:
        bridge.close()


def test_get_service_is_thread_safe_and_binds_single_proxy(monkeypatch, tmp_path):
    """Concurrent get_service(panel) calls must not leak a duplicate GstBridge onto PROXY_PORT."""
    from concurrent.futures import ThreadPoolExecutor
    from extmusic import service as extmusic_service

    # Use an ephemeral port as PROXY_PORT so host state never interferes
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(gstbridge, "PROXY_PORT", free_port)

    created_bridges = []
    orig_gst_init = gstbridge.GstBridge.__init__
    entered = threading.Event()
    proceed = threading.Event()

    def slow_gst_init(self, panel):
        orig_gst_init(self, panel)
        created_bridges.append(self)

    orig_yt_account = extmusic_service.YTAccount

    def delayed_yt_account(*args, **kwargs):
        # Pause first thread inside ExtMusicService.__init__ after GstBridge bound PROXY_PORT
        entered.set()
        proceed.wait(timeout=2.0)
        return orig_yt_account(*args, **kwargs)

    monkeypatch.setattr(gstbridge.GstBridge, "__init__", slow_gst_init)
    monkeypatch.setattr(extmusic_service, "YTAccount", delayed_yt_account)
    monkeypatch.setattr(
        extmusic_service,
        "ExtMusicStore",
        lambda *a, **kw: ExtMusicStore(tmp_path / "extmusic.json"),
    )

    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda _p: {"ok": True}, state={"playing": False}))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(extmusic_service.get_service, panel)
            assert entered.wait(timeout=2.0)
            f2 = pool.submit(extmusic_service.get_service, panel)
            time.sleep(0.08)
            proceed.set()
            s1 = f1.result(timeout=5.0)
            s2 = f2.result(timeout=5.0)

        assert s1 is s2
        assert len(created_bridges) == 1
        assert s1.gst._server is not None
        assert s1.gst._server.server_address[1] == free_port
    finally:
        for b in created_bridges:
            b.close()


def test_gstbridge_falls_back_to_ephemeral_port_when_proxy_port_busy(monkeypatch, tmp_path):
    """When PROXY_PORT is already occupied, GstBridge binds a live ephemeral port so seek() still works."""
    ov_file = tmp_path / "override_url.txt"
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(ov_file))

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    monkeypatch.setattr(gstbridge, "PROXY_PORT", busy_port)

    blob, _, _ = _build_fragmented_mp4(seg_count=4, seg_dur=10000, timescale=1000)

    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Used", {}, None)

    monkeypatch.setattr(gstbridge, "ipv4_urlopen", fake_urlopen)
    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda _p: {"ok": True}, state={"playing": False}))
    bridge = None
    try:
        bridge = gstbridge.GstBridge(panel)
        assert bridge._server is not None
        actual_port = bridge._server.server_address[1]
        assert actual_port > 0
        assert actual_port != busy_port

        monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
        monkeypatch.setattr(bridge, "_init_streamer", lambda _u: None)
        bridge.play("https://rr2.googlevideo.com/videoplayback?itag=140", "T", "A", 40)
        assert ov_file.read_text(encoding="utf-8").startswith(f"http://127.0.0.1:{actual_port}/stream.mp4?seek=0")

        # Install a ready streamer directly and verify seek() writes the live ephemeral port
        streamer = object.__new__(gstbridge.MP4Streamer)
        streamer._lock = threading.Lock()
        streamer.ready = True
        streamer.total_size = len(blob)
        streamer.init_header = blob[:40]
        streamer.timescale = 1000
        streamer.segments = [(i * 10.0, 40 + i * 60, 60, i * 10000, 10.0) for i in range(4)]
        bridge.streamer = streamer

        bridge.seek(12000)
        ov_seek = ov_file.read_text(encoding="utf-8")
        assert ov_seek.startswith(f"http://127.0.0.1:{actual_port}/stream.mp4?seek=")
        assert f":{busy_port}/" not in ov_seek
    finally:
        if bridge is not None:
            bridge.close()
        blocker.close()


def test_gstbridge_seek_does_not_write_dead_proxy_port_when_server_unavailable(monkeypatch, tmp_path):
    """If proxy binding failed completely, seek() must not write a broken 127.0.0.1:8788 URL."""
    ov_file = tmp_path / "override_url.txt"
    seek_flag = tmp_path / "user_seek.txt"
    monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(ov_file))
    monkeypatch.setattr(gstbridge, "USER_SEEK_FLAG", str(seek_flag))

    sent = []
    panel = SimpleNamespace(glagol=SimpleNamespace(send=lambda p: sent.append(p), state={"playing": True}))
    monkeypatch.setattr(gstbridge.GstBridge, "_ensure_server", lambda self: None)
    bridge = gstbridge.GstBridge(panel)
    monkeypatch.setattr(bridge, "ensure_injected", lambda: True)
    monkeypatch.setattr(bridge, "_init_streamer", lambda _u: None)

    url = "https://rr2.googlevideo.com/videoplayback?itag=140"
    bridge.play(url, "T", "A", 40)

    streamer = object.__new__(gstbridge.MP4Streamer)
    streamer._lock = threading.Lock()
    streamer.ready = True
    streamer.total_size = 1000
    streamer.init_header = b"\x00" * 40
    streamer.timescale = 1000
    streamer.segments = [(0.0, 40, 100, 0, 10.0), (10.0, 140, 100, 10000, 10.0)]
    bridge.streamer = streamer

    bridge.seek(12000)
    ov = ov_file.read_text(encoding="utf-8")
    assert f"127.0.0.1:{gstbridge.PROXY_PORT}" not in ov
    assert ov == url
    assert any(cmd.get("command") == "rewind" for cmd in sent)


def test_restart_self_closes_gstbridge_proxy_before_execv(monkeypatch):
    """restart_self() must close active GstBridge proxy before os.execv replaces the process."""
    import server as srv_mod

    closed = []
    fake_gst = SimpleNamespace(close=lambda: closed.append("gst_closed"))
    fake_panel = SimpleNamespace(
        metrics=SimpleNamespace(_save=lambda: None),
        _extmusic_service=SimpleNamespace(gst=fake_gst),
    )
    monkeypatch.setattr(srv_mod.Handler, "panel", fake_panel)
    monkeypatch.setattr(srv_mod, "RESTART_HOOK", None)

    execv_calls = []
    monkeypatch.setattr(srv_mod.os, "execv", lambda exe, argv: execv_calls.append((exe, argv)))

    srv_mod.restart_self()
    assert closed == ["gst_closed"]
    assert len(execv_calls) == 1

