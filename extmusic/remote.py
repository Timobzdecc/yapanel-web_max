"""BLE remote control (Yandex Remote 2, /dev/input/event*) for YouTube Music.

Dynamically discovers the Yandex BLE remote via /proc/bus/input/devices,
grabs it ONLY while external music (GstBridge / ExtMusicService) is active and
a /dev/uinput passthrough device is ready, routes media/D-pad gestures to
ExtMusicService, and forwards Volume/Mute/Power/Voice/Home/Back natively to
Android InputReader with matching Vendor/Product IDs so Vendor_057a_Product_006f.kl
keeps working seamlessly.
"""
from __future__ import annotations

import logging
import os
import re
import select
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows host tests
    fcntl = None  # type: ignore[assignment]

_log = logging.getLogger("yapanel.extmusic.remote")

# Linux input event constants (AArch64 / Linux 4.9+)
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
EV_MSC = 0x04
SYN_REPORT = 0x00

EVIOCGRAB = 0x40044590
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_MSCBIT = 0x40045568
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
BUS_BLUETOOTH = 0x05

# Linux keycodes matching /vendor/usr/keylayout/Vendor_057a_Product_006f.kl
KEY_ENTER = 28
KEY_UP = 103
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_DOWN = 108
KEY_NEXTSONG = 163
KEY_PLAYPAUSE = 164
KEY_PREVIOUSSONG = 165
KEY_STOP = 166
KEY_REWIND = 168
KEY_PLAY = 200
KEY_PAUSE = 201
KEY_PLAYCD = 207
KEY_FASTFORWARD = 208
KEY_SELECT = 353  # DPAD_CENTER on Yandex Remote 2

MEDIA_KEYS: Set[int] = {
    KEY_PLAYPAUSE,
    KEY_PLAY,
    KEY_PLAYCD,
    KEY_PAUSE,
    KEY_NEXTSONG,
    KEY_PREVIOUSSONG,
    KEY_STOP,
    KEY_FASTFORWARD,
    KEY_REWIND,
}

DPAD_KEYS: Set[int] = {
    KEY_ENTER,
    KEY_SELECT,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_UP,
    KEY_DOWN,
}

OK_KEYS: Set[int] = {KEY_ENTER, KEY_SELECT, KEY_PLAYPAUSE}
NEXT_KEYS: Set[int] = {KEY_RIGHT, KEY_NEXTSONG}
PREV_KEYS: Set[int] = {KEY_LEFT, KEY_PREVIOUSSONG}

LONG_PRESS_SEC = 0.65
DOUBLE_CLICK_SEC = 0.28
SEEK_STEP_MS = 15000

PROC_INPUT_DEVICES = "/proc/bus/input/devices"
HDMI_HPD_PATH = "/sys/class/amhdmitx/amhdmitx0/hpd_state"
UINPUT_PATH = "/dev/uinput"
PASSTHROUGH_NAME = "yapanel-remote-pass"

# On 64-bit Linux (aarch64/x86_64) struct input_event is 24 bytes (llHHi)
_EVENT_STRUCT = struct.Struct("llHHi") if os.name == "posix" else struct.Struct("<qqHHi")

_VENDOR_RE = re.compile(r"Vendor=([0-9a-fA-F]{4})")
_PRODUCT_RE = re.compile(r"Product=([0-9a-fA-F]{4})")
_EVENT_RE = re.compile(r"\bevent(\d+)\b")


def find_remote_devices(proc_text: str) -> List[Dict[str, str]]:
    """Parse /proc/bus/input/devices and return matching Yandex BLE remote devices."""
    devices: List[Dict[str, str]] = []
    blocks = re.split(r"\r?\n\s*\r?\n", str(proc_text or "").strip())
    for block in blocks:
        if not block.strip():
            continue
        name = ""
        sysfs = ""
        handlers = ""
        vendor = ""
        product = ""
        uniq = ""
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if line.startswith("I:"):
                mv = _VENDOR_RE.search(line)
                mp = _PRODUCT_RE.search(line)
                if mv:
                    vendor = mv.group(1).lower()
                if mp:
                    product = mp.group(1).lower()
            elif line.startswith("N: Name="):
                name = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("S: Sysfs="):
                sysfs = line[len("S: Sysfs="):].strip()
            elif line.startswith("H: Handlers="):
                handlers = line[len("H: Handlers="):].strip()
            elif line.startswith("U: Uniq="):
                uniq = line[len("U: Uniq="):].strip()

        # Never bind our own virtual uinput passthrough device
        if name.lower().startswith("yapanel-") or (
            "/devices/virtual/input/" in sysfs and "uhid" not in sysfs
        ):
            continue

        is_yandex = (vendor == "057a") or ("yandex remote" in name.lower())
        if not is_yandex:
            continue
        me = _EVENT_RE.search(handlers)
        if not me:
            continue
        devices.append(
            {
                "path": f"/dev/input/event{me.group(1)}",
                "name": name or "Yandex Remote",
                "vendor": vendor or "057a",
                "product": product or "006f",
                "uniq": uniq,
                "sysfs": sysfs,
            }
        )
    return devices


def read_hdmi_connected(hpd_path: str = HDMI_HPD_PATH) -> bool:
    """Return True if HDMI display is currently connected (hpd_state == 1)."""
    try:
        with open(hpd_path, "r", encoding="ascii", errors="ignore") as f:
            txt = f.read(16).strip()
        return txt.startswith("1")
    except OSError:
        return False


class RemoteGestureState:
    """Pure state machine turning EV_KEY press/release/repeat into player actions.

    Actions emitted:
      - "toggle_play": single press OK / PlayPause
      - "toggle_like": double press OK / PlayPause
      - "next_mix":    long press OK / PlayPause
      - "resume":      KEY_PLAY / KEY_PLAYCD
      - "pause":       KEY_PAUSE
      - "stop":        KEY_STOP
      - "next":        short press Right / NextSong
      - "prev":        short press Left / PrevSong
      - "seek_fwd":    long press Right / NextSong or KEY_FASTFORWARD
      - "seek_back":   long press Left / PrevSong or KEY_REWIND
      - "like":        short press Up
      - "show_osd":    short press Down
      - "dislike":     long press Down
    """

    def __init__(
        self,
        long_press_sec: float = LONG_PRESS_SEC,
        double_click_sec: float = DOUBLE_CLICK_SEC,
    ):
        self.long_press_sec = float(long_press_sec)
        self.double_click_sec = float(double_click_sec)
        self._down_at: Dict[int, float] = {}
        self._long_fired: Set[int] = set()
        self._pending_ok_at: Optional[float] = None

    def reset(self) -> None:
        self._down_at.clear()
        self._long_fired.clear()
        self._pending_ok_at = None

    def on_key(self, code: int, value: int, now: float) -> List[str]:
        """Handle EV_KEY (value: 1=down, 0=up, 2=repeat). Returns list of action names."""
        actions: List[str] = []

        if value == 1:  # key down
            self._down_at[code] = now
            self._long_fired.discard(code)
            if code in (KEY_PLAY, KEY_PLAYCD):
                actions.append("resume")
            elif code == KEY_PAUSE:
                actions.append("pause")
            elif code == KEY_STOP:
                actions.append("stop")
            elif code == KEY_FASTFORWARD:
                actions.append("seek_fwd")
            elif code == KEY_REWIND:
                actions.append("seek_back")
            return actions

        if value == 2:  # key repeat (held)
            down = self._down_at.get(code)
            if down is not None and code not in self._long_fired and (now - down) >= self.long_press_sec:
                act = self._fire_long(code)
                if act:
                    actions.append(act)
            return actions

        if value == 0:  # key up
            down = self._down_at.pop(code, None)
            was_long = code in self._long_fired
            self._long_fired.discard(code)
            if down is None or was_long:
                return actions

            held = now - down
            if held >= self.long_press_sec:
                act = self._long_action_for(code)
                if act:
                    self._pending_ok_at = None
                    actions.append(act)
                    return actions

            if code in OK_KEYS:
                if self._pending_ok_at is not None and (now - self._pending_ok_at) <= self.double_click_sec:
                    self._pending_ok_at = None
                    actions.append("toggle_like")
                else:
                    self._pending_ok_at = now
            elif code in NEXT_KEYS:
                actions.append("next")
            elif code in PREV_KEYS:
                actions.append("prev")
            elif code == KEY_UP:
                actions.append("like")
            elif code == KEY_DOWN:
                actions.append("show_osd")
            return actions

        return actions

    def poll(self, now: float) -> List[str]:
        """Advance timers for active long-press holds and pending double-click."""
        actions: List[str] = []
        for code, down in list(self._down_at.items()):
            if code not in self._long_fired and (now - down) >= self.long_press_sec:
                act = self._fire_long(code)
                if act:
                    actions.append(act)
        if self._pending_ok_at is not None and (now - self._pending_ok_at) >= self.double_click_sec:
            self._pending_ok_at = None
            actions.append("toggle_play")
        return actions

    @staticmethod
    def _long_action_for(code: int) -> Optional[str]:
        if code in OK_KEYS:
            return "next_mix"
        if code in NEXT_KEYS:
            return "seek_fwd"
        if code in PREV_KEYS:
            return "seek_back"
        if code == KEY_DOWN:
            return "dislike"
        return None

    def _fire_long(self, code: int) -> Optional[str]:
        act = self._long_action_for(code)
        if not act:
            return None
        self._long_fired.add(code)
        if code in OK_KEYS:
            self._pending_ok_at = None
        return act


class UInputPassthrough:
    """Virtual /dev/uinput keyboard mirroring Yandex Remote 2 VID/PID."""

    def __init__(self, vendor: int = 0x057A, product: int = 0x006F, path: str = UINPUT_PATH):
        self.vendor = int(vendor)
        self.product = int(product)
        self.path = path
        self._fd: Optional[int] = None

    @property
    def ready(self) -> bool:
        return self._fd is not None

    def open(self) -> bool:
        if self._fd is not None:
            return True
        if fcntl is None or not os.path.exists(self.path):
            return False
        fd = None
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_MSC)
            # Register standard key range 1..599 (covers volume, power, voice 217/582, dpad, back, home)
            for code in range(1, 600):
                try:
                    fcntl.ioctl(fd, UI_SET_KEYBIT, code)
                except OSError:
                    pass
            try:
                fcntl.ioctl(fd, UI_SET_RELBIT, 6)  # REL_HWHEEL
                fcntl.ioctl(fd, UI_SET_MSCBIT, 4)  # MSC_SCAN
            except OSError:
                pass
            uidev = struct.pack(
                "80sHHHHI" + "i" * 256,
                PASSTHROUGH_NAME.encode("ascii"),
                BUS_BLUETOOTH,
                self.vendor,
                self.product,
                0,
                0,
                *([0] * 256),
            )
            os.write(fd, uidev)
            fcntl.ioctl(fd, UI_DEV_CREATE)
            self._fd = fd
            _log.info("BLE remote uinput passthrough created (%04x:%04x)", self.vendor, self.product)
            return True
        except OSError as err:
            _log.warning("Cannot open uinput passthrough (%s): %s", self.path, err)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False

    def emit(self, ev_type: int, code: int, value: int, sync: bool = False) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            os.write(fd, _EVENT_STRUCT.pack(0, 0, ev_type, code, value))
            if sync:
                os.write(fd, _EVENT_STRUCT.pack(0, 0, EV_SYN, SYN_REPORT, 0))
        except OSError:
            pass

    def close(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


class MaxBleRemote:
    """Background listener routing Yandex BLE remote buttons to ExtMusicService."""

    def __init__(
        self,
        panel,
        *,
        proc_path: str = PROC_INPUT_DEVICES,
        hpd_path: str = HDMI_HPD_PATH,
        uinput_factory: Optional[Callable[[int, int], UInputPassthrough]] = None,
    ):
        self.panel = panel
        self.proc_path = proc_path
        self.hpd_path = hpd_path
        self._uinput_factory = uinput_factory or (lambda v, p: UInputPassthrough(v, p))
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gesture = RemoteGestureState()

        cfg = self._load_config()
        self._enabled: bool = bool(cfg.get("remote_enabled", True))
        self._dpad_hdmi: bool = bool(cfg.get("remote_dpad_hdmi", False))

        self._connected = False
        self._device_info: Optional[Dict[str, str]] = None
        self._grabbed = False
        self._uinput: Optional[UInputPassthrough] = None
        self._last_action = ""
        self._last_key_at = 0.0
        self._mix_cursor = 0
        self._suppressed_keys: Set[int] = set()
        self._forwarded_keys: Set[int] = set()
        self._action_lock = threading.Lock()

    def _load_config(self) -> Dict[str, Any]:
        try:
            loader = getattr(self.panel, "_load_settings", None)
            if callable(loader):
                s = loader()
            else:
                import server

                s = server.load_settings()
            hw = s.get("extmusic_hw") if isinstance(s, dict) else None
            if isinstance(hw, dict):
                return dict(hw)
        except Exception:  # noqa: BLE001
            pass
        return {}

    def is_supported(self) -> bool:
        return os.name == "posix" and os.path.exists(self.proc_path)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def dpad_hdmi(self) -> bool:
        return self._dpad_hdmi

    def configure(
        self,
        *,
        enabled: Optional[bool] = None,
        dpad_hdmi: Optional[bool] = None,
    ) -> None:
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            if dpad_hdmi is not None:
                self._dpad_hdmi = bool(dpad_hdmi)

    def start(self) -> bool:
        if not self.is_supported():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="max-ble-remote")
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.5)
        self._close_uinput()

    def _close_uinput(self) -> None:
        ui = self._uinput
        self._uinput = None
        if ui is not None:
            ui.close()

    def _ext_service(self):
        return getattr(self.panel, "_extmusic_service", None)

    def _is_ext_active(self) -> bool:
        svc = self._ext_service()
        if svc is None:
            return False
        cur = getattr(svc, "current", None)
        if not cur or cur.get("native"):
            return False
        return True

    def _should_intercept_key(self, code: int) -> bool:
        if not self._enabled or not self._is_ext_active():
            return False
        if code in MEDIA_KEYS:
            return True
        if code in DPAD_KEYS:
            if self._dpad_hdmi:
                return True
            if not read_hdmi_connected(self.hpd_path):
                return True
            return False
        return False

    def handle_raw_event(
        self,
        ev_type: int,
        code: int,
        value: int,
        now: Optional[float] = None,
        uinput: Optional[UInputPassthrough] = None,
    ) -> List[str]:
        """Process one input_event; intercept or forward via uinput. Returns dispatched actions."""
        ts = time.monotonic() if now is None else float(now)
        ui = uinput if uinput is not None else self._uinput
        actions: List[str] = []

        if ev_type == EV_KEY:
            if value == 1:
                if self._should_intercept_key(code):
                    self._suppressed_keys.add(code)
                    actions = self._gesture.on_key(code, value, ts)
                else:
                    self._forwarded_keys.add(code)
                    if ui is not None:
                        ui.emit(ev_type, code, value, sync=True)
            elif value == 2:
                if code in self._suppressed_keys:
                    actions = self._gesture.on_key(code, value, ts)
                elif code in self._forwarded_keys and ui is not None:
                    ui.emit(ev_type, code, value, sync=True)
            elif value == 0:
                if code in self._suppressed_keys:
                    self._suppressed_keys.discard(code)
                    actions = self._gesture.on_key(code, value, ts)
                else:
                    self._forwarded_keys.discard(code)
                    if ui is not None:
                        ui.emit(ev_type, code, value, sync=True)
        elif ev_type == EV_SYN:
            # Forwarded EV_KEY events already sync immediately; forward SYN only when not suppressed.
            if not self._suppressed_keys and ui is not None:
                ui.emit(ev_type, code, value, sync=False)
        else:
            if ui is not None:
                ui.emit(ev_type, code, value, sync=False)

        for act in actions:
            self._dispatch_async(act)
        return actions

    def _dispatch_async(self, action: str) -> None:
        self._last_action = action
        self._last_key_at = time.time()
        threading.Thread(
            target=self.execute_action,
            args=(action,),
            daemon=True,
            name=f"ble-act-{action}",
        ).start()

    def execute_action(self, action: str) -> Dict[str, Any]:
        """Execute a mapped remote gesture against ExtMusicService and optional LED OSD."""
        svc = self._ext_service()
        if svc is None:
            return {"ok": False, "error": "no extmusic service"}
        osd = getattr(self.panel, "_extmusic_osd", None)

        with self._action_lock:
            try:
                cur = getattr(svc, "current", None) or {}
                if action == "toggle_play":
                    st = svc.status()
                    playing = bool((st.get("player") or {}).get("playing"))
                    if playing:
                        res = svc.pause()
                        if osd is not None:
                            osd.show_icon("pause")
                        return {"ok": True, "action": "pause", "res": res}
                    res = svc.resume()
                    if osd is not None:
                        osd.show_icon("play")
                    return {"ok": True, "action": "resume", "res": res}

                if action == "pause":
                    res = svc.pause()
                    if osd is not None:
                        osd.show_icon("pause")
                    return {"ok": True, "action": "pause", "res": res}

                if action == "resume":
                    res = svc.resume()
                    if osd is not None:
                        osd.show_icon("play")
                    return {"ok": True, "action": "resume", "res": res}

                if action == "stop":
                    return {"ok": True, "action": "stop", "res": svc.stop()}

                if action == "next":
                    return {"ok": True, "action": "next", "res": svc.next()}

                if action == "prev":
                    return {"ok": True, "action": "prev", "res": svc.prev()}

                if action in ("seek_fwd", "seek_back"):
                    st = svc.status()
                    pos = int((st.get("player") or {}).get("position") or 0)
                    dur = int((st.get("player") or {}).get("duration") or 0)
                    delta = SEEK_STEP_MS if action == "seek_fwd" else -SEEK_STEP_MS
                    target = max(0, pos + delta)
                    if dur > 0:
                        target = min(max(0, dur - 2000), target)
                    return {"ok": True, "action": action, "res": svc.seek(target)}

                if action in ("like", "toggle_like"):
                    vid = str(cur.get("id") or "")
                    if not vid or str(cur.get("source") or "") != "yt":
                        return {"ok": False, "error": "no yt track"}
                    if action == "toggle_like":
                        already = bool(svc.yt_track_liked(vid))
                        new_on = not already
                    else:
                        new_on = True
                    res = svc.yt_like(vid, on=new_on)
                    return {"ok": True, "action": action, "liked": new_on, "res": res}

                if action == "dislike":
                    vid = str(cur.get("id") or "")
                    if not vid or str(cur.get("source") or "") != "yt":
                        return {"ok": False, "error": "no yt track"}
                    res = svc.yt_dislike(vid, skip=True)
                    return {"ok": True, "action": "dislike", "res": res}

                if action == "next_mix":
                    self._mix_cursor = (self._mix_cursor % 4) + 1
                    if osd is not None:
                        osd.show_icon("mix")
                    res = svc.yt_mix_play(index=self._mix_cursor)
                    return {"ok": True, "action": "next_mix", "index": self._mix_cursor, "res": res}

                if action == "show_osd":
                    if osd is not None and cur:
                        osd.show_track(str(cur.get("title") or ""), str(cur.get("artist") or ""), force=True)
                    return {"ok": True, "action": "show_osd"}

                return {"ok": False, "error": f"unknown action {action}"}
            except Exception as err:  # noqa: BLE001 - remote action must never crash thread
                _log.warning("BLE remote action %s failed: %s", action, err)
                return {"ok": False, "action": action, "error": str(err)}

    def _discover_device(self) -> Optional[Dict[str, str]]:
        try:
            with open(self.proc_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None
        devs = find_remote_devices(text)
        return devs[0] if devs else None

    def _set_grab(self, fd: int, grab: bool) -> bool:
        if fcntl is None:
            return False
        if grab == self._grabbed:
            return True
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1 if grab else 0)
            self._grabbed = grab
            if not grab:
                self._gesture.reset()
                self._suppressed_keys.clear()
                self._forwarded_keys.clear()
            _log.info("BLE remote grab=%s on %s", grab, (self._device_info or {}).get("path", "?"))
            return True
        except OSError as err:
            _log.warning("BLE remote EVIOCGRAB(%s) failed: %s", grab, err)
            return False

    def _loop(self) -> None:
        ev_size = _EVENT_STRUCT.size
        while not self._stop_evt.is_set():
            info = self._discover_device()
            if not info:
                self._connected = False
                self._device_info = None
                self._grabbed = False
                self._stop_evt.wait(3.0)
                continue

            path = info["path"]
            vendor = int(info.get("vendor", "057a"), 16)
            product = int(info.get("product", "006f"), 16)
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self._device_info = info
                self._connected = True
                self._grabbed = False
                self._gesture.reset()
                self._suppressed_keys.clear()
                self._forwarded_keys.clear()
                _log.info("BLE remote attached: %s (%s)", info["name"], path)

                buf = bytearray()
                while not self._stop_evt.is_set():
                    want_grab = self._enabled and self._is_ext_active()
                    if want_grab:
                        if self._uinput is None or not self._uinput.ready:
                            self._uinput = self._uinput_factory(vendor, product)
                            if self._uinput.open():
                                # Give Android InputReader a moment to register the virtual device.
                                self._stop_evt.wait(0.12)
                        if self._uinput and self._uinput.ready:
                            self._set_grab(fd, True)
                        else:
                            # Never grab without working passthrough!
                            self._set_grab(fd, False)
                    else:
                        if self._grabbed:
                            self._set_grab(fd, False)

                    try:
                        ready, _, _ = select.select([fd], [], [], 0.08)
                    except (OSError, ValueError):
                        break

                    now = time.monotonic()
                    if ready:
                        try:
                            chunk = os.read(fd, ev_size * 16)
                        except BlockingIOError:
                            chunk = b""
                        except OSError:
                            break
                        if not chunk:
                            break
                        buf.extend(chunk)
                        while len(buf) >= ev_size:
                            raw = bytes(buf[:ev_size])
                            del buf[:ev_size]
                            _sec, _usec, ev_type, code, value = _EVENT_STRUCT.unpack(raw)
                            if self._grabbed:
                                self.handle_raw_event(ev_type, code, value, now=now)

                    if self._grabbed:
                        for act in self._gesture.poll(now):
                            self._dispatch_async(act)

            except OSError as err:
                _log.debug("BLE remote %s disconnected/unavailable: %s", path, err)
            finally:
                if fd is not None:
                    try:
                        if self._grabbed:
                            self._set_grab(fd, False)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._connected = False
                self._grabbed = False
                self._gesture.reset()
                self._suppressed_keys.clear()
                self._forwarded_keys.clear()
                if not self._stop_evt.is_set():
                    self._stop_evt.wait(2.0)

    def status(self) -> Dict[str, Any]:
        info = self._device_info or {}
        if not self._connected and self.is_supported():
            # Lightweight probe so UI shows connected remote even if worker is starting
            found = self._discover_device()
            if found:
                info = found
        return {
            "supported": self.is_supported(),
            "enabled": self._enabled,
            "dpad_hdmi": self._dpad_hdmi,
            "connected": bool(self._connected or info),
            "device_name": str(info.get("name") or ""),
            "device_path": str(info.get("path") or ""),
            "grabbed": self._grabbed,
            "uinput_ready": bool(self._uinput and self._uinput.ready),
            "hdmi_connected": read_hdmi_connected(self.hpd_path) if self.is_supported() else False,
            "last_action": self._last_action,
            "last_key_at": self._last_key_at,
        }
