"""Synchronous Multiroom Coordinator for External Music (YouTube Music / DLNA).

Coordinates playback (play, pause, resume, seek, stop, optional volume) from a
Master YapAnel station to one or more LAN Follower YapAnel stations via their
/api/extmusic/* endpoints and local GstBridge LAN relay URLs.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_log = logging.getLogger("yapanel.extmusic.multiroom")

DEFAULT_PANEL_PORT = 8765


def _local_ips() -> Set[str]:
    ips: Set[str] = {"127.0.0.1", "::1"}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.168.1.1", 80))
            ip = sock.getsockname()[0]
            if ip:
                ips.add(ip)
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr:
                ips.add(str(addr).split("%", 1)[0])
    except OSError:
        pass
    return ips


def validate_follower_url(
    url: str,
    *,
    self_ips: Optional[Set[str]] = None,
    self_port: int = DEFAULT_PANEL_PORT,
    allow_loopback_diff_port: bool = True,
) -> str:
    """Validate and normalize a follower speaker URL (RFC1918 LAN IP only, no self-loop)."""
    raw = str(url or "").strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise ValueError("Некорректный адрес ведомой колонки")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as err:
        raise ValueError("Некорректный адрес ведомой колонки") from err

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("Разрешены только http:// и https:// адреса колонок")
    if parsed.username or parsed.password:
        raise ValueError("Логин и пароль в URL не поддерживаются")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Адрес колонки не должен содержать путь или параметры")

    host = (parsed.hostname or "").strip("[]").lower()
    if not host:
        raise ValueError("Не указан IP-адрес колонки")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as err:
        raise ValueError("Укажите локальный IPv4-адрес колонки в домашней сети") from err

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError("Недопустимый системный IP-адрес")

    is_loop = bool(ip.is_loopback)
    if not (ip.is_private and not is_loop) and not (is_loop and allow_loopback_diff_port):
        raise ValueError("Мультирум работает только с колонками в домашней локальной сети (RFC1918)")

    try:
        port = parsed.port or (DEFAULT_PANEL_PORT if scheme == "http" else 443)
    except ValueError as err:
        raise ValueError("Некорректный порт колонки") from err
    if not (1 <= port <= 65535):
        raise ValueError("Некорректный порт колонки")

    ips = self_ips if self_ips is not None else _local_ips()
    norm_ip = str(ip)
    if port == int(self_port or DEFAULT_PANEL_PORT) and (norm_ip in ips or is_loop):
        raise ValueError("Нельзя добавить эту же колонку в качестве ведомой")

    return f"{scheme}://{norm_ip}:{port}"


def load_speakers_json_followers(root_dir: Path, data_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read optional speakers.json suggestions for one-click follower setup."""
    candidates: List[Path] = []
    if data_dir:
        candidates.append(Path(data_dir) / "speakers.json")
    if root_dir:
        candidates.append(Path(root_dir) / "speakers.json")
    found: List[Dict[str, Any]] = []
    seen_urls: Set[str] = set()
    for p in candidates:
        if not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = raw.get("speakers") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            dev = str(item.get("device") or "").strip()
            host = dev.partition(":")[0] if ":" in dev else dev
            try:
                norm_url = validate_follower_url(host)
            except ValueError:
                continue
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)
            found.append(
                {
                    "id": str(item.get("id") or hashlib.sha1(norm_url.encode("utf-8")).hexdigest()[:8]),
                    "name": str(item.get("name") or host)[:80],
                    "url": norm_url,
                }
            )
    return found


class MaxMultiroomCoordinator:
    """Master-side synchronous controller fanning out External Music commands to followers."""

    def __init__(self, panel):
        self.panel = panel
        self._lock = threading.RLock()
        cfg = self._load_config()
        self._enabled: bool = bool(cfg.get("enabled", False))
        self._sync_volume: bool = bool(cfg.get("sync_volume", False))
        self._followers: List[Dict[str, Any]] = []
        for raw_f in cfg.get("followers") or []:
            if not isinstance(raw_f, dict):
                continue
            try:
                norm_url = validate_follower_url(str(raw_f.get("url") or ""))
            except ValueError:
                continue
            fid = str(raw_f.get("id") or hashlib.sha1(norm_url.encode("utf-8")).hexdigest()[:10])
            self._followers.append(
                {
                    "id": fid,
                    "name": str(raw_f.get("name") or norm_url)[:80],
                    "url": norm_url,
                    "enabled": bool(raw_f.get("enabled", True)),
                    "offset_ms": max(-2000, min(5000, int(raw_f.get("offset_ms") or 0))),
                    "last_status": "",
                    "latency_ms": 0,
                }
            )
        self._gen = 0

    def _load_config(self) -> Dict[str, Any]:
        try:
            loader = getattr(self.panel, "_load_settings", None)
            s = loader() if callable(loader) else None
            if not isinstance(s, dict):
                import server

                s = server.load_settings()
            cfg = s.get("extmusic_multiroom") if isinstance(s, dict) else None
            if isinstance(cfg, dict):
                return dict(cfg)
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_config(self) -> None:
        with self._lock:
            payload = {
                "enabled": bool(self._enabled),
                "sync_volume": bool(self._sync_volume),
                "followers": [
                    {
                        "id": f["id"],
                        "name": f["name"],
                        "url": f["url"],
                        "enabled": bool(f["enabled"]),
                        "offset_ms": int(f.get("offset_ms") or 0),
                    }
                    for f in self._followers
                ],
            }
        try:
            import server

            updater = getattr(server, "update_settings_section", None)
            if callable(updater):
                updater("extmusic_multiroom", payload)
        except Exception:  # noqa: BLE001
            pass

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def sync_volume(self) -> bool:
        return self._sync_volume

    def _active_followers(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not self._enabled:
                return []
            return [dict(f) for f in self._followers if f.get("enabled", True)]

    def _http_post(self, base_url: str, path: str, payload: Dict[str, Any], timeout: float = 3.5) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-YapAnel-Multiroom-Origin": "1",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(262144).decode("utf-8", errors="replace")
        return json.loads(raw or "{}")

    def _send_to_follower(
        self,
        follower: Dict[str, Any],
        path: str,
        payload: Dict[str, Any],
        timeout: float = 3.5,
    ) -> Tuple[bool, int, str]:
        t0 = time.monotonic()
        try:
            self._http_post(follower["url"], path, payload, timeout=timeout)
            rtt = int((time.monotonic() - t0) * 1000)
            self._record_follower_state(follower["id"], "ok", rtt)
            return True, rtt, "ok"
        except Exception as err:  # noqa: BLE001 - network errors must be captured per follower
            rtt = int((time.monotonic() - t0) * 1000)
            msg = str(err)[:120] or type(err).__name__
            self._record_follower_state(follower["id"], msg, rtt)
            return False, rtt, msg

    def _record_follower_state(self, follower_id: str, status_msg: str, latency_ms: int) -> None:
        with self._lock:
            for f in self._followers:
                if f["id"] == follower_id:
                    f["last_status"] = status_msg
                    f["latency_ms"] = latency_ms
                    break

    def _fanout(self, worker_fn, blocking: bool = False) -> None:
        followers = self._active_followers()
        if not followers:
            return
        with self._lock:
            gen = self._gen
        threads = [
            threading.Thread(
                target=worker_fn,
                args=(f, gen),
                daemon=True,
                name=f"mr-{f['id']}",
            )
            for f in followers
        ]
        for t in threads:
            t.start()
        if blocking:
            for t in threads:
                t.join(timeout=4.0)

    def on_master_play(self, track: Dict[str, Any], relay_url: Optional[str] = None, blocking: bool = False) -> None:
        if not isinstance(track, dict) or track.get("multiroom_follower"):
            return
        if not self._active_followers():
            return
        with self._lock:
            self._gen += 1
        stream_url = str(relay_url or track.get("url") or "").strip()
        fallback_urls = [u for u in (track.get("url"), relay_url) if u and u != stream_url]
        follower_track = {
            "source": str(track.get("source") or "yt"),
            "id": str(track.get("id") or "stream"),
            "title": str(track.get("title") or ""),
            "artist": str(track.get("artist") or ""),
            "album": str(track.get("album") or ""),
            "duration": int(track.get("duration") or 0),
            "thumb": str(track.get("thumb") or ""),
            "url": stream_url,
            "fallback_urls": fallback_urls,
        }

        def run(f: Dict[str, Any], gen: int) -> None:
            ok, _rtt, _msg = self._send_to_follower(
                f,
                "/api/extmusic/play",
                {"track": follower_track, "multiroom_follower": True},
                timeout=8.0,
            )
            offset_ms = int(f.get("offset_ms") or 0)
            if ok and offset_ms > 0:
                time.sleep(0.35)
                with self._lock:
                    if self._gen != gen:
                        return
                self._send_to_follower(
                    f,
                    "/api/extmusic/seek",
                    {"position": offset_ms, "multiroom_follower": True},
                    timeout=3.0,
                )

        self._fanout(run, blocking=blocking)

    def on_master_pause(self, blocking: bool = False) -> None:
        self._fanout(
            lambda f, _g: self._send_to_follower(f, "/api/extmusic/pause", {"multiroom_follower": True}),
            blocking=blocking,
        )

    def on_master_resume(self, blocking: bool = False) -> None:
        self._fanout(
            lambda f, _g: self._send_to_follower(f, "/api/extmusic/resume", {"multiroom_follower": True}),
            blocking=blocking,
        )

    def on_master_seek(self, position_ms: int, blocking: bool = False) -> None:
        base_ms = max(0, int(position_ms or 0))
        started = time.monotonic()

        def run(f: Dict[str, Any], _g: int) -> None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            offset_ms = int(f.get("offset_ms") or 0)
            target_ms = max(0, base_ms + elapsed_ms + offset_ms)
            self._send_to_follower(
                f,
                "/api/extmusic/seek",
                {"position": target_ms, "multiroom_follower": True},
                timeout=4.0,
            )

        self._fanout(run, blocking=blocking)

    def on_master_stop(self, blocking: bool = False) -> None:
        with self._lock:
            self._gen += 1
        self._fanout(
            lambda f, _g: self._send_to_follower(f, "/api/extmusic/stop", {"multiroom_follower": True}),
            blocking=blocking,
        )

    def on_master_volume(self, level: int, blocking: bool = False) -> None:
        if not self._sync_volume or not self._active_followers():
            return
        lvl = max(0, min(100, int(level)))
        self._fanout(
            lambda f, _g: self._send_to_follower(
                f, "/api/volume", {"level": lvl, "multiroom_follower": True}
            ),
            blocking=blocking,
        )

    def add_follower(self, url: str, name: str = "", offset_ms: int = 0, enabled: bool = True) -> Dict[str, Any]:
        norm_url = validate_follower_url(url)
        label = str(name or "").strip()[:80] or urllib.parse.urlsplit(norm_url).hostname or norm_url
        try:
            off = max(-2000, min(5000, int(offset_ms or 0)))
        except (TypeError, ValueError):
            off = 0
        fid = hashlib.sha1(norm_url.encode("utf-8")).hexdigest()[:10]
        with self._lock:
            existing = next((f for f in self._followers if f["url"] == norm_url), None)
            if existing is not None:
                existing["name"] = label
                existing["offset_ms"] = off
                existing["enabled"] = bool(enabled)
            else:
                self._followers.append(
                    {
                        "id": fid,
                        "name": label,
                        "url": norm_url,
                        "enabled": bool(enabled),
                        "offset_ms": off,
                        "last_status": "",
                        "latency_ms": 0,
                    }
                )
        self._save_config()
        return self.status()

    def remove_follower(self, follower_id: str) -> Dict[str, Any]:
        target = str(follower_id or "").strip()
        with self._lock:
            self._followers = [f for f in self._followers if f["id"] != target]
        self._save_config()
        return self.status()

    def update_follower(self, follower_id: str, **patch: Any) -> Dict[str, Any]:
        target = str(follower_id or "").strip()
        with self._lock:
            item = next((f for f in self._followers if f["id"] == target), None)
            if item is None:
                raise ValueError("Колонка не найдена")
            if "enabled" in patch and patch["enabled"] is not None:
                item["enabled"] = bool(patch["enabled"])
            if "name" in patch and patch["name"] is not None:
                item["name"] = str(patch["name"]).strip()[:80] or item["name"]
            if "offset_ms" in patch and patch["offset_ms"] is not None:
                item["offset_ms"] = max(-2000, min(5000, int(patch["offset_ms"])))
        self._save_config()
        return self.status()

    def sync_now(self) -> Dict[str, Any]:
        svc = getattr(self.panel, "_extmusic_service", None)
        if svc is None or not getattr(svc, "current", None):
            raise ValueError("Сейчас не играет внешний поток")
        st = svc.status()
        cur = st.get("current") or {}
        player = st.get("player") or {}
        pos_ms = max(0, int(player.get("position") or 0))
        relay = svc.master_relay_url(seek_sec=pos_ms / 1000.0) if hasattr(svc, "master_relay_url") else None
        self.on_master_play(cur, relay_url=relay, blocking=True)
        if pos_ms > 500:
            self.on_master_seek(pos_ms, blocking=True)
        return self.status()

    def configure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("Ожидается JSON-объект мультирума")
        changed = False
        if "enabled" in data and data["enabled"] is not None:
            self._enabled = bool(data["enabled"])
            changed = True
        if "sync_volume" in data and data["sync_volume"] is not None:
            self._sync_volume = bool(data["sync_volume"])
            changed = True
        if changed:
            self._save_config()

        act = str(data.get("action") or "").strip().lower()
        if act == "add" or (not act and data.get("url")):
            return self.add_follower(
                url=str(data.get("url") or ""),
                name=str(data.get("name") or ""),
                offset_ms=data.get("offset_ms", 0),
                enabled=bool(data.get("enabled_follower", True)),
            )
        if act == "remove":
            return self.remove_follower(str(data.get("id") or ""))
        if act == "update":
            return self.update_follower(
                str(data.get("id") or ""),
                enabled=data.get("enabled_follower"),
                name=data.get("name"),
                offset_ms=data.get("offset_ms"),
            )
        if act == "sync":
            return self.sync_now()

        return self.status()

    def status(self) -> Dict[str, Any]:
        root_dir = Path(__file__).resolve().parent.parent
        suggestions = load_speakers_json_followers(root_dir, None)
        with self._lock:
            existing_urls = {f["url"] for f in self._followers}
            return {
                "ok": True,
                "enabled": self._enabled,
                "sync_volume": self._sync_volume,
                "followers": [dict(f) for f in self._followers],
                "suggestions": [s for s in suggestions if s["url"] not in existing_urls],
            }
