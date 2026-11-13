"""Безопасный клиент очереди Hybrid rootd для ограниченных init-операций.

Клиент предназначен для подготовленного установщика. Он не выполняет команды
самостоятельно: команда всегда явно передаётся через ``--command``. Вызов
через rootd нужен потому, что SSH и обычный ADB shell на этой станции имеют
контекст ``u:r:shell:s0`` и не могут изменять ctl-свойства Android init.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


class RootdError(RuntimeError):
    """Ошибка транспорта rootd или выполнения удалённой команды."""


def _quote(value):
    return shlex.quote(str(value))


def _adb(adb, serial, *args, timeout=20, input_data=None):
    command = [adb]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    try:
        return subprocess.run(
            command,
            input=input_data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RootdError(f"ADB-команда не выполнена: {' '.join(command)}") from exc


def _require_success(result, description):
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RootdError(f"Не удалось {description}: {stderr or 'код ' + str(result.returncode)}")


def parse_result(raw):
    """Разбираем служебный ответ rootd и возвращаем его поля."""
    fields = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    if fields.get("status") != "executed" or fields.get("result") != "exited":
        reason = fields.get("reason") or fields.get("status") or "неизвестный ответ"
        raise RootdError(f"rootd не подтвердил выполнение команды: {reason}")
    try:
        fields["exit"] = int(fields.get("exit", "0"))
    except ValueError as exc:
        raise RootdError("rootd вернул некорректный код завершения") from exc
    return fields


def extract_output(raw, request_id):
    """Извлекаем stdout конкретного запроса из общего rootout."""
    text = raw.decode("utf-8", "replace")
    begin = f"--- rootd request={request_id} begin ---"
    end_pattern = re.compile(rf"--- rootd request={re.escape(request_id)} (?:exited )?exit=\d+ ---")
    begin_index = text.find(begin)
    if begin_index < 0:
        raise RootdError("В rootd output отсутствует начало блока ответа")
    content_start = text.find("\n", begin_index)
    end_match = end_pattern.search(text, content_start + 1)
    if content_start < 0 or not end_match:
        raise RootdError("В rootd output отсутствует конец блока ответа")
    return text[content_start + 1:end_match.start()]


def run(command, serial, adb="adb", timeout=30):
    """Выполняем одну команду через rootd и возвращаем stdout и код выхода."""
    request_id = "yp-" + time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:10]
    queue = "/data/local/tmp"
    request = f"{queue}/rootcmd-{request_id}"
    temporary_request = f"{queue}/.rootcmd-{request_id}.tmp"
    response = f"{queue}/rootres.{request_id}"
    output = f"{queue}/rootout"

    try:
        with tempfile.TemporaryDirectory(prefix="yapanel-rootd-") as temporary:
            request_file = Path(temporary) / "request"
            request_file.write_bytes((command.rstrip() + "\n").encode("utf-8"))
            pushed = _adb(adb, serial, "push", str(request_file), temporary_request, timeout=timeout)
            _require_success(pushed, "передать rootd-запрос")

        publish = f"chmod 0660 {_quote(temporary_request)} && mv -f {_quote(temporary_request)} {_quote(request)}"
        _require_success(_adb(adb, serial, "shell", publish, timeout=min(timeout, 20)), "опубликовать rootd-запрос")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = _adb(adb, serial, "shell", f"[ -f {_quote(response)} ]", timeout=min(timeout, 10))
            if result.returncode == 0:
                break
            time.sleep(0.4)
        else:
            raise RootdError("rootd не вернул ответ за отведённое время")

        result = _adb(adb, serial, "shell", "cat " + _quote(response), timeout=min(timeout, 20))
        _require_success(result, "прочитать ответ rootd")
        fields = parse_result(result.stdout)
        if fields.get("id") not in (None, request_id):
            raise RootdError("rootd вернул ответ другой команды")

        result = _adb(adb, serial, "shell", "cat " + _quote(output), timeout=min(timeout, 20))
        _require_success(result, "прочитать rootd output")
        stdout = extract_output(result.stdout, request_id)
        return stdout, fields["exit"]
    finally:
        cleanup = f"rm -f {_quote(temporary_request)} {_quote(response)}"
        _adb(adb, serial, "shell", cleanup, timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Выполнение одной команды через Hybrid rootd")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--timeout", type=float, default=30)
    command_group = parser.add_mutually_exclusive_group(required=True)
    command_group.add_argument("--command")
    command_group.add_argument("--command-base64")
    args = parser.parse_args(argv)
    if args.command_base64:
        try:
            command = base64.b64decode(args.command_base64, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            parser.error(f"некорректная base64-команда: {exc}")
    else:
        command = args.command
    try:
        stdout, exit_code = run(command, args.serial, args.adb, args.timeout)
        print(json.dumps({"ok": exit_code == 0, "exit": exit_code, "stdout": stdout}, ensure_ascii=False))
        return 0 if exit_code == 0 else 2
    except RootdError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
