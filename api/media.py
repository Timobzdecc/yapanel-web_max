"""HTTP-маршруты медиа: музыка, радио, визуализатор и Bluetooth."""
import glagol

from extmusic.service import get_service

from .router import Response, Router


def _radio_logo(req):
    logo = req.panel.radio_logo(req.q("name", ""))
    if not logo:
        return Response({"error": "логотип не найден"}, status=404)
    return Response(logo[0], content_type=logo[1],
                    headers=(("Content-Security-Policy", "default-src 'none'"),))


def _music_lyrics(req):
    if req.q("id"):
        return req.panel.music_lyrics(req.q("id"))
    return req.panel.ymusic.lyrics_by_name(
        req.q("title", ""), req.q("artist", ""), req.q("album"), float(req.q("duration") or 0))


def _ext_artist_track(panel, track):
    fn = getattr(panel, "_ext_track_payload", None)
    if callable(fn):
        res = fn(track)
        if isinstance(res, dict):
            return res
    vid = str(track.get("id") or track.get("video_id") or "").strip()
    artist = track.get("artist") or "YouTube Music"
    album = track.get("album") or "YouTube Music"
    duration = int(track.get("duration") or 0) * 1000
    cover = track.get("thumb") or ""
    artist_id = str(track.get("artist_id") or "").strip()
    aid = (artist_id if artist_id.startswith("ext:yt:") else f"ext:yt:{artist_id}") if artist_id else ""
    return {
        "id": f"ext:yt:{vid}",
        "title": track.get("title") or "Без названия",
        "artists": artist,
        "artistList": [{"id": aid, "name": artist}],
        "album": {"id": "", "title": album},
        "durationMs": duration,
        "cover": cover,
        "coverBig": cover,
        "liked": False,
        "available": True,
    }


def _music_artist(req):
    raw_id = str(req.q("id", "") or "").strip()
    if raw_id.startswith("ext:yt:"):
        browse_id = raw_id[7:].strip()
        if not browse_id:
            raise ValueError("Не указан идентификатор исполнителя")
        col = get_service(req.panel).yt_artist(browse_id)
        bid = str(col.get("browse_id") or browse_id)
        tracks = [t for t in (col.get("tracks") or []) if isinstance(t, dict)]
        albums = [a for a in (col.get("albums") or []) if isinstance(a, dict)]
        similar = []
        for item in (col.get("similar") or [])[:12]:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("browse_id") or item.get("id") or "").strip()
            sname = str(item.get("title") or item.get("name") or "").strip()
            if not sname:
                continue
            similar.append({
                "id": f"ext:yt:{sid}" if sid else "",
                "browse_id": sid,
                "name": sname,
                "thumb": str(item.get("thumb") or ""),
            })
        thumb = str(col.get("thumb") or "")
        return {
            "id": f"ext:yt:{bid}",
            "browse_id": bid,
            "source": "yt",
            "name": str(col.get("title") or col.get("artist") or "YouTube Music"),
            "subtitle": str(col.get("subtitle") or ""),
            "genres": [],
            "cover": thumb,
            "cover_big": thumb,
            "likes": None,
            "tracks": len(tracks) or None,
            "albums": len(albums) or None,
            "albumList": albums,
            "popular": [_ext_artist_track(req.panel, t) for t in tracks[:10]],
            "similar": similar,
            "description": str(col.get("description") or "")[:1500],
        }
    req.panel._need_login()
    return req.panel.ymusic.artist(raw_id)


def register(router: Router):
    router.get("/api/music", lambda r: r.panel.music())
    router.get("/api/music/info", lambda r: r.panel.music_info(r.q("id", "")))
    router.get("/api/music/lyrics", _music_lyrics)
    router.get("/api/music/queue", lambda r: r.panel.music_queue())
    router.get("/api/music/similar", lambda r: r.panel.music_similar(r.q("id", "")))
    router.get("/api/music/artist", _music_artist)
    router.get("/api/auth", lambda r: {**r.panel.auth.info(), "oauth_url": glagol.OAUTH_URL})
    router.get("/api/auth/qr", lambda r: r.panel.auth_qr_status())
    router.get("/api/radio", lambda r: r.panel.radio_status())
    router.get("/api/radio/search", lambda r: r.panel.radio_search(r.q("q", ""), r.q("country", "")))
    router.get("/api/radio/logo", _radio_logo)
    router.get("/api/viz", lambda r: r.panel.viz_status())
    router.get("/api/bt", lambda r: r.panel.bt_state())
    router.get("/api/bt/memes", lambda r: r.panel.bt_memes_status())

    router.post("/api/music/cmd", lambda r: r.panel.music_cmd(r.data))
    router.post("/api/music/like", lambda r: r.panel.music_like(r.data["id"], bool(r.data.get("like", True))))
    router.post("/api/music/dislike", lambda r: r.panel.music_dislike(r.data["id"]))
    router.post("/api/music/play", lambda r: r.panel.music_play(r.data["id"], r.data.get("type", "track")))
    router.post("/api/auth/qr", lambda r: r.panel.auth_qr_start())
    router.post("/api/auth/oauth", lambda r: r.panel.auth_oauth(r.data["url"]))
    router.post("/api/auth/logout", lambda r: r.panel.auth_logout())
    router.post("/api/radio", lambda r: r.panel.radio_set(r.data))
    router.post("/api/radio/play", lambda r: r.panel.radio_play(r.data))
    router.post("/api/radio/stop", lambda r: r.panel.radio_stop())
    router.post("/api/radio/miss-seen", lambda r: r.panel.assistant_command({"radio_miss_seen": True}))
    router.post("/api/radio/check", lambda r: r.panel.radio_check(r.data.get("url")))
    router.post("/api/radio/alice", lambda r: r.panel.radio_alice(r.data.get("name")))
    router.post("/api/viz", lambda r: r.panel.viz_set(bool(r.data.get("on"))))
    router.post("/api/bt", lambda r: r.panel.bt_action(r.data))
    router.post("/api/bt/memes", lambda r: r.panel.bt_memes_set(bool(r.data.get("enabled"))))
    router.post("/api/bt/memes/reset", lambda r: r.panel.bt_memes_reset(r.data.get("kind")))
