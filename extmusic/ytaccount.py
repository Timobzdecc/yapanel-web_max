"""YouTube Music account library through OAuth or Cookies.

No Android applications (ReVanced, GmsCore, MicroG) are needed on the speaker.
Supports:
1. Google OAuth 2.0 Device Flow (RFC 8628).
2. Netscape cookies.txt or browser Cookie header import.

Fetches personal Liked Music (FEmusic_liked_videos), playlists, artists, and mixes.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Set

from . import ipv4_urlopen, ytmusic
from .google_oauth import GoogleOAuth, GoogleOAuthError
from .store import ExtMusicStore
from .ytmusic import YTMusicError

API = "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false"
OAUTH_API_BASE = "https://www.youtube.com/youtubei/v1"
COOKIES_API_BASE = "https://music.youtube.com/youtubei/v1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CLIENT_VERSION = "1.20241111.01.00"
_ENDPOINT_RE = re.compile(r"^[a-z0-9_/]+$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PLAYLIST_ID_RE = re.compile(r"^(?:PL|OLAK5uy_|RD|LM|LL|WL)[A-Za-z0-9_-]{0,200}$")
_BROWSE_COLLECTION_RE = re.compile(r"^(?:VL|PL|UC|MPREb_|OLAK5uy_|FE|RD)[A-Za-z0-9_-]{2,200}$")
LIKED_THUMB_URL = "https://www.gstatic.com/youtube/media/ytm/images/pbg/liked-songs-delhi-576.png"


class YTAccountError(ValueError):
    """A YouTube Music account error that can be shown to the user."""


def _text(value: Any, limit: int = 300) -> str:
    if isinstance(value, dict):
        if "simpleText" in value:
            return str(value.get("simpleText") or "")[:limit]
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(str((run or {}).get("text") or "") for run in runs)[:limit]
    return str(value or "")[:limit]


def _duration(value: str) -> int:
    parts = _text(value, 20).split(":")
    if len(parts) < 2 or len(parts) > 3 or not all(part.isdigit() for part in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _walk(node: Any, key: str) -> Iterable[Any]:
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from _walk(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, key)


def _thumbnail(item: Dict[str, Any]) -> str:
    for thumb in (item.get("thumbnail"), item.get("thumbnailRenderer")):
        if not isinstance(thumb, dict):
            continue
        renderer = thumb.get("musicThumbnailRenderer") or {}
        for source in (
            ((renderer.get("thumbnail") or {}).get("thumbnails") or []),
            (((thumb.get("image") or {}).get("sources")) or []),
        ):
            if not isinstance(source, list):
                continue
            for entry in reversed(source):
                url = _text((entry or {}).get("url"), 1000)
                if url.startswith("https://"):
                    return url
    return ""


def _browse_endpoint(item: Dict[str, Any]) -> Dict[str, str]:
    endpoint = (item.get("navigationEndpoint") or {}).get("browseEndpoint") or {}
    if not endpoint:
        for run in (item.get("title") or {}).get("runs") or []:
            if isinstance(run, dict) and run.get("navigationEndpoint"):
                endpoint = run["navigationEndpoint"].get("browseEndpoint") or {}
                if endpoint:
                    break
    config = ((endpoint.get("browseEndpointContextSupportedConfigs") or {})
              .get("browseEndpointContextMusicConfig") or {})
    return {
        "browse_id": _text(endpoint.get("browseId"), 200),
        "playlist_id": _text(config.get("playlistIdForFallback"), 200),
        "page_type": _text(config.get("pageType"), 80),
    }


def _watch_endpoint(item: Dict[str, Any]) -> Dict[str, str]:
    endpoint = (item.get("navigationEndpoint") or {}).get("watchEndpoint") or {}
    if not endpoint:
        for run in (item.get("title") or {}).get("runs") or []:
            if isinstance(run, dict) and run.get("navigationEndpoint"):
                endpoint = run["navigationEndpoint"].get("watchEndpoint") or {}
                if endpoint:
                    break
    return {
        "video_id": _text(endpoint.get("videoId"), 40),
        "playlist_id": _text(endpoint.get("playlistId"), 200),
    }


def _apply_track_run_links(track: Dict[str, Any], runs: Any) -> None:
    if not isinstance(runs, list) or not runs:
        return
    artist_id, artist_from_run, album_id, album_from_run = ytmusic._extract_run_links(runs)
    if artist_id:
        track["artist_id"] = artist_id
    if artist_from_run and (not track.get("artist") or track.get("artist") == "YouTube Music"):
        track["artist"] = artist_from_run
    if album_from_run and not track.get("album"):
        track["album"] = album_from_run
    if album_id and not track.get("album_id"):
        track["album_id"] = album_id


def _two_column(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    title = _text(item.get("title") or {})
    subtitle_obj = item.get("subtitle") or {}
    subtitle = _text(subtitle_obj)
    if not title:
        return None
    browse = _browse_endpoint(item)
    watch = _watch_endpoint(item)
    base = {
        "title": title,
        "subtitle": subtitle,
        "thumb": _thumbnail(item),
        "browse_id": browse["browse_id"],
        "playlist_id": browse["playlist_id"] or watch["playlist_id"],
        "page_type": browse["page_type"],
        "video_id": watch["video_id"],
    }
    if watch["video_id"]:
        parts = [part.strip() for part in subtitle.split("•") if part.strip()]
        base.update({
            "kind": "track",
            "artist": parts[0] if parts else "YouTube Music",
            "duration": _duration(parts[-1]) if parts else 0,
        })
        if isinstance(subtitle_obj, dict):
            _apply_track_run_links(base, subtitle_obj.get("runs"))
    else:
        base["kind"] = "artist" if browse["page_type"] == "MUSIC_PAGE_TYPE_ARTIST" else "playlist"
    return base


def _two_row(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    title = _text(item.get("title") or {})
    subtitle_obj = item.get("subtitle") or {}
    subtitle = _text(subtitle_obj)
    if not title or title.strip().lower() in ("новый", "создать"):
        return None
    browse = _browse_endpoint(item)
    watch = _watch_endpoint(item)
    base = {
        "title": title,
        "subtitle": subtitle,
        "thumb": _thumbnail(item),
        "browse_id": browse["browse_id"],
        "playlist_id": browse["playlist_id"] or watch["playlist_id"],
        "page_type": browse["page_type"],
        "video_id": watch["video_id"],
        "kind": "track" if watch["video_id"] else ("artist" if browse["page_type"] == "MUSIC_PAGE_TYPE_ARTIST" else "playlist"),
    }
    if watch["video_id"]:
        parts = [part.strip() for part in subtitle.split("•") if part.strip()]
        base.update({
            "artist": parts[0] if parts else "YouTube Music",
            "duration": _duration(parts[-1]) if parts else 0,
        })
        if isinstance(subtitle_obj, dict):
            _apply_track_run_links(base, subtitle_obj.get("runs"))
    return base


def _responsive(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    columns = item.get("flexColumns") or []
    if not isinstance(columns, list) or not columns:
        return None
    title = _text(((columns[0] or {}).get("musicResponsiveListItemFlexColumnRenderer") or {}).get("text") or {})
    parts: List[str] = []
    all_runs: List[Any] = []
    for col_idx, col in enumerate(columns[1:], start=1):
        text = ((col or {}).get("musicResponsiveListItemFlexColumnRenderer") or {}).get("text") or {}
        runs = text.get("runs") or []
        if isinstance(runs, list):
            all_runs.extend(runs)
        if col_idx == 1:
            for run in runs:
                value = _text((run or {}).get("text"))
                if value and value not in ("•", ","):
                    parts.append(value)
    video_id = ""
    for endpoint in _walk(item, "watchEndpoint"):
        if isinstance(endpoint, dict) and endpoint.get("videoId"):
            video_id = _text(endpoint.get("videoId"), 40)
            break
    if not video_id and (item.get("playlistItemData") or {}).get("videoId"):
        video_id = _text(item["playlistItemData"]["videoId"], 40)
    if not title or not video_id:
        return None
    artist = parts[0] if parts else "YouTube Music"
    track: Dict[str, Any] = {
        "kind": "track",
        "video_id": video_id,
        "title": title,
        "artist": artist,
        "duration": _duration(parts[-1]) if len(parts) > 1 else 0,
        "thumb": _thumbnail(item),
        "playlist_id": "",
        "browse_id": "",
        "page_type": "",
        "subtitle": " • ".join(parts),
    }
    _apply_track_run_links(track, all_runs)
    return track


def _tile_track(tile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(tile, dict):
        return None
    meta = tile.get("metadata", {}).get("tileMetadataRenderer", {})
    title_obj = meta.get("title", {})
    title = _text(title_obj)

    video_id = ""
    runs = title_obj.get("runs") or []
    for run in runs:
        v = (run or {}).get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
        if v:
            video_id = _text(v, 40)
            break
    if not video_id:
        video_id = _text(tile.get("onSelectCommand", {}).get("watchEndpoint", {}).get("videoId") or "", 40)
    if not video_id:
        video_id = _text(tile.get("contentId") or "", 40)
    if not video_id:
        for item in tile.get("onLongPressCommand", {}).get("showMenuCommand", {}).get("menu", {}).get("menuRenderer", {}).get("items", []):
            nav = item.get("menuNavigationItemRenderer", {}).get("navigationEndpoint", {}).get("watchEndpoint", {})
            if nav.get("videoId"):
                video_id = _text(nav.get("videoId"), 40)
                break

    if not video_id or not title:
        return None

    parts: List[str] = []
    duration = 0
    lines = meta.get("lines") or []
    for line in lines:
        for it in (line or {}).get("lineRenderer", {}).get("items", []):
            txt = _text((it or {}).get("lineItemRenderer", {}).get("text", ""))
            if txt:
                parts.append(txt)
            if ":" in txt:
                d = _duration(txt)
                if d > 0:
                    duration = d

    header = tile.get("header", {}).get("tileHeaderRenderer", {})
    if duration == 0:
        for overlay in header.get("thumbnailOverlays", []):
            status = (overlay or {}).get("thumbnailOverlayTimeStatusRenderer", {})
            if status:
                duration = _duration(_text(status.get("text")))

    thumb = ""
    thumbs = header.get("thumbnail", {}).get("thumbnails", [])
    if thumbs:
        thumb = thumbs[-1].get("url", "")

    artist = parts[0] if parts else "YouTube Music"
    return {
        "kind": "track",
        "video_id": video_id,
        "title": title,
        "artist": artist,
        "duration": duration,
        "thumb": thumb,
        "playlist_id": "",
        "browse_id": "",
        "page_type": "",
        "subtitle": " • ".join(parts),
    }


def _tile_playlist(tile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(tile, dict):
        return None
    nav = tile.get("onSelectCommand", {}) or {}
    browse = nav.get("browseEndpoint", {}) or {}
    browse_id = browse.get("browseId") or ""
    if not browse_id:
        return None
    ctx = (browse.get("browseEndpointContextSupportedConfigs", {})
           .get("browseEndpointContextMusicConfig", {}))
    page_type = ctx.get("pageType", "")
    is_artist = (page_type == "MUSIC_PAGE_TYPE_ARTIST" or browse_id.startswith("UC"))
    is_playlist = (browse_id.startswith("VL") or browse_id.startswith("FE") or page_type == "MUSIC_PAGE_TYPE_PLAYLIST")
    if not (is_artist or is_playlist):
        return None

    meta = tile.get("metadata", {}).get("tileMetadataRenderer", {})
    title = _text(meta.get("title"))
    if not title:
        return None
    parts: List[str] = []
    lines = meta.get("lines") or []
    for line in lines:
        for it in (line or {}).get("lineRenderer", {}).get("items", []):
            txt = _text((it or {}).get("lineItemRenderer", {}).get("text", ""))
            if txt and txt != "•":
                parts.append(txt)
    header = tile.get("header", {}).get("tileHeaderRenderer", {})
    thumb = ""
    thumbs = header.get("thumbnail", {}).get("thumbnails", [])
    if thumbs:
        thumb = thumbs[-1].get("url", "")
    kind = "artist" if is_artist else "playlist"
    sub = " • ".join(parts) or ("Исполнитель" if is_artist else "Плейлист")
    return {
        "kind": kind,
        "title": title,
        "subtitle": sub,
        "browse_id": browse_id,
        "playlist_id": browse_id[2:] if browse_id.startswith("VL") else browse_id,
        "thumb": thumb,
    }


def _tracks(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    seen = set()
    for item in _walk(payload, "musicTwoColumnItemRenderer"):
        track = _two_column(item)
        if track and track.get("video_id") and track["video_id"] not in seen:
            seen.add(track["video_id"])
            tracks.append(track)
    for item in _walk(payload, "musicResponsiveListItemRenderer"):
        track = _responsive(item)
        if track and track["video_id"] not in seen:
            seen.add(track["video_id"])
            tracks.append(track)
    for item in _walk(payload, "tileRenderer"):
        track = _tile_track(item)
        if track and track["video_id"] not in seen:
            seen.add(track["video_id"])
            tracks.append(track)
    return tracks


def _continuation(payload: Dict[str, Any]) -> str:
    for key in (
        "musicPlaylistShelfRenderer",
        "musicShelfRenderer",
        "sectionListRenderer",
        "gridRenderer",
        "playlistVideoListRenderer",
        "shelfRenderer",
        "musicPlaylistShelfContinuation",
        "musicShelfContinuation",
        "sectionListContinuation",
        "gridContinuation",
        "playlistVideoListContinuation",
    ):
        for shelf in _walk(payload, key):
            if not isinstance(shelf, dict):
                continue
            for item in shelf.get("continuations") or []:
                token = ((item or {}).get("nextContinuationData") or {}).get("continuation")
                if token:
                    return _text(token, 4000)
    return ""


def _clean_sub_part(text: str) -> str:
    return text.strip().strip("•").strip()


def _parse_tv_collection_header(payload: Dict[str, Any], browse_id: str) -> Dict[str, str]:
    """Parse TVHTML5 entityMetadataRenderer / overlayPanelHeaderRenderer collection headers."""
    em = next((n for n in _walk(payload, "entityMetadataRenderer") if isinstance(n, dict)), {})
    oph = next((n for n in _walk(em, "overlayPanelHeaderRenderer") if isinstance(n, dict)), None)
    if oph is None:
        oph = next((n for n in _walk(payload, "overlayPanelHeaderRenderer") if isinstance(n, dict)), {})

    title = _text(oph.get("title"), 300).strip() or _text(em.get("title"), 300).strip()
    sub_parts: List[str] = []

    def _add_part(val: str) -> None:
        cleaned = _clean_sub_part(val)
        if cleaned and cleaned not in sub_parts:
            sub_parts.append(cleaned)

    for node in (oph, em):
        raw_sub = _text(node.get("subtitle") or {}, 300)
        if raw_sub:
            for piece in raw_sub.split("•"):
                _add_part(piece)
        for extra in node.get("additionalSubtitles") or []:
            if isinstance(extra, dict):
                _add_part(_text(extra, 300))
        for line in node.get("metadataLines") or []:
            if not isinstance(line, dict):
                continue
            for item in (line.get("lineRenderer") or {}).get("items") or []:
                if not isinstance(item, dict):
                    continue
                _add_part(_text((item.get("lineItemRenderer") or {}).get("text") or {}, 200))

    thumb = _thumbnail(oph) or _thumbnail(em)
    if not thumb:
        for holder in (oph, em):
            thumbs = (holder.get("thumbnail") or {}).get("thumbnails") or []
            if isinstance(thumbs, list):
                for entry in reversed(thumbs):
                    url = _text((entry or {}).get("url"), 1000)
                    if url.startswith("https://"):
                        thumb = url
                        break
            if thumb:
                break

    if browse_id in ("VLLM", "FEmusic_liked_videos"):
        kind = "liked"
        playlist_id = "LM"
    elif browse_id.startswith("UC"):
        kind = "artist"
        playlist_id = ""
    elif browse_id.startswith(("MPREb_", "VLOLAK5uy_")):
        kind = "album"
        playlist_id = browse_id[2:] if browse_id.startswith("VL") else ""
    else:
        kind = "playlist"
        playlist_id = browse_id[2:] if browse_id.startswith("VL") else browse_id

    return {
        "kind": kind,
        "title": title,
        "subtitle": " • ".join(sub_parts),
        "description": "",
        "thumb": thumb,
        "playlist_id": playlist_id,
        "artist": "",
        "artist_id": "",
    }


def _normalize_track(track: Dict[str, Any], playlist_id: str = "") -> Dict[str, Any]:
    item = dict(track)
    vid = _text(item.get("video_id") or item.get("id"), 40)
    item["source"] = "yt"
    if vid:
        item["id"] = vid
        item["video_id"] = vid
    if playlist_id and not item.get("playlist_id"):
        item["playlist_id"] = playlist_id
    return item


def _extract_like_status(payload: Dict[str, Any], video_id: str) -> str:
    for renderer in _walk(payload, "likeButtonRenderer"):
        if not isinstance(renderer, dict):
            continue
        target_vid = _text((renderer.get("target") or {}).get("videoId"), 40)
        if target_vid and target_vid != video_id:
            continue
        status = _text(renderer.get("likeStatus"), 32).upper()
        if status in ("LIKE", "DISLIKE", "INDIFFERENT"):
            return status
    for status_val in _walk(payload, "likeStatus"):
        status = _text(status_val, 32).upper()
        if status in ("LIKE", "DISLIKE", "INDIFFERENT"):
            return status
    return ""


class YTAccount:
    _PERSONAL_MIX_DEFS = (
        ("VLRDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY", "Мой супермикс"),
        ("VLRDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50", "Мой микс 1"),
        ("VLRDTMAK5uy_lz2owBgwWf1mjzyn_NbxzMViQzIg8IAIg", "Мой микс 2"),
        ("VLRDTMAK5uy_k5UUl0lmrrfrjMpsT0CoMpdcBz1ruAO1k", "Мой микс 3"),
        ("VLRDTMAK5uy_nTsa0Irmcu2li2-qHBoZxtrpG9HuC3k_Q", "Мой микс 4"),
        ("VLRDTMAK5uy_lfZhS7zmIcmUhsKtkWylKzc0EN0LW90-s", "Мой микс 5"),
    )
    _PERSONAL_MIX_TTL = 1800

    def __init__(self, store: Optional[ExtMusicStore] = None, panel=None):
        self.store = store or ExtMusicStore()
        self.panel = panel
        self.oauth_handler = GoogleOAuth(self.store)
        self._lock = threading.RLock()
        self._liked_ids: Optional[Set[str]] = None
        self._optimistic_likes: Set[str] = set()
        self._optimistic_unlikes: Set[str] = set()
        self._personal_mix_cache: Dict[str, Any] = {"at": 0.0, "entries": {}}

    def is_configured(self) -> bool:
        return self.store.is_yt_authenticated()

    def account(self) -> Dict[str, Any]:
        if self.store.has_oauth():
            oauth_data = self.store.oauth()
            email = str(oauth_data.get("email") or "Google-аккаунт")
            return {"configured": True, "type": "oauth", "account": email}
        if self.store.has_auth_cookies():
            email = self.store.yt_account_name() or "Google-аккаунт"
            return {"configured": True, "type": "cookies", "account": email}
        return {"configured": False, "type": "", "account": ""}

    def forget(self) -> None:
        self.store.clear_cookies()
        self.store.clear_oauth()
        with self._lock:
            self._liked_ids = None
            self._optimistic_likes.clear()
            self._optimistic_unlikes.clear()
            self._personal_mix_cache = {"at": 0.0, "entries": {}}

    def logout(self) -> Dict[str, Any]:
        email = self.account().get("account") or ""
        self.forget()
        return {"ok": True, "account": email}

    def _get_oauth_token(self) -> str:
        tokens = self.store.oauth()
        access_token = str(tokens.get("access_token") or "")
        expires_at = float(tokens.get("expires_at") or 0)
        if time.time() >= expires_at - 60 and tokens.get("refresh_token"):
            try:
                access_token = self.oauth_handler.refresh_access_token(tokens)
            except GoogleOAuthError:
                pass
        return access_token

    @staticmethod
    def _validate_video_id(video_id: str) -> str:
        vid = _text(video_id, 40).strip()
        if not vid or not _VIDEO_ID_RE.fullmatch(vid):
            raise YTAccountError("Некорректный идентификатор трека")
        return vid

    @staticmethod
    def _normalize_browse_id(browse_id: str) -> str:
        bid = _text(browse_id, 200).strip()
        if not bid or not _BROWSE_COLLECTION_RE.fullmatch(bid):
            raise YTAccountError("Некорректный идентификатор плейлиста или исполнителя")
        if bid.startswith(("PL", "OLAK5uy_", "RD")):
            return "VL" + bid
        return bid

    def _post(self, body: Dict[str, Any], retry: bool = True, endpoint: str = "browse") -> Dict[str, Any]:
        ep = "" if endpoint is None else str(endpoint).strip().lstrip("/")
        if not _ENDPOINT_RE.fullmatch(ep) or "//" in ep or ep.endswith("/"):
            raise YTAccountError("Некорректный endpoint YouTube Music")

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }

        if self.store.has_oauth():
            token = self._get_oauth_token()
            if not token:
                raise YTAccountError("Сессия Google OAuth устарела. Войдите заново.")
            headers["Authorization"] = f"Bearer {token}"
            headers["User-Agent"] = "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/20"
            client_ctx = {
                "clientName": "TVHTML5",
                "clientVersion": "7.20241111.00.00",
                "hl": "ru",
                "gl": "RU",
            }
            api_url = f"{OAUTH_API_BASE}/{ep}?prettyPrint=false"
        elif self.store.has_auth_cookies():
            cookie_header = self.store.get_cookie_header()
            if not cookie_header:
                raise YTAccountError("Cookies YouTube Music отсутствуют или повреждены.")
            headers["User-Agent"] = USER_AGENT
            headers["Cookie"] = cookie_header
            headers["Origin"] = "https://music.youtube.com"
            headers["Referer"] = "https://music.youtube.com/"
            headers["X-Goog-AuthUser"] = "0"
            headers["x-origin"] = "https://music.youtube.com"
            sapisid = self.store.get_sapisid_hash()
            if sapisid:
                headers["Authorization"] = sapisid
            client_ctx = {
                "clientName": "WEB_REMIX",
                "clientVersion": CLIENT_VERSION,
                "hl": "ru",
                "gl": "RU",
            }
            api_url = f"{COOKIES_API_BASE}/{ep}?prettyPrint=false"
        else:
            raise YTAccountError(
                "Аккаунт YouTube Music не подключён. Импортируйте cookies или войдите через Google."
            )

        payload_body = dict(body)
        ctx = dict(payload_body.get("context") or {})
        ctx["client"] = client_ctx
        payload_body["context"] = ctx

        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload_body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        for attempt in range(2):
            try:
                with ipv4_urlopen(request, timeout=25) as response:
                    payload = json.loads(response.read(8 << 20).decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    if self.store.has_oauth() and retry:
                        tokens = self.store.oauth()
                        try:
                            self.oauth_handler.refresh_access_token(tokens)
                            return self._post(body, retry=False, endpoint=ep)
                        except Exception:
                            pass
                    raise YTAccountError(
                        "Авторизация в YouTube Music устарела. Обновите cookies или войдите заново через QR-код."
                    ) from error
                if error.code == 413:
                    raise YTAccountError(
                        "Размер заголовков/cookies слишком велик (HTTP 413). Очистите cookies и сохраните заново."
                    ) from error
                raise YTAccountError(f"YouTube Music ответил HTTP {error.code}") from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                raise YTAccountError(f"Нет связи с YouTube Music: {error}") from error
            except ValueError as error:
                raise YTAccountError("YouTube Music вернул некорректный ответ") from error

        if not isinstance(payload, dict):
            raise YTAccountError("YouTube Music вернул некорректный ответ")
        return payload

    def _browse(self, browse_id: str = "", continuation: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if continuation:
            body["continuation"] = continuation
        else:
            body["browseId"] = browse_id
        return self._post(body, endpoint="browse")

    def library(self) -> Dict[str, Any]:
        payload = self._browse("FEmusic_library_landing")
        liked = None
        playlists: List[Dict[str, Any]] = []
        artists: List[Dict[str, Any]] = []
        seen = set()

        for item in _walk(payload, "musicTwoColumnItemRenderer"):
            entry = _two_column(item)
            if not entry:
                continue
            key = entry.get("browse_id") or entry.get("video_id") or entry.get("title")
            if key in seen:
                continue
            seen.add(key)
            if entry.get("playlist_id") == "LM" or entry.get("browse_id") == "VLLM":
                liked = entry
            elif entry.get("kind") == "artist":
                artists.append(entry)
            else:
                playlists.append(entry)

        for item in _walk(payload, "tileRenderer"):
            entry = _tile_playlist(item)
            if not entry:
                continue
            key = entry.get("browse_id") or entry.get("title")
            if key in seen:
                continue
            seen.add(key)
            if entry.get("browse_id") in ("VLLM", "FEmusic_liked_videos") or entry.get("title") == "Понравившаяся музыка":
                liked = entry
            elif entry.get("kind") == "artist":
                artists.append(entry)
            else:
                playlists.append(entry)

        # Also load playlists from FEmusic_liked_playlists for modern YouTube Music
        try:
            p_payload = self._browse("FEmusic_liked_playlists")
            for item in _walk(p_payload, "musicTwoRowItemRenderer"):
                entry = _two_row(item)
                if not entry:
                    continue
                key = entry.get("browse_id") or entry.get("title")
                if key in seen:
                    continue
                seen.add(key)
                if entry.get("browse_id") == "VLLM" or entry.get("title") == "Понравившаяся музыка":
                    if not liked:
                        liked = entry
                elif entry.get("kind") == "artist":
                    artists.append(entry)
                else:
                    playlists.append(entry)

            for item in _walk(p_payload, "tileRenderer"):
                entry = _tile_playlist(item)
                if not entry:
                    continue
                key = entry.get("browse_id") or entry.get("title")
                if key in seen:
                    continue
                seen.add(key)
                if entry.get("browse_id") in ("VLLM", "FEmusic_liked_videos") or entry.get("title") == "Понравившаяся музыка":
                    if not liked:
                        liked = entry
                elif entry.get("kind") == "artist":
                    artists.append(entry)
                else:
                    playlists.append(entry)
        except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
            pass

        # If liked was not in twoColumn/twoRow, default to VLLM
        if not liked:
            liked = {
                "title": "Понравившаяся музыка",
                "subtitle": "Автоматический плейлист",
                "kind": "liked",
                "browse_id": "VLLM",
                "playlist_id": "LM",
                "thumb": LIKED_THUMB_URL,
            }
        else:
            liked["thumb"] = liked.get("thumb") or LIKED_THUMB_URL

        acc_info = self.account()
        return {
            "configured": True,
            "account": acc_info.get("account") or "",
            "type": acc_info.get("type") or "",
            "liked": liked,
            "playlists": playlists,
            "artists": artists,
        }

    def _paginate_tracks(
        self,
        payload: Dict[str, Any],
        limit: int,
        initial_tracks: Optional[List[Dict[str, Any]]] = None,
        max_pages: int = 15,
    ) -> List[Dict[str, Any]]:
        """Collect tracks across continuation pages with a cycle guard and page cap."""
        tracks = list(initial_tracks) if initial_tracks is not None else _tracks(payload)
        continuation = _continuation(payload)
        pages = 0
        seen_tokens: Set[str] = set()
        while len(tracks) < limit and continuation and pages < max_pages:
            if continuation in seen_tokens:
                break
            seen_tokens.add(continuation)
            pages += 1
            page = self._browse(continuation=continuation)
            tracks.extend(_tracks(page))
            continuation = _continuation(page)
        return tracks[:limit]

    def liked(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(300, int(limit or 100)))
        browse_id = "FEmusic_liked_videos" if self.store.has_oauth() else "VLLM"
        payload = self._browse(browse_id)
        tracks = _tracks(payload)
        if not tracks and browse_id != "VLLM":
            payload = self._browse("VLLM")
            tracks = _tracks(payload)
        if not tracks:
            for param in payload.get("responseContext", {}).get("serviceTrackingParams", []):
                for p in param.get("params", []):
                    if p.get("key") in ("logged_in", "yt_li") and str(p.get("value")) == "0":
                        raise YTAccountError(
                            "Сессия YouTube Music устарела. Обновите cookies (экспортируйте с music.youtube.com) или войдите через Google."
                        )
        tracks = self._paginate_tracks(payload, limit, initial_tracks=tracks)
        result = [_normalize_track(track, playlist_id="LM") for track in tracks]
        ids = {str(t.get("video_id") or t.get("id") or "") for t in result}
        ids.discard("")
        with self._lock:
            merged = set(ids)
            merged.update(self._optimistic_likes)
            merged.difference_update(self._optimistic_unlikes)
            if self._liked_ids is None:
                self._liked_ids = merged
            else:
                self._liked_ids.update(merged)
                self._liked_ids.difference_update(self._optimistic_unlikes)
        return result

    def _record_like_state(self, vid: str, liked: bool) -> None:
        """Update single-track optimistic overlay without fabricating a full library cache."""
        with self._lock:
            if liked:
                self._optimistic_likes.add(vid)
                self._optimistic_unlikes.discard(vid)
                if self._liked_ids is not None:
                    self._liked_ids.add(vid)
            else:
                self._optimistic_likes.discard(vid)
                self._optimistic_unlikes.add(vid)
                if self._liked_ids is not None:
                    self._liked_ids.discard(vid)

    def liked_ids(self, refresh: bool = False) -> Set[str]:
        """Return cached liked video IDs, fetching from the account library when needed."""
        with self._lock:
            if not refresh and self._liked_ids is not None:
                return set(self._liked_ids)
        if not self.is_configured():
            with self._lock:
                self._liked_ids = set()
                self._optimistic_likes.clear()
                self._optimistic_unlikes.clear()
            return set()
        snap_likes: Set[str] = set()
        snap_unlikes: Set[str] = set()
        if refresh:
            with self._lock:
                snap_likes = set(self._optimistic_likes)
                snap_unlikes = set(self._optimistic_unlikes)
                self._optimistic_likes.clear()
                self._optimistic_unlikes.clear()
        try:
            tracks = self.liked(limit=300)
        except Exception:
            if refresh:
                with self._lock:
                    self._optimistic_likes.update(snap_likes)
                    self._optimistic_unlikes.update(snap_unlikes)
            raise
        ids = {str(t.get("video_id") or t.get("id") or "") for t in tracks}
        ids.discard("")
        with self._lock:
            ids.update(self._optimistic_likes)
            ids.difference_update(self._optimistic_unlikes)
            self._liked_ids = ids
            return set(self._liked_ids)

    def is_liked(self, video_id: str) -> Optional[bool]:
        """Check like status from optimistic/full cache first, then query InnerTube `next`."""
        vid = _text(video_id, 40).strip()
        if not vid or not _VIDEO_ID_RE.fullmatch(vid):
            return None
        with self._lock:
            if vid in self._optimistic_likes:
                return True
            if vid in self._optimistic_unlikes:
                return False
            if self._liked_ids is not None and vid in self._liked_ids:
                return True
        if not self.is_configured():
            return None
        try:
            payload = self._post({"videoId": vid}, endpoint="next")
        except YTAccountError:
            with self._lock:
                return False if self._liked_ids is not None else None
        status = _extract_like_status(payload, vid)
        if status == "LIKE":
            self._record_like_state(vid, True)
            return True
        if status in ("DISLIKE", "INDIFFERENT"):
            self._record_like_state(vid, False)
            return False
        with self._lock:
            return False if self._liked_ids is not None else None

    def like(self, video_id: str, on: bool = True) -> Dict[str, Any]:
        """Like or remove like on a YouTube video via the active account transport."""
        vid = self._validate_video_id(video_id)
        liked = bool(on)
        endpoint = "like/like" if liked else "like/removelike"
        self._post({"target": {"videoId": vid}}, endpoint=endpoint)
        self._record_like_state(vid, liked)
        return {"ok": True, "liked": liked, "video_id": vid}

    def dislike(self, video_id: str) -> Dict[str, Any]:
        """Dislike a YouTube video and drop it from the cached liked IDs."""
        vid = self._validate_video_id(video_id)
        self._post({"target": {"videoId": vid}}, endpoint="like/dislike")
        self._record_like_state(vid, False)
        return {"ok": True, "disliked": True, "video_id": vid}

    def _browse_collection_private(self, browse_id: str, params: str = "", limit: int = 100) -> Dict[str, Any]:
        if params:
            payload = self._post({"browseId": browse_id, "params": _text(params, 400)}, endpoint="browse")
        else:
            payload = self._browse(browse_id)
        meta = ytmusic._parse_collection_header(payload, browse_id)
        if not meta.get("title") or not meta.get("subtitle"):
            tv_meta = _parse_tv_collection_header(payload, browse_id)
            for key in ("kind", "title", "subtitle", "description", "thumb", "playlist_id"):
                if not meta.get(key) and tv_meta.get(key):
                    meta[key] = tv_meta[key]
        if browse_id in ("VLLM", "FEmusic_liked_videos"):
            meta["kind"] = "liked"
        tracks = self._paginate_tracks(payload, limit)
        playlist_id = meta.get("playlist_id") or (
            "LM" if browse_id in ("VLLM", "FEmusic_liked_videos")
            else (browse_id[2:] if browse_id.startswith("VL") else browse_id)
        )
        normalized = [_normalize_track(track, playlist_id=playlist_id) for track in tracks]
        for track in normalized:
            track["playlist_id"] = playlist_id
        thumb = meta.get("thumb") or next((str(t.get("thumb") or "") for t in normalized if t.get("thumb")), "")
        if not thumb and browse_id in ("VLLM", "FEmusic_liked_videos"):
            thumb = LIKED_THUMB_URL
        return {
            "kind": meta.get("kind") or "playlist",
            "title": meta.get("title") or "Без названия",
            "subtitle": meta.get("subtitle") or "",
            "description": meta.get("description") or "",
            "thumb": thumb,
            "browse_id": browse_id,
            "playlist_id": playlist_id,
            "artist": meta.get("artist") or "",
            "artist_id": meta.get("artist_id") or "",
            "tracks": normalized,
            "albums": [],
            "similar": [],
        }

    def _liked_collection(self, browse_id: str, limit: int = 100) -> Dict[str, Any]:
        """Return full liked-songs collection using paginated liked() instead of TVHTML5 preview."""
        tracks = self.liked(limit=limit)
        return {
            "kind": "liked",
            "title": "Понравившаяся музыка",
            "subtitle": f"{len(tracks)} треков" if tracks else "Автоматический плейлист",
            "description": "",
            "thumb": LIKED_THUMB_URL,
            "browse_id": browse_id,
            "playlist_id": "LM",
            "artist": "",
            "artist_id": "",
            "tracks": tracks,
            "albums": [],
            "similar": [],
        }

    def _public_collection(self, browse_id: str, params: str = "", limit: int = 100) -> Dict[str, Any]:
        col = ytmusic.browse_collection(browse_id, params=params, limit=limit)
        pl_id = str(col.get("playlist_id") or "")
        col["tracks"] = [_normalize_track(t, playlist_id=pl_id) for t in (col.get("tracks") or [])[:limit]]
        return col

    def collection(self, browse_id: str, params: str = "", limit: int = 100) -> Dict[str, Any]:
        """Fetch full metadata and tracks for an album, artist, or playlist with public fallback."""
        norm_id = self._normalize_browse_id(browse_id)
        limit = max(1, min(300, int(limit or 100)))
        is_private_only = norm_id in ("VLLM", "FEmusic_liked_videos")
        # Keep VLRD* (personal RDTMAK* mixes) private-first when configured: public client drops/mismatches them.
        is_public_type = norm_id.startswith(("MPREb_", "UC", "VLOLAK5uy_"))

        if not self.is_configured():
            if is_private_only:
                raise YTAccountError(
                    "Аккаунт YouTube Music не подключён. Импортируйте cookies или войдите через Google."
                )
            try:
                return self._public_collection(norm_id, params=params, limit=limit)
            except YTMusicError as error:
                raise YTAccountError(str(error)) from error

        if is_private_only:
            return self._liked_collection(norm_id, limit=limit)

        if is_public_type:
            try:
                return self._public_collection(norm_id, params=params, limit=limit)
            except YTMusicError:
                return self._browse_collection_private(norm_id, params=params, limit=limit)

        private_err: Optional[YTAccountError] = None
        private_col: Optional[Dict[str, Any]] = None
        try:
            private_col = self._browse_collection_private(norm_id, params=params, limit=limit)
            if private_col["tracks"]:
                return private_col
        except YTAccountError as error:
            private_err = error

        try:
            return self._public_collection(norm_id, params=params, limit=limit)
        except YTMusicError:
            pass

        if private_err is not None:
            raise private_err
        return private_col if private_col is not None else self._browse_collection_private(norm_id, params=params, limit=limit)

    def playlist(self, browse_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.collection(browse_id, limit=limit)["tracks"]

    def _resolve_personal_mix_entry(self, browse_id: str, fallback_title: str) -> Optional[Dict[str, Any]]:
        try:
            col = self._browse_collection_private(browse_id, limit=10)
        except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
            return None
        tracks = [t for t in (col.get("tracks") or []) if isinstance(t, dict)]
        if not tracks:
            return None
        artists = list(dict.fromkeys(
            str(t.get("artist") or "").strip()
            for t in tracks
            if str(t.get("artist") or "").strip() and str(t.get("artist") or "").strip() != "YouTube Music"
        ))
        subtitle = ", ".join(artists[:4]) or str(col.get("subtitle") or "").strip() or "Персональный микс"
        thumb = str(col.get("thumb") or "").strip() or next((str(t.get("thumb") or "") for t in tracks if t.get("thumb")), "")
        title = str(col.get("title") or "").strip()
        if not title or title == "Плейлист":
            title = fallback_title
        return {
            "kind": "playlist",
            "title": title,
            "subtitle": subtitle,
            "browse_id": browse_id,
            "playlist_id": browse_id[2:] if browse_id.startswith("VL") else browse_id,
            "thumb": thumb,
        }

    def _collect_personal_mixes(
        self, topics_payload: Optional[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        by_bid: Dict[str, Dict[str, Any]] = {}
        topics_personal: List[Dict[str, Any]] = []
        topics_other: List[Dict[str, Any]] = []

        if topics_payload:
            seen_t: Set[str] = set()
            for entry in (
                [_tile_playlist(item) for item in _walk(topics_payload, "tileRenderer")]
                + [_two_row(item) for item in _walk(topics_payload, "musicTwoRowItemRenderer")]
            ):
                if not isinstance(entry, dict) or entry.get("kind") != "playlist":
                    continue
                bid = str(entry.get("browse_id") or "")
                if not bid or bid in seen_t:
                    continue
                seen_t.add(bid)
                if bid.startswith("VLRDTMAK"):
                    by_bid[bid] = entry
                    topics_personal.append(entry)
                else:
                    topics_other.append(entry)

        if by_bid or (self.is_configured() and self.store.has_oauth()):
            now = time.time()
            with self._lock:
                cache_fresh = now - float(self._personal_mix_cache.get("at") or 0.0) < self._PERSONAL_MIX_TTL
                cached_entries = dict(self._personal_mix_cache.get("entries") or {}) if cache_fresh else {}

            missing: List[Tuple[str, str]] = []
            for core_bid, core_title in self._PERSONAL_MIX_DEFS:
                if core_bid in by_bid:
                    cached_entries[core_bid] = by_bid[core_bid]
                elif core_bid in cached_entries:
                    by_bid[core_bid] = cached_entries[core_bid]
                else:
                    missing.append((core_bid, core_title))

            if missing:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
                    futures = [(core_bid, pool.submit(self._resolve_personal_mix_entry, core_bid, core_title))
                               for core_bid, core_title in missing]
                    for core_bid, fut in futures:
                        try:
                            resolved = fut.result(timeout=8)
                        except Exception:
                            resolved = None
                        if isinstance(resolved, dict):
                            by_bid[core_bid] = resolved
                            cached_entries[core_bid] = resolved

            with self._lock:
                self._personal_mix_cache = {"at": now, "entries": cached_entries}

        personal: List[Dict[str, Any]] = []
        used: Set[str] = set()
        super_bid = self._PERSONAL_MIX_DEFS[0][0]
        if super_bid in by_bid:
            personal.append(by_bid[super_bid])
            used.add(super_bid)

        if self.is_configured():
            personal.append({
                "kind": "liked",
                "title": "Понравившаяся музыка",
                "subtitle": "Создан автоматически",
                "browse_id": "VLLM",
                "playlist_id": "LM",
                "thumb": LIKED_THUMB_URL,
            })
            used.add("VLLM")

        for core_bid, _ in self._PERSONAL_MIX_DEFS[1:]:
            if core_bid in by_bid and core_bid not in used:
                personal.append(by_bid[core_bid])
                used.add(core_bid)

        for entry in topics_personal:
            bid = str(entry.get("browse_id") or "")
            if bid and bid not in used:
                personal.append(entry)
                used.add(bid)

        return personal, topics_other

    def _collect_mixes(
        self,
        payload: Optional[Dict[str, Any]],
        feed: List[Dict[str, Any]],
        topics_payload: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        mixes: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        def _add_mix(entry: Optional[Dict[str, Any]]) -> None:
            if not isinstance(entry, dict):
                return
            bid = str(entry.get("browse_id") or "")
            title = str(entry.get("title") or "").strip()
            key = bid or title
            if not key or key in seen:
                return
            seen.add(key)
            mixes.append(entry)

        personal_mixes, topics_other = self._collect_personal_mixes(topics_payload)
        for entry in personal_mixes:
            _add_mix(entry)

        if payload:
            for item in _walk(payload, "musicTwoRowItemRenderer"):
                entry = _two_row(item)
                if entry and entry.get("kind") == "playlist":
                    _add_mix(entry)
            for item in _walk(payload, "tileRenderer"):
                entry = _tile_playlist(item)
                if entry and entry.get("kind") == "playlist":
                    _add_mix(entry)

        has_real_personal = any(str(m.get("browse_id") or "").startswith("VLRDTMAK") for m in mixes)
        if not has_real_personal:
            for entry in topics_other:
                _add_mix(entry)

        if self.is_configured():
            try:
                lib = self.library()
                for pl in (lib.get("playlists") or [])[:4]:
                    _add_mix(pl)
                if not has_real_personal:
                    for art in (lib.get("artists") or [])[:3]:
                        art_id = str(art.get("browse_id") or "")
                        art_name = str(art.get("title") or "").strip()
                        if not art_id or not art_name:
                            continue
                        _add_mix({
                            "kind": "artist",
                            "title": f"Микс: {art_name}",
                            "subtitle": "Персональный микс",
                            "browse_id": art_id,
                            "playlist_id": str(art.get("playlist_id") or ""),
                            "artist_id": art_id,
                            "thumb": str(art.get("thumb") or ""),
                        })
            except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
                pass

        if not mixes and feed:
            seed = next((str(t.get("video_id") or t.get("id") or "") for t in feed if t.get("video_id") or t.get("id")), "")
            if seed:
                _add_mix({
                    "kind": "playlist",
                    "title": "Мой микс",
                    "subtitle": "Радио по вашей ленте",
                    "browse_id": f"VLRDAMVM{seed}",
                    "playlist_id": f"RDAMVM{seed}",
                    "thumb": str(feed[0].get("thumb") or ""),
                })
        return mixes[:16]

    def home(self, limit: int = 30) -> Dict[str, Any]:
        """Combine public quick picks/moods with personalized account mixes and home feed."""
        limit = max(1, min(100, int(limit or 30)))
        quick_picks: List[Dict[str, Any]] = []
        moods: List[Dict[str, str]] = []
        seen_moods: Set[str] = set()

        try:
            pub = ytmusic.home(limit=limit)
            if isinstance(pub, dict):
                quick_picks = [_normalize_track(t) for t in (pub.get("tracks") or []) if isinstance(t, dict)]
                for m in pub.get("moods") or []:
                    if isinstance(m, dict) and m.get("title") and m["title"] not in seen_moods:
                        seen_moods.add(m["title"])
                        moods.append(m)
        except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
            pass

        payload: Optional[Dict[str, Any]] = None
        topics_payload: Optional[Dict[str, Any]] = None
        feed: List[Dict[str, Any]] = []
        if self.is_configured():
            has_oauth = self.store.has_oauth()
            home_browse_id = "FEwhat_to_watch" if has_oauth else "FEmusic_home"
            try:
                payload = self._browse(home_browse_id)
            except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
                payload = None
            if has_oauth:
                try:
                    topics_payload = self._browse("FEtopics_music")
                except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
                    topics_payload = None

            if payload:
                feed = [_normalize_track(t) for t in _tracks(payload)]
                for chip in _walk(payload, "chipCloudChipRenderer"):
                    if not isinstance(chip, dict):
                        continue
                    title = _text(chip.get("text") or {})
                    endpoint = ((chip.get("navigationEndpoint") or {}).get("browseSectionListReloadEndpoint") or {})
                    token = ((endpoint.get("continuation") or {}).get("reloadContinuationData") or {}).get("continuation")
                    if title and token and title not in seen_moods:
                        seen_moods.add(title)
                        moods.append({"title": title, "token": _text(token, 4000)})

        mixes = self._collect_mixes(payload, feed, topics_payload=topics_payload)

        tracks = list(feed) if feed else list(quick_picks)
        if not tracks and self.is_configured():
            try:
                tracks = self.liked(limit=limit)
            except (YTAccountError, YTMusicError, KeyError, TypeError, ValueError):
                tracks = []

        return {
            "tracks": tracks[:limit],
            "moods": moods,
            "quick_picks": quick_picks[:limit],
            "mixes": mixes,
            "feed": feed[:limit],
        }

    def mood(self, token: str, limit: int = 100) -> Dict[str, Any]:
        token = _text(token, 4000)
        if not token:
            raise YTAccountError("Некорректная подборка")
        payload = self._browse(continuation=token)
        tracks = []
        for model in _walk(payload, "musicListItemCarouselModel"):
            if not isinstance(model, dict):
                continue
            for item in model.get("items") or []:
                if not isinstance(item, dict):
                    continue
                command = ((item.get("onTap") or {}).get("innertubeCommand") or {})
                endpoint = command.get("watchEndpoint") or {}
                video_id = _text(endpoint.get("videoId"), 40)
                title = _text(item.get("title") or {})
                if not video_id or not title:
                    continue
                subtitle = _text(item.get("subtitle") or {})
                parts = [part.strip() for part in subtitle.split("•") if part.strip()]
                thumbs = ((item.get("thumbnail") or {}).get("image") or {}).get("sources") or []
                thumb = _text((thumbs[-1] or {}).get("url"), 1000) if isinstance(thumbs, list) and thumbs else ""
                tracks.append({
                    "source": "yt",
                    "id": video_id,
                    "kind": "track",
                    "video_id": video_id,
                    "title": title,
                    "artist": parts[0] if parts else "YouTube Music",
                    "duration": 0,
                    "thumb": thumb,
                    "playlist_id": _text(endpoint.get("playlistId"), 200),
                    "browse_id": "",
                    "page_type": "",
                    "subtitle": subtitle,
                })
        return {"tracks": tracks[:limit]}

    def create_playlist(
        self,
        title: str,
        video_ids: Optional[Iterable[str]] = None,
        description: str = "",
        privacy: str = "PRIVATE",
    ) -> Dict[str, Any]:
        """Create a YouTube Music playlist and optionally populate it with tracks."""
        clean_title = _text(title, 150).strip()
        if not clean_title:
            raise YTAccountError("Введите название плейлиста")
        priv = str(privacy or "PRIVATE").strip().upper()
        if priv not in ("PRIVATE", "PUBLIC", "UNLISTED"):
            priv = "PRIVATE"

        vids: List[str] = []
        seen: Set[str] = set()
        for raw in (video_ids or []):
            s = _text(raw, 60).strip()
            if s.startswith("ext:yt:"):
                s = s[7:].strip()
            if not s:
                continue
            vid = self._validate_video_id(s)
            if vid not in seen:
                seen.add(vid)
                vids.append(vid)

        body: Dict[str, Any] = {"title": clean_title, "privacyStatus": priv}
        clean_desc = _text(description, 500).strip()
        if clean_desc:
            body["description"] = clean_desc
        if vids:
            body["videoIds"] = vids

        payload = self._post(body, endpoint="playlist/create")
        pid = _text(payload.get("playlistId") or "", 120).strip()
        if not pid:
            for cand in _walk(payload, "playlistId"):
                val = _text(cand, 120).strip()
                if val:
                    pid = val
                    break
        if not pid:
            raise YTAccountError("YouTube Music не вернул ID созданного плейлиста")
        raw_pid = pid[2:] if pid.startswith("VL") else pid
        return {
            "ok": True,
            "playlist_id": raw_pid,
            "browse_id": f"VL{raw_pid}",
            "title": clean_title,
            "added": len(vids),
        }

    def add_to_playlist(self, playlist_id: str, video_ids: Any) -> Dict[str, Any]:
        """Add one or more tracks to a user's YouTube Music playlist."""
        raw_pid = _text(playlist_id, 120).strip()
        if raw_pid.startswith("ext:yt:"):
            raw_pid = raw_pid[7:].strip()
        if raw_pid.startswith("VL"):
            raw_pid = raw_pid[2:].strip()
        if not raw_pid or (not _PLAYLIST_ID_RE.fullmatch(raw_pid) and raw_pid != "FEmusic_liked_videos"):
            raise YTAccountError("Некорректный ID плейлиста YouTube Music")

        raw_list = [video_ids] if isinstance(video_ids, str) else list(video_ids or [])
        vids: List[str] = []
        seen: Set[str] = set()
        for raw in raw_list:
            s = _text(raw, 60).strip()
            if s.startswith("ext:yt:"):
                s = s[7:].strip()
            if not s:
                continue
            vid = self._validate_video_id(s)
            if vid not in seen:
                seen.add(vid)
                vids.append(vid)
        if not vids:
            raise YTAccountError("Не указаны треки для добавления в плейлист")

        if raw_pid in ("LM", "FEmusic_liked_videos"):
            for vid in vids:
                self.like(vid, True)
            return {
                "ok": True,
                "playlist_id": "LM",
                "browse_id": "VLLM",
                "added": len(vids),
                "video_ids": vids,
            }

        actions = [{"addedVideoId": vid, "action": "ACTION_ADD_VIDEO"} for vid in vids]
        payload = self._post({"playlistId": raw_pid, "actions": actions}, endpoint="browse/edit_playlist")
        if str(payload.get("status") or "").upper().startswith("STATUS_FAILED"):
            raise YTAccountError("YouTube Music отклонил добавление в плейлист")
        return {
            "ok": True,
            "playlist_id": raw_pid,
            "browse_id": f"VL{raw_pid}",
            "added": len(vids),
            "video_ids": vids,
        }
