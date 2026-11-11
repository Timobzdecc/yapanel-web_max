"""Сквозная проверка updater без обращения к реальной станции.

Тесты используют реальные временные каталоги, Python CLI, дочерние HTTP-
процессы и PowerShell-контракт. Android init моделируется повторным запуском
внешнего bootstrap после завершения дочернего процесса.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import update_agent


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe")
if not POWERSHELL:
    candidate = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    POWERSHELL = str(candidate) if candidate.is_file() else None


FAKE_SERVER = r'''# station-max-update-overlay-v2
# station-max-supervisor-v1
# station-max-health-overlay-v1
import argparse
import json
import os
import signal
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PATH_MARKER = "export PATH=/data/yapanel/bin:$PATH"
PANEL_SUPERVISOR_MODE = os.environ.get("PANEL_SUPERVISOR_MODE")
RESTART_HOOK = None


class PanelUpdater:
    pass


if False:
    from panel_update_integration import install as _install_station_updater
    _install_station_updater(PanelUpdater)


def _supervised_restart(_args):
    os._exit(75)


def _serve(version, mode):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/panel/health":
                payload = {
                    "ready": mode == "healthy",
                    "version": version,
                    "critical_errors": 0 if mode == "healthy" else 1,
                }
                self.send_response(200)
            elif self.path == "/api/status":
                payload = {"uptime": max(0.0, time.monotonic() - started)}
                self.send_response(200)
            elif self.path == "/api/panel/version":
                payload = {"version": version}
                self.send_response(200)
            else:
                self.send_response(404)
                payload = {"error": "not found"}
            body = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.timeout = 0.2
    stop_at = time.monotonic() + float(os.environ.get("FAKE_SERVER_LIFETIME", "2"))
    while time.monotonic() < stop_at and not stopping:
        server.handle_request()
    server.server_close()


def stop(*_args):
    global stopping
    stopping = True


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="local")
parser.add_argument("--model", default="max")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--https-port", default="0")
parser.add_argument("--data-dir")
args = parser.parse_args()
version = (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()
mode = os.environ.get("FAKE_HEALTH", "healthy")
stopping = False
started = time.monotonic()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
_serve(version, mode)
'''


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_common_panel(root: Path, version: str, marker: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.py").write_text(
        FAKE_SERVER.replace("# station-max-update-overlay-v2", f"{marker}\n# station-max-update-overlay-v2"),
        encoding="utf-8",
    )
    for name in (
        "config.py",
        "profiles.py",
        "max_profile.py",
        "max_privacy.py",
        "assistant_max.py",
        "assistant_socks.py",
    ):
        (root / name).write_text(f"VERSION = {version!r}\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    static = root / "static"
    static.mkdir(exist_ok=True)
    (static / "app.js").write_text(
        "/* station-max-update-ui-v2 */\n"
        "const statusEndpoint = '/api/panel/update/status';\n"
        "const pendingState = 'activated_pending_health';\n",
        encoding="utf-8",
    )
    (root / "start.sh").write_text(
        "#!/system/bin/sh\n"
        "exec /data/python/bin/python3 server.py --device local --host 0.0.0.0 --port 8765\n",
        encoding="utf-8",
    )


def _make_release(path: Path, version: str) -> Path:
    source = path / "release"
    _write_common_panel(source, version, f"# release-{version}")
    shutil.copy2(ROOT / "panel_update_integration.py", source / "panel_update_integration.py")
    for name in ("update_agent.py", "station_overlay.py", "updater_bootstrap.py"):
        shutil.copy2(ROOT / name, source / name)
    archive = path / f"release-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for file in source.rglob("*"):
            if file.is_file():
                output.write(file, Path("release") / file.relative_to(source))
    return archive


def _prepare_fixture(tmp_path: Path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    _write_common_panel(panel, "0.3.5", "# installed-0.3.5")
    (panel / "start.sh").write_bytes(b"#!/system/bin/sh\nexec legacy\n")
    agent = update_agent.UpdateAgent(panel, state_dir=state, python_executable=sys.executable)
    managed = agent._managed_files(panel)
    agent.capture_baseline(managed)
    updater = state / "updater"
    updater.mkdir(parents=True, exist_ok=True)
    for name in ("update_agent.py", "station_overlay.py", "updater_bootstrap.py"):
        shutil.copy2(ROOT / name, updater / name)
    original = panel / "start.sh"
    original_sha = sha256(original)
    patched = panel / "start.sh.bootstrap"
    patched.write_text(
        "#!/system/bin/sh\n"
        "exec /data/python/bin/python3 /data/yapanel-state/updater/updater_bootstrap.py\n",
        encoding="utf-8",
    )
    patched_sha = sha256(patched)
    shutil.copy2(original, updater / "start.sh.before-migration")
    (updater / "start.sh.before-migration.json").write_text(
        json.dumps({"original_sha256": original_sha, "patched_sha256": patched_sha}),
        encoding="utf-8",
    )
    import updater_bootstrap

    updater_bootstrap._prepare_wrapper_intent(
        panel, state, original_sha, patched_sha, "0.3.11"
    )
    shutil.copy2(patched, original)
    return panel, state, agent


def _run_cli(panel: Path, state: Path, archive: Path, version: str):
    digest = sha256(archive)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "update_agent.py"),
            "apply",
            "--root",
            str(panel),
            "--state",
            str(state),
            "--archive",
            str(archive),
            "--version",
            version,
            "--sha256",
            digest,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_bootstrap(panel: Path, state: Path, port: int, health: str = "healthy"):
    environment = os.environ.copy()
    environment.update({"FAKE_HEALTH": health, "FAKE_SERVER_LIFETIME": "1.4"})
    command = [
        sys.executable,
        str(ROOT / "updater_bootstrap.py"),
        "--root",
        str(panel),
        "--state",
        str(state),
        "--python",
        sys.executable,
        "--health-timeout",
        "4",
        "--",
        "--device",
        "local",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        str(state / "data"),
    ]
    return subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15)


def _journal(state: Path, transaction: str) -> dict:
    return json.loads(
        (state / "updater" / "transactions" / transaction / "transaction.json").read_text(
            encoding="utf-8"
        )
    )


def test_real_cli_bootstrap_health_and_second_release(tmp_path):
    panel, state, agent = _prepare_fixture(tmp_path)
    first_archive = _make_release(tmp_path / "first", "0.3.11")

    result = _run_cli(panel, state, first_archive, "0.3.11")
    assert result["state"] == "activated_pending_health"
    assert _journal(state, result["transaction"])["state"] == "activated_pending_health"

    first_bootstrap = _run_bootstrap(panel, state, 18765)
    assert first_bootstrap.returncode == 0, first_bootstrap.stderr
    status = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "healthy"
    assert _journal(state, result["transaction"])["state"] == "healthy"
    assert (state / "updater" / "updater_bootstrap.py").is_file()

    second_archive = _make_release(tmp_path / "second", "0.3.12")
    second = _run_cli(panel, state, second_archive, "0.3.12")
    assert second["state"] == "activated_pending_health"
    second_bootstrap = _run_bootstrap(panel, state, 18766)
    assert second_bootstrap.returncode == 0, second_bootstrap.stderr
    status = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "healthy"
    assert status["version"] == "0.3.12"


def test_real_health_failure_rolls_back_to_legacy(tmp_path):
    panel, state, _agent = _prepare_fixture(tmp_path)
    archive = _make_release(tmp_path / "failed", "0.3.11")
    result = _run_cli(panel, state, archive, "0.3.11")

    bootstrap = _run_bootstrap(panel, state, 18767, health="failed")
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.5"
    assert (panel / "start.sh").read_bytes() == b"#!/system/bin/sh\nexec legacy\n"
    assert _journal(state, result["transaction"])["state"] == "rolled_back"
    status = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "rolled_back"


def test_cli_payload_is_accepted_by_windows_powershell_contract(tmp_path):
    if POWERSHELL is None or os.name != "nt":
        pytest.skip("Windows PowerShell 5.1 недоступен")
    panel, state, _agent = _prepare_fixture(tmp_path)
    archive = _make_release(tmp_path / "contract", "0.3.11")
    result = _run_cli(panel, state, archive, "0.3.11")
    result_path = tmp_path / "cli-result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    contract = ROOT / "tools" / "station_apply_contract.ps1"
    journal_path = state / "updater" / "transactions" / result["transaction"] / "transaction.json"
    command = textwrap.dedent(
        f"""
        function Invoke-StationSsh {{ param([string]$RemoteCommand); Get-Content -Raw -LiteralPath '{journal_path}' }}
        . '{contract}'
        $result = Get-Content -Raw -LiteralPath '{result_path}' | ConvertFrom-Json
        $journal = Assert-StationApplyContract $result '0.3.11'
        Write-Output ($journal.id + '|' + $journal.state)
        """
    )
    checked = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == f"{result['transaction']}|activated_pending_health"


def test_windows_powershell_harness_runs_real_cli_and_contract(tmp_path):
    if POWERSHELL is None or os.name != "nt":
        pytest.skip("Windows PowerShell 5.1 недоступен")
    panel, state, _agent = _prepare_fixture(tmp_path)
    archive = _make_release(tmp_path / "powershell", "0.3.11")
    result_path = tmp_path / "powershell-result.json"
    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "tools" / "run_local_update_harness.ps1"),
        "-Python",
        sys.executable,
        "-UpdateAgent",
        str(ROOT / "update_agent.py"),
        "-Contract",
        str(ROOT / "tools" / "station_apply_contract.ps1"),
        "-Root",
        str(panel),
        "-State",
        str(state),
        "-Archive",
        str(archive),
        "-Version",
        "0.3.11",
        "-ResultPath",
        str(result_path),
    ]
    checked = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert result["state"] == "activated_pending_health"
    assert _journal(state, result["transaction"])["state"] == "activated_pending_health"


def test_baseline_conflict_blocks_before_backup_or_file_change(tmp_path):
    panel, state, agent = _prepare_fixture(tmp_path)
    original = (panel / "server.py").read_bytes()
    (panel / "server.py").write_bytes(original + b"\n# unknown local modification\n")
    archive = _make_release(tmp_path / "conflict", "0.3.11")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "update_agent.py"),
            "apply",
            "--root",
            str(panel),
            "--state",
            str(state),
            "--archive",
            str(archive),
            "--version",
            "0.3.11",
            "--sha256",
            sha256(archive),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert (panel / "server.py").read_bytes() == original + b"\n# unknown local modification\n"
    assert not list((state / "updater" / "backups").glob("*/backup.complete"))


def test_power_loss_after_partial_activation_recovers_real_files(tmp_path):
    panel, state, agent = _prepare_fixture(tmp_path)
    archive = _make_release(tmp_path / "partial", "0.3.11")
    transaction, _manifest = agent.stage(
        archive.read_bytes(), version="0.3.11", expected_sha256=sha256(archive)
    )
    backup = agent.create_backup(transaction)
    journal = agent.read_journal(transaction)
    source_root = Path(journal["stage"])
    shutil.copy2(source_root / "server.py", panel / "server.py")
    (panel / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    journal.update({"state": "activating", "completed": ["server.py", "VERSION"]})
    agent._write_journal(journal, agent._transaction_dir(transaction))

    bootstrap = _run_bootstrap(panel, state, 18768)
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.5"
    assert _journal(state, transaction)["state"] == "rolled_back"
    assert json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))["state"] == "rolled_back"
    assert (panel / "start.sh").read_bytes() == b"#!/system/bin/sh\nexec legacy\n"
    assert backup.joinpath("backup.complete").is_file()


def test_unknown_journal_fails_closed_without_starting_child(tmp_path):
    panel, state, _agent = _prepare_fixture(tmp_path)
    transaction_dir = state / "updater" / "transactions" / "unknown"
    transaction_dir.mkdir(parents=True, exist_ok=True)
    (transaction_dir / "transaction.json").write_text(
        json.dumps({"id": "unknown", "state": "future_state", "version": "0.3.11"}),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "updater_bootstrap.py"),
            "--root",
            str(panel),
            "--state",
            str(state),
            "--python",
            sys.executable,
            "--health-timeout",
            "1",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            "18769",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.7)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
    status = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "rollback_failed"
    assert "Неизвестное состояние" in status["error"]


def test_web_ui_runs_two_real_cli_releases(tmp_path, monkeypatch):
    import access
    import server

    panel, state, _agent = _prepare_fixture(tmp_path)
    archives = {
        "0.3.11": _make_release(tmp_path / "web-first", "0.3.11"),
        "0.3.12": _make_release(tmp_path / "web-second", "0.3.12"),
    }
    versions = iter(("0.3.11", "0.3.12"))
    events = []

    class ImmediateTimer:
        def __init__(self, _delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    class Handler(server.Handler):
        __test__ = False

    class Panel:
        conn = SimpleNamespace(online=True)

        def __getattr__(self, _name):
            return lambda *args, **kwargs: {}

    auth = access.PanelAccess(tmp_path / "auth.json")
    auth.set_password("", "integration-password", "integration")
    Handler.panel = Panel()
    Handler.access = auth
    Handler.ports = {}
    updater = server.PanelUpdater(
        restart=lambda: events.append("restart"),
        config=SimpleNamespace(server=SimpleNamespace(data_dir=state / "data")),
    )

    def check(refresh=False):
        version = next(versions)
        return {"latest": version, "update": True, "install": "zip"}

    def local_from_zip(version):
        result = _run_cli(panel, state, archives[version], version)
        updater._save_update_status(
            state=result["state"], version=version,
            transaction=result["transaction"], error=None,
        )

    updater.check = check
    updater._from_zip = local_from_zip
    Handler.updater = updater
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    session = auth._new_session("integration")
    cookie = f"{access.COOKIE}={session}"
    monkeypatch.setattr(server.threading, "Timer", ImmediateTimer)

    def post_update():
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        try:
            connection.request(
                "POST", "/api/panel/update", body=b"{}",
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    try:
        first_status, first = post_update()
        assert first_status == 200, first
        assert first["state"] == "activated_pending_health"
        assert events == ["restart"]
        first_bootstrap = _run_bootstrap(panel, state, 18770)
        assert first_bootstrap.returncode == 0, first_bootstrap.stderr

        second_status, second = post_update()
        assert second_status == 200
        assert second["state"] == "activated_pending_health"
        second_bootstrap = _run_bootstrap(panel, state, 18771)
        assert second_bootstrap.returncode == 0, second_bootstrap.stderr
        status = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
        assert status["state"] == "healthy"
        assert status["version"] == "0.3.12"
        assert events == ["restart", "restart"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
