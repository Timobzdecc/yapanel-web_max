"""Запускаем панель с восстановлением незавершённых обновлений.

Скрипт размещается отдельно от рабочего каталога и вызывается сохранённым
start.sh. Поэтому его код остаётся доступным, даже если новый server.py не
может импортироваться.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class BootstrapFailure(RuntimeError):
    """Ошибка bootstrap, после которой нельзя продолжать переключение версии."""


CRASH_LOOP_LIMIT = 3
CRASH_LOOP_WINDOW = 60
SUPERVISOR_RESTART_CODE = 75


def _load_status(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fsync_directory(path):
    if sys.platform == "win32":
        return
    descriptor = os.open(str(Path(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save_status(path, **updates):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    current = _load_status(target)
    current.update(updates)
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    _fsync_directory(target.parent)


def _save_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _wrapper_intent_path(state_root):
    return Path(state_root) / "updater" / "wrapper-intent.json"


def _load_wrapper_intent(state_root):
    path = _wrapper_intent_path(state_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapFailure("Повреждён startup intent launch wrapper") from exc
    if not isinstance(payload, dict) or payload.get("state") != "prepared":
        raise BootstrapFailure("Неизвестное состояние startup intent launch wrapper")
    return payload


def _clear_wrapper_intent(state_root):
    path = _wrapper_intent_path(state_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BootstrapFailure(f"Не удалось удалить startup intent launch wrapper: {exc}") from exc
    _fsync_directory(path.parent)


def _prepare_wrapper_intent(root, state_root, original_sha256, patched_sha256, target_version):
    root = Path(root)
    state_root = Path(state_root)
    updater_dir = state_root / "updater"
    source = updater_dir / "start.sh.before-migration"
    metadata_path = updater_dir / "start.sh.before-migration.json"
    target = root / "start.sh"
    expected_original = str(original_sha256 or "").lower()
    expected_patched = str(patched_sha256 or "").lower()
    if len(expected_original) != 64 or len(expected_patched) != 64:
        raise BootstrapFailure("Некорректные SHA-256 launch wrapper")
    if not source.is_file() or not metadata_path.is_file() or not target.is_file():
        raise BootstrapFailure("Не найден полный набор файлов launch wrapper")
    if _sha256(source).lower() != expected_original or _sha256(target).lower() != expected_original:
        raise BootstrapFailure("Исходный launch wrapper изменился до подготовки intent")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapFailure("Повреждён metadata launch wrapper") from exc
    if (str(metadata.get("original_sha256") or "").lower() != expected_original or
            str(metadata.get("patched_sha256") or "").lower() != expected_patched):
        raise BootstrapFailure("Metadata launch wrapper не совпадает с проверенными hashes")
    _save_json(_wrapper_intent_path(state_root), {
        "state": "prepared",
        "original_sha256": expected_original,
        "patched_sha256": expected_patched,
        "target_version": str(target_version or ""),
        "prepared_at": time.time(),
    })


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_launch_wrapper(root, state_root):
    """Восстанавливаем start.sh самим supervisor до запуска legacy-панели."""
    updater_dir = Path(state_root) / "updater"
    source = updater_dir / "start.sh.before-migration"
    metadata_path = updater_dir / "start.sh.before-migration.json"
    target = Path(root) / "start.sh"
    if not source.exists() and not metadata_path.exists():
        return False
    if not source.is_file() or not metadata_path.is_file():
        raise BootstrapFailure("Не найден полный snapshot launch wrapper")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapFailure("Повреждён metadata launch wrapper") from exc
    expected = str(metadata.get("original_sha256") or "").lower()
    patched = str(metadata.get("patched_sha256") or "").lower()
    if len(expected) != 64 or _sha256(source).lower() != expected:
        raise BootstrapFailure("SHA-256 исходного launch wrapper не совпадает")
    current = _sha256(target).lower() if target.is_file() else ""
    if current == expected:
        return False
    if patched and current != patched:
        raise BootstrapFailure("Текущий launch wrapper отличается от проверенной версии")
    temporary = target.with_name(target.name + ".rollback-tmp")
    try:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        shutil.copyfile(source, temporary)
        os.chmod(temporary, os.stat(source).st_mode & 0o7777)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise BootstrapFailure(f"Не удалось атомарно восстановить start.sh: {exc}") from exc
    if _sha256(target).lower() != expected:
        raise BootstrapFailure("После восстановления SHA-256 start.sh не совпал")
    return True


def _port(server_args):
    for index, value in enumerate(server_args):
        if value == "--port" and index + 1 < len(server_args):
            return int(server_args[index + 1])
        if value.startswith("--port="):
            return int(value.split("=", 1)[1])
    return 8765


def _wait_for_health(port, expected_version, timeout):
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/panel/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status != 200:
                    time.sleep(0.5)
                    continue
                payload = json.loads(response.read().decode("utf-8"))
                if (payload.get("ready") is True
                        and str(payload.get("version")) == str(expected_version)
                        and payload.get("critical_errors", 1) == 0):
                    return True
        except (OSError, ValueError, urllib.error.HTTPError, http.client.HTTPException):
            pass
        time.sleep(0.5)
    return False


def _read_json_endpoint(url):
    """Получаем JSON endpoint и отвергаем любой ответ без HTTP 200 и объекта JSON."""
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.HTTPError, http.client.HTTPException):
        return None


def _wait_for_legacy_health(port, expected_version, timeout):
    """Проверяем старую панель без endpoint /api/panel/health.

    Одного HTTP 200 недостаточно: проверяем одновременно статус панели и её
    собственную версию. Поле uptime подтверждает, что /api/status вернул
    рабочий объект, а не произвольную заглушку.
    """
    deadline = time.monotonic() + timeout
    status_url = f"http://127.0.0.1:{port}/api/status"
    version_url = f"http://127.0.0.1:{port}/api/panel/version"
    while time.monotonic() < deadline:
        status = _read_json_endpoint(status_url)
        version = _read_json_endpoint(version_url)
        try:
            uptime = float(status.get("uptime")) if status else -1
        except (TypeError, ValueError):
            uptime = -1
        if (status is not None and uptime >= 0 and
                version is not None and str(version.get("version")) == str(expected_version)):
            return True
        time.sleep(0.5)
    return False


def _start(root, python, server_args):
    environment = os.environ.copy()
    environment["PANEL_SUPERVISOR_MODE"] = "1"
    return subprocess.Popen(
        [str(python), "-B", str(Path(root) / "server.py"), *server_args],
        cwd=str(root),
        env=environment,
    )


def _stop_gracefully(child, timeout=10):
    """Останавливаем дочерний процесс только кооперативно.

    Принудительное завершение опасно во время rollback: процесс может еще
    удерживать порт или писать пользовательские данные. При зависании
    останавливаем процедуру и оставляем init живым через hold-состояние.
    """
    poll = getattr(child, "poll", None)
    if callable(poll) and poll() is not None:
        return getattr(child, "returncode", None)
    child.terminate()
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BootstrapFailure("Дочерний процесс не завершился после graceful stop") from exc


def _record_child_exit(status_path, returncode):
    """Ограничиваем цикл рестартов init после быстрых аварийных завершений."""
    now = time.time()
    status = _load_status(status_path)
    previous = float(status.get("last_child_exit_at") or 0)
    count = int(status.get("child_crash_count") or 0)
    if not previous or now - previous > CRASH_LOOP_WINDOW:
        count = 0
    count += 1 if returncode else 0
    if returncode:
        _save_status(
            status_path,
            child_crash_count=count,
            last_child_exit_at=now,
            last_child_exit_code=returncode,
        )
    else:
        _save_status(
            status_path,
            child_crash_count=0,
            last_child_exit_at=now,
            last_child_exit_code=returncode,
        )
    return count


def _hold_after_crash_loop(status_path, returncode):
    message = f"Дочерний процесс аварийно завершился {CRASH_LOOP_LIMIT} раза подряд, код {returncode}"
    _save_status(status_path, state="rollback_failed", error=message, checked=time.time())
    _hold_on_failure(status_path, message)
    raise BootstrapFailure(message)


def _pending_transaction(agent):
    transactions = agent.updater_dir / "transactions"
    if not transactions.is_dir():
        return None
    pending = []
    for journal_path in transactions.glob("*/transaction.json"):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if journal.get("state") == "activated_pending_health":
            pending.append(journal)
    return max(pending, key=lambda item: str(item.get("id", "")), default=None)


_RECOVERY_STATES = {
    "backup",
    "activating",
    "activated_pending_health",
    "rollback_started",
}
_TERMINAL_STATES = {"healthy", "rolled_back"}
_IGNORED_STATES = {"staged", "abandoned"}


def _durable_transaction(agent, preferred_transaction=None):
    """Возвращаем journal, который определяет фазу recovery.

    status.json используется только как указатель на transaction. При любом
    расхождении состояние transaction.json имеет приоритет.
    """
    transactions = agent.updater_dir / "transactions"
    if not transactions.is_dir():
        return None
    journals = []
    for journal_path in sorted(transactions.glob("*/transaction.json")):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BootstrapFailure(f"Повреждён durable journal: {journal_path}") from exc
        state = journal.get("state")
        if state not in _RECOVERY_STATES | _TERMINAL_STATES | _IGNORED_STATES:
            raise BootstrapFailure(
                f"Неизвестное состояние durable journal {journal.get('id')}: {state}"
            )
        journals.append(journal)
    active = [item for item in journals if item.get("state") in _RECOVERY_STATES]
    if len(active) > 1:
        raise BootstrapFailure("Найдено несколько активных durable transaction")
    if active:
        return active[0]
    if preferred_transaction:
        selected = [item for item in journals if item.get("id") == preferred_transaction]
        if len(selected) > 1:
            raise BootstrapFailure("Для transaction найдено несколько durable journal")
        if selected:
            return selected[0]
    terminal = [item for item in journals if item.get("state") in _TERMINAL_STATES]
    return max(terminal, key=lambda item: str(item.get("id", "")), default=None)


def _hold_on_failure(status_path, error):
    """Не отдаём init неисправный сервис в бесконечный цикл рестартов."""
    _save_status(status_path, state="rollback_failed", error=error, checked=time.time())
    while True:
        time.sleep(3600)


def run(root, state, python, server_args, health_timeout=30):
    root = Path(root).resolve()
    state_root = Path(state).resolve()
    external_updater = state_root / "updater"
    sys.path.insert(0, str(external_updater))
    sys.path.insert(1, str(root))
    from update_agent import UpdateAgent, UpdateFailure

    agent = UpdateAgent(root, state_dir=state_root, python_executable=python)
    status_path = agent.updater_dir / "status.json"
    _save_status(
        status_path,
        supervisor_owner={"pid": os.getpid(), "started_at": time.time()},
    )
    recovered = []
    try:
        recovered = agent.recover()
    except UpdateFailure as exc:
        _hold_on_failure(status_path, f"Не удалось восстановить незавершённую транзакцию: {exc}")
    if recovered:
        try:
            _restore_launch_wrapper(root, state_root)
        except BootstrapFailure as exc:
            _hold_on_failure(status_path, str(exc))
        _save_status(
            status_path,
            state="rollback_pending_health",
            error="Восстановлена незавершённая транзакция; ожидается legacy health",
        )

    status = _load_status(status_path)
    status_state = status.get("state")
    if status_state == "rollback_failed":
        _hold_on_failure(status_path, status.get("error") or "Предыдущий rollback не завершился")
    try:
        durable = _durable_transaction(agent, status.get("transaction"))
    except BootstrapFailure as exc:
        _hold_on_failure(status_path, str(exc))
        durable = None

    transaction = durable.get("id") if durable else status.get("transaction")
    durable_state = durable.get("state") if durable else None
    if durable_state in {"backup", "activating", "rollback_started"}:
        _hold_on_failure(
            status_path,
            f"Незавершённая transaction требует recovery: {durable_state}",
        )

    pending = durable_state == "activated_pending_health"
    healthy_pending_check = durable_state == "healthy"
    rollback_pending_health = (
        durable_state == "rolled_back" or status_state == "rollback_pending_health"
    )
    rolled_back = durable_state == "rolled_back"
    if pending:
        status["version"] = durable.get("version")
        _save_status(
            status_path,
            state="activated_pending_health",
            version=status["version"],
            transaction=transaction,
            error=None,
        )
    elif healthy_pending_check:
        status["version"] = durable.get("version")
        _save_status(
            status_path,
            state="healthy_pending_check",
            version=status["version"],
            transaction=transaction,
            error=None,
        )
    elif rolled_back:
        _save_status(
            status_path,
            state="rollback_pending_health",
            transaction=transaction,
            error=None,
        )
    elif status_state == "activated_pending_health":
        _hold_on_failure(
            status_path,
            "status.json указывает pending health, но durable journal не найден",
        )
    else:
        # Если wrapper был подготовлен, но managed-файлы ещё не активировались,
        # восстанавливаем исходный запуск и не оставляем станцию в промежуточном
        # состоянии после сбоя питания до apply.
        wrapper_intent = _load_wrapper_intent(state_root)
        if wrapper_intent:
            try:
                _restore_launch_wrapper(root, state_root)
                _clear_wrapper_intent(state_root)
            except BootstrapFailure as exc:
                _hold_on_failure(status_path, str(exc))
            _save_status(
                status_path,
                state="preparation_aborted",
                error="Подготовка wrapper прервана до активации; восстановлен исходный запуск",
                checked=time.time(),
            )
            transaction_state = "preparation_aborted"
            rollback_pending_health = False
            rolled_back = False
            pending = False
            healthy_pending_check = False
            transaction = None
    while True:
        child = _start(root, python, server_args)
        if pending and transaction:
            if _wait_for_health(_port(server_args), status.get("version"), health_timeout):
                if hasattr(agent, "mark_healthy"):
                    try:
                        agent.mark_healthy(transaction)
                    except UpdateFailure as exc:
                        _save_status(status_path, state="rollback_failed", error=str(exc), checked=time.time())
                        _hold_on_failure(status_path, str(exc))
                _clear_wrapper_intent(state_root)
                _save_status(status_path, state="healthy", checked=time.time(), error=None)
            else:
                try:
                    _stop_gracefully(child)
                except BootstrapFailure as exc:
                    message = f"Не удалось безопасно остановить новую версию: {exc}"
                    _save_status(status_path, state="rollback_failed", error=message, checked=time.time())
                    _hold_on_failure(status_path, message)
                    raise
                try:
                    agent.rollback(transaction)
                except UpdateFailure as exc:
                    _save_status(status_path, state="rollback_failed", error=str(exc))
                    raise
                try:
                    _restore_launch_wrapper(root, state_root)
                except BootstrapFailure as exc:
                    _save_status(status_path, state="rollback_failed", error=str(exc))
                    _hold_on_failure(status_path, str(exc))
                _save_status(
                    status_path,
                    state="rollback_pending_health",
                    error="Новая версия не прошла health check; ожидается legacy health",
                )
                child = _start(root, python, server_args)
                old_version = (root / "VERSION").read_text(encoding="utf-8").strip()
                if not _wait_for_legacy_health(_port(server_args), old_version, health_timeout):
                    try:
                        _stop_gracefully(child)
                    except BootstrapFailure as exc:
                        message = f"Не удалось безопасно остановить восстановленную версию: {exc}"
                        _save_status(status_path, state="rollback_failed", error=message, checked=time.time())
                        _hold_on_failure(status_path, message)
                        raise
                    _save_status(
                        status_path,
                        state="rollback_pending_health",
                        error="Предыдущая версия не прошла legacy health check",
                        checked=time.time(),
                    )
                    _hold_on_failure(status_path, "Предыдущая версия не прошла legacy health check")
                rollback_status = _load_status(status_path)
                _clear_wrapper_intent(state_root)
                _save_status(
                    status_path,
                    state="rolled_back",
                    error=rollback_status.get("error"),
                    checked=time.time(),
                )
        elif healthy_pending_check:
            expected_version = str(status.get("version") or durable.get("version"))
            if not _wait_for_health(_port(server_args), expected_version, health_timeout):
                message = "Durable journal healthy, но новая версия не прошла повторный health check"
                _save_status(status_path, state="rollback_failed", error=message, checked=time.time())
                _hold_on_failure(status_path, message)
            _clear_wrapper_intent(state_root)
            _save_status(
                status_path,
                state="healthy",
                version=expected_version,
                transaction=transaction,
                checked=time.time(),
                error=None,
            )
        elif rolled_back or rollback_pending_health:
            try:
                _restore_launch_wrapper(root, state_root)
            except BootstrapFailure as exc:
                _save_status(status_path, state="rollback_failed", error=str(exc))
                _hold_on_failure(status_path, str(exc))
            old_version = (root / "VERSION").read_text(encoding="utf-8").strip()
            if not _wait_for_legacy_health(_port(server_args), old_version, health_timeout):
                try:
                    _stop_gracefully(child)
                except BootstrapFailure as exc:
                    message = f"Не удалось безопасно остановить восстановленную версию: {exc}"
                    _save_status(status_path, state="rollback_failed", error=message, checked=time.time())
                    _hold_on_failure(status_path, message)
                    raise
                _save_status(
                    status_path,
                    state="rollback_pending_health",
                    error="Восстановленная версия не прошла legacy health check",
                    checked=time.time(),
                )
                _hold_on_failure(status_path, "Восстановленная версия не прошла legacy health check")
            if rollback_pending_health:
                rollback_status = _load_status(status_path)
                _clear_wrapper_intent(state_root)
                _save_status(
                    status_path,
                    state="rolled_back",
                    error=rollback_status.get("error"),
                    checked=time.time(),
                )
        returncode = child.wait()
        if returncode == SUPERVISOR_RESTART_CODE:
            status = _load_status(status_path)
            pending_journal = _pending_transaction(agent)
            if status.get("state") != "activated_pending_health" and not pending_journal:
                message = "Supervisor restart не связан с ожидающей транзакцией"
                _save_status(status_path, state="rollback_failed", error=message, checked=time.time())
                _hold_on_failure(status_path, message)
            pending = True
            rolled_back = False
            transaction = status.get("transaction") or pending_journal.get("id")
            if pending_journal:
                status["version"] = pending_journal.get("version")
            continue
        crash_count = _record_child_exit(status_path, returncode)
        if returncode and crash_count >= CRASH_LOOP_LIMIT:
            _hold_after_crash_loop(status_path, returncode)
        return returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Запуск панели с автоматическим rollback")
    parser.add_argument("--root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--health-timeout", type=float, default=30)
    parser.add_argument("--prepare-wrapper", action="store_true")
    parser.add_argument("--original-sha256")
    parser.add_argument("--patched-sha256")
    parser.add_argument("--target-version")
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.prepare_wrapper:
        _prepare_wrapper_intent(
            args.root,
            args.state,
            args.original_sha256,
            args.patched_sha256,
            args.target_version,
        )
        return 0
    server_args = list(args.server_args)
    if server_args and server_args[0] == "--":
        server_args = server_args[1:]
    return run(args.root, args.state, args.python, server_args, args.health_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
