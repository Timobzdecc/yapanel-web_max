"""Готовим внешний bootstrap hook для проверенного start.sh Station Max."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


EXPECTED_START_SHA256 = "61d13d10a194dc31d1225fc2c094f9ecadf8b4c2655e7139ced1eaa9c6d44f36"
BOOTSTRAP_MARKER = 'exec "$PY" -B "$STATE/updater/updater_bootstrap.py"'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patch(source, expected_sha256=EXPECTED_START_SHA256):
    raw = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    actual = hashlib.sha256(raw).hexdigest()
    if BOOTSTRAP_MARKER.encode("utf-8") in raw:
        return raw, {"changed": False, "sha256": actual, "reason": "already-present"}
    if actual.lower() != expected_sha256.lower():
        raise ValueError(f"Неожиданный SHA-256 start.sh: {actual}")
    old = b'exec "$PY" -B server.py \\\n'
    new = (
        b'exec "$PY" -B "$STATE/updater/updater_bootstrap.py" \\\n'
        b'    --root "$BASE" \\\n'
        b'    --state "$STATE" \\\n'
        b'    --python "$PY" \\\n'
        b'    -- \\\n'
    )
    if raw.count(old) != 1:
        raise ValueError("Не найдено ровно одно место запуска server.py")
    patched = raw.replace(old, new, 1)
    required = (
        b'--device local',
        b'--model max',
        b'--host 0.0.0.0',
        b'--port 8765',
        b'--https-port 0',
        b'--data-dir "$STATE/data"',
    )
    if any(item not in patched for item in required):
        raise ValueError("После patch потеряны параметры запуска панели")
    return patched, {"changed": True, "sha256": hashlib.sha256(patched).hexdigest(), "reason": "patched"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Подготовка start.sh к внешнему updater bootstrap")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-sha256", default=EXPECTED_START_SHA256)
    args = parser.parse_args(argv)
    source = Path(args.input).read_bytes()
    patched, result = patch(source, args.expected_sha256)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(patched)
    print(result)


if __name__ == "__main__":
    main()
