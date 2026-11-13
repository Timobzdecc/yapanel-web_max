"""Изолированные сценарии staged-обновления без станции и сети."""
from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from update_agent import UpdateAgent, UpdateFailure
import station_overlay
import updater_bootstrap
from tools.patch_station_start import patch as patch_station_start


REQUIRED = {
    "server.py": "import config\nimport max_privacy\nimport assistant_socks\n",
    "config.py": "VALUE = 1\n",
    "profiles.py": "PROFILES = {}\n",
    "max_profile.py": "import max_privacy\n",
    "max_privacy.py": "VALUE = 1\n",
    "assistant_max.py": "VALUE = 1\n",
    "assistant_socks.py": "VALUE = 1\n",
    "VERSION": "0.3.11\n",
    "start.sh": "#!/system/bin/sh\nexport PANEL_ALLOW_NETWORKS=192.168.31.0/24\n",
    "api/__init__.py": "",
    "api/router.py": "VALUE = 1\n",
    "static/index.html": "<html></html>\n",
    "device/probe.sh": "#!/system/bin/sh\n",
}


def make_archive(version="0.3.11", extra=None):
    files = dict(REQUIRED)
    files["VERSION"] = version + "\n"
    files.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr("release/" + name, content)
    return output.getvalue()


def make_static_arm64_elf():
    header = bytearray(160)
    header[:4] = b"\x7fELF"
    header[4:6] = bytes((2, 1))
    struct.pack_into("<H", header, 18, 183)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<H", header, 54, 56)
    struct.pack_into("<H", header, 56, 1)
    struct.pack_into("<I", header, 64, 1)
    return bytes(header) + b"go1.22.12"


def seed_root(path):
    for name, content in REQUIRED.items():
        if name == "start.sh":
            continue
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.replace("0.3.11", "0.3.5"), encoding="utf-8")
    (path / "start.sh").write_text(REQUIRED["start.sh"], encoding="utf-8")


def test_apply_cli_returns_state_from_durable_activation_journal(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    (panel / "panel_update_integration.py").write_text(
        "def install(_updater):\n    return None\n", encoding="utf-8"
    )
    archive = subprocess.check_output(
        ["git", "archive", "--format=zip", "--prefix=release/", "v0.3.11"],
        cwd=Path(__file__).parents[1],
    )

    # Получаем manifest тем же UpdateAgent, но не активируем подготовительную
    # транзакцию. Реальный subprocess ниже выполняет именно CLI apply.
    probe = UpdateAgent(panel, state)
    transaction, manifest = probe.stage(archive, version="0.3.11", apply_overlays=False)
    baseline = {
        name: probe.sha256(panel / name)
        for name in manifest["managed"]
        if (panel / name).is_file()
    }
    shutil.rmtree(probe._transaction_dir(transaction))
    (state / "updater").mkdir(parents=True, exist_ok=True)
    (state / "updater" / "baseline.json").write_text(
        json.dumps(baseline), encoding="utf-8"
    )
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(archive)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "update_agent.py"),
            "apply",
            "--root", str(panel),
            "--state", str(state),
            "--archive", str(archive_path),
            "--version", "0.3.11",
            "--sha256", hashlib.sha256(archive).hexdigest(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    journal = json.loads(
        (state / "updater" / "transactions" / payload["transaction"] / "transaction.json")
        .read_text(encoding="utf-8")
    )
    assert payload == {
        "transaction": journal["id"],
        "version": journal["version"],
        "state": journal["state"],
    }
    assert payload["state"] == "activated_pending_health"


def test_powershell_accepts_real_cli_payload_against_durable_journal(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows PowerShell-тест запускается только в Windows")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        candidate = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        if candidate.is_file():
            powershell = str(candidate)
    if not powershell:
        pytest.skip("Windows PowerShell недоступен")

    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    (panel / "panel_update_integration.py").write_text(
        "def install(_updater):\n    return None\n", encoding="utf-8"
    )
    archive = subprocess.check_output(
        ["git", "archive", "--format=zip", "--prefix=release/", "v0.3.11"],
        cwd=Path(__file__).parents[1],
    )
    probe = UpdateAgent(panel, state)
    transaction, manifest = probe.stage(archive, version="0.3.11", apply_overlays=False)
    baseline = {
        name: probe.sha256(panel / name)
        for name in manifest["managed"]
        if (panel / name).is_file()
    }
    shutil.rmtree(probe._transaction_dir(transaction))
    (state / "updater").mkdir(parents=True, exist_ok=True)
    (state / "updater" / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(archive)
    result = subprocess.run(
        [
            sys.executable, str(Path(__file__).parents[1] / "update_agent.py"), "apply",
            "--root", str(panel), "--state", str(state), "--archive", str(archive_path),
            "--version", "0.3.11", "--sha256", hashlib.sha256(archive).hexdigest(),
        ],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    result_path = tmp_path / "result.json"
    result_path.write_text(result.stdout, encoding="utf-8")
    journal_path = state / "updater" / "transactions" / payload["transaction"] / "transaction.json"
    helper = Path(__file__).parents[1] / "tools" / "station_apply_contract.ps1"
    result_file = str(result_path).replace("'", "''")
    journal_file = str(journal_path).replace("'", "''")
    helper_file = str(helper).replace("'", "''")
    command = f"""
function Invoke-StationSsh {{ param([string]$Command) Get-Content -Raw -LiteralPath '{journal_file}' }}
. '{helper_file}'
$payload = Get-Content -Raw -LiteralPath '{result_file}' | ConvertFrom-Json
$checked = Assert-StationApplyContract $payload '0.3.11'
if ([string]$checked.state -ne 'activated_pending_health') {{ exit 2 }}
try {{ Assert-StationApplyContract ([pscustomobject]@{{ transaction = $payload.transaction; version = $payload.version }}) '0.3.11' | Out-Null; exit 3 }} catch {{ exit 0 }}
"""
    checked = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True, text=True,
    )
    assert checked.returncode == 0, checked.stderr


def test_stage_copies_new_modules_and_rejects_bad_checksum(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    agent = UpdateAgent(panel, tmp_path / "state")
    blob = make_archive()

    with pytest.raises(UpdateFailure, match="Контрольная сумма"):
        agent.stage(blob, version="0.3.11", expected_sha256="0" * 64)

    transaction, manifest = agent.stage(blob, version="0.3.11", apply_overlays=False)
    stage = Path(agent.read_journal(transaction)["stage"])
    assert (stage / "max_privacy.py").is_file()
    assert (stage / "assistant_socks.py").is_file()
    assert "start.sh" not in manifest["managed"]


def test_baseline_generator_records_only_explicit_generated_lineage(tmp_path):
    output = tmp_path / "baseline.json"
    lineage = Path(__file__).parents[1] / "tools" / "station-max-generated-lineage.json"
    script = Path(__file__).parents[1] / "tools" / "make_updater_baseline.py"
    result = subprocess.run(
        [sys.executable, str(script), "v0.3.5", str(output),
         str(Path(__file__).parents[1] / "tools" / "station-max-v0.3.5-overrides.json"),
         str(lineage)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True,
    )
    assert "baseline" in result.stdout
    baseline = json.loads(output.read_text(encoding="utf-8"))
    overrides = {
        "server.py": "dd9411f9d60c30371fee282ad3d74f0d67a2e37266ded2cd48ad9293bd0d9e74",
        "max_profile.py": "ac5e1ca84e3c9f10c59819164d7a8a6ee8c38a2f0e9df860f3cccd55fc9823e4",
        "static/app.js": "b7a797f1a53a9439689381d2c46c314abc139267e8760517b8a78410327c91de",
    }
    for name, approved in overrides.items():
        assert baseline[name]["approved"] == [approved]
    entry = baseline["device/agent/build/yapanel-agent"]
    assert entry["base"] is None
    assert entry["approved"] == ["ff096256a027cbcff547c591d9c2622914d9078258978d9e64651a50f71b4761"]
    assert entry["lineage"]["repository"] == "Timobzdecc/yapanel-web_max"
    assert entry["lineage"]["release"] == "v0.3.4"
    lineage_hashes = {
        "device/agent/build/VERSION": "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2",
        "device/agent/build/yapanel-agent": "ff096256a027cbcff547c591d9c2622914d9078258978d9e64651a50f71b4761",
        "device/assistant/build/VERSION": "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2",
        "device/assistant/build/assistantd": "cd0e90d7045c194b88e81f0e9873c7ebd5d7b1586071b35834832b1f1b492df9",
        "device/viz/build/VERSION": "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2",
        "device/viz/build/vizd": "b61f4a152f614667ba538d652cb6695b5df020f4ceb4a77d2afa69112558755b",
    }
    for name, digest in lineage_hashes.items():
        assert baseline[name]["approved"] == [digest]


def test_manifest_excludes_windows_runtime_and_installers(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    agent = UpdateAgent(panel, tmp_path / "state")
    transaction, manifest = agent.stage(make_archive(extra={
        "runtime/python/python.exe": "windows",
        "install_to_max.bat": "windows",
        "install_to_max.ps1": "windows",
        "device/probe.exe": "windows",
    }), apply_overlays=False)
    assert transaction
    assert not any(name.startswith("runtime/") for name in manifest["managed"])
    assert not any(name.endswith((".bat", ".ps1", ".exe", ".dll", ".pyd")) for name in manifest["managed"])


def test_stage_rejects_corrupt_or_traversing_archive(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    agent = UpdateAgent(panel, tmp_path / "state")
    with pytest.raises(UpdateFailure, match="повреждён"):
        agent.stage(b"not a zip")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("release/../server.py", "print('bad')")
    with pytest.raises(UpdateFailure, match="Небезопасный путь"):
        agent.stage(output.getvalue())


def test_conflict_blocks_before_backup_and_preserves_start_script(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}
    (panel / "max_profile.py").write_text("# локальная правка\n", encoding="utf-8")

    with pytest.raises(UpdateFailure, match="локальные изменения"):
        agent.activate(transaction, baseline=baseline)

    assert "локальная правка" in (panel / "max_profile.py").read_text(encoding="utf-8")
    assert "192.168.31.0/24" in (panel / "start.sh").read_text(encoding="utf-8")
    assert not (state / "updater" / "backups").exists()


def test_installed_station_035_nine_baseline_conflicts_block_without_changes(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)

    conflict_files = {
        "CONTRIBUTION.md": {
            "installed_sha256": None,
            "baseline_sha256": "ae0c27b66d6d600a676a929b04c13a8cf90bfa7fc482ddcd6243ebde57e40a96",
        },
        "LICENSE": {
            "installed_sha256": None,
            "baseline_sha256": "8bd6f88648efb826282bb743c31072bd838c9e6e8091dca2ad0d8556561f0dc1",
        },
        "README.md": {
            "installed_sha256": None,
            "baseline_sha256": "a363760bfb5ea7773f06b36b592e8ff7b9500fc4cf8db5889e9fb4410e668915",
        },
        "device/agent/build/VERSION": {
            "installed_sha256": "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2",
            "size": 6,
        },
        "device/agent/build/yapanel-agent": {
            "installed_sha256": "ff096256a027cbcff547c591d9c2622914d9078258978d9e64651a50f71b4761",
            "size": 5308568,
        },
        "device/assistant/build/VERSION": {
            "installed_sha256": "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2",
            "size": 6,
        },
        "device/assistant/build/assistantd": {
            "installed_sha256": "cd0e90d7045c194b88e81f0e9873c7ebd5d7b1586071b35834832b1f1b492df9",
            "size": 5308568,
        },
        "device/viz/build/VERSION": {
            "installed_sha256": "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2",
            "size": 6,
        },
        "device/viz/build/vizd": {
            "installed_sha256": "b61f4a152f614667ba538d652cb6695b5df020f4ceb4a77d2afa69112558755b",
            "size": 2162840,
        },
    }
    for name, metadata in conflict_files.items():
        if metadata.get("installed_sha256"):
            target = panel / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((name + "\n").encode("utf-8"))

    generated_artifacts = {
        "device/agent/build/VERSION": "0.3.11\n",
        "device/agent/build/yapanel-agent": make_static_arm64_elf(),
        "device/assistant/build/VERSION": "0.3.11\n",
        "device/assistant/build/assistantd": make_static_arm64_elf(),
        "device/viz/build/VERSION": "0.3.11\n",
        "device/viz/build/vizd": make_static_arm64_elf(),
    }
    archive = make_archive(extra={
        "CONTRIBUTION.md": "upstream contribution\n",
        "LICENSE": "upstream license\n",
        "README.md": "upstream readme\n",
        **generated_artifacts,
    })
    transaction, manifest = agent.stage(archive, apply_overlays=False)
    baseline = {
        name: agent.sha256(panel / name)
        for name in manifest["managed"]
        if (panel / name).is_file()
    }
    baseline.update({
        name: metadata["baseline_sha256"]
        for name, metadata in conflict_files.items()
        if metadata.get("baseline_sha256")
    })
    for name, metadata in conflict_files.items():
        if not metadata.get("baseline_sha256"):
            baseline.pop(name, None)

    conflicts = agent.baseline_conflicts(baseline, manifest["managed"])
    assert {item["file"] for item in conflicts} == {
        name for name in conflict_files if name.startswith("device/")
    }
    assert set(manifest["optional_release_metadata"]) == {
        "CONTRIBUTION.md", "LICENSE", "README.md"
    }
    assert all(item["reason"] for item in conflicts)

    with pytest.raises(UpdateFailure, match="локальные изменения"):
        agent.activate(transaction, baseline=baseline)

    assert not (state / "updater" / "backups").exists()
    assert (panel / "start.sh").read_text(encoding="utf-8") == REQUIRED["start.sh"]
    assert agent.read_journal(transaction)["state"] == "staged"


def test_official_generated_lineage_accepts_known_v034_and_blocks_unknown_hash(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    agent = UpdateAgent(panel, tmp_path / "state")
    names = (
        "device/agent/build/VERSION",
        "device/agent/build/yapanel-agent",
        "device/assistant/build/VERSION",
        "device/assistant/build/assistantd",
        "device/viz/build/VERSION",
        "device/viz/build/vizd",
    )
    for name in names:
        target = panel / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"official-v0.3.4")
    hashes = {
        name: "027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2"
        if name.endswith("VERSION") else "f" * 64
        for name in names
    }
    agent.sha256 = lambda path: hashes[path.relative_to(panel).as_posix()]
    baseline = {
        name: {
            "base": None,
            "approved": [hashes[name]],
            "lineage": {"release": "v0.3.4", "asset": "official"},
        }
        for name in names
    }

    assert agent.baseline_conflicts(baseline, names) == []

    hashes[names[1]] = "0" * 64
    conflicts = agent.baseline_conflicts(baseline, names)
    assert [item["file"] for item in conflicts] == [names[1]]
    assert "официальным asset" in conflicts[0]["reason"]


def test_close_staged_is_atomic_idempotent_and_preserves_artifacts(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    transaction_dir = agent._transaction_dir(transaction)
    archive = transaction_dir.with_suffix(".zip")
    archive_hash = agent.sha256(archive)
    conflicts = ["README.md", "device/agent/build/yapanel-agent"]

    closed = agent.close_staged(transaction, "baseline_conflict", conflicts)
    assert closed["state"] == "abandoned"
    assert agent.read_journal(transaction)["state"] == "abandoned"
    assert (transaction_dir / "closure.json").is_file()
    assert archive.is_file()
    assert agent.sha256(archive) == archive_hash
    assert agent.recover() == []

    repeated = agent.close_staged(transaction, "baseline_conflict", conflicts)
    assert repeated["state"] == "abandoned"
    assert agent.sha256(archive) == archive_hash


def test_close_staged_rejects_active_transaction(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    journal = agent.read_journal(transaction)
    journal["state"] = "activated_pending_health"
    agent._write_journal(journal, agent._transaction_dir(transaction))

    with pytest.raises(UpdateFailure, match="Нельзя закрыть транзакцию"):
        agent.close_staged(transaction, "baseline_conflict", ["README.md"])


def test_generated_release_binaries_require_static_arm64_and_release_version(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    agent = UpdateAgent(panel, state)
    binaries = {
        "device/agent/build/yapanel-agent": make_static_arm64_elf(),
        "device/assistant/build/assistantd": make_static_arm64_elf(),
        "device/viz/build/vizd": make_static_arm64_elf(),
        "device/agent/build/VERSION": "0.3.11\n",
        "device/assistant/build/VERSION": "0.3.11\n",
        "device/viz/build/VERSION": "0.3.11\n",
    }
    transaction, manifest = agent.stage(make_archive(extra=binaries), apply_overlays=False)
    assert transaction
    assert all(name in manifest["managed"] for name in binaries)

    invalid = dict(binaries)
    invalid["device/viz/build/vizd"] = b"not an ELF"
    with pytest.raises(UpdateFailure, match="не является ELF"):
        agent.stage(make_archive(extra=invalid), apply_overlays=False)


def test_station_runtime_directories_are_preserved_from_shell_updater(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    (panel / "device" / "android_audio").mkdir(parents=True)
    (panel / "device" / "android_audio" / "StationAudio.dex").write_bytes(b"station runtime")
    (panel / "device" / "bt_memes").mkdir(parents=True)
    (panel / "device" / "bt_memes" / "bt-memes").write_bytes(b"station runtime")
    (panel / "__pycache__").mkdir()
    (panel / "__pycache__" / "config.cpython-314.pyc").write_bytes(b"generated cache")
    (panel / "server.py").write_text("# panel\n", encoding="utf-8")
    agent = UpdateAgent(panel, state)

    archive = make_archive(extra={
        "device/android_audio/StationAudio.dex": b"release runtime",
        "device/bt_memes/bt-memes": b"release runtime",
    })
    transaction, manifest = agent.stage(archive, version="0.3.11", apply_overlays=False)
    assert "device/android_audio/StationAudio.dex" not in manifest["managed"]
    assert "device/bt_memes/bt-memes" not in manifest["managed"]

    agent.create_backup(transaction)
    backup = state / "updater" / "backups" / transaction / "panel-backup.tar.gz"
    with tarfile.open(backup, "r:gz") as tar:
        names = tar.getnames()
    assert "panel/device/android_audio/StationAudio.dex" not in names
    assert "panel/device/bt_memes/bt-memes" not in names
    assert "panel/__pycache__/config.cpython-314.pyc" not in names


def test_reviewed_overlay_hash_allows_known_local_change(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, tmp_path / "state")
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    base = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}
    local = "# reviewed local overlay\n"
    (panel / "server.py").write_text(local, encoding="utf-8")
    base["server.py"] = {"base": base["server.py"], "approved": [agent.sha256(panel / "server.py")]}

    agent.activate(transaction, baseline=base)
    assert (panel / "server.py").read_text(encoding="utf-8") != local


def test_successful_activation_refreshes_baseline_for_repeat_update(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}

    agent.activate(transaction, baseline=baseline)
    refreshed = json.loads((state / "updater" / "baseline.json").read_text(encoding="utf-8"))
    assert refreshed["server.py"] == agent.sha256(panel / "server.py")
    journal = agent.read_journal(transaction)
    assert journal["state"] == "activated_pending_health"
    assert (Path(journal["backup"]) / "backup.complete").read_text(encoding="utf-8") == "complete\n"
    agent.mark_healthy(transaction)
    assert agent.read_journal(transaction)["state"] == "healthy"

    second_transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    agent.activate(second_transaction, baseline=refreshed)


def test_activation_preserves_state_and_rolls_back_partial_write(tmp_path, monkeypatch):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    (state / "data").mkdir(parents=True)
    (state / "data" / ".panel_settings.json").write_text('{"device":"local"}', encoding="utf-8")
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}

    original = agent._write_atomic
    calls = {"count": 0}

    def fail_on_second(source, destination):
        calls["count"] += 1
        if calls["count"] == 2:
            (state / "data" / ".panel_settings.json").write_text('{"device":"changed"}', encoding="utf-8")
            (state / "data" / "new-setting.json").write_text("temporary", encoding="utf-8")
            raise OSError("simulated write failure")
        return original(source, destination)

    monkeypatch.setattr(agent, "_write_atomic", fail_on_second)
    with pytest.raises(OSError, match="simulated write failure"):
        agent.activate(transaction, baseline=baseline)

    for name, content in REQUIRED.items():
        target = panel / name
        if target.is_file():
            assert target.read_text(encoding="utf-8") == content.replace("0.3.11", "0.3.5")
    assert json.loads((state / "data" / ".panel_settings.json").read_text(encoding="utf-8")) == {"device": "local"}
    assert not (state / "data" / "new-setting.json").exists()
    assert agent.read_journal(transaction)["state"] == "rolled_back"


def test_activation_durably_marks_activating_before_first_file(tmp_path, monkeypatch):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}
    states_seen = []
    original = agent._write_atomic

    def observe(source, destination):
        states_seen.append(agent.read_journal(transaction)["state"])
        return original(source, destination)

    monkeypatch.setattr(agent, "_write_atomic", observe)
    agent.activate(transaction, baseline=baseline)

    assert states_seen
    assert set(states_seen) == {"activating"}


def test_final_activation_journal_failure_keeps_recovery_phase(tmp_path, monkeypatch):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}
    original_write_journal = agent._write_journal

    def fail_final_journal(data, transaction_dir):
        if data.get("state") == "activated_pending_health":
            raise OSError("simulated final journal write failure")
        return original_write_journal(data, transaction_dir)

    monkeypatch.setattr(agent, "_write_journal", fail_final_journal)
    with pytest.raises(OSError, match="final journal"):
        agent.activate(transaction, baseline=baseline)

    assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.11"
    assert agent.read_journal(transaction)["state"] == "activating"

    monkeypatch.setattr(agent, "_write_journal", original_write_journal)
    assert agent.recover() == [transaction]
    assert (panel / "VERSION").read_text(encoding="utf-8").strip() == "0.3.5"
    assert agent.read_journal(transaction)["state"] == "rolled_back"


def test_recovery_rejects_unknown_transaction_state(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    journal = agent.read_journal(transaction)
    journal["state"] = "future_state"
    agent._write_journal(journal, agent._transaction_dir(transaction))

    with pytest.raises(UpdateFailure, match="Неизвестное состояние"):
        agent.recover()


def test_incomplete_backup_is_not_usable_for_rollback(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}
    backup = agent.create_backup(transaction)
    (backup / "backup.complete").unlink()

    with pytest.raises(UpdateFailure, match="Резервная копия"):
        agent.rollback(transaction, backup_dir=backup)


def test_rollback_restores_relative_and_absolute_symlinks_without_following_targets(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    (state / "data").mkdir(parents=True)
    relative_link = panel / "server-link"
    absolute_link = state / "data" / "python-link"
    try:
        os.symlink("server.py", relative_link)
        os.symlink("/data/python/bin/python3", absolute_link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"В окружении недоступно создание symlink: {exc}")

    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    backup = agent.create_backup(transaction)
    relative_link.unlink()
    absolute_link.unlink()

    agent.rollback(transaction, backup_dir=backup)

    assert relative_link.is_symlink()
    assert os.readlink(relative_link) == "server.py"
    assert absolute_link.is_symlink()
    assert os.readlink(absolute_link) == "/data/python/bin/python3"


def test_power_loss_recovery_retries_interrupted_symlink_rollback(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    link = panel / "server-link"
    try:
        os.symlink("/data/python/bin/python3", link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"В окружении недоступно создание symlink: {exc}")

    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    agent.create_backup(transaction)
    journal = agent.read_journal(transaction)
    journal.update({"state": "rollback_started", "rollback_attempts": 1})
    agent._write_journal(journal, agent._transaction_dir(transaction))

    assert agent.recover() == [transaction]

    assert agent.read_journal(transaction)["state"] == "rolled_back"
    assert link.is_symlink()
    assert os.readlink(link) == "/data/python/bin/python3"


def test_rollback_restores_panel_and_state_hardlinks(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    data_dir = state / "data"
    data_dir.mkdir(parents=True)
    panel_first = panel / "hardlink-first"
    panel_second = panel / "hardlink-second"
    state_first = data_dir / "hardlink-first"
    state_second = data_dir / "hardlink-second"
    panel_first.write_text("panel hardlink\n", encoding="utf-8")
    state_first.write_text("state hardlink\n", encoding="utf-8")
    try:
        os.link(panel_first, panel_second)
        os.link(state_first, state_second)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"В окружении недоступно создание hard link: {exc}")

    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    backup = agent.create_backup(transaction)

    with tarfile.open(backup / "panel-backup.tar.gz", "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert members["panel/hardlink-second"].islnk()
    assert members["state/data/hardlink-second"].islnk()
    assert members["panel/hardlink-second"].linkname in {"panel/hardlink-first", "hardlink-first"}
    assert members["state/data/hardlink-second"].linkname in {"state/data/hardlink-first", "hardlink-first"}

    archive_path = backup / "panel-backup.tar.gz"
    reordered_path = backup / "panel-backup-reordered.tar.gz"
    with tarfile.open(archive_path, "r:gz") as source:
        records = []
        for member in source.getmembers():
            payload = source.extractfile(member).read() if member.isfile() else None
            records.append((member, payload))
    with tarfile.open(reordered_path, "w:gz") as target:
        for member, payload in sorted(records, key=lambda item: not item[0].islnk()):
            target.addfile(member, io.BytesIO(payload) if payload is not None else None)
    reordered_path.replace(archive_path)

    panel_first.unlink()
    panel_second.unlink()
    state_first.unlink()
    state_second.unlink()
    agent.rollback(transaction, backup_dir=backup)

    assert panel_first.read_text(encoding="utf-8") == "panel hardlink\n"
    assert state_first.read_text(encoding="utf-8") == "state hardlink\n"
    assert os.stat(panel_first).st_ino == os.stat(panel_second).st_ino
    assert os.stat(state_first).st_ino == os.stat(state_second).st_ino
    assert os.stat(panel_first).st_nlink >= 2
    assert os.stat(state_first).st_nlink >= 2


def test_power_loss_recovery_restores_hardlinks_after_interrupted_activation(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    data_dir = state / "data"
    data_dir.mkdir(parents=True)
    first = panel / "hardlink-first"
    second = panel / "hardlink-second"
    first.write_text("power loss\n", encoding="utf-8")
    try:
        os.link(first, second)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"В окружении недоступно создание hard link: {exc}")

    agent = UpdateAgent(panel, state)
    transaction, _ = agent.stage(make_archive(), apply_overlays=False)
    agent.create_backup(transaction)
    journal = agent.read_journal(transaction)
    journal.update({"state": "activating", "completed": ["server.py"]})
    agent._write_journal(journal, agent._transaction_dir(transaction))
    first.unlink()
    second.unlink()
    (panel / "server.py").write_text("partial activation\n", encoding="utf-8")

    assert agent.recover() == [transaction]
    assert first.read_text(encoding="utf-8") == "power loss\n"
    assert os.stat(first).st_ino == os.stat(second).st_ino
    assert agent.read_journal(transaction)["state"] == "rolled_back"


def test_health_check_requires_expected_version(tmp_path, monkeypatch):
    agent = UpdateAgent(tmp_path / "panel", tmp_path / "state")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"version":"0.3.11"}'

    monkeypatch.setattr("update_agent.urllib.request.urlopen", lambda request, timeout=0: Response())
    assert agent.health_check("http://127.0.0.1/api/panel/version", "0.3.11")[0]
    assert not agent.health_check("http://127.0.0.1/api/panel/version", "0.4.0")[0]


def test_station_overlay_applies_only_to_known_v0311_server(tmp_path):
    source = subprocess.check_output(["git", "show", "v0.3.11:server.py"]).decode("utf-8")
    server = tmp_path / "server.py"
    server.write_bytes(source.encode("utf-8"))

    result = station_overlay.apply(tmp_path, "0.3.11")
    patched = server.read_text(encoding="utf-8")
    assert result["applied"] is True
    assert patched.count(station_overlay.PATH_MARKER) == 1
    assert station_overlay.HEALTH_MARKER in patched
    assert '"/api/panel/health"' in patched
    assert station_overlay.apply(tmp_path, "0.3.11")["applied"] is False

    server.write_bytes(source.replace("def shell(", "def other_shell(", 1).encode("utf-8"))
    with pytest.raises(RuntimeError, match="не нашёл|несовместим"):
        station_overlay.apply(tmp_path, "0.3.12")


def test_station_overlay_transfers_reviewed_max_profile_fallback(tmp_path):
    profile = tmp_path / "max_profile.py"
    profile.write_bytes(subprocess.check_output(["git", "show", "v0.3.11:max_profile.py"]))

    result = station_overlay._inject_max_profile(profile, "0.3.11")

    assert result["applied"] is True
    assert UpdateAgent.sha256(profile) == station_overlay.MAX_PROFILE_OVERLAY_SHA256["0.3.11"]
    assert "max_assistant_hook_fallback" in profile.read_text(encoding="utf-8")
    assert station_overlay._inject_max_profile(profile, "0.3.11")["applied"] is False


def test_station_overlay_applies_reviewed_v0312_release(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "server.py").write_bytes(subprocess.check_output(["git", "show", "v0.3.12:server.py"]))
    (tmp_path / "max_profile.py").write_bytes(subprocess.check_output(["git", "show", "v0.3.12:max_profile.py"]))
    (tmp_path / "static" / "app.js").write_bytes(subprocess.check_output(["git", "show", "v0.3.12:static/app.js"]))

    result = station_overlay.apply(tmp_path, "0.3.12")

    assert result["applied"] is True
    assert UpdateAgent.sha256(tmp_path / "max_profile.py") == station_overlay.MAX_PROFILE_OVERLAY_SHA256["0.3.12"]
    assert station_overlay.SERVER_MARKER in (tmp_path / "server.py").read_text(encoding="utf-8")
    assert station_overlay.HEALTH_MARKER in (tmp_path / "server.py").read_text(encoding="utf-8")
    assert station_overlay.STATIC_MARKER in (tmp_path / "static" / "app.js").read_text(encoding="utf-8")


def test_station_overlay_blocks_unknown_max_profile_revision(tmp_path):
    profile = tmp_path / "max_profile.py"
    profile.write_bytes(subprocess.check_output(["git", "show", "v0.3.11:max_profile.py"]))
    profile.write_text(profile.read_text(encoding="utf-8") + "\n# неизвестная правка\n", encoding="utf-8", newline="\n")

    with pytest.raises(RuntimeError, match="совместимый max_profile.py"):
        station_overlay._inject_max_profile(profile, "0.3.11")


def test_apply_patch_cli_uses_durable_transaction_and_refreshes_baseline(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    managed = agent._managed_files(panel)
    agent.capture_baseline(managed)
    baseline_path = state / "updater" / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["device/agent/build/yapanel-agent"] = {
        "base": None,
        "approved": ["official-lineage-hash"],
        "lineage": "v0.3.4",
    }
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    current_sha = agent.sha256(panel / "max_profile.py")
    patch = tmp_path / "max_profile.patch.py"
    patch.write_text("import max_privacy\nVALUE = 'station-overlay'\n", encoding="utf-8", newline="\n")
    target_sha = agent.sha256(patch)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "update_agent.py"),
            "apply-patch",
            "--root", str(panel),
            "--state", str(state),
            "--file", str(patch),
            "--version", "0.3.11",
            "--sha256", target_sha,
            "--expected-current-sha256", current_sha,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "activated_pending_health"
    assert payload["file"] == "max_profile.py"
    assert agent.sha256(panel / "max_profile.py") == target_sha
    baseline = json.loads((state / "updater" / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["max_profile.py"] == target_sha
    assert baseline["device/agent/build/yapanel-agent"]["approved"] == ["official-lineage-hash"]

    agent.rollback(payload["transaction"])

    assert agent.sha256(panel / "max_profile.py") == current_sha
    restored_baseline = json.loads((state / "updater" / "baseline.json").read_text(encoding="utf-8"))
    assert restored_baseline["max_profile.py"] == current_sha
    assert restored_baseline["device/agent/build/yapanel-agent"]["lineage"] == "v0.3.4"


def test_apply_patch_cli_targets_requested_server_file(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    agent.capture_baseline(agent._managed_files(panel))
    current_sha = agent.sha256(panel / "server.py")
    patch = tmp_path / "server.py.patch"
    patch.write_text("VALUE = 'assistant-autostart-overlay'\n", encoding="utf-8", newline="\n")
    target_sha = agent.sha256(patch)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "update_agent.py"),
            "apply-patch",
            "--root", str(panel),
            "--state", str(state),
            "--file", str(patch),
            "--target-file", "server.py",
            "--version", "0.3.12",
            "--sha256", target_sha,
            "--expected-current-sha256", current_sha,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["file"] == "server.py"
    assert agent.sha256(panel / "server.py") == target_sha


def test_station_start_patch_preserves_launch_parameters_and_is_idempotent():
    source = (
        b"#!/system/bin/sh\n"
        b"exec \"$PY\" -B server.py \\\n"
        b"    --device local \\\n"
        b"    --model max \\\n"
        b"    --host 0.0.0.0 \\\n"
        b"    --port 8765 \\\n"
        b"    --https-port 0 \\\n"
        b"    --data-dir \"$STATE/data\"\n"
    )
    expected = hashlib.sha256(source).hexdigest()
    patched, result = patch_station_start(source, expected)

    assert result["changed"] is True
    assert b'"$STATE/updater/updater_bootstrap.py"' in patched
    assert b"--device local" in patched
    assert b"--port 8765" in patched
    repeated, repeated_result = patch_station_start(patched, expected)
    assert repeated == patched
    assert repeated_result["changed"] is False

    with pytest.raises(ValueError, match="Неожиданный SHA-256"):
        patch_station_start(source, "0" * 64)


def test_sequential_upstream_releases_keep_external_updater_integration(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    for name in ("update_agent.py", "panel_update_integration.py", "station_overlay.py", "updater_bootstrap.py"):
        (panel / name).write_text("CONTROLLED = True\n", encoding="utf-8")
    future_server = """
import os
import sys

class Adb:
    def shell(self, cmd, timeout=15, check=True):
        if getattr(self, "is_local", False):
            return cmd

PUBLIC_PATHS = {"/api/panel-auth/login"}

class Handler:
    ready = False
    def do_GET(self):
        routes = {
            "/api/panel/version": lambda: self.updater.check(q.get("refresh") == "1"),
        }

class PanelUpdater:
    pass

def _max_assistant_autostart_worker():
    pass

def main():
    args = type("Args", (), {"port": 8765})()
    srv = type("Server", (), {"server_address": ("127.0.0.1", 8765)})()
    args.port = srv.server_address[1]

RESTART_HOOK = None   # (аргументы без имени скрипта) → перезапуск снаружи, см. android/

def start_panel(model, adb):
    if model == "max" and adb.is_local is True and Path("/system/bin/sh").is_file() and Path("/data/quasar").is_dir():
        _max_assistant_autostart_worker()

# Платформа колонки Станция Макс
"""
    future_static = """
if ($('#upd-go')) $('#upd-go').onclick = e => {};
loadPanelVersion().catch(() => {});
"""
    archive = make_archive(extra={"server.py": future_server, "static/app.js": future_static})
    agent = UpdateAgent(panel, state)

    first, first_manifest = agent.stage(archive, version="0.3.12")
    first_baseline = {name: agent.sha256(panel / name) for name in first_manifest["managed"] if (panel / name).is_file()}
    agent.activate(first, baseline=first_baseline)
    assert station_overlay.SERVER_MARKER in (panel / "server.py").read_text(encoding="utf-8")
    assert station_overlay.BRIDGE_CALL in (panel / "server.py").read_text(encoding="utf-8")
    assert station_overlay.SUPERVISOR_MARKER in (panel / "server.py").read_text(encoding="utf-8")
    assert station_overlay.ASSISTANT_AUTOSTART_MARKER in (panel / "server.py").read_text(encoding="utf-8")
    assert station_overlay.STATIC_MARKER in (panel / "static" / "app.js").read_text(encoding="utf-8")
    assert (panel / "panel_update_integration.py").is_file()

    second, second_manifest = agent.stage(archive, version="0.3.13")
    baseline = json.loads((state / "updater" / "baseline.json").read_text(encoding="utf-8"))
    agent.activate(second, baseline=baseline)
    assert station_overlay.SERVER_MARKER in (panel / "server.py").read_text(encoding="utf-8")
    assert station_overlay.STATIC_MARKER in (panel / "static" / "app.js").read_text(encoding="utf-8")
    assert second_manifest["version"] == "0.3.13"


def test_upstream_agent_stage_without_supervisor_protocol_is_blocked(tmp_path):
    source = """
import update_agent

class PanelUpdater:
    def update(self):
        agent.stage(blob)

# Платформа колонки Станция Макс
"""
    (tmp_path / "server.py").write_text(source, encoding="utf-8")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.js").write_text(
        "if ($('#upd-go')) $('#upd-go').onclick = e => {};\n"
        "loadPanelVersion().catch(() => {});\n", encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="supervisor protocol"):
        station_overlay.apply(tmp_path, "0.3.12")

    assert station_overlay.BRIDGE_CALL not in (tmp_path / "server.py").read_text(encoding="utf-8")


def test_future_release_cannot_replace_existing_bootstrap(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    for name in ("update_agent.py", "panel_update_integration.py", "station_overlay.py", "updater_bootstrap.py"):
        (panel / name).write_text("local bootstrap\n", encoding="utf-8")
    agent = UpdateAgent(panel, state)
    archive = make_archive(extra={"update_agent.py": "UPSTREAM = True\n"})
    transaction, manifest = agent.stage(archive, version="0.3.14", apply_overlays=False)
    baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}

    with pytest.raises(UpdateFailure, match="bootstrap-файл"):
        agent.activate(transaction, baseline=baseline)

    assert (panel / "update_agent.py").read_text(encoding="utf-8") == "local bootstrap\n"
    assert not (state / "updater" / "backups").exists()


def test_recovery_restores_baseline_after_interrupted_activation(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    old_baseline = {name: agent.sha256(panel / name) for name in manifest["managed"] if (panel / name).is_file()}
    (state / "updater").mkdir(parents=True, exist_ok=True)
    (state / "updater" / "baseline.json").write_text(json.dumps(old_baseline), encoding="utf-8")
    agent.create_backup(transaction)
    journal = agent.read_journal(transaction)
    journal.update({"state": "activating", "completed": ["server.py"]})
    agent._write_journal(journal, agent._transaction_dir(transaction))
    (panel / "server.py").write_text("partial activation\n", encoding="utf-8")
    (state / "updater" / "baseline.json").write_text(json.dumps({"server.py": "new-version"}), encoding="utf-8")

    assert agent.recover() == [transaction]
    assert json.loads((state / "updater" / "baseline.json").read_text(encoding="utf-8")) == old_baseline
    assert agent.read_journal(transaction)["state"] == "rolled_back"


def test_recovery_does_not_retry_interrupted_rollback_forever(tmp_path):
    panel = tmp_path / "panel"
    state = tmp_path / "state"
    panel.mkdir()
    seed_root(panel)
    agent = UpdateAgent(panel, state)
    transaction, manifest = agent.stage(make_archive(), apply_overlays=False)
    agent.create_backup(transaction)
    journal = agent.read_journal(transaction)
    journal.update({"state": "rollback_started", "rollback_attempts": 2})
    agent._write_journal(journal, agent._transaction_dir(transaction))

    with pytest.raises(UpdateFailure, match="Лимит автоматических попыток"):
        agent.recover()

    assert agent.read_journal(transaction)["state"] == "rollback_started"


def test_bootstrap_health_checks_pending_journal_when_status_was_lost(tmp_path, monkeypatch):
    state = tmp_path / "state"
    transaction_dir = state / "updater" / "transactions" / "transaction-1"
    transaction_dir.mkdir(parents=True)
    (tmp_path / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "transaction-1",
        "state": "activated_pending_health",
        "version": "0.3.11",
    }), encoding="utf-8")

    class Child:
        def wait(self, timeout=None):
            return 0

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = Path(state_dir) / "updater"
            self.marked = []

        def recover(self):
            return []

        def mark_healthy(self, transaction):
            self.marked.append(transaction)

    fake_agent = None

    def build_agent(root, state_dir, python_executable):
        nonlocal fake_agent
        fake_agent = FakeAgent(root, state_dir, python_executable)
        return fake_agent

    monkeypatch.setattr("update_agent.UpdateAgent", build_agent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: Child())
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda port, version, timeout: version == "0.3.11")

    assert updater_bootstrap.run(tmp_path, state, "python", ["--port", "8765"], health_timeout=0) == 0
    status = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "healthy"
    assert status["transaction"] == "transaction-1"
    assert fake_agent.marked == ["transaction-1"]


def test_bootstrap_recovers_interrupted_transaction_before_start(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (tmp_path / "VERSION").write_text("0.3.5\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "transaction-1",
    }), encoding="utf-8")
    transaction_dir = state / "updater" / "transactions" / "transaction-1"
    transaction_dir.mkdir(parents=True, exist_ok=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "transaction-1",
        "state": "rolled_back",
        "version": "0.3.11",
    }), encoding="utf-8")

    class Child:
        def wait(self, timeout=None):
            return 0

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = Path(state_dir) / "updater"

        def recover(self):
            return ["transaction-1"]

    started = []
    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: started.append(args) or Child())
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda port, version, timeout: True)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_legacy_health", lambda port, version, timeout: True)

    assert updater_bootstrap.run(tmp_path, state, "python", ["--port", "8765"]) == 0
    assert len(started) == 1
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "rolled_back"
    assert status["transaction"] == "transaction-1"


def test_bootstrap_prefers_external_updater_directory(tmp_path, monkeypatch):
    state = tmp_path / "state"
    external = state / "updater"
    external.mkdir(parents=True)
    (external / "update_agent.py").write_text(
        "from pathlib import Path\n"
        "class UpdateAgent:\n"
        "    def __init__(self, root, state_dir, python_executable):\n"
        "        self.updater_dir = Path(state_dir) / 'updater'\n"
        "    def recover(self):\n"
        "        return []\n"
        "class UpdateFailure(RuntimeError):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "update_agent.py").write_text("ROOT = True\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("0.3.5\n", encoding="utf-8")

    class Child:
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: Child())
    previous = sys.modules.pop("update_agent", None)
    try:
        assert updater_bootstrap.run(tmp_path, state, "python", ["--port", "8765"]) == 0
        assert Path(sys.modules["update_agent"].__file__).resolve() == (external / "update_agent.py").resolve()
    finally:
        sys.modules.pop("update_agent", None)
        if previous is not None:
            sys.modules["update_agent"] = previous


def test_bootstrap_rolls_back_when_new_process_does_not_pass_health(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (tmp_path / "VERSION").write_text("0.3.5\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "transaction-1",
    }), encoding="utf-8")
    transaction_dir = state / "updater" / "transactions" / "transaction-1"
    transaction_dir.mkdir(parents=True, exist_ok=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "transaction-1",
        "state": "activated_pending_health",
        "version": "0.3.11",
    }), encoding="utf-8")

    class Child:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = Path(state_dir) / "updater"
            self.rollback_calls = []

        def recover(self):
            return []

        def rollback(self, transaction):
            self.rollback_calls.append(transaction)

    children = []
    fake_agent = None

    def build_agent(root, state_dir, python_executable):
        nonlocal fake_agent
        fake_agent = FakeAgent(root, state_dir, python_executable)
        return fake_agent

    monkeypatch.setattr("update_agent.UpdateAgent", build_agent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda root, python, args: children.append(Child()) or children[-1])
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda port, version, timeout: False)
    monkeypatch.setattr(updater_bootstrap, "_hold_on_failure", lambda path, error: None)

    assert updater_bootstrap.run(tmp_path, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert fake_agent.rollback_calls == ["transaction-1"]
    assert len(children) == 2
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "rolled_back"


def test_bootstrap_health_requires_ready_expected_version_and_zero_errors(monkeypatch):
    class Response:
        def __init__(self, status, payload):
            self.status = status
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.payload

    responses = iter([
        Response(401, {"ready": True, "version": "0.3.11", "critical_errors": 0}),
        Response(200, {"ready": False, "version": "0.3.11", "critical_errors": 0}),
        Response(200, {"ready": True, "version": "0.3.10", "critical_errors": 0}),
        Response(200, {"ready": True, "version": "0.3.11", "critical_errors": 1}),
        Response(200, {"ready": True, "version": "0.3.11", "critical_errors": 0}),
    ])
    monkeypatch.setattr(updater_bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(updater_bootstrap.time, "sleep", lambda seconds: None)

    assert updater_bootstrap._wait_for_health(8765, "0.3.11", 0.01) is True


def test_bootstrap_does_not_retry_rollback_after_rollback_failure(tmp_path, monkeypatch):
    state = tmp_path / "state"
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({"state": "rollback_failed", "error": "старый процесс не запустился"}), encoding="utf-8")
    calls = []

    class Child:
        def wait(self, timeout=None):
            return 0

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = Path(state_dir) / "updater"

        def recover(self):
            return []

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: calls.append("start") or Child())
    class Held(Exception):
        pass

    def hold(path, error):
        calls.append(("hold", error))
        raise Held()

    monkeypatch.setattr(updater_bootstrap, "_hold_on_failure", hold)

    with pytest.raises(Held):
        updater_bootstrap.run(tmp_path, state, "python", ["--port", "8765"])
    assert calls == [("hold", "старый процесс не запустился")]
