"""Unit tests for Yandex Remote 2 BLE evdev/uinput integration (extmusic/remote.py)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from extmusic.remote import (
    EV_KEY,
    EV_SYN,
    KEY_DOWN,
    KEY_ENTER,
    KEY_LEFT,
    KEY_NEXTSONG,
    KEY_PLAYPAUSE,
    KEY_PREVIOUSSONG,
    KEY_RIGHT,
    KEY_SELECT,
    KEY_STOP,
    KEY_UP,
    MaxBleRemote,
    RemoteGestureState,
    find_remote_devices,
)

SAMPLE_PROC_INPUT = """
I: Bus=0010 Vendor=0001 Product=0001 Version=0100
N: Name="gpio_keypad"
P: Phys=gpio_keypad/input0
S: Sysfs=/devices/platform/gpio-keys/input/input0
U: Uniq=
H: Handlers=event0

I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="rotary-volume"
S: Sysfs=/devices/platform/rotary-volume/input/input2
H: Handlers=kbd event2

I: Bus=0005 Vendor=057a Product=006f Version=0000
N: Name="Yandex Remote 2"
S: Sysfs=/devices/virtual/misc/uhid/0005:057A:006F.0001/input/input4
U: Uniq=54:a9:c8:97:c1:4e
H: Handlers=kbd event4

I: Bus=0005 Vendor=057a Product=006f Version=0000
N: Name="yapanel-remote-pass"
S: Sysfs=/devices/virtual/input/input5
U: Uniq=
H: Handlers=kbd event5
"""


class _FakeUInput:
    def __init__(self):
        self.ready = True
        self.events = []

    def emit(self, ev_type, code, value, sync=False):
        self.events.append((ev_type, code, value, sync))


class _FakeOSD:
    def __init__(self):
        self.calls = []

    def show_icon(self, kind, force=False):
        self.calls.append(("icon", kind, force))

    def show_track(self, title, artist="", force=False):
        self.calls.append(("track", title, artist, force))


class _FakeExtSvc:
    def __init__(self, playing=True):
        self.current = {"source": "yt", "id": "abc123", "title": "Song", "artist": "Band"}
        self.playing = playing
        self.liked = False
        self.position = 30000
        self.duration = 200000
        self.calls = []

    def status(self):
        return {"current": self.current, "player": {"playing": self.playing, "position": self.position, "duration": self.duration}}

    def pause(self):
        self.playing = False
        self.calls.append(("pause",))
        return self.status()

    def resume(self):
        self.playing = True
        self.calls.append(("resume",))
        return self.status()

    def stop(self):
        self.current = None
        self.calls.append(("stop",))
        return self.status()

    def next(self):
        self.calls.append(("next",))
        return self.status()

    def prev(self):
        self.calls.append(("prev",))
        return self.status()

    def seek(self, pos):
        self.position = pos
        self.calls.append(("seek", pos))
        return self.status()

    def yt_track_liked(self, vid):
        return self.liked

    def yt_like(self, vid, on=True):
        self.liked = on
        self.calls.append(("like", vid, on))
        return {"ok": True, "liked": on}

    def yt_dislike(self, vid, skip=True):
        self.calls.append(("dislike", vid, skip))
        return {"ok": True}

    def yt_mix_play(self, index=None):
        self.calls.append(("mix", index))
        return self.status()


class _FakePanel:
    def __init__(self, svc=None, osd=None):
        self._extmusic_service = svc
        self._extmusic_osd = osd
        self._settings = {}

    def _load_settings(self):
        return self._settings


class TestRemoteDiscoveryAndGestures(unittest.TestCase):
    def test_find_remote_devices_matches_yandex_and_ignores_virtual_uinput(self):
        devs = find_remote_devices(SAMPLE_PROC_INPUT)
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["path"], "/dev/input/event4")
        self.assertEqual(devs[0]["name"], "Yandex Remote 2")
        self.assertEqual(devs[0]["vendor"], "057a")
        self.assertEqual(devs[0]["product"], "006f")

    def test_gesture_single_double_and_long_press_on_ok(self):
        g = RemoteGestureState(long_press_sec=0.65, double_click_sec=0.28)

        # Single tap -> toggle_play after double-click window
        self.assertEqual(g.on_key(KEY_SELECT, 1, 1.00), [])
        self.assertEqual(g.on_key(KEY_SELECT, 0, 1.08), [])
        self.assertEqual(g.poll(1.20), [])
        self.assertEqual(g.poll(1.40), ["toggle_play"])

        # Double tap -> toggle_like immediately on second release, no stray toggle_play
        self.assertEqual(g.on_key(KEY_PLAYPAUSE, 1, 2.00), [])
        self.assertEqual(g.on_key(KEY_PLAYPAUSE, 0, 2.05), [])
        self.assertEqual(g.on_key(KEY_PLAYPAUSE, 1, 2.15), [])
        self.assertEqual(g.on_key(KEY_PLAYPAUSE, 0, 2.20), ["toggle_like"])
        self.assertEqual(g.poll(2.60), [])

        # Long press -> next_mix during hold, nothing on release
        self.assertEqual(g.on_key(KEY_ENTER, 1, 3.00), [])
        self.assertEqual(g.poll(3.70), ["next_mix"])
        self.assertEqual(g.on_key(KEY_ENTER, 0, 3.85), [])
        self.assertEqual(g.poll(4.20), [])

    def test_gesture_directional_and_media_keys(self):
        g = RemoteGestureState(long_press_sec=0.65, double_click_sec=0.28)
        # Right short = next, Right long = seek_fwd
        self.assertEqual(g.on_key(KEY_RIGHT, 1, 1.0), [])
        self.assertEqual(g.on_key(KEY_RIGHT, 0, 1.1), ["next"])
        self.assertEqual(g.on_key(KEY_NEXTSONG, 1, 2.0), [])
        self.assertEqual(g.on_key(KEY_NEXTSONG, 2, 2.7), ["seek_fwd"])
        self.assertEqual(g.on_key(KEY_NEXTSONG, 0, 2.8), [])

        # Left short = prev, Left long = seek_back
        self.assertEqual(g.on_key(KEY_LEFT, 1, 3.0), [])
        self.assertEqual(g.on_key(KEY_LEFT, 0, 3.1), ["prev"])
        self.assertEqual(g.on_key(KEY_PREVIOUSSONG, 1, 4.0), [])
        self.assertEqual(g.poll(4.7), ["seek_back"])
        self.assertEqual(g.on_key(KEY_PREVIOUSSONG, 0, 4.8), [])

        # Up = like, Down short = show_osd, Down long = dislike
        self.assertEqual(g.on_key(KEY_UP, 1, 5.0), [])
        self.assertEqual(g.on_key(KEY_UP, 0, 5.1), ["like"])
        self.assertEqual(g.on_key(KEY_DOWN, 1, 6.0), [])
        self.assertEqual(g.on_key(KEY_DOWN, 0, 6.1), ["show_osd"])
        self.assertEqual(g.on_key(KEY_DOWN, 1, 7.0), [])
        self.assertEqual(g.poll(7.7), ["dislike"])
        self.assertEqual(g.on_key(KEY_DOWN, 0, 7.8), [])

        # Stop fires immediately on key down
        self.assertEqual(g.on_key(KEY_STOP, 1, 8.0), ["stop"])


class TestRemoteRoutingAndDispatch(unittest.TestCase):
    def test_volume_and_voice_always_forward_while_dpad_respects_hdmi(self):
        with tempfile.TemporaryDirectory() as td:
            hpd = Path(td) / "hpd_state"
            hpd.write_text("0\n", encoding="ascii")
            svc = _FakeExtSvc(playing=True)
            panel = _FakePanel(svc=svc, osd=_FakeOSD())
            remote = MaxBleRemote(panel, hpd_path=str(hpd))
            ui = _FakeUInput()

            # Volume Up (115) & Voice Assist (217) always forwarded to Android uinput
            self.assertEqual(remote.handle_raw_event(EV_KEY, 115, 1, now=1.0, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, 115, 0, now=1.1, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, 217, 1, now=1.2, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, 217, 0, now=1.3, uinput=ui), [])
            self.assertEqual([(c, v) for _t, c, v, _s in ui.events], [(115, 1), (115, 0), (217, 1), (217, 0)])

            # HDMI unplugged (0): DPAD_RIGHT intercepted, NOT forwarded
            ui.events.clear()
            remote._dispatch_async = lambda act: None
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_RIGHT, 1, now=2.0, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_RIGHT, 0, now=2.1, uinput=ui), ["next"])
            self.assertEqual(ui.events, [])

            # HDMI connected (1) & dpad_hdmi=False: DPAD_RIGHT forwarded to Android TV!
            hpd.write_text("1\n", encoding="ascii")
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_RIGHT, 1, now=3.0, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_RIGHT, 0, now=3.1, uinput=ui), [])
            self.assertEqual([(c, v) for _t, c, v, _s in ui.events], [(KEY_RIGHT, 1), (KEY_RIGHT, 0)])

            # Dedicated media key (KEY_NEXTSONG) STILL intercepted even with HDMI=1
            ui.events.clear()
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_NEXTSONG, 1, now=4.0, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_NEXTSONG, 0, now=4.1, uinput=ui), ["next"])
            self.assertEqual(ui.events, [])

            # If dpad_hdmi=True, DPAD_RIGHT is intercepted even with HDMI=1
            remote.configure(dpad_hdmi=True)
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_RIGHT, 1, now=5.0, uinput=ui), [])
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_RIGHT, 0, now=5.1, uinput=ui), ["next"])
            self.assertEqual(ui.events, [])

            # Orphan key-up after playback stops mid-hold is NOT leaked to uinput
            self.assertEqual(remote.handle_raw_event(EV_KEY, KEY_LEFT, 1, now=6.0, uinput=ui), [])
            svc.current = None
            remote.handle_raw_event(EV_KEY, KEY_LEFT, 0, now=6.1, uinput=ui)
            self.assertEqual(ui.events, [])

    def test_execute_action_dispatches_all_commands(self):
        svc = _FakeExtSvc(playing=True)
        osd = _FakeOSD()
        panel = _FakePanel(svc=svc, osd=osd)
        remote = MaxBleRemote(panel)

        self.assertTrue(remote.execute_action("toggle_play")["ok"])
        self.assertEqual(svc.playing, False)
        self.assertEqual(osd.calls[-1], ("icon", "pause", False))

        self.assertTrue(remote.execute_action("toggle_play")["ok"])
        self.assertEqual(svc.playing, True)
        self.assertEqual(osd.calls[-1], ("icon", "play", False))

        self.assertTrue(remote.execute_action("next")["ok"])
        self.assertTrue(remote.execute_action("prev")["ok"])
        self.assertTrue(remote.execute_action("seek_fwd")["ok"])
        self.assertEqual(svc.position, 45000)
        self.assertTrue(remote.execute_action("seek_back")["ok"])
        self.assertEqual(svc.position, 30000)

        self.assertTrue(remote.execute_action("toggle_like")["liked"])
        self.assertFalse(remote.execute_action("toggle_like")["liked"])
        self.assertTrue(remote.execute_action("like")["liked"])
        self.assertTrue(remote.execute_action("show_osd")["ok"])
        self.assertEqual(osd.calls[-1], ("track", "Song", "Band", True))
        self.assertTrue(remote.execute_action("next_mix")["ok"])
        self.assertTrue(remote.execute_action("dislike")["ok"])
        self.assertTrue(remote.execute_action("stop")["ok"])

    def test_hw_api_routes_status_and_configure(self):
        from api.extmusic import register
        from api.router import ApiRequest, Router
        from extmusic.service import ExtMusicService

        class _RoutePanel:
            def __init__(self):
                self.adb = type("_Adb", (), {"is_local": False, "shell": lambda *a, **k: ("", "", 0)})()
                self._settings = {}

            def _load_settings(self):
                return self._settings

        panel = _RoutePanel()
        svc = ExtMusicService(panel)
        panel._extmusic_service = svc

        router = Router()
        register(router)

        get_fn = router.match("GET", "/api/extmusic/hw")
        self.assertIsNotNone(get_fn)
        r_get = get_fn(ApiRequest(panel=panel))
        self.assertTrue(r_get["ok"])
        self.assertIn("remote", r_get)
        self.assertIn("osd", r_get)

        post_fn = router.match("POST", "/api/extmusic/hw")
        self.assertIsNotNone(post_fn)
        r_post = post_fn(
            ApiRequest(
                panel=panel,
                data={"remote_enabled": False, "osd_enabled": False, "remote_dpad_hdmi": True},
            )
        )
        self.assertFalse(r_post["remote"]["enabled"])
        self.assertTrue(r_post["remote"]["dpad_hdmi"])
        self.assertFalse(r_post["osd"]["enabled"])


if __name__ == "__main__":
    unittest.main()
