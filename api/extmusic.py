"""HTTP API for external music sources (VK Music and YouTube Music)."""
from __future__ import annotations

from .router import Response, Router
from extmusic.service import ExtMusicError, get_service


def _thumb(req):
    url = str(req.q("url", "") or "").strip()
    if not url:
        return Response({"error": "Не указан url обложки"}, status=400)
    try:
        data, ctype = get_service(req.panel).fetch_thumb(url)
    except (ExtMusicError, ValueError) as error:
        return Response({"error": str(error)}, status=400)
    norm_ctype = str(ctype or "").split(";", 1)[0].strip().lower()
    if (
        not norm_ctype.startswith("image/")
        or norm_ctype.startswith("image/svg")
        or not isinstance(data, (bytes, bytearray))
    ):
        return Response({"error": "Ответ не является безопасным изображением"}, status=400)
    return Response(
        bytes(data),
        content_type=norm_ctype,
        headers=(
            ("Cache-Control", "public, max-age=86400"),
            ("Access-Control-Allow-Origin", "*"),
            ("X-Content-Type-Options", "nosniff"),
        ),
    )


def _yt_like(req, svc):
    d = req.data
    vid = str(d.get("id") or d.get("video_id") or "")
    if "like" in d and d["like"] is not None:
        on = bool(d["like"])
    elif "on" in d and d["on"] is not None:
        on = bool(d["on"])
    else:
        on = True
    return svc.yt_like(vid, on=on)


def _yt_dislike(req, svc):
    d = req.data
    vid = str(d.get("id") or d.get("video_id") or "")
    skip = True if d.get("skip") is None else bool(d["skip"])
    return svc.yt_dislike(vid, skip=skip)


def _yt_playlist_create(req, svc):
    d = req.data
    vids = d.get("video_ids")
    if vids is None:
        vids = d.get("ids")
    return svc.yt_playlist_create(
        title=str(d.get("title") or d.get("name") or ""),
        video_ids=vids,
        description=str(d.get("description") or ""),
        privacy=str(d.get("privacy") or "PRIVATE"),
        from_queue=bool(d.get("from_queue")),
    )


def _yt_playlist_add(req, svc):
    d = req.data
    pid = str(d.get("playlist_id") or d.get("browse_id") or d.get("id") or "")
    vids = d.get("video_ids")
    if vids is None:
        vids = d.get("video_id")
    if vids is None:
        vids = d.get("track_id")
    if vids is None:
        vids = d.get("ids")
    return svc.yt_playlist_add(pid, vids)


def _shuffle(req, svc):
    d = req.data
    val = d["enable"] if "enable" in d else d.get("enabled")
    return svc.set_shuffle(None if val is None else bool(val))


def _repeat(req, svc):
    mode = req.data.get("mode")
    return svc.set_repeat(None if mode is None else str(mode))


def _autoplay(req, svc):
    d = req.data
    if "enable" in d:
        val = d["enable"]
    elif "enabled" in d:
        val = d["enabled"]
    else:
        val = req.arg("enable")
    if val is None:
        raise ValueError("Нужно поле enable")
    return svc.set_autoplay(bool(val))


def _queue_add(req, svc):
    d = req.data
    items = d.get("items")
    if items is None:
        items = d.get("tracks")
    if items is None:
        items = d.get("track")
    return svc.queue_add(items, mode=str(d.get("mode") or "end"))


def _require_int_index(val, msg):
    """Accept only integer queue indices (int or integer string); reject floats/bools."""
    if val is None or isinstance(val, (bool, float)):
        raise ValueError(msg)
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s or (s[0] in "+-" and len(s) == 1) or not (s[1:] if s[0] in "+-" else s).isdigit():
            raise ValueError(msg)
        return int(s)
    raise ValueError(msg)


def _queue_remove(req, svc):
    idx = _require_int_index(req.arg("index"), "Неверный индекс трека в очереди")
    return svc.queue_remove(idx)


def _queue_move(req, svc):
    d = req.data
    fi = d["from"] if "from" in d else d.get("from_index")
    ti = d["to"] if "to" in d else d.get("to_index")
    fi = _require_int_index(fi, "Нужны индексы from и to")
    ti = _require_int_index(ti, "Нужны индексы from и to")
    return svc.queue_move(fi, ti)


def register(router: Router):
    def service(req):
        return get_service(req.panel)

    router.get("/api/extmusic/status", lambda r: service(r).status())
    router.get("/api/extmusic/state", lambda r: service(r).status())
    router.post("/api/extmusic/source", lambda r: service(r).set_source(str(r.data.get("source") or "")))
    router.post("/api/extmusic/vk/auth", lambda r: service(r).auth(r.data))
    router.post("/api/extmusic/vk/logout", lambda r: service(r).logout())
    router.post("/api/extmusic/vk/login/start", lambda r: service(r).login_start(r.data))
    router.post("/api/extmusic/vk/login/code", lambda r: service(r).login_code(r.data))
    router.post("/api/extmusic/vk/login/captcha", lambda r: service(r).login_captcha(r.data))
    router.post("/api/extmusic/vk/login/cancel", lambda r: service(r).login_cancel(r.data))
    router.get("/api/extmusic/search", lambda r: service(r).search(
        r.q("q", ""),
        int(r.q("count", 30) or 30),
        str(r.q("source", "") or ""),
        str(r.q("kind", "songs") or "songs"),
    ))
    router.get("/api/extmusic/vk/search", lambda r: service(r).search(r.q("q", ""), int(r.q("count", 30) or 30), "vk"))
    router.get("/api/extmusic/yt/search", lambda r: service(r).yt_search(
        r.q("q", ""),
        int(r.q("count", 30) or 30),
        str(r.q("kind", "songs") or "songs"),
    ))
    router.get("/api/extmusic/yt/home", lambda r: service(r).yt_home(
        limit=int(r.q("limit") or r.q("count") or 30)
    ))
    router.get("/api/extmusic/yt/explore", lambda r: service(r).yt_explore())
    router.get("/api/extmusic/yt/charts", lambda r: service(r).yt_charts(str(r.q("country", "RU") or "RU")))
    router.get("/api/extmusic/yt/mood", lambda r: service(r).yt_mood(
        str(r.q("browse_id", "") or ""),
        str(r.q("params", "") or ""),
        str(r.q("continuation", "") or ""),
        int(r.q("limit", 60) or 60),
    ))
    router.get("/api/extmusic/yt/collection", lambda r: service(r).yt_collection(
        str(r.q("browse_id", "") or ""),
        str(r.q("params", "") or ""),
        int(r.q("limit", 100) or 100),
    ))
    router.get("/api/extmusic/yt/cookies", lambda r: service(r).yt_cookies_status())
    router.post("/api/extmusic/yt/cookies", lambda r: service(r).yt_cookies_save(str(r.data.get("cookies") or "")))
    router.post("/api/extmusic/yt/cookies/clear", lambda r: service(r).yt_cookies_clear())
    router.get("/api/extmusic/yt/library", lambda r: service(r).yt_library(int(r.q("count", 100) or 100)))
    router.get("/api/extmusic/yt/account", lambda r: service(r).yt_account_info())
    router.post("/api/extmusic/yt/account/logout", lambda r: service(r).yt_account_logout())
    router.get("/api/extmusic/yt/login", lambda r: service(r).yt_login_status())
    router.post("/api/extmusic/yt/login/start", lambda r: service(r).yt_login_start(r.data))
    router.post("/api/extmusic/yt/login/code", lambda r: service(r).yt_login_code(r.data))
    router.post("/api/extmusic/yt/login/cancel", lambda r: service(r).yt_login_cancel())
    router.get("/api/extmusic/yt/account/library", lambda r: service(r).yt_account_library())
    router.get("/api/extmusic/yt/account/liked", lambda r: service(r).yt_account_liked(int(r.q("count", 100) or 100)))
    router.get("/api/extmusic/yt/account/playlist", lambda r: service(r).yt_account_playlist(
        str(r.q("browse_id", "") or ""), int(r.q("count", 100) or 100)
    ))
    router.get("/api/extmusic/yt/account/home", lambda r: service(r).yt_account_home(int(r.q("count", 30) or 30)))
    router.post("/api/extmusic/yt/account/mood", lambda r: service(r).yt_account_mood(
        str(r.data.get("token") or ""), int(r.data.get("count", 100) or 100)
    ))
    router.post("/api/extmusic/yt/like", lambda r: _yt_like(r, service(r)))
    router.post("/api/extmusic/yt/dislike", lambda r: _yt_dislike(r, service(r)))
    router.post("/api/extmusic/yt/playlist/create", lambda r: _yt_playlist_create(r, service(r)))
    router.post("/api/extmusic/yt/playlist/add", lambda r: _yt_playlist_add(r, service(r)))
    router.get("/api/extmusic/thumb", _thumb)
    router.get("/api/extmusic/screen/frame", lambda r: service(r).screen_frame())
    router.post("/api/extmusic/screen/start", lambda r: service(r).screen_start())
    router.post("/api/extmusic/screen/library", lambda r: service(r).screen_library())
    router.post("/api/extmusic/screen/home", lambda r: service(r).screen_home())
    router.post("/api/extmusic/screen/tap", lambda r: service(r).screen_tap(r.data))
    router.post("/api/extmusic/screen/text", lambda r: service(r).screen_text(r.data))
    router.post("/api/extmusic/screen/key", lambda r: service(r).screen_key(r.data))
    router.post("/api/extmusic/screen/clear", lambda r: service(r).screen_clear())
    router.post("/api/extmusic/play", lambda r: service(r).play(r.data.get("track"), r.data.get("queue")))
    router.post("/api/extmusic/pause", lambda r: service(r).pause())
    router.post("/api/extmusic/resume", lambda r: service(r).resume())
    router.post("/api/extmusic/seek", lambda r: service(r).seek(int(r.data.get("position", 0) or 0)))
    router.post("/api/extmusic/stop", lambda r: service(r).stop())
    router.post("/api/extmusic/next", lambda r: service(r).next())
    router.post("/api/extmusic/prev", lambda r: service(r).prev())
    router.post("/api/extmusic/shuffle", lambda r: _shuffle(r, service(r)))
    router.post("/api/extmusic/repeat", lambda r: _repeat(r, service(r)))
    router.post("/api/extmusic/autoplay", lambda r: _autoplay(r, service(r)))
    router.post("/api/extmusic/queue/add", lambda r: _queue_add(r, service(r)))
    router.post("/api/extmusic/queue/remove", lambda r: _queue_remove(r, service(r)))
    router.post("/api/extmusic/queue/move", lambda r: _queue_move(r, service(r)))
    router.post("/api/extmusic/queue/clear", lambda r: service(r).queue_clear_upcoming())
    router.post("/api/extmusic/radio", lambda r: service(r).radio_play(
        track=r.data.get("track"),
        browse_id=str(r.data.get("browse_id") or ""),
    ))
    router.post("/api/extmusic/yt/mix", lambda r: service(r).yt_mix_play(
        name=str(r.data.get("name") or r.data.get("query") or ""),
        index=r.data.get("index"),
        browse_id=str(r.data.get("browse_id") or ""),
    ))
