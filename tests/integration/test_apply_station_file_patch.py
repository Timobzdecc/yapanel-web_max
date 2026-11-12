"""Сквозная проверка maintenance helper в изолированной fixture-станции."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
EMULATOR = ROOT / "tests" / "fixtures" / "maintenance_patch_station.py"
ARCHIVE = ROOT / "station-patch-0.3.11" / "backups" / "yapanel-station-0.3.11-20261113-001049.tar.gz"
COMPONENTS = (
    "update_agent.py",
    "station_overlay.py",
    "updater_bootstrap.py",
    "panel_update_integration.py",
    "max_profile.py.patch",
)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_member(archive: Path, member: str) -> bytes:
    with tarfile.open(archive, "r:gz") as source:
        return source.extractfile(member).read()


def _prepare_fixture(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    fixture = tmp_path / "station"
    panel = fixture / "data" / "yapanel"
    updater = fixture / "data" / "yapanel-state" / "updater"
    panel.mkdir(parents=True)
    updater.mkdir(parents=True)
    (fixture / "odm" / "bin").mkdir(parents=True)
    (fixture / "system").mkdir()
    (fixture / "odm" / "bin" / "rootd").write_bytes(b"fixture-rootd\n")
    original_max_profile = _extract_member(ARCHIVE, "yapanel/max_profile.py")
    (panel / "max_profile.py").write_bytes(original_max_profile)
    (panel / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    (panel / "server.py").write_text("# fixture server\n", encoding="utf-8")
    (panel / "start.sh").write_text(
        "#!/system/bin/sh\n"
        "PANEL_ALLOW_NETWORKS=192.168.31.0/24\n"
        "exec /data/python/bin/python3 /data/yapanel-state/updater/updater_bootstrap.py "
        "--device local --host 0.0.0.0 --port 8765\n",
        encoding="utf-8",
    )
    (updater / "start.sh.before-migration").write_bytes(b"#!/system/bin/sh\nexec legacy\n")
    original_start_sha = _sha(updater / "start.sh.before-migration")
    (updater / "start.sh.before-migration.json").write_text(json.dumps({
        "original_sha256": original_start_sha,
        "patched_sha256": _sha(panel / "start.sh"),
    }), encoding="utf-8")
    old_files = {}
    for name in COMPONENTS[:-1]:
        data = _extract_member(ARCHIVE, f"yapanel-state/updater/{name}")
        old_files[name] = data
        (updater / name).write_bytes(data)
        os.chmod(updater / name, 0o600)
    old_hash = _sha(panel / "max_profile.py")
    (updater / "baseline.json").write_text(json.dumps({"max_profile.py": old_hash, "server.py": "fixture-server"}), encoding="utf-8")
    transaction = "20261112-102200-309777a8"
    transaction_dir = updater / "transactions" / transaction
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": transaction,
        "state": "healthy",
        "version": "0.3.11",
        "managed": ["max_profile.py"],
    }), encoding="utf-8")
    (updater / "status.json").write_text(json.dumps({
        "state": "healthy",
        "version": "0.3.11",
        "transaction": transaction,
    }), encoding="utf-8")
    (fixture / "emulator-state.json").write_text(json.dumps({
        "service": "running",
        "panel_pid": "10011",
        "next_pid": 10012,
    }), encoding="utf-8")
    return fixture, old_files


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/panel/health":
            self.send_response(404)
            self.end_headers()
            return
        payload = b'{"ready":true,"version":"0.3.11","critical_errors":0}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def _run_operation(tmp_path: Path, fixture: Path, scenario: str, *operation: str):
    command_dir = tmp_path / "transport"
    command_dir.mkdir(parents=True)
    (command_dir / "fake_station_ssh.cmd").write_text(
        f'@echo off\npython.exe "{EMULATOR}" ssh %*\n', encoding="utf-8"
    )
    adb = command_dir / "fake_station_adb.cmd"
    adb.write_text(
        f'@echo off\n'
        f'if /i "%~3"=="push" python.exe "{EMULATOR}" adb %1 %2 %3 %4 %5\n'
        f'if /i "%~1"=="connect" python.exe "{EMULATOR}" adb %1 %2\n'
        f'if /i not "%~3"=="push" if /i not "%~1"=="connect" python.exe "{EMULATOR}" adb %1 %2 %3 "%~4"\n',
        encoding="utf-8",
    )
    adb_ps = command_dir / "fake_station_adb.ps1"
    adb_ps.write_text(
        f'& python.exe "{EMULATOR}" adb @args\nexit $LASTEXITCODE\n', encoding="utf-8"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = os.environ.copy()
    env.update({
        "YA_MAINTENANCE_FIXTURE": str(fixture),
        "YA_MAINTENANCE_SCENARIO": scenario,
        "YA_REPO_ROOT": str(ROOT),
    })
    work = tmp_path / "work"
    args = [
        str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "tools" / "apply_station_file_patch.ps1"),
        *operation,
        "-SshAlias", "fixture", "-AdbSerial", "fixture",
        "-SshExecutable", str(command_dir / "fake_station_ssh.cmd"),
        "-AdbExecutable", str(adb_ps if "-Apply" in operation else adb),
        "-HealthUri", f"http://127.0.0.1:{server.server_address[1]}",
        "-WorkDirectory", str(work),
    ]
    try:
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()
        server.server_close()
    return result


def _run_helper(tmp_path: Path, scenario: str, *operation: str):
    fixture, old_files = _prepare_fixture(tmp_path)
    if scenario == "baseline_conflict":
        baseline = fixture / "data" / "yapanel-state" / "updater" / "baseline.json"
        baseline.write_text(json.dumps({"max_profile.py": "unknown-baseline-hash"}), encoding="utf-8")
    result = _run_operation(tmp_path, fixture, scenario, *operation)
    return result, fixture, old_files


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 недоступен")
def test_real_maintenance_apply_and_restart_preserve_bundle_and_baseline(tmp_path):
    fixture, old_files = _prepare_fixture(tmp_path / "fixture")
    applied = _run_operation(tmp_path / "apply", fixture, "success", "-Apply")
    assert applied.returncode == 0, applied.stderr + applied.stdout
    transaction = next(line.split(":", 1)[1].strip() for line in applied.stdout.splitlines() if line.startswith("Transaction:"))
    restarted = _run_operation(tmp_path / "restart", fixture, "success", "-Restart", "-TransactionId", transaction)
    assert restarted.returncode == 0, restarted.stderr + restarted.stdout
    updater = fixture / "data" / "yapanel-state" / "updater"
    for name in COMPONENTS[:-1]:
        assert (updater / name).read_bytes() == (ROOT / name).read_bytes()
    assert (updater / "max_profile.py.patch").read_bytes() == (ROOT / "max_profile.py").read_bytes()
    metadata = json.loads((fixture / "fixture-metadata.json").read_text(encoding="utf-8"))
    for name in COMPONENTS:
        assert metadata[f"/data/yapanel-state/updater/{name}"] == "shell:shell:600"
    assert (fixture / "data" / "yapanel" / "max_profile.py").read_bytes() == (ROOT / "max_profile.py").read_bytes()
    journal = json.loads((updater / "transactions" / transaction / "transaction.json").read_text(encoding="utf-8"))
    assert journal["state"] == "healthy"
    old_journal = json.loads((updater / "transactions" / "20261112-102200-309777a8" / "transaction.json").read_text(encoding="utf-8"))
    assert old_journal["state"] == "healthy"
    baseline = json.loads((updater / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["max_profile.py"] == _sha(ROOT / "max_profile.py")
    assert baseline["server.py"] == "fixture-server"
    state = json.loads((fixture / "emulator-state.json").read_text(encoding="utf-8"))
    assert state["panel_pid"] == "10012"


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="Windows PowerShell 5.1 недоступен")
@pytest.mark.parametrize("scenario", ("baseline_conflict", "corrupt_payload", "incomplete_transfer", "hash_failure", "publish_failure_second", "publish_failure_third"))
def test_maintenance_faults_leave_active_updater_consistent(tmp_path, scenario):
    result, fixture, old_files = _run_helper(tmp_path, scenario, "-Apply")
    assert result.returncode != 0
    updater = fixture / "data" / "yapanel-state" / "updater"
    old_bundle = {name: data for name, data in old_files.items()}
    new_bundle = {name: (ROOT / name).read_bytes() for name in old_files}
    if scenario == "baseline_conflict":
        actual = {name: (updater / name).read_bytes() for name in old_files}
        assert actual == old_bundle or actual == new_bundle
    else:
        for name, data in old_bundle.items():
            assert (updater / name).read_bytes() == data
        assert not (updater / "max_profile.py.patch").exists()
    journals = list((updater / "transactions").glob("*/transaction.json"))
    states = [json.loads(path.read_text(encoding="utf-8"))["state"] for path in journals]
    assert states.count("healthy") == 1
    if scenario == "baseline_conflict":
        assert states.count("staged") == 1
    else:
        assert len(journals) == 1

