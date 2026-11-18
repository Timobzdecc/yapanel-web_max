"""Direct VK authorization for the official Android client.

VK does not allow browser OAuth for the official app (``401 incorrect app``),
so the only way to get a token that works with the audio API is the same
password grant the app itself uses:

    login + password -> [captcha] -> [SMS/2FA code] -> access_token

This module keeps the short-lived login state in memory only. The password is
never written to disk, never logged and discarded as soon as the token is
issued (or the login is cancelled/expired). Only the resulting token is passed
to :class:`extmusic.store.ExtMusicStore`.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import extmusic

AUTH_URL = "https://oauth.vk.com/token"
VALIDATE_PHONE_URL = "https://api.vk.com/method/auth.validatePhone"
CLIENT_ID = "2274003"
CLIENT_SECRET = "hHbZxrka2uZ6jB1inYsH"
USER_AGENT = "VKAndroidApp/4.13.1-1206 (Android 4.4.3; SDK 19; armeabi; ; ru)"
SCOPE = "audio,offline"
VERSION = "5.131"
SESSION_TTL = 15 * 60
MAX_SESSIONS = 8
MAX_ATTEMPTS = 6


class VKAuthError(ValueError):
    """A login error that is safe to show to the user."""


def _text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _post_form(url: str, params: Dict[str, Any], user_agent: str, timeout: int = 25) -> Dict[str, Any]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    try:
        with extmusic.ipv4_urlopen(request, timeout=timeout) as response:
            raw = response.read(1 << 20)
    except urllib.error.HTTPError as error:
        # VK returns the useful JSON body with 401/400 status codes.
        raw = error.read(1 << 20)
        if not raw:
            raise VKAuthError(f"VK ответил HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise VKAuthError(f"Нет связи с VK: {error}") from error
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as error:
        raise VKAuthError("VK вернул некорректный ответ") from error
    if not isinstance(payload, dict):
        raise VKAuthError("VK вернул некорректный ответ")
    return payload


def _extract_token(value: str) -> str:
    """Accept a raw token or a URL/redirect containing ``success_token``."""
    value = _text(value, 2000)
    if not value:
        return ""
    if "success_token=" not in value and "access_token=" not in value:
        return value
    for marker in ("success_token", "access_token"):
        match = None
        for part in value.replace("#", "&").replace("?", "&").split("&"):
            name, _, raw = part.partition("=")
            if name.strip() == marker and raw:
                match = raw
                break
        if match:
            return urllib.parse.unquote(match).strip()
    return value


@dataclass
class LoginSession:
    id: str
    login: str
    password: str = field(repr=False)
    code: str = ""
    captcha_key: str = ""
    captcha_token: str = ""
    captcha_sid: str = ""
    captcha_img: str = ""
    captcha_url: str = ""
    validation_sid: str = ""
    validation_type: str = ""
    device_id: str = field(default_factory=lambda: secrets.token_hex(8))
    created: float = field(default_factory=time.time)
    attempts: int = 0

    def __repr__(self) -> str:  # never leak the password into logs/tracebacks
        return f"LoginSession(id={self.id!r}, login={self.login!r}, attempts={self.attempts})"


class VKDirectAuth:
    """Stateful password-grant login for the official VK Android client."""

    USER_AGENT = USER_AGENT

    def __init__(
        self,
        transport: Optional[Callable[[str, Dict[str, Any], str, int], Dict[str, Any]]] = None,
        ttl: int = SESSION_TTL,
    ):
        self._transport = transport or _post_form
        self._ttl = ttl
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = threading.Lock()

    # ---------- public API ----------
    def start(self, login: str, password: str) -> Dict[str, Any]:
        login = _text(login, 120)
        password = str(password or "")
        if not login or not password:
            raise VKAuthError("Введите телефон/почту и пароль VK")
        if len(password) > 512:
            raise VKAuthError("Слишком длинный пароль")
        with self._lock:
            self._cleanup()
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda item: item.created)
                self._drop(oldest.id)
            session = LoginSession(id=secrets.token_urlsafe(18), login=login, password=password)
            self._sessions[session.id] = session
        return self._request(session)

    def submit_code(self, session_id: str, code: str) -> Dict[str, Any]:
        code = _text(code, 40)
        if not code:
            raise VKAuthError("Введите код из SMS или приложения")
        session = self._get(session_id)
        session.code = code
        return self._request(session)

    def submit_captcha(self, session_id: str, value: str) -> Dict[str, Any]:
        value = _extract_token(value)
        if not value:
            raise VKAuthError("Введите ответ капчи или ссылку с success_token")
        session = self._get(session_id)
        if session.captcha_sid and session.captcha_img:
            session.captcha_key = value
        else:
            session.captcha_token = value
        return self._request(session)

    def cancel(self, session_id: str) -> None:
        with self._lock:
            self._drop(session_id)

    # ---------- internals ----------
    def _get(self, session_id: str) -> LoginSession:
        session_id = _text(session_id, 120)
        with self._lock:
            self._cleanup()
            session = self._sessions.get(session_id)
            if session is None:
                raise VKAuthError("Сессия входа истекла — начните заново")
            return session

    def _drop(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.password = ""
            session.code = ""
            session.captcha_key = ""
            session.captcha_token = ""

    def _cleanup(self) -> None:
        deadline = time.time() - self._ttl
        for session_id in [key for key, item in self._sessions.items() if item.created < deadline]:
            self._drop(session_id)

    def _request(self, session: LoginSession) -> Dict[str, Any]:
        session.attempts += 1
        if session.attempts > MAX_ATTEMPTS:
            self._drop(session.id)
            raise VKAuthError("Слишком много попыток — начните вход заново")
        params: Dict[str, Any] = {
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": session.login,
            "password": session.password,
            "scope": SCOPE,
            "2fa_supported": 1,
            "force_sms": 1,
            "v": VERSION,
            "lang": "ru",
            "device_id": session.device_id,
        }
        if session.code:
            params["code"] = session.code
        if session.captcha_token:
            params["success_token"] = session.captcha_token
        elif session.captcha_key:
            params["captcha_sid"] = session.captcha_sid
            params["captcha_key"] = session.captcha_key

        payload = self._transport(AUTH_URL, params, USER_AGENT, 25)
        token = _text(payload.get("access_token"), 1000)
        if token:
            user_id = _text(payload.get("user_id"), 40)
            self._drop(session.id)
            return {"step": "done", "token": token, "user_id": user_id}

        error = _text(payload.get("error"), 120)
        description = _text(payload.get("error_description"), 300)
        if error == "need_captcha":
            session.captcha_sid = _text(payload.get("captcha_sid"), 120)
            session.captcha_img = _text(payload.get("captcha_img"), 1000)
            session.captcha_url = _text(payload.get("redirect_uri"), 2000)
            session.captcha_key = ""
            session.captcha_token = ""
            session.code = ""
            return {
                "step": "captcha",
                "session": session.id,
                "image": session.captcha_img,
                "url": session.captcha_url,
                "hint": description or "VK просит подтвердить, что вы не робот",
            }
        if error == "need_validation":
            session.validation_sid = _text(payload.get("validation_sid"), 120)
            session.validation_type = _text(payload.get("validation_type"), 40)
            session.code = ""
            self._send_code(session)
            return {
                "step": "code",
                "session": session.id,
                "type": session.validation_type,
                "hint": self._code_hint(session.validation_type, description),
            }
        if error == "invalid_request" and session.code:
            session.code = ""
            return {
                "step": "code",
                "session": session.id,
                "type": session.validation_type,
                "hint": "Неверный код. Попробуйте ещё раз.",
            }
        if error == "invalid_client":
            self._drop(session.id)
            raise VKAuthError("VK не принял логин или пароль")
        if "Flood control" in error or payload.get("error_type") == "password_bruteforce_attempt":
            self._drop(session.id)
            raise VKAuthError(
                "VK временно заблокировал вход по паролю: слишком много попыток. "
                "Подождите несколько часов. Можно также создать в VK пароль приложения "
                "(Настройки → Безопасность → Пароли для приложений) и войти с ним."
            )
        self._drop(session.id)
        raise VKAuthError(description or f"VK отклонил вход ({error or 'unknown'})")

    def _send_code(self, session: LoginSession) -> None:
        """Ask VK to deliver the 2FA code (SMS or app push). Errors are ignored."""
        if not session.validation_sid:
            return
        try:
            self._transport(
                VALIDATE_PHONE_URL,
                {"sid": session.validation_sid, "v": VERSION},
                USER_AGENT,
                15,
            )
        except VKAuthError:
            pass

    @staticmethod
    def _code_hint(validation_type: str, description: str) -> str:
        if validation_type == "2fa_app":
            return "Введите код из приложения VK (или SMS, если пришёл)"
        if validation_type == "2fa_callreset":
            return "VK позвонит: введите последние 4 цифры номера"
        return description or "VK отправил код подтверждения — введите его"
