import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class Redact(unittest.TestCase):
    def test_hides_secrets_and_addresses(self):
        text = server.redact("sk-proj-abcdefghijklmnop y0_AgAAAAABCDEFGHIJKLMNOP token=abcdefghijklmnopqrstu "
                             "passport_uid=123456789 puid:987654321 uid=5555555 "
                             "192.168.2.115:5555 aa:bb:cc:dd:ee:ff me@mail.ru /home/dimap/x C:\\Users\\Dima\\y")
        for secret in ("abcdefghijklmnop", "ABCDEFGHIJKLMNOP", "abcdefghijklmnopqrstu",
                       "123456789", "987654321", "5555555", "192.168", "aa:bb", "me@", "dimap", "Dima"):
            self.assertNotIn(secret, text)

    def test_keeps_versions(self):
        for keep in ("0.369.1.50.4794654278.20261021.133", "127.0.0.1:8765", "adb 1.0.41", "uid=1000"):
            self.assertEqual(server.redact(keep), keep)


class JournalFile(unittest.TestCase):
    def test_repeats_collapse_and_survive_restart(self):
        path = Path(tempfile.mkdtemp()) / "j.jsonl"
        j = server.Journal(); j.open(path)
        for _ in range(3):
            j.add("связь: потеряна", "warn")
        j.add("POST /api/x — ок")
        self.assertIn("(×3)", j.text())
        again = server.Journal(); again.open(path)
        self.assertEqual([i["msg"] for i in again.items], ["связь: потеряна", "POST /api/x — ок"])

    def test_trims_to_limit(self):
        path = Path(tempfile.mkdtemp()) / "j.jsonl"
        j = server.Journal(); j.LIMIT = 50; j.open(path)
        for n in range(450):
            j.add(f"событие {n}")
        self.assertLessEqual(len(path.read_text().splitlines()), 50 + 200)
        self.assertEqual(j.items[-1]["msg"], "событие 449")


class RequestJournal(unittest.TestCase):
    def handler(self, method, path, data=None, headers=None):
        h = SimpleNamespace(command=method, path=path, _jdata=data or {}, headers=headers or {}, JOURNAL_QUIET=server.Handler.JOURNAL_QUIET,
                            JOURNAL_KEYS=server.Handler.JOURNAL_KEYS)
        return lambda **kw: server.Handler._journal(h, **kw)

    def setUp(self):
        server.JOURNAL.items.clear()

    def test_actions_without_secrets(self):
        self.handler("POST", "/api/assistant", {"api_key": "sk-secret-123456789", "name": "x"})()
        self.handler("POST", "/api/volume", {"level": 5})()          # ползунок — не пишем
        self.handler("GET", "/api/status")()                          # опрос — не пишем
        self.handler("GET", "/api/status")(error="Колонка не ответила")
        msgs = [i["msg"] for i in server.JOURNAL.items]
        self.assertEqual(msgs, ["POST /api/assistant (name=x) — ок", "GET /api/status: Колонка не ответила"])
        self.assertNotIn("sk-secret", " ".join(msgs))


class SpeakerFinder(unittest.TestCase):
    """Поиск колонки из панели: адрес запоминается, панель перезапускается с ним."""

    def test_restart_replaces_device_args(self):
        from unittest.mock import patch
        cases = {("server.py", "--open", "--device", "1.2.3.4", "--port", "1"): ["server.py", "--port", "1"],
                 ("server.py", "--find", "--port", "1"): ["server.py", "--port", "1"],
                 ("server.py", "--find", "10.0.0.0/24", "--device=x"): ["server.py"]}
        for argv, rest in cases.items():
            with patch.object(server.sys, "argv", list(argv)), patch.object(server.os, "execv") as ex, \
                    patch.object(server.Handler, "panel", None, create=True):
                server.restart_self(device="192.168.1.50:5555")
            self.assertEqual(ex.call_args.args[1][1:], rest + ["--device", "192.168.1.50:5555"])

    def test_use_accepts_only_home_ip(self):
        from unittest.mock import patch
        h = SimpleNamespace()
        for bad in ("8.8.8.8", "evil.com", "abc", "", None, "192.168.1.5; reboot"):
            with self.assertRaises(server.AdbError):
                server.Handler._conn_use(h, bad)
        with patch.object(server, "save_settings") as save, patch.object(server, "load_settings", return_value={}), \
                patch.object(server.threading, "Timer") as timer:
            self.assertEqual(server.Handler._conn_use(h, "192.168.1.50")["serial"], "192.168.1.50:5555")
        save.assert_called_once_with({"device": "192.168.1.50:5555"})
        self.assertEqual(timer.call_args.kwargs["kwargs"], {"device": "192.168.1.50:5555"})


class SayOnSpeaker(unittest.TestCase):
    """«Сказать на колонке»: валидация текста, Glagol и запасной TTS."""

    def test_alice_voice(self):
        p = server.Panel(Mock())
        p.glagol = SimpleNamespace(ws=True, send=Mock())
        res = p.say("ужин; rm -rf / $(id)\nвторая строка")
        self.assertEqual(res, {"ok": True, "method": "glagol"})
        p.glagol.send.assert_called_once_with(
            {"command": "sendText", "text": "Повтори за мной ужин; rm -rf / $(id) вторая строка"}
        )

        p2 = server.Panel(Mock())
        p2.glagol = SimpleNamespace(ws=False, send=Mock())
        p2.detach = Mock()
        res2 = p2.say("привет; $(id)")
        self.assertEqual(res2, {"ok": True, "method": "tts"})
        script = p2.detach.call_args.args[1]
        self.assertNotIn("$(id)", script)
        self.assertIn("translate_tts", script)

    def test_assistant_voice_and_limits(self):
        p = server.Panel(Mock())
        p.assistant_command = Mock(return_value={"ok": True})
        p.say("привет", "assistant")
        p.assistant_command.assert_called_once_with({"say": "привет"})
        for bad in ("", "   ", "x" * 301):
            with self.assertRaises(server.AdbError):
                p.say(bad)


class ErrorCaptureAndReport(unittest.TestCase):
    def setUp(self):
        server.JOURNAL.items.clear()
        server.ERROR_LOG.items.clear()
        with server._SERVER_LOG_LOCK:
            server._SERVER_LOG_RING.clear()

    def _make_handler(self, method="GET", path="/api/test"):
        h = server.Handler.__new__(server.Handler)
        h.command, h.path, h._jdata = method, path, {}
        h.headers, h.close_connection = {}, False
        h.request_version, h.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
        h.client_address, h._headers_buffer = ("127.0.0.1", 1), []
        sent = []
        h._send = lambda code, body, ctype="application/json; charset=utf-8", headers=(): sent.append((code, body))
        return h, sent

    def test_handle_logs_500_traceback_and_api_400_response(self):
        import api
        h500, sent500 = self._make_handler("GET", "/api/boom")
        def boom():
            raise RuntimeError("secret sk-proj-1234567890 exploded")
        h500._handle(boom)
        self.assertEqual(sent500[0][0], 500)
        self.assertIn("RuntimeError", sent500[0][1]["error"])
        jmsg = server.JOURNAL.items[-1]["msg"]
        self.assertIn("boom", jmsg)
        self.assertIn("test_journal.py:", jmsg)
        self.assertNotIn("sk-proj-1234567890", jmsg)
        err_txt = server.ERROR_LOG.text()
        self.assertIn("Traceback", err_txt)
        self.assertNotIn("sk-proj-1234567890", err_txt)

        h400, sent400 = self._make_handler("GET", "/api/extmusic/thumb")
        h400._handle(lambda: api.Response({"error": "bad cover"}, status=400))
        self.assertEqual(sent400[0][0], 400)
        self.assertEqual(server.JOURNAL.items[-1]["level"], "error")
        self.assertIn("bad cover", server.JOURNAL.items[-1]["msg"])

    def test_journal_log_handler_and_thread_hook(self):
        import logging
        logger = logging.getLogger("yapanel.test_bridge")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = server._JournalLogHandler()
        logger.addHandler(handler)
        try:
            logger.info("обычное событие 192.168.1.50")
            self.assertEqual(len(server.JOURNAL.items), 0)
            self.assertIn("x.x.x.x", server._server_log_text())

            logger.warning("предупреждение без стека")
            self.assertEqual(server.JOURNAL.items[-1]["level"], "warn")
            self.assertEqual(server.ERROR_LOG.text(), "")

            try:
                raise ValueError("плохой ключ sk-test-abcdef123456")
            except ValueError:
                logger.error("ошибка модуля", exc_info=True)
            self.assertEqual(server.JOURNAL.items[-1]["level"], "error")
            self.assertIn("ERROR", server.ERROR_LOG.text())
            self.assertNotIn("abcdef123456", server.ERROR_LOG.text())
        finally:
            logger.removeHandler(handler)

        try:
            raise KeyError("missing_field")
        except KeyError as exc:
            server._record_thread_exception(SimpleNamespace(
                exc_type=type(exc), exc_value=exc, exc_traceback=exc.__traceback__,
                thread=SimpleNamespace(name="bg-worker"),
            ))
        self.assertIn("поток bg-worker", server.JOURNAL.items[-1]["msg"])
        self.assertIn("thread:bg-worker", server.ERROR_LOG.text())

    def test_http_handle_error_ignores_client_disconnect_and_logs_real_errors(self):
        for exc_cls in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            try:
                raise exc_cls("gone")
            except OSError:
                server._handle_http_error(None, ("192.168.1.10", 1234))
        self.assertEqual(len(server.JOURNAL.items), 0)
        self.assertEqual(server.ERROR_LOG.text(), "")

        try:
            raise RuntimeError("socket loop crashed")
        except RuntimeError:
            server._handle_http_error(None, ("192.168.1.10", 1234))
        self.assertEqual(server.JOURNAL.items[-1]["level"], "error")
        self.assertIn("RuntimeError: socket loop crashed", server.JOURNAL.items[-1]["msg"])
        self.assertIn("socket loop crashed", server.ERROR_LOG.text())

    def test_report_includes_errors_and_tails_redacted(self):
        server.JOURNAL.add("POST /api/play — ок")
        server.ERROR_LOG.add("POST /api/broken", "Traceback:\n  File x.py: sk-proj-secret1234567890")
        adb = Mock()
        adb.adb = "adb"
        adb.is_local = True
        def fake_shell(cmd, **kw):
            if "device-tree/model" in cmd:
                return "Max\n4.9.113\nсвободно в /data: 5.0G\n", "", 0
            if "__YP_LOG__" in cmd:
                return (
                    "__YP_LOG__\n2026-11-18 08:00:00 y0_AgAAAASECRET123456 192.168.1.50 [ERROR] gst boom\n"
                    "__YP_ASST__\nвнешняя музыка: timeout\n"
                    "__YP_BOOT__\n2026-11-18 08:00:01 START_OK\n",
                    "",
                    0,
                )
            return "", "", 0
        adb.shell.side_effect = fake_shell
        p = server.Panel(adb)
        p.conn = SimpleNamespace(state="online", attempts=0, online=True)
        p.version = lambda: {"quasar_version": "3.367.34"}
        p.hook_status = lambda: {"hook": True, "fresh": True, "users": ["ассистент"], "possible": True, "file": "/data/quasar/webpanel/boot_hook.sh"}
        p.footprint = lambda: {"items": []}
        p.glagol = SimpleNamespace(error="нет связи 192.168.1.50", tls_warning=None)
        p.extmusic = SimpleNamespace(error="YouTube error")

        h = server.Handler.__new__(server.Handler)
        h.panel = p
        h.updater = SimpleNamespace(kind=lambda: "zip")
        rep = h._report()["text"]
        self.assertIn("Недавние трассировки и ошибки", rep)
        self.assertIn("Журнал панели", rep)
        self.assertIn("Хвост yapanel.log", rep)
        self.assertIn("Хвост assistant.log", rep)
        self.assertIn("Хвост assistant-autostart.log", rep)
        self.assertIn("Активные ошибки модулей", rep)
        self.assertNotIn("secret1234567890", rep)
        self.assertNotIn("SECRET123456", rep)
        self.assertNotIn("192.168.1.50", rep)
        self.assertIn("POST /api/broken", p.log("errors", 10)["text"])
        server.ERROR_LOG.items.clear()
        self.assertIn("не зафиксировано", p.log("errors", 10)["text"])
        self.assertIn("assistant-autostart.log", p._max_log_files())
        self.assertIn("yapanel.log.1", p._max_log_files())


if __name__ == "__main__":
    unittest.main()
