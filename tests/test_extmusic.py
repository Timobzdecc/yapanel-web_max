from pathlib import Path

import pytest

from extmusic.store import ExtMusicStore
from extmusic.service import ExtMusicService
from extmusic.vk import VKError, track_from_item
from extmusic.vkauth import (
    VALIDATE_PHONE_URL,
    VKAuthError,
    VKDirectAuth,
    _extract_token,
)


def test_track_from_item_maps_vk_fields():
    track = track_from_item({
        "id": 123,
        "owner_id": -456,
        "title": "Song",
        "artist": "Artist",
        "duration": 187,
        "url": "https://example.test/audio.mp3",
        "album": {"thumb": {"photo_300": "https://example.test/cover.jpg"}},
    })
    assert track == {
        "source": "vk",
        "id": "123",
        "owner_id": "-456",
        "title": "Song",
        "artist": "Artist",
        "duration": 187,
        "url": "https://example.test/audio.mp3",
        "thumb": "https://example.test/cover.jpg",
    }


def test_track_from_item_requires_ids():
    with pytest.raises(VKError):
        track_from_item({"title": "No ids"})


def test_store_roundtrip_and_clear(tmp_path: Path):
    store = ExtMusicStore(tmp_path / "extmusic.json")
    assert store.vk() == {}
    store.save_vk("secret-token", "Kate/1", "42", "Test User")
    assert store.vk() == {
        "token": "secret-token",
        "user_agent": "Kate/1",
        "user_id": "42",
        "name": "Test User",
    }
    store.clear_vk()
    assert store.vk() == {}


class FakePlayer:
    def __init__(self):
        self.played = ""
        self.stopped = False
        self.state = "idle"

    def play(self, url):
        self.played = url
        self.state = "playing"
        return {"state": "playing"}

    def status(self):
        return {"state": self.state, "position": 1, "duration": 10, "playing": self.state == "playing"}

    def pause(self):
        self.state = "paused"
        return {"state": "paused"}

    def resume(self):
        self.state = "playing"
        return {"state": "playing"}

    def seek(self, position):
        return {"state": self.state, "position": position}

    def stop(self):
        self.stopped = True
        self.state = "idle"
        return {"state": "idle"}


class FakeVK:
    def resolve(self, track, force=False):
        return {**track, "url": "https://example.test/audio.mp3"}


class FakePanel:
    glagol = None

    @staticmethod
    def music_cmd(data):
        return {"ok": True}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params, user_agent, timeout):
        self.calls.append((url, dict(params), user_agent))
        if not self.responses:
            raise AssertionError("unexpected VK request")
        return self.responses.pop(0)


def test_direct_auth_success():
    transport = FakeTransport({"access_token": "tok", "user_id": 42})
    auth = VKDirectAuth(transport)
    result = auth.start("+79990000000", "secret")
    assert result == {"step": "done", "token": "tok", "user_id": "42"}
    assert auth._sessions == {}
    url, params, user_agent = transport.calls[0]
    assert url.endswith("/token")
    assert params["client_id"] == "2274003"
    assert params["username"] == "+79990000000"
    assert user_agent.startswith("VKAndroidApp/")


def test_direct_auth_2fa_and_sms_request():
    transport = FakeTransport(
        {
            "error": "need_validation",
            "validation_sid": "sid-1",
            "validation_type": "2fa_app",
            "error_description": "Enter code",
        },
        {},
        {"access_token": "tok", "user_id": 7},
    )
    auth = VKDirectAuth(transport)
    first = auth.start("+79990000000", "secret")
    assert first["step"] == "code"
    assert first["type"] == "2fa_app"
    assert transport.calls[1][0] == VALIDATE_PHONE_URL
    assert transport.calls[1][1]["sid"] == "sid-1"
    done = auth.submit_code(first["session"], "1234")
    assert done["step"] == "done"
    assert transport.calls[2][1]["code"] == "1234"


def test_direct_auth_image_captcha():
    transport = FakeTransport(
        {
            "error": "need_captcha",
            "captcha_sid": "cap-1",
            "captcha_img": "https://example.test/captcha.png",
        },
        {"access_token": "tok", "user_id": 1},
    )
    auth = VKDirectAuth(transport)
    first = auth.start("+79990000000", "secret")
    assert first["step"] == "captcha"
    assert first["image"] == "https://example.test/captcha.png"
    done = auth.submit_captcha(first["session"], "ab12")
    assert done["step"] == "done"
    params = transport.calls[1][1]
    assert params["captcha_sid"] == "cap-1"
    assert params["captcha_key"] == "ab12"


def test_direct_auth_link_captcha_extracts_success_token():
    transport = FakeTransport(
        {"error": "need_captcha", "redirect_uri": "https://id.vk.com/captcha?x=1"},
        {"access_token": "tok", "user_id": 1},
    )
    auth = VKDirectAuth(transport)
    first = auth.start("+79990000000", "secret")
    assert first["url"] == "https://id.vk.com/captcha?x=1"
    done = auth.submit_captcha(
        first["session"], "https://id.vk.com/done#success_token=SUCCESS%2BTOKEN&x=1"
    )
    assert done["step"] == "done"
    assert transport.calls[1][1]["success_token"] == "SUCCESS+TOKEN"


def test_direct_auth_wrong_code_keeps_session():
    transport = FakeTransport(
        {"error": "need_validation", "validation_sid": "sid", "validation_type": "sms"},
        {},
        {"error": "invalid_request", "error_description": "wrong code"},
        {"access_token": "tok", "user_id": 3},
    )
    auth = VKDirectAuth(transport)
    first = auth.start("+79990000000", "secret")
    wrong = auth.submit_code(first["session"], "0000")
    assert wrong["step"] == "code"
    assert "Неверный код" in wrong["hint"]
    done = auth.submit_code(first["session"], "1111")
    assert done["step"] == "done"


def test_direct_auth_invalid_client_does_not_leak_password():
    transport = FakeTransport({"error": "invalid_client", "error_description": "bad"})
    auth = VKDirectAuth(transport)
    with pytest.raises(VKAuthError) as info:
        auth.start("+79990000000", "super-secret-password")
    assert "super-secret-password" not in str(info.value)
    assert auth._sessions == {}


def test_direct_auth_repr_hides_password():
    transport = FakeTransport({"error": "need_validation", "validation_sid": "sid"}, {})
    auth = VKDirectAuth(transport)
    auth.start("+79990000000", "super-secret-password")
    session = next(iter(auth._sessions.values()))
    assert "super-secret-password" not in repr(session)


def test_direct_auth_cancel_and_expiry():
    transport = FakeTransport({"error": "need_validation", "validation_sid": "sid"}, {})
    auth = VKDirectAuth(transport)
    first = auth.start("+79990000000", "secret")
    auth.cancel(first["session"])
    with pytest.raises(VKAuthError):
        auth.submit_code(first["session"], "1234")

    old = VKDirectAuth(
        FakeTransport({"error": "need_validation", "validation_sid": "sid"}, {}), ttl=-1
    )
    first = old.start("+79990000000", "secret")
    with pytest.raises(VKAuthError):
        old.submit_code(first["session"], "1234")


def test_extract_token_accepts_raw_and_urls():
    assert _extract_token("raw-token") == "raw-token"
    assert _extract_token("https://x.test/#access_token=abc%2B1&expires_in=0") == "abc+1"
    assert _extract_token("https://x.test/?success_token=abc") == "abc"


class FakeLoginAuth:
    def start(self, login, password):
        return {"step": "done", "token": "tok", "user_id": "1"}

    def submit_code(self, session, code):
        return {"step": "done", "token": "tok", "user_id": "1"}

    def submit_captcha(self, session, value):
        return {"step": "done", "token": "tok", "user_id": "1"}

    def cancel(self, session):
        return None


def test_service_login_finishes_with_token():
    service = ExtMusicService(FakePanel())
    service._vk_auth = FakeLoginAuth()
    service.auth = lambda data: {"configured": True, "account": "Test"}
    result = service.login_start({"login": "x", "password": "y"})
    assert result == {"step": "done", "status": {"configured": True, "account": "Test"}}


def test_service_login_cancel():
    service = ExtMusicService(FakePanel())
    service._vk_auth = FakeLoginAuth()
    assert service.login_cancel({"session": "s"}) == {"step": "cancelled"}


def test_service_queue_and_stop():
    service = ExtMusicService(FakePanel())
    service._vk = lambda: FakeVK()
    service.player = FakePlayer()
    first = {"id": "1", "owner_id": "2", "title": "One", "artist": "A", "duration": 10}
    second = {"id": "3", "owner_id": "4", "title": "Two", "artist": "B", "duration": 10}
    result = service.play(first, [first, second])
    assert result["current"]["title"] == "One"
    assert len(service.queue) == 2
    assert service.index == 0
    assert service.player.played == "https://example.test/audio.mp3"
    service.stop()
    assert service.current is None
    assert service.queue == []
    assert service.player.stopped is True


class FakeNative:
    def __init__(self):
        self.played = []
        self.controls = []
        self.seeked = None
        self.state = "playing"

    def available(self):
        return True

    def warmup(self):
        return None

    def play(self, video_id, playlist=""):
        self.played.append((video_id, playlist))

    def control(self, action):
        self.controls.append(action)

    def seek(self, position, duration=0):
        self.seeked = (position, duration)

    def queue(self):
        return {"active_id": -1, "items": []}

    def skip(self, queue_id):
        self.controls.append("skip")

    def status(self):
        return {
            "available": True,
            "session": True,
            "state": self.state,
            "playing": self.state == "playing",
            "paused": self.state == "paused",
            "loading": False,
            "position": 5000,
            "duration": 185000,
            "title": "Numb",
            "artist": "Linkin Park",
            "album": "Meteora",
            "queue_size": 25,
        }


def test_native_youtube_play_control_and_queue():
    service = ExtMusicService(FakePanel())
    service.player = FakePlayer()
    service.native = FakeNative()
    first = {"source": "yt", "id": "5qZQEq_C3vc", "title": "Numb", "artist": "Linkin Park", "duration": 185}
    second = {"source": "yt", "id": "gB5aXPgZ61s", "title": "Smack My Bitch Up", "artist": "The Prodigy", "duration": 344}
    result = service.play(first, [first, second])
    assert service.native.played == [("5qZQEq_C3vc", "RDAMVM5qZQEq_C3vc")]
    assert result["current"]["native"] is True
    assert result["player"]["state"] == "loading"
    service._native_force_loading_until = 0
    service._native_loading_until = 0
    assert service.status()["player"]["state"] == "playing"
    assert service.status()["player"]["position"] == 5000
    assert result["sources"]["yt"]["native"] is True
    service.pause()
    assert service.native.controls[-1] == "pause"
    service.resume()
    assert service.native.controls[-1] == "play"
    service.seek(60000)
    assert service.native.seeked == (60000, 185000)
    service.next()
    assert service.native.controls[-1] == "next"
    service.stop()
    assert service.native.controls[-1] == "stop"
    assert service.current is None


def test_native_sync_adopts_track_from_radio():
    service = ExtMusicService(FakePanel())
    service.player = FakePlayer()
    service.native = FakeNative()
    first = {"source": "yt", "id": "5qZQEq_C3vc", "title": "Numb", "artist": "Linkin Park", "duration": 185}
    service.play(first, [first])
    service._native_loading_until = 0
    service._native_sync({"title": "Hysteria", "artist": "Muse", "album": "Absolution", "duration": 343423})
    assert service.current["title"] == "Hysteria"
    assert service.current["artist"] == "Muse"
    assert service.current["native"] is True


def test_service_does_not_touch_display_framebuffer():
    class DisplayPanel:
        glagol = None
        adb = None
        def __init__(self):
            self.frames = []
        def display_eq_frame(self, frame):
            self.frames.append(frame)

    panel = DisplayPanel()
    service = ExtMusicService(panel)
    service.player = FakePlayer()

    assert not hasattr(service, "_eq_was_active")
    assert not hasattr(service, "_start_eq")
    assert panel.frames == []


class FakeGst:
    def __init__(self):
        self.active = False
        self.paused = False
        self.played = []
        self.seeked = []
        self.stopped = 0
        self.position_ms = 0
        self.duration_ms = 180000
        self.state_override = None

    def available(self):
        return True

    def ensure_injected(self):
        return True

    def play(self, url, title="", artist="", duration=0):
        self.active = True
        self.paused = False
        self.position_ms = 0
        self.duration_ms = int(duration or 180) * 1000
        self.played.append({"url": url, "title": title, "artist": artist, "duration": duration})
        return 1

    def pause(self):
        self.paused = True
        return 1

    def resume(self):
        self.paused = False
        return 1

    def stop(self):
        self.active = False
        self.paused = False
        self.position_ms = 0
        self.stopped += 1
        return 1

    def seek(self, position_ms):
        self.position_ms = int(position_ms)
        self.seeked.append(int(position_ms))
        return 1

    def player(self):
        if self.state_override:
            return dict(self.state_override)
        state = "paused" if self.paused else ("playing" if self.active else "stopped")
        return {
            "ok": True,
            "state": state,
            "playing": self.active and not self.paused,
            "position": self.position_ms,
            "duration": self.duration_ms,
            "levels": [],
            "error": "",
        }


def _make_gst_test_service(tmp_path, monkeypatch, panel=None):
    from extmusic import fast_yt, ytdlp

    svc = ExtMusicService(panel or FakePanel())
    svc.store = ExtMusicStore(tmp_path / "extmusic.json")
    svc.yt_account.store = svc.store
    svc.player = FakePlayer()
    svc.gst = FakeGst()
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
    return svc


def test_shuffle_repeat_and_queue_manipulation(tmp_path, monkeypatch):
    from extmusic.service import ExtMusicError

    svc = _make_gst_test_service(tmp_path, monkeypatch)
    try:
        st0 = svc.status()
        assert st0["shuffle"] is False
        assert st0["repeat"] == "none"
        assert st0["autoplay_radio"] is True
        assert st0["native"] is False

        tracks = [{"source": "yt", "id": f"v{i}", "title": f"T{i}", "artist": "A", "duration": 120} for i in range(4)]
        svc.play(tracks[0], tracks)
        assert svc._ext_started_at > 0
        assert svc.store.recent_yt()[0]["id"] == "v0"

        svc.set_shuffle(True)
        assert svc.status()["shuffle"] is True
        assert svc.current["id"] == "v0" and svc.index == 0
        shuffled_ids = [t["id"] for t in svc.queue]
        assert set(shuffled_ids) == {"v0", "v1", "v2", "v3"}
        assert shuffled_ids != ["v0", "v1", "v2", "v3"]

        # Switching to a track inside existing shuffled queue preserves shuffled order
        target_id = shuffled_ids[2]
        svc.play(svc.queue[2])
        assert svc.current["id"] == target_id and svc.index == 2
        assert [t["id"] for t in svc.queue] == shuffled_ids

        # Restoring shuffle=False brings back _orig_queue and locates current track
        svc.set_shuffle(False)
        assert [t["id"] for t in svc.queue] == ["v0", "v1", "v2", "v3"]
        assert svc.index == ["v0", "v1", "v2", "v3"].index(target_id)

        # Providing a distinct new queue while shuffle is True resets _orig_queue and puts played track at index 0
        svc.set_shuffle(True)
        other = [{"source": "yt", "id": f"o{i}", "title": f"O{i}", "artist": "B"} for i in range(4)]
        svc.play(other[2], other)
        assert svc.current["id"] == "o2" and svc.index == 0
        svc.set_shuffle(False)
        assert [t["id"] for t in svc.queue] == ["o0", "o1", "o2", "o3"]
        assert svc.index == 2

        # Repeat cycling + explicit modes + wrap on all
        assert svc.set_repeat()["repeat"] == "all"
        assert svc.set_repeat()["repeat"] == "one"
        assert svc.set_repeat()["repeat"] == "none"
        with pytest.raises(ExtMusicError):
            svc.set_repeat("bogus")

        svc.play(tracks[0], tracks)
        svc.set_repeat("All")
        svc.index = 3
        svc.current = svc.queue[3]
        svc.next()
        assert svc.current["id"] == "v0" and svc.index == 0

        # Repeat one: auto=True seeks to 0 without advancing; auto=False advances
        svc.set_repeat("one")
        svc.gst.position_ms = 50000
        svc.next(auto=True)
        assert svc.current["id"] == "v0" and svc.index == 0
        assert svc.gst.seeked[-1] == 0
        svc.next(auto=False)
        assert svc.current["id"] == "v1" and svc.index == 1

        # Queue add/remove/move/clear_upcoming
        svc.queue_add({"source": "yt", "id": "v99", "title": "NextUp", "artist": "A"}, mode="next")
        assert svc.queue[svc.index + 1]["id"] == "v99"
        svc.queue_add({"source": "yt", "id": "v100", "title": "EndUp", "artist": "A"}, mode="end")
        assert svc.queue[-1]["id"] == "v100"
        svc.queue_remove(svc.index + 1)
        assert all(t["id"] != "v99" for t in svc.queue)

        # Move upcoming track before current index and verify index tracks current
        last_idx = len(svc.queue) - 1
        last_id = svc.queue[last_idx]["id"]
        cur_id = svc.current["id"]
        svc.queue_move(last_idx, 0)
        assert svc.queue[0]["id"] == last_id
        assert svc.queue[svc.index]["id"] == cur_id

        svc.queue_clear_upcoming()
        assert len(svc.queue) == svc.index + 1

        with pytest.raises(ExtMusicError):
            svc.queue_remove(999)
        with pytest.raises(ExtMusicError):
            svc.queue_move(0, 999)
    finally:
        svc.stop()


def test_auto_radio_at_queue_end_and_native_isolation(tmp_path, monkeypatch):
    import threading
    from extmusic.service import ExtMusicError

    svc = _make_gst_test_service(tmp_path, monkeypatch)
    lock_free_during_radio = []

    def fake_radio(vid, pid="", limit=30):
        acquired = svc.lock.acquire(blocking=False)
        if acquired:
            svc.lock.release()
        lock_free_during_radio.append(acquired)
        return [
            {"source": "yt", "id": vid, "title": "Dup", "artist": "A"},
            {"source": "yt", "id": "rad1", "title": "Radio 1", "artist": "R"},
            {"source": "yt", "id": "rad2", "title": "Radio 2", "artist": "R"},
        ]

    try:
        monkeypatch.setattr("extmusic.ytmusic.radio", fake_radio)
        svc.play({"source": "yt", "id": "v1", "title": "One", "artist": "A"})
        svc.next(auto=True)
        assert lock_free_during_radio == [True]
        assert svc.current["id"] == "rad1"
        assert [t["id"] for t in svc.queue] == ["v1", "rad1", "rad2"]
        assert [t["id"] for t in svc._orig_queue] == ["v1", "rad1", "rad2"]

        # Stale generation check: if play() interrupts while radio() is fetching, stale radio must not overwrite
        evt_entered = threading.Event()
        evt_release = threading.Event()

        def slow_radio(vid, pid="", limit=30):
            evt_entered.set()
            evt_release.wait(2.0)
            return [{"source": "yt", "id": "stale_rad", "title": "Stale", "artist": "R"}]

        monkeypatch.setattr("extmusic.ytmusic.radio", slow_radio)
        svc.play({"source": "yt", "id": "v2", "title": "Two", "artist": "A"})
        t = threading.Thread(target=svc.next, kwargs={"auto": True})
        t.start()
        assert evt_entered.wait(1.0)
        svc.play({"source": "yt", "id": "fresh", "title": "Fresh", "artist": "A"})
        evt_release.set()
        t.join(timeout=2.0)
        assert svc.current["id"] == "fresh"

        # Disabling autoplay stops cleanly at queue end
        svc.set_autoplay(False)
        assert svc.status()["autoplay_radio"] is False
        svc.next(auto=True)
        assert svc.current is None

        # radio_play starts radio from track
        monkeypatch.setattr("extmusic.ytmusic.radio", fake_radio)
        res = svc.radio_play(track={"source": "yt", "id": "seed1", "title": "Seed", "artist": "S"})
        assert res["current"]["id"] == "seed1"
        assert [x["id"] for x in res["queue"]] == ["seed1", "rad1", "rad2"]

        # NativeYT isolation across all mutating queue/mode methods
        svc.current = {"source": "yt", "id": "nat1", "title": "Nat", "native": True}
        assert svc.status()["native"] is True
        for fn in (
            lambda: svc.set_shuffle(True),
            lambda: svc.set_repeat("all"),
            lambda: svc.set_autoplay(False),
            lambda: svc.queue_add({"source": "yt", "id": "x", "title": "X", "artist": "A"}),
            lambda: svc.queue_remove(0),
            lambda: svc.queue_move(0, 0),
            lambda: svc.queue_clear_upcoming(),
            lambda: svc.radio_play(track={"source": "yt", "id": "x", "title": "X", "artist": "A"}),
        ):
            with pytest.raises(ExtMusicError):
                fn()
    finally:
        svc.current = None
        svc.stop()


def test_poll_loop_auto_advance_and_eos_reliability(tmp_path, monkeypatch):
    import time as _time

    svc = _make_gst_test_service(tmp_path, monkeypatch)
    svc._poll_interval = 0.02
    radio_calls = []

    def fake_radio(vid, pid="", limit=30):
        radio_calls.append(vid)
        return [{"source": "yt", "id": "rad_eos", "title": "Radio EOS", "artist": "R"}]

    monkeypatch.setattr("extmusic.ytmusic.radio", fake_radio)
    try:
        tracks = [
            {"source": "yt", "id": "t1", "title": "T1", "artist": "A", "duration": 10},
            {"source": "yt", "id": "t2", "title": "T2", "artist": "A", "duration": 10},
        ]
        svc.play(tracks[0], tracks)
        assert svc.current["id"] == "t1"

        # Simulate EOS on t1 -> must advance to t2 once (not skip t2)
        svc.gst.position_ms = svc.gst.duration_ms - 500
        deadline = _time.time() + 1.5
        while _time.time() < deadline and (svc.current or {}).get("id") != "t2":
            _time.sleep(0.02)
        assert svc.current["id"] == "t2"
        assert svc.index == 1

        # Simulate EOS on t2 -> must trigger auto-radio and advance to rad_eos
        svc.gst.position_ms = svc.gst.duration_ms - 500
        deadline = _time.time() + 1.5
        while _time.time() < deadline and (svc.current or {}).get("id") != "rad_eos":
            _time.sleep(0.02)
        assert svc.current["id"] == "rad_eos"
        assert radio_calls == ["t2"]
    finally:
        svc.stop()


def test_youtube_facade_cache_lyrics_fallback_and_dislike_skip(tmp_path, monkeypatch):
    from extmusic import ytmusic
    from extmusic.service import ExtMusicError

    class LyricsPanel(FakePanel):
        def __init__(self):
            self.ymusic_calls = []
            svc_ref = self

            class FakeYMusic:
                def lyrics_by_name(self, title, artist, album=None, duration=0):
                    svc_ref.ymusic_calls.append((title, artist, album, duration))
                    return {"sync": [[1.0, "Clean line"]], "text": "Clean line", "source": "LRCLIB"}

            self.ymusic = FakeYMusic()

    panel = LyricsPanel()
    svc = _make_gst_test_service(tmp_path, monkeypatch, panel=panel)
    try:
        svc.store.push_recent_yt({"source": "yt", "id": "rec1", "title": "Recent Song", "artist": "RecArt"})

        monkeypatch.setattr(
            ytmusic,
            "home",
            lambda limit=30: {
                "tracks": [{"source": "yt", "id": "pub1", "title": "Public", "artist": "PubArt"}],
                "shelves": [{"title": "S1", "items": [{"kind": "album", "source": "yt", "id": "MPREb_1", "browse_id": "MPREb_1", "title": "Alb"}]}],
                "moods": [],
            },
        )
        home_anon = svc.yt_home()
        assert home_anon["recent"][0]["id"] == "rec1"
        assert home_anon["tracks"][0]["id"] == "pub1"
        assert svc.yt_resolve_known_track("pub1")["title"] == "Public"
        # Collection card (album) must NOT be cached as a playable video track
        assert svc.yt_resolve_known_track("MPREb_1") is None

        # Authenticated yt_home delegates to yt_account.home
        svc.store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
        monkeypatch.setattr(
            svc.yt_account,
            "home",
            lambda limit=30: {
                "tracks": [{"source": "yt", "id": "acc1", "title": "AccTrack", "artist": "AccArt"}],
                "quick_picks": [{"source": "yt", "id": "qp1", "title": "QP", "artist": "QPA"}],
                "mixes": [],
                "feed": [],
                "moods": [],
            },
        )
        home_auth = svc.yt_home()
        assert home_auth["recent"][0]["id"] == "rec1"
        assert home_auth["tracks"][0]["id"] == "acc1"
        assert svc.yt_resolve_known_track("qp1")["title"] == "QP"

        # Artist & similar caching allows resolving tracks not in queue
        monkeypatch.setattr(
            svc.yt_account,
            "collection",
            lambda bid, params="", limit=100: {
                "kind": "artist",
                "title": "Linkin Park",
                "browse_id": bid,
                "tracks": [{"source": "yt", "id": "art_t1", "title": "Faint", "artist": "Linkin Park", "artist_id": bid}],
                "albums": [],
                "similar": [],
            },
        )
        art = svc.yt_artist("UC123456")
        assert art["title"] == "Linkin Park"
        assert svc.yt_resolve_known_track("art_t1")["artist_id"] == "UC123456"

        monkeypatch.setattr(
            ytmusic,
            "radio",
            lambda vid, pid="", limit=30: [{"source": "yt", "id": "sim1", "title": "Sim", "artist": "S"}],
        )
        sim = svc.yt_similar("v0")
        assert sim[0]["id"] == "sim1"
        assert svc.yt_resolve_known_track("sim1")["title"] == "Sim"

        # yt_lyrics uses ytmusic.lyrics first, then clean_track_meta + panel.ymusic.lyrics_by_name
        monkeypatch.setattr(ytmusic, "lyrics", lambda vid: {"sync": None, "text": "YT text", "source": "YouTube Music"})
        assert svc.yt_lyrics("v0")["source"] == "YouTube Music"

        monkeypatch.setattr(ytmusic, "lyrics", lambda vid: {"sync": None, "text": None, "source": None})
        ly_fb = svc.yt_lyrics("v0", title="Noize MC — Спящая красавица (Official Video)", artist="Noize MC", album="Alb", duration=200)
        assert ly_fb["source"] == "LRCLIB"
        assert panel.ymusic_calls == [("Спящая красавица", "Noize MC", "Alb", 200)]

        # yt_like / yt_track_liked / yt_dislike skip
        monkeypatch.setattr(svc.yt_account, "like", lambda vid, on=True: {"ok": True, "liked": on, "video_id": vid})
        monkeypatch.setattr(svc.yt_account, "is_liked", lambda vid: vid == "d1")
        monkeypatch.setattr(svc.yt_account, "dislike", lambda vid: {"ok": True, "disliked": True, "video_id": vid})
        assert svc.yt_like("d1", True)["liked"] is True
        assert svc.yt_track_liked("d1") is True
        assert svc.yt_track_liked("d2") is False

        t1 = {"source": "yt", "id": "d1", "title": "D1", "artist": "A"}
        t2 = {"source": "yt", "id": "d2", "title": "D2", "artist": "A"}
        svc.play(t1, [t1, t2])
        svc.yt_dislike("d1")
        assert svc.current["id"] == "d2"

        # fetch_thumb wraps YTMusicError into ExtMusicError
        monkeypatch.setattr(ytmusic, "fetch_thumb", lambda url: (_ for _ in ()).throw(ytmusic.YTMusicError("bad url")))
        with pytest.raises(ExtMusicError):
            svc.fetch_thumb("http://evil.local/x.jpg")
    finally:
        svc.stop()


def test_glagol_auto_stop_ignores_fallback_track_and_stops_on_new_track(tmp_path, monkeypatch):
    svc = _make_gst_test_service(tmp_path, monkeypatch)
    try:
        # Inactive extmusic returns False
        assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "999999", "playerType": "yandex"}}) is False

        svc.play({"source": "yt", "id": "v1", "title": "One", "artist": "A"})
        # Must ignore within 3s grace period
        assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "999999", "playerType": "yandex"}}) is False

        svc._ext_started_at -= 10
        # Must ignore GstBridge dummy track 153109827
        assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "153109827", "playerType": "yandex"}}) is False
        assert svc.current is not None
        # Must ignore not playing
        assert svc.maybe_auto_stop_from_glagol({"playing": False, "playerState": {"id": "999999", "playerType": "yandex"}}) is False
        # Must ignore ext: IDs
        assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "ext:yt:v1", "playerType": "external"}}) is False
        assert svc.current is not None

        # Must stop when an independent Yandex track starts after grace period
        assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "999999", "playerType": "yandex"}}) is True
        assert svc.current is None

        # Must stop when Bluetooth starts playing after grace period
        svc.play({"source": "yt", "id": "v2", "title": "Two", "artist": "A"})
        svc._ext_started_at -= 10
        assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"playerType": "bluetooth"}}) is True
        assert svc.current is None
    finally:
        svc.stop()


def test_gst_youtube_ignores_yandex_onyourwave_auto_advance_and_advances_queue(tmp_path, monkeypatch):
    svc = _make_gst_test_service(tmp_path, monkeypatch)
    t1 = {"source": "yt", "id": "v1", "title": "One", "artist": "A", "duration": 200}
    t2 = {"source": "yt", "id": "v2", "title": "Two", "artist": "B", "duration": 210}
    try:
        svc.play(t1, [t1, t2])
        svc._ext_started_at -= 10

        # Auto-advance into Yandex user:onyourwave without recent voice activity must NOT stop ExtMusic
        leaked_state = {
            "playing": True,
            "timeSinceLastVoiceActivity": 600,
            "playerState": {"id": "19698553", "playlistId": "user:onyourwave", "playerType": "music_thin"},
        }
        assert svc.maybe_auto_stop_from_glagol(leaked_state) is False
        assert svc.current["id"] == "v1"

        # _poll_loop sees leaked user:onyourwave during GstBridge playback and advances YouTube queue instead
        svc.panel.glagol = type("FakeGlagol", (), {"state": leaked_state})()
        svc._poll_interval = 0.02
        advanced = []
        orig_auto_next = svc._auto_next

        def spy_auto_next(eos_gen):
            advanced.append(eos_gen)
            orig_auto_next(eos_gen)
            svc._poll_stop.set()

        monkeypatch.setattr(svc, "_auto_next", spy_auto_next)
        stop_evt = svc._poll_stop
        svc._poll_loop(svc._gen, stop_evt)
        assert len(advanced) == 1
        assert svc.current["id"] == "v2"

        # Explicit voice-triggered user:onyourwave (timeSinceLastVoiceActivity <= 15) MUST hand over to Yandex
        svc._ext_started_at -= 10
        voiced_wave = {
            "playing": True,
            "timeSinceLastVoiceActivity": 3,
            "playerState": {"id": "19698553", "playlistId": "user:onyourwave", "playerType": "music_thin"},
        }
        assert svc.maybe_auto_stop_from_glagol(voiced_wave) is True
        assert svc.current is None
    finally:
        svc.stop()


def test_queue_edge_cases_radio_browse_and_catalog_facades(tmp_path, monkeypatch):
    from extmusic import ytmusic
    from extmusic.service import _KNOWN_TRACKS_LIMIT
    from extmusic.ytaccount import YTAccountError

    svc = _make_gst_test_service(tmp_path, monkeypatch)
    try:
        # queue_add when idle starts playback; replace replaces queue
        t0 = {"source": "yt", "id": "q0", "title": "Q0", "artist": "A"}
        t1 = {"source": "yt", "id": "q1", "title": "Q1", "artist": "A"}
        t2 = {"source": "yt", "id": "q2", "title": "Q2", "artist": "A"}
        res = svc.queue_add(t0, mode="next")
        assert res["current"]["id"] == "q0"
        svc.queue_add([t1, t2], mode="end")
        assert [t["id"] for t in svc.queue] == ["q0", "q1", "q2"]

        # Adding an existing track with mode="next" moves it after current without duplicating
        svc.queue_add(t2, mode="next")
        assert [t["id"] for t in svc.queue] == ["q0", "q2", "q1"]

        # Removing active track advances to next; removing sole remaining track stops
        svc.queue_remove(0)
        assert svc.current["id"] == "q2" and svc.index == 0
        svc.queue_clear_upcoming()
        assert len(svc.queue) == 1
        svc.queue_remove(0)
        assert svc.current is None

        # mode="replace" replaces active queue
        svc.play(t0, [t0, t1])
        svc.queue_add([t2], mode="replace")
        assert [t["id"] for t in svc.queue] == ["q2"]

        # radio_play by browse_id
        monkeypatch.setattr(
            svc.yt_account,
            "collection",
            lambda bid, params="", limit=100: {
                "kind": "playlist",
                "title": "PL",
                "browse_id": bid,
                "playlist_id": "PL123",
                "tracks": [{"source": "yt", "id": "pl_t1", "title": "PL1", "artist": "A"}],
                "albums": [],
                "similar": [],
            },
        )
        monkeypatch.setattr(
            ytmusic,
            "radio",
            lambda vid, pid="", limit=30: [{"source": "yt", "id": "pl_rad1", "title": "PR1", "artist": "R"}],
        )
        r_pl = svc.radio_play(browse_id="VLPL123")
        assert r_pl["current"]["id"] == "pl_rad1"

        # Catalog facades + yt_home account fallback
        monkeypatch.setattr(ytmusic, "explore", lambda: {"releases": [], "moods": [], "tracks": [{"source": "yt", "id": "exp1", "title": "Exp", "artist": "A"}]})
        monkeypatch.setattr(ytmusic, "charts", lambda country="RU": {"tracks": [{"source": "yt", "id": "ch1", "title": "Ch", "artist": "A"}], "artists": [], "videos": []})
        monkeypatch.setattr(ytmusic, "browse_mood", lambda bid, params="", continuation="", limit=60: {"title": "Mood", "tracks": [{"source": "yt", "id": "md1", "title": "Md", "artist": "A"}], "collections": []})
        monkeypatch.setattr(ytmusic, "search", lambda q, count=30, kind="songs": [{"kind": "album", "source": "yt", "id": "MPREb_alb", "browse_id": "MPREb_alb", "title": "Meteora"}])

        assert svc.yt_explore()["tracks"][0]["id"] == "exp1"
        assert svc.yt_charts("RU")["tracks"][0]["id"] == "ch1"
        assert svc.yt_mood("FEmusic_mood")["tracks"][0]["id"] == "md1"
        sr = svc.yt_search("Meteora", count=10, kind="albums")
        assert sr["kind"] == "albums" and sr["items"][0]["browse_id"] == "MPREb_alb"
        assert svc.yt_resolve_known_track("exp1") is not None
        assert svc.yt_resolve_known_track("ch1") is not None
        assert svc.yt_resolve_known_track("md1") is not None
        assert svc.yt_resolve_known_track("MPREb_alb") is None

        svc.store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
        monkeypatch.setattr(svc.yt_account, "home", lambda limit=30: (_ for _ in ()).throw(YTAccountError("expired")))
        monkeypatch.setattr(ytmusic, "home", lambda limit=30: {"tracks": [{"source": "yt", "id": "fb1", "title": "Fallback", "artist": "A"}], "shelves": [], "moods": []})
        h_fb = svc.yt_home()
        assert h_fb["tracks"][0]["id"] == "fb1"
        assert "expired" in h_fb["account_error"]

        # Bounded _known_tracks cache
        many = [{"source": "yt", "id": f"bulk_{i}", "title": f"B{i}", "artist": "A"} for i in range(_KNOWN_TRACKS_LIMIT + 50)]
        svc._cache_tracks(many)
        assert len(svc._known_tracks) == _KNOWN_TRACKS_LIMIT
    finally:
        svc.stop()


def test_player_repeat_one_and_poll_loop_transient_error_resilience(tmp_path, monkeypatch):
    import time as _time
    from extmusic import fast_yt, ytdlp

    svc = ExtMusicService(FakePanel())
    svc.store = ExtMusicStore(tmp_path / "extmusic.json")
    svc.yt_account.store = svc.store
    svc._poll_interval = 0.02

    class FlakyPlayer(FakePlayer):
        def __init__(self):
            super().__init__()
            self.play_calls = 0
            self.status_calls = 0

        def play(self, url):
            self.play_calls += 1
            return super().play(url)

        def status(self):
            self.status_calls += 1
            if self.status_calls == 1:
                raise OSError("transient glitch")
            return super().status()

    fp = FlakyPlayer()
    svc.player = fp
    monkeypatch.setattr(svc.gst, "available", lambda: False)
    monkeypatch.setattr(svc.gst, "ensure_injected", lambda: False)
    monkeypatch.setattr(svc.native, "available", lambda: False)
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(fast_yt, "fast_resolve", lambda vid, cookies=None: (_ for _ in ()).throw(RuntimeError("skip")))
    monkeypatch.setattr(ytdlp, "resolve", lambda vid, **kwargs: {"url": f"https://rr.example/{vid}.m4a", "duration": 10})

    try:
        t1 = {"source": "yt", "id": "p1", "title": "P1", "artist": "A", "duration": 10}
        t2 = {"source": "yt", "id": "p2", "title": "P2", "artist": "A", "duration": 10}
        svc.play(t1, [t1, t2])
        svc.set_repeat("one")
        assert fp.play_calls == 1

        # Wait past first transient status() error, then trigger EOS
        _time.sleep(0.08)
        assert svc._poll_thread.is_alive()
        fp.state = "ended"

        deadline = _time.time() + 1.5
        while _time.time() < deadline and fp.play_calls < 2:
            _time.sleep(0.02)
        assert fp.play_calls == 2
        assert svc.current["id"] == "p1"
        assert svc.index == 0
    finally:
        svc.stop()


def test_glagol_auto_stop_releases_gstbridge_without_stopping_glagol(tmp_path, monkeypatch):
    from extmusic import fast_yt, ytdlp
    from extmusic.gstbridge import FALLBACK_TRACK_ID, GstBridge

    class SpyGlagol:
        def __init__(self):
            self.ws = object()
            self.state = None
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)
            return {"status": "SUCCESS"}

    class SpyPanel(FakePanel):
        def __init__(self):
            self.glagol = SpyGlagol()

    panel = SpyPanel()
    overrides = []
    monkeypatch.setattr(GstBridge, "_ensure_server", lambda self: None)
    monkeypatch.setattr(GstBridge, "available", lambda self, max_age=8.0: True)
    monkeypatch.setattr(GstBridge, "ensure_injected", lambda self: True)
    monkeypatch.setattr(GstBridge, "_write_override", lambda self, url: overrides.append(url))
    monkeypatch.setattr(GstBridge, "_init_streamer", lambda self, url: None)

    svc = ExtMusicService(panel)
    svc.store = ExtMusicStore(tmp_path / "extmusic.json")
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
        # 1. Normal user svc.stop() MUST send {"command": "stop"} to Glagol and clear override_url
        svc.play({"source": "yt", "id": "v_stop", "title": "StopMe", "artist": "A"})
        assert panel.glagol.sent[-1] == {"command": "playMusic", "id": FALLBACK_TRACK_ID, "type": "track"}
        assert overrides[-1] == "https://rr.example/v_stop.m4a"

        panel.glagol.sent.clear()
        svc.stop()
        assert panel.glagol.sent == [{"command": "stop"}]
        assert overrides[-1] == ""
        assert svc.gst._is_active is False
        assert svc.current is None

        # 2. Glagol auto-stop (Yandex track or Bluetooth) MUST clear override & release GstBridge
        #    WITHOUT sending {"command": "stop"} to Glagol
        svc.play({"source": "yt", "id": "v_hand", "title": "Handoff", "artist": "A"})
        assert svc.gst._is_active is True
        assert overrides[-1] == "https://rr.example/v_hand.m4a"
        panel.glagol.sent.clear()
        svc._ext_started_at -= 10

        stopped = svc.maybe_auto_stop_from_glagol(
            {"playing": True, "playerState": {"id": "999999", "playerType": "yandex"}}
        )
        assert stopped is True
        assert svc.current is None
        assert svc.gst._is_active is False
        assert overrides[-1] == ""
        assert panel.glagol.sent == []
    finally:
        svc.stop()


def test_server_music_bridge_controls_extras_artist_and_non_queue_play(tmp_path, monkeypatch):
    import server
    from extmusic import ytmusic

    class StubGlagol:
        def __init__(self):
            self.ws = object()
            self.state = {"playing": False, "playerState": None}
            self.sent = []

        def info(self):
            return {"connected": True, "state": self.state}

        def send(self, payload):
            self.sent.append(payload)
            return {"status": "SUCCESS", "payload": payload}

    class StubAuth:
        music_token = "ym-token"

        @staticmethod
        def info():
            return {"logged_in": True, "login": "tester"}

    class StubYMusic:
        def __init__(self):
            self.calls = []

        def track(self, tid):
            self.calls.append(("track", tid))
            return {"id": tid, "title": "YM Track"}

        def lyrics(self, tid):
            self.calls.append(("lyrics", tid))
            return {"sync": None, "text": "YM Lyrics", "source": "Яндекс Музыка"}

        def similar(self, tid):
            self.calls.append(("similar", tid))
            return [{"id": "ym_sim", "title": "YM Sim"}]

        def like(self, tid, on):
            self.calls.append(("like", tid, on))
            return {"liked": bool(on)}

        def dislike(self, tid):
            self.calls.append(("dislike", tid))
            return {"ok": True}

    panel = object.__new__(server.Panel)
    panel.auth = StubAuth()
    panel.glagol = StubGlagol()
    panel.ymusic = StubYMusic()
    panel._extmusic_service = None

    svc = _make_gst_test_service(tmp_path, monkeypatch, panel=panel)
    panel._extmusic_service = svc

    t1 = {
        "source": "yt",
        "id": "abc123",
        "title": "Song 1",
        "artist": "Artist 1",
        "artist_id": "UC_art1",
        "duration": 180,
        "thumb": "https://i.ytimg.com/vi/abc123/hq.jpg",
    }
    t2 = {
        "source": "yt",
        "id": "def456",
        "title": "Song 2",
        "artist": "Artist 2",
        "duration": 200,
    }

    try:
        # Must never invoke network is_liked inside music() or music_queue() polling
        monkeypatch.setattr(
            svc.yt_account,
            "is_liked",
            lambda vid: (_ for _ in ()).throw(AssertionError("is_liked must not run during polling")),
        )

        svc.play(t1, [t1, t2])
        p = panel.music()
        ep = p["glagol"]["state"]["playerState"]
        assert ep["entityInfo"] == {"shuffled": False, "repeatMode": "None"}
        assert ep["native"] is False
        assert ep["autoplayRadio"] is True
        assert ep["hasPrev"] is False
        assert ep["hasNext"] is True

        # Single YT track with autoplay_radio=False vs True
        svc.play(t1, [t1])
        assert panel.music()["glagol"]["state"]["playerState"]["hasNext"] is True
        svc.set_autoplay(False)
        st_no_radio = panel.music()["glagol"]["state"]["playerState"]
        assert st_no_radio["autoplayRadio"] is False
        assert st_no_radio["hasNext"] is False

        # Repeat="all" makes single track have hasNext=True
        panel.music_cmd({"command": "repeat", "mode": "All"})
        assert svc.repeat == "all"
        st_rep_all = panel.music()["glagol"]["state"]["playerState"]
        assert st_rep_all["entityInfo"]["repeatMode"] == "All"
        assert st_rep_all["hasNext"] is True

        # Shuffle & repeat="One" via music_cmd (plus toggle/cycle without args)
        svc.play(t1, [t1, t2])
        panel.music_cmd({"command": "shuffle", "enable": True})
        panel.music_cmd({"command": "repeat", "mode": "One"})
        assert svc.shuffle is True
        assert svc.repeat == "one"
        ep2 = panel.music()["glagol"]["state"]["playerState"]
        assert ep2["entityInfo"] == {"shuffled": True, "repeatMode": "One"}
        panel.music_cmd({"command": "shuffle"})
        panel.music_cmd({"command": "repeat"})
        assert svc.shuffle is False
        assert svc.repeat == "none"

        # Invalid repeat mode or NativeYT raises AdbError in music_cmd instead of silent stub
        with pytest.raises(server.AdbError):
            panel.music_cmd({"command": "repeat", "mode": "bogus"})

        # _ext_track_payload includes artistList[0].id and cached liked without network calls
        svc.yt_account._optimistic_likes.add("abc123")
        q_res = panel.music_queue()
        first_q = next(x for x in q_res["tracks"] if x["id"] == "ext:yt:abc123")
        assert first_q["artistList"] == [{"id": "ext:yt:UC_art1", "name": "Artist 1"}]
        assert first_q["liked"] is True

        # music_info enriches active track's liked status via yt_track_liked
        liked_checks = []
        monkeypatch.setattr(svc, "yt_track_liked", lambda vid: liked_checks.append(vid) or (vid == "abc123"))
        info = panel.music_info("ext:yt:abc123")
        assert liked_checks == ["abc123"]
        assert info["liked"] is True
        assert info["artistList"][0]["id"] == "ext:yt:UC_art1"

        # music_like / music_dislike forward to yt_like / yt_dislike(skip=True)
        monkeypatch.setattr(svc.yt_account, "like", lambda vid, on=True: {"ok": True, "liked": on, "video_id": vid})
        monkeypatch.setattr(svc.yt_account, "dislike", lambda vid: {"ok": True, "disliked": True, "video_id": vid})
        svc.play(t1, [t1, t2])
        res_like = panel.music_like("ext:yt:abc123", True)
        assert res_like["liked"] is True
        res_dislike = panel.music_dislike("ext:yt:abc123")
        assert res_dislike["ok"] is True
        assert svc.current["id"] == "def456"

        # music_lyrics & music_similar for ext:yt:
        monkeypatch.setattr(ytmusic, "lyrics", lambda vid: {"sync": None, "text": "YT song lyrics", "source": "YouTube Music"})
        ly = panel.music_lyrics("ext:yt:def456")
        assert ly["text"] == "YT song lyrics" and ly["source"] == "YouTube Music"

        monkeypatch.setattr(
            ytmusic,
            "radio",
            lambda vid, pid="", limit=30: [
                {"source": "yt", "id": "sim777", "title": "Sim Track", "artist": "Sim Art", "artist_id": "UC_sim", "duration": 150}
            ],
        )
        sim = panel.music_similar("ext:yt:def456")
        assert len(sim) == 1
        assert sim[0]["id"] == "ext:yt:sim777"
        assert sim[0]["artistList"] == [{"id": "ext:yt:UC_sim", "name": "Sim Art"}]
        assert sim[0]["durationMs"] == 150000

        # music_play with ext:yt:<vid> NOT in queue plays via known cache or minimal fallback instead of failing
        panel.music_play("ext:yt:sim777", "track")
        assert svc.current["id"] == "sim777"
        assert svc.current["title"] == "Sim Track"

        panel.music_play("ext:yt:brandnew99", "track")
        assert svc.current["id"] == "brandnew99"
        assert svc.current["artist"] == "YouTube Music"

        # music_play with ext:yt:<vid> also works when stopped (svc.current is None)
        svc.stop()
        panel.music_play("ext:yt:sim777", "track")
        assert svc.current["id"] == "sim777"

        # Yandex Music paths remain intact
        assert panel.music_lyrics("1001")["source"] == "Яндекс Музыка"
        assert panel.music_similar("1001") == [{"id": "ym_sim", "title": "YM Sim"}]
        assert panel.music_like("1001", True) == {"liked": True}
        panel.music_dislike("1001")
        assert {"command": "next"} in panel.glagol.sent
        panel.music_play("1002", "track")
        assert {"command": "playMusic", "id": "1002", "type": "track"} in panel.glagol.sent

        # music() calls maybe_auto_stop_from_glagol BEFORE formatting ext_p and survives exceptions
        svc.play(t1, [t1, t2])
        svc._ext_started_at -= 10
        panel.glagol.state = {"playing": True, "playerState": {"id": "153109827", "playerType": "yandex"}}
        assert panel.music()["glagol"]["state"]["playerState"]["id"] == "ext:yt:abc123"

        with monkeypatch.context() as mctx:
            mctx.setattr(svc, "maybe_auto_stop_from_glagol", lambda st: (_ for _ in ()).throw(RuntimeError("boom")))
            assert panel.music()["glagol"]["state"]["playerState"]["id"] == "ext:yt:abc123"

        panel.glagol.state = {"playing": True, "playerState": {"id": "999999", "playerType": "yandex", "title": "YM Live"}}
        handoff = panel.music()
        assert svc.current is None
        assert handoff["glagol"]["state"]["playerState"]["id"] == "999999"
        assert handoff["extmusic"] is None

        # NativeYT isolation in music() and music_cmd()
        import time as _time
        panel.glagol.state = {"playing": False, "playerState": None}
        svc._ext_started_at = _time.time()
        svc.current = {"source": "yt", "id": "nat1", "title": "Nat", "native": True}
        monkeypatch.setattr(svc.native, "available", lambda: True)
        monkeypatch.setattr(svc.native, "status", lambda: {"available": True, "state": "playing", "playing": True})
        monkeypatch.setattr(svc.native, "queue", lambda: {"active_id": -1, "items": []})
        nat_p = panel.music()["glagol"]["state"]["playerState"]
        assert nat_p["native"] is True
        assert nat_p["hasPrev"] is True and nat_p["hasNext"] is True
        with pytest.raises(server.AdbError):
            panel.music_cmd({"command": "shuffle", "enable": True})
    finally:
        svc.current = None
        svc.stop()


def test_yt_resolve_artist_id_caches_and_backfills_music_info(tmp_path, monkeypatch):
    import server
    from extmusic import ytmusic

    panel = object.__new__(server.Panel)
    panel.auth = type("Auth", (), {"music_token": "", "info": staticmethod(lambda: {"logged_in": False})})()
    panel.glagol = type("Glagol", (), {"ws": None, "state": {}, "info": staticmethod(lambda: {"connected": False, "state": {}})})()
    panel.ymusic = type("YM", (), {})()
    svc = _make_gst_test_service(tmp_path, monkeypatch, panel=panel)
    panel._extmusic_service = svc

    next_calls = []
    search_calls = []

    def fake_next_info(vid):
        next_calls.append(vid)
        if vid == "OYjZK_6i37M":
            return {"artist_id": "UCAekf1Wstvqk5LeNNHuMlDg", "artist": "Godsmack", "album_id": "", "album": "Faceless"}
        return {}

    def fake_find_artist(artist="", title=""):
        search_calls.append((artist, title))
        if artist == "Disturbed":
            return {"artist_id": "UCdisturbed", "artist": "Disturbed"}
        return {}

    monkeypatch.setattr(ytmusic, "track_artist_info", fake_next_info)
    monkeypatch.setattr(ytmusic, "find_artist_id", fake_find_artist)
    monkeypatch.setattr(svc, "yt_track_liked", lambda vid: False)

    try:
        # 1. Track played from OAuth personal mix has NO artist_id initially
        mix_track = {
            "source": "yt",
            "id": "OYjZK_6i37M",
            "title": "I Stand Alone",
            "artist": "Godsmack",
            "duration": 245,
        }
        svc.play(mix_track, [mix_track])
        info = panel.music_info("ext:yt:OYjZK_6i37M")
        assert info["artistList"] == [{"id": "ext:yt:UCAekf1Wstvqk5LeNNHuMlDg", "name": "Godsmack"}]
        assert next_calls == ["OYjZK_6i37M"]
        # Backfilled into active current track and cache so second lookup hits 0 network calls
        assert svc.current["artist_id"] == "UCAekf1Wstvqk5LeNNHuMlDg"
        info2 = panel.music_info("ext:yt:OYjZK_6i37M")
        assert info2["artistList"][0]["id"] == "ext:yt:UCAekf1Wstvqk5LeNNHuMlDg"
        assert next_calls == ["OYjZK_6i37M"]
        # Same artist on synthetic native track reuses cached art:godsmack without network calls
        assert svc.yt_resolve_artist_id("na000000000000001", artist="Godsmack", title="Awake") == "UCAekf1Wstvqk5LeNNHuMlDg"
        assert search_calls == []

        # 2. Uncached synthetic native MediaSession track skips track_artist_info and resolves by artist name
        nat_aid = svc.yt_resolve_artist_id("n1234567890abcdef", artist="Disturbed", title="Stupify")
        assert nat_aid == "UCdisturbed"
        assert next_calls == ["OYjZK_6i37M"]
        assert search_calls == [("Disturbed", "Stupify")]
        # Second search for same artist uses positive artist cache
        assert svc.yt_resolve_artist_id("nf000000000000001", artist="Disturbed", title="Voices") == "UCdisturbed"
        assert len(search_calls) == 1
    finally:
        svc.current = None
        svc.stop()


def test_v044_synced_lyrics_clickable_album_disk_cache_failover_and_playlists(tmp_path, monkeypatch):
    import server
    from extmusic import gstbridge, ytmusic

    class SyncStubYMusic:
        def __init__(self, synced=True):
            self.synced = synced

        def lyrics_by_name(self, title, artist, album=None, duration=0):
            if self.synced:
                return {"sync": [[1.5, "Sync line 1"]], "text": "Sync line 1", "source": "LRCLIB"}
            return {"sync": None, "text": None}

    panel = object.__new__(server.Panel)
    panel.auth = type("Auth", (), {"music_token": "", "info": staticmethod(lambda: {"logged_in": False})})()
    panel.glagol = type("Glagol", (), {"ws": None, "state": {}, "info": staticmethod(lambda: {"connected": False, "state": {}})})()
    panel.ymusic = SyncStubYMusic(synced=True)
    svc = _make_gst_test_service(tmp_path, monkeypatch, panel=panel)
    panel._extmusic_service = svc

    try:
        # 1. Plain YT lyrics upgrades to LRCLIB synced karaoke when LRCLIB has timestamps,
        #    and falls back to YT plain text when LRCLIB has no sync.
        t_al = {
            "source": "yt",
            "id": "OYjZK_6i37M",
            "title": "I Stand Alone",
            "artist": "Godsmack",
            "artist_id": "UCAekf1Wstvqk5LeNNHuMlDg",
            "album": "Faceless",
            "album_id": "MPREb_faceless",
            "duration": 245,
        }
        svc.play(t_al, [t_al])
        monkeypatch.setattr(ytmusic, "lyrics", lambda vid: {"sync": None, "text": "Plain YT", "source": "YouTube Music"})
        ly_up = svc.yt_lyrics("OYjZK_6i37M")
        assert ly_up["source"] == "LRCLIB" and ly_up["sync"] == [[1.5, "Sync line 1"]]

        panel.ymusic.synced = False
        ly_keep = svc.yt_lyrics("OYjZK_6i37M")
        assert ly_keep == {"sync": None, "text": "Plain YT", "source": "YouTube Music"}

        # 2. Clickable album.id is propagated in _ext_track_payload & store.recent_yt
        info = panel.music_info("ext:yt:OYjZK_6i37M")
        assert info["album"] == {"id": "ext:yt:MPREb_faceless", "title": "Faceless"}
        assert svc.store.recent_yt()[0]["album_id"] == "MPREb_faceless"

        # 3. Persistent disk artist cache survives a brand-new ExtMusicService instance
        monkeypatch.setattr(
            ytmusic,
            "find_artist_id",
            lambda artist="", title="": {"artist_id": "UCPersist123", "artist": "Korn"},
        )
        assert svc.yt_resolve_artist_id("n_korn_1", artist="Korn", title="Blind") == "UCPersist123"
        assert svc.store.artist_cache().get("art:korn") == "UCPersist123"

        svc2 = _make_gst_test_service(tmp_path, monkeypatch, panel=panel)
        try:
            monkeypatch.setattr(
                ytmusic,
                "find_artist_id",
                lambda artist="", title="": (_ for _ in ()).throw(AssertionError("should hit persistent disk cache")),
            )
            assert svc2.yt_resolve_artist_id("n_korn_2", artist="Korn", title="Freak") == "UCPersist123"
        finally:
            svc2.stop()

        # 4. GstBridge _open_upstream promotes backup URL on primary timeout
        monkeypatch.setattr(gstbridge, "PROXY_PORT", 0)
        monkeypatch.setattr(gstbridge, "OVERRIDE_PATH", str(tmp_path / "ov.txt"))
        br = gstbridge.GstBridge(panel)
        try:
            br._last_url = "https://primary.fail/audio"
            br._fallback_urls = ["https://backup.ok/audio"]
            tried = []

            class FakeResp:
                headers = {"Content-Range": "bytes 0-3/10"}

                def close(self):
                    pass

            def fake_open_range(url, start, end=None, attempts=12, timeout=2.5, max_elapsed=6.0):
                tried.append(url)
                if "primary.fail" in url:
                    raise TimeoutError("handshake timed out")
                return FakeResp()

            monkeypatch.setattr(gstbridge, "_open_range", fake_open_range)
            resp = gstbridge._StreamRequestHandler._open_upstream(br, "https://primary.fail/audio", 0, 3)
            assert isinstance(resp, FakeResp)
            assert tried == ["https://primary.fail/audio", "https://backup.ok/audio"]
            assert br._last_url == "https://backup.ok/audio"
        finally:
            br.close()

        # 5. yt_playlist_create from_queue and yt_playlist_add
        svc.queue = [t_al]
        monkeypatch.setattr(
            svc.yt_account,
            "create_playlist",
            lambda title, video_ids=None, description="", privacy="PRIVATE": {
                "ok": True, "playlist_id": "PLq1", "browse_id": "VLPLq1", "title": title, "added": len(video_ids or [])
            },
        )
        monkeypatch.setattr(
            svc.yt_account,
            "add_to_playlist",
            lambda pid, vids: {"ok": True, "playlist_id": pid, "browse_id": f"VL{pid}", "added": 1, "video_ids": vids},
        )
        pl_res = svc.yt_playlist_create("Очередь", from_queue=True)
        assert pl_res["playlist_id"] == "PLq1" and pl_res["added"] == 1
        add_res = svc.yt_playlist_add("PLq1", ["OYjZK_6i37M"])
        assert add_res["ok"] is True and add_res["added"] == 1
    finally:
        svc.current = None
        svc.stop()
