"""LED matrix 25x16 (/dev/graphics/fb4) OSD for YouTube Music on Station Max.

Shows a short scrolling track title on track switches, an animated heart on
like/unlike, and crisp play/pause/dislike icons when controlled via BLE remote.
Never interrupts user-pinned display modes (timer, temp, date, equalizer, party)
and always restores Quasar's clock when finished.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("yapanel.extmusic.osd")

MAX_SCREEN_COLS = 25
MAX_SCREEN_ROWS = 16
MAX_SCREEN_BYTES = 1200
MAX_SCREEN_DEV = "/dev/graphics/fb4"

try:
    from max_profile import (
        FONT_4X7 as _BASE_FONT,
        MAX_SCREEN_DEV as _PROFILE_SCREEN_DEV,
        matrix_grid_to_bytes,
        max_pause_alice_leds,
        max_resume_alice_leds,
        render_matrix_text,
    )
    MAX_SCREEN_DEV = _PROFILE_SCREEN_DEV
except Exception:  # pragma: no cover - standalone fallback
    _BASE_FONT = {}

    def matrix_grid_to_bytes(grid):
        buf = bytearray(MAX_SCREEN_BYTES)
        for r in range(MAX_SCREEN_ROWS):
            for c in range(MAX_SCREEN_COLS):
                val = max(0, min(255, int(grid[r][c])))
                idx = (r * MAX_SCREEN_COLS + c) * 3
                buf[idx] = val
                buf[idx + 1] = val
                buf[idx + 2] = val
        return bytes(buf)

    def render_matrix_text(text="12:34", brightness=180):
        return bytes(MAX_SCREEN_BYTES)

    def max_pause_alice_leds(adb):
        return False

    def max_resume_alice_leds(adb):
        return False


# Compact 4x7/5x7 supplemental glyphs ensured present even if imported in tests
_EXTRA_GLYPHS: Dict[str, List[str]] = {
    " ": ["00", "00", "00", "00", "00", "00", "00"],
    "-": ["000", "000", "000", "111", "000", "000", "000"],
    "&": ["0110", "1001", "0110", "1010", "1001", "1001", "0111"],
    "'": ["1", "1", "0", "0", "0", "0", "0"],
    '"': ["101", "101", "000", "000", "000", "000", "000"],
    "[": ["110", "100", "100", "100", "100", "100", "110"],
    "]": ["011", "001", "001", "001", "001", "001", "011"],
    "#": ["01010", "11111", "01010", "01010", "11111", "01010", "00000"],
    "♪": ["00110", "00101", "00100", "00100", "01100", "11100", "01100"],
    "♥": ["01010", "11111", "11111", "11111", "01110", "00100", "00000"],
}
OSD_FONT: Dict[str, List[str]] = {**_EXTRA_GLYPHS, **_BASE_FONT}

# Pixel-art shapes for 25x16 white LED matrix
_HEART_SMALL = [
    "0110110",
    "1111111",
    "1111111",
    "0111110",
    "0011100",
    "0001000",
]

_HEART_MED = [
    "011000110",
    "111101111",
    "111111111",
    "111111111",
    "011111110",
    "001111100",
    "000111000",
    "000010000",
]

_HEART_FULL = [
    "0011100011100",
    "0111110111110",
    "1111111111111",
    "1111111111111",
    "1111111111111",
    "0111111111110",
    "0011111111100",
    "0001111111000",
    "0000111110000",
    "0000011100000",
    "0000001000000",
]

_HEART_HOLLOW = [
    "0011100011100",
    "0100010100010",
    "1000001000001",
    "1000000000001",
    "1000000000001",
    "0100000000010",
    "0010000000100",
    "0001000001000",
    "0000100010000",
    "0000010100000",
    "0000001000000",
]

_HEART_DISLIKE = [
    "1011100011100",
    "0110010100010",
    "1010001000001",
    "1001000000001",
    "1000100000001",
    "0100010000010",
    "0010001000100",
    "0001000101000",
    "0000100010000",
    "0000010101000",
    "0000001000101",
]

_ICON_PLAY = [
    "110000000",
    "111100000",
    "111111000",
    "111111110",
    "111111111",
    "111111110",
    "111111000",
    "111100000",
    "110000000",
]

_ICON_PAUSE = [
    "111000111",
    "111000111",
    "111000111",
    "111000111",
    "111000111",
    "111000111",
    "111000111",
    "111000111",
    "111000111",
]

_ICON_MIX = [
    "00001111100",
    "00001000100",
    "00001111100",
    "00001000100",
    "00001000100",
    "01111011100",
    "11111111100",
    "01110011000",
]

_DASH_RE = re.compile(r"[—–−]+")
_SPACE_RE = re.compile(r"\s+")


def _clean_display_meta(title: str, artist: str = "") -> Tuple[str, str]:
    t = str(title or "").strip()
    a = str(artist or "").strip()
    try:
        from extmusic.ytmusic import clean_track_meta

        ct, ca = clean_track_meta(t, a)
        if ct:
            t = ct
        if ca:
            a = ca
    except Exception:  # noqa: BLE001 - fallback to raw strings
        pass
    return t, a


def normalize_osd_text(text: str, max_chars: int = 44) -> str:
    """Normalize text for crisp 4x7 matrix rendering."""
    s = str(text or "")
    s = _DASH_RE.sub("-", s)
    s = s.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    s = _SPACE_RE.sub(" ", s).strip()
    out: List[str] = []
    for ch in s:
        upper = ch.upper()
        if upper in OSD_FONT:
            out.append(upper)
        elif ch == "ё":
            out.append("Ё")
        elif ch.isspace():
            out.append(" ")
    cleaned = _SPACE_RE.sub(" ", "".join(out)).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip(" -") + "."
    return cleaned


def build_text_strip(text: str, font: Optional[Dict[str, List[str]]] = None) -> List[List[int]]:
    """Render normalized string into a 7-row binary bitmap strip."""
    use_font = font or OSD_FONT
    rows: List[List[int]] = [[] for _ in range(7)]
    first = True
    for ch in str(text or ""):
        glyph = use_font.get(ch.upper(), use_font.get(" ", ["00"] * 7))
        if not first:
            for r in range(7):
                rows[r].append(0)
        first = False
        gw = len(glyph[0]) if glyph else 2
        for r in range(7):
            line = glyph[r] if r < len(glyph) else ("0" * gw)
            for c in range(gw):
                rows[r].append(1 if c < len(line) and line[c] == "1" else 0)
    return rows


def _blit_bitmap(
    bitmap: List[str],
    brightness: int,
    sparkles: Optional[List[Tuple[int, int, int]]] = None,
) -> bytes:
    grid = [[0] * MAX_SCREEN_COLS for _ in range(MAX_SCREEN_ROWS)]
    bh = len(bitmap)
    bw = len(bitmap[0]) if bh else 0
    y0 = (MAX_SCREEN_ROWS - bh) // 2
    x0 = (MAX_SCREEN_COLS - bw) // 2
    val = max(0, min(255, int(brightness)))
    for r, row in enumerate(bitmap):
        yy = y0 + r
        if not (0 <= yy < MAX_SCREEN_ROWS):
            continue
        for c, ch in enumerate(row):
            xx = x0 + c
            if ch == "1" and 0 <= xx < MAX_SCREEN_COLS:
                grid[yy][xx] = val
    if sparkles:
        for sx, sy, sb in sparkles:
            if 0 <= sy < MAX_SCREEN_ROWS and 0 <= sx < MAX_SCREEN_COLS:
                grid[sy][sx] = max(grid[sy][sx], max(0, min(255, int(sb))))
    return matrix_grid_to_bytes(grid)


def _strip_window_frame(strip: List[List[int]], offset_x: int, brightness: int) -> bytes:
    grid = [[0] * MAX_SCREEN_COLS for _ in range(MAX_SCREEN_ROWS)]
    strip_w = len(strip[0]) if strip else 0
    y0 = (MAX_SCREEN_ROWS - 7) // 2  # row 4
    val = max(0, min(255, int(brightness)))
    for r in range(7):
        row = strip[r]
        for x in range(MAX_SCREEN_COLS):
            sx = offset_x + x
            if 0 <= sx < strip_w and row[sx]:
                grid[y0 + r][x] = val
    return matrix_grid_to_bytes(grid)


def render_marquee_frames(
    title: str,
    artist: str = "",
    brightness: int = 195,
    max_duration: float = 4.6,
) -> List[Tuple[bytes, float]]:
    """Build smooth scrolling marquee frames for a track title on the 25x16 matrix."""
    clean_title, clean_artist = _clean_display_meta(title, artist)
    if clean_artist and clean_artist.lower() not in ("youtube music", "vk music"):
        if clean_artist.lower() in clean_title.lower():
            raw_text = f"♪ {clean_title}"
        else:
            raw_text = f"♪ {clean_artist} - {clean_title}"
    else:
        raw_text = f"♪ {clean_title or 'MUSIC'}"

    norm = normalize_osd_text(raw_text, max_chars=44) or "♪ MUSIC"
    strip = build_text_strip(norm)
    strip_w = len(strip[0]) if strip else 0
    if strip_w == 0:
        return []

    br = max(40, min(255, int(brightness)))
    frames: List[Tuple[bytes, float]] = []

    if strip_w <= MAX_SCREEN_COLS:
        pad = -((MAX_SCREEN_COLS - strip_w) // 2)
        frames.append((_strip_window_frame(strip, pad, br // 3), 0.05))
        frames.append((_strip_window_frame(strip, pad, (br * 2) // 3), 0.05))
        frames.append((_strip_window_frame(strip, pad, br), min(2.4, max(1.2, max_duration - 0.3))))
        frames.append((_strip_window_frame(strip, pad, br // 2), 0.06))
        frames.append((_strip_window_frame(strip, pad, br // 5), 0.06))
        return frames

    # Long text: hold start briefly so it's readable immediately, then scroll left smoothly
    end_offset = strip_w - MAX_SCREEN_COLS
    frames.append((_strip_window_frame(strip, 0, br // 2), 0.05))
    frames.append((_strip_window_frame(strip, 0, br), 0.38))

    scroll_budget = max(1.0, float(max_duration) - 0.95)
    step_dt = max(0.032, min(0.058, scroll_budget / max(1, end_offset)))
    # If text is very wide, advance by 2px per tick only if 1px would exceed max_duration
    stride = 1 if (end_offset * 0.032 <= scroll_budget) else 2
    if stride == 2:
        step_dt = max(0.036, min(0.058, scroll_budget / max(1, (end_offset + 1) // 2)))

    for offset in range(stride, end_offset + 1, stride):
        frames.append((_strip_window_frame(strip, offset, br), step_dt))
    if end_offset % stride != 0:
        frames.append((_strip_window_frame(strip, end_offset, br), step_dt))

    frames.append((_strip_window_frame(strip, end_offset, br), 0.38))
    frames.append((_strip_window_frame(strip, end_offset, br // 2), 0.06))
    frames.append((_strip_window_frame(strip, end_offset, br // 5), 0.06))
    return frames


def render_heart_frames(liked: bool = True, brightness: int = 230) -> List[Tuple[bytes, float]]:
    """Build pixel-art heart animation frames for like / unlike feedback."""
    br = max(60, min(255, int(brightness)))
    if not liked:
        return [
            (_blit_bitmap(_HEART_HOLLOW, int(br * 0.8)), 0.34),
            (_blit_bitmap(_HEART_HOLLOW, int(br * 0.45)), 0.12),
            (_blit_bitmap(_HEART_HOLLOW, int(br * 0.18)), 0.10),
        ]

    sparkles_a = [(3, 2, 160), (21, 2, 160), (4, 13, 120), (20, 13, 120)]
    sparkles_b = [(2, 1, 200), (22, 1, 200), (3, 14, 150), (21, 14, 150)]
    return [
        (_blit_bitmap(_HEART_SMALL, int(br * 0.7)), 0.07),
        (_blit_bitmap(_HEART_MED, int(br * 0.9)), 0.07),
        (_blit_bitmap(_HEART_FULL, br), 0.16),
        (_blit_bitmap(_HEART_FULL, 255, sparkles=sparkles_a), 0.15),
        (_blit_bitmap(_HEART_FULL, int(br * 0.5)), 0.11),
        (_blit_bitmap(_HEART_FULL, 255, sparkles=sparkles_b), 0.22),
        (_blit_bitmap(_HEART_FULL, int(br * 0.88)), 0.42),
        (_blit_bitmap(_HEART_FULL, int(br * 0.55)), 0.09),
        (_blit_bitmap(_HEART_FULL, int(br * 0.22)), 0.08),
    ]


def render_icon_frames(kind: str, brightness: int = 210) -> List[Tuple[bytes, float]]:
    """Build short feedback icon frames ('play', 'pause', 'dislike', 'mix')."""
    br = max(60, min(255, int(brightness)))
    bmp = {
        "play": _ICON_PLAY,
        "pause": _ICON_PAUSE,
        "dislike": _HEART_DISLIKE,
        "mix": _ICON_MIX,
    }.get(str(kind or "").lower())
    if bmp is None:
        return []
    hold = 0.65 if kind == "dislike" else 0.55
    return [
        (_blit_bitmap(bmp, int(br * 0.5)), 0.05),
        (_blit_bitmap(bmp, br), hold),
        (_blit_bitmap(bmp, int(br * 0.45)), 0.06),
        (_blit_bitmap(bmp, int(br * 0.18)), 0.05),
    ]


class MaxLedOSD:
    """Thread-safe, preemptible OSD runner for Station Max 25x16 LED matrix."""

    def __init__(self, panel):
        self.panel = panel
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._gen = 0
        self._pending: Optional[Tuple[int, List[Tuple[bytes, float]], str]] = None
        self._active = False
        self._last_kind = ""
        self._last_at = 0.0
        self._enabled = self._load_enabled()
        self._thread: Optional[threading.Thread] = None

    def _load_enabled(self) -> bool:
        try:
            loader = getattr(self.panel, "_load_settings", None)
            if callable(loader):
                s = loader()
            else:
                import server

                s = server.load_settings()
            hw = s.get("extmusic_hw") if isinstance(s, dict) else None
            if isinstance(hw, dict) and "osd_enabled" in hw:
                return bool(hw["osd_enabled"])
        except Exception:  # noqa: BLE001
            pass
        return True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        if not self._enabled:
            with self._lock:
                self._gen += 1
                self._pending = None

    def is_supported(self) -> bool:
        if os.path.exists(MAX_SCREEN_DEV):
            return True
        if callable(getattr(self.panel, "_get_disp_mgr", None)):
            return True
        return False

    def _disp_mgr(self):
        getter = getattr(self.panel, "_get_disp_mgr", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
        return None

    def _is_matrix_busy(self) -> bool:
        """True when user pinned a display mode (timer/eq/temp/date/party)."""
        mgr = self._disp_mgr()
        if mgr is not None:
            lock = getattr(mgr, "lock", None)
            if lock is not None:
                with lock:
                    if getattr(mgr, "running", False) and getattr(mgr, "mode", None) not in (None, "osd"):
                        return True
            elif getattr(mgr, "running", False) and getattr(mgr, "mode", None) not in (None, "osd"):
                return True
        party = getattr(self.panel, "party", None)
        if party is not None:
            settings = getattr(party, "settings", None)
            if isinstance(settings, dict) and settings.get("running"):
                return True
        return False

    def _write_frame(self, raw: bytes) -> None:
        if len(raw) < MAX_SCREEN_BYTES:
            return
        mgr = self._disp_mgr()
        if mgr is not None and hasattr(mgr, "_write_frame"):
            mgr._write_frame(raw)
            return
        if os.path.exists(MAX_SCREEN_DEV):
            try:
                with open(MAX_SCREEN_DEV, "wb") as f:
                    f.write(raw[:MAX_SCREEN_BYTES])
            except OSError:
                pass

    def _pause_clock(self) -> None:
        adb = getattr(self.panel, "adb", None)
        if adb is not None:
            try:
                max_pause_alice_leds(adb)
            except Exception:  # noqa: BLE001
                pass

    def _resume_clock(self) -> None:
        try:
            now_str = time.strftime("%H:%M")
            self._write_frame(render_matrix_text(now_str, 180))
        except Exception:  # noqa: BLE001
            pass
        adb = getattr(self.panel, "adb", None)
        if adb is not None:
            try:
                max_resume_alice_leds(adb)
            except Exception:  # noqa: BLE001
                pass

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="max-led-osd")
        self._thread.start()

    def enqueue(self, frames: List[Tuple[bytes, float]], kind: str = "osd", force: bool = False) -> bool:
        if not frames or not self.is_supported():
            return False
        if not force and not self._enabled:
            return False
        if self._is_matrix_busy():
            return False
        with self._lock:
            self._gen += 1
            self._pending = (self._gen, list(frames), str(kind or "osd"))
            self._ensure_worker()
            self._wake.set()
        return True

    def show_track(self, title: str, artist: str = "", force: bool = False) -> bool:
        if not title and not artist:
            return False
        frames = render_marquee_frames(title, artist)
        return self.enqueue(frames, kind="track", force=force)

    def show_like(self, liked: bool = True, force: bool = False) -> bool:
        frames = render_heart_frames(liked=bool(liked))
        return self.enqueue(frames, kind="like" if liked else "unlike", force=force)

    def show_icon(self, kind: str, force: bool = False) -> bool:
        frames = render_icon_frames(kind)
        return self.enqueue(frames, kind=kind, force=force)

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._pending = None
            self._stop_evt.set()
            self._wake.set()

    def status(self) -> Dict[str, Any]:
        return {
            "supported": self.is_supported(),
            "enabled": self._enabled,
            "active": self._active,
            "last_kind": self._last_kind,
            "last_at": self._last_at,
        }

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop_evt.is_set():
                break

            with self._lock:
                item = self._pending
                self._pending = None
            if not item:
                continue

            if self._is_matrix_busy():
                continue

            paused = False
            try:
                self._pause_clock()
                paused = True
                self._active = True
                while item is not None and not self._stop_evt.is_set():
                    gen, frames, kind = item
                    self._last_kind = kind
                    self._last_at = time.time()
                    for raw, dur in frames:
                        if self._stop_evt.is_set():
                            break
                        with self._lock:
                            if self._gen != gen:
                                break
                        if self._is_matrix_busy():
                            # User took over the matrix mid-OSD — release without overriding their screen.
                            paused = False
                            break
                        self._write_frame(raw)
                        deadline = time.monotonic() + max(0.01, float(dur))
                        while True:
                            rem = deadline - time.monotonic()
                            if rem <= 0 or self._stop_evt.is_set():
                                break
                            with self._lock:
                                if self._gen != gen:
                                    break
                            time.sleep(min(0.025, rem))

                    with self._lock:
                        item = self._pending
                        self._pending = None
                    if not paused:
                        break
            except Exception as err:  # noqa: BLE001 - OSD must never crash server
                _log.debug("LED OSD error: %s", err)
            finally:
                self._active = False
                if paused:
                    self._resume_clock()
