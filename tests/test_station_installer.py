from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
if not POWERSHELL:
    _powershell_candidate = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    POWERSHELL = str(_powershell_candidate) if _powershell_candidate.is_file() else None


def test_station_installer_uses_graceful_handoff_without_ctl_control():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    assert "[Text.Encoding]::UTF8.GetBytes($RemoteCommand)" in source
    assert "/system/bin/base64 -d | /system/bin/sh" in source
    assert "--command-base64" in source
    assert "kill -TERM" in source
    assert "Invoke-StationPanelHandoff" in source
    assert "setprop ctl.stop" not in source
    assert "setprop ctl.start" not in source


def test_station_installer_transport_overrides_keep_production_defaults():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    assert '[string]$SshExecutable = "ssh.exe"' in source
    assert '[string]$AdbExecutable = "adb.exe"' in source
    assert '[string]$HealthUri = "http://192.168.31.196:8765"' in source
    assert "--adb $AdbExecutable" in source
    assert "-SshExecutable $SshExecutable" in source
    assert "$originalError = $_" in source
    assert "Write-Error $originalError.Exception.Message -ErrorAction Continue" in source


def test_station_snapshot_keeps_original_launch_invariants():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    for marker in (
        "START_DEVICE_LOCAL",
        "START_HOST",
        "START_PORT",
        "START_NETWORKS",
        "START_CA",
    ):
        assert marker in source


def test_station_installer_requires_explicit_generated_lineage():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    assert "station-max-generated-lineage.json" in source
    assert "строгая lineage-запись" in source
    assert "@($entry.approved).Count -ne 1" in source


def test_station_installer_requires_the_reviewed_local_approved_hashes():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    for digest in (
        "dd9411f9d60c30371fee282ad3d74f0d67a2e37266ded2cd48ad9293bd0d9e74",
        "ac5e1ca84e3c9f10c59819164d7a8a6ee8c38a2f0e9df860f3cccd55fc9823e4",
        "b7a797f1a53a9439689381d2c46c314abc139267e8760517b8a78410327c91de",
    ):
        assert digest in source
    assert "В baseline указан неподтверждённый approved hash" in source


def test_staged_transaction_closure_has_read_only_guards_and_no_lifecycle_control():
    source = (ROOT / "tools" / "close_staged_station_transaction.ps1").read_text(encoding="utf-8")

    assert "test \"$state\" = staged" in source
    assert "active_updater_status" in source
    assert "active_updater_process" in source
    assert "closurePython" in source
    assert "journal_sha256_before" in source
    assert "state=abandoned" in source
    assert "ctl.stop" not in source
    assert "ctl.start" not in source
    assert "SIGTERM" not in source


def test_staged_failure_restores_launch_wrapper_without_transaction_id():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    assert "function Restore-LaunchWrapper" in source
    assert "--prepare-wrapper" in source
    assert "Assert-StationApplyContract" in source
    assert "Find-StationRecoveryTransaction" in source
    assert "wrapper-intent.json" in source
    assert "$launchWrapperMayHaveChanged = $true" in source
    assert "if ($launchWrapperMayHaveChanged -and -not $handoffAttempted -and $activationEvidence)" in source
    assert "Restore-LaunchWrapper" in source
    assert "start.sh.before-migration.json" in source
    assert "_restore_launch_wrapper" not in source
    assert "source_meta=\"$(stat -c '%u:%g:%a' \"$source\")\"" in source
    assert "test \"$(sha256sum \"$target\" | sed 's/ .*//')\" = \"$expected\"" in source


def test_wrapper_is_installed_before_apply_and_journal_is_verified():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")
    contract = (ROOT / "tools" / "station_apply_contract.ps1").read_text(encoding="utf-8")

    assert source.index("--prepare-wrapper") < source.index("update_agent.py apply")
    assert "station_apply_contract.ps1" in source
    assert "station_activation_sequence" not in source
    assert not (ROOT / "tools" / "station_activation_sequence.ps1").exists()
    assert "Get-StationTransactionJournal" in contract
    assert "transaction id CLI не совпадает с durable journal" in contract
    assert "Durable journal не подтверждает activated_pending_health" in contract
    assert "if ($activationEvidence -and $transaction)" in source
    assert "Invoke-StationDirectRollback $transaction $false" not in source


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell недоступен")
def test_powershell_classifies_activation_and_unknown_journals_as_recovery(tmp_path):
    helper = ROOT / "tools" / "station_apply_contract.ps1"
    command = f"""
. '{helper}'
$activating = [pscustomobject]@{{ state = 'activating' }}
$rollback = [pscustomobject]@{{ state = 'rollback_started' }}
$unknown = [pscustomobject]@{{ state = 'future_state' }}
$staged = [pscustomobject]@{{ state = 'staged' }}
Write-Output ((Test-StationRecoveryRequired $activating), (Test-StationRecoveryRequired $rollback), (Test-StationRecoveryRequired $unknown), (Test-StationRecoveryRequired $staged) -join ',')
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "True,True,True,False"


def test_panel_handoff_accepts_relative_and_absolute_server_argv_safely():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    assert "server.py|/data/yapanel/server.py" in source
    assert "/data/python/bin/python3|/data/python/bin/python3.*" in source
    assert "readlink \"$proc/cwd\"" in source
    assert "PANEL_CANDIDATE_COUNT" in source
    assert "updater_bootstrap.py" in source
    assert "Stop-ConfirmedPanelProcess" in source
    assert "Wait-StationSupervisorRollback" in source
    assert 'temporary="$target.restore-tmp"' in source
    assert 'mv -f "$temporary" "$target"' in source


def test_rollback_order_stops_child_before_direct_file_rollback():
    source = (ROOT / "tools" / "install_station_max.ps1").read_text(encoding="utf-8")

    helper_index = source.index("function Invoke-StationDirectRollback")
    rollback_index = source.index("update_agent.py rollback", helper_index)
    assert "Stop-ConfirmedPanelProcess $runningPid" not in source[helper_index:rollback_index]
    assert "Direct rollback запрещён после handoff" in source
    assert "kill -9" not in source


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell недоступен")
@pytest.mark.parametrize(
    ("handoff", "service", "pid", "candidates", "owner", "expected"),
    (
        ("true", "running", "123", "1", "yes", "supervisor"),
        ("true", "stopped", "", "0", "no", "supervisor"),
        ("false", "running", "123", "1", "no", "blocked"),
        ("false", "stopped", "", "0", "yes", "blocked"),
        ("false", "stopped", "", "0", "no", "direct"),
    ),
)
def test_rollback_policy_blocks_concurrent_file_restore(
        handoff, service, pid, candidates, owner, expected,
):
    policy = ROOT / "tools" / "station_rollback_policy.ps1"
    command = (
        f". '{policy}'; "
        f"Get-StationRollbackOwner -HandoffAttempted:${handoff} "
        f"-ServiceState '{service}' -PanelPid '{pid}' "
        f"-CandidateCount '{candidates}' -SupervisorOwner '{owner}'"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == expected
