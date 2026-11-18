"""Control the native YouTube Music Android app on the station.

The ReVanced YouTube Music app plays with its own ExoPlayer and queue; the
panel only opens deep links and controls/reads the MediaSession. This avoids
yt-dlp URL extraction and the slow blocking MediaPlayer.prepare().

A tiny Java daemon (NativeMediaCtl.dex) runs through app_process as root in
the shell SELinux domain. Root passes the MEDIA_CONTENT_CONTROL check and the
shell domain can reach the media_session service, so the daemon can read an
accurate position/duration and seek. If the daemon is unavailable, status and
basic controls fall back to dumpsys/media keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import close_adb_forward

PACKAGE = "app.rvx.android.apps.youtube.music"
ACTIVITY = "com.google.android.apps.youtube.music.activities.MusicActivity"
PORT = 8782
REMOTE_DIR = "/data/yapanel-state/bin"
REMOTE_DEX = REMOTE_DIR + "/NativeMediaCtl.dex"
REMOTE_SCRIPT = REMOTE_DIR + "/native-media-start.sh"
REMOTE_PID = "/data/yapanel-state/native-media.pid"
REMOTE_LOG = "/data/yapanel-state/native-media.log"
STATE = {
    0: "none", 1: "stopped", 2: "paused", 3: "playing", 4: "fast_forward",
    5: "rewind", 6: "buffering", 7: "error", 8: "connecting", 9: "skipping",
    10: "skipped", 11: "skipping",
}
VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class NativeYTError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NativeYT:
    def __init__(self, panel):
        self.panel = panel
        self._available: Optional[bool] = None
        self._available_at = 0.0
        self._status: Dict[str, Any] = {}
        self._status_at = 0.0
        self._queue: Dict[str, Any] = {}
        self._queue_at = 0.0
        self._port: Optional[int] = None
        self._lock = threading.RLock()
        self._state_gen = 0
        self._start_lock = threading.Lock()
        self._start_retry_at = 0.0

    def _reset_port(self) -> None:
        with self._lock:
            old_port = self._port
            self._port = None
        if old_port is not None:
            close_adb_forward(getattr(self.panel, "adb", None), old_port)

    def close(self) -> None:
        """Close any forwarded port associated with this controller."""
        self._reset_port()

    # ---------- assets / daemon ----------
    @property
    def _local_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "device" / "native_yt"

    def _remote_sha(self, path: str) -> str:
        out, _, _ = self.panel.adb.shell(
            f"sha256sum {path} 2>/dev/null | cut -d' ' -f1", check=False, timeout=10
        )
        return (out or "").strip().splitlines()[0] if (out or "").strip() else ""

    def ensure_assets(self) -> None:
        self.panel.adb.shell(f"mkdir -p {REMOTE_DIR} /data/yapanel-state", check=False, timeout=10)
        local_dex = self._local_dir / "NativeMediaCtl.dex"
        local_script = self._local_dir / "native-media-start.sh"
        if not local_dex.is_file() or not local_script.is_file():
            raise NativeYTError("В установке нет нативного контроллера YouTube Music")
        if self._remote_sha(REMOTE_DEX) != _sha256(local_dex):
            self.panel.adb.push(local_dex, REMOTE_DEX, timeout=60)
            self.panel.adb.shell(f"chmod 644 {REMOTE_DEX}", check=False)
        if self._remote_sha(REMOTE_SCRIPT) != _sha256(local_script):
            self.panel.adb.push(local_script, REMOTE_SCRIPT, timeout=60)
            self.panel.adb.shell(f"chmod 755 {REMOTE_SCRIPT}", check=False)

    def _stop_existing(self) -> None:
        self.panel.adb.shell(
            "for p in $(ps -A -o PID,ARGS | grep '[N]ativeMediaCtl' | awk '{print $1}'); "
            "do kill \"$p\" 2>/dev/null; done; true",
            check=False,
            timeout=10,
        )

    def _start_daemon(self) -> bool:
        with self._start_lock:
            try:
                if self._send_once("PING", timeout=0.7).get("ok"):
                    return True
            except Exception:  # noqa: BLE001
                self._reset_port()
            now = time.time()
            with self._lock:
                if now < self._start_retry_at:
                    return False
            try:
                self.ensure_assets()
                self._stop_existing()
                self._reset_port()
                self.panel.adb.shell(
                    f"rm -f {REMOTE_PID}; "
                    f"if [ -x /odm/bin/start-stop-daemon ]; then "
                    f"/odm/bin/start-stop-daemon -S -b -m -p {REMOTE_PID} -x /system/bin/sh -- {REMOTE_SCRIPT}; "
                    f"else nohup /system/bin/sh {REMOTE_SCRIPT} >/dev/null 2>&1 & echo $! > {REMOTE_PID}; fi; true",
                    check=False,
                    timeout=20,
                )
                deadline = time.time() + 8
                while time.time() < deadline:
                    try:
                        if self._send_once("PING", timeout=1).get("ok"):
                            return True
                    except (OSError, NativeYTError):
                        pass
                    time.sleep(0.2)
            except Exception:  # noqa: BLE001 - app/daemon are optional
                pass
            with self._lock:
                self._start_retry_at = time.time() + 30
            return False

    def warmup(self) -> None:
        if self.available():
            self._start_daemon()

    # ---------- socket ----------
    def _connect(self):
        with self._lock:
            if self._port is None:
                self._port = self.panel.adb.forward(PORT)
            port = int(self._port)
        return socket.create_connection(("127.0.0.1", port), timeout=2)

    def _send_once(self, line: str, timeout: float = 2.0) -> Dict[str, Any]:
        with self._connect() as sock:
            sock.settimeout(timeout)
            sock.sendall((line + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data and len(data) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        text = data.decode("utf-8", "replace").splitlines()
        if not text:
            raise NativeYTError("Контроллер YouTube Music не ответил")
        try:
            result = json.loads(text[0])
        except ValueError as error:
            raise NativeYTError("Контроллер YouTube Music вернул некорректный ответ") from error
        if not isinstance(result, dict):
            raise NativeYTError("Контроллер YouTube Music вернул некорректный ответ")
        return result

    def _send(self, line: str, timeout: float = 2.0, start: bool = False) -> Dict[str, Any]:
        try:
            return self._send_once(line, timeout)
        except (OSError, NativeYTError):
            self._reset_port()
        if start and self._start_daemon():
            return self._send_once(line, timeout)
        raise NativeYTError("Контроллер YouTube Music недоступен")

    # ---------- app ----------
    def available(self) -> bool:
        now = time.time()
        with self._lock:
            if self._available is not None and now - self._available_at < 30:
                return self._available
            self._available_at = now
        try:
            out, _, _ = self.panel.adb.shell(
                f"runcon u:r:shell:s0 pm path {PACKAGE}", check=False, timeout=10
            )
            avail = "package:" in (out or "")
        except Exception:  # noqa: BLE001 - app is optional
            avail = False
        with self._lock:
            self._available = avail
            self._available_at = time.time()
            return bool(self._available)

    def _shell(self, command: str, timeout: int = 20, check: bool = False):
        # The panel runs as root in a context that cannot see the activity
        # service. runcon switches the child to the shell domain, exactly like
        # adb shell from a computer.
        return self.panel.adb.shell(
            "runcon u:r:shell:s0 " + command, timeout=timeout, check=check
        )

    def play(self, video_id: str, playlist: str = "") -> None:
        video_id = str(video_id or "").strip()
        if not VIDEO_RE.fullmatch(video_id):
            raise NativeYTError("Некорректный id трека YouTube")
        if not self.available():
            raise NativeYTError("YouTube Music не установлен на станции")
        url = f"https://music.youtube.com/watch?v={video_id}"
        playlist = str(playlist or "").strip()
        if playlist and re.fullmatch(r"[A-Za-z0-9_-]{2,200}", playlist):
            url += "&list=" + playlist
            if playlist.startswith("RD"):
                url += "&start_radio=1"
        command = (
            'am start -a android.intent.action.VIEW '
            f'-d "{url}" -n {PACKAGE}/{ACTIVITY}'
        )
        out, err, rc = self._shell(command, timeout=20)
        text = (out or "") + (err or "")
        if rc != 0 or "Error:" in text or "Exception" in text:
            raise NativeYTError("YouTube Music не открыл трек: " + text.strip()[:200])
        self._invalidate_cache()
        threading.Thread(target=self._ensure_playing, daemon=True, name="native-yt-autoplay").start()
        threading.Thread(target=self.warmup, daemon=True, name="native-yt-warmup").start()

    def _invalidate_cache(self) -> None:
        with self._lock:
            self._state_gen += 1
            self._status = {}
            self._status_at = 0.0
            self._queue = {}
            self._queue_at = 0.0

    @staticmethod
    def _copy_queue(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "active_id": data.get("active_id", -1),
            "items": [dict(item) if isinstance(item, dict) else item for item in (data.get("items") or [])],
        }

    def _ensure_playing(self) -> None:
        """Deep links can open paused; start the native player once loaded."""
        for _ in range(4):
            time.sleep(1.0)
            try:
                state = self.status(force=True)
            except Exception:  # noqa: BLE001
                continue
            if state.get("playing"):
                return
            if state.get("state") in ("loading", "buffering", "connecting"):
                continue
            try:
                self.control("play")
            except Exception:  # noqa: BLE001
                return

    def seek(self, position_ms: int, duration_ms: int = 0) -> None:
        position_ms = max(0, int(position_ms))
        if duration_ms:
            position_ms = min(position_ms, max(0, int(duration_ms) - 1000))
        self._send(f"SEEK {position_ms}", timeout=3, start=True)
        self._invalidate_cache()

    def skip(self, queue_id: int) -> None:
        self._send(f"SKIP {int(queue_id)}", timeout=3, start=True)
        self._invalidate_cache()

    def queue(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if not force and self._queue and now - self._queue_at < 0.8:
                return self._copy_queue(self._queue)
            gen = self._state_gen
        try:
            result = self._send("QUEUE", timeout=2)
        except (OSError, NativeYTError):
            new_q = {"active_id": -1, "items": []}
            with self._lock:
                if self._state_gen == gen:
                    self._queue = new_q
                    self._queue_at = time.time()
                return self._copy_queue(new_q)
        if not result.get("ok"):
            new_q = {"active_id": -1, "items": []}
            with self._lock:
                if self._state_gen == gen:
                    self._queue = new_q
                    self._queue_at = time.time()
                return self._copy_queue(new_q)
        items = []
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            queue_id = int(item.get("queue_id") or 0)
            duration = int(item.get("duration") or 0)
            items.append({
                "source": "yt",
                "id": "q" + str(queue_id),
                "title": str(item.get("title") or "Без названия")[:300],
                "artist": str(item.get("artist") or "YouTube Music")[:300],
                "album": "",
                "duration": round(duration / 1000) if duration else 0,
                "thumb": "",
                "url": "",
                "native": True,
                "queue_id": queue_id,
            })
        new_q = {
            "active_id": int(result.get("active_id") or -1),
            "items": items,
        }
        with self._lock:
            if self._state_gen == gen:
                self._queue = new_q
                self._queue_at = time.time()
        return self._copy_queue(new_q)

    def control(self, action: str) -> None:
        commands = {
            "play": "PLAY", "pause": "PAUSE", "toggle": "TOGGLE",
            "stop": "STOP", "next": "NEXT", "prev": "PREV",
        }
        command = commands.get(action)
        if command is None:
            raise NativeYTError("Неизвестная команда плеера")
        try:
            self._send(command, timeout=3, start=True)
        except NativeYTError:
            # Fallback: media keys work through the active media session even
            # when the controller daemon cannot start.
            keys = {
                "play": 126, "pause": 127, "toggle": 85,
                "next": 87, "prev": 88, "stop": 127,
            }
            self._shell(f"input keyevent {keys[action]}", timeout=10)
        self._invalidate_cache()

    # ---------- status ----------
    def _uptime_ms(self) -> float:
        try:
            if os.path.exists("/proc/uptime"):
                return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0]) * 1000.0
        except (OSError, ValueError, IndexError):
            pass
        try:
            out, _, _ = self.panel.adb.shell("cat /proc/uptime", check=False, timeout=5)
            return float((out or "0").split()[0]) * 1000.0
        except (OSError, ValueError, IndexError):
            return 0.0

    def _dumpsys_status(self) -> Dict[str, Any]:
        try:
            out, _, _ = self._shell("dumpsys media_session", timeout=15)
        except Exception:  # noqa: BLE001
            return {"available": True, "state": "none", "playing": False}
        text = out or ""
        index = text.find("YouTube playerlib " + PACKAGE)
        if index < 0:
            return {"available": True, "state": "none", "playing": False}
        block = text[index:index + 12000]
        state_match = re.search(
            r"state=PlaybackState \{state=(\d+), position=(\d+), buffered position=(\d+), speed=([\d.]+), updated=(\d+)",
            block,
        )
        desc_match = re.search(r"metadata:size=\d+, description=([^\n]+)", block)
        queue_match = re.search(r"queueTitle=([^,\n]*), size=(\d+)", block)
        state_num = int(state_match.group(1)) if state_match else 0
        position = int(state_match.group(2)) if state_match else 0
        if state_match and state_num == 3:
            speed = float(state_match.group(4))
            updated = int(state_match.group(5))
            position += int(max(0.0, self._uptime_ms() - updated) * speed)
        title = artist = album = ""
        if desc_match:
            parts = [part.strip() for part in desc_match.group(1).split(",")]
            title = parts[0] if parts else ""
            artist = parts[1] if len(parts) > 1 else ""
            album = parts[2] if len(parts) > 2 else ""
        return {
            "available": True,
            "session": True,
            "state": STATE.get(state_num, "none"),
            "state_code": state_num,
            "playing": state_num == 3,
            "paused": state_num == 2,
            "loading": state_num in (6, 8, 9),
            "position": max(0, position),
            "duration": 0,
            "title": "" if title == "null" else title,
            "artist": "" if artist == "null" else artist,
            "album": "" if album == "null" else album,
            "queue_size": int(queue_match.group(2)) if queue_match else 0,
            "source": "dumpsys",
        }

    def status(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if not force and self._status and now - self._status_at < 0.4:
                return dict(self._status)
            gen = self._state_gen
        if not self.available():
            st = {"available": False, "state": "none", "playing": False}
            with self._lock:
                if self._state_gen == gen:
                    self._status = st
                    self._status_at = time.time()
            return dict(st)
        try:
            result = self._send("STATUS", timeout=1.5)
        except (OSError, NativeYTError):
            result = None
        if not result or not result.get("ok"):
            with self._lock:
                should_recover = time.time() >= self._start_retry_at
            if should_recover:
                threading.Thread(target=self._start_daemon, daemon=True, name="native-yt-recover").start()
            fallback = self._dumpsys_status()
            with self._lock:
                if self._state_gen == gen:
                    self._status = fallback
                    self._status_at = time.time()
            return dict(fallback)
        state_code = int(result.get("state") or 0)
        st = {
            "available": True,
            "session": bool(result.get("session")),
            "state": STATE.get(state_code, result.get("state_name") or "none"),
            "state_code": state_code,
            "playing": bool(result.get("playing")),
            "paused": bool(result.get("paused")),
            "loading": state_code in (6, 8, 9, 10, 11),
            "position": max(0, int(result.get("position") or 0)),
            "duration": max(0, int(result.get("duration") or 0)),
            "speed": float(result.get("speed") or 0),
            "title": str(result.get("title") or ""),
            "artist": str(result.get("artist") or ""),
            "album": str(result.get("album") or ""),
            "media_id": str(result.get("media_id") or ""),
            "queue_size": int(result.get("queue_size") or 0),
            "source": "daemon",
        }
        with self._lock:
            if self._state_gen == gen:
                self._status = st
                self._status_at = time.time()
        return dict(st)
