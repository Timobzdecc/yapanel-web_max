"""Direct stream resolution through the bundled yt-dlp zipapp.

yt-dlp is shipped in ``device/ytmusic/yt-dlp`` and runs as a subprocess with the
panel's own Python, so the station needs no pip install. Playback resolves only
public tracks; account cookies are used for the library endpoint.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"
MIN_SIZE = 1_000_000


class YTDLPError(ValueError):
    """A resolution error that can be shown to the user."""


def path() -> Path:
    override = os.environ.get("YTDLP_PATH", "").strip()
    if override:
        return Path(override)
    default_p = Path(__file__).resolve().parent.parent / "device" / "ytmusic" / "yt-dlp"
    if default_p.is_file() and default_p.stat().st_size >= MIN_SIZE:
        return default_p
    fallback_p = Path("/data/local/tmp/yt-dlp")
    if fallback_p.is_file() and fallback_p.stat().st_size >= MIN_SIZE:
        return fallback_p
    return default_p


def available() -> bool:
    try:
        return path().is_file() and path().stat().st_size >= MIN_SIZE
    except OSError:
        return False


def version(timeout: int = 15) -> str:
    if not available():
        return ""
    try:
        result = subprocess.run(
            [sys.executable, str(path()), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()[:40] if result.returncode == 0 else ""


def _friendly(stderr: str) -> str:
    text = (stderr or "").strip()
    lower = text.lower()
    if "sign in to confirm" in lower or "not a bot" in lower or "confirm you're not a bot" in lower:
        return (
            "YouTube требует подтверждение входа. Обновите yt-dlp "
            "(device/ytmusic/yt-dlp) или попробуйте позже."
        )
    if "video unavailable" in lower or "this video is unavailable" in lower:
        return "Трек недоступен в YouTube"
    if "private video" in lower:
        return "Трек закрыт автором"
    if "http error 403" in lower or "forbidden" in lower:
        return "YouTube отклонил поток (403). Обновите yt-dlp и попробуйте снова."
    if "http error 429" in lower or "too many requests" in lower:
        return "YouTube временно ограничил запросы, попробуйте позже"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    detail = lines[-1] if lines else "неизвестная ошибка"
    return f"yt-dlp: {detail[:220]}"


def library(cookies: str, limit: int = 100, timeout: int = 90) -> list:
    """Liked music from the signed-in YouTube Music account (playlist ``LM``)."""
    if not cookies or not Path(cookies).is_file():
        raise YTDLPError("Сначала подключите аккаунт YouTube (cookies.txt)")
    if not available():
        raise YTDLPError("yt-dlp не найден на станции — переустановите панель")
    # yt-dlp writes the cookie jar back after a request and can replace a
    # signed-in file with anonymous cookies when the session is rejected.
    # Never let it touch the panel's stored copy.
    with tempfile.TemporaryDirectory() as directory:
        cookie_copy = Path(directory) / "cookies.txt"
        shutil.copyfile(cookies, cookie_copy)
        try:
            os.chmod(cookie_copy, 0o600)
        except OSError:
            pass
        command = [
            sys.executable,
            str(path()),
            "--no-warnings",
            "--flat-playlist",
            "--cookies",
            str(cookie_copy),
            "--dump-single-json",
            "https://music.youtube.com/playlist?list=LM",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise YTDLPError("YouTube не ответил вовремя") from error
        except OSError as error:
            raise YTDLPError(f"Не удалось запустить yt-dlp: {error}") from error
    if result.returncode != 0:
        raise YTDLPError(_friendly(result.stderr))
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise YTDLPError("yt-dlp вернул некорректный ответ") from error
    entries = payload.get("entries") if isinstance(payload, dict) else None
    tracks = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        thumbs = entry.get("thumbnails") or []
        thumb = ""
        if isinstance(thumbs, list) and thumbs:
            thumb = str((thumbs[-1] or {}).get("url") or "")
        artist = str(entry.get("uploader") or entry.get("channel") or entry.get("artist") or "YouTube Music")
        tracks.append({
            "source": "yt",
            "id": str(entry.get("id"))[:40],
            "title": str(entry.get("title") or "Без названия")[:300],
            "artist": artist[:300],
            "album": str(entry.get("album") or "")[:300],
            "duration": int(entry.get("duration") or 0),
            "thumb": thumb[:1000],
            "url": "",
        })
        if len(tracks) >= max(1, min(200, int(limit or 100))):
            break
    return tracks


def resolve(video_id: str, timeout: int = 90, cookies: str = "", fast: bool = False) -> Dict[str, Any]:
    video_id = str(video_id or "").strip()
    if not video_id or not all(ch.isalnum() or ch in "-_" for ch in video_id):
        raise YTDLPError("Некорректный id трека YouTube")
    if not available():
        raise YTDLPError("yt-dlp не найден на станции — переустановите панель")
    command = [
        sys.executable,
        str(path()),
        "--no-warnings",
        "--no-playlist",
    ]
    if cookies and Path(cookies).is_file():
        command += ["--cookies", cookies]
    command += [
        "--socket-timeout",
        "15",
        "--retries",
        "2",
        "-f",
        FORMAT,
    ]
    if fast:
        command += ["--get-url", f"https://music.youtube.com/watch?v={video_id}"]
    else:
        command += ["--dump-single-json", f"https://music.youtube.com/watch?v={video_id}"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise YTDLPError("YouTube не ответил вовремя") from error
    except OSError as error:
        raise YTDLPError(f"Не удалось запустить yt-dlp: {error}") from error
    if result.returncode != 0:
        raise YTDLPError(_friendly(result.stderr))
    if fast:
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        stream = lines[-1] if lines else ""
        if not (stream.startswith("http://") or stream.startswith("https://")):
            raise YTDLPError("yt-dlp не отдал прямой аудиопоток")
        return {"url": stream, "ext": "m4a", "acodec": "mp4a.40.2", "abr": 0, "duration": 0,
                "title": "", "uploader": ""}
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise YTDLPError("yt-dlp вернул некорректный ответ") from error
    if not isinstance(payload, dict):
        raise YTDLPError("yt-dlp вернул некорректный ответ")
    entries = payload.get("entries")
    if isinstance(entries, list) and entries:
        payload = entries[0] if isinstance(entries[0], dict) else {}
    stream = str(payload.get("url") or "").strip()
    if not (stream.startswith("http://") or stream.startswith("https://")):
        raise YTDLPError("yt-dlp не отдал прямой аудиопоток")
    raw_loud = payload.get("loudness_db")
    loudness_db = None
    if raw_loud is not None:
        try:
            val = float(raw_loud)
            if math.isfinite(val) and -40.0 <= val <= 25.0:
                loudness_db = round(val, 2)
        except (TypeError, ValueError):
            pass
    return {
        "url": stream,
        "ext": str(payload.get("ext") or "")[:20],
        "acodec": str(payload.get("acodec") or "")[:40],
        "abr": payload.get("abr") or 0,
        "duration": payload.get("duration") or 0,
        "title": str(payload.get("title") or "")[:200],
        "uploader": str(payload.get("uploader") or payload.get("channel") or "")[:200],
        "loudness_db": loudness_db,
    }
