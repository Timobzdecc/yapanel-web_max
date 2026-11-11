"""Создаёт baseline hashes для согласованной чистой версии панели.

Пример:
    python tools/make_updater_baseline.py v0.3.5 updater-baseline.json
    python tools/make_updater_baseline.py v0.3.5 updater-baseline.json station-overrides.json
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    args = list(argv or sys.argv[1:])
    if len(args) not in (2, 3, 4):
        raise SystemExit("Использование: make_updater_baseline.py TAG OUTPUT [OVERRIDES] [LINEAGE]")
    tag, output_name = args[:2]
    overrides = {}
    if len(args) >= 3:
        overrides = json.loads(Path(args[2]).read_text(encoding="utf-8"))
    lineage = {}
    lineage_document = {}
    if len(args) == 4:
        lineage_document = json.loads(Path(args[3]).read_text(encoding="utf-8"))
        lineage = lineage_document.get("files", {})
        if not isinstance(lineage, dict):
            raise SystemExit("LINEAGE должен содержать объект files")
    names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", tag], text=True).splitlines()
    managed = [
        name for name in names
        if name.endswith(".py")
        or name in {"VERSION", "LICENSE", "README.md", "CONTRIBUTION.md"}
        or name.startswith(("api/", "static/", "device/"))
    ]
    result = {}
    for name in managed:
        data = subprocess.check_output(["git", "show", f"{tag}:{name}"])
        clean_hash = hashlib.sha256(data).hexdigest()
        if name in overrides:
            result[name] = {"base": clean_hash, "approved": [str(overrides[name])]}
        else:
            result[name] = clean_hash
    for name, metadata in lineage.items():
        digest = str(metadata.get("sha256", "")).lower()
        if not name or "/" not in name or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SystemExit(f"Некорректная lineage-запись: {name}")
        entry = result.get(name, {"base": None, "approved": []})
        if not isinstance(entry, dict):
            entry = {"base": entry, "approved": []}
        approved = [str(value).lower() for value in entry.get("approved", [])]
        if digest not in approved:
            approved.append(digest)
        entry["approved"] = approved
        entry["lineage"] = {
            "repository": lineage_document.get("repository"),
            "release": lineage_document.get("release"),
            "archive_asset": lineage_document.get("archive_asset"),
            **metadata,
        }
        result[name] = entry
    output = Path(output_name)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Создан baseline для {tag}: {len(result)} файлов -> {output}")


if __name__ == "__main__":
    main()
