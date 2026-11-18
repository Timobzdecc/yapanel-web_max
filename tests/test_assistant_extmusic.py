import time
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
