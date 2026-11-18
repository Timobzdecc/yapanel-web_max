"""HTTP-тесты настоящего server.Handler: оболочка, файлы и вход в панель.

Роутер домена проверяется через реальный HTTP-сервер, а колонка заменена
фейковой панелью: тесты не трогают adb и не требуют устройства.
"""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import access
import server

PASSWORD = "unit-test-password"


class FakeConn:
    online = True
    state = "online"
    attempts = 1

    def info(self):
        return {"state": self.state, "online": True}

    def retry_now(self):
        return {"ok": True}


class _Anything:
    """Заглушка для методов панели, которые перечисляются, но не вызываются в тесте."""

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self


class FakePanel:
    def __init__(self):
        self.conn = FakeConn()
        self.calls = []
        self.uploads = []

    def __getattr__(self, name):
        # Остальные маршруты Handler перечисляет заранее; для них достаточно заглушки.
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything()

    def fs_list(self, path):
        self.calls.append(("fs_list", path))
        return {"path": path, "entries": [{"name": "track.mp3", "dir": False}]}

    def fs_read(self, path):
        self.calls.append(("fs_read", path))
        return {"path": path, "text": "hello"}

    def fs_write(self, path, text):
        self.calls.append(("fs_write", path, text))
        return {"ok": True}

    def fs_delete(self, path):
        self.calls.append(("fs_delete", path))
        return {"ok": True}

    def fs_download(self, path):
        self.calls.append(("fs_download", path))
        directory = tempfile.mkdtemp()
        local = Path(directory) / "track.mp3"
        local.write_bytes(b"fake-audio")
        return "track.mp3", local, directory

    def fs_upload(self, directory, name, body):
        self.uploads.append((directory, name, body))
        return {"ok": True}

    def bt_memes_status(self):
        return {"supported": True, "installed": True, "running": True,
                "autostart": False, "sounds": {}}

    def bt_memes_set(self, on):
        self.calls.append(("bt_memes_set", on))
        return {"ok": True}

    def bt_memes_upload(self, kind, data):
        self.calls.append(("bt_memes_upload", kind, data))
        return {"ok": True}

    def bt_memes_reset(self, kind):
        self.calls.append(("bt_memes_reset", kind))
        return {"ok": True}


class TestHandler(server.Handler):
    __test__ = False


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.auth = access.PanelAccess(Path(cls.tmp.name) / ".panel_auth.json")
        cls.auth.set_password("", PASSWORD, "unittest")
        cls.panel = FakePanel()
        TestHandler.panel = cls.panel
        TestHandler.access = cls.auth
        TestHandler.updater = None
        TestHandler.ports = {}
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        self.cookie = f"{access.COOKIE}={self.auth._new_session('unittest')}"

    def request(self, method, path, data=None, raw=None, cookie=None, headers=None):
        body = raw
        hdrs = dict(headers or {})
        if data is not None:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
        token = self.cookie if cookie is None else cookie
        if token:
            hdrs["Cookie"] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def json(self, response):
        return json.loads(response[1])

    def test_shell_requires_login(self):
        status, body, _ = self.request("GET", "/api/term", cookie="")
        self.assertEqual(status, 401)
        self.assertTrue(self.json((status, body))["auth"])

    def test_shell_requires_websocket_upgrade(self):
        status, body, _ = self.request("GET", "/api/term?cols=80&rows=24")
        self.assertEqual(status, 400)
        self.assertEqual(self.json((status, body))["error"], "нужен WebSocket")

    def test_shell_rejects_foreign_origin(self):
        headers = {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                   "Origin": "https://evil.example"}
        status, body, _ = self.request("GET", "/api/term", headers=headers)
        self.assertEqual(status, 403)
        self.assertIn("чужого сайта", self.json((status, body))["error"])

    def test_shell_limits_open_terminals_before_touching_adb(self):
        headers = {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                   "Origin": f"http://127.0.0.1:{self.httpd.server_port}"}
        old = server.Terminal.active
        server.Terminal.active = server.Terminal.MAX
        try:
            status, body, _ = self.request("GET", "/api/term", headers=headers)
        finally:
            server.Terminal.active = old
        self.assertEqual(status, 429)
        self.assertIn("консолей", self.json((status, body))["error"])

    def test_fs_list_and_read(self):
        status, body, _ = self.request("GET", "/api/fs/list?path=%2Fdata%2Fmusic")
        self.assertEqual(status, 200)
        self.assertEqual(self.json((status, body))["path"], "/data/music")
        self.assertEqual(self.panel.calls[-1], ("fs_list", "/data/music"))

        status, body, _ = self.request("GET", "/api/fs/read?path=%2Fdata%2Fmusic%2Fa.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(self.json((status, body))["text"], "hello")

    def test_fs_write_and_missing_field(self):
        status, body, _ = self.request("POST", "/api/fs/write",
                                       {"path": "/data/a.txt", "text": "new"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("fs_write", "/data/a.txt", "new"))

        status, body, _ = self.request("POST", "/api/fs/delete", {})
        self.assertEqual(status, 400)
        self.assertIn("path", self.json((status, body))["error"])

    def test_bt_memes_routes(self):
        status, body, _ = self.request("GET", "/api/bt/memes")
        self.assertEqual(status, 200)
        self.assertTrue(self.json((status, body))["supported"])

        status, body, _ = self.request("POST", "/api/bt/memes", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("bt_memes_set", True))

        status, body, _ = self.request("POST", "/api/bt/memes/upload?kind=off", raw=b"OggS-payload",
                                       headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("bt_memes_upload", "off", b"OggS-payload"))

        status, body, _ = self.request("POST", "/api/bt/memes/reset", {"kind": "on"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.calls[-1], ("bt_memes_reset", "on"))

    def test_fs_upload_is_raw_and_fs_download_streams(self):
        status, body, _ = self.request("POST", "/api/fs/upload", raw=b"payload",
                                       headers={"Content-Type": "application/octet-stream",
                                                "X-Filename": "voice.mp3", "X-Dir": "/tmp"})
        self.assertEqual(status, 200)
        self.assertEqual(self.panel.uploads[-1], ("/tmp", "voice.mp3", b"payload"))

        status, body, headers = self.request("GET", "/api/fs/download?path=%2Fdata%2Fa.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"fake-audio")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("track.mp3", headers["Content-Disposition"])

    def test_fs_post_rejects_foreign_origin(self):
        status, body, _ = self.request("POST", "/api/fs/delete", {"path": "/data/a.txt"},
                                       headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("чужого сайта", self.json((status, body))["error"])

    def login(self, password):
        status, body, headers = self.request("POST", "/api/panel-auth/login",
                                             {"password": password}, cookie="")
        return status, body, headers

    def test_auth_login_session_and_logout(self):
        status, body, _ = self.request("GET", "/api/panel-auth", cookie="")
        self.assertEqual(status, 200)
        info = self.json((status, body))
        self.assertTrue(info["enabled"])
        self.assertFalse(info["authed"])

        status, body, _ = self.login("wrong-password")
        self.assertEqual(status, 400)
        self.assertIn("Неверный пароль", self.json((status, body))["error"])

        status, body, headers = self.login(PASSWORD)
        self.assertEqual(status, 200)
        self.assertTrue(self.json((status, body))["ok"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn(access.COOKIE + "=", cookie)

        status, body, _ = self.request("GET", "/api/fs/list?path=%2F", cookie=cookie)
        self.assertEqual(status, 200)

        status, body, _ = self.request("POST", "/api/panel-auth/logout", {}, cookie=cookie)
        self.assertEqual(status, 200)
        status, body, _ = self.request("GET", "/api/fs/list?path=%2F", cookie=cookie)
        self.assertEqual(status, 401)

    def test_auth_logout_others_keeps_current_session(self):
        status, body, headers = self.login(PASSWORD)
        self.assertEqual(status, 200)
        other = headers["Set-Cookie"].split(";", 1)[0]
        status, body, _ = self.request("POST", "/api/panel-auth/logout-others", {}, cookie=other)
        self.assertEqual(status, 200)
        self.assertIsInstance(self.json((status, body))["closed"], int)
        status, body, _ = self.request("GET", "/api/fs/list?path=%2F", cookie=other)
        self.assertEqual(status, 200)

    def test_extmusic_routes_and_thumb_proxy_security(self):
        from unittest.mock import patch
        from extmusic.service import ExtMusicError

        class FakeYMusic:
            def __init__(self):
                self.artist_calls = []

            def artist(self, aid):
                self.artist_calls.append(aid)
                return {"id": str(aid), "name": "YM Artist", "popular": [], "similar": []}

        class FakeExtService:
            def __init__(self):
                self.calls = []
                self.thumb_result = (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")
                self.thumb_error = None

            def search(self, query, count=30, source="", kind="songs"):
                self.calls.append(("search", query, count, source, kind))
                return {"source": source or "yt", "tracks": [{"id": "t1", "title": query}]}

            def yt_search(self, query, count=30, kind="songs"):
                self.calls.append(("yt_search", query, count, kind))
                return {"source": "yt", "kind": kind, "tracks": [{"id": "a1"}], "items": [{"id": "a1"}]}

            def yt_home(self, limit=30):
                self.calls.append(("yt_home", limit))
                return {"recent": [], "tracks": [], "quick_picks": [], "mixes": [], "feed": [], "moods": []}

            def yt_explore(self):
                self.calls.append(("yt_explore",))
                return {"releases": [], "moods": [], "tracks": []}

            def yt_charts(self, country="RU"):
                self.calls.append(("yt_charts", country))
                return {"tracks": [], "artists": [], "videos": []}

            def yt_mood(self, browse_id, params="", continuation="", limit=60):
                self.calls.append(("yt_mood", browse_id, params, continuation, limit))
                return {"title": "Mood", "tracks": [], "collections": []}

            def yt_collection(self, browse_id, params="", limit=100):
                self.calls.append(("yt_collection", browse_id, params, limit))
                return {"kind": "album", "title": "Alb", "tracks": []}

            def yt_artist(self, browse_id):
                self.calls.append(("yt_artist", browse_id))
                return {
                    "kind": "artist",
                    "title": "Linkin Park",
                    "subtitle": "10M listeners",
                    "description": "Rock band",
                    "thumb": "https://lh3.googleusercontent.com/lp",
                    "browse_id": browse_id,
                    "artist": "Linkin Park",
                    "artist_id": browse_id,
                    "tracks": [
                        {
                            "source": "yt",
                            "id": "vid1",
                            "title": "Numb",
                            "artist": "Linkin Park",
                            "artist_id": browse_id,
                            "duration": 185,
                            "thumb": "https://i.ytimg.com/vi/vid1/hq.jpg",
                        }
                    ],
                    "albums": [{"kind": "album", "browse_id": "MPREb_1", "title": "Meteora"}],
                    "similar": [{"kind": "artist", "browse_id": "UCsim", "title": "Muse", "thumb": "https://lh3.googleusercontent.com/m"}],
                }

            def yt_resolve_artist_id(self, video_id="", artist="", title=""):
                self.calls.append(("yt_resolve_artist_id", video_id, artist, title))
                return "UCresolved" if artist == "Godsmack" or video_id == "OYjZK_6i37M" else ""

            def yt_like(self, video_id, on=True):
                self.calls.append(("yt_like", video_id, on))
                return {"ok": True, "liked": bool(on), "video_id": video_id}

            def yt_dislike(self, video_id, skip=True):
                self.calls.append(("yt_dislike", video_id, skip))
                return {"ok": True, "disliked": True, "video_id": video_id}

            def set_shuffle(self, enabled=None):
                self.calls.append(("set_shuffle", enabled))
                return {"shuffle": True if enabled is None else bool(enabled)}

            def set_repeat(self, mode=None):
                self.calls.append(("set_repeat", mode))
                return {"repeat": str(mode or "all").lower()}

            def set_autoplay(self, enabled):
                self.calls.append(("set_autoplay", enabled))
                return {"autoplay_radio": bool(enabled)}

            def queue_add(self, items, mode="end"):
                self.calls.append(("queue_add", items, mode))
                return {"queue": []}

            def queue_remove(self, index):
                self.calls.append(("queue_remove", int(index)))
                return {"queue": []}

            def queue_move(self, from_index, to_index):
                self.calls.append(("queue_move", int(from_index), int(to_index)))
                return {"queue": []}

            def queue_clear_upcoming(self):
                self.calls.append(("queue_clear_upcoming",))
                return {"queue": []}

            def radio_play(self, track=None, browse_id=""):
                self.calls.append(("radio_play", track, browse_id))
                return {"current": {"id": "r1"}}

            def yt_mix_play(self, name="", index=None, browse_id=""):
                self.calls.append(("yt_mix_play", name, index, browse_id))
                return {"current": {"id": "m1"}}

            def fetch_thumb(self, url):
                self.calls.append(("fetch_thumb", url))
                if self.thumb_error:
                    raise self.thumb_error
                return self.thumb_result

        svc = FakeExtService()
        ym = FakeYMusic()
        login_calls = []
        old_svc = getattr(self.panel, "_extmusic_service", None)
        old_ym = getattr(self.panel, "ymusic", None)
        old_need_login = getattr(self.panel, "_need_login", None)
        self.panel._extmusic_service = svc
        self.panel.ymusic = ym
        self.panel._need_login = lambda: login_calls.append(True)
        try:
            # 1. Search with kind forwarding + backwards-compatible search without kind
            st, body, _ = self.request("GET", "/api/extmusic/search?source=yt&q=Meteora&count=10&kind=albums")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("search", "Meteora", 10, "yt", "albums"))

            st, body, _ = self.request("GET", "/api/extmusic/search?source=yt&q=Numb&count=5")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("search", "Numb", 5, "yt", "songs"))

            # 2. YT browse & search endpoints
            st, body, _ = self.request("GET", "/api/extmusic/yt/search?q=Muse&count=15&kind=artists")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_search", "Muse", 15, "artists"))

            st, _, _ = self.request("GET", "/api/extmusic/yt/home?limit=12")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_home", 12))

            st, _, _ = self.request("GET", "/api/extmusic/yt/explore")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_explore",))

            st, _, _ = self.request("GET", "/api/extmusic/yt/charts?country=US")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_charts", "US"))

            st, _, _ = self.request("GET", "/api/extmusic/yt/mood?browse_id=FEm1&params=p1&continuation=c1&limit=25")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_mood", "FEm1", "p1", "c1", 25))

            st, _, _ = self.request("GET", "/api/extmusic/yt/collection?browse_id=MPREb_1&params=p2&limit=50")
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_collection", "MPREb_1", "p2", 50))

            # 3. YT account like/dislike & queue/playback controls
            st, body, _ = self.request("POST", "/api/extmusic/yt/like", {"id": "v1", "like": False})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_like", "v1", False))

            st, body, _ = self.request("POST", "/api/extmusic/yt/like", {"id": "v1"})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_like", "v1", True))

            st, _, _ = self.request("POST", "/api/extmusic/yt/dislike", {"id": "v1", "skip": False})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_dislike", "v1", False))

            st, _, _ = self.request("POST", "/api/extmusic/yt/dislike", {"id": "v1"})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_dislike", "v1", True))

            st, _, _ = self.request("POST", "/api/extmusic/shuffle", {"enable": True})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("set_shuffle", True))

            st, _, _ = self.request("POST", "/api/extmusic/shuffle", {})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("set_shuffle", None))

            st, _, _ = self.request("POST", "/api/extmusic/repeat", {"mode": "One"})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("set_repeat", "One"))

            st, _, _ = self.request("POST", "/api/extmusic/repeat", {})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("set_repeat", None))

            st, _, _ = self.request("POST", "/api/extmusic/autoplay", {"enable": False})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("set_autoplay", False))

            st, _, _ = self.request("POST", "/api/extmusic/autoplay", {})
            self.assertEqual(st, 400)

            st, _, _ = self.request("POST", "/api/extmusic/queue/add", {"items": [{"id": "t8"}], "mode": "end"})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_add", [{"id": "t8"}], "end"))

            st, _, _ = self.request("POST", "/api/extmusic/queue/add", {"tracks": [{"id": "t7"}]})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_add", [{"id": "t7"}], "end"))

            st, _, _ = self.request("POST", "/api/extmusic/queue/add", {"track": {"id": "t9"}, "mode": "next"})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_add", {"id": "t9"}, "next"))

            st, _, _ = self.request("POST", "/api/extmusic/queue/remove", {"index": 0})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_remove", 0))

            st, _, _ = self.request("POST", "/api/extmusic/queue/remove", {})
            self.assertEqual(st, 400)

            st, _, _ = self.request("POST", "/api/extmusic/queue/remove", {"index": True})
            self.assertEqual(st, 400)

            st, _, _ = self.request("POST", "/api/extmusic/queue/move", {"from": 0, "to": 2})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_move", 0, 2))

            st, _, _ = self.request("POST", "/api/extmusic/queue/move", {"from_index": 2, "to_index": 1})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_move", 2, 1))

            st, _, _ = self.request("POST", "/api/extmusic/queue/move", {})
            self.assertEqual(st, 400)

            st, _, _ = self.request("POST", "/api/extmusic/queue/clear", {})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("queue_clear_upcoming",))

            st, _, _ = self.request("POST", "/api/extmusic/radio", {"track": "v1", "browse_id": "VL1"})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("radio_play", "v1", "VL1"))

            st, _, _ = self.request("POST", "/api/extmusic/yt/mix", {"name": "мой микс 2", "index": 2})
            self.assertEqual(st, 200)
            self.assertEqual(svc.calls[-1], ("yt_mix_play", "мой микс 2", 2, ""))

            st, _, _ = self.request("POST", "/api/extmusic/yt/jam", {"mode": "my"})
            self.assertEqual(st, 404)

            # CSRF protection still enforced on extmusic POST routes
            st, _, _ = self.request(
                "POST", "/api/extmusic/shuffle", {"enable": True}, headers={"Origin": "https://evil.example"}
            )
            self.assertEqual(st, 403)

            # 4. /api/extmusic/thumb: valid 200, missing url 400, SVG 400, SSRF/ExtMusicError 400
            st, raw, hdrs = self.request(
                "GET", "/api/extmusic/thumb?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2Fabc%2Fhq.jpg"
            )
            self.assertEqual(st, 200)
            self.assertEqual(raw, b"\xff\xd8\xff\xe0fakejpeg")
            self.assertEqual(hdrs.get("Content-Type"), "image/jpeg")
            self.assertEqual(hdrs.get("Cache-Control"), "public, max-age=86400")
            self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "*")
            self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")

            st, _, _ = self.request("GET", "/api/extmusic/thumb")
            self.assertEqual(st, 400)

            svc.thumb_result = (b"<svg/>", "image/svg+xml")
            st, _, _ = self.request(
                "GET", "/api/extmusic/thumb?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2Fabc%2Fhq.jpg"
            )
            self.assertEqual(st, 400)

            svc.thumb_result = (b"<html>", "text/html")
            st, _, _ = self.request(
                "GET", "/api/extmusic/thumb?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2Fabc%2Fhq.jpg"
            )
            self.assertEqual(st, 400)

            svc.thumb_error = ExtMusicError("Недопустимый URL обложки")
            st, _, _ = self.request("GET", "/api/extmusic/thumb?url=http%3A%2F%2F169.254.169.254%2Flatest")
            self.assertEqual(st, 400)

            # Real ytmusic.fetch_thumb SSRF check through /api/extmusic/thumb without network
            from urllib.parse import quote
            from extmusic.service import ExtMusicService
            real_svc = ExtMusicService(self.panel)
            self.panel._extmusic_service = real_svc
            with patch("urllib.request.urlopen") as mock_open:
                for bad_url in (
                    "http://127.0.0.1/evil.png",
                    "http://169.254.169.254/latest/meta-data",
                    "https://evil.example.com/a.jpg",
                ):
                    st, _, _ = self.request("GET", f"/api/extmusic/thumb?url={quote(bad_url, safe='')}")
                    self.assertEqual(st, 400)
                mock_open.assert_not_called()
            self.panel._extmusic_service = svc

            # 5. Auth guard: loopback /api/extmusic/* allowed without cookie; non-loopback rejected with 401
            st, _, _ = self.request(
                "GET",
                "/api/extmusic/thumb?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2Fabc%2Fhq.jpg",
                cookie="",
            )
            self.assertEqual(st, 400)  # svc.thumb_error still set -> passed auth guard on loopback!
            svc.thumb_error = None
            svc.thumb_result = (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")
            st, _, _ = self.request(
                "GET",
                "/api/extmusic/thumb?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2Fabc%2Fhq.jpg",
                cookie="",
            )
            self.assertEqual(st, 200)

            # Simulate non-loopback client without cookie on /api/extmusic/* including /thumb -> 401
            handler = object.__new__(TestHandler)
            sent = []
            handler.client_address = ("192.168.1.50", 54321)
            handler.command = "GET"
            handler.headers = {}
            handler.access = self.auth
            handler._send = lambda code, body, **kw: sent.append((code, body))
            self.assertTrue(handler._guard("/api/extmusic/thumb"))
            self.assertEqual(sent[-1][0], 401)
            self.assertTrue(handler._guard("/api/extmusic/yt/home"))
            self.assertEqual(sent[-1][0], 401)
            # With valid session cookie, non-loopback is allowed
            sent.clear()
            handler.headers = {"Cookie": self.cookie}
            self.assertFalse(handler._guard("/api/extmusic/thumb"))
            self.assertEqual(sent, [])

            # 6. /api/music/artist for ext:yt: vs Yandex
            st, body, _ = self.request("GET", "/api/music/artist?id=ext%3Ayt%3AUC123456")
            self.assertEqual(st, 200)
            art = self.json((st, body))
            self.assertEqual(art["id"], "ext:yt:UC123456")
            self.assertEqual(art["name"], "Linkin Park")
            self.assertEqual(art["tracks"], 1)
            self.assertEqual(art["albums"], 1)
            self.assertEqual(art["popular"][0]["id"], "ext:yt:vid1")
            self.assertEqual(art["popular"][0]["durationMs"], 185000)
            self.assertEqual(art["similar"][0]["id"], "ext:yt:UCsim")
            self.assertEqual(login_calls, [])

            # Fallback resolution when id is empty for YouTube Music tracks
            st, body, _ = self.request(
                "GET",
                "/api/music/artist?id=&source=yt&track_id=ext%3Ayt%3AOYjZK_6i37M&artist=Godsmack&title=I%20Stand%20Alone",
            )
            self.assertEqual(st, 200)
            res_art = self.json((st, body))
            self.assertEqual(res_art["id"], "ext:yt:UCresolved")
            self.assertEqual(svc.calls[-2:], [
                ("yt_resolve_artist_id", "OYjZK_6i37M", "Godsmack", "I Stand Alone"),
                ("yt_artist", "UCresolved"),
            ])

            st, _, _ = self.request("GET", "/api/music/artist?id=&source=yt&artist=UnknownXYZ")
            self.assertEqual(st, 404)

            st, body, _ = self.request("GET", "/api/music/artist?id=999")
            self.assertEqual(st, 200)
            self.assertEqual(self.json((st, body))["name"], "YM Artist")
            self.assertEqual(login_calls, [True])
            self.assertEqual(ym.artist_calls, ["999"])
        finally:
            self.panel._extmusic_service = old_svc
            self.panel.ymusic = old_ym
            if old_need_login is None:
                try:
                    del self.panel._need_login
                except AttributeError:
                    pass
            else:
                self.panel._need_login = old_need_login

    def test_serve_file_etag_includes_version_and_zero_mtime_never_returns_304(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_static:
            static_root = Path(tmp_static)
            index_file = static_root / "index.html"
            sw_file = static_root / "sw.js"
            app_file = static_root / "app.js"
            index_file.write_text("<html>v-test</html>", encoding="utf-8")
            sw_file.write_text("self.addEventListener('fetch',()=>{});", encoding="utf-8")
            app_file.write_text("console.log('ok');", encoding="utf-8")
            for p in (index_file, sw_file, app_file):
                os.utime(p, (0, 0))

            with patch.object(server, "STATIC", static_root):
                # 1. HTML shell and service worker never allow stale browser store caching
                for route in ("/", "/index.html", "/sw.js", "/static/sw.js"):
                    st, _, hdrs = self.request("GET", route, cookie="")
                    self.assertEqual(st, 200, route)
                    self.assertEqual(
                        hdrs.get("Cache-Control"),
                        "no-cache, no-store, must-revalidate",
                        route,
                    )

                # 2. Epoch-0 mtime must NEVER return 304 for If-Modified-Since
                for route in ("/", "/static/app.js"):
                    st, body, hdrs = self.request(
                        "GET",
                        route,
                        cookie="",
                        headers={"If-Modified-Since": "Wed, 18 Nov 2026 00:00:00 GMT"},
                    )
                    self.assertEqual(st, 200, f"{route} returned {st} for mtime=0 IMS")
                    self.assertTrue(body)
                    self.assertTrue(
                        (hdrs.get("ETag") or "").startswith(f'"{server.VERSION}-'),
                        f"{route} ETag missing VERSION: {hdrs.get('ETag')!r}",
                    )

                # 3. Positive mtime (>0) supports standard 304 for matching ETag or newer IMS
                os.utime(app_file, (1_795_000_000, 1_795_000_000))
                st, _, hdrs = self.request("GET", "/static/app.js", cookie="")
                self.assertEqual(st, 200)
                self.assertEqual(hdrs.get("Cache-Control"), "no-cache")
                etag = hdrs.get("ETag", "")
                self.assertTrue(etag.startswith(f'"{server.VERSION}-'), etag)

                st, _, _ = self.request(
                    "GET", "/static/app.js", cookie="", headers={"If-None-Match": etag}
                )
                self.assertEqual(st, 304)

                st, _, _ = self.request(
                    "GET", "/static/app.js", cookie="", headers={"If-None-Match": '"0.0.0-stale"'}
                )
                self.assertEqual(st, 200)

                st, _, _ = self.request(
                    "GET",
                    "/static/app.js",
                    cookie="",
                    headers={"If-Modified-Since": "Wed, 01 Jan 2031 00:00:00 GMT"},
                )
                self.assertEqual(st, 304)

                st, _, _ = self.request(
                    "GET",
                    "/static/app.js",
                    cookie="",
                    headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"},
                )
                self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
