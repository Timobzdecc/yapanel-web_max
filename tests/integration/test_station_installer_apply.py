"""Запускаем настоящий install_station_max.ps1 в изолированной fixture-станции."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
FIXTURE = ROOT / "tests" / "fixtures"
BACKUP_CANDIDATES = sorted(ROOT.glob("station-install-review*/backups/*.tar.gz"))
ASSET_CANDIDATES = sorted(ROOT.glob("station-install-0.3.11/yapanel-web-0.3.11-windows-arm64.zip"))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _extract_backup(archive: Path, root: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            name = Path(member.name.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts:
                raise AssertionError(f"Небезопасный backup member: {member.name}")
        source.extractall(root / "data", filter="data")


def _prepare_fixture(tmp_path: Path, scenario: str) -> tuple[Path, Path, Path]:
    if not BACKUP_CANDIDATES or not ASSET_CANDIDATES:
        pytest.skip("Нужны подтверждённый station snapshot и локальный 0.3.11 asset")
    fixture = tmp_path / "station"
    fixture.mkdir()
    _extract_backup(BACKUP_CANDIDATES[-1], fixture)
    if scenario == "baseline_conflict":
        server = fixture / "data/yapanel/server.py"
        server.write_bytes(server.read_bytes() + b"\n# unknown fixture modification\n")
    (fixture / "odm" / "bin").mkdir(parents=True)
    (fixture / "system").mkdir()
    (fixture / "odm" / "bin" / "rootd").write_bytes(b"fixture-rootd\n")
    (fixture / "emulator-state.json").write_text(
        json.dumps({
            "service": "running",
            "panel_pid": "10011",
            "next_pid": 10012,
            "handoff_count": 0,
            "bootstrap_started": False,
            "handoff_attempted": False,
        }),
        encoding="utf-8",
    )
    return fixture, BACKUP_CANDIDATES[-1], ASSET_CANDIDATES[-1]


def _run_installer(tmp_path: Path, scenario: str, health: str = "healthy") -> tuple[subprocess.CompletedProcess, Path]:
    fixture, _backup, asset = _prepare_fixture(tmp_path, scenario)
    port = _port()
    environment = os.environ.copy()
    environment.update({
        "YA_STATION_FIXTURE": str(fixture),
        "YA_STATION_SCENARIO": scenario,
        "YA_REPO_ROOT": str(ROOT),
        "YA_STATION_HEALTH": health,
    })
    server = subprocess.Popen(
        [sys.executable, str(FIXTURE / "station_health_server.py"), str(fixture), str(port)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    work = tmp_path / "work"
    stdout_path = tmp_path / "installer.stdout.log"
    stderr_path = tmp_path / "installer.stderr.log"
    command = [
        str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "tools" / "install_station_max.ps1"),
        "-Apply",
        "-SshAlias", "fixture",
        "-AdbSerial", "fixture",
        "-SshExecutable", str(FIXTURE / "fake_station_ssh.cmd"),
        "-AdbExecutable", str(FIXTURE / "fake_station_adb.cmd"),
        "-HealthUri", f"http://127.0.0.1:{port}",
        "-HealthAttempts", "3",
        "-HealthDelaySeconds", "0",
        "-SupervisorWaitAttempts", "30",
        "-SupervisorWaitDelaySeconds", "0",
        "-AssetPath", str(asset),
        "-WorkDirectory", str(work),
    ]
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                command, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr, text=True, timeout=180,
            )
        result = subprocess.CompletedProcess(
            command,
            completed.returncode,
            stdout_path.read_text(encoding="utf-8"),
            stderr_path.read_text(encoding="utf-8"),
        )
    finally:
        server.terminate()
        server.wait(timeout=10)
    return result, fixture


def _state(fixture: Path) -> dict:
    return json.loads((fixture / "emulator-state.json").read_text(encoding="utf-8"))


def _journal_states(fixture: Path) -> list[dict]:
    result = []
    for path in (fixture / "data/yapanel-state/updater/transactions").glob("*/transaction.json"):
        result.append(json.loads(path.read_text(encoding="utf-8")))
    return result


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 недоступен")
@pytest.mark.parametrize("scenario", ("success", "failed_health", "power_loss_before_handoff", "apply_error_after_activation"))
def test_production_installer_apply_runs_real_activation_paths(tmp_path, scenario):
    result, fixture = _run_installer(tmp_path, scenario, health="failed" if scenario != "success" else "healthy")
    panel = fixture / "data/yapanel"
    if scenario == "success":
        assert result.returncode == 0, result.stderr + result.stdout
        assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.11"
        assert _state(fixture)["handoff_count"] == 1
        assert any(item.get("state") == "healthy" for item in _journal_states(fixture))
    else:
        assert result.returncode != 0
        assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.5"
        assert (panel / "start.sh").read_bytes() == (fixture / "data/yapanel-state/updater/start.sh.before-migration").read_bytes()
        assert any(item.get("state") == "rolled_back" for item in _journal_states(fixture))


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 недоступен")
@pytest.mark.parametrize("scenario", ("baseline_conflict", "wrapper_install_failure", "supervisor_unavailable"))
def test_production_installer_apply_failures_preserve_safe_boundary(tmp_path, scenario):
    result, fixture = _run_installer(tmp_path, scenario, health="failed" if scenario == "supervisor_unavailable" else "healthy")
    panel = fixture / "data/yapanel"
    assert result.returncode != 0
    expected_start = (
        "7424f025be9b9171038bf2761833dbdc914bb687bfc4127c049187e075851067"
        if scenario == "supervisor_unavailable"
        else "61d13d10a194dc31d1225fc2c094f9ecadf8b4c2655e7139ced1eaa9c6d44f36"
    )
    assert _sha(panel / "start.sh") == expected_start
    if scenario != "supervisor_unavailable":
        assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.5"
    else:
        assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.11"
        assert _state(fixture)["bootstrap_started"] is True
        assert json.loads((fixture / "data/yapanel-state/updater/status.json").read_text(encoding="utf-8"))["state"] == "rollback_failed"


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 недоступен")
def test_production_installer_uses_binary_backup_and_real_json_contract(tmp_path):
    result, fixture = _run_installer(tmp_path, "success")
    assert result.returncode == 0, result.stderr + result.stdout
    archives = list((tmp_path / "work/backups").glob("*.tar.gz"))
    assert len(archives) == 1
    assert _sha(archives[0])
    with tarfile.open(archives[0], "r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    assert {"yapanel/VERSION", "yapanel/start.sh", "yapanel/server.py"}.issubset(names)
    assert any(item.get("state") == "healthy" for item in _journal_states(fixture))
