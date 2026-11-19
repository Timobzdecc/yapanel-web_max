"""Unit tests for Station Max 25x16 LED matrix OSD (extmusic/led_osd.py)."""
from __future__ import annotations

import threading
import time
import unittest

from extmusic import led_osd
from extmusic.led_osd import (
    MAX_SCREEN_BYTES,
    MaxLedOSD,
    build_text_strip,
    normalize_osd_text,
    render_heart_frames,
    render_icon_frames,
    render_marquee_frames,
)


class _FakeDispMgr:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.mode = None
        self.frames = []

    def _write_frame(self, raw: bytes):
        self.frames.append(raw)


class _FakePanel:
    def __init__(self, mgr=None):
        self._mgr = mgr or _FakeDispMgr()
        self.adb = object()
        self.party = None
        self._settings = {}

    def _get_disp_mgr(self):
        return self._mgr

    def _load_settings(self):
        return self._settings


class TestLedOsdRendering(unittest.TestCase):
    def test_normalize_and_strip_support_cyrillic_latin_and_symbols(self):
        norm = normalize_osd_text("♪ Кино — Группа крови (Live)!")
        self.assertIn("♪", norm)
        self.assertIn("КИНО - ГРУППА КРОВИ", norm)
        strip = build_text_strip(norm)
        self.assertEqual(len(strip), 7)
        self.assertGreater(len(strip[0]), 25)
        self.assertTrue(any(cell == 1 for row in strip for cell in row))

    def test_marquee_short_and_long_frames_are_valid_rgb888(self):
        short_frames = render_marquee_frames("Hi", "")
        self.assertGreaterEqual(len(short_frames), 3)
        for raw, dur in short_frames:
            self.assertEqual(len(raw), MAX_SCREEN_BYTES)
            self.assertGreater(dur, 0.0)

        long_frames = render_marquee_frames(
            "Summertime Sadness (Official Music Video)",
            "LanaDelReyVEVO",
            max_duration=4.5,
        )
        self.assertGreater(len(long_frames), 10)
        total_dur = sum(dur for _raw, dur in long_frames)
        self.assertLessEqual(total_dur, 5.5)
        for raw, _dur in long_frames:
            self.assertEqual(len(raw), MAX_SCREEN_BYTES)
        # First and middle frames must differ because the text is scrolling
        self.assertNotEqual(long_frames[2][0], long_frames[len(long_frames) // 2][0])

    def test_heart_and_icon_frames_render_pixels(self):
        for liked in (True, False):
            frames = render_heart_frames(liked=liked)
            self.assertGreaterEqual(len(frames), 3)
            for raw, dur in frames:
                self.assertEqual(len(raw), MAX_SCREEN_BYTES)
                self.assertGreater(dur, 0.0)
            self.assertTrue(any(b > 0 for b in frames[0][0]))

        for kind in ("play", "pause", "dislike", "mix"):
            frames = render_icon_frames(kind)
            self.assertGreaterEqual(len(frames), 3)
            self.assertEqual(len(frames[1][0]), MAX_SCREEN_BYTES)
            self.assertTrue(any(b > 0 for b in frames[1][0]))


class TestLedOsdCoordinator(unittest.TestCase):
    def test_plays_frames_and_restores_quasar_clock(self):
        mgr = _FakeDispMgr()
        panel = _FakePanel(mgr)
        osd = MaxLedOSD(panel)
        events = []
        osd._pause_clock = lambda: events.append("pause")
        osd._resume_clock = lambda: events.append("resume")

        f1 = (bytes([100] * MAX_SCREEN_BYTES), 0.02)
        f2 = (bytes([200] * MAX_SCREEN_BYTES), 0.02)
        self.assertTrue(osd.enqueue([f1, f2], kind="track"))

        deadline = time.time() + 2.0
        while "resume" not in events and time.time() < deadline:
            time.sleep(0.02)

        osd.stop()
        self.assertEqual(events, ["pause", "resume"])
        self.assertEqual(len(mgr.frames), 2)

    def test_skips_when_user_pinned_display_mode_or_party_running(self):
        mgr = _FakeDispMgr()
        mgr.running = True
        mgr.mode = "timer"
        panel = _FakePanel(mgr)
        osd = MaxLedOSD(panel)

        self.assertFalse(osd.show_track("Song", "Artist"))

        mgr.running = False
        mgr.mode = None
        panel.party = type("_P", (), {"settings": {"running": True}})()
        self.assertFalse(osd.show_like(True))

        panel.party = None
        osd.set_enabled(False)
        self.assertFalse(osd.show_like(True))
        # force=True (UI Test button) still works when matrix is free
        osd._pause_clock = lambda: None
        osd._resume_clock = lambda: None
        self.assertTrue(osd.show_like(True, force=True))
        osd.stop()

    def test_rapid_enqueue_preempts_previous_sequence(self):
        mgr = _FakeDispMgr()
        panel = _FakePanel(mgr)
        osd = MaxLedOSD(panel)
        events = []
        osd._pause_clock = lambda: events.append("pause")
        osd._resume_clock = lambda: events.append("resume")

        slow = [(bytes([10] * MAX_SCREEN_BYTES), 0.5)] * 10
        fast = [(bytes([222] * MAX_SCREEN_BYTES), 0.02)]
        osd.enqueue(slow, kind="track")
        time.sleep(0.04)
        osd.enqueue(fast, kind="like")

        deadline = time.time() + 2.0
        while "resume" not in events and time.time() < deadline:
            time.sleep(0.02)

        osd.stop()
        self.assertIn(bytes([222] * MAX_SCREEN_BYTES), mgr.frames)
        self.assertEqual(events[-1], "resume")


if __name__ == "__main__":
    unittest.main()
