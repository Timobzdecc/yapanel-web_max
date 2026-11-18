import pytest

from extmusic.screen import ScreenError, StationScreen


class FakeAdb:
    def __init__(self, focus="app.revanced.android.gms/org.microg.gms.auth.login.LoginActivity"):
        self.commands = []
        self.reads = []
        self.image = b"\x89PNG\r\n"
        self.focus = focus

    def shell(self, command, timeout=15, check=False):
        self.commands.append(command)
        if "dumpsys window windows" in command:
            return "  mCurrentFocus=Window{1 u0 " + self.focus + "}", "", 0
        return "Physical size: 1920x1080\n", "", 0

    def read_bytes(self, path, timeout=120):
        self.reads.append(path)
        return self.image


class FakePanel:
    def __init__(self):
        self.adb = FakeAdb()


def test_screen_size_and_frame():
    panel = FakePanel()
    screen = StationScreen(panel)
    assert screen.size() == (1920, 1080)
    assert screen.frame() == b"\x89PNG\r\n"
    assert any("screencap -p" in command for command in panel.adb.commands)
    assert panel.adb.reads == ["/data/local/tmp/panel-screen.png"]
    assert any("rm -f /data/local/tmp/panel-screen.png" in command for command in panel.adb.commands)


def test_screen_tap_checks_bounds():
    screen = StationScreen(FakePanel())
    screen.tap(960, 540)
    assert any("input tap 960 540" in command for command in screen.panel.adb.commands)
    with pytest.raises(ScreenError):
        screen.tap(-1, 540)
    with pytest.raises(ScreenError):
        screen.tap(1920, 540)
    with pytest.raises(ScreenError):
        screen.tap("x", 540)


def test_screen_text_escapes_spaces_and_rejects_newlines():
    screen = StationScreen(FakePanel())
    screen.text("user name@example.com")
    assert any("input text" in command and "user%sname@example.com" in command
               for command in screen.panel.adb.commands)
    with pytest.raises(ScreenError):
        screen.text("line\nbreak")
    with pytest.raises(ScreenError):
        screen.text("")


def test_screen_input_blocked_outside_login():
    panel = FakePanel()
    panel.adb.focus = "com.yandex.tv.home/.HomeActivity"
    screen = StationScreen(panel)
    for call in (lambda: screen.tap(10, 10), lambda: screen.text("secret"), lambda: screen.key("back"), screen.clear):
        with pytest.raises(ScreenError):
            call()
    assert not any("input tap" in command or "input text" in command or "input keyevent 67" in command
                   for command in panel.adb.commands)


def test_screen_library_mode_allows_app_input():
    panel = FakePanel()
    screen = StationScreen(panel)
    screen.start_library()
    panel.adb.focus = "app.rvx.android.apps.youtube.music/com.google.android.apps.youtube.music.activities.MusicActivity"
    screen.tap(100, 200)
    assert any("input tap 100 200" in command for command in panel.adb.commands)
    with pytest.raises(ScreenError):
        screen.clear()


def test_screen_key_and_start():
    panel = FakePanel()
    screen = StationScreen(panel)
    screen.key("back")
    screen.key("enter")
    with pytest.raises(ScreenError):
        screen.key("nope")
    screen.clear()
    screen.start_login()
    assert any("input keyevent 4" in command for command in panel.adb.commands)
    assert any("input keyevent 66" in command for command in panel.adb.commands)
    assert any("input keyevent 123 67" in command for command in panel.adb.commands)
    assert any("LoginActivity" in command and "tmpl new_account" in command
               for command in panel.adb.commands)
