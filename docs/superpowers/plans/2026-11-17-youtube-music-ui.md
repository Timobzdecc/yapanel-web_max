# YouTube Music Modern UI & Full Playback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перестроить раздел YouTube Music в ЯПанели в полноценный современный музыкальный интерфейс с реальными обложками, лайками/дизлайками, коллекциями (без редактирования), чартами/поиском с фильтрами, очередью, перемешиванием, повтором, авто-радио и текстами песен в едином стиле ЯПанели через её штатные `#minibar` и `#tab-player`.

**Architecture:** Разделить публичный каталог (`extmusic/ytmusic.py` на `WEB_REMIX`: витрина, поиск с фильтрами, чарты, альбомы/артисты/плейлисты, радио `RDAMVM*`, тексты `MPLYt*` + очистка для LRCLIB) и авторизованные действия (`extmusic/ytaccount.py` через параметризованный `_post(endpoint=...)` для `TVHTML5` OAuth и `WEB_REMIX` cookies: лайки/дизлайки, `likeStatus`, `FEmusic_library_landing`, `FEwhat_to_watch`). Состояние очереди, `shuffle`, `repeat`, `autoplay_radio` и авто-остановка при внешнем переключении через `Glagol` инкапсулируются в `ExtMusicService`, транслируются через `server.py`/`api/*` в штатные `#minibar` и `#tab-player`, а `#tab-yt` очищается от дублирующего `#yt-player-card` и получает карточки, обложки и навигацию ЯПанели.

**Tech Stack:** Python 3.9–3.11 (`urllib`, `pytest`), Vanilla ES2020 JS / CSS / HTML (YapAnel design tokens, `#tab-player`, `#minibar`, `sw.js`), Node test checks (`tests/*.mjs`, `node --check`).

**Spec:** `docs/superpowers/specs/2026-11-17-youtube-music-ui-design.md`

---

## Global Constraints

- Не модифицировать `/system` и `/vendor`; аудиовывод остаётся строго на штатном GStreamer / `ExtMusicService`.
- Не ломать существующие loopback-маршруты `/api/extmusic/*` для голосового ассистента (`assistant_max.py`), а также Яндекс Музыку, VK Music и Bluetooth в `#tab-player` и `#minibar`.
- Никакого редактирования плейлистов (без создания, переименования, удаления и локальных плейлистов) — только просмотр и воспроизведение.
- Сохранить обратную совместимость `track_from_item` в `tests/test_ytmusic.py` (`artist_id`/`album_id` добавлять только при наличии `browseEndpoint`, без ломки существующих словарей) и резервного `NativeYT` в `tests/test_extmusic.py`.
- Строго использовать стили, переменные и компоненты ЯПанели (`.card`, `.pill`, `.seg`, `.pl-hero`, `.trow`, `--pl-tint`), не менять 5 штатных пунктов мобильной `.mbar`.

## Review Focus

1. **SSRF/CORS прокси обложек (`/api/extmusic/thumb`):** посторонние URL (`http://`, `127.0.0.1`, `http://169.254.169.254`, произвольные домены) обязаны отклоняться с `400`/`403` без сетевого запроса; разрешены только `https://` поддомены `ytimg.com`, `googleusercontent.com`, `ggpht.com`, `youtube.com` с лимитом 2 МБ. (Тестируется в Task 1 + Task 4.)
2. **Ложное авто-закрытие при `GstBridge.FALLBACK_TRACK_ID` (`"153109827"`):** пока `Glagol` играет служебный трек `153109827` для удержания пайплайна `GstBridge` (или стоит на паузе сразу после запуска YouTube), `maybe_auto_stop_from_glagol()` не должен сбрасывать `ext.current`; сброс допустим только при активном новом воспроизведении Яндекс-трека (`id != "153109827"`) или `bluetooth` после старта внешнего трека. (Тестируется в Task 3.)
3. **Изоляция `NativeYT` (`native=True`):** при срабатывании резервного `NativeYT` вызовы `set_shuffle`, `set_repeat`, `queue_add/remove/move` не должны мутировать или запускать GStreamer-очередь, а `music()` и UI обязаны помечать `native: True` и оставлять недоступные кнопки выключенными. (Тестируется в Task 3 + Task 4.)
4. **Отказоустойчивая деградация «Главной», текстов и протухших cookies:** если публичный `FEmusic_home` или личный `FEwhat_to_watch` падает, или в LRCLIB приходит грязное YouTube-название `"Artist — Song (Official Video) [4K]"`, эндпоинт обязан мягко деградировать / очищать метаданные; при `401/403` от браузерных cookies возвращается понятная ошибка обновления cookies без краша публичного поиска. (Тестируется в Task 1 + Task 2.)
5. **Маршрутизация хэшей с параметрами (`#yt?artist=...`) и кэш `sw.js`:** `showTab()` в `static/app.js` должен парсить имя вкладки до `?`, не сваливаясь в `overview`, а `static/sw.js` должен обновить `NAMESPACE` / версии скриптов. (Тестируется в Task 5 + Task 6.)

---

### Task 1: Public YouTube Music Catalog, Filters, Collections, Radio, Lyrics & Thumb Guard

**Files:**
- Modify: `extmusic/store.py` (recent history persistence)
- Modify: `extmusic/ytmusic.py` (search filters, public home, explore/charts/moods, album/artist/playlist collection browser, radio `next`, lyrics `MPLYt`, title cleaner, thumb allowlist)
- Test: `tests/test_ytmusic.py`

**Interfaces:**
- Produces in `extmusic/store.py`:
  - `ExtMusicStore.recent_yt() -> List[Dict[str, Any]]`
  - `ExtMusicStore.push_recent_yt(track: Dict[str, Any], limit: int = 30) -> List[Dict[str, Any]]`
- Produces in `extmusic/ytmusic.py`:
  - `SEARCH_PARAMS: Dict[str, str]` (`songs`, `videos`, `albums`, `artists`, `playlists`)
  - `search(query: str, count: int = 30, kind: str = "songs") -> List[Dict[str, Any]]`
  - `home(limit: int = 30) -> Dict[str, Any]` (`{"tracks": [...], "shelves": [...], "moods": [...]}`)
  - `explore() -> Dict[str, Any]` (`{"releases": [...], "moods": [...], "tracks": [...]}`)
  - `charts(country: str = "RU") -> Dict[str, Any]` (`{"tracks": [...], "artists": [...], "videos": [...]}`)
  - `browse_mood(browse_id: str, params: str = "", continuation: str = "", limit: int = 60) -> Dict[str, Any]` (`{"title": str, "tracks": [...], "collections": [...]}`)
  - `browse_collection(browse_id: str, params: str = "", limit: int = 100) -> Dict[str, Any]` (`{"kind": "album"|"artist"|"playlist", "title": str, "subtitle": str, "thumb": str, "browse_id": str, "playlist_id": str, "tracks": [...], "albums": [...], "similar": [...]}`)
  - `radio(video_id: str, playlist_id: str = "", limit: int = 30) -> List[Dict[str, Any]]`
  - `lyrics(video_id: str) -> Dict[str, Any]` (`{"sync": None, "text": Optional[str], "source": Optional[str]}`)
  - `clean_track_meta(title: str, artist: str = "") -> Tuple[str, str]`
  - `is_allowed_thumb_url(url: str) -> bool`
  - `fetch_thumb(url: str, max_bytes: int = 2 << 20) -> Tuple[bytes, str]`

- [ ] **Step 1: Write failing tests for store history, search filters, collections, radio, lyrics cleanup, and thumb URL allowlist**

```python
# In tests/test_ytmusic.py
def test_store_recent_yt_dedupes_and_caps(tmp_path):
    store = ExtMusicStore(tmp_path / "ext.json")
    for i in range(35):
        store.push_recent_yt({"source": "yt", "id": f"v{i}", "title": f"S{i}", "artist": "A", "thumb": "https://i.ytimg.com/vi/x/hq.jpg"}, limit=30)
    store.push_recent_yt({"source": "yt", "id": "v5", "title": "S5", "artist": "A"}, limit=30)
    rec = store.recent_yt()
    assert len(rec) == 30
    assert rec[0]["id"] == "v5"
    assert sum(1 for x in rec if x["id"] == "v5") == 1


def test_search_with_kind_filters_and_card_types(monkeypatch):
    seen = []
    def fake_post(body, endpoint="search"):
        seen.append((endpoint, body))
        return {"contents": {"musicShelfRenderer": {"contents": [{
            "musicResponsiveListItemRenderer": {
                "flexColumns": [
                    _text_column("Meteora"),
                    _text_column("Альбом • Linkin Park • 2003"),
                ],
                "navigationEndpoint": {"browseEndpoint": {
                    "browseId": "MPREb_123",
                    "browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}},
                }},
                "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://lh3.googleusercontent.com/alb"}]}}},
            }
        }]}}}
    monkeypatch.setattr(ytmusic, "_post", fake_post)
    res = ytmusic.search("Meteora", count=10, kind="albums")
    assert seen[0][1]["params"] == ytmusic.SEARCH_PARAMS["albums"]
    assert res[0]["kind"] == "album"
    assert res[0]["browse_id"] == "MPREb_123"


def test_clean_track_meta_and_thumb_allowlist():
    t, a = ytmusic.clean_track_meta("Noize MC — Спящая красавица (Official Video) [4K]", "Noize MC")
    assert t == "Спящая красавица" and a == "Noize MC"
    assert ytmusic.is_allowed_thumb_url("https://i.ytimg.com/vi/abc/hq720.jpg") is True
    assert ytmusic.is_allowed_thumb_url("https://lh3.googleusercontent.com/xyz=w544") is True
    assert ytmusic.is_allowed_thumb_url("http://i.ytimg.com/vi/abc/hq.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://127.0.0.1/evil.png") is False
    assert ytmusic.is_allowed_thumb_url("https://evil.example.com/a.jpg") is False


def test_radio_and_lyrics_from_next(monkeypatch):
    def fake_post(body, endpoint="browse"):
        if endpoint == "next":
            return {
                "contents": {"singleColumnMusicWatchNextResultsRenderer": {"tabbedRenderer": {"watchNextTabbedResultsRenderer": {"tabs": [
                    {"tabRenderer": {"title": "Далее", "content": {"musicQueueRenderer": {"content": {"playlistPanelRenderer": {"contents": [
                        {"playlistPanelVideoRenderer": {"videoId": "r1", "title": {"runs": [{"text": "R1"}]}, "shortBylineText": {"runs": [{"text": "Art1"}]}, "lengthText": {"runs": [{"text": "3:10"}]}, "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/r1/hq.jpg"}]}}}
                    ]}}}}}},
                    {"tabRenderer": {"title": "Текст", "endpoint": {"browseEndpoint": {"browseId": "MPLYt_1"}}}},
                ]}}}}
            }
        return {"contents": {"sectionListRenderer": {"contents": [{"musicDescriptionShelfRenderer": {"description": {"runs": [{"text": "Line 1\nLine 2"}]}}}]}}}
    monkeypatch.setattr(ytmusic, "_post", fake_post)
    rad = ytmusic.radio("v0", limit=10)
    assert rad[0]["id"] == "r1" and rad[0]["duration"] == 190
    ly = ytmusic.lyrics("v0")
    assert "Line 1" in ly["text"] and ly["source"] == "YouTube Music"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ytmusic.py -v`
Expected: FAIL (`AttributeError` for `recent_yt`, `SEARCH_PARAMS`, `clean_track_meta`, `radio`, `lyrics`, `is_allowed_thumb_url`).

- [ ] **Step 3: Implement `recent_yt`/`push_recent_yt` in `extmusic/store.py` and extend `extmusic/ytmusic.py`**

- In `extmusic/store.py`: добавить методы `recent_yt() -> List[Dict[str, Any]]` и `push_recent_yt(track: Dict[str, Any], limit: int = 30) -> List[Dict[str, Any]]`, сохраняющие облегчённый словарь трека (`source`, `id`, `title`, `artist`, `album`, `duration`, `thumb`, `artist_id`) в ключ `"yt_recent"` файла `.extmusic.json`.
- In `extmusic/ytmusic.py`:
  - Обобщить `_post(body, endpoint="search", timeout=20)` для `https://music.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false`.
  - Сохранить обратную совместимость `track_from_item`: добавлять `artist_id` / `album_id` в результат **только** когда `navigationEndpoint.browseEndpoint.browseId` найден в соответствующих runs (чтобы старые тесты `==` проходили без изменений!). Добавить `item_from_responsive(item, expected_kind)` для результатов поиска `albums` (`MPREb_`/`OLAK5uy_`), `artists` (`UC...`), `playlists` (`VL...`), `videos`, `songs`.
  - Реализовать `SEARCH_PARAMS`, `search(query, count=30, kind="songs")`, `home(limit=30)` (`FEmusic_home`), `explore()` (`FEmusic_explore`), `charts(country="RU")` (`FEmusic_charts`), `browse_mood(browse_id, params="", continuation="", limit=60)`, `browse_collection(browse_id, params="", limit=100)` (разбор `musicResponsiveHeaderRenderer` / `musicDetailHeaderRenderer` / `musicImmersiveHeaderRenderer`, подстановка `album` + `thumb` трекам альбома, разбор полок артиста `albums`/`similar`), `radio(video_id, playlist_id="", limit=30)`, `lyrics(video_id)`, `clean_track_meta(title, artist)`, `is_allowed_thumb_url(url)`, `fetch_thumb(url, max_bytes=2 << 20)`.
  - Добавить тесты в `tests/test_ytmusic.py` также для `browse_collection` (альбом/артист) и `explore`/`charts`/`home` с мокнутым `_post`.

- [ ] **Step 4: Run tests to verify all pass**

Run: `pytest tests/test_ytmusic.py -v`
Expected: All existing + new tests in `tests/test_ytmusic.py` PASS.

- [ ] **Step 5: Commit**

```bash
git add extmusic/store.py extmusic/ytmusic.py tests/test_ytmusic.py
git commit -m "feat(ytmusic): add search filters, collections, explore/charts, radio, lyrics, and recent history"
```

---

### Task 2: Dual-Transport Account Likes, Dislikes, Live `likeStatus`, Dual Home & Public Fallback

**Files:**
- Modify: `extmusic/ytaccount.py` (refactor `_post(body, endpoint="browse")`, add `like`, `dislike`, `liked_ids`, `track_like_status`, update `playlist()` fallback, upgrade `home()` to return `quick_picks`/`mixes`/`feed` without crashing)
- Test: `tests/test_ytaccount.py`

**Interfaces:**
- Produces in `extmusic/ytaccount.py`:
  - `YTAccount._post(body: Dict[str, Any], retry: bool = True, endpoint: str = "browse") -> Dict[str, Any]`
  - `YTAccount.liked_ids(refresh: bool = False) -> Set[str]`
  - `YTAccount.is_liked(video_id: str) -> Optional[bool]`
  - `YTAccount.like(video_id: str, on: bool = True) -> Dict[str, Any]` (`{"ok": True, "liked": bool, "video_id": str}`)
  - `YTAccount.dislike(video_id: str) -> Dict[str, Any]` (`{"ok": True, "disliked": True, "video_id": str}`)
  - `YTAccount.playlist(browse_id: str, limit: int = 100) -> Dict[str, Any]` OR keeps returning `List[Dict]`? Wait: let's check existing callers of `yt_account.playlist()`! In `service.py:472`, `yt_account_playlist()` wraps `{"tracks": ...}`; keep `ytaccount.playlist()` signature or update `service.py` caller cleanly! Keeping `ytaccount.playlist()` returning `List[Dict]` AND adding `YTAccount.collection(browse_id, params="", limit=100) -> Dict[str, Any]` avoids breaking existing callers while adding full collection metadata!
  - `YTAccount.home(limit: int = 30) -> Dict[str, Any]` returning `{"tracks": [...], "moods": [...], "quick_picks": [...], "mixes": [...], "feed": [...]}`

- [ ] **Step 1: Write failing tests for dual-transport `like`, `dislike`, `is_liked`, `liked_ids`, cookie 401/403 error message, and structured `home()`**

```python
# In tests/test_ytaccount.py
def test_post_uses_endpoint_for_oauth_and_cookies(tmp_path, monkeypatch):
    seen_urls = []
    class FakeResp:
        def __init__(self, body): self._b = body
        def read(self, n=None): return json.dumps(self._b).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def fake_urlopen(req, timeout=25):
        seen_urls.append((req.full_url, json.loads(req.data.decode()), dict(req.header_items())))
        return FakeResp({"ok": True})
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)
    acc.like("vid1", True)
    assert "www.youtube.com/youtubei/v1/like/like" in seen_urls[-1][0]
    assert seen_urls[-1][1]["target"] == {"videoId": "vid1"}
    assert "vid1" in acc.liked_ids()

    acc.like("vid1", False)
    assert "www.youtube.com/youtubei/v1/like/removelike" in seen_urls[-1][0]
    assert "vid1" not in acc.liked_ids()

    store.clear_oauth()
    store.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ts123\n")
    acc.dislike("vid2")
    assert "music.youtube.com/youtubei/v1/like/dislike" in seen_urls[-1][0]


def test_home_combines_public_music_and_tv_feed_resiliently(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)
    monkeypatch.setattr("extmusic.ytmusic.home", lambda limit=30: {"tracks": [{"id": "qp1", "title": "Quick", "artist": "Art"}], "shelves": [], "moods": [{"title": "Workout", "browse_id": "FEmusic_mood", "params": "p1"}]})
    monkeypatch.setattr(acc, "_browse", lambda browse_id="", continuation="": {
        "contents": {"a": {"tileRenderer": {"contentId": "tv1", "metadata": {"tileMetadataRenderer": {"title": {"simpleText": "YouTube Video"}, "lines": []}}}}}
    } if browse_id == "FEwhat_to_watch" else {})
    res = acc.home(limit=20)
    assert any(t["id"] == "qp1" for t in res["quick_picks"])
    assert any(t["video_id"] == "tv1" for t in res["feed"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ytaccount.py -v`
Expected: FAIL (`AttributeError: 'YTAccount' object has no attribute 'like'`).

- [ ] **Step 3: Implement dual-transport `_post(endpoint=...)`, `like`, `dislike`, `liked_ids`, `is_liked`, `collection`, and resilient `home` in `extmusic/ytaccount.py`**

- Refactor `_post(self, body, retry=True, endpoint="browse")`: validate `endpoint` against allowed characters `[a-z0-9_/]+`, build `https://www.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false` for OAuth (`TVHTML5`) and `https://music.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false` for cookies (`WEB_REMIX` + `SAPISIDHASH`). Retain transient DNS/URLError single retry and 401/403 clear user prompt.
- Add `liked_ids`, `is_liked` (checks cached `liked_ids` first, queries `next` `likeStatus` on unknown active track if authenticated), `like(video_id, on=True)`, `dislike(video_id)` (updates local `_liked_ids` cache optimistically alongside API call).
- In `home(limit=30)`: gracefully call `ytmusic.home()`, personalized `mixes` (derived from `library()` artists / liked songs radio seeds when authenticated), and `feed` (`FEwhat_to_watch` under OAuth or personal `FEmusic_home` under cookies), keeping `tracks` populated for backwards compatibility (`assistant_max.py` fallback!), and catching partial failures so an upstream blip in one shelf doesn't drop the whole response.
- In `playlist(browse_id, limit)` and new `collection(browse_id, params="", limit=100)`: fallback to `ytmusic.browse_collection()` when not authenticated or when viewing `MPREb_` / public collections so albums/artists work identically logged in or out.

- [ ] **Step 4: Run tests to verify all pass**

Run: `pytest tests/test_ytaccount.py tests/test_assistant_extmusic.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add extmusic/ytaccount.py tests/test_ytaccount.py
git commit -m "feat(ytaccount): implement YouTube like/dislike sync, dual home feeds, and album/artist collection fallback"
```

---

### Task 3: Full Playback Engine State — Shuffle, Repeat, Queue Ops, Auto-Radio, Lyrics, Artist & Glagol Auto-Stop

**Files:**
- Modify: `extmusic/service.py`
- Test: `tests/test_extmusic.py`

**Interfaces:**
- Produces on `ExtMusicService`:
  - Attributes in `status()`: `"shuffle": bool`, `"repeat": str`, `"autoplay_radio": bool`, `"native": bool`
  - Methods: `set_shuffle(enabled: Optional[bool] = None) -> Dict[str, Any]`, `set_repeat(mode: Optional[str] = None) -> Dict[str, Any]`, `set_autoplay(enabled: bool) -> Dict[str, Any]`
  - Methods: `queue_add(items: Any, mode: str = "end") -> Dict[str, Any]`, `queue_remove(index: int) -> Dict[str, Any]`, `queue_move(from_index: int, to_index: int) -> Dict[str, Any]`, `queue_clear_upcoming() -> Dict[str, Any]`, `radio_play(track: Any = None, browse_id: str = "") -> Dict[str, Any]`
  - Methods: `yt_home()`, `yt_explore()`, `yt_charts(country="RU")`, `yt_mood(browse_id, params="", continuation="", limit=60)`, `yt_search(query, count=30, kind="songs")`, `yt_collection(browse_id, params="", limit=100)`
  - Methods: `yt_like(video_id: str, on: bool = True) -> Dict[str, Any]`, `yt_dislike(video_id: str) -> Dict[str, Any]`, `yt_track_liked(video_id: str) -> bool`, `yt_similar(video_id: str) -> List[Dict[str, Any]]`, `yt_lyrics(video_id: str, title: str = "", artist: str = "", album: str = "", duration: float = 0) -> Dict[str, Any]`, `yt_artist(browse_id: str) -> Dict[str, Any]`, `fetch_thumb(url: str) -> Tuple[bytes, str]`
  - Method: `maybe_auto_stop_from_glagol(glagol_state: Dict[str, Any]) -> bool`

- [ ] **Step 1: Write failing tests for shuffle/repeat/autoplay-radio, queue manipulation, native gating, and Glagol auto-stop boundary**

```python
# In tests/test_extmusic.py
def test_shuffle_repeat_and_queue_manipulation(tmp_path, monkeypatch):
    svc = _make_gst_test_service(tmp_path, monkeypatch)  # FakeGst / FakePlayer without NativeYT
    tracks = [{"source": "yt", "id": f"v{i}", "title": f"T{i}", "artist": "A"} for i in range(4)]
    svc.play(tracks[0], tracks)
    svc.set_shuffle(True)
    assert svc.status()["shuffle"] is True
    assert svc.current["id"] == "v0" and svc.index == 0
    svc.set_shuffle(False)
    assert [t["id"] for t in svc.queue] == ["v0", "v1", "v2", "v3"]

    svc.set_repeat("all")
    svc.index = 3
    svc.current = svc.queue[3]
    svc.next()
    assert svc.current["id"] == "v0"

    svc.queue_add({"source": "yt", "id": "v99", "title": "NextUp", "artist": "A"}, mode="next")
    assert svc.queue[svc.index + 1]["id"] == "v99"
    svc.queue_remove(svc.index + 1)
    assert all(t["id"] != "v99" for t in svc.queue)


def test_auto_radio_at_queue_end_and_native_isolation(tmp_path, monkeypatch):
    svc = _make_gst_test_service(tmp_path, monkeypatch)
    monkeypatch.setattr("extmusic.ytmusic.radio", lambda vid, pid="", limit=30: [{"source": "yt", "id": "rad1", "title": "Radio", "artist": "R"}])
    svc.play({"source": "yt", "id": "v1", "title": "One", "artist": "A"})
    svc.next(auto=True)
    assert svc.current["id"] == "rad1"

    # NativeYT isolation
    svc.current = {"source": "yt", "id": "nat1", "title": "Nat", "native": True}
    with pytest.raises(ExtMusicError):
        svc.set_shuffle(True)


def test_glagol_auto_stop_ignores_fallback_track_and_stops_on_new_track(tmp_path, monkeypatch):
    svc = _make_gst_test_service(tmp_path, monkeypatch)
    svc.play({"source": "yt", "id": "v1", "title": "One", "artist": "A"})
    # Must ignore GstBridge dummy track 153109827
    assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "153109827", "playerType": "yandex"}}) is False
    assert svc.current is not None
    # Must stop when an independent track starts after extmusic start
    svc._ext_started_at -= 10
    assert svc.maybe_auto_stop_from_glagol({"playing": True, "playerState": {"id": "999999", "playerType": "yandex"}}) is True
    assert svc.current is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extmusic.py -v`
Expected: FAIL (`AttributeError: set_shuffle / queue_add / maybe_auto_stop_from_glagol`).

- [ ] **Step 3: Implement queue/shuffle/repeat/auto-radio, history logging, YouTube extras wrappers, `NativeYT` guards, and Glagol auto-stop in `extmusic/service.py`**

- При запуске `play()` записывать YouTube-трек в `self.store.push_recent_yt()`, фиксировать `self._ext_started_at = time.time()`, инициализировать/обновлять `self._orig_queue` (если переключение не внутри существующей перемешанной очереди — либо аккуратно поддерживать `_orig_queue`), сбрасывать если передана новая явная `queue` и применять `self.shuffle`, если `shuffle` уже включён.
- Реализовать `set_shuffle`, `set_repeat`, `set_autoplay`, `queue_add`, `queue_remove`, `queue_move`, `queue_clear_upcoming`, `radio_play`. Все мутации защищать проверкой `if self._is_native_current(): raise ExtMusicError(...)`.
- Обновить `next(auto=False)`:
  - `auto and self.repeat == "one"` -> `seek(0)` / перезапуск текущего трека.
  - конец очереди -> при `repeat == "all"` переход к индексу `0`; при `repeat == "none"` и `autoplay_radio` + `yt` -> догрузка `ytmusic.radio(...)`, фильтрация дубликатов, аппенд в `queue` и переход к новому треку.
- Вызвать `self.next(auto=True)` из `_poll_loop` при завершении трека.
- Добавить `maybe_auto_stop_from_glagol(glagol_state)`: игнорирует пока `time.time() - self._ext_started_at < 3.0`, игнорирует `not playing`, игнорирует `playerState.id == "153109827"` и `"ext:"`; останавливает (`self.stop()`) и возвращает `True`, если `glagol` активно играет сторонний трек Яндекс Музыки или `bluetooth`.
- Добавить `yt_home`, `yt_explore`, `yt_charts`, `yt_mood`, `yt_search`, `yt_collection`, `yt_like`, `yt_dislike` (сохраняя отметку в аккаунте + опционально `self.next()`), `yt_similar`, `yt_lyrics` (пробует `ytmusic.lyrics(vid)` -> при пустом результате зовёт `panel.ymusic.lyrics_by_name(*ytmusic.clean_track_meta(title, artist), album, duration)`), `yt_artist`, `fetch_thumb`.

- [ ] **Step 4: Run tests to verify all pass**

Run: `pytest tests/test_extmusic.py tests/test_assistant_extmusic.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add extmusic/service.py tests/test_extmusic.py
git commit -m "feat(service): add shuffle, repeat, queue editing, auto-radio, recent history, and Glagol handoff"
```

---

### Task 4: Wire HTTP Routes & YapAnel Player Bridge (`api/extmusic.py`, `api/media.py`, `server.py`)

**Files:**
- Modify: `api/extmusic.py`
- Modify: `api/media.py:15-43`
- Modify: `server.py:6279-6316,6356-6399,6430-6515`
- Test: `tests/test_http_api.py` (or dedicated route tests in `tests/test_extmusic.py`)

**Interfaces:**
- HTTP routes in `api/extmusic.py`:
  - `GET /api/extmusic/search` (дополнительно параметр `kind`)
  - `GET /api/extmusic/yt/search`, `/api/extmusic/yt/home`, `/api/extmusic/yt/explore`, `/api/extmusic/yt/charts`, `/api/extmusic/yt/mood`, `/api/extmusic/yt/collection`
  - `POST /api/extmusic/yt/like` (`{"id": str, "like": bool}`)
  - `POST /api/extmusic/yt/dislike` (`{"id": str}`)
  - `POST /api/extmusic/shuffle` (`{"enable": Optional[bool]}`)
  - `POST /api/extmusic/repeat` (`{"mode": Optional[str]}`)
  - `POST /api/extmusic/autoplay` (`{"enable": bool}`)
  - `POST /api/extmusic/queue/add`, `/api/extmusic/queue/remove`, `/api/extmusic/queue/move`, `/api/extmusic/queue/clear`
  - `POST /api/extmusic/radio` (`{"track": ..., "browse_id": ...}`)
  - `GET /api/extmusic/thumb` -> `Response(bytes, content_type=ctype, headers=(("Cache-Control", "public, max-age=86400"), ("Access-Control-Allow-Origin", "*")))`
- Updated methods in `server.py`: `music()`, `music_cmd()`, `music_info()`, `music_lyrics()`, `music_similar()`, `music_like()`, `music_dislike()`, `music_play()`, `_ext_track_payload()`
- Updated `/api/music/artist` in `api/media.py` handling `id` starting with `ext:yt:`

- [ ] **Step 1: Write failing tests for HTTP routes, `server.py` music bridge (shuffle/repeat/like/dislike/lyrics/similar/artist), and thumb proxy security**

```python
# In tests/test_extmusic.py (or tests/test_http_api.py)
def test_server_music_bridge_forwards_shuffle_repeat_like_and_extras(monkeypatch, tmp_path):
    # Test Panel methods on stubbed panel object with ExtMusicService
    ...
    p = panel.music()
    ep = p["glagol"]["state"]["playerState"]
    assert ep["entityInfo"] == {"shuffled": False, "repeatMode": "None"}
    panel.music_cmd({"command": "shuffle", "enable": True})
    panel.music_cmd({"command": "repeat", "mode": "One"})
    assert panel._extmusic_service.shuffle is True
    assert panel._extmusic_service.repeat == "one"

    res_like = panel.music_like("ext:yt:abc123", True)
    assert res_like["liked"] is True
    res_dislike = panel.music_dislike("ext:yt:abc123")
    assert res_dislike["ok"] is True

    sim = panel.music_similar("ext:yt:abc123")
    assert sim and sim[0]["id"].startswith("ext:yt:")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extmusic.py -v`
Expected: FAIL on stubbed `server.py` (`entityInfo` missing, `music_similar` returns `[]`, `music_like` ignores `yt_like`).

- [ ] **Step 3: Register routes in `api/extmusic.py`, update `/api/music/artist` in `api/media.py`, and remove external stubs in `server.py`**

- `api/extmusic.py`: добавить все новые маршруты и `/api/extmusic/thumb` (ошибка `ExtMusicError` / `ValueError` при невалидном URL возвращает 400). Проверить, чтобы `/api/extmusic/*` loopback-исключение в `server.py:7415` продолжало пускать запросы ассистента и не открывало ничего лишнего наружу (для браузера действует стандартный cookie-guard!). Внимание: `/api/extmusic/thumb` требует авторизованную сессию панели (как и остальные `/api/extmusic/`), но т.к. `<img>` / `fetch` браузера отправляет cookie сессии панели (same-origin), `canvas` прочитает пиксели для `plTint()` без проблем!
- `server.py`:
  - В `music()` вызывать `ext.maybe_auto_stop_from_glagol(g.get("state") or {})` до формирования `ext_p`; добавлять `entityInfo` (`shuffled`, `repeatMode`), `native`, `autoplayRadio`, корректные `hasPrev`/`hasNext` (с учётом `repeat` и `autoplay_radio`).
  - В `_ext_track_payload()` добавлять `liked` (через сервис) и `artist_id` -> `artistList[0].id = f"ext:yt:{artist_id}"` если он есть.
  - В `music_cmd()`: вызывать `ext.set_shuffle` и `ext.set_repeat` вместо пустой заглушки.
  - В `music_like()` и `music_dislike()`: вызывать `ext.yt_like(...)` и `ext.yt_dislike(...)` (который сразу вызывает `ext.next()` как в Яндекс Музыке).
  - В `music_lyrics()`: для `ext:yt:<vid>` использовать `ext.yt_lyrics(...)`.
  - В `music_similar()`: возвращать `[self._ext_track_payload(t) for t in ext.yt_similar(vid)]`.
  - В `music_play()`: разрешить запуск `ext:yt:<vid>` и когда трек пришёл из «Похожих» / «Об исполнителе» (если его нет в текущей `ext.queue`, передать/создать мини-трек из похожих или `ytmusic`/`play`).
- `api/media.py`: обработать `ext:yt:<UC...>` в `/api/music/artist` через `get_service(r.panel).yt_artist(...)`.

- [ ] **Step 4: Run tests to verify all pass**

Run: `pytest tests/test_extmusic.py tests/test_http_api.py tests/test_panel_fixes.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add api/extmusic.py api/media.py server.py tests/test_extmusic.py
git commit -m "feat(api): expose yt catalog, queue/playback controls, thumb proxy, and remove server.py music stubs"
```

---

### Task 5: Unified YapAnel Player Integration (`#minibar` & `#tab-player`) in `static/index.html` and `static/app.js`

**Files:**
- Modify: `static/index.html` (кнопка `■ Стоп` в шапке `#tab-player`, поддержка авто-радио и действий в очереди `#pl-queue`, мелкие CSS стили для кнопок трек-строк и шапок)
- Modify: `static/app.js` (`showTab()` парсинг `?`, `renderMusic()`, `loadPlayerExtras()`, `plTint()` через `/api/extmusic/thumb`, `musicAction()`, `artist` link `#yt?artist=...`)
- Test: `node tests/test_ui_hardening.mjs`, `node tests/test_paths.mjs`, `node tests/test_renderer.mjs`

**Interfaces:**
- Consumes HTTP endpoints from Task 4 (`/api/music/*`, `/api/extmusic/stop`, `/api/extmusic/autoplay`, `/api/extmusic/queue/remove`, `/api/extmusic/thumb`).
- Dispatches `window.dispatchEvent(new CustomEvent('yt:navigate', { detail }))` при клике по исполнителю/источнику из `#tab-player`, а также парсит `location.hash` `#yt?...`.

- [ ] **Step 1: Add/run frontend regression check for `showTab()` query hashes & JS syntax**

Verify current JS test suite passes first, and check how `test_ui_hardening.mjs` validates `index.html` & `app.js`:
Run: `node tests/test_ui_hardening.mjs; node tests/test_paths.mjs`
Expected: Existing checks pass (establishing baseline before editing).

- [ ] **Step 2: Update `static/index.html` for `#tab-player` external Stop button & queue controls**

- В `#tab-player > .page-head` добавить кнопку `<button type="button" class="btn sm danger-soft" id="pl-ext-stop" hidden>■ Стоп YouTube</button>` (и компактную кнопку в `.pl-mtop` для мобильных).
- В шапку секции `Очередь` (`#pl-queue` card-head) добавить `<label class="row" id="pl-autoradio-wrap" style="gap:6px;font-size:12.5px" hidden><input type="checkbox" class="switch" id="pl-autoradio"><span>Авто-радио</span></label>` и кнопку «Очистить».
- Добавить аккуратные стили для обложек коллекций, строк треков с действиями (`trow-acts`) и адаптивных карточек `.yt-cards` / `.yt-hero` strictly using `--card`, `--field`, `--line`, `--accent`, `--accent-soft`, `--muted`, `--text`.

- [ ] **Step 3: Update `static/app.js` to unlock full YouTube Music features inside `#minibar`, `#music-card`, and `#tab-player`**

- В `showTab()`: разбирать `const [rawH, queryStr] = location.hash.slice(1).split('?');` -> сохранять `tab` корректно для `#yt?...` и уведомлять `window.dispatchEvent(new CustomEvent('panel:tab', { detail: { tab, query: queryStr || '' } }))`.
- В `renderMusic()`:
  - Показывать `#pl-ext-stop` (и мобильную кнопку стопа) только при `isExt && !isBt` (`onclick` вызывает `POST /api/extmusic/stop` и `loadMusic()`).
  - В строке 3961 изменить блокировку кнопок: разрешить `shuffle`, `repeat`, `like`, `dislike`, если `!isBt` и `(!isExt || (p?.extSource === 'yt' && !p?.native))` (для VK/Bluetooth остаются disabled).
  - Ссылки названия/артиста и `#pl-source`: для `isExt` клик на источник ведёт в `#yt`, а если у `t.artistList[0].id` префикс `ext:yt:`, ссылка ведёт в `#yt?artist=<encodeURIComponent(id.slice(7))>` (внутренний переход, без ухода на `music.yandex.ru`!).
  - В `plTint()`: для внешних https URL обёртывать адрес в `proxyThumbUrl(url) => '/api/extmusic/thumb?url=' + encodeURIComponent(url)`, чтобы `<canvas>` не получал CORS taint и цветная подсветка `--pl-tint` работала для YouTube Music как для Яндекс Музыки!
- В `loadPlayerExtras()`:
  - Убрать блокировку `(isBt || isExt) ? none` у `/api/music/similar?id=...` для `isExt && p?.extSource === 'yt'`, отправляя `api('/api/music/similar?id=' + encodeURIComponent(id))`.
  - Для `lyUrl` у `isExt && p?.extSource === 'yt'` передавать `id` (`'/api/music/lyrics?id=' + encodeURIComponent(id)`), чтобы сервер сначала искал нативные тексты YouTube Music (`MPLYt`), а затем чистый LRCLIB, и добавить `'YouTube Music': 'Текст: YouTube Music'` в словарь `SRC`.
  - Убрать заглушку `if (isExt) { $('#pl-artist').innerHTML = ...; return; }` для `extSource === 'yt'`, запрашивая `/api/music/artist?id=` + `aid` (с кликабельным переходом на страницу артиста внутри `#tab-yt` и списком популярных треков!).
  - В рендере `#pl-queue`: при `isExt` показывать `#pl-autoradio-wrap`, синхронизировать его checked с `mu.info.extmusic.autoplay_radio`, а в `trackRow` для предстоящих (`k > cur`) добавлять кнопку удаления из очереди `✕` (`POST /api/extmusic/queue/remove`).

- [ ] **Step 4: Run JS checks and verify syntax & DOM invariants**

Run: `node tests/test_ui_hardening.mjs; node tests/test_paths.mjs; node tests/test_renderer.mjs; node tests/test_voice_input.mjs`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/app.js
git commit -m "feat(ui): unlock YouTube controls, lyrics, similar tracks, artist card, queue actions, and ambient cover tint in YapAnel player"
```

---

### Task 6: Modern YapAnel-Styled YouTube Music Hub (`static/index.html`, `static/extmusic.js`, `static/sw.js`) + Live Verification

**Files:**
- Modify: `static/index.html:1897-2012` (полная переработка HTML секции `#tab-yt`)
- Modify: `static/extmusic.js` (полная логика каталога, навигатора, поиска с фильтрами, страниц коллекций/альбомов/артистов, реальных обложек, лайков, очереди, deep-link `#yt?artist=...`)
- Modify: `static/sw.js` (обновление `NAMESPACE` на `panel-v12` и версии `extmusic.js?v=...` / `app.js?v=...`)
- Modify: `.gitignore` (добавить `.superpowers/`)
- Test: `tests/test_ui_hardening.mjs`, `node --check static/extmusic.js`, `pytest`

**Interfaces:**
- Consumes all Task 4 `/api/extmusic/*` endpoints and triggers `window.loadMusic()` после `play` / `stop` / `like`, чтобы `#minibar` и `#tab-player` обновлялись мгновенно без ожидания поллинга!
- Слушает `panel:tab` (`hashchange`) для обработки `#yt?artist=<id>` и `#yt?browse=<id>`.

- [ ] **Step 1: Verify baseline node syntax & UI test expectations before replacing `#tab-yt`**

Check if any tests in `tests/` reference old `#yt-player-card` or `#yt-*` DOM IDs using search!
Run: `rg -n "yt-player-card|yt-liked-open|yt-results|yt-account-card" tests/`
Expected: Identify any referenced IDs so existing tests stay green or get updated if testing obsolete `#yt-player-card`.

- [ ] **Step 2: Replace `#tab-yt` markup in `static/index.html`**

- Удалить `#yt-player-card` целиком.
- Построить шапку `.page-head`: заголовок «YouTube Music», подзаголовок статуса/аккаунта, кнопка `■ Стоп` (`#yt-ext-stop`, hidden когда ничего не играет) + `.seg` переключатель разделов: **Главная**, **Навигатор**, **Поиск**, **Библиотека**, **Аккаунт**.
- Сохранить `#yt-error`, `#yt-search-form` + фильтр-пилюли (`songs`, `albums`, `artists`, `playlists`, `videos`), контейнер детальной страницы коллекции (`#yt-detail-view` с кнопкой `← Назад`, `.pl-hero`-образной шапкой и треками/альбомами артиста), секции Home (`Недавнее`, `Быстрый выбор`, `Миксы для вас`, `Из вашей ленты YouTube`), Навигатора (`Чарты`, `Новые релизы`, `Настроения`), Библиотеки (фильтры *Всё / Плейлисты / Исполнители* — только чтение и воспроизведение, без кнопок создания/редактирования!), и существующие формы QR OAuth + `cookies.txt` в разделе **Аккаунт** (сохранив их ID для безболезненной работы `login_start` / cookies).

- [ ] **Step 3: Implement modern `static/extmusic.js`**

- Везде в карточках и строках треков рендерить настоящие `<img>` с обложками (`onerror` мягко переключает на SVG-иконку ноты, а не показывает сломанную картинку). Для артистов — круглые обложки (`border-radius: 50%`).
- Отображать кнопки в строках треков: лайк `♥`/`♡` (с синхронизацией с `/api/extmusic/yt/like` и синхронизацией кэша `likedSet`), `⏭+` (Играть следующим -> `/api/extmusic/queue/add` `mode="next"`), `☰+` (В очередь -> `mode="end"`).
- Клик по альбому/плейлисту/артисту открывает `#yt-detail-view` в стиле `.pl-hero` (кнопки `▶ Играть всё`, `🔀 Перемешать`, `📻 Радио` -> `/api/extmusic/radio`); у артиста также показывает сетку его альбомов/синглов.
- После любого `play` / `stop` / `like` дергать `loadMusic()` (если доступна глобально в `app.js`), чтобы `#minibar` и `#tab-player` обновлялись за долю секунды.
- Поддержать внутренний стек навигации (`← Назад`) и внешние ссылки `#yt?artist=<id>` / `#yt?browse=<id>` из `#tab-player`.

- [ ] **Step 4: Bump `static/sw.js` cache version & update `.gitignore`**

- Обновить `panel-v12` и query-строки версий скриптов в `static/sw.js` и `static/index.html` (`?v=0.3.24`).
- Добавить `.superpowers/` в `.gitignore` (и при желании удалить временные файлы сессии).

- [ ] **Step 5: Run full automated test suite (Python + Node)**

Run:
`pytest`
`node --check static/extmusic.js`
`node tests/test_ui_hardening.mjs`
`node tests/test_renderer.mjs`
`node tests/test_paths.mjs`
`node tests/test_voice_input.mjs`
Expected: 100% PASS without skipped/relaxed assertions.

- [ ] **Step 6: Deploy updated files to Station Max (`<IP_КОЛОНКИ>:5555`), restart panel, and verify live API endpoints**

- Загрузить обновлённые `server.py`, `api/extmusic.py`, `api/media.py`, `extmusic/*`, `static/*` в `/data/yapanel/` на Станции Макс.
- Перезапустить процесс `server.py`, убедиться что ассистент `assistant_max.py` работает без сбоев.
- Проверить реальными curl-запросами: `/api/extmusic/yt/home`, `/api/extmusic/yt/explore`, `/api/extmusic/yt/charts`, `/api/extmusic/yt/search?kind=albums&q=Linkin+Park`, `/api/extmusic/yt/collection?browse_id=UCZU9T1ceaOgwfLRq7OKFU4Q`, `/api/music/lyrics?id=ext:yt:VMkTBHQn1vo`, `/api/music/similar?id=ext:yt:VMkTBHQn1vo`, `/api/extmusic/thumb?...` (включая отказ 400 на посторонний домен).

---

## Self-Review Results

- **Spec coverage check:**
  - Единый стиль + удаление `#yt-player-card` -> Task 5 + Task 6.
  - Реальные обложки + `--pl-tint` proxy -> Task 1 + Task 4 + Task 5 + Task 6.
  - Настоящие лайки/дизлайки в YouTube (`TVHTML5` + `cookies.txt`) + разблокировка кнопок в `#minibar`/`#tab-player` -> Task 2 + Task 3 + Task 4 + Task 5 + Task 6.
  - Полный «Стоп» (`■ Стоп` + авто-сброс при внешнем `Glagol`) -> Task 3 + Task 4 + Task 5 + Task 6.
  - Read-only коллекции (Плейлист/Альбом/Артист) с `.pl-hero` и `Играть/Перемешать/Радио` -> Task 1 + Task 2 + Task 6.
  - Главная (`Недавнее`, `Быстрый выбор`, `Миксы для вас`, `Из вашей ленты YouTube`), Навигатор (`Чарты`, `Новые релизы`, `Настроения`), Поиск с фильтрами, Библиотека (*Всё / Плейлисты / Исполнители*) -> Task 1 + Task 2 + Task 6.
  - Очередь (добавление, удаление, перемещение, авто-радио), Shuffle/Repeat, Тексты песен (`MPLYt` + чистый LRCLIB), Похожие и Об артисте в `#tab-player` -> Task 1 + Task 3 + Task 4 + Task 5.
- **Consistency check:** Имена методов (`recent_yt`, `push_recent_yt`, `clean_track_meta`, `is_allowed_thumb_url`, `set_shuffle`, `set_repeat`, `set_autoplay`, `queue_add`, `maybe_auto_stop_from_glagol`), маршруты `/api/extmusic/...` и ID элементов (`#pl-ext-stop`, `#yt-ext-stop`, `#pl-autoradio-wrap`, `#yt-detail-view`) согласованы между всеми 6 задачами.
- **Gaps fixed during self-review:** Учтено требование `tests/test_ytmusic.py::test_track_from_item_maps_yt_music_fields` (точное сравнение словаря трека без лишних пустых полей), а также обновление кэша `static/sw.js`.
