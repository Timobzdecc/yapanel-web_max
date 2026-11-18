"""Google OAuth 2.0 Device Authorization Flow (RFC 8628).

Allows signing into YouTube Music without any local Android applications or
WebView. The user visits https://www.google.com/device and enters the code.
The panel polls Google's token endpoint and securely saves the refresh token.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from . import ipv4_urlopen

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
SCOPE = "https://www.googleapis.com/auth/youtube"

# YouTube on TV standard client ID & secret (RFC 8628 Device Flow)
DEFAULT_CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
DEFAULT_SCOPE = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/userinfo.email"


class GoogleOAuthError(ValueError):
    """Raised when Google OAuth fails."""


class GoogleOAuth:
    def __init__(self, store) -> None:
        self.store = store
        self.active_session: Dict[str, Any] = {}

    def start(self, client_id: str = "", client_secret: str = "") -> Dict[str, Any]:
        """Request a new device code and user code from Google."""
        client_id = str(client_id or "").strip()
        client_secret = str(client_secret or "").strip()
        if not client_id:
            client_id = DEFAULT_CLIENT_ID
            client_secret = DEFAULT_CLIENT_SECRET
            scope = DEFAULT_SCOPE
        else:
            scope = SCOPE

        data = urllib.parse.urlencode({
            "client_id": client_id,
            "scope": scope,
        }).encode("utf-8")

        req = urllib.request.Request(
            DEVICE_CODE_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with ipv4_urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                desc = body.get("error_description") or body.get("error") or str(error)
            except Exception:
                desc = str(error)
            raise GoogleOAuthError(f"Ошибка запроса кода устройства: {desc}") from error
        except Exception as error:
            raise GoogleOAuthError(f"Ошибка связи с Google OAuth: {error}") from error

        device_code = res.get("device_code")
        user_code = res.get("user_code")
        verification_url = res.get("verification_url") or "https://www.google.com/device"
        qr_url = f"{verification_url}?user_code={user_code}"
        interval = int(res.get("interval") or 5)
        expires_in = int(res.get("expires_in") or 1800)

        session = {
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": verification_url,
            "verification_url_complete": qr_url,
            "qr_url": qr_url,
            "interval": interval,
            "expires_at": time.time() + expires_in,
            "step": "code",
            "message": f"Отсканируйте QR-код или перейдите на {verification_url} и введите код: {user_code}",
        }
        self.active_session = session
        return session

    def poll(self) -> Dict[str, Any]:
        """Poll the token endpoint to check if the user completed authorization."""
        sess = self.active_session
        if not sess or sess.get("step") != "code":
            return {"step": "idle", "message": ""}

        if time.time() > sess.get("expires_at", 0):
            self.active_session = {}
            return {"step": "expired", "message": "Срок действия кода истёк. Запросите новый."}

        data = urllib.parse.urlencode({
            "client_id": sess["client_id"],
            "client_secret": sess.get("client_secret", ""),
            "device_code": sess["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }).encode("utf-8")

        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with ipv4_urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                err_code = body.get("error")
            except Exception:
                err_code = ""

            if err_code == "authorization_pending":
                qr_url = sess.get("qr_url") or f"{sess['verification_url']}?user_code={sess['user_code']}"
                return {
                    "step": "code",
                    "user_code": sess["user_code"],
                    "verification_url": sess["verification_url"],
                    "verification_url_complete": qr_url,
                    "qr_url": qr_url,
                    "message": "Ожидание подтверждения на устройстве...",
                }
            if err_code == "slow_down":
                sess["interval"] = sess.get("interval", 5) + 2
                qr_url = sess.get("qr_url") or f"{sess['verification_url']}?user_code={sess['user_code']}"
                return {
                    "step": "code",
                    "user_code": sess["user_code"],
                    "verification_url": sess["verification_url"],
                    "verification_url_complete": qr_url,
                    "qr_url": qr_url,
                    "message": "Ожидание...",
                }
            if err_code == "expired_token":
                self.active_session = {}
                return {"step": "expired", "message": "Срок действия кода истёк."}

            desc = body.get("error_description") or err_code or str(error)
            self.active_session = {}
            return {"step": "error", "message": f"Ошибка авторизации: {desc}"}
        except Exception as error:
            return {"step": "error", "message": f"Ошибка связи: {error}"}

        access_token = res.get("access_token")
        refresh_token = res.get("refresh_token")
        expires_in = int(res.get("expires_in") or 3600)

        email = self._fetch_userinfo(access_token) or "Google-аккаунт"

        tokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in - 60,
            "client_id": sess["client_id"],
            "client_secret": sess.get("client_secret", ""),
            "email": email,
        }
        self.store.save_oauth(tokens)
        self.active_session = {}

        return {
            "step": "done",
            "message": f"Аккаунт {email} успешно подключён!",
            "account": email,
        }

    def cancel(self) -> Dict[str, Any]:
        self.active_session = {}
        return {"step": "idle", "message": "Вход отменён"}

    def _fetch_userinfo(self, access_token: str) -> str:
        req = urllib.request.Request(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            with ipv4_urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("email") or data.get("name") or ""
        except Exception:
            return ""

    def refresh_access_token(self, tokens: Dict[str, Any]) -> str:
        refresh_token = tokens.get("refresh_token")
        client_id = tokens.get("client_id") or DEFAULT_CLIENT_ID
        client_secret = tokens.get("client_secret") or (DEFAULT_CLIENT_SECRET if client_id == DEFAULT_CLIENT_ID else "")

        if not refresh_token:
            raise GoogleOAuthError("Отсутствует refresh_token для обновления сессии Google")

        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with ipv4_urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except Exception as error:
            raise GoogleOAuthError(f"Не удалось обновить токен Google: {error}") from error

        access_token = res.get("access_token")
        expires_in = int(res.get("expires_in") or 3600)
        tokens["access_token"] = access_token
        tokens["expires_at"] = time.time() + expires_in - 60
        self.store.save_oauth(tokens)
        return access_token
