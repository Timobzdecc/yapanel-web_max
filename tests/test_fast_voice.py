"""Unit tests for fast local voice commands (< 200 ms) in ExtMusicService and AssistantDaemon."""
from __future__ import annotations

import json
import unittest

import assistant_max
from api.extmusic import register
from api.router import ApiRequest, Router
from extmusic.service import ExtMusicService


class _FakePanel:
    def __init__(self):
        self.adb = type("_Adb", (), {"is_local": False, "shell": lambda *a, **k: ("", "", 0)})()
        self._settings = {}

    def _load_settings(self):
        return self._settings


def _make_active_service():
    panel = _FakePanel()
    svc = ExtMusicService(panel)
    svc.current = {"source": "yt", "id": "dQw4w9WgXcQ", "title": "Test Song", "artist": "Test Band", "duration": 240}
    svc.queue = [dict(svc.current), {"source": "yt", "id": "abc12345678", "title": "Next Song", "artist": "Next"}]
    svc.index = 0
    return panel, svc


class TestFastVoiceService(unittest.TestCase):
    def test_returns_unhandled_fast_when_no_external_track_playing(self):
        panel = _FakePanel()
        svc = ExtMusicService(panel)
        res = svc.voice_command("поставь лайк")
        self.assertTrue(res["ok"])
        self.assertFalse(res["handled"])
        self.assertLess(res["elapsed_ms"], 50.0)

    def test_like_unlike_dislike_shuffle_repeat_and_seek_commands(self):
        _panel, svc = _make_active_service()
        calls = []
        svc.yt_like = lambda vid, on=True: (calls.append(("like", vid, on)), {"ok": True})[1]
        svc.yt_dislike = lambda vid, skip=True: (calls.append(("dislike", vid, skip)), {"ok": True})[1]
        svc.set_shuffle = lambda enabled=None: (calls.append(("shuffle", enabled)), svc.status())[1]
        svc.set_repeat = lambda mode=None: (calls.append(("repeat", mode)), svc.status())[1]
        svc.seek = lambda pos_ms, **kw: (calls.append(("seek", pos_ms)), svc.status())[1]
        svc.pause = lambda **kw: (calls.append(("pause",)), svc.status())[1]
        svc.resume = lambda **kw: (calls.append(("resume",)), svc.status())[1]
        svc.next = lambda **kw: (calls.append(("next",)), svc.status())[1]
        svc.prev = lambda **kw: (calls.append(("prev",)), svc.status())[1]
        svc.stop = lambda **kw: (calls.append(("stop",)), svc.status())[1]
        svc.status = lambda: {
            "current": svc.current,
            "player": {"position": 60000, "duration": 240000, "playing": True},
        }

        self.assertEqual(svc.voice_command("Алиса, поставь лайк")["action"], "like")
        self.assertEqual(calls[-1], ("like", "dQw4w9WgXcQ", True))

        self.assertEqual(svc.voice_command("убери лайк")["action"], "unlike")
        self.assertEqual(calls[-1], ("like", "dQw4w9WgXcQ", False))

        self.assertEqual(svc.voice_command("мне не нравится больше не включай")["action"], "dislike")
        self.assertEqual(calls[-1], ("dislike", "dQw4w9WgXcQ", True))

        self.assertEqual(svc.voice_command("включи шаффл перемешай")["action"], "shuffle_on")
        self.assertEqual(calls[-1], ("shuffle", True))

        self.assertEqual(svc.voice_command("по порядку выключи перемешивание")["action"], "shuffle_off")
        self.assertEqual(calls[-1], ("shuffle", False))

        self.assertEqual(svc.voice_command("на повтор")["action"], "repeat_one")
        self.assertEqual(calls[-1], ("repeat", "one"))

        self.assertEqual(svc.voice_command("повторяй весь плейлист")["action"], "repeat_all")
        self.assertEqual(calls[-1], ("repeat", "all"))

        self.assertEqual(svc.voice_command("выключи повтор")["action"], "repeat_off")
        self.assertEqual(calls[-1], ("repeat", "none"))

        self.assertEqual(svc.voice_command("включи сначала")["action"], "replay")
        self.assertEqual(calls[-1], ("seek", 0))

        self.assertEqual(svc.voice_command("перемотай на 30 секунд вперед")["action"], "seek_fwd")
        self.assertEqual(calls[-1], ("seek", 90000))

        self.assertEqual(svc.voice_command("перемотай назад на две минуты")["action"], "seek_back")
        self.assertEqual(calls[-1], ("seek", 0))

        what = svc.voice_command("что сейчас играет")
        self.assertEqual(what["action"], "what_playing")
        self.assertIn("Test Song", what["say"])

    def test_voice_api_router_endpoint(self):
        panel, svc = _make_active_service()
        panel._extmusic_service = svc
        svc.yt_like = lambda vid, on=True: {"ok": True}
        router = Router()
        register(router)
        fn = router.match("POST", "/api/extmusic/voice")
        self.assertIsNotNone(fn)
        res = fn(ApiRequest(panel=panel, data={"phrase": "поставь лайк"}))
        self.assertTrue(res["handled"])
        self.assertEqual(res["action"], "like")


class TestAssistantFastVoiceIntegration(unittest.TestCase):
    def test_assistant_dispatches_fast_voice_with_single_http_call_and_cancels_alice(self):
        d = assistant_max.AssistantDaemon.__new__(assistant_max.AssistantDaemon)
        d.lock = assistant_max.threading.RLock()
        d.cfg = {"enabled": False}
        d._ext_at = 0.0
        d._ext_cache = None

        cancels = []
        spoken = []
        http_calls = []
        d._alice_cancel = lambda: cancels.append(1)
        d._say_async = lambda txt: spoken.append(txt)
        d._ext_call = lambda path, method="GET", body=None, timeout=30: (
            http_calls.append((path, method, body, timeout)),
            {"ok": True, "handled": True, "action": "like", "say": "Поставил лайк.", "elapsed_ms": 1.2},
        )[1]

        handled = d.handle_extmusic("лайк")
        self.assertTrue(handled)
        self.assertEqual(len(cancels), 1)
        self.assertEqual(len(http_calls), 1)
        self.assertEqual(http_calls[0][0], "/api/extmusic/voice")
        self.assertEqual(spoken, ["Поставил лайк."])


if __name__ == "__main__":
    unittest.main()
