"""Minimal VK audio API client (unofficial, token + User-Agent).

Only public search and direct audio URLs are used. No password is ever sent by
the panel: the user obtains a token with a compatible client and pastes it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

import extmusic

API = "https://api.vk.com/method/"
VERSION = "5.131"
# The same user-agent as vkpymusic's KateMobileClient. A token issued for a
# different client must be paired with that client's exact user-agent.
DEFAULT_USER_AGENT = (
    "VKAndroidApp/4.13.1-1206 (Android 4.4.3; SDK 19; armeabi; ; ru)"
)


class VKError(ValueError):
    """A VK API error that can be shown to the user."""


def _text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _thumb(item: Dict[str, Any]) -> str:
    album = item.get("album")
    candidates = []
    if isinstance(album, dict):
        thumb = album.get("thumb")
        if isinstance(thumb, dict):
            candidates.extend(thumb.values())
    thumb = item.get("thumb")
    if isinstance(thumb, dict):
        candidates.extend(thumb.values())
    for value in candidates:
        value = str(value or "").strip()
        if value.startswith("https://"):
            return value
    return ""


def track_from_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise VKError("VK вернул некорректный трек")
    track_id = _text(item.get("id"), 40)
    owner_id = _text(item.get("owner_id"), 40)
    if not track_id or not owner_id:
        raise VKError("У трека нет id")
    return {
        "source": "vk",
        "id": track_id,
        "owner_id": owner_id,
        "title": _text(item.get("title") or "Без названия"),
        "artist": _text(item.get("artist") or item.get("performer") or "VK Music"),
        "duration": _number(item.get("duration")),
        "url": _text(item.get("url"), 2000),
        "thumb": _thumb(item),
    }


class VKClient:
    def __init__(self, token: str, user_agent: str = "", timeout: int = 15):
        self.token = str(token or "").strip()
        self.user_agent = str(user_agent or "").strip() or DEFAULT_USER_AGENT
        self.timeout = timeout
        if not self.token:
            raise VKError("Не указан токен VK")

    def _call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query = {
            "access_token": self.token,
            "v": VERSION,
            "https": 1,
            "lang": "ru",
        }
        query.update(params or {})
        url = API + method + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with extmusic.ipv4_urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4 << 20)
        except urllib.error.HTTPError as error:
            raise VKError(f"VK API HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise VKError(f"VK недоступен: {error}") from error
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise VKError("VK вернул не JSON") from error
        if not isinstance(payload, dict):
            raise VKError("VK вернул неожиданный ответ")
        if payload.get("error"):
            error = payload["error"] if isinstance(payload["error"], dict) else {}
            code = _number(error.get("error_code"))
            message = _text(error.get("error_msg") or "ошибка VK", 500)
            if code == 14:
                raise VKError("VK просит капчу: получите новый токен")
            if code in (5, 15):
                raise VKError("Токен VK не имеет доступа к аудио или истёк")
            if code == 6:
                raise VKError("VK ограничил частоту запросов, попробуйте позже")
            raise VKError(message or f"Ошибка VK {code}")
        return payload.get("response")

    def validate(self) -> Dict[str, Any]:
        """Check that the token has audio access and return a small profile hint."""
        response = self._call("audio.search", {"q": "test", "count": 1, "offset": 0})
        # A successful call proves the audio scope. The profile call is optional.
        name = ""
        user_id = ""
        try:
            profile = self._call("account.getProfileInfo")
            if isinstance(profile, dict):
                name = " ".join(
                    part for part in (_text(profile.get("first_name"), 80), _text(profile.get("last_name"), 80)) if part
                )
                user_id = _text(profile.get("uid") or profile.get("id"), 40)
        except VKError:
            pass
        return {"ok": True, "name": name, "user_id": user_id, "sample": response}

    @staticmethod
    def _items(response: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            items = response.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def search(self, query: str, count: int = 30, offset: int = 0) -> List[Dict[str, Any]]:
        query = str(query or "").strip()[:120]
        if len(query) < 2:
            raise VKError("Запрос слишком короткий")
        count = max(1, min(100, int(count or 30)))
        offset = max(0, int(offset or 0))
        response = self._call(
            "audio.search",
            {"q": query, "count": count, "offset": offset, "sort": 0, "autocomplete": 1},
        )
        return [track_from_item(item) for item in self._items(response)]

    def get_by_id(self, owner_id: str, track_id: str) -> Optional[Dict[str, Any]]:
        owner_id = _text(owner_id, 40)
        track_id = _text(track_id, 40)
        if not owner_id or not track_id:
            return None
        response = self._call("audio.getById", {"audios": f"{owner_id}_{track_id}"})
        for item in self._items(response):
            track = track_from_item(item)
            if track["id"] == track_id and track["owner_id"] == owner_id:
                return track
        return None

    def resolve(self, track: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """Return a track with a fresh direct audio URL."""
        track = dict(track or {})
        url = _text(track.get("url"), 2000)
        if force or not url:
            fresh = self.get_by_id(track.get("owner_id", ""), track.get("id", ""))
            if fresh:
                track.update(fresh)
                url = fresh.get("url", "")
        if not url:
            raise VKError("VK не отдал ссылку на этот трек (возможно, нужен другой токен)")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise VKError("VK вернул неподдерживаемую ссылку")
        track["url"] = url
        return track
