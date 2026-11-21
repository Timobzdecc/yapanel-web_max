"""Unit tests for synchronous Multiroom (extmusic/multiroom.py) & GstBridge LAN relay guard."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api.extmusic import register
from api.router import ApiRequest, Router
from extmusic.gstbridge import _is_allowed_client_ip, _should_parse_mp4_sidx
from extmusic.multiroom import (
    MaxMultiroomCoordinator,
    load_speakers_json_followers,
    validate_follower_url,
)
from extmusic.service import ExtMusicService


class _FakePanel:
    def __init__(self):
        self.adb = type("_Adb", (), {"is_local": False, "shell": lambda *a, **k: ("", "", 0)})()
        self._settings = {}

    def _load_settings(self):
        return self._settings


class TestMultiroomValidationAndDiscovery(unittest.TestCase):
    def test_validate_follower_url_normalizes_lan_and_blocks_unsafe_targets(self):
        self.assertEqual(
            validate_follower_url("10.0.0.43", self_ips={"10.0.0.42"}),
            "http://10.0.0.43:8765",
        )
        self.assertEqual(
            validate_follower_url("https://10.0.0.12:8766/", self_ips={"10.0.0.42"}),
            "https://10.0.0.12:8766",
        )
        # Self IP on own port must be blocked
        with self.assertRaises(ValueError):
            validate_follower_url("10.0.0.42:8765", self_ips={"10.0.0.42"}, self_port=8765)
        # Loopback on different port allowed for local multi.py/testing, blocked on same port
        self.assertEqual(
            validate_follower_url("127.0.0.1:9001", self_port=8765),
            "http://127.0.0.1:9001",
        )
        for bad in (
            "",
            "8.8.8.8:8765",
            "https://evil.example.com",
            "http://169.254.169.254:80",
            "http://10.0.0.43:8765/path?x=1",
            "http://user:pass@10.0.0.43:8765",
        ):
            with self.assertRaises(ValueError, msg=bad):
                validate_follower_url(bad, self_ips={"10.0.0.42"})

    def test_load_speakers_json_followers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "speakers.json").write_text(
                json.dumps(
                    {
                        "speakers": [
                            {"id": "hall", "name": "Зал", "device": "10.0.0.43:5555"},
                            {"id": "usb", "name": "USB", "device": "ABC123SERIAL"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sugs = load_speakers_json_followers(root)
            self.assertEqual(len(sugs), 1)
            self.assertEqual(sugs[0]["url"], "http://10.0.0.43:8765")
            self.assertEqual(sugs[0]["name"], "Зал")


class TestMultiroomBroadcastAndGuard(unittest.TestCase):
    def test_master_play_seek_pause_resume_stop_and_loop_prevention(self):
        panel = _FakePanel()
        mr = MaxMultiroomCoordinator(panel)
        sent = []
        mr._http_post = lambda url, path, payload, timeout=3.5: (sent.append((url, path, payload)), {"ok": True})[1]

        mr.configure({"enabled": True, "sync_volume": True})
        mr.add_follower("10.0.0.43", name="Кухня", offset_ms=120)

        track = {"source": "yt", "id": "vid12345678", "title": "Track", "artist": "Art", "url": "https://cdn/a.m4a"}
        mr.on_master_play(track, relay_url="http://10.0.0.42:8788/stream.mp4?seek=0", blocking=True)
        play_calls = [c for c in sent if c[1] == "/api/extmusic/play"]
        self.assertEqual(len(play_calls), 1)
        self.assertTrue(play_calls[0][2]["multiroom_follower"])
        self.assertEqual(play_calls[0][2]["track"]["url"], "http://10.0.0.42:8788/stream.mp4?seek=0")

        # Follower track must NEVER trigger re-broadcast (loop guard)
        sent.clear()
        mr.on_master_play({**track, "multiroom_follower": True}, blocking=True)
        self.assertEqual(sent, [])

        mr.on_master_seek(10000, blocking=True)
        seek_calls = [c for c in sent if c[1] == "/api/extmusic/seek"]
        self.assertEqual(len(seek_calls), 1)
        self.assertGreaterEqual(seek_calls[0][2]["position"], 10120)

        mr.on_master_pause(blocking=True)
        mr.on_master_resume(blocking=True)
        mr.on_master_volume(40, blocking=True)
        mr.on_master_stop(blocking=True)
        paths = [c[1] for c in sent]
        self.assertIn("/api/extmusic/pause", paths)
        self.assertIn("/api/extmusic/resume", paths)
        self.assertIn("/api/volume", paths)
        self.assertIn("/api/extmusic/stop", paths)

    def test_gstbridge_ip_guard_and_sidx_detection(self):
        self.assertTrue(_is_allowed_client_ip("127.0.0.1"))
        self.assertTrue(_is_allowed_client_ip("::1"))
        self.assertTrue(_is_allowed_client_ip("192.168.1.145"))
        self.assertTrue(_is_allowed_client_ip("::ffff:192.168.1.145"))
        self.assertFalse(_is_allowed_client_ip("8.8.8.8"))
        self.assertFalse(_is_allowed_client_ip("1.1.1.1"))

        self.assertFalse(_should_parse_mp4_sidx("http://192.168.1.50/song.mp3"))
        self.assertFalse(_should_parse_mp4_sidx("http://192.168.1.50/song.FLAC?token=1"))
        self.assertTrue(_should_parse_mp4_sidx("https://rr5.googlevideo.com/videoplayback?mime=audio%2Fmp4"))

    def test_multiroom_api_routes(self):
        panel = _FakePanel()
        svc = ExtMusicService(panel)
        panel._extmusic_service = svc

        router = Router()
        register(router)
        get_mr = router.match("GET", "/api/extmusic/multiroom")
        post_mr = router.match("POST", "/api/extmusic/multiroom")

        st = get_mr(ApiRequest(panel=panel))
        self.assertTrue(st["ok"])
        self.assertFalse(st["enabled"])

        added = post_mr(ApiRequest(panel=panel, data={"action": "add", "url": "192.168.1.148", "name": "Спальня"}))
        self.assertEqual(len(added["followers"]), 1)
        fid = added["followers"][0]["id"]

        updated = post_mr(ApiRequest(panel=panel, data={"action": "update", "id": fid, "offset_ms": 250, "enabled_follower": False}))
        self.assertEqual(updated["followers"][0]["offset_ms"], 250)
        self.assertFalse(updated["followers"][0]["enabled"])

        removed = post_mr(ApiRequest(panel=panel, data={"action": "remove", "id": fid}))
        self.assertEqual(removed["followers"], [])


if __name__ == "__main__":
    unittest.main()
