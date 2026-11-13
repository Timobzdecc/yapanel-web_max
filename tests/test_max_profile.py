import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import MagicMock, Mock, patch

import profiles
import max_profile

ROOT = Path(__file__).resolve().parents[1]


def fresh_server():
    spec = importlib.util.spec_from_file_location("isolated_panel", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MaxProfileTests(unittest.TestCase):
    def test_max_profile_registered(self):
        self.assertIn("max", profiles.PROFILES)
        prof = profiles.PROFILES["max"]
        self.assertEqual(prof["name"], "Станция Макс")
        self.assertEqual(prof["platform"], "yandexstation_2")
        self.assertTrue(prof["clock"])
        self.assertTrue(prof["light"])
        self.assertTrue(prof["boot"])
        self.assertFalse(prof["alarms"])
        self.assertFalse(prof["scenes"])
        self.assertTrue(prof["assistant"])
        self.assertTrue(prof["apps"])

    def test_max_detection(self):
        s = fresh_server()
        adb_mock = SimpleNamespace(
            shell=lambda *a, **k: ("amlogic, g12a Amlogic \nyandexstation_2", "", 0)
        )
        model, platform = s.detect_model(adb_mock)
        self.assertEqual(model, "max")
        self.assertEqual(platform, "yandexstation_2")

    def test_max_install_and_methods(self):
        s = fresh_server()
        profiles.install(s, "max")
        self.assertEqual(s.GLAGOL_PLATFORM, "yandexstation_2")

        p = s.Panel(Mock())
        # Test version
        p.adb.shell = lambda cmd, *a, **k: ("yandexstation_2_tv/132\n", "", 0)
        p._configure_glagol = Mock()
        ver = p.version()
        self.assertEqual(ver["model"], "max")
        self.assertEqual(ver["quasar_version"], "yandexstation_2_tv/132")

    def test_max_ring_frame_bytes_are_bgr_and_full_length(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        buf = p._ring_frame_bytes([[1, 2, 3]])
        self.assertEqual(len(buf), 72)
        self.assertEqual(buf[:3], bytes([3, 2, 1]))
        self.assertEqual(buf[3:], bytes(69))
        self.assertEqual(p._ring_frame_bytes(None), bytes(72))

    def test_max_boot_hook_binds_ring_and_clock_overrides(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.shell.return_value = ("watchdog\n", "", 0)
        pushed = {}
        p.adb.push = lambda local, remote, *a, **k: pushed.__setitem__(
            remote, Path(local).read_text(encoding="utf-8"))
        p._ensure_hook()
        hook = pushed[s.BOOT_HOOK]
        self.assertIn("for k in ring clock", hook)
        self.assertIn(f"{s.OVR_DIR}/ledpatterns", hook)
        self.assertIn(s.PATTERNS["ring"], hook)
        self.assertIn(s.PATTERNS["clock"], hook)
        self.assertNotIn(f"{s.PATTERNS['clock']}/ledpatterns/ring", hook)

    def test_max_boot_route_requires_a_real_android_caller(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.shell.return_value = ("", "", 0)

        with self.assertRaisesRegex(s.AdbError, "Автозапуск boot_hook.sh не подтверждён"):
            p._boot_route()

    def test_max_hook_repair_refuses_before_writing_without_boot_route(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.shell.return_value = ("", "", 0)

        with self.assertRaisesRegex(s.AdbError, "Автозапуск boot_hook.sh не подтверждён"):
            p.hook_repair()

        p.adb.push.assert_not_called()

    def test_max_hook_status_reports_unavailable_boot_route(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.shell.side_effect = [
            ("", "", 0),
            ("@ассистент\nhook\n0000  /data/quasar/webpanel/boot_hook.sh\n", "", 0),
        ]

        status = p.hook_status()

        self.assertFalse(status["possible"])
        self.assertIn("Автозапуск boot_hook.sh не подтверждён", status["reason"])
        self.assertTrue(status["hook"])
        self.assertTrue(status["broken"])

    def test_max_hook_status_uses_local_assistant_worker_without_boot_route(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.is_local = True
        p.adb.shell.side_effect = [
            ("", "", 0),
            ("@ассистент\nhook\n0000  /data/quasar/webpanel/boot_hook.sh\n", "", 0),
            ("assistant-worker\n", "", 0),
        ]

        status = p.hook_status()

        self.assertFalse(status["possible"])
        self.assertFalse(status["broken"])
        self.assertIn("worker панели", status["reason"])

    def test_max_hook_status_does_not_hide_assistant_when_worker_prerequisites_are_missing(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.is_local = True
        p.adb.shell.side_effect = [
            ("", "", 0),
            ("@ассистент\nhook\n0000  /data/quasar/webpanel/boot_hook.sh\n", "", 0),
            ("", "", 0),
        ]

        status = p.hook_status()

        self.assertFalse(status["possible"])
        self.assertTrue(status["broken"])

    def test_max_assistant_hook_fallback_checks_real_worker_prerequisites(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.is_local = True
        p.adb.shell.return_value = ("", "", 0)

        p.max_assistant_hook_fallback(["ассистент"])

        command = p.adb.shell.call_args.args[0]
        self.assertIn("/data/quasar", command)
        self.assertIn(s.ASSIST_CONF, command)
        self.assertIn(s.ASSISTD, command)
        self.assertIn(s.ASSIST_NO_AUTOSTART, command)

    def test_max_hook_users_disable_assistant_when_autostart_flag_exists(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())

        hook_users = p._hook_users_script()

        self.assertIn(f"[ ! -f {s.ASSIST_NO_AUTOSTART} ]", hook_users)

    def test_max_boot_route_accepts_a_caller_that_references_hook(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.shell.return_value = ("watchdog\n", "", 0)

        self.assertEqual(p._boot_route(), "watchdog")

    def test_max_ring_try_writes_bgr_bytes_not_list(self):
        import base64
        import time
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.adb.is_local = False
        p.adb.shell = Mock(return_value=("", "", 0))
        text = "loop\nff0000 " + "000000 " * 23 + "40\n"
        self.assertTrue(p.ring_try(text, 0.15)["ok"])
        time.sleep(0.6)
        commands = [call.args[0] for call in p.adb.shell.call_args_list
                    if "base64 -d" in call.args[0]]
        self.assertTrue(commands)
        frames = []
        for command in commands:
            payload = command.split("echo '", 1)[1].split("'", 1)[0]
            data = base64.b64decode(payload)
            self.assertEqual(len(data), 72)
            frames.append(data)
        self.assertIn(bytes([0, 0, 255]) + bytes(69), frames)

    def test_max_say_falls_back_to_tts_when_glagol_is_offline(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.glagol = SimpleNamespace(ws=None)
        p.adb.is_local = False
        p.detach = Mock()

        result = p.say("Привет мир")

        self.assertEqual(result, {"ok": True, "method": "tts"})
        p.detach.assert_called_once()
        tag, script = p.detach.call_args.args
        self.assertEqual(tag, "say")
        self.assertIn(quote("Привет мир"), script)

    def test_max_say_uses_android_player_when_local(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        p.glagol = SimpleNamespace(ws=None)
        p.adb.is_local = True
        with patch.object(max_profile, "_max_android_say") as say:
            result = p.say("Привет мир")
        self.assertEqual(result, {"ok": True, "method": "tts_android"})
        say.assert_called_once_with("Привет мир")

    def test_max_android_player_installs_verified_dex(self):
        import tempfile

        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "bin" / "StationAudio.dex"
            with patch.object(max_profile, "_MAX_AUDIO_DEX_PATH", target):
                self.assertEqual(max_profile._max_audio_player(), target)
                self.assertTrue(target.is_file())
                self.assertEqual(
                    max_profile.hashlib.sha256(target.read_bytes()).hexdigest(),
                    max_profile._MAX_AUDIO_DEX_SHA256,
                )
                self.assertEqual(max_profile._max_audio_player(), target)

    def test_max_android_say_downloads_and_plays_audio(self):
        import tempfile

        audio = bytes.fromhex("fff3") + b"0" * 512
        response = MagicMock()
        response.headers = {"Content-Type": "audio/mpeg"}
        response.read.return_value = audio
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with tempfile.TemporaryDirectory() as work:
            work_dir = Path(work)
            with patch.object(max_profile, "_MAX_AUDIO_TMP_DIR", work_dir), \
                 patch.object(max_profile, "_max_audio_player", return_value=work_dir / "player.dex"), \
                 patch.object(max_profile.urllib.request, "urlopen", return_value=response) as urlopen, \
                 patch.object(max_profile.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
                max_profile._max_android_say("Привет мир")

            self.assertIn(quote("Привет мир"), urlopen.call_args.args[0].full_url)
            self.assertEqual(run.call_args.args[0][2], "StationAudio")
            self.assertFalse(list(work_dir.glob("say-*.mp3")))

    def test_max_render_matrix_text(self):
        buf = max_profile.render_matrix_text("12:34", 180)
        self.assertEqual(len(buf), 1200)
        # Check that there are non-zero pixels
        non_zero = sum(1 for b in buf if b > 0)
        self.assertGreater(non_zero, 30)

        # Test Cyrillic and Latin
        buf_ru = max_profile.render_matrix_text("ПРИВЕТ", 200)
        self.assertEqual(len(buf_ru), 1200)
        self.assertGreater(sum(1 for b in buf_ru if b > 0), 50)

    def test_v36_to_matrix(self):
        v = [0] * 36
        v[0] = 255  # Segment A of digit 0
        v[32] = 200 # Colon dot
        raw = max_profile.v36_to_matrix(v)
        self.assertEqual(len(raw), 1200)
        self.assertGreater(sum(1 for b in raw if b > 0), 0)

    def test_max_ring_write(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        calls = []
        p.adb.shell = lambda cmd, *a, **k: (calls.append(cmd), ("", "", 0))[1]
        res = p.ring({"red": 255, "green": 0, "blue": 128})
        self.assertTrue(res["ok"])
        self.assertTrue(any("/dev/graphics/fb5" in c for c in calls))

    def test_max_bt_info_parser(self):
        sample_dumpsys = """
    A2dpMediaBrowserService com.android.bluetooth/A2dpMediaBrowserService (userId=0)
      ownerPid=3801, ownerUid=1002, userId=0
      package=com.android.bluetooth
      active=true
      flags=3
      state=PlaybackState {state=3, position=123450, buffered position=0, speed=1.0}
      metadata:size=7, description=Blinding Lights, The Weeknd, After Hours
"""
        adb_mock = SimpleNamespace(shell=lambda *a, **k: (sample_dumpsys, "", 0))
        info = max_profile.max_bt_info(adb_mock)
        self.assertIsNotNone(info)
        self.assertEqual(info["title"], "Blinding Lights")
        self.assertEqual(info["artist"], "The Weeknd")
        self.assertEqual(info["album"], "After Hours")
        self.assertTrue(info["playing"])
        self.assertEqual(info["position"], 123.45)

    def test_max_footprint_structure_and_hook(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        # Mock shell response for max_footprint
        # 18 tokens:
        # [boot_mounted, boot_custom, sound_mounted, sound_custom, always, orig, s_orig,
        #  size_kb, hosts_mounted, ovr_sounds_cnt, ovr_sounds_kb, ovr_anim_cnt, my_anim_cnt,
        #  bt_off, viz_present, viz_kb, hook_active, agent_bin]
        mock_output = "0 0 0 0 0 0 0 1024 1 0 0 0 1 0 1 2048 1 0"
        p.adb.shell = lambda cmd, *a, **k: (mock_output, "", 0)
        fp = p.footprint()
        self.assertIn("items", fp)
        self.assertEqual(fp["dir"], s.WEB_DIR)
        self.assertEqual(fp["size_kb"], 1024)

        items_map = {i["key"]: i for i in fp["items"]}
        self.assertIn("hook", items_map)
        self.assertTrue(items_map["hook"]["on"])
        self.assertIn("boot_hook.sh", items_map["hook"]["name"])
        self.assertIn("adbd_watchdog", items_map["hook"]["name"])

        self.assertIn("backup", items_map)
        self.assertNotIn("rootkeeper.sh", items_map["backup"]["detail"])

        self.assertIn("viz", items_map)
        self.assertTrue(items_map["viz"]["on"])
        self.assertEqual(items_map["viz"]["size_kb"], 2048)

    def test_max_footprint_reset(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())
        commands = []
        p.adb.shell = lambda cmd, *a, **k: (commands.append(cmd), ("0 " * 18, "", 0))[1]
        p.ring_reset = Mock()
        p._agent_forget = Mock()
        p.adb.only_adb = lambda: unittest.mock.MagicMock(__enter__=lambda self: None, __exit__=lambda self, *a: None)

        res = p._footprint_reset()
        self.assertTrue(any("svc bluetooth enable" in c for c in commands))
        self.assertFalse(any("/q restart" in c for c in commands))
        self.assertFalse(any("rootkeeper.sh.webpanel.bak" in c for c in commands))
        p.ring_reset.assert_called_once()

    def test_max_android_audio_and_no_buildroot(self):
        s = fresh_server()
        profiles.install(s, "max")
        p = s.Panel(Mock())

        # Verify Android TV paths on server module
        self.assertEqual(s.HOSTS_TARGET, "/system/etc/hosts")
        self.assertIn("com.yandex.tv.services/no_backup/appmetrica", s.METRICA_BATCH)
        self.assertIn("com.yandex.tv.services/no_backup/appmetrica", s.ANALYTICS_PURGE)
        self.assertIn("com.yandex.io.sdk/databases/appmetrica", s.METRICA_BATCH)
        p.adb.shell.return_value = ("watchdog\n", "", 0)
        self.assertEqual(p._boot_route(), "watchdog")

        # Verify audio player is native Android playaudio
        commands = []
        p.adb.shell = lambda cmd, *a, **k: (commands.append(cmd), ("", "", 0))[1]
        p.detach = lambda kind, cmd: commands.append(cmd)
        p._play_remote_wav("/data/local/tmp/test.wav")
        self.assertTrue(any("/odm/bin/playaudio" in c for c in commands))

        # Verify hook users strictly excludes Buildroot daemons
        hook_users_sh = p._hook_users_script()
        self.assertNotIn("yam2", hook_users_sh)
        self.assertIn("wakeword", hook_users_sh)
        self.assertIn("frida", hook_users_sh)
        self.assertNotIn("alarmd", hook_users_sh)
        self.assertIn("webpanel/hosts", hook_users_sh)

    def test_analytics_hosts_text_has_ipv4_and_ipv6(self):
        s = fresh_server()
        text = s.analytics_hosts_text("127.0.0.1 localhost\r\n")
        self.assertIn("127.0.0.1 localhost", text)
        self.assertIn("127.0.0.1\tclck.yandex.net", text)
        self.assertIn("::1\tclck.yandex.net", text)
        self.assertIn("::1\treport.appmetrica.yandex.net", text)
        self.assertIn("webpanel", text)

    def test_max_boot_hook_does_not_write_assistant_pid(self):
        source = (ROOT / "max_profile.py").read_text(encoding="utf-8")
        start = source.index('f"[ -f {s.ASSIST_CONF} ]')
        stop = source.index('f"[ -f {s.BT_MEMES_AUTOSTART} ]', start)
        assistant_hook = source[start:stop]
        self.assertIn("start-stop-daemon -S -b -x", assistant_hook)
        self.assertNotIn(" -m -p ", assistant_hook)
        self.assertNotIn("rm -f {s.ASSIST_SD}", assistant_hook)
        self.assertIn("s.ASSIST_RUN", assistant_hook)
        self.assertIn("[ ! -f {s.ASSIST_NO_AUTOSTART} ]", assistant_hook)

    def test_max_boot_hook_does_not_override_existing_odm_hosts(self):
        source = (ROOT / "max_profile.py").read_text(encoding="utf-8")
        self.assertIn("! grep -q appmetrica.yandex.net /odm/etc/hosts", source)

    def test_max_parse_bt_log_line(self):
        line_off = "2026-11-12 13:04:12.345 20801 20965 I quark-native: [sdk] [info] routeDirective (xx043d5c91)stop_bluetooth.activation via BluetoothCapability, vins=abc"
        line_on = "2026-11-12 13:05:01.100 20801 20965 I quark-native: [sdk] [info] routeDirective (xx12345678)start_bluetooth.activation via BluetoothCapability, vins=def"
        line_pre = "2026-11-12 13:04:11.477 20801 20953 I quark-native: Add directive divui_hide_all_views because of stop_bluetooth"
        self.assertEqual(max_profile.max_parse_bt_log_line(line_off), ("2026-11-12 13:04:12.345", False))
        self.assertEqual(max_profile.max_parse_bt_log_line(line_on), ("2026-11-12 13:05:01.100", True))
        self.assertEqual(max_profile.max_parse_bt_log_line(line_pre), ("2026-11-12 13:04:11.477", False))
        self.assertIsNone(max_profile.max_parse_bt_log_line("2026-11-12 13:04:12.000 I quark-native: unrelated"))

    def test_max_bt_soft_sync_alice_to_panel(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            s = fresh_server()
            s.DATA_DIR = td
            profiles.install(s, "max")
            p = s.Panel(Mock(is_local=False))
            p._pair_until = 0
            dumpsys_ble_only = "\n".join([
                "  enabled: true",
                "  state: ON",
                "  name: Яндекс Станция Макс",
                "  ScanMode: SCAN_MODE_NONE",
                "  ConnectionState: STATE_CONNECTED",
                "Profile: GattService",
                "Profile: A2dpSinkService",
                "  StateMachine: name=A2dpSinkStateMachine state=Disconnected",
            ])
            logcat_lines = [
                "2026-11-12 13:04:12.345 20801 20965 I quark-native: routeDirective (xx04)stop_bluetooth.activation via BluetoothCapability",
            ]
            cmds = []

            def fake_shell(cmd, *a, **k):
                cmds.append(cmd)
                if "dumpsys bluetooth_manager" in cmd:
                    return (dumpsys_ble_only, "", 0)
                if "logcat -d" in cmd:
                    return ("\n".join(logcat_lines), "", 0)
                return ("", "", 0)

            p.adb.shell = fake_shell
            st = p.bt_state()
            self.assertFalse(st["powered"])
            self.assertFalse(st["discoverable"])
            self.assertFalse(st["pairable"])
            self.assertFalse(any("svc bluetooth disable" in c for c in cmds))

            # Alice turns BT back on via voice -> panel follows
            p._bt_last_remote_poll = 0.0
            logcat_lines.append(
                "2026-11-12 13:05:01.100 20801 20965 I quark-native: routeDirective (xx05)start_bluetooth.activation via BluetoothCapability"
            )
            st2 = p.bt_state()
            self.assertTrue(st2["powered"])
            self.assertTrue(st2["pairable"])

    def test_max_bt_action_soft_sync_panel_to_alice(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            s = fresh_server()
            s.DATA_DIR = td
            profiles.install(s, "max")
            p = s.Panel(Mock(is_local=False))
            cmds = []
            dumpsys_out = "  enabled: true\n  state: ON\n  ScanMode: SCAN_MODE_NONE\n"

            def fake_shell(cmd, *a, **k):
                cmds.append(cmd)
                if "adbd_watchdog.sh" in cmd and "boot_hook.sh" in cmd:
                    return ("watchdog\n", "", 0)
                if "dumpsys bluetooth_manager" in cmd:
                    return (dumpsys_out, "", 0)
                return ("", "", 0)

            p.adb.shell = fake_shell
            p.glagol = None
            with self.assertRaises(s.AdbError):
                p.bt_action({"action": "power_off"})

            sent = []
            p.glagol = SimpleNamespace(ws=object(), send=lambda payload: sent.append(payload))
            st_off = p.bt_action({"action": "power_off"})
            self.assertEqual(sent[-1], {"command": "sendText", "text": "выключи блютуз"})
            self.assertFalse(st_off["powered"])
            self.assertFalse(any("svc bluetooth disable" in c for c in cmds))

            st_on = p.bt_action({"action": "power_on"})
            self.assertEqual(sent[-1], {"command": "sendText", "text": "включи блютуз"})
            self.assertTrue(st_on["powered"])

            # Verify boot_hook no longer kills BLE adapter on Station Max
            pushed_hooks = []
            p.adb.push = lambda src, dst, **k: pushed_hooks.append((dst, Path(src).read_text(encoding="utf-8")))
            p._ensure_hook()
            self.assertTrue(pushed_hooks)
            self.assertNotIn("svc bluetooth disable", pushed_hooks[-1][1])


if __name__ == "__main__":
    unittest.main()
