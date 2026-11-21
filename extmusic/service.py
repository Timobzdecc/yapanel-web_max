"""Music service for the panel: VK Music and YouTube Music.

One active source, one queue, one station-side player. Yandex Music is
untouched: the old /api/music path continues to use Glagol directly.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import fast_yt, ipv4_urlopen, ytdlp, ytmusic
from .fast_yt import FastYTError
from .gstbridge import FALLBACK_TRACK_ID, GstBridge, GstBridgeError
from .google_oauth import GoogleOAuth, GoogleOAuthError
from .native_yt import NativeYT, NativeYTError
from .player import PlayerClient, PlayerError
from .screen import ScreenError, StationScreen
from .store import ExtMusicStore
from .ytaccount import YTAccount, YTAccountError
from .vk import VKClient, VKError, track_from_item
from .vkauth import VKAuthError, VKDirectAuth
from .ytdlp import YTDLPError
from .ytmusic import YTMusicError

_KNOWN_TRACKS_LIMIT = 500
_YT_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ARTIST_CACHE_TTL = 6 * 3600
_ARTIST_NEGATIVE_TTL = 120
_log = logging.getLogger("yapanel.extmusic")


class ExtMusicError(ValueError):
    pass


class ExtMusicService:
    def __init__(self, panel):
        self.panel = panel
        self.store = ExtMusicStore()
        self.player = PlayerClient(panel)
        self.native = NativeYT(panel)
        self.gst = GstBridge(panel)
        self.gst.set_normalize(self.store.normalize())
        self.screen = StationScreen(panel)
        self.ytmusic = ytmusic
        self.yt_account = YTAccount(store=self.store, panel=panel)
        self.lock = threading.RLock()
        self.current: Optional[Dict[str, Any]] = None
        self.queue: List[Dict[str, Any]] = []
        self._orig_queue: List[Dict[str, Any]] = []
        self.index = -1
        self.shuffle: bool = False
        self.repeat: str = "none"
        self.autoplay_radio: bool = True
        self._ext_started_at: float = 0.0
        self._known_tracks: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._artist_cache: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        self._artist_cache_loaded = False
        self._known_lock = threading.Lock()
        self.error = ""
        self.skipped: List[Dict[str, str]] = []
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_interval: float = 1.0
        self._gen = 0
        self._vk_auth: Optional[VKDirectAuth] = None
        self._prefetch: Dict[str, Dict[str, Any]] = {}
        self._prefetching = set()
        self._prefetch_lock = threading.Lock()
        self._prefetch_ttl = 600
        self._native_loading_until = 0.0
        self._native_force_loading_until = 0.0
        self._native_stopped_since = 0.0
        self._native_warm = False
        self._hw_lock = threading.Lock()
        self._dlna = None
        self._multiroom = None

    # ---------- Station Max hardware integration (BLE remote + LED matrix OSD + DLNA + Multiroom) ----------
    def _get_dlna(self):
        dlna = getattr(self.panel, "_extmusic_dlna", None)
        if dlna is not None:
            return dlna
        with self._hw_lock:
            dlna = getattr(self.panel, "_extmusic_dlna", None)
            if dlna is None:
                from extmusic.dlna import MaxDlnaRenderer

                dlna = MaxDlnaRenderer(self.panel)
                self._dlna = dlna
                try:
                    self.panel._extmusic_dlna = dlna
                except Exception:  # noqa: BLE001
                    pass
            return dlna

    def _get_multiroom(self):
        mr = getattr(self.panel, "_extmusic_multiroom", None)
        if mr is not None:
            return mr
        with self._hw_lock:
            mr = getattr(self.panel, "_extmusic_multiroom", None)
            if mr is None:
                from extmusic.multiroom import MaxMultiroomCoordinator

                mr = MaxMultiroomCoordinator(self.panel)
                self._multiroom = mr
                try:
                    self.panel._extmusic_multiroom = mr
                except Exception:  # noqa: BLE001
                    pass
            return mr

    def master_relay_url(self, seek_sec: float = 0.0) -> Optional[str]:
        if not self._is_gst_current():
            return None
        try:
            from extmusic.dlna import detect_lan_ip

            lan_ip = detect_lan_ip()
            if not lan_ip or lan_ip == "127.0.0.1":
                return None
            return self.gst.lan_proxy_url(lan_ip, seek_sec=seek_sec)
        except Exception:  # noqa: BLE001
            return None

    def dlna_status(self) -> Dict[str, Any]:
        return self._get_dlna().status()

    def dlna_action(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._get_dlna().configure(data)
        except ValueError as err:
            raise ExtMusicError(str(err)) from err

    def multiroom_status(self) -> Dict[str, Any]:
        return self._get_multiroom().status()

    def multiroom_action(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._get_multiroom().configure(data)
        except ValueError as err:
            raise ExtMusicError(str(err)) from err

    def _emit_multiroom_play(self, track: Dict[str, Any]) -> None:
        if not isinstance(track, dict) or track.get("multiroom_follower"):
            return
        try:
            mr = self._get_multiroom()
            if mr.enabled:
                relay = self.master_relay_url(0.0)
                mr.on_master_play(track, relay_url=relay)
        except Exception:  # noqa: BLE001
            pass

    def _emit_multiroom_event(self, event: str, *args: Any, suppress: bool = False) -> None:
        if suppress:
            return
        try:
            mr = getattr(self.panel, "_extmusic_multiroom", None) or self._multiroom
            if mr is None or not mr.enabled:
                return
            fn = getattr(mr, f"on_master_{event}", None)
            if callable(fn):
                fn(*args)
        except Exception:  # noqa: BLE001
            pass
    def _get_osd(self):
        osd = getattr(self.panel, "_extmusic_osd", None)
        if osd is not None:
            return osd
        with self._hw_lock:
            osd = getattr(self.panel, "_extmusic_osd", None)
            if osd is None:
                from extmusic.led_osd import MaxLedOSD

                osd = MaxLedOSD(self.panel)
                try:
                    self.panel._extmusic_osd = osd
                except Exception:  # noqa: BLE001
                    pass
            return osd

    def _get_remote(self):
        remote = getattr(self.panel, "_extmusic_remote", None)
        if remote is not None:
            return remote
        with self._hw_lock:
            remote = getattr(self.panel, "_extmusic_remote", None)
            if remote is None:
                from extmusic.remote import MaxBleRemote

                remote = MaxBleRemote(self.panel)
                try:
                    self.panel._extmusic_remote = remote
                except Exception:  # noqa: BLE001
                    pass
            return remote

    def _preinject_gst(self) -> None:
        try:
            self.gst.ensure_injected()
        except Exception:  # noqa: BLE001 - best-effort warm injection at startup
            pass

    def ensure_hw_started(self) -> Dict[str, Any]:
        """Start Station Max BLE remote listener, LED OSD, and optional DLNA renderer."""
        osd = self._get_osd()
        remote = self._get_remote()
        if remote.is_supported():
            remote.start()
        dlna = self._get_dlna()
        if dlna.enabled:
            dlna.start()
        self._get_multiroom()
        is_max_hw = bool(osd.is_supported() or remote.is_supported() or os.environ.get("PANEL_LOCAL") == "1")
        if is_max_hw and (os.environ.get("PANEL_LOCAL") == "1" or hasattr(self.panel, "adb")):
            threading.Thread(target=self._preinject_gst, daemon=True, name="gst-preinject").start()
        return {"osd": osd.status(), "remote": remote.status(), "dlna": dlna.status()}

    def stop_hw(self) -> None:
        osd = getattr(self.panel, "_extmusic_osd", None)
        if osd is not None:
            try:
                osd.stop()
            except Exception:  # noqa: BLE001
                pass
        remote = getattr(self.panel, "_extmusic_remote", None)
        if remote is not None:
            try:
                remote.stop()
            except Exception:  # noqa: BLE001
                pass
        dlna = getattr(self.panel, "_extmusic_dlna", None) or self._dlna
        if dlna is not None:
            try:
                dlna.stop()
            except Exception:  # noqa: BLE001
                pass

    def _emit_osd_track(self, track: Optional[Dict[str, Any]]) -> None:
        if not isinstance(track, dict):
            return
        try:
            osd = self._get_osd()
            if osd.is_supported() and osd.enabled:
                osd.show_track(str(track.get("title") or ""), str(track.get("artist") or ""))
        except Exception:  # noqa: BLE001 - OSD must never block playback
            pass

    def _emit_osd_like(self, liked: bool) -> None:
        try:
            osd = self._get_osd()
            if osd.is_supported() and osd.enabled:
                osd.show_like(liked=bool(liked))
        except Exception:  # noqa: BLE001
            pass

    def _emit_osd_icon(self, kind: str) -> None:
        try:
            osd = self._get_osd()
            if osd.is_supported() and osd.enabled:
                osd.show_icon(kind)
        except Exception:  # noqa: BLE001
            pass

    def hw_status(self) -> Dict[str, Any]:
        osd = self._get_osd()
        remote = self._get_remote()
        norm_fn = getattr(self.gst, "norm_status", None)
        norm = norm_fn() if callable(norm_fn) else {
            "enabled": self.store.normalize(),
            "loudness_db": None,
            "gain": None,
        }
        return {
            "ok": True,
            "supported": bool(osd.is_supported() or remote.is_supported()),
            "osd": osd.status(),
            "remote": remote.status(),
            "normalize": norm,
        }

    def hw_configure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ExtMusicError("Ожидается JSON-объект настроек")
        osd = self._get_osd()
        remote = self._get_remote()
        changed = False
        if "osd_enabled" in data and data["osd_enabled"] is not None:
            osd.set_enabled(bool(data["osd_enabled"]))
            changed = True
        if "remote_enabled" in data and data["remote_enabled"] is not None:
            remote.configure(enabled=bool(data["remote_enabled"]))
            if remote.enabled and remote.is_supported():
                remote.start()
            changed = True
        if "remote_dpad_hdmi" in data and data["remote_dpad_hdmi"] is not None:
            remote.configure(dpad_hdmi=bool(data["remote_dpad_hdmi"]))
            changed = True
        if "normalize_enabled" in data and data["normalize_enabled"] is not None:
            norm_on = bool(data["normalize_enabled"])
            self.store.save_normalize(norm_on)
            set_norm = getattr(self.gst, "set_normalize", None)
            if callable(set_norm):
                set_norm(norm_on)

        if changed:
            payload = {
                "osd_enabled": bool(osd.enabled),
                "remote_enabled": bool(remote.enabled),
                "remote_dpad_hdmi": bool(remote.dpad_hdmi),
            }
            try:
                import server

                updater = getattr(server, "update_settings_section", None)
                if callable(updater):
                    updater("extmusic_hw", payload)
            except Exception:  # noqa: BLE001
                pass

        test_osd = str(data.get("test_osd") or "").strip().lower()
        if test_osd:
            if not osd.is_supported():
                raise ExtMusicError("Экран 25×16 доступен только на Станции Макс")
            if test_osd in ("heart", "like"):
                osd.show_like(liked=True, force=True)
            elif test_osd in ("track", "title"):
                cur = self.current or {}
                title = str(cur.get("title") or "YouTube Music")
                artist = str(cur.get("artist") or "YapAnel")
                osd.show_track(title, artist, force=True)
            elif test_osd in ("play", "pause", "dislike", "mix"):
                osd.show_icon(test_osd, force=True)
            else:
                raise ExtMusicError(f"Неизвестный тест экрана: {test_osd}")

        return self.hw_status()

    # ---------- credentials ----------
    def _vk(self) -> VKClient:
        creds = self.store.vk()
        if not creds.get("token"):
            raise ExtMusicError("VK не подключён: войдите в VK или вставьте токен")
        return VKClient(creds.get("token", ""), creds.get("user_agent", ""))

    def auth(self, data: Dict[str, Any]) -> Dict[str, Any]:
        token = str(data.get("token") or data.get("access_token") or "").strip()
        user_agent = str(data.get("user_agent") or data.get("userAgent") or "").strip()
        config = data.get("config")
        if isinstance(config, str) and config.strip():
            import json

            try:
                parsed = json.loads(config)
            except ValueError as error:
                raise ExtMusicError("Конфиг должен быть JSON") from error
            if isinstance(parsed, dict):
                token = token or str(parsed.get("token") or parsed.get("access_token") or "").strip()
                user_agent = user_agent or str(parsed.get("user_agent") or parsed.get("userAgent") or "").strip()
        if not token:
            raise ExtMusicError("Вставьте токен VK")
        client = VKClient(token, user_agent)
        info = client.validate()
        self.store.save_vk(token, client.user_agent, info.get("user_id", ""), info.get("name", ""))
        self.error = ""
        return self.status()

    def logout(self) -> Dict[str, Any]:
        self.stop()
        self.store.clear_vk()
        return self.status()

    # ---------- interactive login (official VK client) ----------
    def _login(self) -> VKDirectAuth:
        if self._vk_auth is None:
            self._vk_auth = VKDirectAuth()
        return self._vk_auth

    def login_start(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self._login().start(
                str(data.get("login") or ""),
                str(data.get("password") or ""),
            )
        except VKAuthError as error:
            raise ExtMusicError(str(error)) from error
        return self._complete_login(result)

    def login_code(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self._login().submit_code(
                str(data.get("session") or ""),
                str(data.get("code") or ""),
            )
        except VKAuthError as error:
            raise ExtMusicError(str(error)) from error
        return self._complete_login(result)

    def login_captcha(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self._login().submit_captcha(
                str(data.get("session") or ""),
                str(data.get("value") or ""),
            )
        except VKAuthError as error:
            raise ExtMusicError(str(error)) from error
        return self._complete_login(result)

    def login_cancel(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self._login().cancel(str(data.get("session") or ""))
        return {"step": "cancelled"}

    def _complete_login(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("step") != "done":
            return result
        token = str(result.pop("token", "") or "")
        try:
            status = self.auth({"token": token, "user_agent": VKDirectAuth.USER_AGENT})
        except VKError as error:
            raise ExtMusicError(str(error)) from error
        return {"step": "done", "status": status}

    # ---------- source / status ----------
    def source(self) -> str:
        return self.store.source()

    def set_source(self, source: str) -> Dict[str, Any]:
        source = str(source or "").strip().lower()
        if source not in ("vk", "yt"):
            raise ExtMusicError("Неизвестный источник музыки")
        if self.current:
            self.stop()
        else:
            # Do not wake the station-side player just to switch an empty tab.
            with self.lock:
                self.queue = []
                self._orig_queue = []
                self.index = -1
                self.error = ""
                self.skipped = []
        self.store.save_source(source)
        return self.status()

    def status(self) -> Dict[str, Any]:
        self._warm_native()
        if self._is_native_current():
            self._native_queue_sync()
        with self.lock:
            current = dict(self.current) if isinstance(self.current, dict) else self.current
            queue = [dict(t) if isinstance(t, dict) else t for t in self.queue]
            index = self.index
            error = self.error
            skipped = list(self.skipped[-5:])
            shuffle = bool(self.shuffle)
            repeat = self.repeat
            autoplay_radio = bool(self.autoplay_radio)
            is_native = self._is_native_current()
            is_gst = self._is_gst_current()
        source = self.store.source()
        if source not in ("vk", "yt"):
            source = "yt"
        creds = self.store.vk()
        vk_ready = bool(creds.get("token"))
        gst_ready = self.gst.available()
        native_ready = self.native.available()
        yt_ready = gst_ready or native_ready or ytdlp.available()
        yt_account_info = self.yt_account.account()
        yt_authenticated = bool(yt_account_info.get("configured"))
        yt_user = yt_account_info.get("account") or ("YouTube Music" if yt_ready else "")
        state: Dict[str, Any] = {
            "source": source,
            "configured": vk_ready if source == "vk" else yt_ready,
            "account": (creds.get("name") or "") if source == "vk" else yt_user,
            "user_id": creds.get("user_id") or "",
            "sources": {
                "vk": {"configured": vk_ready, "account": creds.get("name") or ""},
                "yt": {
                    "configured": yt_ready,
                    "account": yt_user,
                    "authenticated": yt_authenticated,
                    "auth_type": yt_account_info.get("type") or "",
                    "gst": gst_ready,
                    "native": native_ready,
                    "cookies": self.store.has_auth_cookies(),
                    "oauth": self.store.has_oauth(),
                },
            },
            "current": current,
            "queue": queue,
            "index": index,
            "shuffle": shuffle,
            "repeat": repeat,
            "autoplay_radio": autoplay_radio,
            "native": is_native,
            "error": error,
            "skipped": skipped,
            "player": {},
        }
        if current:
            if is_gst:
                state["player"] = self._gst_player(current)
            elif is_native:
                try:
                    state["player"] = self._native_player(current)
                except Exception as error:  # noqa: BLE001 - helper is optional
                    state["player"] = {"ok": False, "native": True, "state": "error", "error": str(error)}
            else:
                try:
                    state["player"] = self.player.status()
                except Exception as error:  # noqa: BLE001 - helper is optional
                    state["player"] = {"ok": False, "state": "error", "error": str(error)}
        return state

    # ---------- native YouTube Music ----------
    def _warm_native(self) -> None:
        if self._native_warm:
            return
        self._native_warm = True
        threading.Thread(target=self.native.warmup, daemon=True, name="native-yt-warmup").start()

    def _is_native_current(self) -> bool:
        return bool(self.current and self.current.get("native"))

    def _is_gst_current(self) -> bool:
        return bool(self.current and self.current.get("gst"))

    def _gst_player(self, current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = self.gst.player()
        cur = current if current is not None else self.current
        if cur and cur.get("duration"):
            p["duration"] = int(cur["duration"]) * 1000
        return p

    def _resolve_gst(self, track: Dict[str, Any]) -> Dict[str, Any]:
        video_id = str(track.get("id") or "")
        info = self._prefetch_get(video_id)
        if info is None:
            cookies = self.store.yt_cookies() or None
            try:
                info = fast_yt.fast_resolve(video_id, cookies=cookies)
            except Exception:
                info = ytdlp.resolve(video_id, fast=True)
            self._prefetch_put(video_id, info)
        url = str(info.get("url") or "")
        if not url:
            raise ExtMusicError("Не удалось получить аудиопоток YouTube")
        clean_info = {
            k: v
            for k, v in info.items()
            if (v is not None and v != "") or k not in track
        }
        return {**track, **clean_info, "url": url, "gst": True}

    def _native_player(self, current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cur = current if current is not None else (self.current or {})
        native = self.native.status()
        duration = int(native.get("duration") or 0)
        if not duration:
            duration = int(cur.get("duration") or 0) * 1000
        position = int(native.get("position") or 0)
        if duration:
            position = min(position, duration)
        state = str(native.get("state") or "none")
        title = str(native.get("title") or "")
        title_matches = not title or self._same_text(title, cur.get("title"))
        loading = bool(native.get("loading")) or time.time() < self._native_force_loading_until
        if time.time() < self._native_loading_until and not (title_matches and state in ("playing", "paused")):
            loading = True
            # While the app is still switching, the session may describe the
            # previous track; show the requested track and its known length.
            duration = int(cur.get("duration") or 0) * 1000 or duration
            position = 0
        if loading:
            state = "loading"
        elif state in ("stopped", "none"):
            state = "idle"
        return {
            "ok": True,
            "native": True,
            "state": state,
            "position": max(0, position),
            "duration": max(0, duration),
            "playing": bool(native.get("playing")),
            "error": "",
        }

    @staticmethod
    def _same_text(left: Any, right: Any) -> bool:
        def norm(value):
            text = str(value or "").casefold().replace("ё", "е")
            return re.sub(r"[^\w]+", " ", text).strip()

        a, b = norm(left), norm(right)
        if not a or not b:
            return False
        return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))

    def _native_sync(self, native: Dict[str, Any]) -> None:
        title = str(native.get("title") or "").strip()
        if not title or title == "null" or self._same_text(title, (self.current or {}).get("title")):
            return
        artist = str(native.get("artist") or "").strip()
        duration_ms = int(native.get("duration") or 0)
        match = None
        for index, track in enumerate(self.queue):
            if self._same_text(track.get("title"), title) and (
                not artist or not track.get("artist") or self._same_text(track.get("artist"), artist)
            ):
                match = (index, track)
                break
        if match:
            index, track = match
            self.index = index
            self.current = {**track, "native": True}
            return
        key = f"{title}|{artist}".encode("utf-8")
        self.current = {
            "source": "yt",
            "id": "n" + hashlib.sha1(key).hexdigest()[:16],
            "title": title[:300],
            "artist": (artist or "YouTube Music")[:300],
            "album": str(native.get("album") or "")[:300],
            "duration": round(duration_ms / 1000) if duration_ms else 0,
            "thumb": "",
            "url": "",
            "native": True,
            "synthetic": True,
        }

    def _native_queue_sync(self) -> Dict[str, Any]:
        """The native app owns the queue, like Glagol owns Yandex Music."""
        try:
            data = self.native.queue()
        except Exception:  # noqa: BLE001 - queue sync is best effort
            return {"active_id": -1, "items": []}
        items = data.get("items") or []
        if items:
            self.queue = items
            active = int(data.get("active_id") or -1)
            index = next(
                (i for i, item in enumerate(items) if int(item.get("queue_id") or -1) == active),
                -1,
            )
            if index < 0 and self.current:
                index = next(
                    (i for i, item in enumerate(items)
                     if self._same_text(item.get("title"), self.current.get("title"))),
                    -1,
                )
            if index >= 0:
                self.index = index
        elif self.queue and self.queue[0].get("native"):
            self.queue = []
            self.index = -1
        return data


    # ---------- YouTube account (cookies) ----------
    def yt_cookies_status(self) -> Dict[str, Any]:
        path = self.store.cookies_path()
        try:
            stat = path.stat()
        except OSError:
            stat = None
        authenticated = self.store.has_auth_cookies()
        return {
            "configured": authenticated,
            "authenticated": authenticated,
            "file": bool(stat and stat.st_size),
            "size": int(stat.st_size) if stat else 0,
            "updated": float(stat.st_mtime) if stat else 0.0,
        }

    def yt_cookies_save(self, text: str) -> Dict[str, Any]:
        try:
            self.store.save_cookies(text)
        except ValueError as error:
            raise ExtMusicError(str(error)) from error
        self._prefetch.clear()
        return self.yt_cookies_status()

    def yt_cookies_clear(self) -> Dict[str, Any]:
        self.store.clear_cookies()
        self.stop()
        return self.yt_cookies_status()

    def yt_library(self, limit: int = 100) -> Dict[str, Any]:
        if not self.store.has_auth_cookies():
            raise ExtMusicError(
                "Cookies без входа в YouTube. Импортируйте cookies заново после входа в аккаунт."
            )
        try:
            tracks = ytdlp.library(self._cookies(), limit=limit)
        except YTDLPError as error:
            raise ExtMusicError(str(error)) from error
        return {"tracks": tracks}

    # ---------- YouTube Music account library (app token) ----------
    def yt_account_info(self) -> Dict[str, Any]:
        try:
            return self.yt_account.account()
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error

    def yt_account_logout(self) -> Dict[str, Any]:
        try:
            res = self.yt_account.logout()
        except Exception as error:  # noqa: BLE001
            raise ExtMusicError(f"Ошибка выхода: {error}") from error
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            pass
        return res

    def yt_login_status(self) -> Dict[str, Any]:
        return self.yt_account.oauth_handler.poll()

    def yt_login_start(self, data: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(data.get("client_id") or data.get("clientId") or "").strip()
        client_secret = str(data.get("client_secret") or data.get("clientSecret") or "").strip()
        try:
            return self.yt_account.oauth_handler.start(client_id, client_secret)
        except GoogleOAuthError as error:
            raise ExtMusicError(str(error)) from error

    def yt_login_code(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.yt_account.oauth_handler.poll()

    def yt_login_cancel(self) -> Dict[str, Any]:
        return self.yt_account.oauth_handler.cancel()

    def yt_account_library(self) -> Dict[str, Any]:
        try:
            return self.yt_account.library()
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error

    def yt_account_liked(self, count: int = 100) -> Dict[str, Any]:
        try:
            tracks = self.yt_account.liked(count)
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error
        self._cache_tracks(tracks)
        return {"tracks": tracks}

    def yt_account_playlist(self, browse_id: str, count: int = 100) -> Dict[str, Any]:
        try:
            tracks = self.yt_account.playlist(browse_id, count)
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error
        self._cache_tracks(tracks)
        return {"tracks": tracks}

    def yt_account_home(self, count: int = 30) -> Dict[str, Any]:
        try:
            res = self.yt_account.home(count)
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error
        if isinstance(res, dict):
            self._cache_tracks(res.get("tracks"))
            self._cache_tracks(res.get("quick_picks"))
            self._cache_tracks(res.get("feed"))
        return res

    def yt_account_mood(self, token: str, count: int = 100) -> Dict[str, Any]:
        try:
            res = self.yt_account.mood(token, count)
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error
        if isinstance(res, dict):
            self._cache_tracks(res.get("tracks"))
        return res

    # ---------- YouTube Music public catalog, cache, likes & lyrics ----------
    @staticmethod
    def _strip_ext_yt(value: Any) -> str:
        text = str(value or "").strip()
        if text.startswith("ext:yt:"):
            return text[7:].strip()
        return text

    def _normalize_yt_cache_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "").strip().lower()
        if kind in ("album", "artist", "playlist", "liked"):
            return None
        if item.get("browse_id") and not item.get("video_id") and item.get("id") == item.get("browse_id"):
            return None
        if str(item.get("source") or "yt") != "yt" and not item.get("video_id"):
            return None
        norm = self._queue_track({**item, "source": "yt"})
        if not norm or norm.get("source") != "yt":
            return None
        norm.pop("url", None)
        return norm

    def _cache_tracks(self, items: Any) -> None:
        if not isinstance(items, (list, tuple)):
            return
        normalized: List[Dict[str, Any]] = []
        for raw in items:
            norm = self._normalize_yt_cache_item(raw)
            if norm:
                normalized.append(norm)
        if not normalized:
            return
        with self._known_lock:
            for norm in normalized:
                vid = norm["id"]
                existing = self._known_tracks.get(vid)
                if existing:
                    merged = {**existing}
                    for k, v in norm.items():
                        if v:
                            merged[k] = v
                    self._known_tracks[vid] = merged
                    self._known_tracks.move_to_end(vid)
                else:
                    self._known_tracks[vid] = norm
            while len(self._known_tracks) > _KNOWN_TRACKS_LIMIT:
                self._known_tracks.popitem(last=False)

    def yt_resolve_known_track(self, video_id: str) -> Optional[Dict[str, Any]]:
        vid = self._strip_ext_yt(video_id)[:40]
        if not vid:
            return None
        with self.lock:
            candidates: List[Any] = []
            if self.current is not None:
                candidates.append(self.current)
            candidates.extend(self.queue)
            candidates.extend(self._orig_queue)
            for t in candidates:
                if isinstance(t, dict) and str(t.get("source") or "") == "yt" and str(t.get("id") or "") == vid:
                    return dict(t)
        with self._known_lock:
            cached = self._known_tracks.get(vid)
            if cached:
                self._known_tracks.move_to_end(vid)
                return dict(cached)
        for rec in self.store.recent_yt():
            if str(rec.get("id") or "") == vid:
                self._cache_tracks([rec])
                return dict(rec)
        return None

    def _ensure_artist_cache_loaded(self) -> None:
        if self._artist_cache_loaded:
            return
        loader = getattr(self.store, "artist_cache", None)
        persisted: Dict[str, str] = {}
        if callable(loader):
            try:
                persisted = loader() or {}
            except (OSError, ValueError):
                persisted = {}
        with self._known_lock:
            if not self._artist_cache_loaded:
                exp = time.time() + _ARTIST_CACHE_TTL
                for k, v in persisted.items():
                    if k and v and k not in self._artist_cache:
                        self._artist_cache[k] = (exp, v)
                while len(self._artist_cache) > _KNOWN_TRACKS_LIMIT:
                    self._artist_cache.popitem(last=False)
                self._artist_cache_loaded = True

    def _artist_cache_get(self, keys: List[str]) -> Optional[str]:
        self._ensure_artist_cache_loaded()
        now = time.time()
        with self._known_lock:
            for key in keys:
                entry = self._artist_cache.get(key)
                if not entry:
                    continue
                exp, val = entry
                if exp > now:
                    self._artist_cache.move_to_end(key)
                    return val
                self._artist_cache.pop(key, None)
        return None

    def _artist_cache_set(self, keys: List[str], value: str) -> None:
        if not keys:
            return
        self._ensure_artist_cache_loaded()
        ttl = _ARTIST_CACHE_TTL if value else _ARTIST_NEGATIVE_TTL
        exp = time.time() + ttl
        with self._known_lock:
            for key in keys:
                self._artist_cache[key] = (exp, value)
                self._artist_cache.move_to_end(key)
            while len(self._artist_cache) > _KNOWN_TRACKS_LIMIT:
                self._artist_cache.popitem(last=False)
        if value and str(value).startswith("UC"):
            saver = getattr(self.store, "save_artist_entry", None)
            if callable(saver):
                for key in keys:
                    if key.startswith("art:"):
                        try:
                            saver(key, str(value))
                        except (OSError, ValueError):
                            pass

    def _backfill_yt_artist(self, video_id: str, artist_id: str, meta: Optional[Dict[str, str]] = None) -> None:
        if not video_id or not artist_id:
            return
        clean_artist = str((meta or {}).get("artist") or "").strip()
        clean_album = str((meta or {}).get("album") or "").strip()
        album_id = str((meta or {}).get("album_id") or "").strip()
        with self._known_lock:
            cached = self._known_tracks.get(video_id)
            if cached is not None:
                cached["artist_id"] = artist_id
                if clean_artist and (not cached.get("artist") or cached.get("artist") == "YouTube Music"):
                    cached["artist"] = clean_artist
                if clean_album and not cached.get("album"):
                    cached["album"] = clean_album
                if album_id and not cached.get("album_id"):
                    cached["album_id"] = album_id
        with self.lock:
            targets: List[Any] = [self.current, *self.queue, *self._orig_queue]
            for tr in targets:
                if (
                    isinstance(tr, dict)
                    and str(tr.get("source") or "") == "yt"
                    and str(tr.get("id") or "") == video_id
                ):
                    if not tr.get("artist_id"):
                        tr["artist_id"] = artist_id
                    if clean_artist and (not tr.get("artist") or tr.get("artist") == "YouTube Music"):
                        tr["artist"] = clean_artist
                    if clean_album and not tr.get("album"):
                        tr["album"] = clean_album
                    if album_id and not tr.get("album_id"):
                        tr["album_id"] = album_id

    def yt_resolve_artist_id(self, video_id: str = "", artist: str = "", title: str = "") -> str:
        """Resolve a YouTube Music artist browse_id (`UC...`) for a track or artist name."""
        vid = self._strip_ext_yt(video_id)[:40]
        art_text = str(artist or "").strip()
        title_text = str(title or "").strip()

        if vid:
            known = self.yt_resolve_known_track(vid)
            if known:
                known_aid = self._strip_ext_yt(known.get("artist_id") or "")
                if known_aid:
                    return known_aid
                if not art_text or art_text == "YouTube Music":
                    art_text = str(known.get("artist") or "").strip()
                if not title_text:
                    title_text = str(known.get("title") or "").strip()

        cache_keys: List[str] = []
        if vid:
            cache_keys.append(f"vid:{vid}")
        art_norm = re.sub(r"\s+", " ", art_text.casefold()).strip()
        title_norm = re.sub(r"\s+", " ", title_text.casefold()).strip()
        if art_norm and art_norm not in ("youtube music", "youtube", "various artists"):
            cache_keys.append(f"art:{art_norm}")
        elif title_norm:
            cache_keys.append(f"title:{title_norm}")

        cached_aid = self._artist_cache_get(cache_keys)
        if cached_aid is not None:
            if cached_aid and vid:
                self._backfill_yt_artist(vid, cached_aid)
            return cached_aid

        meta: Dict[str, str] = {}
        aid = ""
        if vid and _YT_VIDEO_ID_RE.fullmatch(vid) and hasattr(self.ytmusic, "track_artist_info"):
            try:
                meta = self.ytmusic.track_artist_info(vid) or {}
                aid = self._strip_ext_yt(meta.get("artist_id") or "")
                if meta.get("artist") and (not art_text or art_text == "YouTube Music"):
                    art_text = str(meta["artist"]).strip()
            except (YTMusicError, OSError, ValueError):
                meta = {}

        if not aid and hasattr(self.ytmusic, "find_artist_id"):
            try:
                found = self.ytmusic.find_artist_id(artist=art_text, title=title_text) or {}
                aid = self._strip_ext_yt(found.get("artist_id") or "")
                if aid and not meta.get("artist") and found.get("artist"):
                    meta["artist"] = str(found["artist"])
            except (YTMusicError, OSError, ValueError):
                aid = ""

        if aid:
            final_art_norm = re.sub(r"\s+", " ", art_text.casefold()).strip()
            if final_art_norm and final_art_norm not in ("youtube music", "youtube", "various artists"):
                k = f"art:{final_art_norm}"
                if k not in cache_keys:
                    cache_keys.append(k)
            self._artist_cache_set(cache_keys, aid)
            if vid:
                self._backfill_yt_artist(vid, aid, meta=meta)
            return aid

        self._artist_cache_set(cache_keys, "")
        return ""

    def yt_home(self, limit: int = 30, count: Optional[int] = None) -> Dict[str, Any]:
        eff = count if count is not None else limit
        recent = self.store.recent_yt()
        self._cache_tracks(recent)
        account_error = ""
        if self.yt_account.is_configured():
            try:
                data = self.yt_account.home(limit=eff)
                if isinstance(data, dict):
                    tracks = data.get("tracks") or []
                    quick_picks = data.get("quick_picks") or tracks
                    feed = data.get("feed") or []
                    shelves = data.get("shelves") or []
                    self._cache_tracks(tracks)
                    self._cache_tracks(quick_picks)
                    self._cache_tracks(feed)
                    for shelf in shelves:
                        if isinstance(shelf, dict):
                            self._cache_tracks(shelf.get("items"))
                    return {
                        "recent": recent,
                        "tracks": tracks,
                        "quick_picks": quick_picks,
                        "shelves": shelves,
                        "mixes": data.get("mixes") or [],
                        "feed": feed,
                        "moods": data.get("moods") or [],
                        "authenticated": True,
                    }
            except YTAccountError as error:
                account_error = str(error)
        try:
            pub = self.ytmusic.home(limit=eff)
        except YTMusicError as error:
            raise ExtMusicError(str(error)) from error
        tracks = pub.get("tracks") or [] if isinstance(pub, dict) else []
        shelves = pub.get("shelves") or [] if isinstance(pub, dict) else []
        moods = pub.get("moods") or [] if isinstance(pub, dict) else []
        mixes = [
            item
            for shelf in shelves
            if isinstance(shelf, dict)
            for item in (shelf.get("items") or [])
            if isinstance(item, dict) and item.get("kind") in ("album", "playlist")
        ][:12]
        self._cache_tracks(tracks)
        for shelf in shelves:
            if isinstance(shelf, dict):
                self._cache_tracks(shelf.get("items"))
        result: Dict[str, Any] = {
            "recent": recent,
            "tracks": tracks,
            "quick_picks": tracks,
            "shelves": shelves,
            "mixes": mixes,
            "feed": [],
            "moods": moods,
            "authenticated": False,
        }
        if account_error:
            result["account_error"] = account_error
        return result

    def yt_explore(self) -> Dict[str, Any]:
        try:
            data = self.ytmusic.explore()
        except YTMusicError as error:
            raise ExtMusicError(str(error)) from error
        if isinstance(data, dict):
            self._cache_tracks(data.get("tracks"))
        return data

    def yt_charts(self, country: str = "RU") -> Dict[str, Any]:
        try:
            data = self.ytmusic.charts(country=country)
        except YTMusicError as error:
            raise ExtMusicError(str(error)) from error
        if isinstance(data, dict):
            self._cache_tracks(data.get("tracks"))
            self._cache_tracks(data.get("videos"))
        return data

    def yt_mood(self, browse_id: str, params: str = "", continuation: str = "", limit: int = 60) -> Dict[str, Any]:
        try:
            data = self.ytmusic.browse_mood(browse_id, params=params, continuation=continuation, limit=limit)
        except YTMusicError as error:
            raise ExtMusicError(str(error)) from error
        if isinstance(data, dict):
            self._cache_tracks(data.get("tracks"))
        return data

    def yt_search(self, query: str, count: int = 30, kind: str = "songs") -> Dict[str, Any]:
        norm_kind = str(kind or "songs").strip().lower() or "songs"
        res = self.search(query, count=count, source="yt", kind=norm_kind)
        items = res.get("tracks") or []
        return {"source": "yt", "kind": norm_kind, "tracks": items, "items": items}

    def yt_collection(self, browse_id: str, params: str = "", limit: int = 100) -> Dict[str, Any]:
        bid = self._strip_ext_yt(browse_id)
        try:
            col = self.yt_account.collection(bid, params=params, limit=limit)
        except (YTAccountError, YTMusicError) as error:
            raise ExtMusicError(str(error)) from error
        if isinstance(col, dict):
            self._cache_tracks(col.get("tracks"))
        return col

    def yt_artist(self, browse_id: str) -> Dict[str, Any]:
        bid = self._strip_ext_yt(browse_id)
        col = self.yt_collection(bid, limit=100)
        if isinstance(col, dict):
            self._cache_tracks(col.get("tracks"))
        return col

    def yt_similar(self, video_id: str) -> List[Dict[str, Any]]:
        vid = self._strip_ext_yt(video_id)
        if not vid:
            return []
        try:
            tracks = self.ytmusic.radio(vid, "", 30)
        except YTMusicError as error:
            raise ExtMusicError(str(error)) from error
        filtered = [t for t in (tracks or []) if isinstance(t, dict) and str(t.get("id") or "") != vid]
        self._cache_tracks(filtered)
        return filtered

    def yt_like(self, video_id: str, on: bool = True) -> Dict[str, Any]:
        vid = self._strip_ext_yt(video_id)
        try:
            res = self.yt_account.like(vid, on=bool(on))
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error
        self._emit_osd_like(bool(on))
        return res

    def yt_dislike(self, video_id: str, skip: bool = True) -> Dict[str, Any]:
        vid = self._strip_ext_yt(video_id)
        try:
            res = self.yt_account.dislike(vid)
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error
        self._emit_osd_icon("dislike")
        if skip:
            matches_current = False
            with self.lock:
                if (
                    self.current
                    and str(self.current.get("source") or "") == "yt"
                    and str(self.current.get("id") or "") == vid
                ):
                    matches_current = True
            if matches_current:
                self.next()
        return res

    def yt_track_liked(self, video_id: str) -> bool:
        vid = self._strip_ext_yt(video_id)
        if not vid:
            return False
        try:
            return bool(self.yt_account.is_liked(vid))
        except YTAccountError:
            return False

    def yt_playlist_create(
        self,
        title: str,
        video_ids: Optional[List[str]] = None,
        description: str = "",
        privacy: str = "PRIVATE",
        from_queue: bool = False,
    ) -> Dict[str, Any]:
        vids = list(video_ids) if video_ids else []
        if from_queue and not vids:
            with self.lock:
                vids = [
                    str(t.get("id") or "")
                    for t in self.queue
                    if isinstance(t, dict) and str(t.get("source") or "") == "yt" and t.get("id")
                ]
            if not vids:
                raise ExtMusicError("Очередь YouTube Music пуста")
        try:
            return self.yt_account.create_playlist(
                title=title,
                video_ids=vids,
                description=description,
                privacy=privacy,
            )
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error

    def yt_playlist_add(self, playlist_id: str, video_ids: Any) -> Dict[str, Any]:
        try:
            return self.yt_account.add_to_playlist(playlist_id, video_ids)
        except YTAccountError as error:
            raise ExtMusicError(str(error)) from error

    def yt_lyrics(
        self,
        video_id: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration: float = 0,
    ) -> Dict[str, Any]:
        vid = self._strip_ext_yt(video_id)
        if vid and (not title or not artist):
            known = self.yt_resolve_known_track(vid)
            if known:
                title = title or str(known.get("title") or "")
                artist = artist or str(known.get("artist") or "")
                album = album or str(known.get("album") or "")
                duration = duration or float(known.get("duration") or 0)
        yt_plain: Optional[Dict[str, Any]] = None
        if vid:
            try:
                ly = self.ytmusic.lyrics(vid)
                if isinstance(ly, dict):
                    if ly.get("sync") and ly.get("text"):
                        return ly
                    if ly.get("text"):
                        yt_plain = ly
            except (YTMusicError, Exception):  # noqa: BLE001 - fallback to LRCLIB
                pass
        ym = getattr(self.panel, "ymusic", None)
        if ym is not None and hasattr(ym, "lyrics_by_name"):
            clean_title, clean_artist = self.ytmusic.clean_track_meta(title, artist)
            if clean_title and clean_artist:
                try:
                    res = ym.lyrics_by_name(clean_title, clean_artist, album=album, duration=duration)
                    if isinstance(res, dict):
                        if res.get("sync") and res.get("text"):
                            return res
                        if yt_plain is not None:
                            return yt_plain
                        return res
                except Exception:  # noqa: BLE001
                    pass
        if yt_plain is not None:
            return yt_plain
        return {"sync": None, "text": None, "source": None}

    def fetch_thumb(self, url: str) -> Tuple[bytes, str]:
        raw_url = str(url or "").strip()
        cur = self.current if isinstance(self.current, dict) else None
        if cur and str(cur.get("source") or "") == "dlna" and raw_url and raw_url == str(cur.get("thumb") or "").strip():
            from .dlna import validate_stream_url

            clean = validate_stream_url(raw_url, allow_lan=True)
            req = urllib.request.Request(clean, headers={"User-Agent": "YapAnel-DLNA/1.0"})
            try:
                with ipv4_urlopen(req, timeout=8) as resp:
                    ctype = str((resp.headers or {}).get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    data = resp.read(2 * 1024 * 1024 + 1)
            except Exception as err:  # noqa: BLE001
                raise ExtMusicError(f"Не удалось загрузить обложку DLNA: {err}") from err
            if len(data) > 2 * 1024 * 1024:
                raise ExtMusicError("Обложка больше 2 МБ")
            if not ctype.startswith("image/") or ctype.startswith("image/svg"):
                raise ExtMusicError("Ответ не является безопасным изображением")
            return data, ctype
        try:
            return self.ytmusic.fetch_thumb(raw_url)
        except YTMusicError as error:
            raise ExtMusicError(str(error)) from error

    # ---------- station screen (Google sign-in) ----------
    def screen_frame(self) -> Dict[str, Any]:
        try:
            data = self.screen.frame()
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {
            "ok": True,
            "width": self.screen.width,
            "height": self.screen.height,
            "image": base64.b64encode(data).decode("ascii"),
        }

    def screen_start(self) -> Dict[str, Any]:
        try:
            self.screen.start_login()
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    def screen_library(self) -> Dict[str, Any]:
        try:
            self.screen.start_library()
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    def screen_home(self) -> Dict[str, Any]:
        try:
            self.screen.start_home()
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    def screen_tap(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.screen.tap(data.get("x"), data.get("y"))
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    def screen_text(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.screen.text(data.get("text"))
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    def screen_key(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.screen.key(data.get("key"))
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    def screen_clear(self) -> Dict[str, Any]:
        try:
            self.screen.clear()
        except ScreenError as error:
            raise ExtMusicError(str(error)) from error
        return {"ok": True}

    # ---------- search ----------
    def search(
        self, query: str, count: int = 30, source: str = "", kind: str = "songs"
    ) -> Dict[str, Any]:
        source = (source or self.store.source()).strip().lower()
        if source == "yt":
            norm_kind = str(kind or "songs").strip().lower() or "songs"
            try:
                if norm_kind in ("songs", "song", "tracks", "track"):
                    tracks = self.ytmusic.search(query, count=count)
                else:
                    tracks = self.ytmusic.search(query, count=count, kind=norm_kind)
            except YTMusicError as error:
                raise ExtMusicError(str(error)) from error
            self._cache_tracks(tracks)
            if tracks and norm_kind in ("songs", "song", "tracks", "track", "videos", "video"):
                # The first result is what the voice command and most clicks use.
                self._prefetch_track(tracks[0])
            res: Dict[str, Any] = {"source": "yt", "tracks": tracks}
            if norm_kind not in ("songs", "song", "tracks", "track"):
                res["kind"] = norm_kind
            return res
        if source != "vk":
            raise ExtMusicError("Неизвестный источник музыки")
        try:
            tracks = self._vk().search(query, count=count)
        except VKError as error:
            raise ExtMusicError(str(error)) from error
        return {"source": "vk", "tracks": tracks}

    # ---------- playback ----------
    def _queue_track(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "").strip().lower()
        if kind in ("album", "artist", "playlist", "liked"):
            return None
        if str(item.get("source") or "") == "dlna":
            stream_url = str(item.get("url") or "").strip()[:4000]
            if not stream_url:
                return None
            try:
                duration = max(0, int(item.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
            tid = str(item.get("id") or ("dlna_" + hashlib.sha1(stream_url.encode("utf-8")).hexdigest()[:12]))[:60]
            track = {
                "source": "dlna",
                "id": tid,
                "url": stream_url,
                "title": str(item.get("title") or "DLNA Поток")[:300],
                "artist": str(item.get("artist") or "DLNA / Поток")[:300],
                "album": str(item.get("album") or "")[:300],
                "duration": duration,
                "thumb": str(item.get("thumb") or "")[:1000],
            }
            if isinstance(item.get("fallback_urls"), list):
                track["fallback_urls"] = [str(u) for u in item["fallback_urls"] if u][:4]
            return track
        if str(item.get("source") or "") == "yt" or item.get("video_id"):
            try:
                duration = max(0, int(item.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
            try:
                queue_id = int(item.get("queue_id") or 0)
            except (TypeError, ValueError):
                queue_id = 0
            track = {
                "source": "yt",
                "id": str(item.get("id") or item.get("video_id") or "")[:40],
                "title": str(item.get("title") or "Без названия")[:300],
                "artist": str(item.get("artist") or "YouTube Music")[:300],
                "album": str(item.get("album") or "")[:300],
                "duration": duration,
                "thumb": str(item.get("thumb") or "")[:1000],
                "url": str(item.get("url") or "")[:4000],
                "playlist_id": str(item.get("playlist_id") or "")[:200],
                "queue_id": queue_id,
            }
            artist_id = str(item.get("artist_id") or "")[:200]
            album_id = str(item.get("album_id") or "")[:200]
            if artist_id:
                track["artist_id"] = artist_id
            if album_id:
                track["album_id"] = album_id
            return track if track["id"] else None
        try:
            return track_from_item(item)
        except (VKError, TypeError, ValueError):
            return None

    @staticmethod
    def _track_key(track: Dict[str, Any]):
        return (str(track.get("source") or "vk"), str(track.get("owner_id") or ""), str(track.get("id") or ""))

    def _remember_queue(self, tracks: Any, current: Dict[str, Any]) -> None:
        queue: List[Dict[str, Any]] = []
        seen = set()
        for item in tracks if isinstance(tracks, list) else []:
            track = self._queue_track(item)
            if not track:
                continue
            key = self._track_key(track)
            if key in seen:
                continue
            seen.add(key)
            queue.append(track)
        key = self._track_key(current)
        if key not in seen:
            queue.insert(0, current)
        self.queue = queue
        self.index = next(
            (i for i, item in enumerate(queue) if self._track_key(item) == key),
            0,
        )

    def _cookies(self) -> str:
        try:
            return str(self.store.cookies_path()) if self.store.has_auth_cookies() else ""
        except Exception:  # noqa: BLE001 - cookies are an optional optimization
            return ""

    # ---------- prefetch: resolve the next track while the current one plays ----------
    def _prefetch_get(self, video_id: str) -> Optional[Dict[str, Any]]:
        with self._prefetch_lock:
            item = self._prefetch.get(video_id)
            if not item:
                return None
            if item.get("at", 0) + self._prefetch_ttl < time.time():
                self._prefetch.pop(video_id, None)
                return None
            return dict(item)

    def _prefetch_put(self, video_id: str, info: Dict[str, Any]) -> None:
        with self._prefetch_lock:
            self._prefetch[video_id] = {**info, "at": time.time()}
            while len(self._prefetch) > 6:
                oldest = min(self._prefetch, key=lambda key: self._prefetch[key].get("at", 0))
                self._prefetch.pop(oldest, None)

    def _prefetch_one(self, video_id: str) -> None:
        try:
            if self._prefetch_get(video_id):
                return
            cookies = self.store.yt_cookies() or None
            try:
                info = fast_yt.fast_resolve(video_id, cookies=cookies)
            except Exception:
                info = ytdlp.resolve(video_id, timeout=60)
            self._prefetch_put(video_id, info)
        except Exception:  # noqa: BLE001 - prefetch is an optimization only
            pass
        finally:
            with self._prefetch_lock:
                self._prefetching.discard(video_id)

    def _prefetch_track(self, track: Dict[str, Any]) -> None:
        if str(track.get("source") or "") != "yt" or self.native.available() or not ytdlp.available():
            return
        video_id = str(track.get("id") or "")
        if not video_id:
            return
        with self._prefetch_lock:
            if video_id in self._prefetching:
                return
            self._prefetching.add(video_id)
        threading.Thread(target=self._prefetch_one, args=(video_id,), daemon=True, name="extmusic-prefetch").start()

    def _prefetch_after(self, queue: Any, current: Dict[str, Any]) -> None:
        if str(current.get("source") or "") != "yt" or self.native.available() or not ytdlp.available():
            return
        tracks = [track for track in (queue if isinstance(queue, list) else []) if isinstance(track, dict)]
        try:
            index = next(i for i, track in enumerate(tracks) if self._track_key(track) == self._track_key(current))
        except StopIteration:
            tracks = [current]
            index = 0
        # Two upcoming tracks: the next one is ready almost instantly, the one
        # after it is usually ready by the time the listener reaches it.
        for track in tracks[index + 1:index + 3]:
            self._prefetch_track(track)

    def _resolve(self, track: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        source = str(track.get("source") or self.store.source()).strip().lower()
        if source == "yt":
            if not ytdlp.available():
                raise ExtMusicError("yt-dlp не найден на станции — переустановите панель")
            video_id = str(track.get("id") or "")
            info = None if force else self._prefetch_get(video_id)
            if info is None and not force:
                # A search or the previous track may already be resolving this
                # id in the background; give it a short head start.
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    with self._prefetch_lock:
                        busy = video_id in self._prefetching
                    info = self._prefetch_get(video_id)
                    if info or not busy:
                        break
                    time.sleep(0.05)
            if info is None:
                # Playback deliberately does not send account cookies: with a
                # signed-in YouTube session yt-dlp gets SABR-only formats and
                # refuses direct audio URLs. Cookies are used for the library.
                info = ytdlp.resolve(video_id)
                self._prefetch_put(video_id, info)
            resolved = dict(track)
            resolved.update(
                {
                    "source": "yt",
                    "url": info["url"],
                    "duration": info.get("duration") or track.get("duration") or 0,
                    "ext": info.get("ext") or "",
                    "acodec": info.get("acodec") or "",
                    "abr": info.get("abr") or 0,
                }
            )
            return resolved
        if source != "vk":
            raise ExtMusicError("Неизвестный источник музыки")
        return self._vk().resolve(track, force=force)

    def _require_not_native(self) -> None:
        if self._is_native_current():
            raise ExtMusicError("Операция недоступна в нативном плеере YouTube Music")

    def _apply_shuffle_locked(self) -> None:
        if not self.queue:
            self.index = -1
            return
        if self.current is not None and 0 <= self.index < len(self.queue):
            cur_item = self.queue[self.index]
            cur_key = self._track_key(cur_item)
        elif self.current is not None:
            cur_key = self._track_key(self.current)
            cur_item = next((t for t in self.queue if self._track_key(t) == cur_key), None)
        else:
            cur_item = None
            cur_key = None

        if cur_item is not None:
            rest = [t for t in self.queue if self._track_key(t) != cur_key]
            if len(rest) >= 2:
                orig_rest_keys = [self._track_key(t) for t in rest]
                for _ in range(4):
                    random.shuffle(rest)
                    if [self._track_key(t) for t in rest] != orig_rest_keys:
                        break
                else:
                    rest.append(rest.pop(0))
            self.queue = [cur_item, *rest]
            self.index = 0
        else:
            shuffled = list(self.queue)
            if len(shuffled) >= 2:
                orig_keys = [self._track_key(t) for t in shuffled]
                for _ in range(4):
                    random.shuffle(shuffled)
                    if [self._track_key(t) for t in shuffled] != orig_keys:
                        break
                else:
                    shuffled.append(shuffled.pop(0))
            self.queue = shuffled
            self.index = -1

    def play_external_stream(
        self,
        track: Dict[str, Any],
        queue: Any = None,
        next_track: Optional[Dict[str, Any]] = None,
        multiroom_follower: bool = False,
    ) -> Dict[str, Any]:
        """Play a pre-resolved HTTP/LAN stream (DLNA or multiroom follower relay)."""
        if not isinstance(track, dict) or not str(track.get("url") or "").strip():
            raise ExtMusicError("Не указан URL аудиопотока")
        stream_url = str(track.get("url") or "").strip()
        fb_urls = [str(u) for u in (track.get("fallback_urls") or []) if u and str(u) != stream_url]
        dur_sec = max(0, int(track.get("duration") or 0))
        resolved: Dict[str, Any] = {
            "source": str(track.get("source") or "dlna"),
            "id": str(track.get("id") or ("dlna_" + hashlib.sha1(stream_url.encode("utf-8")).hexdigest()[:12])),
            "title": str(track.get("title") or "Внешний поток")[:300],
            "artist": str(track.get("artist") or "DLNA / Поток")[:300],
            "album": str(track.get("album") or "")[:300],
            "duration": dur_sec,
            "thumb": str(track.get("thumb") or "")[:1000],
            "url": stream_url,
            "playlist_id": str(track.get("playlist_id") or "")[:200],
        }
        if multiroom_follower:
            resolved["multiroom_follower"] = True

        with self.lock:
            self._gen += 1
            gen = self._gen
            self.error = ""
            self.skipped = []
            self._stop_yandex()
            was_gst = self._is_gst_current()
            started_gst = False
            if self.gst.available() or self.gst.ensure_injected():
                try:
                    self._stop_station_player()
                    try:
                        self.gst.play(
                            stream_url,
                            resolved["title"],
                            resolved["artist"],
                            duration=dur_sec,
                            fallback_urls=fb_urls,
                        )
                    except TypeError:
                        self.gst.play(
                            stream_url,
                            resolved["title"],
                            resolved["artist"],
                            duration=dur_sec,
                        )
                    resolved["gst"] = True
                    started_gst = True
                    self._native_loading_until = 0.0
                    self._native_force_loading_until = 0.0
                except GstBridgeError as err:
                    _log.warning("external stream gst failed: %s", err)
            if not started_gst:
                self._stop_gst_locked(was_gst)
                try:
                    self.player.play(stream_url)
                except (PlayerError, OSError) as err:
                    raise ExtMusicError(f"Не удалось воспроизвести поток: {err}") from err

            self.current = resolved
            self._ext_started_at = time.time()
            q_items = list(queue) if isinstance(queue, list) and queue else [resolved]
            if isinstance(next_track, dict) and next_track.get("url"):
                q_items = [resolved, next_track]
            self._remember_queue(q_items, resolved)
            self._orig_queue = list(self.queue)
            self._start_poll(gen)

        self._emit_osd_track(resolved)
        if not multiroom_follower:
            self._emit_multiroom_play(resolved)
        return self.status()

    def play(self, track: Dict[str, Any], queue: Any = None, multiroom_follower: bool = False) -> Dict[str, Any]:
        if not isinstance(track, dict):
            raise ExtMusicError("Не выбран трек")
        if multiroom_follower and str(track.get("url") or "").strip():
            return self.play_external_stream(track, queue=queue, multiroom_follower=True)
        if str(track.get("source") or "") == "dlna":
            return self.play_external_stream(track, queue=queue, multiroom_follower=multiroom_follower)
        normalized = self._queue_track(track)
        if not normalized:
            raise ExtMusicError("Не выбран трек")
        if normalized.get("source") == "yt":
            self._cache_tracks([normalized])
        with self.lock:
            self._gen += 1
            gen = self._gen
            self.error = ""
            self.skipped = []
            track_source = normalized["source"]
            key = self._track_key(normalized)
            existing_idx = next(
                (i for i, item in enumerate(self.queue) if self._track_key(item) == key),
                -1,
            )
            same_queue = (
                not self._is_native_current()
                and bool(self.queue)
                and existing_idx >= 0
                and (queue is None or queue is self.queue)
            )
            if track_source in ("vk", "yt") and track_source != self.store.source():
                self.store.save_source(track_source)
            self._stop_yandex()
            was_gst = self._is_gst_current()
            resolved: Optional[Dict[str, Any]] = None
            if track_source == "yt":
                if not self.gst.available():
                    self.gst.ensure_injected()
                if self.gst.available():
                    try:
                        self._stop_station_player()
                        resolved = self._resolve_gst(normalized)
                        fb_urls = list(resolved.pop("fallback_urls", None) or [])
                        dur_sec = int(resolved.get("duration") or normalized.get("duration") or 0)
                        try:
                            self.gst.play(
                                resolved["url"],
                                str(normalized.get("title") or ""),
                                str(normalized.get("artist") or ""),
                                duration=dur_sec,
                                fallback_urls=fb_urls,
                                loudness_db=resolved.get("loudness_db"),
                            )
                        except TypeError:
                            self.gst.play(
                                resolved["url"],
                                str(normalized.get("title") or ""),
                                str(normalized.get("artist") or ""),
                                duration=dur_sec,
                            )
                        resolved["duration"] = dur_sec
                        self._native_loading_until = 0.0
                        self._native_force_loading_until = 0.0
                    except (GstBridgeError, YTDLPError, FastYTError, ExtMusicError) as error:
                        _log.warning("native gst failed: %s", error)
                        resolved = None
            if resolved is None:
                self._stop_gst_locked(was_gst)
            if resolved is None and track_source == "yt" and self.native.available():
                try:
                    self._stop_station_player()
                    if normalized.get("queue_id") and self._is_native_current():
                        self.native.skip(int(normalized["queue_id"]))
                        resolved = {**normalized, "native": True}
                    else:
                        playlist = normalized.get("playlist_id") or (
                            f"RDAMVM{normalized['id']}" if track_source == "yt" else ""
                        )
                        self.native.play(normalized["id"], playlist)
                        resolved = {**normalized, "native": True, "playlist_id": playlist}
                    self._native_loading_until = time.time() + 4.0
                    self._native_force_loading_until = time.time() + 0.9
                    self._native_stopped_since = 0.0
                except NativeYTError as error:
                    _log.warning("native yt failed: %s", error)
                    resolved = None
            if resolved is None:
                if self._is_native_current():
                    try:
                        self.native.control("pause")
                    except Exception:  # noqa: BLE001 - fallback must not depend on it
                        pass
                try:
                    resolved = self._resolve(normalized)
                except (VKError, YTMusicError, YTDLPError) as error:
                    raise ExtMusicError(str(error)) from error
                try:
                    self.player.play(resolved["url"])
                except (PlayerError, OSError) as first:
                    # Stream URLs expire. Resolve once more and retry before giving up.
                    try:
                        resolved = self._resolve(normalized, force=True)
                        self.player.play(resolved["url"])
                    except (VKError, YTMusicError, YTDLPError, PlayerError, OSError) as second:
                        raise ExtMusicError(f"Не удалось включить: {second or first}") from second
            self.current = resolved
            self._ext_started_at = time.time()
            if resolved.get("native"):
                # The app owns the queue; it is filled from MediaSession on
                # the next poll. Do not replace it with the search list.
                self.queue = []
                self._orig_queue = []
                self.index = -1
            elif same_queue:
                self.index = existing_idx
                self._prefetch_after(self.queue, resolved)
            else:
                self._remember_queue(queue, resolved)
                self._orig_queue = list(self.queue)
                if self.shuffle:
                    self._apply_shuffle_locked()
                self._prefetch_after(self.queue, resolved)
            if track_source == "yt":
                self._cache_tracks([resolved, *self.queue])
                try:
                    self.store.push_recent_yt(resolved)
                except (OSError, ValueError):
                    pass
            self._start_poll(gen)
        self._emit_osd_track(resolved)
        if not multiroom_follower:
            self._emit_multiroom_play(resolved)
        return self.status()

    def set_shuffle(self, enabled: Optional[bool] = None) -> Dict[str, Any]:
        with self.lock:
            self._require_not_native()
            target = (not self.shuffle) if enabled is None else bool(enabled)
            if target != self.shuffle:
                self.shuffle = target
                if self.queue:
                    if target:
                        if not self._orig_queue:
                            self._orig_queue = list(self.queue)
                        self._apply_shuffle_locked()
                    else:
                        if self._orig_queue:
                            self.queue = list(self._orig_queue)
                        if self.current is not None:
                            cur_key = self._track_key(self.current)
                            self.index = next(
                                (i for i, t in enumerate(self.queue) if self._track_key(t) == cur_key),
                                0 if self.queue else -1,
                            )
                        else:
                            self.index = -1
                    if self.current is not None:
                        self._prefetch_after(self.queue, self.current)
        return self.status()

    def set_repeat(self, mode: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            self._require_not_native()
            if mode is None or str(mode).strip() == "":
                self.repeat = {"none": "all", "all": "one", "one": "none"}.get(self.repeat, "none")
            else:
                norm = str(mode).strip().lower()
                if norm not in ("none", "one", "all"):
                    raise ExtMusicError(f"Неизвестный режим повтора: {mode}")
                self.repeat = norm
        return self.status()

    def set_autoplay(self, enabled: bool) -> Dict[str, Any]:
        with self.lock:
            self._require_not_native()
            self.autoplay_radio = bool(enabled)
        return self.status()

    def queue_add(self, items: Any, mode: str = "end") -> Dict[str, Any]:
        norm_mode = str(mode or "end").strip().lower()
        if norm_mode not in ("next", "end", "replace"):
            raise ExtMusicError(f"Неизвестный режим добавления в очередь: {mode}")
        raw_items = list(items) if isinstance(items, (list, tuple)) else [items]
        tracks: List[Dict[str, Any]] = []
        seen_new = set()
        for raw in raw_items:
            t = self._queue_track(raw)
            if not t:
                continue
            k = self._track_key(t)
            if k in seen_new:
                continue
            seen_new.add(k)
            tracks.append(t)
        if not tracks:
            raise ExtMusicError("Не выбраны треки для добавления")
        self._cache_tracks([t for t in tracks if t.get("source") == "yt"])

        start_fresh = False
        with self.lock:
            self._require_not_native()
            if norm_mode == "replace" or not self.current or not self.queue:
                start_fresh = True
            else:
                cur_key = self._track_key(self.current)
                to_insert = [t for t in tracks if self._track_key(t) != cur_key]
                if to_insert:
                    insert_keys = {self._track_key(t) for t in to_insert}
                    new_queue: List[Dict[str, Any]] = []
                    new_index = self.index
                    for i, existing in enumerate(self.queue):
                        if self._track_key(existing) in insert_keys:
                            if i < self.index:
                                new_index -= 1
                            continue
                        new_queue.append(existing)
                    self.queue = new_queue
                    self.index = max(0, min(new_index, len(self.queue) - 1)) if self.queue else -1
                    if norm_mode == "next":
                        pos = (self.index + 1) if self.index >= 0 else len(self.queue)
                        self.queue[pos:pos] = to_insert
                    else:
                        self.queue.extend(to_insert)

                    if not self._orig_queue:
                        self._orig_queue = list(self.queue)
                    else:
                        self._orig_queue = [
                            t for t in self._orig_queue if self._track_key(t) not in insert_keys
                        ]
                        if norm_mode == "next":
                            orig_idx = next(
                                (i for i, t in enumerate(self._orig_queue) if self._track_key(t) == cur_key),
                                len(self._orig_queue) - 1,
                            )
                            orig_pos = orig_idx + 1 if orig_idx >= 0 else len(self._orig_queue)
                            self._orig_queue[orig_pos:orig_pos] = to_insert
                        else:
                            self._orig_queue.extend(to_insert)
                    self._prefetch_after(self.queue, self.current)
        if start_fresh:
            return self.play(tracks[0], tracks)
        return self.status()

    def queue_remove(self, index: int) -> Dict[str, Any]:
        need_next = False
        need_stop = False
        with self.lock:
            self._require_not_native()
            try:
                idx = int(index)
            except (TypeError, ValueError) as error:
                raise ExtMusicError("Неверный индекс трека в очереди") from error
            if idx < 0 or idx >= len(self.queue):
                raise ExtMusicError("Неверный индекс трека в очереди")
            removed = self.queue.pop(idx)
            rem_key = self._track_key(removed)
            self._orig_queue = [t for t in self._orig_queue if self._track_key(t) != rem_key]
            if idx < self.index:
                self.index -= 1
            elif idx == self.index:
                if not self.queue:
                    need_stop = True
                else:
                    self.index = idx - 1
                    need_next = True
            elif self.current is not None:
                self._prefetch_after(self.queue, self.current)
        if need_stop:
            return self.stop()
        if need_next:
            return self.next(auto=False)
        return self.status()

    def queue_move(self, from_index: int, to_index: int) -> Dict[str, Any]:
        with self.lock:
            self._require_not_native()
            try:
                fi = int(from_index)
                ti = int(to_index)
            except (TypeError, ValueError) as error:
                raise ExtMusicError("Неверный индекс трека в очереди") from error
            n = len(self.queue)
            if fi < 0 or fi >= n or ti < 0 or ti >= n:
                raise ExtMusicError("Неверный индекс трека в очереди")
            if fi != ti:
                item = self.queue.pop(fi)
                self.queue.insert(ti, item)
                if fi == self.index:
                    self.index = ti
                elif fi < self.index <= ti:
                    self.index -= 1
                elif ti <= self.index < fi:
                    self.index += 1
                if not self.shuffle:
                    self._orig_queue = list(self.queue)
                if self.current is not None:
                    self._prefetch_after(self.queue, self.current)
        return self.status()

    def queue_clear_upcoming(self) -> Dict[str, Any]:
        with self.lock:
            self._require_not_native()
            if self.queue and self.index >= 0:
                removed = self.queue[self.index + 1:]
                self.queue = self.queue[:self.index + 1]
                if not self.shuffle:
                    self._orig_queue = list(self.queue)
                else:
                    rem_keys = {self._track_key(t) for t in removed}
                    self._orig_queue = [t for t in self._orig_queue if self._track_key(t) not in rem_keys]
        return self.status()

    _RADIO_QUEUE_SIZE = 25

    def _append_unique_tracks(
        self,
        dest: List[Dict[str, Any]],
        items: Iterable[Any],
        seen: set,
        limit: Optional[int] = None,
    ) -> int:
        added = 0
        for raw in items or ():
            if limit is not None and len(dest) >= limit:
                break
            nt = self._queue_track(raw)
            if not nt:
                continue
            k = self._track_key(nt)
            if k in seen:
                continue
            seen.add(k)
            dest.append(nt)
            added += 1
        return added

    def _load_mix_tracks(self, browse_id: str, limit: int = _RADIO_QUEUE_SIZE) -> List[Dict[str, Any]]:
        bid = self._strip_ext_yt(browse_id)
        if not bid:
            return []
        if bid in ("VLLM", "FEmusic_liked_videos") and self.yt_account.is_configured():
            raw = self.yt_account.liked(limit=limit) or []
        else:
            col = self.yt_collection(bid, limit=limit)
            raw = col.get("tracks") or []
        queue: List[Dict[str, Any]] = []
        self._append_unique_tracks(queue, raw, set(), limit=limit)
        return queue

    def _select_personal_mix_id(self, mixes: List[Dict[str, Any]], name: str = "", index: Any = None) -> str:
        norm = " ".join(str(name or "").strip().lower().replace("ё", "е").split())
        req_idx: Optional[int] = None
        if index is not None and not isinstance(index, bool):
            try:
                val = int(index)
                if 1 <= val <= 9:
                    req_idx = val
            except (TypeError, ValueError):
                req_idx = None
        if req_idx is None and norm:
            last_tok = norm.split()[-1]
            if last_tok.isdigit() and 1 <= int(last_tok) <= 9:
                req_idx = int(last_tok)

        def _bid_of(entry: Dict[str, Any]) -> str:
            return str(entry.get("browse_id") or "").strip()

        if req_idx is not None:
            for m in mixes:
                title = str(m.get("title") or "").strip().lower().replace("ё", "е")
                if title in (f"мой микс {req_idx}", f"my mix {req_idx}", f"микс {req_idx}"):
                    return _bid_of(m)
            core_defs = getattr(self.yt_account, "_PERSONAL_MIX_DEFS", ())
            if req_idx < len(core_defs):
                return str(core_defs[req_idx][0])

        if any(tok in norm for tok in ("понравив", "лайк", "liked")):
            for m in mixes:
                if _bid_of(m) in ("VLLM", "FEmusic_liked_videos") or m.get("kind") == "liked":
                    return "VLLM"
            return "VLLM"

        if "супермикс" in norm or "supermix" in norm:
            words = [w for w in norm.split() if w not in ("мой", "супермикс", "supermix", "для")]
            if words:
                for m in mixes:
                    title = str(m.get("title") or "").strip().lower().replace("ё", "е")
                    if "супермикс" in title and all(w in title for w in words):
                        return _bid_of(m)
            for m in mixes:
                title = str(m.get("title") or "").strip().lower().replace("ё", "е")
                if title in ("мой супермикс", "my supermix"):
                    return _bid_of(m)
            core_defs = getattr(self.yt_account, "_PERSONAL_MIX_DEFS", ())
            if core_defs:
                return str(core_defs[0][0])

        if norm:
            for m in mixes:
                title = str(m.get("title") or "").strip().lower().replace("ё", "е")
                if title == norm:
                    return _bid_of(m)
            if norm in ("мой микс", "my mix", "персональный микс", "личный микс"):
                for m in mixes:
                    title = str(m.get("title") or "").strip().lower().replace("ё", "е")
                    if title in ("мой микс 1", "my mix 1"):
                        return _bid_of(m)
            for m in mixes:
                title = str(m.get("title") or "").strip().lower().replace("ё", "е")
                if norm in title:
                    return _bid_of(m)

        for m in mixes:
            bid = _bid_of(m)
            if bid.startswith("VLRDTMAK"):
                return bid
        for m in mixes:
            bid = _bid_of(m)
            if bid:
                return bid
        return ""

    def yt_mix_play(
        self,
        name: str = "",
        index: Any = None,
        browse_id: str = "",
        limit: int = _RADIO_QUEUE_SIZE,
    ) -> Dict[str, Any]:
        """Play a personalized YouTube Music mix ('Мой супермикс', 'Мой микс 1..5', 'Понравившаяся музыка')."""
        with self.lock:
            self._require_not_native()
        target = max(1, min(100, int(limit or self._RADIO_QUEUE_SIZE)))
        bid = self._strip_ext_yt(str(browse_id or ""))
        queue: List[Dict[str, Any]] = []

        if bid:
            queue = self._load_mix_tracks(bid, limit=target)
        elif self.yt_account.is_configured():
            mixes: List[Dict[str, Any]] = []
            try:
                home_data = self.yt_account.home(limit=target) or {}
                mixes = [m for m in (home_data.get("mixes") or []) if isinstance(m, dict)]
            except (YTAccountError, YTMusicError, ExtMusicError):
                mixes = []
            picked_bid = self._select_personal_mix_id(mixes, name=name, index=index)
            if picked_bid:
                try:
                    queue = self._load_mix_tracks(picked_bid, limit=target)
                except (YTAccountError, YTMusicError, ExtMusicError):
                    queue = []

        if not queue:
            try:
                home_data = self.yt_home(limit=target)
                fallback = (
                    (home_data.get("tracks") or [])
                    or (home_data.get("quick_picks") or [])
                    or (home_data.get("feed") or [])
                )
                self._append_unique_tracks(queue, fallback, set(), limit=target)
            except (YTAccountError, YTMusicError, ExtMusicError):
                queue = []

        if not queue:
            raise ExtMusicError("Не удалось загрузить персональный микс YouTube Music")
        self._cache_tracks(queue)
        return self.play(queue[0], queue)

    def radio_play(self, track: Any = None, browse_id: str = "") -> Dict[str, Any]:
        with self.lock:
            self._require_not_native()
            cur = dict(self.current) if isinstance(self.current, dict) else None

        seed_track = self._queue_track(track) if isinstance(track, dict) else None
        if seed_track is None and isinstance(track, str) and track.strip():
            vid_arg = self._strip_ext_yt(track)
            seed_track = self.yt_resolve_known_track(vid_arg) or {
                "source": "yt",
                "id": vid_arg,
                "title": "YouTube Music",
                "artist": "YouTube Music",
            }
        bid = self._strip_ext_yt(str(browse_id or ""))

        seed_vid = ""
        seed_pl = ""
        fallback_tracks: List[Dict[str, Any]] = []
        target_size = self._RADIO_QUEUE_SIZE

        if seed_track is not None:
            if seed_track.get("source") != "yt":
                raise ExtMusicError("Радио доступно только для YouTube Music")
            seed_vid = str(seed_track.get("id") or "")
            pl_cand = str(seed_track.get("playlist_id") or "")
            if pl_cand.startswith("RD") and not pl_cand.startswith("RDTMAK"):
                seed_pl = pl_cand
        elif bid:
            norm_bid = bid[2:] if bid.startswith("VL") else bid
            if norm_bid.startswith("RD") and not norm_bid.startswith("RDTMAK"):
                seed_pl = norm_bid
            else:
                col = self.yt_collection(bid, limit=target_size)
                col_tracks = [t for t in (col.get("tracks") or []) if isinstance(t, dict)]
                fallback_tracks = col_tracks
                col_pl = str(col.get("playlist_id") or "")
                if col_pl.startswith("RD") and not col_pl.startswith("RDTMAK"):
                    seed_pl = col_pl
                elif col_tracks:
                    seed_vid = str(col_tracks[0].get("id") or col_tracks[0].get("video_id") or "")
        elif cur is not None:
            if cur.get("source") != "yt":
                raise ExtMusicError("Радио доступно только для YouTube Music")
            seed_vid = str(cur.get("id") or "")
        else:
            raise ExtMusicError("Не указан трек или коллекция для запуска радио")

        if not seed_vid and not seed_pl:
            raise ExtMusicError("В коллекции нет треков для запуска радио")

        try:
            radio_tracks = self.ytmusic.radio(seed_vid, seed_pl, target_size) or []
        except YTMusicError as error:
            if not fallback_tracks and seed_track is None:
                raise ExtMusicError(str(error)) from error
            radio_tracks = []

        queue: List[Dict[str, Any]] = []
        seen = set()
        if seed_track is not None:
            matched = next(
                (r for r in radio_tracks if isinstance(r, dict) and str(r.get("id") or "") == seed_vid),
                None,
            )
            use_seed = matched if (matched and seed_track.get("title") in ("", "Без названия", "YouTube Music")) else seed_track
            self._append_unique_tracks(queue, [use_seed], seen, limit=target_size)
        self._append_unique_tracks(queue, radio_tracks or fallback_tracks, seen, limit=target_size)
        if 10 <= len(queue) < target_size and radio_tracks:
            tail_id = str(queue[-1].get("id") or "")
            if tail_id and tail_id != seed_vid:
                try:
                    more = self.ytmusic.radio(tail_id, "", target_size) or []
                    self._append_unique_tracks(queue, more, seen, limit=target_size)
                except YTMusicError:
                    pass
        if not queue:
            raise ExtMusicError("Не удалось подобрать радио-очередь")
        self._cache_tracks(queue)
        return self.play(queue[0], queue)

    def _stop_gst_locked(self, was_gst: bool = False) -> None:
        if not (was_gst or getattr(self.gst, "_is_active", False) or getattr(self.gst, "active", False)):
            return
        try:
            self.gst.stop()
        except GstBridgeError:
            if hasattr(self.gst, "release"):
                try:
                    self.gst.release()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    def _stop_station_player(self) -> None:
        if getattr(self.player, "port", None) is None and not (self.current and not self.current.get("native")):
            return
        try:
            self.player.stop()
        except Exception:  # noqa: BLE001 - helper is optional
            pass

    def _stop_yandex(self) -> None:
        try:
            if getattr(getattr(self.panel, "glagol", None), "ws", None):
                self.panel.music_cmd({"command": "stop"})
        except Exception:  # noqa: BLE001 - Yandex may be offline; external music can still play
            pass

    def pause(self, multiroom_follower: bool = False) -> Dict[str, Any]:
        with self.lock:
            if self._is_gst_current():
                self.gst.pause()
            elif self._is_native_current():
                self.native.control("pause")
                self._wait_native(lambda state: not state.get("playing"))
            else:
                self.player.pause()
        self._emit_multiroom_event("pause", suppress=multiroom_follower)
        return self.status()

    def resume(self, multiroom_follower: bool = False) -> Dict[str, Any]:
        with self.lock:
            if self._is_gst_current():
                self.gst.resume()
            elif self._is_native_current():
                self.native.control("play")
                self._wait_native(lambda state: bool(state.get("playing")))
            else:
                self.player.resume()
        self._emit_multiroom_event("resume", suppress=multiroom_follower)
        return self.status()

    def _wait_native(self, done, timeout: float = 1.2) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if done(self.native.status(force=True)):
                    return
            except Exception:  # noqa: BLE001 - status is best effort
                return
            time.sleep(0.15)

    def seek(self, position_ms: int, multiroom_follower: bool = False) -> Dict[str, Any]:
        with self.lock:
            if self._is_gst_current():
                self.gst.seek(position_ms)
                self._ext_started_at = time.time()
            elif self._is_native_current():
                duration = int((self.native.status().get("duration") or 0))
                if not duration:
                    duration = int(self.current.get("duration") or 0) * 1000
                try:
                    self.native.seek(position_ms, duration)
                except NativeYTError as error:
                    raise ExtMusicError(str(error)) from error
            else:
                self.player.seek(position_ms)
        self._emit_multiroom_event("seek", int(position_ms or 0), suppress=multiroom_follower)
        return self.status()

    def _stop_locked(self, stop_glagol: bool = True) -> None:
        self._gen += 1
        self._poll_stop.set()
        is_gst = self._is_gst_current()
        is_native = self._is_native_current()
        if is_gst:
            try:
                if not stop_glagol:
                    if hasattr(self.gst, "release"):
                        self.gst.release()
                    elif hasattr(self.gst, "_clear_override"):
                        self.gst._clear_override()
                    else:
                        self.gst.stop()
                else:
                    self.gst.stop()
            except GstBridgeError:
                pass
        if is_native:
            try:
                self.native.control("stop")
            except Exception:  # noqa: BLE001
                pass
        if stop_glagol or (not is_gst and not is_native) or getattr(self.player, "port", None) is not None:
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass
        self.current = None
        self.queue = []
        self._orig_queue = []
        self.index = -1
        self._ext_started_at = 0.0
        self.error = ""
        self.skipped = []
        self._native_loading_until = 0.0
        self._native_force_loading_until = 0.0
        self._native_stopped_since = 0.0
        with self._prefetch_lock:
            self._prefetch.clear()
            self._prefetching.clear()

    def stop(self, stop_glagol: bool = True, multiroom_follower: bool = False) -> Dict[str, Any]:
        with self.lock:
            self._stop_locked(stop_glagol=stop_glagol)
        self._emit_multiroom_event("stop", suppress=multiroom_follower)
        return self.status()

    def _try_play_candidates(self, indices: Iterable[int], skipped: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        for index in indices:
            if index < 0 or index >= len(self.queue):
                continue
            track = self.queue[index]
            try:
                result = self.play(track, self.queue)
                self.skipped = skipped
                result["skipped"] = self.skipped[-5:]
                return result
            except ExtMusicError as error:
                skipped.append({
                    "title": str(track.get("title") or "")[:120],
                    "id": str(track.get("id") or "")[:40],
                    "reason": str(error)[:240],
                })
        return None

    def next(self, auto: bool = False) -> Dict[str, Any]:
        seed_vid = ""
        seed_pl = ""
        radio_gen = -1
        early_result: Optional[Dict[str, Any]] = None
        need_status = False
        with self.lock:
            if self._is_native_current():
                self.native.control("next")
                self._native_loading_until = time.time() + 4.0
                self._native_force_loading_until = time.time() + 0.9
                need_status = True
            elif auto and self.repeat == "one" and self.current:
                if self._is_gst_current():
                    try:
                        self.gst.seek(0)
                        self._gen += 1
                        self._ext_started_at = time.time()
                        self.error = ""
                        need_status = True
                    except GstBridgeError:
                        pass
                if not need_status:
                    early_result = self.play(self.current, self.queue if self.queue else [self.current])
            elif not self.queue:
                self._stop_locked()
                need_status = True
            else:
                skipped: List[Dict[str, str]] = []
                if self.index + 1 < len(self.queue):
                    res = self._try_play_candidates(
                        range(self.index + 1, min(len(self.queue), self.index + 6)),
                        skipped,
                    )
                    if res is not None:
                        early_result = res
                    elif self._is_native_current():
                        try:
                            self.native.control("next")
                            self._native_loading_until = time.time() + 4.0
                            self._native_force_loading_until = time.time() + 0.9
                            self.skipped = skipped
                            need_status = True
                        except Exception:  # noqa: BLE001
                            pass
                    if early_result is None and not need_status:
                        self._stop_locked()
                        self.skipped = skipped
                        self.error = "Не удалось включить ни один следующий трек"
                        need_status = True
                elif self.repeat == "all":
                    res = self._try_play_candidates(range(0, min(len(self.queue), 5)), skipped)
                    if res is not None:
                        early_result = res
                    else:
                        self._stop_locked()
                        self.skipped = skipped
                        self.error = "Не удалось включить ни один следующий трек"
                        need_status = True
                elif (
                    self.repeat == "none"
                    and self.autoplay_radio
                    and self.current
                    and self.current.get("source") == "yt"
                ):
                    seed_vid = str(self.current.get("id") or "")
                    pl_cand = str(self.current.get("playlist_id") or "")
                    seed_pl = pl_cand if pl_cand.startswith("RD") else ""
                    radio_gen = self._gen
                else:
                    self._stop_locked()
                    need_status = True
        if early_result is not None:
            return early_result
        if need_status:
            return self.status()

        # Fetch auto-radio outside self.lock
        radio_error = ""
        radio_tracks: List[Dict[str, Any]] = []
        try:
            radio_tracks = self.ytmusic.radio(seed_vid, seed_pl, self._RADIO_QUEUE_SIZE) or []
        except YTMusicError as error:
            radio_error = str(error)
        except Exception as error:  # noqa: BLE001
            radio_error = str(error)
        if radio_tracks:
            self._cache_tracks(radio_tracks)

        result: Optional[Dict[str, Any]] = None
        with self.lock:
            if self._gen != radio_gen or not self.current:
                pass
            elif radio_error:
                self._stop_locked()
                self.error = radio_error
            else:
                existing_keys = {self._track_key(t) for t in self.queue}
                existing_keys.add(self._track_key(self.current))
                new_tracks: List[Dict[str, Any]] = []
                self._append_unique_tracks(new_tracks, radio_tracks, existing_keys, limit=self._RADIO_QUEUE_SIZE)
                if not new_tracks:
                    self._stop_locked()
                else:
                    start_idx = len(self.queue)
                    self.queue.extend(new_tracks)
                    orig_keys = {self._track_key(t) for t in self._orig_queue}
                    self._orig_queue.extend(t for t in new_tracks if self._track_key(t) not in orig_keys)

                    skipped = []
                    res = self._try_play_candidates(
                        range(start_idx, min(len(self.queue), start_idx + 5)),
                        skipped,
                    )
                    if res is not None:
                        result = res
                    else:
                        self._stop_locked()
                        self.skipped = skipped
                        self.error = "Не удалось включить ни один следующий трек"
        return result if result is not None else self.status()

    def prev(self) -> Dict[str, Any]:
        result: Optional[Dict[str, Any]] = None
        do_seek_zero = False
        with self.lock:
            if self._is_native_current():
                self.native.control("prev")
                self._native_loading_until = time.time() + 4.0
                self._native_force_loading_until = time.time() + 0.9
            elif not self.queue:
                pass
            else:
                skipped: List[Dict[str, str]] = []
                for index in range(self.index - 1, max(-1, self.index - 6), -1):
                    track = self.queue[index]
                    try:
                        result = self.play(track, self.queue)
                        self.skipped = skipped
                        result["skipped"] = self.skipped[-5:]
                        break
                    except ExtMusicError as error:
                        skipped.append({
                            "title": str(track.get("title") or "")[:120],
                            "id": str(track.get("id") or "")[:40],
                            "reason": str(error)[:240],
                        })
                if result is None:
                    self.skipped = skipped
                    do_seek_zero = True
        if result is not None:
            return result
        if do_seek_zero:
            return self.seek(0)
        return self.status()

    @staticmethod
    def _is_yandex_auto_advance(state: Dict[str, Any], pstate: Dict[str, Any]) -> bool:
        """True when Yandex SDK auto-advanced from FALLBACK_TRACK_ID into Radio/My Wave without voice input."""
        playlist_id = str(pstate.get("playlistId") or "").strip()
        if playlist_id == f"track:{FALLBACK_TRACK_ID}":
            return True
        if playlist_id != "user:onyourwave":
            return False
        voice_age = state.get("timeSinceLastVoiceActivity")
        if isinstance(voice_age, (int, float)) and voice_age <= 15:
            return False
        return True

    def maybe_auto_stop_from_glagol(self, glagol_state: Any) -> bool:
        if not isinstance(glagol_state, dict):
            return False
        state = glagol_state
        if "state" in state and isinstance(state.get("state"), dict) and "playerState" not in state:
            state = state["state"]
        if not state.get("playing"):
            return False
        pstate = state.get("playerState")
        if not isinstance(pstate, dict) or not pstate:
            return False
        ptype = str(pstate.get("playerType") or "").strip().lower()
        pid = str(pstate.get("id") or "").strip()
        if ptype == "external" or pid == FALLBACK_TRACK_ID or pid.startswith("ext:"):
            return False
        if ptype != "bluetooth" and not pid:
            return False
        with self.lock:
            if self.current is None:
                return False
            if (time.time() - self._ext_started_at) < 3.0:
                return False
            if self._is_gst_current() and self._is_yandex_auto_advance(state, pstate):
                return False
        self.stop(stop_glagol=False)
        return True

    # ---------- completion watcher ----------
    def _start_poll(self, gen: int) -> None:
        if self._poll_thread and self._poll_thread.is_alive() and not self._poll_stop.is_set():
            return
        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(gen, self._poll_stop), daemon=True, name="extmusic-poll"
        )
        self._poll_thread.start()

    def _auto_next(self, eos_gen: int) -> None:
        with self.lock:
            if self._gen != eos_gen or not self.current:
                return
        self.next(auto=True)

    def _poll_loop(self, gen: int, stop: threading.Event) -> None:
        last_eos_gen = -1
        last_prefetch_gen = -1
        err_count = 0
        while not stop.wait(self._poll_interval):
            gstate = None
            eos_gen: Optional[int] = None
            with self.lock:
                if stop.is_set() or not self.current:
                    return
                glagol = getattr(self.panel, "glagol", None)
                if glagol is not None:
                    raw_state = getattr(glagol, "state", None)
                    if isinstance(raw_state, dict):
                        gstate = raw_state

                if self._is_gst_current():
                    try:
                        player = self._gst_player()
                    except Exception as error:  # noqa: BLE001
                        err_count += 1
                        self.error = str(error)
                        if err_count >= 5:
                            _log.error("extmusic gst poll stopped after %d errors: %s", err_count, error)
                            return
                        continue
                    err_count = 0
                    state = str(player.get("state") or "")
                    if state == "error":
                        self.error = str(player.get("error") or "Родной плеер сообщил об ошибке")
                        _log.error("extmusic gst error: %s", self.error)
                        return
                    duration = int(player.get("duration") or 0)
                    position = int(player.get("position") or 0)
                    if duration > 0 and position >= max(0, duration - 20000) and self._gen != last_prefetch_gen:
                        last_prefetch_gen = self._gen
                        self._prefetch_after(self.queue, self.current)
                    g_inner = gstate.get("state") if isinstance(gstate, dict) and isinstance(gstate.get("state"), dict) and "playerState" not in gstate else gstate
                    g_pstate = (g_inner.get("playerState") or {}) if isinstance(g_inner, dict) else {}
                    yandex_leaked = (
                        isinstance(g_inner, dict)
                        and bool(g_inner.get("playing"))
                        and (time.time() - self._ext_started_at) >= 3.0
                        and self._is_yandex_auto_advance(g_inner, g_pstate)
                    )
                    if state == "ended" or yandex_leaked or (
                        state != "paused"
                        and not player.get("buffering")
                        and duration > 0
                        and position >= duration - 1200
                    ):
                        if self._gen != last_eos_gen:
                            eos_gen = self._gen
                            last_eos_gen = eos_gen
                    else:
                        self.error = ""
                elif self._is_native_current():
                    try:
                        native = self.native.status()
                    except Exception as error:  # noqa: BLE001
                        err_count += 1
                        self.error = f"YouTube Music: {error}"
                        if err_count >= 5:
                            _log.error("extmusic native yt poll stopped after %d errors: %s", err_count, error)
                            return
                        continue
                    err_count = 0
                    if not native.get("available"):
                        self.error = "YouTube Music не установлен на станции"
                        return
                    self._native_queue_sync()
                    title = str(native.get("title") or "")
                    if not (time.time() < self._native_loading_until and title
                            and not self._same_text(title, self.current.get("title"))):
                        self._native_sync(native)
                    state = str(native.get("state") or "none")
                    if state == "error":
                        self.error = "YouTube Music сообщил об ошибке"
                        return
                    if state in ("stopped", "none") and not native.get("playing"):
                        now = time.time()
                        if now < self._native_loading_until:
                            continue
                        if not self._native_stopped_since:
                            self._native_stopped_since = now
                            continue
                        if now - self._native_stopped_since < 2.0:
                            continue
                        self._native_stopped_since = 0.0
                        if self._gen != last_eos_gen:
                            eos_gen = self._gen
                            last_eos_gen = eos_gen
                    else:
                        self._native_stopped_since = 0.0
                        self.error = ""
                else:
                    try:
                        state = self.player.status()
                    except Exception as error:  # noqa: BLE001
                        err_count += 1
                        self.error = f"Плеер: {error}"
                        if err_count >= 5:
                            return
                        continue
                    err_count = 0
                    pstate = str(state.get("state") or "")
                    if pstate == "ended":
                        if self._gen != last_eos_gen:
                            eos_gen = self._gen
                            last_eos_gen = eos_gen
                    elif pstate == "error":
                        self.error = str(state.get("error") or "Ошибка плеера")
                        return
                    else:
                        self.error = ""

            if eos_gen is not None:
                try:
                    self._auto_next(eos_gen)
                except Exception as error:  # noqa: BLE001
                    with self.lock:
                        self.error = str(error)
            elif gstate is not None:
                try:
                    if self.maybe_auto_stop_from_glagol(gstate):
                        return
                except Exception as error:  # noqa: BLE001
                    _log.warning("poll glagol auto-stop error: %s", error)

    # ---------- fast local voice command router (< 50 ms) ----------
    @staticmethod
    def _parse_voice_seek_ms(norm: str, default_sec: int = 30) -> int:
        words_to_num = {
            "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
            "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
            "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
            "пятьдесят": 50,
        }
        if "полминуты" in norm or "пол минуты" in norm:
            return 30 * 1000
        if "полторы" in norm and "минут" in norm:
            return 90 * 1000
        num = None
        m = re.search(r"\b(\d+)\b", norm)
        if m:
            num = int(m.group(1))
        else:
            for w, val in words_to_num.items():
                if re.search(rf"\b{w}\b", norm):
                    num = val
                    break
        if num is None:
            num = 1 if re.search(r"\bминут[уае]?\b", norm) else default_sec
        mult = 60 if re.search(r"\bминут", norm) else 1
        return max(1, min(3600, num * mult)) * 1000

    def voice_command(self, phrase: str) -> Dict[str, Any]:
        """Parse and execute a fast local voice control command when external music is active."""
        t0 = time.monotonic()
        raw = str(phrase or "").strip()
        norm = re.sub(r"[^\w\s]", " ", raw.lower().replace("ё", "е"))
        norm = re.sub(r"\s+", " ", norm).strip()
        for wake in ("окей алиса", "ей алиса", "алиса"):
            if norm == wake:
                norm = ""
                break
            if norm.startswith(wake + " "):
                norm = norm[len(wake) + 1 :].strip()
                break

        with self.lock:
            cur = dict(self.current) if isinstance(self.current, dict) else None
        if not norm or not cur:
            return {
                "ok": True,
                "handled": False,
                "action": "",
                "say": "",
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
            }

        action = ""
        say = ""
        cur_id = str(cur.get("id") or "")
        cur_src = str(cur.get("source") or "yt")

        # 1. Negative / toggle-off modifiers before positive forms
        if any(p in norm for p in ("убери лайк", "сними лайк", "удали лайк", "удали из избран", "удали из понравив", "разонравил")):
            action = "unlike"
            if cur_src == "yt" and cur_id:
                self.yt_like(cur_id, on=False)
                say = "Убрал лайк."
        elif re.search(r"\b(дизлайк\w*|не нравится|больше не включай|ужасн\w* песн\w*)\b", norm):
            action = "dislike"
            if cur_src == "yt" and cur_id:
                self.yt_dislike(cur_id, skip=True)
            else:
                self.next()
        elif re.search(r"\b(лайк\w*|мне нравится|нравится песн\w*|нравится трек\w*|добавь в избран\w*|добавь в понравив\w*|классн\w* песн\w*|супер песн\w*)\b", norm):
            action = "like"
            if cur_src == "yt" and cur_id:
                self.yt_like(cur_id, on=True)
                say = "Поставил лайк."
        elif any(p in norm for p in ("выключи шаффл", "выключи перемеш", "отключи перемеш", "не перемешивай", "по порядку")):
            action = "shuffle_off"
            if not cur.get("native"):
                self.set_shuffle(False)
                say = "Включаю по порядку."
        elif re.search(r"\b(перемешай\w*|вперемешку|шаффл\w*|случайн\w* поряд\w*)\b", norm):
            action = "shuffle_on"
            if not cur.get("native"):
                self.set_shuffle(True)
                say = "Перемешал очередь."
        elif any(p in norm for p in ("выключи повтор", "отключи повтор", "без повтора", "не повторяй")):
            action = "repeat_off"
            if not cur.get("native"):
                self.set_repeat("none")
                say = "Повтор выключен."
        elif (
            any(p in norm for p in ("повторяй очередь", "повторяй плейлист", "повтор всего", "повтор очереди", "повтор плейлиста"))
            or re.search(r"\b(?:повтор\w*|зацикл\w*)\b.*\b(?:очеред\w*|плейлист\w*|вс[её]|всего|альбом\w*)\b", norm)
        ):
            action = "repeat_all"
            if not cur.get("native"):
                self.set_repeat("all")
                say = "Повторяю очередь."
        elif re.search(r"\b(на повтор|повтор\w*|зацикл\w*)\b", norm):
            action = "repeat_one"
            if not cur.get("native"):
                self.set_repeat("one")
                say = "Повторяю трек."
        elif re.search(r"\b(сначала|заново|с самого начала)\b", norm):
            action = "replay"
            self.seek(0)
        elif re.search(r"\b(перемотай|отмотай|мотай)\b", norm) or re.search(r"\b(вперед|назад)\s+(?:на\s+)?(?:\d+|минут|секунд|полмин)", norm):
            step_ms = self._parse_voice_seek_ms(norm)
            st = self.status()
            pos_ms = int((st.get("player") or {}).get("position") or 0)
            dur_ms = int((st.get("player") or {}).get("duration") or 0)
            if "назад" in norm or "обратно" in norm:
                action = "seek_back"
                target_ms = max(0, pos_ms - step_ms)
            else:
                action = "seek_fwd"
                target_ms = pos_ms + step_ms
                if dur_ms > 0:
                    target_ms = min(max(0, dur_ms - 2000), target_ms)
            self.seek(target_ms)
        elif any(q in norm for q in ("что играет", "что сейчас играет", "какая песня", "какой трек", "что за песня")):
            action = "what_playing"
            title = cur.get("title") or "без названия"
            artist = cur.get("artist") or "неизвестный исполнитель"
            say = f"Сейчас играет {title} — {artist}."
        elif re.search(r"\b(дальше|следующ\w*|переключ\w*|пропуст\w*|вперед|next|skip)\b", norm):
            action = "next"
            self.next()
        elif re.search(r"\b(назад|предыдущ\w*|верни\w*|previous|prev)\b", norm):
            action = "prev"
            self.prev()
        elif re.search(r"\b(пауз\w*|приостанов\w*|pause)\b", norm):
            action = "pause"
            self.pause()
        elif re.search(r"\b(стоп|останов\w*|хватит|заткнись|stop)\b", norm) or norm in ("выключи", "выключи музыку", "отключи музыку", "выключи песню"):
            action = "stop"
            self.stop()
        elif re.search(r"\b(продолж\w*|возобнов\w*|сними с паузы|resume)\b", norm) or norm in ("играй", "включи музыку", "play"):
            action = "resume"
            self.resume()

        return {
            "ok": True,
            "handled": bool(action),
            "action": action,
            "say": say,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
        }

    def alice_cancel(
        self,
        tts_sec: float = 4.5,
        music_sec: float = 12.0,
        clear_override: bool = False,
    ) -> Dict[str, Any]:
        """Cancel active native Alice dialog and suppress cloud fallback audio windows."""
        try:
            if hasattr(self.gst, "suppress_alice"):
                self.gst.suppress_alice(
                    tts_sec=float(tts_sec or 0.0),
                    music_sec=float(music_sec or 0.0),
                    clear_override=bool(clear_override),
                )
        except Exception:  # noqa: BLE001 - best-effort native cancel
            pass
        self._stop_yandex()
        return {"ok": True}

    def alice_say(self, text: str) -> Dict[str, Any]:
        """Speak short confirmation through the panel's authenticated Glagol WS connection."""
        clean = re.sub(r"[\x00-\x1f]", " ", str(text or "")).strip()[:300]
        if not clean:
            return {"ok": False, "spoken": False}
        try:
            if hasattr(self.gst, "clear_alice_tts_suppression"):
                self.gst.clear_alice_tts_suppression()
        except Exception:  # noqa: BLE001
            pass
        glagol = getattr(self.panel, "glagol", None)
        if glagol is not None and getattr(glagol, "ws", None) is not None:
            try:
                res = glagol.send({"command": "sendText", "text": f"Повтори за мной {clean}"})
                return {
                    "ok": True,
                    "spoken": True,
                    "status": (res or {}).get("status", "SUCCESS") if isinstance(res, dict) else "SUCCESS",
                }
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "spoken": False}


_SERVICE_LOCK = threading.Lock()


def get_service(panel) -> ExtMusicService:
    """Return the singleton ExtMusicService bound to ``panel`` thread-safely."""
    service = getattr(panel, "_extmusic_service", None)
    if service is not None:
        return service
    with _SERVICE_LOCK:
        service = getattr(panel, "_extmusic_service", None)
        if service is None:
            service = ExtMusicService(panel)
            panel._extmusic_service = service
        return service
