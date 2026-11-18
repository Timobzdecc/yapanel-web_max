"""Client for the small on-station MediaPlayer daemon.

The daemon is intentionally separate from the panel process: a bad stream,
codec or URL can only take down this helper, while the panel and Yandex Music
keep working.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import close_adb_forward

PORT = 8781
REMOTE_DIR = "/data/yapanel-state/bin"
REMOTE_DEX = REMOTE_DIR + "/ExtMusicPlayer.dex"
REMOTE_SCRIPT = REMOTE_DIR + "/extmusic-start.sh"
REMOTE_PID = "/data/yapanel-state/extmusic.pid"
REMOTE_LOG = "/data/yapanel-state/extmusic.log"


class PlayerError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PlayerClient:
    def __init__(self, panel):
        self.panel = panel
        self.port: Optional[int] = None
        self._started_at = 0.0
        self._lock = threading.RLock()

    def _reset_port(self) -> None:
        with self._lock:
            old_port = self.port
            self.port = None
        if old_port is not None:
            close_adb_forward(getattr(self.panel, "adb", None), old_port)

    def close(self) -> None:
        """Close any active forwarded port."""
        self._reset_port()

    @property
    def _local_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "device" / "extmusic"

    def _remote_sha(self, path: str) -> str:
        out, _, _ = self.panel.adb.shell(
            f"sha256sum {path} 2>/dev/null | cut -d' ' -f1", check=False, timeout=10
        )
        return (out or "").strip().splitlines()[0] if (out or "").strip() else ""

    def ensure_assets(self) -> None:
        self.panel.adb.shell(f"mkdir -p {REMOTE_DIR} /data/yapanel-state", check=False)
        local_dex = self._local_dir / "ExtMusicPlayer.dex"
        local_script = self._local_dir / "extmusic-start.sh"
        if not local_dex.is_file():
            raise PlayerError("В установке нет ExtMusicPlayer.dex")
        if self._remote_sha(REMOTE_DEX) != _sha256(local_dex):
            self.panel.adb.push(local_dex, REMOTE_DEX, timeout=60)
            self.panel.adb.shell(f"chmod 644 {REMOTE_DEX}", check=False)
        if self._remote_sha(REMOTE_SCRIPT) != _sha256(local_script):
            self.panel.adb.push(local_script, REMOTE_SCRIPT, timeout=60)
            self.panel.adb.shell(f"chmod 755 {REMOTE_SCRIPT}", check=False)

    def _stop_existing(self) -> None:
        """Kill stale helpers so they cannot hold port 8781 or a stuck stream."""
        self.panel.adb.shell(
            "for p in $(ps -A -o PID,ARGS | grep '[E]xtMusicPlayer' | awk '{print $1}'); "
            "do kill \"$p\" 2>/dev/null; done; true",
            check=False,
            timeout=10,
        )

    def start(self) -> None:
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
        with self._lock:
            self._started_at = time.time()
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                if self._send_once("PING", timeout=1).get("ok"):
                    return
            except (OSError, PlayerError):
                pass
            time.sleep(0.25)
        raise PlayerError("Плеер не запустился")

    def _connect(self) -> socket.socket:
        with self._lock:
            if self.port is None:
                self.port = self.panel.adb.forward(PORT)
            port = int(self.port)
        return socket.create_connection(("127.0.0.1", port), timeout=8)

    def _send_once(self, line: str, timeout: float = 45.0) -> Dict[str, Any]:
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
            raise PlayerError("Плеер не ответил")
        try:
            result = json.loads(text[0])
        except ValueError as error:
            raise PlayerError("Плеер вернул некорректный ответ") from error
        if not isinstance(result, dict):
            raise PlayerError("Плеер вернул некорректный ответ")
        return result

    def _send(self, line: str, timeout: float = 45.0) -> Dict[str, Any]:
        try:
            return self._send_once(line, timeout)
        except (OSError, PlayerError):
            # The helper may have died after a reboot or OOM. Restart once.
            self._reset_port()
            with self._lock:
                started_at = self._started_at
            if time.time() - started_at > 2:
                self.start()
                time.sleep(0.5)
            return self._send_once(line, timeout)

    def ping(self) -> bool:
        try:
            return bool(self._send_once("PING", timeout=3).get("ok"))
        except Exception:
            return False

    def play(self, url: str) -> Dict[str, Any]:
        encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
        result = self._send("PLAY " + encoded, timeout=60)
        if not result.get("ok"):
            raise PlayerError(result.get("error") or "Не удалось запустить поток")
        return result

    def pause(self) -> Dict[str, Any]:
        return self._send("PAUSE", timeout=10)

    def resume(self) -> Dict[str, Any]:
        return self._send("RESUME", timeout=10)

    def seek(self, position_ms: int) -> Dict[str, Any]:
        return self._send("SEEK %d" % max(0, int(position_ms)), timeout=10)

    def stop(self) -> Dict[str, Any]:
        try:
            return self._send("STOP", timeout=10)
        except Exception:
            return {"ok": True, "state": "idle"}

    def status(self) -> Dict[str, Any]:
        return self._send("STATUS", timeout=5)
