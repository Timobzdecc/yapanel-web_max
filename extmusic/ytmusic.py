"""YouTube Music public catalog, search filters, collections, charts, radio, and lyrics.

Uses the public WEB_REMIX InnerTube endpoints on music.youtube.com without
requiring an account or cookies. Streams are resolved separately via fast_yt /
yt-dlp. Also provides track title normalization for lyrics lookup and a strict
HTTPS allowlist for proxying cover images.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

import extmusic

API = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
API_BASE = "https://music.youtube.com/youtubei/v1"
CLIENT_VERSION = "1.20260101.01.00"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Search filter tokens (WEB_REMIX protobuf params)
SONGS_PARAMS = "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"
SEARCH_PARAMS: Dict[str, str] = {
    "songs": SONGS_PARAMS,
    "videos": "EgWKAQIQAWoKEAkQBRAKEAMQBA%3D%3D",
    "albums": "EgWKAQIYAWoKEAkQBRAKEAMQBA%3D%3D",
    "artists": "EgWKAQIgAWoKEAkQBRAKEAMQBA%3D%3D",
    "playlists": "EgWKAQIoAWoKEAkQBRAKEAMQBA%3D%3D",
}
_SEARCH_KIND_ALIASES = {
    "song": "songs",
    "tracks": "songs",
    "track": "songs",
    "video": "videos",
    "album": "albums",
    "artist": "artists",
    "playlist": "playlists",
}

DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_VIEW_COUNT_RE = re.compile(r"(просмотр|прослушиван|\bviews\b|\bplays\b)", re.I)
_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_/]+$")
_BROWSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,200}$")
_SKIP_EXPLORE_MOOD_IDS = frozenset({
    "FEmusic_new_releases",
    "FEmusic_charts",
    "FEmusic_moods_and_genres",
    "FEmusic_podcasts",
})
_ALLOWED_THUMB_DOMAINS = (
    "ytimg.com",
    "googleusercontent.com",
    "ggpht.com",
    "youtube.com",
    "gstatic.com",
)
_ASCII_HOST_RE = re.compile(r"^[a-z0-9.-]+$")
_ALBUM_SUBTITLE_RE = re.compile(r"\b(?:альбом|album|сингл|single|ep)\b", re.I)
_ALBUM_SHELF_RE = re.compile(
    r"\b(?:альбом(?:ы|ов)?|albums?|сингл(?:ы|ов)?|singles?|eps?|релиз(?:ы|ов)?|releases?)\b",
    re.I,
)
_SIMILAR_SHELF_RE = re.compile(r"(?:\bпохож\w*|\bsimilar\b|\bfans might\b|\bсвязан\w*)", re.I)
_NOISE_TAG_RE = re.compile(
    r"[\(\[]\s*(?:"
    r"official(?:\s+(?:music\s+)?(?:video|audio|lyric(?:\s+video)?|visualizer|clip))?"
    r"|music\s+video|lyric(?:\s+video)?|lyrics?|audio|video|visualizer"
    r"|hd|hq|4k|8k|60\s*fps|explicit|clean|bonus\s+track"
    r"|live(?:\s+[^)\]]*)?|remaster(?:ed)?(?:\s+[^)\]]*)?"
    r"|клип|премьера(?:\s+[^)\]]*)?|текст(?:\s+песни)?"
    r"|(?:feat\.?|ft\.?|featuring)\s+[^)\]]*"
    r")\s*[\)\]]",
    re.I,
)
_TRAILING_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)
_DASH_SPLIT_RE = re.compile(r"^\s*(.+?)\s+(?:—|–|-)\s+(.+?)\s*$")
MAX_PAGES = 3


class YTMusicError(ValueError):
    """A YouTube Music error that can be shown to the user."""


def _text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _node_text(value: Any, limit: int = 300) -> str:
    if isinstance(value, dict):
        if "simpleText" in value:
            return _text(value.get("simpleText"), limit)
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(str((run or {}).get("text") or "") for run in runs if isinstance(run, dict)).strip()[:limit]
        if "content" in value:
            return _text(value.get("content"), limit)
        return ""
    return _text(value, limit)


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_duration(value: str) -> int:
    """``3:08`` / ``1:02:03`` -> seconds."""
    parts = _text(value, 20).split(":")
    if not parts or not all(part.isdigit() for part in parts) or len(parts) > 3:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _runs(column: Dict[str, Any]) -> List[Dict[str, Any]]:
    renderer = column.get("musicResponsiveListItemFlexColumnRenderer") or {}
    text = renderer.get("text") or {}
    runs = text.get("runs")
    return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []


def _column_text(column: Dict[str, Any]) -> str:
    return "".join(_text(run.get("text")) for run in _runs(column)).strip()


def _walk(node: Any, key: str) -> Iterable[Any]:
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from _walk(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, key)


def _context(gl: str = "RU", hl: str = "ru") -> Dict[str, Any]:
    return {
        "client": {
            "clientName": "WEB_REMIX",
            "clientVersion": CLIENT_VERSION,
            "hl": hl,
            "gl": gl,
        }
    }


def _thumbnail(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    candidates: List[Any] = []
    for key in ("thumbnail", "thumbnailRenderer"):
        holder = item.get(key)
        if not isinstance(holder, dict):
            continue
        renderer = holder.get("musicThumbnailRenderer") or {}
        candidates.append((renderer.get("thumbnail") or {}).get("thumbnails"))
        candidates.append(holder.get("thumbnails"))
        candidates.append((holder.get("image") or {}).get("sources"))
    for thumbs in candidates:
        if not isinstance(thumbs, list):
            continue
        for thumb in reversed(thumbs):
            url = _text((thumb or {}).get("url"), 1000)
            if url.startswith("https://"):
                return url
    return ""


def _browse_info(endpoint: Any) -> Tuple[str, str, str, str]:
    if not isinstance(endpoint, dict):
        return "", "", "", ""
    browse = endpoint.get("browseEndpoint") if "browseEndpoint" in endpoint else endpoint
    if not isinstance(browse, dict):
        return "", "", "", ""
    config = (
        (browse.get("browseEndpointContextSupportedConfigs") or {})
        .get("browseEndpointContextMusicConfig")
        or {}
    )
    return (
        _text(browse.get("browseId"), 200),
        _text(browse.get("params"), 400),
        _text(config.get("pageType"), 80),
        _text(config.get("playlistIdForFallback"), 200),
    )


def _extract_run_links(runs: Iterable[Dict[str, Any]]) -> Tuple[str, str, str, str]:
    """Return ``(artist_id, artist_name, album_id, album_name)`` from text runs."""
    artist_id = ""
    artist_name = ""
    album_id = ""
    album_name = ""
    for run in runs:
        if not isinstance(run, dict):
            continue
        bid, _, page_type, _ = _browse_info(run.get("navigationEndpoint"))
        if not bid:
            continue
        text = _text(run.get("text"))
        if page_type == "MUSIC_PAGE_TYPE_ARTIST" or (not page_type and bid.startswith("UC")):
            if not artist_id:
                artist_id = bid
                artist_name = text
        elif page_type == "MUSIC_PAGE_TYPE_ALBUM" or (not page_type and bid.startswith(("MPREb_", "OLAK5uy_"))):
            if not album_id:
                album_id = bid
                album_name = text
    return artist_id, artist_name, album_id, album_name


def _video_id(item: Dict[str, Any]) -> str:
    direct = (item.get("playlistItemData") or {}).get("videoId")
    if direct:
        return _text(direct, 40)
    for endpoint in _walk(item, "watchEndpoint"):
        if isinstance(endpoint, dict) and endpoint.get("videoId"):
            return _text(endpoint.get("videoId"), 40)
    return ""


def _fixed_duration(item: Dict[str, Any]) -> int:
    for col in item.get("fixedColumns") or []:
        if not isinstance(col, dict):
            continue
        renderer = col.get("musicResponsiveListItemFixedColumnRenderer") or {}
        dur = parse_duration(_node_text(renderer.get("text"), 20))
        if dur > 0:
            return dur
    return 0


def _overlay_playlist_id(item: Dict[str, Any]) -> str:
    for wp in _walk(item, "watchPlaylistEndpoint"):
        if isinstance(wp, dict) and wp.get("playlistId"):
            return _text(wp.get("playlistId"), 200)
    return ""


def _split_runs_parts(runs: Iterable[Dict[str, Any]]) -> List[str]:
    parts: List[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        for chunk in _text(run.get("text")).split("•"):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
    return parts


def track_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    video_id = _video_id(item)
    if not video_id:
        return None
    columns = item.get("flexColumns")
    columns = columns if isinstance(columns, list) else []
    title = _column_text(columns[0]) if columns else ""
    col1_runs = _runs(columns[1]) if len(columns) > 1 else []
    parts = _split_runs_parts(col1_runs)
    all_runs = list(col1_runs)
    if len(columns) > 2:
        all_runs.extend(_runs(columns[2]))

    artist_id, artist_from_run, album_id, album_from_run = _extract_run_links(all_runs)
    artist = artist_from_run or (parts[0] if parts else "YouTube Music")
    album = album_from_run
    if not album and len(parts) >= 3 and not _VIEW_COUNT_RE.search(parts[1]) and not DURATION_RE.match(parts[1]):
        album = parts[1]
    if not album and len(columns) > 2:
        col2_text = _column_text(columns[2])
        if col2_text and not _VIEW_COUNT_RE.search(col2_text) and not DURATION_RE.match(col2_text):
            album = col2_text

    duration = parse_duration(parts[-1]) if len(parts) >= 2 else 0
    if duration == 0:
        duration = _fixed_duration(item)

    track: Dict[str, Any] = {
        "source": "yt",
        "id": video_id,
        "title": title or "Без названия",
        "artist": artist,
        "album": album,
        "duration": duration,
        "thumb": _thumbnail(item),
        "url": "",
    }
    if artist_id:
        track["artist_id"] = artist_id
    if album_id:
        track["album_id"] = album_id
    return track


def _card_kind_from_browse(browse_id: str, page_type: str, expected_kind: str = "") -> str:
    if page_type == "MUSIC_PAGE_TYPE_ALBUM" or browse_id.startswith(("MPREb_", "OLAK5uy_", "VLOLAK5uy_")):
        return "album"
    if page_type == "MUSIC_PAGE_TYPE_ARTIST" or browse_id.startswith("UC"):
        return "artist"
    if page_type == "MUSIC_PAGE_TYPE_PLAYLIST" or browse_id.startswith(("VL", "PL", "RDCLAK", "RD")):
        return "playlist"
    return {
        "albums": "album",
        "artists": "artist",
        "playlists": "playlist",
    }.get(expected_kind, "")


def _build_collection_card(
    kind: str,
    browse_id: str,
    playlist_id: str,
    title: str,
    parts: List[str],
    artist: str,
    year: str,
    thumb: str,
    params: str = "",
    artist_id: str = "",
) -> Dict[str, Any]:
    if not artist:
        if kind == "artist":
            artist = title
        elif parts:
            skip = (
                ("альбом", "сингл", "album", "single", "ep")
                if kind == "album"
                else ("альбом", "сингл", "album", "single", "ep", "плейлист", "playlist")
            )
            candidates = [p for p in parts if p != year and p.lower() not in skip]
            artist = candidates[0] if candidates else ""
    card: Dict[str, Any] = {
        "kind": kind,
        "source": "yt",
        "id": browse_id,
        "browse_id": browse_id,
        "playlist_id": playlist_id,
        "title": title or "Без названия",
        "subtitle": " • ".join(parts),
        "artist": artist,
        "album": title if kind == "album" else "",
        "year": year,
        "thumb": thumb,
    }
    if params:
        card["params"] = params
    if artist_id:
        card["artist_id"] = artist_id
    return card


def item_from_responsive(item: Dict[str, Any], expected_kind: str = "songs") -> Optional[Dict[str, Any]]:
    """Parse a ``musicResponsiveListItemRenderer`` as a track or collection card."""
    if not isinstance(item, dict):
        return None
    if expected_kind in ("songs", "videos"):
        track = track_from_item(item)
        if not track:
            return None
        return {**track, "kind": "track", "video_id": track["id"]}

    columns = item.get("flexColumns")
    columns = columns if isinstance(columns, list) else []
    title = _column_text(columns[0]) if columns else ""
    nav = item.get("navigationEndpoint")
    if not nav and columns:
        for run in _runs(columns[0]):
            if run.get("navigationEndpoint"):
                nav = run["navigationEndpoint"]
                break
    browse_id, params, page_type, fallback_pl = _browse_info(nav)
    if not browse_id:
        if not expected_kind:
            track = track_from_item(item)
            return {**track, "kind": "track", "video_id": track["id"]} if track else None
        return None

    card_kind = _card_kind_from_browse(browse_id, page_type, expected_kind)
    if not card_kind:
        return None

    col1_runs = _runs(columns[1]) if len(columns) > 1 else []
    parts = _split_runs_parts(col1_runs)
    artist_id, artist_from_run, _, _ = _extract_run_links(col1_runs)
    year = next((p for p in parts if YEAR_RE.match(p)), "")
    playlist_id = fallback_pl or _overlay_playlist_id(item)
    if not playlist_id and browse_id.startswith("VL"):
        playlist_id = browse_id[2:]
    return _build_collection_card(
        kind=card_kind,
        browse_id=browse_id,
        playlist_id=playlist_id,
        title=title,
        parts=parts,
        artist=artist_from_run,
        year=year,
        thumb=_thumbnail(item),
        params=params,
        artist_id=artist_id,
    )


def _two_row_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    title = _node_text(item.get("title"), 300)
    if not title:
        return None
    subtitle_obj = item.get("subtitle") or {}
    subtitle = _node_text(subtitle_obj, 300)
    thumb = _thumbnail(item)

    nav = item.get("navigationEndpoint") or {}
    if not nav and isinstance(item.get("title"), dict):
        for run in item["title"].get("runs") or []:
            if isinstance(run, dict) and run.get("navigationEndpoint"):
                nav = run["navigationEndpoint"]
                break

    watch = nav.get("watchEndpoint") or {}
    watch_pl = nav.get("watchPlaylistEndpoint") or {}
    video_id = _text(watch.get("videoId"), 40)
    browse_id, params, page_type, fallback_pl = _browse_info(nav)

    sub_runs = subtitle_obj.get("runs") if isinstance(subtitle_obj, dict) else []
    artist_id, artist_from_run, _, _ = _extract_run_links(sub_runs or [])
    parts = [p.strip() for p in subtitle.split("•") if p.strip()]
    year = next((p for p in parts if YEAR_RE.match(p)), "")

    if video_id:
        artist = artist_from_run or (parts[0] if parts else "YouTube Music")
        duration = parse_duration(parts[-1]) if len(parts) >= 2 else 0
        track: Dict[str, Any] = {
            "kind": "track",
            "source": "yt",
            "id": video_id,
            "video_id": video_id,
            "title": title,
            "artist": artist,
            "album": "",
            "duration": duration,
            "thumb": thumb,
            "url": "",
            "subtitle": subtitle,
        }
        if watch.get("playlistId"):
            track["playlist_id"] = _text(watch.get("playlistId"), 200)
        if artist_id:
            track["artist_id"] = artist_id
        return track

    playlist_id = fallback_pl or _text(watch_pl.get("playlistId"), 200) or _overlay_playlist_id(item)
    if not browse_id and playlist_id:
        browse_id = playlist_id if playlist_id.startswith(("VL", "RD")) else f"VL{playlist_id}"
    if not browse_id:
        return None
    if not playlist_id and browse_id.startswith("VL"):
        playlist_id = browse_id[2:]

    kind = _card_kind_from_browse(browse_id, page_type) or "playlist"
    return _build_collection_card(
        kind=kind,
        browse_id=browse_id,
        playlist_id=playlist_id,
        title=title,
        parts=parts,
        artist=artist_from_run,
        year=year,
        thumb=thumb,
        params=params,
        artist_id=artist_id,
    )


def _continuation(payload: Dict[str, Any]) -> str:
    for key in (
        "musicShelfRenderer",
        "musicPlaylistShelfRenderer",
        "musicShelfContinuation",
        "musicPlaylistShelfContinuation",
        "sectionListRenderer",
    ):
        for shelf in _walk(payload, key):
            if not isinstance(shelf, dict):
                continue
            for item in shelf.get("continuations") or []:
                token = ((item or {}).get("nextContinuationData") or {}).get("continuation")
                if token:
                    return _text(token, 4000)
    return ""


def _is_gaierror(error: BaseException) -> bool:
    if isinstance(error, socket.gaierror):
        return True
    return (
        isinstance(error, urllib.error.URLError)
        and not isinstance(error, urllib.error.HTTPError)
        and isinstance(getattr(error, "reason", None), socket.gaierror)
    )


def _open_with_dns_retry(open_fn):
    """Retry a read-only HTTP request once on transient DNS resolution failure."""
    for attempt in range(2):
        try:
            return open_fn()
        except (urllib.error.URLError, socket.gaierror) as error:
            if attempt == 0 and _is_gaierror(error):
                time.sleep(0.15)
                continue
            raise


def _post(body: Dict[str, Any], endpoint: str = "search", timeout: int = 20) -> Dict[str, Any]:
    endpoint = str(endpoint or "search").strip().lstrip("/")
    if not _ENDPOINT_RE.fullmatch(endpoint):
        raise YTMusicError("Некорректный endpoint YouTube Music")
    payload_body = body if "context" in body else {**body, "context": _context()}
    request = urllib.request.Request(
        f"{API_BASE}/{endpoint}?prettyPrint=false",
        data=json.dumps(payload_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Origin": "https://music.youtube.com",
            "Referer": "https://music.youtube.com/",
        },
        method="POST",
    )
    try:
        with _open_with_dns_retry(lambda: extmusic.ipv4_urlopen(request, timeout=timeout)) as response:
            raw = response.read(8 << 20)
    except urllib.error.HTTPError as error:
        raise YTMusicError(f"YouTube Music ответил HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise YTMusicError(f"Нет связи с YouTube Music: {error}") from error
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as error:
        raise YTMusicError("YouTube Music вернул некорректный ответ") from error
    if not isinstance(payload, dict):
        raise YTMusicError("YouTube Music вернул некорректный ответ")
    if payload.get("error"):
        err = payload.get("error")
        msg = _text(err.get("message") if isinstance(err, dict) else err, 300)
        raise YTMusicError(msg or "Ошибка YouTube Music")
    return payload


def search(query: str, count: int = 30, kind: str = "songs") -> List[Dict[str, Any]]:
    query = _text(query, 120)
    if len(query) < 2:
        raise YTMusicError("Введите хотя бы два символа")
    norm_kind = _SEARCH_KIND_ALIASES.get(str(kind or "songs").strip().lower(), str(kind or "songs").strip().lower())
    if norm_kind not in SEARCH_PARAMS:
        norm_kind = "songs"
    params = SEARCH_PARAMS[norm_kind]
    count = max(1, min(50, _number(count) or 30))

    results: List[Dict[str, Any]] = []
    seen = set()
    continuation = ""
    for _ in range(MAX_PAGES):
        body: Dict[str, Any] = {"context": _context()}
        if continuation:
            body["continuation"] = continuation
        else:
            body["query"] = query
            body["params"] = params
        payload = _post(body)
        for item in _walk(payload, "musicResponsiveListItemRenderer"):
            if norm_kind in ("songs", "videos"):
                entry = track_from_item(item)
            else:
                entry = item_from_responsive(item, expected_kind=norm_kind)
            if not entry:
                continue
            key = entry.get("id") or entry.get("browse_id")
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(entry)
            if len(results) >= count:
                return results
        continuation = _continuation(payload)
        if not continuation:
            break
    return results


def _iter_shelves(payload: Dict[str, Any]) -> Iterable[Tuple[str, str, str, List[Dict[str, Any]]]]:
    """Yield ``(shelf_title, browse_id, params, raw_items)`` across carousel, regular, and grid shelves."""
    for shelf in _walk(payload, "musicCarouselShelfRenderer"):
        if not isinstance(shelf, dict):
            continue
        header = (shelf.get("header") or {}).get("musicCarouselShelfBasicHeaderRenderer") or {}
        title_obj = header.get("title") or {}
        title = _node_text(title_obj, 200)
        title_runs = title_obj.get("runs") if isinstance(title_obj, dict) else None
        nav = (
            title_runs[0].get("navigationEndpoint")
            if isinstance(title_runs, list) and title_runs and isinstance(title_runs[0], dict)
            else {}
        )
        bid, params, _, _ = _browse_info(nav)
        if not bid:
            more_btn = (header.get("moreContentButton") or {}).get("buttonRenderer") or {}
            bid, params, _, _ = _browse_info(more_btn.get("navigationEndpoint"))
        yield title, bid, params, [x for x in (shelf.get("contents") or []) if isinstance(x, dict)]
    for shelf in _walk(payload, "musicShelfRenderer"):
        if not isinstance(shelf, dict):
            continue
        title = _node_text(shelf.get("title"), 200)
        runs = (shelf.get("title") or {}).get("runs") if isinstance(shelf.get("title"), dict) else None
        nav = runs[0].get("navigationEndpoint") if isinstance(runs, list) and runs and isinstance(runs[0], dict) else None
        bid, params, _, _ = _browse_info(nav)
        yield title, bid, params, [x for x in (shelf.get("contents") or []) if isinstance(x, dict)]
    for grid in _walk(payload, "gridRenderer"):
        if not isinstance(grid, dict):
            continue
        yield "", "", "", [x for x in (grid.get("items") or []) if isinstance(x, dict)]


def _parse_moods(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    moods: List[Dict[str, str]] = []
    seen = set()
    for chip in _walk(payload, "chipCloudChipRenderer"):
        if not isinstance(chip, dict):
            continue
        title = _node_text(chip.get("text"), 120)
        nav = chip.get("navigationEndpoint") or {}
        browse_id, params, _, _ = _browse_info(nav)
        reload_ep = nav.get("browseSectionListReloadEndpoint") or {}
        cont = _text(((reload_ep.get("continuation") or {}).get("reloadContinuationData") or {}).get("continuation"), 4000)
        if title and (params or cont or browse_id) and title not in seen:
            seen.add(title)
            entry: Dict[str, str] = {"title": title, "browse_id": browse_id or "FEmusic_home", "params": params}
            if cont:
                entry["continuation"] = cont
            moods.append(entry)

    for btn in _walk(payload, "musicNavigationButtonRenderer"):
        if not isinstance(btn, dict):
            continue
        title = _node_text(btn.get("buttonText"), 120)
        browse_id, params, _, _ = _browse_info(btn.get("clickCommand"))
        if not title or not browse_id or browse_id in _SKIP_EXPLORE_MOOD_IDS:
            continue
        key = f"{browse_id}:{params}"
        if key in seen:
            continue
        seen.add(key)
        entry = {"title": title, "browse_id": browse_id, "params": params}
        color = (btn.get("solid") or {}).get("leftStripeColor")
        if isinstance(color, int):
            entry["color"] = f"#{color & 0xFFFFFF:06x}"
        moods.append(entry)
    return moods


def _append_unique(target: List[Dict[str, Any]], seen: set, entry: Optional[Dict[str, Any]]) -> None:
    if not entry:
        return
    key = entry.get("id") or entry.get("browse_id")
    if not key or key in seen:
        return
    seen.add(key)
    target.append(entry)


def home(limit: int = 30) -> Dict[str, Any]:
    limit = max(1, min(100, _number(limit) or 30))
    payload = _post({"context": _context(), "browseId": "FEmusic_home"}, endpoint="browse")

    tracks: List[Dict[str, Any]] = []
    seen_tracks = set()
    shelves: List[Dict[str, Any]] = []

    for title, bid, params, raw_items in _iter_shelves(payload):
        shelf_items: List[Dict[str, Any]] = []
        shelf_seen = set()
        for raw in raw_items:
            if "musicResponsiveListItemRenderer" in raw:
                entry = item_from_responsive(raw["musicResponsiveListItemRenderer"], expected_kind="")
            elif "musicTwoRowItemRenderer" in raw:
                entry = _two_row_item(raw["musicTwoRowItemRenderer"])
            else:
                entry = None
            if not entry:
                continue
            _append_unique(shelf_items, shelf_seen, entry)
            if entry.get("kind") == "track" or entry.get("video_id"):
                _append_unique(tracks, seen_tracks, entry)
        if shelf_items:
            shelf_obj: Dict[str, Any] = {"title": title or "Подборка", "items": shelf_items}
            if bid:
                shelf_obj["browse_id"] = bid
            if params:
                shelf_obj["params"] = params
            shelves.append(shelf_obj)

    if not tracks:
        for item in _walk(payload, "musicResponsiveListItemRenderer"):
            _append_unique(tracks, seen_tracks, track_from_item(item))
        for item in _walk(payload, "musicTwoRowItemRenderer"):
            entry = _two_row_item(item)
            if entry and entry.get("kind") == "track":
                _append_unique(tracks, seen_tracks, entry)

    return {
        "tracks": tracks[:limit],
        "shelves": shelves,
        "moods": _parse_moods(payload),
    }


def explore() -> Dict[str, Any]:
    payload = _post({"context": _context(), "browseId": "FEmusic_explore"}, endpoint="browse")
    releases: List[Dict[str, Any]] = []
    tracks: List[Dict[str, Any]] = []
    seen_rel = set()
    seen_tracks = set()

    for item in _walk(payload, "musicTwoRowItemRenderer"):
        entry = _two_row_item(item)
        if not entry:
            continue
        if entry.get("kind") == "track":
            _append_unique(tracks, seen_tracks, entry)
        elif entry.get("kind") in ("album", "playlist"):
            _append_unique(releases, seen_rel, entry)

    for item in _walk(payload, "musicResponsiveListItemRenderer"):
        _append_unique(tracks, seen_tracks, track_from_item(item))

    return {
        "releases": releases,
        "moods": _parse_moods(payload),
        "tracks": tracks,
    }


def charts(country: str = "RU") -> Dict[str, Any]:
    gl = re.sub(r"[^A-Z]", "", str(country or "RU").upper())[:2] or "RU"
    ctx_gl = "US" if gl == "ZZ" else gl
    payload = _post(
        {
            "context": _context(gl=ctx_gl, hl="ru"),
            "browseId": "FEmusic_charts",
            "formData": {"selectedValues": [gl]},
        },
        endpoint="browse",
    )

    tracks: List[Dict[str, Any]] = []
    artists: List[Dict[str, Any]] = []
    videos: List[Dict[str, Any]] = []
    seen_tracks = set()
    seen_artists = set()
    seen_videos = set()

    for title, _, _, raw_items in _iter_shelves(payload):
        low = title.lower()
        if "подкаст" in low or "podcast" in low:
            continue
        is_video_shelf = any(w in low for w in ("видео", "video", "клип"))
        for raw in raw_items:
            if "musicResponsiveListItemRenderer" in raw:
                resp = raw["musicResponsiveListItemRenderer"]
                if _video_id(resp):
                    tr = track_from_item(resp)
                    if is_video_shelf:
                        _append_unique(videos, seen_videos, tr)
                    else:
                        _append_unique(tracks, seen_tracks, tr)
                else:
                    card = item_from_responsive(resp, expected_kind="artists")
                    if card and card.get("kind") == "artist":
                        _append_unique(artists, seen_artists, card)
            elif "musicTwoRowItemRenderer" in raw:
                entry = _two_row_item(raw["musicTwoRowItemRenderer"])
                if not entry:
                    continue
                if entry.get("kind") == "artist":
                    _append_unique(artists, seen_artists, entry)
                else:
                    _append_unique(videos, seen_videos, entry)

    if not tracks and not artists and not videos:
        for item in _walk(payload, "musicResponsiveListItemRenderer"):
            if _video_id(item):
                _append_unique(tracks, seen_tracks, track_from_item(item))
            else:
                card = item_from_responsive(item, expected_kind="artists")
                if card and card.get("kind") == "artist":
                    _append_unique(artists, seen_artists, card)
        for item in _walk(payload, "musicTwoRowItemRenderer"):
            entry = _two_row_item(item)
            if not entry:
                continue
            if entry.get("kind") == "artist":
                _append_unique(artists, seen_artists, entry)
            else:
                _append_unique(videos, seen_videos, entry)

    if not tracks and videos:
        chart_pl = next((v for v in videos if v.get("browse_id") and not v.get("video_id")), None)
        if chart_pl:
            try:
                tracks = browse_collection(chart_pl["browse_id"], limit=30).get("tracks") or []
            except YTMusicError:
                pass

    return {"tracks": tracks, "artists": artists, "videos": videos}


def browse_mood(browse_id: str, params: str = "", continuation: str = "", limit: int = 60) -> Dict[str, Any]:
    limit = max(1, min(200, _number(limit) or 60))
    continuation = _text(continuation, 4000)
    browse_id = _text(browse_id, 200)
    params = _text(params, 500)

    if continuation:
        body: Dict[str, Any] = {"context": _context(), "continuation": continuation}
    else:
        if not browse_id:
            raise YTMusicError("Не указан идентификатор подборки")
        body = {"context": _context(), "browseId": browse_id}
        if params:
            body["params"] = params

    payload = _post(body, endpoint="browse")
    title = ""
    for hkey in ("musicHeaderRenderer", "musicResponsiveHeaderRenderer", "musicDetailHeaderRenderer"):
        hdr = next(_walk(payload, hkey), None)
        if isinstance(hdr, dict) and hdr.get("title"):
            title = _node_text(hdr.get("title"), 200)
            if title:
                break

    tracks: List[Dict[str, Any]] = []
    collections: List[Dict[str, Any]] = []
    seen_tracks = set()
    seen_cols = set()

    for item in _walk(payload, "musicResponsiveListItemRenderer"):
        if _video_id(item):
            _append_unique(tracks, seen_tracks, track_from_item(item))
        else:
            _append_unique(collections, seen_cols, item_from_responsive(item, expected_kind="playlists"))

    for item in _walk(payload, "musicTwoRowItemRenderer"):
        entry = _two_row_item(item)
        if not entry:
            continue
        if entry.get("kind") == "track":
            _append_unique(tracks, seen_tracks, entry)
        else:
            _append_unique(collections, seen_cols, entry)

    for model in _walk(payload, "musicListItemCarouselModel"):
        if not isinstance(model, dict):
            continue
        for item in model.get("items") or []:
            if not isinstance(item, dict):
                continue
            watch = ((item.get("onTap") or {}).get("innertubeCommand") or {}).get("watchEndpoint") or {}
            vid = _text(watch.get("videoId"), 40)
            t_title = _node_text(item.get("title"), 300)
            if not vid or not t_title:
                continue
            sub = _node_text(item.get("subtitle"), 300)
            parts = [p.strip() for p in sub.split("•") if p.strip()]
            sources = ((item.get("thumbnail") or {}).get("image") or {}).get("sources") or []
            thumb = _text((sources[-1] or {}).get("url"), 1000) if isinstance(sources, list) and sources else ""
            _append_unique(
                tracks,
                seen_tracks,
                {
                    "source": "yt",
                    "id": vid,
                    "title": t_title,
                    "artist": parts[0] if parts else "YouTube Music",
                    "album": "",
                    "duration": 0,
                    "thumb": thumb if thumb.startswith("https://") else "",
                    "url": "",
                },
            )

    return {
        "title": title,
        "tracks": tracks[:limit],
        "collections": collections[:limit],
    }


def _normalize_collection_id(browse_id: str) -> str:
    browse_id = _text(browse_id, 200)
    if not browse_id or not _BROWSE_ID_RE.fullmatch(browse_id):
        raise YTMusicError("Некорректный идентификатор коллекции")
    if browse_id.startswith(("PL", "OLAK5uy_", "RD")):
        return "VL" + browse_id
    return browse_id


def _parse_collection_header(payload: Dict[str, Any], browse_id: str) -> Dict[str, str]:
    header: Dict[str, Any] = {}
    header_key = ""
    for key in (
        "musicResponsiveHeaderRenderer",
        "musicDetailHeaderRenderer",
        "musicImmersiveHeaderRenderer",
        "musicVisualHeaderRenderer",
    ):
        found = next(_walk(payload, key), None)
        if isinstance(found, dict):
            header = found
            header_key = key
            break

    title = _node_text(header.get("title"), 300)
    subtitle_main = _node_text(header.get("subtitle"), 300)
    second_sub = _node_text(header.get("secondSubtitle"), 300)
    strapline_obj = header.get("straplineTextOne") or {}
    strapline = _node_text(strapline_obj, 200)
    strapline_runs = strapline_obj.get("runs") if isinstance(strapline_obj, dict) else []
    artist_id, _, _, _ = _extract_run_links(strapline_runs or [])

    desc_node = header.get("description") or {}
    if isinstance(desc_node, dict) and "musicDescriptionShelfRenderer" in desc_node:
        desc_node = desc_node["musicDescriptionShelfRenderer"].get("description")
    description = _node_text(desc_node, 1500)

    thumb = _thumbnail(header)
    if not thumb:
        for t in _walk(payload, "musicThumbnailRenderer"):
            cand = _thumbnail({"thumbnail": {"musicThumbnailRenderer": t}})
            if cand:
                thumb = cand
                break

    playlist_id = browse_id[2:] if browse_id.startswith("VL") else ""
    if not playlist_id:
        playlist_id = _overlay_playlist_id(header)
    if not playlist_id:
        for wp in _walk(payload, "watchPlaylistEndpoint"):
            if isinstance(wp, dict) and wp.get("playlistId"):
                playlist_id = _text(wp.get("playlistId"), 200)
                break

    if browse_id.startswith("UC") or header_key in ("musicImmersiveHeaderRenderer", "musicVisualHeaderRenderer"):
        kind = "artist"
    elif browse_id.startswith(("MPREb_", "VLOLAK5uy_")) or (
        not browse_id.startswith("VL") and bool(_ALBUM_SUBTITLE_RE.search(subtitle_main))
    ):
        kind = "album"
    else:
        kind = "playlist"

    if kind == "artist" and not subtitle_main:
        listeners = _node_text(header.get("monthlyListenerCount"), 120)
        subs = _node_text(
            ((header.get("subscriptionButton") or {}).get("subscribeButtonRenderer") or {}).get("longSubscriberCountText"),
            120,
        )
        subtitle_main = listeners or subs

    sub_parts = [p for p in (strapline, subtitle_main, second_sub) if p]
    return {
        "kind": kind,
        "title": title,
        "subtitle": " • ".join(sub_parts),
        "description": description,
        "thumb": thumb,
        "playlist_id": playlist_id,
        "artist": strapline or (title if kind == "artist" else ""),
        "artist_id": artist_id or (browse_id if kind == "artist" else ""),
    }


def browse_collection(browse_id: str, params: str = "", limit: int = 100) -> Dict[str, Any]:
    browse_id = _normalize_collection_id(browse_id)
    limit = max(1, min(300, _number(limit) or 100))
    body: Dict[str, Any] = {"context": _context(), "browseId": browse_id}
    if params:
        body["params"] = _text(params, 400)

    payload = _post(body, endpoint="browse")
    meta = _parse_collection_header(payload, browse_id)
    kind = meta["kind"]

    tracks: List[Dict[str, Any]] = []
    seen_tracks = set()
    albums: List[Dict[str, Any]] = []
    seen_albums = set()
    similar: List[Dict[str, Any]] = []
    seen_similar = set()

    def _collect_tracks(page: Dict[str, Any]) -> None:
        for item in _walk(page, "musicResponsiveListItemRenderer"):
            tr = track_from_item(item)
            if not tr:
                continue
            if kind == "album":
                if not tr.get("album") and meta["title"]:
                    tr["album"] = meta["title"]
                if not tr.get("thumb") and meta["thumb"]:
                    tr["thumb"] = meta["thumb"]
                if (not tr.get("artist") or tr["artist"] == "YouTube Music") and meta["artist"]:
                    tr["artist"] = meta["artist"]
                if not tr.get("artist_id") and meta["artist_id"]:
                    tr["artist_id"] = meta["artist_id"]
            elif kind == "artist":
                if not tr.get("artist_id") and browse_id.startswith("UC"):
                    tr["artist_id"] = browse_id
                if (not tr.get("artist") or tr["artist"] == "YouTube Music") and meta["title"]:
                    tr["artist"] = meta["title"]
            if meta["playlist_id"] and not tr.get("playlist_id"):
                tr["playlist_id"] = meta["playlist_id"]
            _append_unique(tracks, seen_tracks, tr)
            if len(tracks) >= limit:
                return

    _collect_tracks(payload)
    continuation = _continuation(payload)
    pages = 0
    while len(tracks) < limit and continuation and pages < 5:
        pages += 1
        page = _post({"context": _context(), "continuation": continuation}, endpoint="browse")
        _collect_tracks(page)
        continuation = _continuation(page)

    for shelf_title, _, _, raw_items in _iter_shelves(payload):
        is_album_shelf = bool(_ALBUM_SHELF_RE.search(shelf_title))
        is_similar_shelf = bool(_SIMILAR_SHELF_RE.search(shelf_title))
        for raw in raw_items:
            if "musicTwoRowItemRenderer" not in raw:
                continue
            entry = _two_row_item(raw["musicTwoRowItemRenderer"])
            if not entry:
                continue
            if is_similar_shelf or (not is_album_shelf and entry.get("kind") == "artist"):
                _append_unique(similar, seen_similar, entry)
            elif is_album_shelf or entry.get("kind") == "album":
                _append_unique(albums, seen_albums, entry)

    return {
        "kind": kind,
        "title": meta["title"] or "Без названия",
        "subtitle": meta["subtitle"],
        "description": meta["description"],
        "thumb": meta["thumb"],
        "browse_id": browse_id,
        "playlist_id": meta["playlist_id"],
        "artist": meta["artist"],
        "artist_id": meta["artist_id"],
        "tracks": tracks[:limit],
        "albums": albums,
        "similar": similar,
    }


def _panel_track(panel_item: Dict[str, Any], fallback_playlist_id: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(panel_item, dict):
        return None
    vid = _text(
        panel_item.get("videoId")
        or ((panel_item.get("navigationEndpoint") or {}).get("watchEndpoint") or {}).get("videoId"),
        40,
    )
    if not vid:
        return None
    title = _node_text(panel_item.get("title"), 300) or "Без названия"
    byline = panel_item.get("longBylineText") or panel_item.get("shortBylineText") or {}
    runs = byline.get("runs") if isinstance(byline, dict) else []
    artist_id, artist_from_run, album_id, album_from_run = _extract_run_links(runs or [])
    byline_text = _node_text(panel_item.get("shortBylineText") or byline, 300)
    parts = [p.strip() for p in byline_text.split("•") if p.strip()]
    artist = artist_from_run or (parts[0] if parts else "YouTube Music")
    duration = parse_duration(_node_text(panel_item.get("lengthText"), 20))
    pl_id = _text(
        ((panel_item.get("navigationEndpoint") or {}).get("watchEndpoint") or {}).get("playlistId"),
        200,
    ) or fallback_playlist_id

    track: Dict[str, Any] = {
        "source": "yt",
        "id": vid,
        "title": title,
        "artist": artist,
        "album": album_from_run,
        "duration": duration,
        "thumb": _thumbnail(panel_item),
        "url": "",
        "playlist_id": pl_id,
    }
    if artist_id:
        track["artist_id"] = artist_id
    if album_id:
        track["album_id"] = album_id
    return track


def radio(video_id: str, playlist_id: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    video_id = _text(video_id, 40)
    playlist_id = _text(playlist_id, 200)
    limit = max(1, min(100, _number(limit) or 30))
    if not video_id and not playlist_id:
        raise YTMusicError("Не указан идентификатор трека для радио")

    if playlist_id.startswith("VL"):
        playlist_id = playlist_id[2:]
    body: Dict[str, Any] = {
        "context": _context(),
        "isAudioOnly": True,
        "playlistId": playlist_id or f"RDAMVM{video_id}",
    }
    if video_id:
        body["videoId"] = video_id

    payload = _post(body, endpoint="next")
    tracks: List[Dict[str, Any]] = []
    seen = set()

    for panel_item in _walk(payload, "playlistPanelVideoRenderer"):
        track = _panel_track(panel_item, fallback_playlist_id=body["playlistId"])
        if not track or track["id"] in seen:
            continue
        seen.add(track["id"])
        tracks.append(track)
        if len(tracks) >= limit:
            break

    return tracks


def track_artist_info(video_id: str) -> Dict[str, str]:
    """Fetch artist/album browse metadata for a YouTube video from InnerTube `next`."""
    vid = _text(video_id, 40)
    if not vid:
        return {}
    payload = _post({"context": _context(), "videoId": vid}, endpoint="next")
    first: Optional[Dict[str, Any]] = None
    chosen: Optional[Dict[str, Any]] = None
    for panel_item in _walk(payload, "playlistPanelVideoRenderer"):
        track = _panel_track(panel_item)
        if not track:
            continue
        if first is None:
            first = track
        if track["id"] == vid:
            chosen = track
            break
    target = chosen or first
    if not target:
        return {}
    return {
        "artist_id": str(target.get("artist_id") or ""),
        "artist": str(target.get("artist") or ""),
        "album_id": str(target.get("album_id") or ""),
        "album": str(target.get("album") or ""),
    }


def _find_lyrics_browse_id(payload: Dict[str, Any]) -> str:
    for tab in _walk(payload, "tabRenderer"):
        if not isinstance(tab, dict):
            continue
        bid, _, page_type, _ = _browse_info(tab.get("endpoint"))
        if not bid:
            continue
        title = _node_text(tab.get("title"), 80).lower()
        if bid.startswith("MPLYt") or page_type == "MUSIC_PAGE_TYPE_TRACK_LYRICS" or title in ("текст", "lyrics"):
            return bid
    return ""


def _extract_timed_lyrics(payload: Dict[str, Any]) -> Optional[List[List[Any]]]:
    """Extract synced `[seconds, text]` lines from `musicTimedLyricsRenderer` when present."""
    for timed in _walk(payload, "musicTimedLyricsRenderer"):
        if not isinstance(timed, dict):
            continue
        lines: List[List[Any]] = []
        for entry in timed.get("timedLyricsData") or []:
            if not isinstance(entry, dict):
                continue
            line = _text(entry.get("lyricLine"), 500).strip()
            cue = entry.get("cueRange") if isinstance(entry.get("cueRange"), dict) else {}
            start_ms = cue.get("startTimeMilliseconds")
            if start_ms is None:
                continue
            try:
                sec = round(max(0.0, int(start_ms) / 1000.0), 2)
            except (TypeError, ValueError):
                continue
            if not line and not lines:
                continue
            lines.append([sec, line])
        if lines:
            return lines
    return None


def _extract_lyrics_text(payload: Dict[str, Any]) -> str:
    for shelf in _walk(payload, "musicDescriptionShelfRenderer"):
        if not isinstance(shelf, dict):
            continue
        text = _node_text(shelf.get("description"), 20000)
        if text:
            return text
    for timed in _walk(payload, "musicTimedLyricsRenderer"):
        if not isinstance(timed, dict):
            continue
        lines = [
            _text((entry or {}).get("lyricLine"), 500)
            for entry in timed.get("timedLyricsData") or []
            if isinstance(entry, dict) and entry.get("lyricLine")
        ]
        if lines:
            return "\n".join(lines)
    return ""


def lyrics(video_id: str) -> Dict[str, Any]:
    video_id = _text(video_id, 40)
    if not video_id:
        return {"sync": None, "text": None, "source": None}
    next_payload = _post({"context": _context(), "videoId": video_id}, endpoint="next")
    lyrics_bid = _find_lyrics_browse_id(next_payload)
    if not lyrics_bid:
        return {"sync": None, "text": None, "source": None}
    ly_payload = _post({"context": _context(), "browseId": lyrics_bid}, endpoint="browse")
    sync = _extract_timed_lyrics(ly_payload)
    text = _extract_lyrics_text(ly_payload)
    if sync and not text:
        text = "\n".join(str(item[1]) for item in sync if len(item) > 1 and item[1])
    if not text:
        return {"sync": None, "text": None, "source": None}
    return {"sync": sync, "text": text, "source": "YouTube Music"}


def clean_track_meta(title: str, artist: str = "") -> Tuple[str, str]:
    """Normalize a YouTube title and artist for external lyrics lookup (e.g. LRCLIB)."""
    clean_artist = str(artist or "").strip()
    clean_artist = re.sub(r"\s*-\s*Topic$", "", clean_artist, flags=re.I)
    clean_artist = re.sub(r"(?<=[A-Za-z0-9])VEVO$", "", clean_artist, flags=re.I)
    clean_artist = _TRAILING_FEAT_RE.sub("", clean_artist)
    clean_artist = re.sub(r"\s+", " ", clean_artist).strip()

    clean_title = str(title or "").strip()
    clean_title = _NOISE_TAG_RE.sub(" ", clean_title)
    clean_title = _TRAILING_FEAT_RE.sub("", clean_title)
    clean_title = re.sub(r"\s*(?:[|]|//|/\s+)\s*(?:official|lyrics?|audio|video|hd|4k).*$", "", clean_title, flags=re.I)
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" -|/")

    match = _DASH_SPLIT_RE.match(clean_title)
    if match:
        left, right = match.group(1).strip(), match.group(2).strip()
        left_norm = re.sub(r"\s+", " ", left.lower())
        art_norm = re.sub(r"\s+", " ", clean_artist.lower())
        left_compact = re.sub(r"\W+", "", left_norm)
        art_compact = re.sub(r"\W+", "", art_norm)
        if not art_norm or art_norm in ("youtube music", "youtube", "various artists"):
            clean_artist, clean_title = left, right
        elif left_compact and art_compact and left_compact == art_compact:
            clean_artist, clean_title = left, right
        elif left_norm == art_norm or left_norm in art_norm or art_norm in left_norm:
            clean_title = right

    clean_title = _NOISE_TAG_RE.sub(" ", clean_title)
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" -|/")
    clean_artist = re.sub(r"\s+", " ", clean_artist).strip(" -|/")
    return clean_title, clean_artist


_GENERIC_ARTIST_NAMES = frozenset({
    "",
    "youtube music",
    "youtube",
    "various artists",
    "разные исполнители",
})
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|/|;|\b(?:feat\.?|ft\.?|featuring|with|x)\b)\s*", re.I)


def _norm_artist_name(value: str) -> str:
    text = re.sub(r"\s*-\s*Topic$", "", str(value or "").strip(), flags=re.I)
    text = text.casefold().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", text).strip()


def _pick_artist_match(entries: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    q_norm = _norm_artist_name(query)
    if not q_norm:
        return None
    uc_entries = [
        e for e in (entries or [])
        if isinstance(e, dict) and str(e.get("browse_id") or "").startswith("UC")
    ]
    for entry in uc_entries:
        if _norm_artist_name(entry.get("title") or "") == q_norm:
            return entry
    if len(q_norm) < 3:
        return None
    q_tokens = set(q_norm.split())
    for entry in uc_entries:
        e_norm = _norm_artist_name(entry.get("title") or "")
        e_tokens = set(e_norm.split())
        if e_tokens and (q_tokens <= e_tokens or e_tokens <= q_tokens):
            return entry
    return None


def find_artist_id(artist: str = "", title: str = "") -> Dict[str, str]:
    """Resolve a YouTube Music artist ``browse_id`` (`UC...`) by artist name or track title."""
    clean_title, clean_artist = clean_track_meta(title, artist)
    art_norm = _norm_artist_name(clean_artist)

    if art_norm not in _GENERIC_ARTIST_NAMES and len(clean_artist) >= 2:
        candidates = [clean_artist]
        primary = _ARTIST_SPLIT_RE.split(clean_artist, maxsplit=1)[0].strip()
        if len(primary) >= 2 and _norm_artist_name(primary) != art_norm:
            candidates.append(primary)
        for cand in candidates:
            hit = _pick_artist_match(search(cand, count=5, kind="artists"), cand)
            if hit:
                return {
                    "artist_id": str(hit.get("browse_id") or ""),
                    "artist": str(hit.get("title") or cand),
                }

    if len(clean_title) >= 2:
        song_q = f"{clean_artist} {clean_title}".strip() if art_norm not in _GENERIC_ARTIST_NAMES else clean_title
        title_norm = _norm_artist_name(clean_title)
        for song in search(song_q, count=5, kind="songs"):
            bid = str(song.get("artist_id") or "").strip()
            if not bid.startswith("UC"):
                continue
            s_title_norm = _norm_artist_name(song.get("title") or "")
            s_art_norm = _norm_artist_name(song.get("artist") or "")
            title_ok = bool(title_norm and s_title_norm and (title_norm in s_title_norm or s_title_norm in title_norm))
            art_ok = art_norm in _GENERIC_ARTIST_NAMES or (art_norm and s_art_norm and (art_norm in s_art_norm or s_art_norm in art_norm))
            if title_ok and art_ok:
                return {
                    "artist_id": bid,
                    "artist": str(song.get("artist") or clean_artist),
                }
    return {}


def is_allowed_thumb_url(url: str) -> bool:
    """Allow only HTTPS YouTube/Google image CDN hosts without userinfo."""
    if not isinstance(url, str):
        return False
    raw = url.strip()
    if not raw or len(raw) > 2048 or "\\" in raw:
        return False
    if any(ord(ch) < 32 or ch.isspace() for ch in raw):
        return False
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    if not parsed.scheme.isascii() or parsed.scheme.lower() != "https":
        return False
    if (
        not parsed.netloc
        or not parsed.netloc.isascii()
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    if port not in (None, 443):
        return False
    raw_host = parsed.hostname or ""
    if not raw_host or not raw_host.isascii():
        return False
    host = raw_host.lower()
    if not host or not _ASCII_HOST_RE.fullmatch(host) or host.startswith(".") or host.endswith(".") or ".." in host:
        return False
    return any(host == dom or host.endswith("." + dom) for dom in _ALLOWED_THUMB_DOMAINS)


class _SafeThumbRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_repeats = 3
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, str(newurl or ""))
        if not is_allowed_thumb_url(target):
            raise YTMusicError("Небезопасный редирект обложки")
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_thumb(url: str, max_bytes: int = 2 << 20) -> Tuple[bytes, str]:
    """Fetch an allowlisted cover image up to ``max_bytes`` with redirect validation."""
    if not is_allowed_thumb_url(url):
        raise YTMusicError("Недопустимый URL обложки")
    limit = max(1, min(10 << 20, _number(max_bytes) or (2 << 20)))
    request = urllib.request.Request(
        url.strip(),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/*,*/*;q=0.8",
            "Referer": "https://music.youtube.com/",
        },
        method="GET",
    )
    def _open_thumb():
        if extmusic.is_urlopen_mocked():
            return urllib.request.urlopen(request, timeout=6)
        return extmusic.build_ipv4_opener(_SafeThumbRedirectHandler).open(request, timeout=6)

    try:
        with _open_with_dns_retry(_open_thumb) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            if final_url and not is_allowed_thumb_url(str(final_url)):
                raise YTMusicError("Небезопасный редирект обложки")
            status = getattr(response, "status", None)
            headers = getattr(response, "headers", None) or {}
            if status in (301, 302, 303, 307, 308):
                loc = headers.get("Location") or headers.get("location") or ""
                if not loc or not is_allowed_thumb_url(urllib.parse.urljoin(url.strip(), str(loc))):
                    raise YTMusicError("Небезопасный редирект обложки")
                raise YTMusicError("Слишком много редиректов обложки")
            content_length = headers.get("Content-Length") or headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > limit:
                        raise YTMusicError("Обложка слишком большая")
                except ValueError:
                    pass
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise YTMusicError("Обложка слишком большая")
            ctype_raw = headers.get("Content-Type") or headers.get("content-type") or "image/jpeg"
            ctype = str(ctype_raw).split(";", 1)[0].strip().lower() or "image/jpeg"
            if not ctype.startswith("image/") or ctype.startswith("image/svg"):
                raise YTMusicError("Ответ не является изображением")
            return raw, ctype
    except YTMusicError:
        raise
    except urllib.error.HTTPError as error:
        raise YTMusicError(f"Обложка недоступна (HTTP {error.code})") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise YTMusicError(f"Ошибка загрузки обложки: {error}") from error
