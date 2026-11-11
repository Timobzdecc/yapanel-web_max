"""Безопасное staged-обновление панели без внешних зависимостей.

Модуль намеренно не импортирует server.py. Его можно скопировать в отдельный
каталог состояния и запустить после замены исходников панели.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

import station_overlay


class UpdateFailure(RuntimeError):
    """Ошибка, после которой активация не должна считаться успешной."""


def fsync_directory(path):
    """Фиксируем rename в каталоге; Windows не позволяет открыть каталог так же, как POSIX."""
    if os.name == "nt":
        return
    descriptor = os.open(str(Path(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_write(path, data, mode="w", encoding="utf-8"):
    """Атомарно записываем небольшой файл и фиксируем файл и его каталог."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    if "b" in mode:
        with temporary.open(mode) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        with temporary.open(mode, encoding=encoding) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(temporary, target)
    fsync_directory(target.parent)


def durable_json(path, value):
    durable_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class UpdateAgent:
    """Подготовка, backup, активация и восстановление релиза панели."""

    REQUIRED_FILES = (
        "server.py",
        "config.py",
        "profiles.py",
        "max_profile.py",
        "max_privacy.py",
        "assistant_max.py",
        "assistant_socks.py",
        "VERSION",
    )
    MANAGED_DIRS = ("api", "static", "device")
    PRESERVED_FILES = {
        "start.sh",
        "start.bat",
        "start.command",
    }
    CONTROLLED_BOOTSTRAP_FILES = {
        "panel_update_integration.py",
        "update_agent.py",
        "station_overlay.py",
        "updater_bootstrap.py",
    }
    OPTIONAL_RELEASE_METADATA = {
        "CONTRIBUTION.md",
        "LICENSE",
        "README.md",
    }
    GENERATED_BINARY_FILES = {
        "device/agent/build/yapanel-agent",
        "device/assistant/build/assistantd",
        "device/viz/build/vizd",
    }
    EXCLUDED_RELEASE_SUFFIXES = {".bat", ".cmd", ".ps1", ".exe", ".dll", ".pyd"}
    JOURNAL_NAME = "transaction.json"
    MAX_RECOVERY_ROLLBACK_ATTEMPTS = 2

    def __init__(self, root, state_dir=None, python_executable=None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / ".yapanel-state").resolve()
        self.updater_dir = self.state_dir / "updater"
        self.python_executable = python_executable or sys.executable

    @staticmethod
    def sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_relative(name):
        path = Path(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise UpdateFailure(f"Небезопасный путь в архиве: {name}")
        if not path.parts or any(not part or part == "." for part in path.parts):
            raise UpdateFailure(f"Некорректный путь в архиве: {name}")
        return Path(*path.parts)

    @staticmethod
    def _is_symlink(info):
        mode = (info.external_attr >> 16) & 0xFFFF
        return stat.S_ISLNK(mode)

    def _restore_backup_member(self, archive, member, destination_root, prefix):
        """Восстанавливаем один элемент tar без выхода за namespace."""
        relative_name = member.name[len(prefix):]
        relative = self._safe_relative(relative_name)
        destination_root = Path(destination_root)
        destination = destination_root / relative
        root_resolved = destination_root.resolve()
        parent_resolved = destination.parent.resolve()
        if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
            raise UpdateFailure("Резервная копия содержит небезопасный путь")

        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            return

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".rollback-tmp")
        if temporary.is_symlink() or temporary.is_file():
            temporary.unlink()
        elif temporary.is_dir():
            shutil.rmtree(temporary)

        if member.issym():
            # Создаём ссылку на имя из архива. os.symlink не открывает target,
            # поэтому абсолютная ссылка не может записать за пределы панели.
            if "\x00" in member.linkname:
                raise UpdateFailure("Символическая ссылка содержит NUL")
            try:
                os.symlink(member.linkname, temporary)
            except OSError as exc:
                raise UpdateFailure(f"Не удалось восстановить символическую ссылку {relative_name}: {exc}") from exc
        elif member.islnk():
            link_name = member.linkname.replace("\\", "/")
            if link_name.startswith(prefix):
                link_name = link_name[len(prefix):]
            elif link_name.startswith(("panel/", "state/data/")):
                raise UpdateFailure(f"Жёсткая ссылка выходит за namespace: {member.linkname}")
            link_relative = self._safe_relative(link_name)
            link_target = destination_root / link_relative
            link_parent = link_target.parent.resolve()
            if link_parent != root_resolved and root_resolved not in link_parent.parents:
                raise UpdateFailure("Жёсткая ссылка в backup выходит за каталог панели")
            if link_target.is_symlink() or not link_target.is_file():
                raise UpdateFailure(f"Цель жёсткой ссылки отсутствует: {member.linkname}")
            os.link(link_target, temporary, follow_symlinks=False)
        else:
            if not member.isfile():
                raise UpdateFailure(f"Неподдерживаемый тип записи backup: {member.name}")
            with archive.extractfile(member) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, member.mode)

        os.replace(temporary, destination)
        fsync_directory(destination.parent)

    def _restore_backup_members(self, archive, members, destination_root, prefix):
        """Восстанавливаем обычные записи до hard links, независимо от порядка tar."""
        for member in members:
            if not member.islnk():
                self._restore_backup_member(archive, member, destination_root, prefix)
        for member in members:
            if member.islnk():
                self._restore_backup_member(archive, member, destination_root, prefix)

    def _content_root(self, stage):
        candidates = [stage]
        candidates.extend(path for path in stage.iterdir() if path.is_dir())
        for candidate in candidates:
            if (candidate / "server.py").is_file():
                return candidate
        raise UpdateFailure("В архиве не найден server.py")

    def extract_zip(self, blob, stage):
        stage = Path(stage)
        stage.mkdir(parents=True, exist_ok=False)
        try:
            with zipfile.ZipFile(io.BytesIO(blob) if isinstance(blob, (bytes, bytearray)) else blob) as archive:
                for info in archive.infolist():
                    if info.is_dir() or info.filename.endswith(("/", "\\")):
                        continue
                    relative = self._safe_relative(info.filename.rstrip("/")) if info.filename.rstrip("/") else None
                    if relative is None:
                        continue
                    if self._is_symlink(info):
                        raise UpdateFailure(f"Символическая ссылка в архиве запрещена: {info.filename}")
                    destination = (stage / relative).resolve()
                    if stage.resolve() not in destination.parents:
                        raise UpdateFailure(f"Путь выходит за staging: {info.filename}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target, 1024 * 1024)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode:
                        destination.chmod(mode)
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(stage, ignore_errors=True)
            if isinstance(exc, zipfile.BadZipFile):
                raise UpdateFailure("Архив обновления повреждён") from exc
            raise UpdateFailure(f"Не удалось распаковать архив: {exc}") from exc
        return self._content_root(stage)

    def _managed_files(self, content_root):
        result = set()
        for path in content_root.iterdir():
            if path.name in self.PRESERVED_FILES or path.name in {"runtime", ".git"}:
                continue
            if (path.is_file() and path.name not in self.OPTIONAL_RELEASE_METADATA and
                    (path.suffix == ".py" or path.name == "VERSION")):
                result.add(path.relative_to(content_root).as_posix())
        for directory in self.MANAGED_DIRS:
            source = content_root / directory
            if not source.is_dir():
                continue
            for path in source.rglob("*"):
                if path.is_file() and path.suffix.lower() not in self.EXCLUDED_RELEASE_SUFFIXES:
                    result.add(path.relative_to(content_root).as_posix())
        return tuple(sorted(result))

    @staticmethod
    def _elf_metadata(path):
        data = Path(path).read_bytes()
        if len(data) < 64 or data[:4] != b"\x7fELF":
            raise UpdateFailure(f"Файл {path.name} не является ELF")
        if data[4] != 2 or data[5] != 1:
            raise UpdateFailure(f"Файл {path.name} не является little-endian ELF64")
        machine = struct.unpack_from("<H", data, 18)[0]
        if machine != 183:
            raise UpdateFailure(f"Файл {path.name} имеет неподдерживаемую архитектуру ELF: {machine}")
        program_offset = struct.unpack_from("<Q", data, 32)[0]
        entry_size = struct.unpack_from("<H", data, 54)[0]
        entry_count = struct.unpack_from("<H", data, 56)[0]
        dynamic = False
        for index in range(entry_count):
            start = program_offset + index * entry_size
            if start + 4 > len(data):
                raise UpdateFailure(f"Повреждённая таблица ELF в {path.name}")
            if struct.unpack_from("<I", data, start)[0] == 2:
                dynamic = True
                break
        marker = re.search(rb"go1\.\d+(?:\.\d+)?", data)
        return {"elf64": True, "aarch64": True, "static": not dynamic,
                "go_version": marker.group(0).decode("ascii") if marker else None}

    def _validate_generated_artifacts(self, content_root, version):
        """Проверяем generated binaries релиза, не разрешая локальные hashes станции."""
        for name in self.GENERATED_BINARY_FILES:
            binary = content_root / name
            version_file = content_root / name.replace(name.rsplit("/", 1)[1], "VERSION")
            if not binary.is_file():
                continue
            metadata = self._elf_metadata(binary)
            if not metadata["static"]:
                raise UpdateFailure(f"Generated binary {name} имеет динамические зависимости")
            if not metadata["go_version"]:
                raise UpdateFailure(f"Generated binary {name} не содержит Go build metadata")
            if not version_file.is_file() or version_file.read_text(encoding="utf-8").strip() != str(version):
                raise UpdateFailure(f"Версия generated binary {name} не совпадает с релизом {version}")

    def build_manifest(self, content_root, version=None, commit=None):
        managed = self._managed_files(content_root)
        missing = [name for name in self.REQUIRED_FILES if not (content_root / name).is_file()]
        if missing:
            raise UpdateFailure("В релизе отсутствуют обязательные файлы: " + ", ".join(missing))
        for name in managed:
            path = content_root / name
            if path.suffix == ".py":
                try:
                    source = path.read_text(encoding="utf-8")
                    compile(source, str(path), "exec")
                except (OSError, SyntaxError, UnicodeError) as exc:
                    raise UpdateFailure(f"Ошибка проверки Python-файла {name}: {exc}") from exc
        manifest = {
            "version": str(version or (content_root / "VERSION").read_text(encoding="utf-8").strip()),
            "commit": commit,
            "managed": list(managed),
            "optional_release_metadata": sorted(
                name for name in self.OPTIONAL_RELEASE_METADATA if (content_root / name).is_file()
            ),
            "required": list(self.REQUIRED_FILES),
            "preserved": sorted(self.PRESERVED_FILES),
        }
        self._validate_generated_artifacts(content_root, manifest["version"])
        self._check_local_imports(content_root, manifest["managed"])
        return manifest

    @staticmethod
    def _check_local_imports(content_root, managed):
        local_modules = {Path(name).stem for name in managed if name.endswith(".py") and "/" not in name}
        local_packages = {name.split("/", 1)[0] for name in managed if "/" in name and name.endswith("/__init__.py")}
        known_project_modules = {
            "access", "agent", "assistant_max", "assistant_socks", "config", "device",
            "dsp_max", "glagol", "max_privacy", "max_profile", "multi", "party",
            "party_audio", "party_store", "profiles", "pyadb", "server", "station_overlay",
            "update_agent",
        }
        for name in managed:
            if not name.endswith(".py"):
                continue
            path = content_root / name
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise UpdateFailure(f"Не удалось разобрать импорт в {name}: {exc}") from exc
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module.split(".", 1)[0])
            for module in imports:
                if module in local_modules or module in local_packages:
                    continue
                candidate = content_root / (module + ".py")
                package = content_root / module / "__init__.py"
                if module in known_project_modules and not (candidate.exists() or package.exists()):
                    raise UpdateFailure(f"Импорт {module} из {name} не найден в staging")

    def preflight(self, manifest, archive_size=0, min_free_bytes=1024 * 1024):
        """Проверяем место, права и версию Python до создания backup."""
        if sys.version_info < (3, 9):
            raise UpdateFailure("Нужен Python 3.9 или новее")
        if not self.root.exists() or not os.access(self.root, os.W_OK):
            raise UpdateFailure("Каталог панели недоступен для записи")
        self.updater_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(self.updater_dir, os.W_OK):
            raise UpdateFailure("Каталог updater недоступен для записи")
        try:
            free = shutil.disk_usage(self.root).free
        except OSError as exc:
            raise UpdateFailure(f"Не удалось проверить свободное место: {exc}") from exc
        required = max(min_free_bytes, int(archive_size) * 2)
        if free < required:
            raise UpdateFailure(f"Недостаточно свободного места: нужно не менее {required} байт")
        return {"python": ".".join(map(str, sys.version_info[:3])), "free_bytes": free}

    def stage(self, blob, version=None, expected_sha256=None, commit=None, apply_overlays=True):
        blob = bytes(blob)
        if expected_sha256 and hashlib.sha256(blob).hexdigest().lower() != expected_sha256.lower():
            raise UpdateFailure("Контрольная сумма архива обновления не совпадает")
        transaction_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        transaction_dir = self.updater_dir / "transactions" / transaction_id
        transaction_dir.parent.mkdir(parents=True, exist_ok=True)
        archive_path = transaction_dir.with_suffix(".zip")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        durable_write(archive_path, blob, mode="wb")
        stage = transaction_dir / "stage"
        try:
            content_root = self.extract_zip(blob, stage)
            overlay = None
            if apply_overlays:
                try:
                    overlay = station_overlay.apply(content_root, version)
                except RuntimeError as exc:
                    raise UpdateFailure(str(exc)) from exc
            manifest = self.build_manifest(content_root, version=version, commit=commit)
            self.preflight(manifest, archive_size=len(blob))
            durable_json(transaction_dir / "manifest.json", manifest)
            self._write_journal({
                "id": transaction_id,
                "state": "staged",
                "version": manifest["version"],
                "commit": commit,
                "stage": str(content_root),
                "archive": str(archive_path),
                "managed": manifest["managed"],
                "overlay": overlay,
            }, transaction_dir)
            return transaction_id, manifest
        except Exception:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            raise

    def _transaction_dir(self, transaction_id):
        return self.updater_dir / "transactions" / transaction_id

    def _write_journal(self, data, transaction_dir):
        transaction_dir = Path(transaction_dir)
        transaction_dir.mkdir(parents=True, exist_ok=True)
        durable_json(transaction_dir / self.JOURNAL_NAME, data)

    def read_journal(self, transaction_id):
        path = self._transaction_dir(transaction_id) / self.JOURNAL_NAME
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UpdateFailure(f"Не удалось прочитать журнал транзакции {transaction_id}") from exc

    def baseline_conflicts(self, baseline, managed):
        conflicts = []
        baseline = baseline or {}
        for name in managed:
            if name in self.PRESERVED_FILES:
                continue
            current = self.root / name
            expected = baseline.get(name)
            approved = []
            lineage = None
            if isinstance(expected, dict):
                approved = [str(value) for value in expected.get("approved", ())]
                lineage = expected.get("lineage")
                expected = expected.get("base")
            current_hash = self.sha256(current) if current.is_file() else None
            if expected is None and approved:
                if current_hash in approved:
                    continue
                reason = "исполняемый файл отсутствует в подтверждённой lineage-базе"
                if lineage:
                    reason += " или его содержимое не совпадает с официальным asset"
                conflicts.append({"file": name, "reason": reason})
                continue
            if expected is None:
                if name in self.CONTROLLED_BOOTSTRAP_FILES:
                    continue
                if current.exists():
                    conflicts.append({"file": name, "reason": "файл существует, но не был частью известной базы"})
                continue
            if current_hash is None:
                conflicts.append({"file": name, "reason": "файл отсутствует в установленной панели, но присутствует в baseline"})
            elif current_hash != expected and current_hash not in approved:
                conflicts.append({"file": name, "reason": "локальное содержимое отличается от базового релиза"})
        return conflicts

    def capture_baseline(self, files):
        """Сохраняем hashes известной чистой установки для первого preflight."""
        baseline = {}
        for name in files:
            path = self.root / name
            if path.is_file():
                baseline[name] = self.sha256(path)
        target = self.updater_dir / "baseline.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        durable_json(target, baseline)
        return baseline

    def create_backup(self, transaction_id, include_state=True):
        transaction_dir = self._transaction_dir(transaction_id)
        backup_dir = self.updater_dir / "backups" / transaction_id
        backup_dir.mkdir(parents=True, exist_ok=False)
        archive = backup_dir / "panel-backup.tar.gz"
        original_files = []
        original_state_files = []
        updater_snapshots = {}
        with tarfile.open(archive, "w:gz") as tar:
            if self.root.exists():
                for path in self.root.rglob("*"):
                    if path.is_file() and self.updater_dir not in path.parents:
                        original_files.append(path.relative_to(self.root).as_posix())
                tar.add(self.root, arcname="panel", recursive=True)
            data_dir = self.state_dir / "data"
            if include_state and data_dir.is_dir():
                original_state_files = [
                    path.relative_to(data_dir).as_posix()
                    for path in data_dir.rglob("*")
                    if path.is_file()
                ]
                tar.add(data_dir, arcname="state/data", recursive=True)
        with archive.open("r+b") as stream:
            os.fsync(stream.fileno())
        fsync_directory(backup_dir)
        for name in ("baseline.json", "status.json"):
            source = self.updater_dir / name
            if source.is_file():
                shutil.copy2(source, backup_dir / f"{name}.before")
                with (backup_dir / f"{name}.before").open("r+b") as stream:
                    os.fsync(stream.fileno())
                updater_snapshots[name] = True
        metadata = {
            "archive": str(archive),
            "original_files": original_files,
            "original_state_files": original_state_files,
            "updater_snapshots": updater_snapshots,
        }
        durable_json(backup_dir / "metadata.json", metadata)
        durable_write(backup_dir / "backup.complete", b"complete\n", mode="wb")
        journal = self.read_journal(transaction_id)
        journal.update({"state": "backup", "backup": str(backup_dir)})
        self._write_journal(journal, transaction_dir)
        return backup_dir

    def _write_atomic(self, source, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".update-tmp")
        shutil.copyfile(source, temporary)
        shutil.copystat(source, temporary, follow_symlinks=False)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)

    def activate(self, transaction_id, baseline=None):
        transaction_dir = self._transaction_dir(transaction_id)
        journal = self.read_journal(transaction_id)
        managed = tuple(journal.get("managed") or ())
        conflicts = self.baseline_conflicts(baseline, managed)
        if conflicts:
            raise UpdateFailure("Обнаружены локальные изменения: " + ", ".join(item["file"] for item in conflicts))
        source_root = Path(journal["stage"])
        if "station-max-update-overlay-v2" in (source_root / "server.py").read_text(encoding="utf-8"):
            if not (self.root / "panel_update_integration.py").is_file() and not (source_root / "panel_update_integration.py").is_file():
                raise UpdateFailure("Не найден внешний panel_update_integration.py для безопасного updater")
        for name in managed:
            if name not in self.CONTROLLED_BOOTSTRAP_FILES:
                continue
            current = self.root / name
            source = source_root / name
            if current.is_file() and self.sha256(current) != self.sha256(source):
                raise UpdateFailure(f"Локальный bootstrap-файл нельзя заменить автоматически: {name}")
        backup_dir = self.create_backup(transaction_id)
        journal = self.read_journal(transaction_id)
        completed = []
        journal.update({"state": "activating", "completed": completed})
        self._write_journal(journal, transaction_dir)
        try:
            for name in managed:
                if name in self.PRESERVED_FILES or (name in self.CONTROLLED_BOOTSTRAP_FILES and (self.root / name).is_file()):
                    continue
                source = source_root / name
                if not source.is_file():
                    raise UpdateFailure(f"В staging отсутствует файл {name}")
                self._write_atomic(source, self.root / name)
                completed.append(name)
                journal["completed"] = completed
                self._write_journal(journal, transaction_dir)
            self.capture_baseline(managed)
        except Exception:
            self.rollback(transaction_id, backup_dir=backup_dir)
            raise
        journal.update({"state": "activated_pending_health"})
        self._write_journal(journal, transaction_dir)
        return journal

    def rollback(self, transaction_id, backup_dir=None):
        journal = self.read_journal(transaction_id)
        attempts = int(journal.get("rollback_attempts", 0)) + 1
        journal.update({"state": "rollback_started", "rollback_attempts": attempts})
        self._write_journal(journal, self._transaction_dir(transaction_id))
        backup_dir = Path(backup_dir or journal.get("backup", ""))
        archive = backup_dir / "panel-backup.tar.gz"
        metadata_path = backup_dir / "metadata.json"
        complete_path = backup_dir / "backup.complete"
        if not archive.is_file() or not metadata_path.is_file() or not complete_path.is_file():
            raise UpdateFailure("Резервная копия для rollback не найдена")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        original = set(metadata.get("original_files", ()))
        original_state = set(metadata.get("original_state_files", ()))
        managed = set(journal.get("managed", ()))
        for name in managed - original:
            target = self.root / name
            if target.is_file() or target.is_symlink():
                target.unlink()
        fsync_directory(self.root)
        data_dir = self.state_dir / "data"
        if data_dir.is_dir():
            for path in sorted(data_dir.rglob("*"), reverse=True):
                if path.is_file() and path.relative_to(data_dir).as_posix() not in original_state:
                    path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            fsync_directory(data_dir)
        for name in ("baseline.json", "status.json"):
            target = self.updater_dir / name
            snapshot = backup_dir / f"{name}.before"
            if snapshot.is_file():
                shutil.copy2(snapshot, target)
            elif target.is_file():
                target.unlink()
        with tarfile.open(archive, "r:gz") as tar:
            panel_members = [
                member for member in tar.getmembers()
                if member.name != "panel" and member.name.startswith("panel/")
            ]
            state_members = [
                member for member in tar.getmembers()
                if member.name != "state/data" and member.name.startswith("state/data/")
            ]
            self._restore_backup_members(tar, panel_members, self.root, "panel/")
            self._restore_backup_members(tar, state_members, data_dir, "state/data/")
        journal.update({"state": "rolled_back"})
        self._write_journal(journal, self._transaction_dir(transaction_id))
        return journal

    def mark_healthy(self, transaction_id):
        """Фиксируем успешный запуск отдельно от записи status.json."""
        journal = self.read_journal(transaction_id)
        journal.update({"state": "healthy", "healthy_at": time.time()})
        self._write_journal(journal, self._transaction_dir(transaction_id))
        return journal

    def close_staged(self, transaction_id, reason, conflicts):
        """Закрываем только незапущенную staged-транзакцию без удаления артефактов."""
        if not reason or not conflicts:
            raise UpdateFailure("Для закрытия staged-транзакции нужны причина и список конфликтов")
        transaction_dir = self._transaction_dir(transaction_id)
        journal_path = transaction_dir / self.JOURNAL_NAME
        journal = self.read_journal(transaction_id)
        state = journal.get("state")
        if state == "abandoned":
            return journal
        if state != "staged":
            raise UpdateFailure(f"Нельзя закрыть транзакцию в состоянии {state}")
        if journal.get("backup") or journal.get("completed"):
            raise UpdateFailure("Staged-транзакция уже содержит признаки активации")
        closure_path = transaction_dir / "closure.json"
        if closure_path.is_file():
            try:
                closure = json.loads(closure_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UpdateFailure("Файл closure.json повреждён") from exc
            if (closure.get("transaction") != transaction_id or
                    closure.get("reason") != str(reason) or
                    closure.get("conflicts") != [str(item) for item in conflicts]):
                raise UpdateFailure("Существующий closure.json не совпадает с повторной операцией")
        else:
            closure = {
                "transaction": transaction_id,
                "previous_state": state,
                "reason": str(reason),
                "conflicts": [str(item) for item in conflicts],
                "journal_sha256_before": self.sha256(journal_path),
                "closed_at": time.time(),
            }
            durable_json(closure_path, closure)
        history = list(journal.get("history") or [])
        history.append({"state": state, "at": closure["closed_at"]})
        journal.update({
            "state": "abandoned",
            "history": history,
            "closed_at": closure["closed_at"],
            "close_reason": str(reason),
            "close_conflicts": [str(item) for item in conflicts],
            "closure": str(closure_path),
        })
        self._write_journal(journal, transaction_dir)
        return journal

    def recover(self):
        """Восстанавливаем незавершённую транзакцию после аварийного старта."""
        transactions = self.updater_dir / "transactions"
        if not transactions.is_dir():
            return []
        recovered = []
        known_states = {
            "staged",
            "backup",
            "activating",
            "activated_pending_health",
            "rollback_started",
            "healthy",
            "rolled_back",
            "abandoned",
        }
        for journal_path in sorted(transactions.glob(f"*/{self.JOURNAL_NAME}")):
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            state = journal.get("state")
            if state not in known_states:
                raise UpdateFailure(
                    f"Неизвестное состояние транзакции {journal.get('id')}: {state}"
                )
            if state in {"backup", "activating"}:
                self.rollback(journal["id"])
                recovered.append(journal["id"])
            elif state == "rollback_started":
                attempts = int(journal.get("rollback_attempts", 0))
                if attempts >= self.MAX_RECOVERY_ROLLBACK_ATTEMPTS:
                    raise UpdateFailure(
                        "Лимит автоматических попыток rollback исчерпан; требуется ручное восстановление"
                    )
                # rollback() повторно восстанавливает полный backup поверх уже
                # частично восстановленных файлов. Все операции внутри него
                # идемпотентны, а journal остаётся rollback_started до финальной
                # durable-записи rolled_back.
                self.rollback(journal["id"])
                recovered.append(journal["id"])
        return recovered

    def health_check(self, url, expected_version, headers=None, timeout=10):
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status}"
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            return False, str(exc)
        actual = str(payload.get("version") or "") if isinstance(payload, dict) else ""
        if actual != str(expected_version):
            return False, f"ожидалась версия {expected_version}, получена {actual or 'неизвестно'}"
        return True, "ok"


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Восстановление staged-обновления yapanel-web_max")
    parser.add_argument("command", choices=("apply", "rollback", "recover", "close-staged"))
    parser.add_argument("transaction", nargs="?")
    parser.add_argument("--root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--archive")
    parser.add_argument("--version")
    parser.add_argument("--sha256")
    parser.add_argument("--commit")
    parser.add_argument("--reason")
    parser.add_argument("--conflict", action="append", dest="conflicts")
    args = parser.parse_args(argv)
    agent = UpdateAgent(args.root, args.state)
    if args.command == "apply":
        if not args.archive or not args.version or not args.sha256:
            parser.error("для apply нужны --archive, --version и --sha256")
        transaction, manifest = agent.stage(
            Path(args.archive).read_bytes(),
            version=args.version,
            expected_sha256=args.sha256,
            commit=args.commit or args.version,
        )
        baseline_path = agent.updater_dir / "baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        journal = agent.activate(transaction, baseline=baseline)
        print(json.dumps({
            "transaction": journal["id"],
            "version": journal["version"],
            "state": journal["state"],
        }, ensure_ascii=False))
    elif args.command == "rollback":
        if not args.transaction:
            parser.error("для rollback нужен transaction")
        agent.rollback(args.transaction)
        print("Откат выполнен")
    elif args.command == "close-staged":
        if not args.transaction or not args.reason or not args.conflicts:
            parser.error("для close-staged нужны transaction, --reason и хотя бы один --conflict")
        journal = agent.close_staged(args.transaction, args.reason, args.conflicts)
        print(json.dumps({"transaction": args.transaction, "state": journal["state"]}, ensure_ascii=False))
    else:
        print(json.dumps({"recovered": agent.recover()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
