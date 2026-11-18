"""Google sign-in through the panel's own form.

The password is typed into the GmsCore login WebView with Android's `input`
command and is never stored in a config file or returned to the browser. The
form only drives the standard Google screens; 2FA/captcha are reported back to
the panel so the user can finish the step there.
"""
from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional, Tuple

from .screen import ScreenError


class GoogleLoginError(ValueError):
    pass


def _bounds(node: ET.Element) -> Optional[Tuple[int, int]]:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _nodes(xml: str):
    try:
        return list(ET.fromstring(xml).iter("node"))
    except (ET.ParseError, ValueError):
        return []


def _texts(nodes) -> str:
    values = []
    for node in nodes:
        value = node.get("text") or node.get("content-desc") or ""
        if value:
            values.append(value)
    return "\n".join(values).lower()


def _input(nodes, kind: str):
    for node in nodes:
        rid = (node.get("resource-id") or "").lower()
        cls = (node.get("class") or "").lower()
        password = (node.get("password") or "").lower() == "true"
        if "edittext" not in cls and "textfield" not in cls:
            continue
        if kind == "email" and ("identifierid" in rid or "email" in rid):
            return node
        if kind == "password" and (password or "password" in rid):
            return node
        if kind == "code" and any(token in rid for token in ("totp", "idv", "code", "pin")):
            return node
    return None


def _button(nodes, names, ids=()):
    wanted = [name.lower() for name in names]
    wanted_ids = [value.lower() for value in ids]
    for node in nodes:
        text = (node.get("text") or node.get("content-desc") or "").strip().lower()
        rid = (node.get("resource-id") or "").lower()
        clickable = (node.get("clickable") or "").lower() == "true"
        if not clickable:
            continue
        if text in wanted or any(value in rid for value in wanted_ids):
            return node
    return None


class GoogleLogin:
    def __init__(self, panel, screen):
        self.panel = panel
        self.screen = screen
        self.lock = threading.RLock()
        self.step = "idle"
        self.message = ""
        self.account = ""
        self.email = ""
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {"step": self.step, "message": self.message, "account": self.account}

    def _set(self, step: str, message: str = "", account: Optional[str] = None) -> None:
        with self.lock:
            self.step, self.message = step, message
            if account is not None:
                self.account = account

    def start(self, email: str, password: str) -> Dict[str, Any]:
        email = str(email or "").strip()
        password = str(password or "")
        if not email or "@" not in email:
            raise GoogleLoginError("Введите email Google")
        if not password:
            raise GoogleLoginError("Введите пароль Google")
        with self.lock:
            if self._thread and self._thread.is_alive():
                raise GoogleLoginError("Вход уже выполняется")
            self.step, self.message, self.account = "starting", "", ""
            self.email = email
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(email, password), daemon=True, name="google-login"
            )
            self._thread.start()
        return self.status()

    def submit_code(self, code: str) -> Dict[str, Any]:
        code = str(code or "").strip()
        if not code:
            raise GoogleLoginError("Введите код подтверждения")
        with self.lock:
            if self.step != "code":
                raise GoogleLoginError("Сейчас не запрашивается код")
            if self._thread and self._thread.is_alive():
                raise GoogleLoginError("Вход уже выполняется")
            self.step, self.message = "checking", ""
            self._thread = threading.Thread(
                target=self._submit_code, args=(code,), daemon=True, name="google-login-code"
            )
            self._thread.start()
        return self.status()

    def cancel(self) -> Dict[str, Any]:
        self._cancel.set()
        try:
            for _ in range(2):
                self.screen._shell("input keyevent 4", timeout=5)
            self.screen._shell("input keyevent 3", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._set("cancelled", "")
        return self.status()

    # ---------- flow ----------
    def _account_exists(self, email: str) -> bool:
        try:
            out, _, _ = self.panel.adb.shell("dumpsys account", timeout=10)
        except Exception:  # noqa: BLE001
            return False
        return bool(re.search(rf"Account \{{name={re.escape(email)}, type=app\.revanced", out or ""))

    def _dump(self) -> list:
        return _nodes(self.screen.dump())

    def _wait(self, predicate, timeout: float = 25):
        deadline = time.time() + timeout
        last = []
        while time.time() < deadline and not self._cancel.is_set():
            last = self._dump()
            try:
                result = predicate(last)
            except Exception:  # noqa: BLE001
                result = None
            if result:
                return result, last
            time.sleep(0.7)
        return None, last

    def _tap(self, node) -> None:
        point = _bounds(node)
        if not point:
            raise GoogleLoginError("Не удалось найти кнопку на экране входа")
        self.screen.tap(point[0], point[1])

    def _start_page(self) -> None:
        self.screen.start_login()
        time.sleep(2.0)
        # A fresh GmsCore login always starts with the assistant intro; its
        # "Войти" button is fixed at the bottom-right on the station. Avoid a
        # slow uiautomator dump here, it can take tens of seconds on WebView.
        self.screen.tap(self.screen.width - 160, self.screen.height - 50)
        time.sleep(2.0)

    def _run(self, email: str, password: str) -> None:
        try:
            self._set("starting", "Открываю Google…")
            self._start_page()
            self._set("email", "Ввожу email…")
            field, nodes = self._wait(lambda ns: _input(ns, "email"), timeout=25)
            if field is None:
                page = _texts(nodes)
                if "уже добавлен" in page or "already added" in page:
                    self._set("exists", "Этот аккаунт уже добавлен на устройство")
                    return
                self._set("error", "Не удалось открыть страницу входа Google")
                return
            time.sleep(1.8)
            self._tap(field)
            time.sleep(1.5)
            self.screen.text(email)
            time.sleep(1.0)
            # The Android keyboard covers the "Далее" button; Enter submits
            # the focused field and works on both email and password pages.
            self.screen.key("enter")
            self._set("password", "Ввожу пароль…")
            field, nodes = self._wait(
                lambda ns: _input(ns, "password") or any(
                    marker in _texts(ns)
                    for marker in ("уже добавлен", "already added", "не удалось найти", "couldn't find",
                                   "аккаунт не найден", "account not found", "введите действительный",
                                   "enter a valid", "неправильный пароль", "wrong password", "введите код",
                                   "enter the code", "проверьте", "verify", "captcha", "не робот")
                ),
                timeout=25,
            )
            if field is None:
                page = _texts(nodes)
                if "уже добавлен" in page or "already added" in page:
                    self._set("exists", "Этот аккаунт уже добавлен на устройство")
                elif any(marker in page for marker in (
                    "не удалось найти", "couldn't find", "аккаунт не найден", "account not found",
                    "введите действительный", "enter a valid", "неправильный пароль", "wrong password",
                )):
                    self._set("error", "Google не принял email или пароль")
                elif _input(nodes, "code"):
                    self._set("code", "Введите код подтверждения")
                elif "captcha" in page or "не робот" in page:
                    self._set("captcha", "Google просит капчу — завершите вход на станции")
                else:
                    self._set("error", "Не удалось перейти к вводу пароля")
                return
            self._tap(field)
            time.sleep(1.2)
            self.screen.text(password)
            time.sleep(0.8)
            self.screen.key("enter")
            password = ""
            self._set("checking", "Проверяю вход…")
            self._finish(email)
        except (GoogleLoginError, ScreenError) as error:
            self._set("error", str(error)[:240])
        except Exception as error:  # noqa: BLE001
            self._set("error", f"Ошибка входа: {error}"[:240])

    def _finish(self, email: str) -> None:
        deadline = time.time() + 60
        while time.time() < deadline and not self._cancel.is_set():
            nodes = self._dump()
            page = _texts(nodes)
            if self._account_exists(email):
                self._set("done", "Аккаунт добавлен", account=email)
                return
            if _input(nodes, "code"):
                self._set("code", "Введите код подтверждения")
                return
            if any(marker in page for marker in ("проверьте свой телефон", "подтвердите вход",
                                                  "check your phone", "approve sign-in")):
                self._set("approve", "Подтвердите вход на телефоне")
            if "captcha" in page or "не робот" in page:
                self._set("captcha", "Google просит капчу — завершите вход на станции")
                return
            if any(marker in page for marker in ("неправильный пароль", "wrong password", "не удалось войти",
                                                  "couldn't sign", "попробуйте еще раз", "try again")):
                self._set("error", "Google не принял пароль")
                return
            if not self.screen.input_foreground():
                # The login activity closed; check the account one more time.
                if self._account_exists(email):
                    self._set("done", "Аккаунт добавлен", account=email)
                else:
                    self._set("error", "Окно входа закрылось без добавления аккаунта")
                return
            time.sleep(0.8)
        self._set("error", "Google не завершил вход вовремя")

    def _submit_code(self, code: str) -> None:
        try:
            field, nodes = self._wait(lambda ns: _input(ns, "code"), timeout=15)
            if field is None:
                self._set("error", "Поле для кода не найдено")
                return
            self._tap(field)
            time.sleep(1.0)
            self.screen.text(code)
            time.sleep(0.8)
            self.screen.key("enter")
            self._set("checking", "Проверяю код…")
            with self.lock:
                target_email = self.email or self.account or ""
            self._finish(target_email)
        except (GoogleLoginError, ScreenError) as error:
            self._set("error", str(error)[:240])
        except Exception as error:  # noqa: BLE001
            self._set("error", f"Ошибка кода: {error}"[:240])
