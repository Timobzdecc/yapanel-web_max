import pytest

from extmusic.google_login import GoogleLogin, GoogleLoginError, _bounds, _button, _input, _nodes


XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node class="android.widget.EditText" resource-id="identifierId"
        text="" password="false" bounds="[372,526][1548,636]" />
  <node class="android.widget.Button" resource-id="identifierNext" text="Далее"
        clickable="true" bounds="[1368,776][1550,850]" />
  <node class="android.webkit.WebView" bounds="[0,0][1920,1080]" />
</hierarchy>"""


def test_nodes_and_fields():
    nodes = _nodes(XML)
    email = _input(nodes, "email")
    assert email is not None
    assert _bounds(email) == (960, 581)
    button = _button(nodes, ("далее", "next"), ("identifiernext",))
    assert button is not None
    assert _bounds(button) == (1459, 813)


def test_password_and_code_fields():
    xml = """<hierarchy>
      <node class="android.widget.EditText" resource-id="password" password="true" bounds="[0,0][100,50]" />
      <node class="android.widget.EditText" resource-id="totpPin" bounds="[0,60][100,110]" />
    </hierarchy>"""
    nodes = _nodes(xml)
    assert _input(nodes, "password") is not None
    assert _input(nodes, "code") is not None
    assert _input(nodes, "email") is None


def test_login_validation():
    class Screen:
        pass

    class Panel:
        adb = None

    login = GoogleLogin(Panel(), Screen())
    with pytest.raises(GoogleLoginError):
        login.start("", "secret")
    with pytest.raises(GoogleLoginError):
        login.start("user@example.com", "")
    assert login.status()["step"] == "idle"


def test_account_exists():
    class Adb:
        def shell(self, command, timeout=15, check=False):
            return "Account {name=user@example.com, type=app.revanced}", "", 0

    class Panel:
        adb = Adb()

    class Screen:
        pass

    login = GoogleLogin(Panel(), Screen())
    assert login._account_exists("user@example.com")
    assert not login._account_exists("other@example.com")
