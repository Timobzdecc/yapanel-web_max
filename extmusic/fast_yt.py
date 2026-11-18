"""Fast direct YouTube Music audio resolver.

Bypasses slow yt-dlp subprocess startup (20-40s) and YouTube bot JS challenge
solvers by using the lightweight InnerTube VISIONOS API client with pre-cached
visitor integrity tokens and forced IPv4 routing. Resolves direct unencrypted
HTTPS audio streams (Opus 160kbps / AAC 128kbps) from googlevideo.com in ~0.5s.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict, Optional

from . import ipv4_urlopen

# Known active visitor token and signature timestamp for VisionOS client profile
VISITOR_ID = (
    "CgtHaUtDWHd1dlF3OCjhoOLXBjIKCgJSVRIEGgAgG2KqAgqnAkN0b0JDQVVTakFFQkRCNlBESUk5YUdz"
    "TnRwU1cydlp2eVJtVXpaVl8zWWhncFdpaG5JV0MwLXF5NHh0RmNhaUMyX1BHZVBSMFo5TW9RNF9raXpm"
    "VGJsaHo3cXFfVUpXNGVJZDcyY21ob0VLZGYxYnhPUDdsc3dtaWIweDlBcGdoR1BKYkZramxCU0Q1SU1z"
    "Snd2OFJWZnZDdG15SU4tMEpRLUNGUDA0eDJ2bnpZZFBwdmtqLTVNV3VmYXpvU00zQUpIWW9BeWdGTWtV"
    "QkhGajFhanRKZm0xaDhIWi0ySHJpUFRYeHd2OE96U0t2bWhzU1kxdnk4ZTM4QUh6aHBFNnZKUHhrRjBh"
    "WXE3bWJVWjVXa01Md2pvSndyV2NucUs2S2E4UFFmd1k%3D"
)
SIGNATURE_TIMESTAMP = 20767

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 10800  # 3 hours
VISIONOS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
)


class FastYTError(ValueError):
    """Raised when YouTube stream resolution fails."""


def fast_resolve(
    video_id: str,
    cookies: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 6.0,
) -> Dict[str, Any]:
    """Resolve a YouTube video ID into a direct HTTPS audio playback URL in ~0.5s."""
    video_id = str(video_id or "").strip()
    if not video_id:
        raise FastYTError("Empty YouTube video ID")

    cached = _CACHE.get(video_id)
    if cached and time.time() < cached["expires_at"]:
        return cached["data"]

    payload = {
        "context": {
            "client": {
                "clientName": "VISIONOS",
                "clientVersion": "1.02",
                "deviceMake": "Apple",
                "deviceModel": "RealityDevice17,1",
                "userAgent": VISIONOS_USER_AGENT,
                "osName": "visionOS",
                "osVersion": "26.5.23O471",
                "hl": "en",
                "gl": "US",
            }
        },
        "videoId": video_id,
        "playbackContext": {
            "contentPlaybackContext": {
                "html5Preference": "HTML5_PREF_WANTS",
                "signatureTimestamp": SIGNATURE_TIMESTAMP,
            }
        },
        "contentCheckOk": True,
        "racyCheckOk": True,
    }

    headers = {
        "X-YouTube-Client-Name": "101",
        "X-YouTube-Client-Version": "1.02",
        "Origin": "https://www.youtube.com",
        "User-Agent": VISIONOS_USER_AGENT,
        "Content-Type": "application/json",
        "X-Goog-Visitor-Id": VISITOR_ID,
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookies:
        headers["Cookie"] = cookies

    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )

    t0 = time.time()
    try:
        with ipv4_urlopen(req, timeout=timeout) as resp:
            elapsed = round(time.time() - t0, 3)
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as error:
        raise FastYTError(f"Ошибка связи с сервером YouTube: {error}") from error

    status = data.get("playabilityStatus", {}).get("status")
    if status != "OK":
        reason = data.get("playabilityStatus", {}).get("reason") or status
        raise FastYTError(f"YouTube отклонил запрос: {reason}")

    streaming_data = data.get("streamingData", {})
    formats = streaming_data.get("adaptiveFormats", []) + streaming_data.get("formats", [])
    audio_formats = [
        f for f in formats if "audio" in f.get("mimeType", "") and f.get("url")
    ]
    if not audio_formats:
        raise FastYTError("YouTube не вернул прямой аудиопоток (требуется вход)")

    # Prioritize audio/mp4 (AAC itag 140/139) because Station Max GStreamer
    # has native qtdemux and aacparse, but lacks matroskademux for audio/webm.
    mp4_audio = [f for f in audio_formats if "audio/mp4" in f.get("mimeType", "")]
    if mp4_audio:
        mp4_audio.sort(key=lambda x: int(x.get("bitrate") or 0), reverse=True)
        pool = mp4_audio
    else:
        audio_formats.sort(key=lambda x: int(x.get("bitrate") or 0), reverse=True)
        pool = audio_formats
    best = pool[0]
    fallback_urls = [
        str(f["url"])
        for f in pool[1:4]
        if f.get("url") and f["url"] != best["url"]
    ]

    details = data.get("videoDetails", {})
    duration = int(details.get("lengthSeconds") or 0)
    title = details.get("title", "YouTube Music")
    artist = details.get("author", "YouTube Artist")
    mime = best.get("mimeType", "")
    acodec = "opus" if "opus" in mime.lower() else "mp4a"
    ext = "webm" if "webm" in mime.lower() else "m4a"

    res = {
        "id": video_id,
        "url": best["url"],
        "fallback_urls": fallback_urls,
        "title": title,
        "artist": artist,
        "duration": duration,
        "ext": ext,
        "acodec": acodec,
        "abr": int((best.get("bitrate") or 0) // 1000),
        "resolve_time": elapsed,
    }

    _CACHE[video_id] = {
        "data": res,
        "expires_at": time.time() + _CACHE_TTL,
    }
    return res
