"""Small, private storage for external music credentials.

The file lives next to the panel's other secrets and is written atomically with
0600 permissions. Tokens are never logged or returned to the browser.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
}

YT_ALLOWED_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "SIDCC", "PREF", "YSC", "VISITOR_INFO1_LIVE",
}


def parse_and_validate_cookies(text: str) -> str:
    """Validate and convert cookie text (Netscape or HTTP Cookie header) to Netscape format."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Пустой ввод cookies")
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Файл cookies слишком большой (максимум 1 МБ)")

    if text.lower().startswith("cookie:"):
        text = text[7:].strip()

    cookie_dict: Dict[str, str] = {}
    if "\t" in text:
        # Netscape format: domain, flag, path, secure, expiration, name, value
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                dom = parts[0].strip().lower()
                name = parts[5].strip()
                val = parts[6].strip()
                if name in YT_ALLOWED_COOKIES:
                    if not dom or "youtube.com" in dom or "google.com" in dom:
                        cookie_dict[name] = val
    else:
        # Semicolon / newline separated header: name=value; name2=value2
        pairs = re.split(r"[;\r\n]+", text)
        for pair in pairs:
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in YT_ALLOWED_COOKIES:
                cookie_dict[k] = v

    found = [k for k in cookie_dict if k in AUTH_COOKIES]
    if not found:
        raise ValueError(
            "В cookies нет данных авторизации YouTube (нужны SID, HSID, SSID, SAPISID или __Secure-3PSID). "
            "Войдите в YouTube Music в браузере и скопируйте cookies заново."
        )

    lines = ["# Netscape HTTP Cookie File", "# https://curl.haxx.se/rfc/cookie_spec.html", ""]
    for k, v in cookie_dict.items():
        lines.append(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}")
        lines.append(f".google.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}")
    return "\n".join(lines) + "\n"


def has_auth_cookie_text(text: str) -> bool:
    """True if a Netscape cookie file contains a signed-in YouTube session."""
    try:
        parse_and_validate_cookies(text)
        return True
    except ValueError:
        return False


def default_data_dir() -> Path:
    try:
        from config import get_config

        return Path(get_config().server.data_dir)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


class ExtMusicStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_data_dir() / ".extmusic.json"
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            _atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def source(self) -> str:
        value = str(self.load().get("source") or "").strip().lower()
        return value if value in ("vk", "yt") else "yt"

    def save_source(self, source: str) -> None:
        source = str(source or "").strip().lower()
        if source not in ("vk", "yt"):
            raise ValueError("неизвестный источник музыки")
        with self._lock:
            data = self.load()
            data["source"] = source
            self._save(data)

    def vk(self) -> Dict[str, str]:
        value = self.load().get("vk")
        if not isinstance(value, dict):
            return {}
        return {
            "token": str(value.get("token") or ""),
            "user_agent": str(value.get("user_agent") or ""),
            "user_id": str(value.get("user_id") or ""),
            "name": str(value.get("name") or ""),
        }

    def save_vk(self, token: str, user_agent: str, user_id: str = "", name: str = "") -> None:
        with self._lock:
            data = self.load()
            data["vk"] = {
                "token": str(token or ""),
                "user_agent": str(user_agent or ""),
                "user_id": str(user_id or ""),
                "name": str(name or ""),
            }
            self._save(data)

    def clear_vk(self) -> None:
        with self._lock:
            data = self.load()
            data.pop("vk", None)
            self._save(data)

    # ---------- YouTube cookies ----------
    def cookies_path(self) -> Path:
        return self.path.parent / ".extmusic.cookies.txt"

    def has_cookies(self) -> bool:
        with self._lock:
            try:
                return self.cookies_path().is_file() and self.cookies_path().stat().st_size > 0
            except OSError:
                return False

    def has_auth_cookies(self) -> bool:
        with self._lock:
            try:
                text = self.cookies_path().read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return has_auth_cookie_text(text)

    def save_cookies(self, text: str) -> None:
        formatted = parse_and_validate_cookies(text)
        with self._lock:
            _atomic_write_text(self.cookies_path(), formatted)

    def clear_cookies(self) -> None:
        with self._lock:
            try:
                self.cookies_path().unlink()
            except OSError:
                pass
            data = self.load()
            data.pop("yt_cookie_account", None)
            self._save(data)

    def get_cookies_text(self) -> str:
        with self._lock:
            try:
                return self.cookies_path().read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

    def yt_cookies(self) -> str:
        return self.get_cookies_text()

    def get_cookies_dict(self) -> Dict[str, str]:
        text = self.get_cookies_text()
        res: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                res[parts[5]] = parts[6]
        return res

    def get_cookie_header(self) -> str:
        c = self.get_cookies_dict()
        filtered = {k: v for k, v in c.items() if k in YT_ALLOWED_COOKIES}
        return "; ".join(f"{k}={v}" for k, v in filtered.items())

    def get_sapisid_hash(self, origin: str = "https://music.youtube.com") -> Optional[str]:
        c = self.get_cookies_dict()
        sapisid = c.get("SAPISID") or c.get("__Secure-3PAPISID") or c.get("__Secure-1PAPISID")
        if not sapisid:
            return None
        now = int(time.time())
        to_hash = f"{now} {sapisid} {origin}".encode("utf-8")
        sig = hashlib.sha1(to_hash).hexdigest()
        return f"SAPISIDHASH {now}_{sig}"

    # ---------- YouTube OAuth ----------
    def oauth(self) -> Dict[str, Any]:
        data = self.load()
        val = data.get("yt_oauth")
        return val if isinstance(val, dict) else {}

    def save_oauth(self, tokens: Dict[str, Any]) -> None:
        with self._lock:
            data = self.load()
            data["yt_oauth"] = tokens
            self._save(data)

    def clear_oauth(self) -> None:
        with self._lock:
            data = self.load()
            data.pop("yt_oauth", None)
            self._save(data)

    def has_oauth(self) -> bool:
        tokens = self.oauth()
        return bool(tokens.get("access_token") or tokens.get("refresh_token"))

    # ---------- YouTube Account Info ----------
    def yt_account_name(self) -> str:
        with self._lock:
            if self.has_oauth():
                return str(self.oauth().get("email") or "Google-аккаунт")
            if self.has_auth_cookies():
                data = self.load()
                return str(data.get("yt_cookie_account") or "Google-аккаунт (Cookies)")
            return ""

    def save_yt_cookie_account(self, name: str) -> None:
        with self._lock:
            data = self.load()
            data["yt_cookie_account"] = str(name or "")
            self._save(data)

    def is_yt_authenticated(self) -> bool:
        with self._lock:
            return self.has_auth_cookies() or self.has_oauth()

    # ---------- YouTube Recent History ----------
    @staticmethod
    def _compact_yt_track(track: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(track, dict):
            return None
        source = str(track.get("source") or "yt").strip().lower()
        if source != "yt":
            return None
        video_id = str(track.get("id") or track.get("video_id") or "").strip()[:40]
        if not video_id:
            return None
        try:
            duration = max(0, int(track.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        return {
            "source": "yt",
            "id": video_id,
            "title": str(track.get("title") or "Без названия")[:300],
            "artist": str(track.get("artist") or "YouTube Music")[:300],
            "album": str(track.get("album") or "")[:300],
            "duration": duration,
            "thumb": str(track.get("thumb") or "")[:1000],
            "artist_id": str(track.get("artist_id") or "")[:200],
        }

    def recent_yt(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = self.load().get("yt_recent")
        if not isinstance(items, list):
            return []
        result: List[Dict[str, Any]] = []
        seen = set()
        for raw in items:
            entry = self._compact_yt_track(raw)
            if not entry or entry["id"] in seen:
                continue
            seen.add(entry["id"])
            result.append(entry)
        return result

    def push_recent_yt(self, track: Dict[str, Any], limit: int = 30) -> List[Dict[str, Any]]:
        entry = self._compact_yt_track(track)
        try:
            cap = max(1, min(100, int(limit or 30)))
        except (TypeError, ValueError):
            cap = 30
        with self._lock:
            if not entry:
                return self.recent_yt()[:cap]
            data = self.load()
            existing = data.get("yt_recent")
            items: List[Dict[str, Any]] = [entry]
            seen = {entry["id"]}
            if isinstance(existing, list):
                for raw in existing:
                    prev = self._compact_yt_track(raw)
                    if not prev or prev["id"] in seen:
                        continue
                    seen.add(prev["id"])
                    items.append(prev)
                    if len(items) >= cap:
                        break
            data["yt_recent"] = items[:cap]
            self._save(data)
            return list(data["yt_recent"])
