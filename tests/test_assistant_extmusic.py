import time
import urllib.parse
from unittest.mock import Mock

import assistant_max


def _bare_daemon():
    daemon = object.__new__(assistant_max.AssistantDaemon)
    daemon.glagol = Mock()
    daemon.speak = Mock()
    daemon._say_async = Mock()
    daemon._alice_cancel = Mock()
    daemon._ext_quick = Mock()
    daemon._ext_play_youtube = Mock()
    daemon._ext_cache = {"current": {"title": "Numb", "artist": "Linkin Park"}}
    daemon._ext_at = time.time()
    return daemon


def test_strip_youtube_prefixes():
    assert assistant_max.AssistantDaemon._strip_words(
        "включи на ютубе linkin park", "включи на ютубе"
    ) == "linkin park"
    assert assistant_max.AssistantDaemon._strip_words(
        "ютуб музыка metallica", "ютуб музыка"
    ) == "metallica"
    assert assistant_max.AssistantDaemon._strip_words("просто вопрос", "ютуб") is None


def test_extmusic_media_command_is_intercepted_when_playing():
    for phrase, action in [
        ("следующий", "next"),
        ("дальше", "next"),
        ("следующую", "next"),
        ("следующий трек", "next"),
        ("переключи", "next"),
        ("пропусти", "next"),
        ("вперёд", "next"),
        ("назад", "prev"),
        ("предыдущий", "prev"),
        ("предыдущую", "prev"),
        ("верни назад", "prev"),
        ("пауза", "pause"),
        ("поставь на паузу", "pause"),
        ("стоп", "stop"),
        ("хватит", "stop"),
        ("продолжи", "resume"),
        ("возобнови", "resume"),
    ]:
        daemon = _bare_daemon()
        assert daemon.handle_extmusic(phrase) is True, f"Failed for phrase: {phrase}"
        daemon._alice_cancel.assert_called_once()
        time.sleep(0.05)
        daemon._ext_quick.assert_called_once_with(action)


def test_extmusic_does_not_steal_unrelated_phrases():
    daemon = _bare_daemon()
    assert daemon.handle_extmusic("какая сегодня погода") is False
    daemon._alice_cancel.assert_not_called()


def test_extmusic_youtube_search_starts_playback():
    daemon = _bare_daemon()
    assert daemon.handle_extmusic("включи на ютубе linkin park numb") is True
    daemon._alice_cancel.assert_called_once()
    time.sleep(0.2)
    daemon._ext_play_youtube.assert_called_once_with("linkin park numb")
    daemon._say_async.assert_called_once_with("Ищу на YouTube.")


def test_extmusic_youtube_default_phrases_and_asr_variants():
    for phrase in (
        "Алиса, включи Ютуб музыку",
        "алиса, включи, пожалуйста, ютуб музыку",
        "Алиса пожалуйста включи ю туб музыку",
        "включи ютьюб музыку",
        "поставь ютуб мьюзик",
        "запусти youtube music",
        "включи музыку с ютуба",
        "включи музыку на ютубе",
        "давай включи ютубчик",
        "ютюб музыка",
    ):
        daemon = _bare_daemon()
        daemon._ext_cache = {"current": None}
        daemon._ext_play_youtube_default = Mock()
        assert daemon.handle_extmusic(phrase) is True, f"Failed for phrase: {phrase}"
        daemon._alice_cancel.assert_called_once()
        daemon._say_async.assert_called_once_with("Включаю YouTube Music.")
        time.sleep(0.05)
        daemon._ext_play_youtube_default.assert_called_once_with()
        daemon._ext_play_youtube.assert_not_called()


def test_extmusic_youtube_search_asr_variants():
    for phrase, expected in (
        ("Алиса, включи пожалуйста на ютубе Linkin Park Numb", "Linkin Park Numb"),
        ("поставь с ютьюба Король и Шут", "Король и Шут"),
        ("запусти ю туб мьюзик Metallica", "Metallica"),
        ("включи в ютубе Rammstein Sonne", "Rammstein Sonne"),
        ("ютуб мьюзик Lindsey Stirling", "Lindsey Stirling"),
    ):
        daemon = _bare_daemon()
        assert daemon.handle_extmusic(phrase) is True, f"Failed for phrase: {phrase}"
        daemon._alice_cancel.assert_called_once()
        time.sleep(0.05)
        daemon._ext_play_youtube.assert_called_once_with(expected)


def test_extmusic_does_not_steal_youtube_questions():
    for phrase in (
        "Алиса, почему ютуб не работает",
        "сколько пользователей на ютубе",
        "что такое youtube",
        "открой ютуб на телевизоре",
        "ютуб это сайт",
        "ютуб почему тормозит",
    ):
        daemon = _bare_daemon()
        assert daemon.handle_extmusic(phrase) is False, f"Falsely intercepted: {phrase}"
        daemon._alice_cancel.assert_not_called()


def test_extmusic_youtube_default_does_not_resume_vk_and_falls_back():
    daemon = object.__new__(assistant_max.AssistantDaemon)
    daemon.glagol = Mock()
    daemon.speak = Mock()
    daemon._say_async = Mock()
    daemon._alice_cancel = Mock()
    daemon._ext_quick = Mock()
    daemon._ext_cache = {"current": {"source": "vk", "title": "VK Song"}}
    daemon._ext_at = time.time()
    called_paths = []

    def fake_call(path, method="GET", body=None, timeout=30):
        called_paths.append((method, path))
        if path == "/api/extmusic/yt/account/liked?count=50":
            return {"tracks": []}
        if path == "/api/extmusic/yt/account/home?count=10":
            return {"tracks": []}
        if path == "/api/extmusic/yt/home?count=10":
            return {"tracks": [{"id": "yt1", "title": "Song", "artist": "Art"}]}
        if path == "/api/extmusic/play":
            return {"ok": True}
        raise AssertionError(f"Unexpected path: {path}")

    daemon._ext_call = fake_call
    assert daemon.handle_extmusic("Алиса, включи Ютуб музыку") is True
    time.sleep(0.1)
    daemon._ext_quick.assert_not_called()
    assert ("GET", "/api/extmusic/yt/account/liked?count=50") in called_paths
    assert ("GET", "/api/extmusic/yt/account/home?count=10") in called_paths
    assert ("GET", "/api/extmusic/yt/home?count=10") in called_paths
    assert any(m == "POST" and p == "/api/extmusic/play" for m, p in called_paths)


def test_extmusic_youtube_personal_mix_phrases():
    cases = (
        ("Алиса, включи мой супермикс", {"name": "мой супермикс"}),
        ("включи пожалуйста супермикс", {"name": "мой супермикс"}),
        ("включи мой микс", {"name": "мой микс"}),
        ("включи мой микс 1", {"name": "мой микс 1", "index": 1}),
        ("поставь второй мой микс", {"name": "мой микс 2", "index": 2}),
        ("включи мой микс три", {"name": "мой микс 3", "index": 3}),
        ("включи мой микс на ютубе", {"name": "мой микс"}),
        ("поставь риплей микс на ютубе", {"name": "риплей микс"}),
        ("включи понравившуюся музыку на ютубе", {"name": "понравившаяся музыка"}),
    )
    for phrase, expected_body in cases:
        daemon = _bare_daemon()
        daemon._ext_cache = {"current": {"source": "yt", "id": "cur1", "title": "Numb", "artist": "Linkin Park"}}
        calls = []

        def fake_call(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            return {"ok": True}

        daemon._ext_call = fake_call
        assert daemon.handle_extmusic(phrase) is True, f"Failed for personal mix phrase: {phrase}"
        daemon._alice_cancel.assert_called_once()
        time.sleep(0.08)
        assert ("POST", "/api/extmusic/yt/mix", expected_body) in calls, f"Wrong API payload for {phrase}: {calls}"
        assert not any(p.startswith("/api/extmusic/search") for _, p, _ in calls)


def test_extmusic_youtube_song_radio_phrases():
    song_tr = {"source": "yt", "id": "sonne1", "title": "Sonne", "artist": "Rammstein"}
    # Query-based song radio searches YT track and posts to /api/extmusic/radio
    for phrase, expected_q in (
        ("Алиса, включи радио на ютубе Rammstein Sonne", "Rammstein Sonne"),
        ("включи радио по песне Linkin Park Numb", "Linkin Park Numb"),
        ("запусти на ютубе радио по треку Звери", "Звери"),
    ):
        daemon = _bare_daemon()
        calls = []

        def fake_call(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            if path.startswith("/api/extmusic/search"):
                return {"tracks": [song_tr]}
            return {"ok": True}

        daemon._ext_call = fake_call
        assert daemon.handle_extmusic(phrase) is True, f"Failed for song radio phrase: {phrase}"
        daemon._alice_cancel.assert_called_once()
        time.sleep(0.08)
        assert any(m == "GET" and urllib.parse.quote(expected_q) in p for m, p, _ in calls), f"Missing search for {phrase}: {calls}"
        assert ("POST", "/api/extmusic/radio", {"track": song_tr}) in calls, f"Missing radio POST for {phrase}: {calls}"

    # Current-track song radio calls /api/extmusic/radio with {}
    for phrase in (
        "включи радио под эту песню",
        "включи радио по текущему треку",
        "включи похожую музыку",
    ):
        daemon = _bare_daemon()
        daemon._ext_cache = {"current": {"source": "yt", "id": "cur1", "title": "Numb", "artist": "Linkin Park"}}
        calls = []

        def fake_call(path, method="GET", body=None, timeout=30):
            calls.append((method, path, body))
            return {"ok": True}

        daemon._ext_call = fake_call
        assert daemon.handle_extmusic(phrase) is True, f"Failed for current song radio: {phrase}"
        daemon._alice_cancel.assert_called_once()
        time.sleep(0.08)
        assert ("POST", "/api/extmusic/radio", {}) in calls, f"Wrong payload for {phrase}: {calls}"


def test_extmusic_does_not_steal_fm_radio_or_removed_jam_phrases():
    for phrase in (
        "Алиса, включи радио",
        "включи радио Европа Плюс",
        "включи радио Дача",
        "включи мою волну",
        "включи микс дня",
        "включи похожую музыку",
        "Алиса, включи мой джем",
        "включи джем по песне Sonne",
        "купи банку малинового джема",
    ):
        daemon = _bare_daemon()
        assert daemon.handle_extmusic(phrase) is False, f"Falsely intercepted: {phrase}"
        daemon._alice_cancel.assert_not_called()


def test_extmusic_voice_youtube_requests_25_track_queue():
    daemon = _bare_daemon()
    # Restore real _ext_play_youtube and _ext_play_youtube_default methods (override _bare_daemon's Mock)
    daemon._ext_play_youtube = assistant_max.AssistantDaemon._ext_play_youtube.__get__(daemon)
    daemon._ext_play_youtube_default = assistant_max.AssistantDaemon._ext_play_youtube_default.__get__(daemon)
    calls = []

    def fake_call(path, method="GET", body=None, timeout=30):
        calls.append((method, path, body))
        if path.startswith("/api/extmusic/search"):
            return {"tracks": [{"id": f"s{i}", "title": f"S{i}", "artist": "A"} for i in range(30)]}
        if path == "/api/extmusic/yt/account/liked?count=50":
            return {"tracks": [{"id": f"l{i}", "title": f"L{i}", "artist": "B"} for i in range(50)]}
        return {"ok": True}

    daemon._ext_call = fake_call
    daemon._ext_play_youtube("Linkin Park")
    assert ("GET", "/api/extmusic/search?source=yt&q=Linkin%20Park&count=25", None) in calls
    play_bodies = [b for m, p, b in calls if p == "/api/extmusic/play"]
    assert len(play_bodies[-1]["queue"]) == 25

    calls.clear()
    daemon._ext_play_youtube_default()
    play_bodies = [b for m, p, b in calls if p == "/api/extmusic/play"]
    assert len(play_bodies[-1]["queue"]) == 25


def test_glagol_client_panel_bridge_fallback_on_port_1961(monkeypatch):
    client = assistant_max.GlagolClient(host="127.0.0.1", port=1961)
    monkeypatch.setattr(client, "send_command", lambda _payload: False)
    posted = []

    def fake_panel_post(path, body, timeout=2.5):
        posted.append((path, body, timeout))
        return True

    monkeypatch.setattr(client, "_panel_post", fake_panel_post)
    assert client.stop() is True
    assert client.say("Включаю мой супермикс на YouTube.") is True
    assert posted[0][0] == "/api/extmusic/alice_cancel"
    assert posted[1][0] == "/api/extmusic/alice_say"
    assert "Включаю мой супермикс на YouTube." in posted[1][1]["text"]

    # Non-1961 ephemeral test ports must not call the live panel HTTP fallback
    other = assistant_max.GlagolClient(host="127.0.0.1", port=45678)
    monkeypatch.setattr(other, "send_command", lambda _payload: False)
    monkeypatch.setattr(other, "_panel_post", fake_panel_post)
    posted.clear()
    assert other.stop() is False
    assert other.say("Привет") is False
    assert posted == []


def test_unwanted_alice_speaking_is_cancelled_but_own_tts_is_preserved():
    daemon = _bare_daemon()
    daemon._last_say_text = "Включаю мой супермикс на YouTube."
    daemon._last_intercepted_phrase = "алиса запусти мой супер микс"
    daemon._last_intercepted_at = time.time()

    # Own "Повтори за мной" output must NOT be cancelled
    own_line_1 = (
        "onAliceStateChanged: SPEAKING, recognizedPhrase='Повтори за мной Включаю мой супермикс на YouTube.', "
        "outputSpeech='Включаю мой супермикс на YouTube.'"
    )
    assert daemon._check_unwanted_alice_speaking(own_line_1) is False
    daemon._alice_cancel.assert_not_called()

    # Unwanted native Love Radio / TV prompt response for the intercepted phrase MUST be cancelled
    unwanted_mix = (
        "onAliceStateChanged: SPEAKING, recognizedPhrase='Запусти мой супер микс', "
        "outputSpeech='Запускаю микс Love Radio'"
    )
    assert daemon._check_unwanted_alice_speaking(unwanted_mix) is True
    daemon._alice_cancel.assert_called_once()

    # Same check for "запусти Ютуб" -> "подключите к телевизору"
    daemon = _bare_daemon()
    daemon._last_say_text = "Включаю YouTube Music."
    daemon._last_intercepted_phrase = "алиса запусти ютуб"
    daemon._last_intercepted_at = time.time()
    unwanted_tv = (
        "onAliceStateChanged: SPEAKING, recognizedPhrase='Запусти ютуб', "
        "outputSpeech='Чтобы смотреть видео, сначала подключите колонку к телевизору.'"
    )
    assert daemon._check_unwanted_alice_speaking(unwanted_tv) is True
    daemon._alice_cancel.assert_called_once()

    # Subsequent unrelated phrase handled by native Alice clears tracking
    daemon = _bare_daemon()
    daemon.cfg = {"enabled": True, "triggers": ["спроси gpt"], "mode": {}}
    daemon.lock = assistant_max.threading.Lock()
    daemon.mode = False
    daemon.reload_config_if_changed = Mock()
    daemon._last_intercepted_phrase = "запусти ютуб"
    daemon._last_intercepted_at = time.time()
    daemon.handle_phrase("Сколько сейчас времени?")
    assert daemon._last_intercepted_phrase == ""
    unrelated = "onAliceStateChanged: SPEAKING, recognizedPhrase='Сколько сейчас времени?', outputSpeech='Девять часов.'"
    assert daemon._check_unwanted_alice_speaking(unrelated) is False
    daemon._alice_cancel.assert_not_called()


def test_extmusic_service_and_router_alice_cancel_and_say(tmp_path):
    from api.router import ApiRequest, Router
    from api.extmusic import register
    from extmusic.service import ExtMusicService

    sent = []

    class FakeGlagol:
        ws = object()
        state = {}

        def send(self, payload, timeout=None):
            sent.append(payload)
            return {"status": "SUCCESS"}

    class FakePanel:
        data_dir = str(tmp_path)
        adb = None
        glagol = FakeGlagol()
        device = "max"
        model = "max"

        def __init__(self):
            self.music_cmds = []

        def music_cmd(self, payload):
            self.music_cmds.append(payload)
            return {"status": "SUCCESS"}

    panel = FakePanel()
    svc = ExtMusicService(panel)
    panel._extmusic_service = svc
    if getattr(svc, "gst", None) is not None and hasattr(svc.gst, "close"):
        svc.gst.close()

    suppressed = []
    cleared = []
    svc.gst = Mock()
    svc.gst.suppress_alice = lambda tts_sec=4.5, music_sec=12.0, clear_override=False: suppressed.append(
        (tts_sec, music_sec, clear_override)
    )
    svc.gst.clear_alice_tts_suppression = lambda: cleared.append(True)

    router = Router()
    register(router)

    cancel_fn = router.match("POST", "/api/extmusic/alice_cancel")
    assert cancel_fn is not None
    resp_cancel = cancel_fn(ApiRequest(panel=panel, data={"tts_sec": 3.5, "music_sec": 9.0}))
    assert resp_cancel["ok"] is True
    assert suppressed == [(3.5, 9.0, False)]
    assert {"command": "stop"} in panel.music_cmds

    say_fn = router.match("POST", "/api/extmusic/alice_say")
    assert say_fn is not None
    resp_say = say_fn(ApiRequest(panel=panel, data={"text": "Включаю YouTube Music."}))
    assert resp_say["ok"] is True
    assert resp_say["spoken"] is True
    assert cleared == [True]
    assert sent == [{"command": "sendText", "text": "Повтори за мной Включаю YouTube Music."}]


def test_ensure_hw_started_triggers_gst_preinject_when_panel_local(tmp_path, monkeypatch):
    import threading
    from extmusic.service import ExtMusicService

    injected_evt = threading.Event()

    class FakePanel:
        data_dir = str(tmp_path)
        adb = None
        glagol = None
        device = "max"
        model = "max"

    monkeypatch.setenv("PANEL_LOCAL", "1")
    panel = FakePanel()
    svc = ExtMusicService(panel)
    if getattr(svc, "gst", None) is not None and hasattr(svc.gst, "close"):
        svc.gst.close()
    svc.gst = Mock()
    svc.gst.ensure_injected = lambda: injected_evt.set() or True

    try:
        res = svc.ensure_hw_started()
        assert isinstance(res, dict)
        assert "osd" in res and "remote" in res and "dlna" in res
        assert injected_evt.wait(2.0), "Expected ensure_hw_started() to pre-inject GstBridge"
    finally:
        svc.stop_hw()


def test_alice_cancel_preserves_media_playback_for_next_and_resume():
    """Non-stop voice transport actions (next, resume) must not send glagol.stop() or MEDIA_STOP."""
    import threading

    daemon = _bare_daemon()
    del daemon._alice_cancel  # use the real AssistantDaemon._alice_cancel method
    daemon._ext_cache = None
    daemon._ext_at = 0.0
    daemon._ext_cancel_at = 0.0
    daemon._next_cancel_stop_media = True
    daemon.speak = lambda *_a, **_k: None

    local_cancels = []
    glagol_stops = []
    media_stop_keys = []
    media_stop_evt = threading.Event()

    daemon._write_local_alice_cancel = lambda tts_sec=4.5, music_sec=12.0: local_cancels.append((tts_sec, music_sec))
    daemon.glagol = type("G", (), {"stop": lambda _self: glagol_stops.append(1)})()

    def fake_media_stop():
        media_stop_keys.append(1)
        media_stop_evt.set()

    daemon._send_media_stop_key = fake_media_stop

    responses = {
        "следующий": {"handled": True, "action": "next", "title": "Next Track"},
        "продолжай": {"handled": True, "action": "resume", "title": "Next Track"},
        "пауза": {"handled": True, "action": "pause", "title": ""},
        "стоп": {"handled": True, "action": "stop", "title": ""},
    }

    def fake_ext_call(path, method="GET", payload=None, timeout=1.5):
        if path == "/api/extmusic/status":
            return {"current": {"id": "1", "source": "yt"}}
        if path == "/api/extmusic/voice":
            return dict(responses[(payload or {}).get("phrase", "")])
        return {}

    daemon._ext_call = fake_ext_call

    assert daemon.handle_extmusic("следующий") is True
    assert local_cancels[-1] == (4.5, 0.0)
    assert glagol_stops == []
    assert media_stop_keys == []

    assert daemon.handle_extmusic("продолжай") is True
    assert local_cancels[-1] == (4.5, 0.0)
    assert glagol_stops == []
    assert media_stop_keys == []

    media_stop_evt.clear()
    assert daemon.handle_extmusic("пауза") is True
    assert local_cancels[-1] == (4.5, 12.0)
    assert len(glagol_stops) == 1
    assert media_stop_evt.wait(1.0)
    assert len(media_stop_keys) == 1

    media_stop_evt.clear()
    assert daemon.handle_extmusic("стоп") is True
    assert local_cancels[-1] == (4.5, 12.0)
    assert len(glagol_stops) == 2
    assert media_stop_evt.wait(1.0)
    assert len(media_stop_keys) == 2


def test_alice_cancel_runs_before_voice_play_when_extmusic_active():
    """Dialog cancel must happen BEFORE /api/extmusic/voice sends playMusic on next/resume."""
    daemon = _bare_daemon()
    del daemon._alice_cancel
    daemon._ext_cache = None
    daemon._ext_at = 0.0
    daemon._ext_cancel_at = 0.0
    daemon._next_cancel_stop_media = True
    daemon.speak = lambda *_a, **_k: None
    daemon.glagol = type("G", (), {"stop": lambda _self: None})()
    daemon._send_media_stop_key = lambda: None

    events = []
    daemon._write_local_alice_cancel = lambda tts_sec=4.5, music_sec=12.0: events.append(("cancel", music_sec))

    def fake_ext_call(path, method="GET", payload=None, timeout=1.5):
        if path == "/api/extmusic/status":
            return {"current": {"id": "abc", "source": "yt"}}
        if path == "/api/extmusic/voice":
            events.append(("voice", (payload or {}).get("phrase", "")))
            return {"handled": True, "action": "next", "title": "Next"}
        return {}

    daemon._ext_call = fake_ext_call

    assert daemon.handle_extmusic("следующий") is True
    assert events == [("cancel", 0.0), ("voice", "следующий")]
