"""Проверяем и извлекаем обязательные файлы station backup без Windows tar metadata."""
from __future__ import annotations

import argparse
import hashlib
import tarfile
from pathlib import Path


REQUIRED = ("yapanel/VERSION", "yapanel/start.sh", "yapanel/server.py")


def safe_member(name):
    path = Path(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Небезопасный путь в backup: {name}")
    return path


def extract_required(archive, output, expected_version):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as source:
        members = {safe_member(member.name).as_posix(): member for member in source.getmembers()}
        for name in REQUIRED:
            member = members.get(name)
            if member is None or not member.isfile():
                raise ValueError(f"В backup отсутствует обязательный файл: {name}")
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError(f"Не удалось извлечь: {name}")
            destination.write_bytes(stream.read())
    version = (output / "yapanel/VERSION").read_text(encoding="utf-8").strip()
    if version != expected_version:
        raise ValueError(f"В backup версия {version}, ожидалась {expected_version}")
    digest = hashlib.sha256((output / "yapanel/start.sh").read_bytes()).hexdigest()
    print(f"version={version}")
    print(f"start_sha256={digest}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка station backup")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    extract_required(args.archive, args.output, args.version)


if __name__ == "__main__":
    main()
