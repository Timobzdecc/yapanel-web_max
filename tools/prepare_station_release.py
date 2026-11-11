"""Собираем локальный initial-release из проверенного официального ZIP.

Официальный asset остаётся источником всех файлов 0.3.11. Локально заменяются
только server.py, static/app.js и updater-компоненты, необходимые для сохранения
штатной кнопки обновления после первой миграции.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path


PATCHED_FILES = (
    "server.py",
    "static/app.js",
    "update_agent.py",
    "panel_update_integration.py",
    "station_overlay.py",
    "updater_bootstrap.py",
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_root(extracted):
    extracted = Path(extracted)
    candidates = [extracted, *[path for path in extracted.iterdir() if path.is_dir()]]
    for candidate in candidates:
        if (candidate / "server.py").is_file():
            return candidate
    raise RuntimeError("В официальном ZIP не найден server.py")


def prepare(asset, output, source_root, expected_sha256, version):
    asset = Path(asset)
    source_root = Path(source_root).resolve()
    if sha256(asset).lower() != expected_sha256.lower():
        raise RuntimeError("Контрольная сумма официального asset не совпадает")
    with tempfile.TemporaryDirectory(prefix="yapanel-station-release-") as temporary:
        extracted = Path(temporary) / "release"
        with zipfile.ZipFile(asset) as archive:
            archive.extractall(extracted)
        root = _content_root(extracted)
        if (root / "VERSION").read_text(encoding="utf-8").strip() != str(version):
            raise RuntimeError("Официальный ZIP содержит другую версию")
        for name in PATCHED_FILES:
            source = source_root / name
            if not source.is_file():
                raise RuntimeError(f"Локальный патч отсутствует: {name}")
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in extracted.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(extracted).as_posix())
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="Подготовка локального ZIP миграции Station Max")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--version", default="0.3.11")
    args = parser.parse_args(argv)
    output = prepare(args.asset, args.output, args.source_root, args.sha256, args.version)
    print(f"Подготовлен initial-release: {output}")
    print(f"SHA-256 initial-release: {sha256(output)}")


if __name__ == "__main__":
    main()
