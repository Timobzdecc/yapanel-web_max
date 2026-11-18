"""Remote view and input helpers for the station screen.

The panel uses this for one-time Google sign-in and for the YouTube Music
library screen: both UIs live in Android apps, so the user taps and types
through a small screen mirror. Nothing is stored; text is passed to Android's
`input` command and forgotten.
"""
from __future__ import annotations

import os
import re
import shlex
import threading
import time
from typing import Any, Dict, Optional, Tuple

PACKAGE = "app.revanced.android.gms"
LOGIN_ACTIVITY = "org.microg.gms.auth.login.LoginActivity"
YTM_PACKAGE = "app.rvx.android.apps.youtube.music"
YTM_ACTIVITY = "com.google.android.apps.youtube.music.activities.MusicActivity"
MODE_LOGIN = "login"
MODE_LIBRARY = "library"
FRAME_PATH = "/data/local/tmp/panel-screen.png"
DUMP_PATH = "/data/local/tmp/panel-ui.xml"
INPUT_HELPER = "/data/yapanel/device/input/InputText.dex"
KEYS = {
    "back": 4,
    "home": 3,
    "enter": 66,
    "tab": 61,
    "delete": 67,
    "escape": 111,
    "space": 62,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
}


class ScreenError(ValueError):
    pass


class StationScreen:
    def __init__(self, panel):
        self.panel = panel
        self._lock = threading.Lock()
        self._size: Optional[Tuple[int, int]] = None
        self._size_at = 0.0
        self._frame: Optional[bytes] = None
        self._frame_at = 0.0
        self._foreground: Optional[bool] = None
        self._foreground_at = 0.0
        self.mode = MODE_LOGIN

    def _shell(self, command: str, timeout: int = 20):
        return self.panel.adb.shell(
            "runcon u:r:shell:s0 " + command, timeout=timeout, check=False
        )

    def input_foreground(self, cache_seconds: float = 0.4) -> bool:
        """Only send input when the app selected by the current mode is in front."""
        allowed = PACKAGE if self.mode == MODE_LOGIN else YTM_PACKAGE
        now = time.time()
        if self._foreground is not None and now - self._foreground_at < cache_seconds:
            return self._foreground
        foreground = False
        try:
            out, _, _ = self._shell("dumpsys window windows", timeout=10)
            match = re.search(
                r"mCurrentFocus=Window\{[^}]*?\s([\w.]+)/([\w.$]+)", out or ""
            )
            if not match:
                out, _, _ = self._shell("dumpsys activity activities", timeout=10)
                match = re.search(
                    r"mResumedActivity: ActivityRecord\{[^}]*?\s([\w.]+)/([\w.$]+)",
                    out or "",
                )
            if match:
                foreground = match.group(1) == allowed
        except Exception:  # noqa: BLE001 - fail closed, never type blindly
            foreground = False
        self._foreground = foreground
        self._foreground_at = now
        return foreground

    def _require_input(self) -> None:
        if not self.input_foreground():
            where = "вход Google" if self.mode == MODE_LOGIN else "библиотеку YouTube Music"
            raise ScreenError(f"Сначала откройте {where} на станции")

    def _require_login(self) -> None:
        if self.mode != MODE_LOGIN:
            raise ScreenError("Очистка доступна только на экране входа Google")
        if not self.input_foreground():
            raise ScreenError("Сначала откройте вход Google на станции")

    def size(self) -> Tuple[int, int]:
        now = time.time()
        if self._size and now - self._size_at < 60:
            return self._size
        try:
            out, _, _ = self._shell("wm size", timeout=10)
            match = re.search(r"(?:Override|Physical) size:\s*(\d+)x(\d+)", out or "")
            if match:
                self._size = (int(match.group(1)), int(match.group(2)))
            else:
                self._size = (1920, 1080)
        except Exception:  # noqa: BLE001 - fixed station layout fallback
            self._size = (1920, 1080)
        self._size_at = now
        return self._size

    @property
    def width(self) -> int:
        return self.size()[0]

    @property
    def height(self) -> int:
        return self.size()[1]

    def frame(self, cache_seconds: float = 0.25) -> bytes:
        now = time.time()
        if self._frame is not None and now - self._frame_at < cache_seconds:
            return self._frame
        with self._lock:
            now = time.time()
            if self._frame is not None and now - self._frame_at < cache_seconds:
                return self._frame
            try:
                self._shell(f"screencap -p {FRAME_PATH} && chmod 644 {FRAME_PATH}", timeout=20)
                data = self.panel.adb.read_bytes(FRAME_PATH, timeout=20)
            except Exception as error:  # noqa: BLE001
                raise ScreenError(f"Не удалось снять экран станции: {error}") from error
            finally:
                try:
                    self._shell(f"rm -f {FRAME_PATH}", timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            if not data:
                raise ScreenError("Станция вернула пустой снимок экрана")
            self._frame = data
            self._frame_at = now
            return data

    def dump(self) -> str:
        try:
            if os.path.exists(INPUT_HELPER):
                self._shell(
                    f"env CLASSPATH={INPUT_HELPER} /system/bin/app_process /system/bin InputText --dump",
                    timeout=25,
                )
                out, _, _ = self._shell(f"cat {DUMP_PATH}", timeout=10)
                return out or ""
            out, _, _ = self._shell(
                f"uiautomator dump {DUMP_PATH} >/dev/null 2>&1; cat {DUMP_PATH}",
                timeout=20,
            )
            return out or ""
        except Exception:  # noqa: BLE001
            return ""

    def start_login(self) -> None:
        # Wake the panel if the screen is off, then open the GmsCore sign-in.
        self.mode = MODE_LOGIN
        self._foreground = None
        self._shell("input keyevent 224", timeout=5)
        self._shell(
            f"am start -n {PACKAGE}/{LOGIN_ACTIVITY} --es tmpl new_account",
            timeout=20,
        )
        self._frame = None
        self._frame_at = 0.0

    def start_library(self) -> None:
        # The app's own library screen: the account is the one signed into the
        # APK, so liked music, playlists and mixes match the app exactly.
        self.mode = MODE_LIBRARY
        self._foreground = None
        self._shell("input keyevent 224", timeout=5)
        self._shell(
            f'am start -a android.intent.action.VIEW -d "https://music.youtube.com/library" '
            f"-n {YTM_PACKAGE}/{YTM_ACTIVITY}",
            timeout=20,
        )
        self._frame = None
        self._frame_at = 0.0

    def start_home(self) -> None:
        # The app's home screen contains personalized mixes and shelves.
        self.mode = MODE_LIBRARY
        self._foreground = None
        self._shell("input keyevent 224", timeout=5)
        self._shell(
            f'am start -a android.intent.action.VIEW -d "https://music.youtube.com/" '
            f"-n {YTM_PACKAGE}/{YTM_ACTIVITY}",
            timeout=20,
        )
        self._frame = None
        self._frame_at = 0.0

    def tap(self, x: Any, y: Any) -> None:
        self._require_input()
        try:
            px, py = int(x), int(y)
        except (TypeError, ValueError) as error:
            raise ScreenError("Некорректные координаты") from error
        width, height = self.size()
        if not (0 <= px < width and 0 <= py < height):
            raise ScreenError("Координаты вне экрана станции")
        self._shell(f"input tap {px} {py}", timeout=10)
        self._frame_at = 0.0

    def text(self, value: Any) -> None:
        self._require_input()
        text = str(value or "")
        if not text:
            raise ScreenError("Пустой текст")
        if len(text) > 512:
            raise ScreenError("Текст длиннее 512 символов")
        if "\n" in text or "\r" in text:
            raise ScreenError("Текст не должен содержать перевод строки")
        # Android's `input text` does not deliver characters under root,
        # even in the shell domain. InputText.dex drops to shell uid 2000.
        encoded = text.replace(" ", "%s")
        if os.path.exists(INPUT_HELPER):
            self._shell(
                f"env CLASSPATH={INPUT_HELPER} /system/bin/app_process /system/bin InputText "
                + shlex.quote(encoded),
                timeout=20,
            )
        else:
            self._shell("input text " + shlex.quote(encoded), timeout=15)
        self._frame_at = 0.0

    def key(self, name: Any) -> None:
        self._require_input()
        key = str(name or "").strip().lower()
        code = KEYS.get(key)
        if code is None:
            raise ScreenError("Неизвестная клавиша")
        self._shell(f"input keyevent {code}", timeout=10)
        self._frame_at = 0.0

    def clear(self) -> None:
        self._require_login()
        # Move the caret to the end and erase enough characters for a normal
        # email/password field. One `input keyevent` supports many keycodes.
        self._shell("input keyevent 123 " + " ".join(["67"] * 60), timeout=20)
        self._frame_at = 0.0
