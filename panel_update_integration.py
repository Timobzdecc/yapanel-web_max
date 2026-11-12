"""Сохраняем безопасный updater при загрузке чистого upstream server.py.

Файл не является частью upstream-релиза. station_overlay добавляет hook после
объявления PanelUpdater, а этот модуль подменяет только update-интеграцию.
Остальная логика конкретной версии панели остаётся upstream-коду.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import update_agent
from update_agent import durable_json


INTEGRATION_ID = "station-panel-updater-v2"


def _state_dir(instance):
    """На Android updater хранится отдельно от каталога исходников панели."""
    configured = Path(getattr(instance.config.server, "data_dir", Path(__file__).resolve().parent))
    candidates = []
    if configured.name == "data":
        candidates.append(configured.parent)
    candidates.append(configured)
    source_root = Path(__file__).resolve().parent
    candidates.append(source_root.parent / "yapanel-state")
    candidates.append(Path("/data/yapanel-state"))
    for candidate in candidates:
        if (candidate / "updater").is_dir():
            return candidate
    return candidates[0]


def _status_path(instance):
    return _state_dir(instance) / "updater" / "status.json"


def _save_status(instance, **updates):
    target = _status_path(instance)
    target.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if target.is_file():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    current.update(updates)
    durable_json(target, current)


def _status(instance):
    target = _status_path(instance)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"state": "unknown"}
    except (OSError, ValueError):
        return {"state": "idle"}


def _from_zip(instance, version):
    import server

    repo = getattr(instance.config.server, "github_repo", server.REPO)
    release_url = f"https://api.github.com/repos/{repo}/releases/tags/v{version}"
    with urllib.request.urlopen(urllib.request.Request(
            release_url,
            headers={"User-Agent": f"yapanel-web/{server.VERSION}",
                     "Accept": "application/vnd.github+json"}), timeout=30) as response:
        release = json.load(response)
    if release.get("tag_name") != f"v{version}":
        raise server.AdbError("GitHub вернул release с неожиданным tag")
    assets = release.get("assets") if isinstance(release, dict) else None
    asset_name = os.environ.get("PANEL_UPDATE_ASSET", "windows-arm64.zip")
    asset = next((item for item in assets or () if item.get("name", "").endswith(asset_name)), None)
    if not asset or not asset.get("browser_download_url"):
        raise server.AdbError(f"В релизе {version} отсутствует проверяемый asset {asset_name}")
    digest = str(asset.get("digest") or "")
    download_url = str(asset.get("browser_download_url") or "")
    if not download_url.startswith(f"https://github.com/{repo}/releases/download/"):
        raise server.AdbError("Release asset находится не на ожидаемом GitHub URL")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise server.AdbError("GitHub не предоставил SHA-256 для release asset")
    _save_status(instance, state="downloading", version=version, transaction=None, error=None)
    with urllib.request.urlopen(urllib.request.Request(
            asset["browser_download_url"], headers={"User-Agent": f"yapanel-web/{server.VERSION}"}), timeout=180) as response:
        blob = response.read()
    state_dir = _state_dir(instance)
    agent = update_agent.UpdateAgent(server.ROOT, state_dir=state_dir)
    try:
        transaction, manifest = agent.stage(
            blob,
            version=version,
            expected_sha256=digest.split(":", 1)[1],
            commit=str(release.get("tag_name") or version),
        )
    except update_agent.UpdateFailure as exc:
        _save_status(instance, state="blocked", version=version, transaction=None, error=str(exc))
        raise server.AdbError(str(exc)) from exc
    _save_status(instance, state="preflight", version=version, transaction=transaction,
                 files=len(manifest["managed"]), error=None)
    baseline_path = agent.updater_dir / "baseline.json"
    if not baseline_path.is_file():
        message = "Не найден baseline текущей установки; обновление заблокировано"
        _save_status(instance, state="blocked", version=version, transaction=transaction, error=message)
        raise server.AdbError(message)
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        agent.activate(transaction, baseline=baseline)
    except (OSError, ValueError, update_agent.UpdateFailure) as exc:
        _save_status(instance, state="blocked", version=version, transaction=transaction, error=str(exc))
        raise server.AdbError(str(exc)) from exc
    _save_status(instance, state="activated_pending_health", version=version,
                 transaction=transaction, error=None)
    return manifest


def install(panel_updater_class):
    """Подключить безопасный updater к PanelUpdater из upstream-кода."""
    if getattr(panel_updater_class, "_station_update_integration", None) == INTEGRATION_ID:
        return

    panel_updater_class._save_update_status = _save_status
    panel_updater_class.status = _status
    panel_updater_class._from_zip = _from_zip
    panel_updater_class._station_update_integration = INTEGRATION_ID
