[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$RecoverFailedPreparation,
    [string]$SshAlias = "station-max",
    [string]$AdbSerial = "192.168.31.196:5555",
    [string]$SshExecutable = "ssh.exe",
    [string]$AdbExecutable = "adb.exe",
    [string]$HealthUri = "http://192.168.31.196:8765",
    [int]$HealthAttempts = 60,
    [int]$HealthDelaySeconds = 1,
    [int]$SupervisorWaitAttempts = 60,
    [int]$SupervisorWaitDelaySeconds = 1,
    [string]$AssetPath,
    [string]$WorkDirectory = (Join-Path (Get-Location) "station-install-0.3.11"),
    [string]$ExpectedVersion = "0.3.11",
    [string]$CurrentVersion = "0.3.5"
)

$ErrorActionPreference = "Stop"
$repo = "Timobzdecc/yapanel-web_max"
$assetName = "windows-arm64.zip"
$assetDigest = "e528d1f9db31bbcb8e19b41341941f0defc605dab3d2ba429cf4ec928a176c1e"
$startDigest = "61d13d10a194dc31d1225fc2c094f9ecadf8b4c2655e7139ced1eaa9c6d44f36"
$patchedStartDigest = "7424f025be9b9171038bf2761833dbdc914bb687bfc4127c049187e075851067"
$python = (Get-Command python.exe -ErrorAction Stop).Source
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rootdControl = Join-Path $root "tools/rootd_control.py"
. (Join-Path $PSScriptRoot "station_rollback_policy.ps1")

function Invoke-StationSsh {
    param([string]$RemoteCommand)
    # PowerShell 5.1 может снять вложенные кавычки из аргумента ssh.exe.
    # Передаём тело команды как base64, чтобы Android shell получил исходные байты.
    $commandBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($RemoteCommand))
    $transport = "printf %s $commandBase64 | /system/bin/base64 -d | /system/bin/sh"
    $output = & $SshExecutable -o BatchMode=yes $SshAlias $transport 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("SSH-команда завершилась с кодом " + $LASTEXITCODE + ": " + $RemoteCommand + [Environment]::NewLine + ($output -join [Environment]::NewLine))
    }
    return @($output)
}

. (Join-Path $PSScriptRoot "station_apply_contract.ps1")

function Send-StationFile {
    param([string]$Source, [string]$Destination)
    $sshPath = (Get-Command $SshExecutable -ErrorAction Stop).Source
    $command = 'type "' + $Source + '" | "' + $sshPath + '" -o BatchMode=yes ' + $SshAlias + ' "cat > ' + $Destination + '"'
    & $env:ComSpec /d /s /c $command 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Не удалось передать файл по бинарному каналу SSH: $Source -> $Destination" }
}

function Receive-StationFile {
    param([string]$RemotePath, [string]$Destination)
    $sshPath = (Get-Command $SshExecutable -ErrorAction Stop).Source
    $command = '"' + $sshPath + '" -o BatchMode=yes ' + $SshAlias + ' "cat ' + $RemotePath + '" > "' + $Destination + '"'
    & $env:ComSpec /d /s /c $command 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать файл по SSH: $RemotePath -> $Destination" }
}

function Invoke-StationRootd {
    param([string]$RemoteCommand)
    $commandBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($RemoteCommand))
    $output = & $python $rootdControl --serial $AdbSerial --adb $AdbExecutable --command-base64 $commandBase64 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("rootd-команда завершилась с ошибкой: " + ($output -join [Environment]::NewLine))
    }
    try {
        $result = (($output -join [Environment]::NewLine) | ConvertFrom-Json)
    } catch {
        throw ("rootd вернул некорректный JSON: " + ($output -join [Environment]::NewLine))
    }
    if (-not $result.ok) {
        throw ("rootd не подтвердил команду: " + [string]$result.error)
    }
    return @(([string]$result.stdout) -split "`r?`n" | Where-Object { $_ -ne "" })
}

function Test-StationRootdControl {
    $probe = Invoke-StationRootd "id -u; id -Z; getprop init.svc.yapanel"
    if (-not ($probe -contains "0")) { throw "rootd не подтвердил UID 0" }
    if (-not ($probe -match "u:r:")) { throw "rootd не вернул SELinux-контекст" }
    if (-not ($probe -contains "running")) { throw "yapanel не находится в состоянии running перед установкой" }
    $context = $probe | Where-Object { $_ -like "context=*" } | Select-Object -First 1
    if ([string]$context -eq "context=u:r:shell:s0") {
        Write-Warning "rootd подтвердил UID 0, но команда выполняется в u:r:shell:s0; используется только graceful handoff процесса панели, без ctl.*."
    }
    Write-Output "rootd capability probe пройден; управляющие команды жизненного цикла пока не выполнялись."
}

function Invoke-StationPanelHandoff {
    param([string]$ExpectedPid)
    if ([string]::IsNullOrWhiteSpace($ExpectedPid) -or $ExpectedPid -notmatch '^\d+$') {
        throw "Не найден проверенный PID процесса панели для graceful handoff"
    }
    $command = @'
panel_pids=""
for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in
        /data/python/bin/python3|/data/python/bin/python3.*) ;;
        *) continue ;;
    esac
    [ "$(readlink "$proc/cwd" 2>/dev/null || true)" = "/data/yapanel" ] || continue
    script_ok=no
    port_ok=no
    expect_port=no
    for arg in $(tr '\000' '\n' < "$proc/cmdline" 2>/dev/null); do
        case "$arg" in
            server.py|/data/yapanel/server.py) script_ok=yes ;;
            --port=8765) port_ok=yes ;;
            --port) expect_port=yes ;;
            8765) [ "$expect_port" = yes ] && port_ok=yes; expect_port=no ;;
            *) expect_port=no ;;
        esac
    done
    [ "$script_ok" = yes ] && [ "$port_ok" = yes ] || continue
    ppid=$(cat "$proc/stat" 2>/dev/null | sed 's/.*) //' | cut -d ' ' -f 2)
    if [ "$ppid" != 1 ]; then
        parent_line=$(tr '\000' ' ' < "/proc/$ppid/cmdline" 2>/dev/null || true)
        case "$parent_line" in *updater_bootstrap.py*) ;; *) continue ;; esac
    fi
    panel_pids="$panel_pids ${proc##*/}"
done
set -- $panel_pids
test "$#" = 1 || { echo "panel_candidate_count=$#" >&2; exit 20; }
panel_pid="$1"
test "$panel_pid" = "__EXPECTED_PID__" || { echo "unexpected_panel_pid=$panel_pid" >&2; exit 21; }
test "$(getprop init.svc.yapanel)" = running || { echo "yapanel_not_running" >&2; exit 22; }
kill -TERM "$panel_pid"
i=0
while [ $i -lt 30 ]; do
    next_pids=""
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        exe=$(readlink "$proc/exe" 2>/dev/null || true)
        case "$exe" in /data/python/bin/python3|/data/python/bin/python3.*) ;; *) continue ;; esac
        [ "$(readlink "$proc/cwd" 2>/dev/null || true)" = "/data/yapanel" ] || continue
        script_ok=no; port_ok=no; expect_port=no
        for arg in $(tr '\000' '\n' < "$proc/cmdline" 2>/dev/null); do
            case "$arg" in
                server.py|/data/yapanel/server.py) script_ok=yes ;;
                --port=8765) port_ok=yes ;;
                --port) expect_port=yes ;;
                8765) [ "$expect_port" = yes ] && port_ok=yes; expect_port=no ;;
                *) expect_port=no ;;
            esac
        done
        [ "$script_ok" = yes ] && [ "$port_ok" = yes ] || continue
        ppid=$(cat "$proc/stat" 2>/dev/null | sed 's/.*) //' | cut -d ' ' -f 2)
        if [ "$ppid" != 1 ]; then
            parent_line=$(tr '\000' ' ' < "/proc/$ppid/cmdline" 2>/dev/null || true)
            case "$parent_line" in *updater_bootstrap.py*) ;; *) continue ;; esac
        fi
        next_pids="$next_pids ${proc##*/}"
    done
    set -- $next_pids
    state=$(getprop init.svc.yapanel)
    if [ "$state" = "running" ] && [ "$#" = 1 ] && [ "$1" != "__EXPECTED_PID__" ]; then
        echo "previous_pid=__EXPECTED_PID__"
        echo "current_pid=$1"
        echo "state=$state"
        exit 0
    fi
    sleep 1
    i=$((i+1))
done
echo "state=$(getprop init.svc.yapanel)" >&2
echo "panel did not restart under init supervision" >&2
exit 23
'@.Replace('__EXPECTED_PID__', $ExpectedPid)
    Invoke-StationRootd $command | Out-Null
}

function Get-LabelValue {
    param([object[]]$Lines, [string]$Label)
    $line = $Lines | Where-Object { $_ -like ($Label + "=*") } | Select-Object -First 1
    if (-not $line) { return $null }
    return ([string]$line).Substring($Label.Length + 1).Trim()
}

function Get-StationSnapshot {
    return Invoke-StationSsh @'
echo VERSION=$(cat /data/yapanel/VERSION)
echo START_SHA256=$(sha256sum /data/yapanel/start.sh | sed 's/ .*//')
echo START_OWNER=$(stat -c "%u:%g:%a" /data/yapanel/start.sh)
panel_pids=""
for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in /data/python/bin/python3|/data/python/bin/python3.*) ;; *) continue ;; esac
    [ "$(readlink "$proc/cwd" 2>/dev/null || true)" = "/data/yapanel" ] || continue
    script_ok=no; port_ok=no; expect_port=no
    for arg in $(tr '\000' '\n' < "$proc/cmdline" 2>/dev/null); do
        case "$arg" in
            server.py|/data/yapanel/server.py) script_ok=yes ;;
            --port=8765) port_ok=yes ;;
            --port) expect_port=yes ;;
            8765) [ "$expect_port" = yes ] && port_ok=yes; expect_port=no ;;
            *) expect_port=no ;;
        esac
    done
    [ "$script_ok" = yes ] && [ "$port_ok" = yes ] || continue
    ppid=$(cat "$proc/stat" 2>/dev/null | sed 's/.*) //' | cut -d ' ' -f 2)
    if [ "$ppid" != 1 ]; then
        parent_line=$(tr '\000' ' ' < "/proc/$ppid/cmdline" 2>/dev/null || true)
        case "$parent_line" in *updater_bootstrap.py*) ;; *) continue ;; esac
    fi
    panel_pids="$panel_pids ${proc##*/}"
done
set -- $panel_pids
panel_pid=""
[ "$#" = 1 ] && panel_pid="$1"
echo PANEL_PID=$panel_pid
echo PANEL_CANDIDATE_COUNT=$#
echo ROOTD_SHA256=$(sha256sum /odm/bin/rootd | sed 's/ .*//')
echo YAPANEL=$(getprop init.svc.yapanel)
echo ROOTD=$(getprop init.svc.rootd)
echo ADBGUARD=$(getprop init.svc.adbguard)
echo ABSSHD=$(getprop init.svc.absshd)
grep -q '"supervisor_owner"' /data/yapanel-state/updater/status.json 2>/dev/null && echo SUPERVISOR_OWNER_PRESENT=yes || echo SUPERVISOR_OWNER_PRESENT=no
echo MOUNTS_SHA256=$(grep -e 'hosts-ota' -e 'hosts-full' -e '/system/etc/hosts' -e '/odm' /proc/self/mountinfo | sha256sum | sed 's/ .*//')
grep -q -- '--device local' /data/yapanel/start.sh && echo START_DEVICE_LOCAL=yes || echo START_DEVICE_LOCAL=no
grep -q -- '--host 0.0.0.0' /data/yapanel/start.sh && echo START_HOST=yes || echo START_HOST=no
grep -q -- '--port 8765' /data/yapanel/start.sh && echo START_PORT=yes || echo START_PORT=no
grep -q 'PANEL_ALLOW_NETWORKS=192.168.31.0/24' /data/yapanel/start.sh && echo START_NETWORKS=yes || echo START_NETWORKS=no
grep -q 'SSL_CERT_FILE=' /data/yapanel/start.sh && echo START_CA=yes || echo START_CA=no
'@
}

function Assert-StationSnapshot {
    param([object[]]$Snapshot, [string]$ExpectedVersion)
    if ((Get-LabelValue $Snapshot "VERSION") -ne $ExpectedVersion) { throw "Неожиданная версия станции" }
    $panelPid = Get-LabelValue $Snapshot "PANEL_PID"
    if ([string]::IsNullOrWhiteSpace($panelPid) -or $panelPid -notmatch '^\d+$' -or
        (Get-LabelValue $Snapshot "PANEL_CANDIDATE_COUNT") -ne "1") {
        throw "Не найден единственный безопасный PID панели"
    }
    foreach ($service in @("ROOTD", "ADBGUARD", "ABSSHD")) {
        if ((Get-LabelValue $Snapshot $service) -ne "running") { throw "Сервис $service не работает" }
    }
    foreach ($check in @("START_DEVICE_LOCAL", "START_HOST", "START_PORT", "START_NETWORKS", "START_CA")) {
        if ((Get-LabelValue $Snapshot $check) -ne "yes") { throw "Не сохранён параметр start.sh: $check" }
    }
}

function Restore-PreparedStart {
    $before = Get-StationSnapshot
    Assert-StationSnapshot $before $CurrentVersion
    Restore-LaunchWrapper
    $after = Get-StationSnapshot
    Assert-StationSnapshot $after $CurrentVersion
    if ((Get-LabelValue $after "START_SHA256") -ne $startDigest) {
        throw "Recovery не восстановил исходный SHA-256 start.sh"
    }
    if ((Get-LabelValue $after "ROOTD_SHA256") -ne (Get-LabelValue $before "ROOTD_SHA256") -or
        (Get-LabelValue $after "MOUNTS_SHA256") -ne (Get-LabelValue $before "MOUNTS_SHA256")) {
        throw "Recovery изменил системные invariants"
    }
    Write-Output "Исходный start.sh восстановлен; системные invariants сохранены."
}

function Restore-LaunchWrapper {
    # Восстанавливаем wrapper независимо от transaction id и состояния managed-файлов.
    # Это позволяет вернуть запуск панели даже при сбое до вывода JSON транзакции.
    $restore = @'
set -eu
source=/data/yapanel-state/updater/start.sh.before-migration
target=/data/yapanel/start.sh
expected=__EXPECTED_START__
test -f "$source"
test "$(sha256sum "$source" | sed 's/ .*//')" = "$expected"
current="$(sha256sum "$target" | sed 's/ .*//')"
if [ "$current" = "$expected" ]; then
    /system/bin/sh -n "$target"
    exit 0
fi
test "$current" = "__EXPECTED_PATCHED__"
source_meta="$(stat -c '%u:%g:%a' "$source")"
temporary="$target.restore-tmp"
rm -f "$temporary"
cp -p "$source" "$temporary"
test "$(sha256sum "$temporary" | sed 's/ .*//')" = "$expected"
test "$(stat -c '%u:%g:%a' "$temporary")" = "$source_meta"
mv -f "$temporary" "$target"
sync
test "$(sha256sum "$target" | sed 's/ .*//')" = "$expected"
test "$(stat -c '%u:%g:%a' "$target")" = "$source_meta"
/system/bin/sh -n "$target"
'@.Replace('__EXPECTED_START__', $startDigest).Replace('__EXPECTED_PATCHED__', $patchedStartDigest)
    Invoke-StationSsh $restore | Out-Null
}

function Clear-StationWrapperIntent {
    Invoke-StationSsh "rm -f /data/yapanel-state/updater/wrapper-intent.json && sync"
}

function Find-StationRecoveryTransaction {
    $paths = Invoke-StationSsh "find /data/yapanel-state/updater/transactions -type f -name transaction.json 2>/dev/null"
    $candidates = @()
    foreach ($path in $paths) {
        if ([string]$path -notmatch '/transactions/([A-Za-z0-9-]+)/transaction\.json$') { continue }
        $id = $Matches[1]
        try {
            $journal = Get-StationTransactionJournal $id
            if (Test-StationRecoveryRequired $journal) {
                $candidates += $journal
            }
        } catch {
            throw "Не удалось классифицировать durable journal $id`: $($_.Exception.Message)"
        }
    }
    if ($candidates.Count -eq 1) { return $candidates[0] }
    if ($candidates.Count -gt 1) { throw "Найдено несколько pending-транзакций ожидаемой версии" }
    return $null
}

function Stop-ConfirmedPanelProcess {
    param([string]$ExpectedPid)
    if ([string]::IsNullOrWhiteSpace($ExpectedPid) -or $ExpectedPid -notmatch '^\d+$') {
        throw "Нельзя остановить неподтверждённый PID панели"
    }
    $command = @'
set -eu
pid=__EXPECTED_PID__
test -r "/proc/$pid/cmdline"
exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
case "$exe" in /data/python/bin/python3|/data/python/bin/python3.*) ;; *) exit 24 ;; esac
[ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "/data/yapanel" ]
args=$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
case "$args" in *server.py*"--port 8765"*|*server.py*"--port=8765"*) ;; *) exit 25 ;; esac
kill -TERM "$pid"
i=0
while [ $i -lt 30 ]; do
    [ ! -e "/proc/$pid" ] && exit 0
    sleep 1
    i=$((i+1))
done
echo "panel_pid_still_running=$pid" >&2
exit 26
'@.Replace('__EXPECTED_PID__', $ExpectedPid)
    Invoke-StationRootd $command | Out-Null
}

function Invoke-StationDirectRollback {
    param([string]$TransactionId, [bool]$HandoffAttempted = $false)
    if ([string]::IsNullOrWhiteSpace($TransactionId)) {
        throw "Нельзя выполнить rollback без идентификатора транзакции"
    }
    if ($HandoffAttempted) {
        throw "Direct rollback запрещён после handoff; rollback принадлежит supervisor"
    }
    $running = Get-StationSnapshot
    $runningPid = Get-LabelValue $running "PANEL_PID"
    $candidateCount = Get-LabelValue $running "PANEL_CANDIDATE_COUNT"
    $owner = Get-StationRollbackOwner `
        -HandoffAttempted $HandoffAttempted `
        -ServiceState (Get-LabelValue $running "YAPANEL") `
        -PanelPid $runningPid `
        -CandidateCount $candidateCount `
        -SupervisorOwner (Get-LabelValue $running "SUPERVISOR_OWNER_PRESENT")
    if ($owner -ne "direct") {
        throw "Direct rollback запрещён: владелец rollback=$owner"
    }
    Invoke-StationSsh ("/data/python/bin/python3 /data/yapanel-state/updater/update_agent.py rollback " + $TransactionId + " --root /data/yapanel --state /data/yapanel-state") | Out-Null
}

function Wait-StationSupervisorRollback {
    for ($attempt = 0; $attempt -lt $SupervisorWaitAttempts; $attempt++) {
        $status = (Invoke-StationSsh "cat /data/yapanel-state/updater/status.json 2>/dev/null || true") -join [Environment]::NewLine
        if ($status -match '"state"\s*:\s*"rolled_back"') { return }
        if ($status -match '"state"\s*:\s*"rollback_failed"') {
            throw "Внешний bootstrap сообщил rollback_failed"
        }
        if ($SupervisorWaitDelaySeconds -gt 0) { Start-Sleep -Seconds $SupervisorWaitDelaySeconds }
    }
    throw "Внешний bootstrap не подтвердил завершение rollback"
}

if ($RecoverFailedPreparation) {
    Restore-PreparedStart
    return
}

New-Item -ItemType Directory -Force -Path $WorkDirectory | Out-Null
$work = (Resolve-Path $WorkDirectory).Path
$baseline = Join-Path $work "updater-baseline.json"
$asset = if ($AssetPath) { (Resolve-Path $AssetPath).Path } else { Join-Path $work "yapanel-web-0.3.11-windows-arm64.zip" }
$initial = Join-Path $work "yapanel-station-$ExpectedVersion.zip"
$remoteStart = Join-Path $work "start.sh.before"
$patchedStart = Join-Path $work "start.sh.bootstrap"
$backupDir = Join-Path $work "backups"
$transaction = $null
$startPatched = $false
$bootstrapInstalled = $false
$stationChangesStarted = $false
$launchWrapperMayHaveChanged = $false
$handoffStarted = $false
$handoffAttempted = $false
$wrapperPrepared = $false
$activationEvidence = $false

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    adb connect ($AdbSerial -replace ":.*$", "") | Out-Null
    & $AdbExecutable -s $AdbSerial shell id | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ADB недоступен" }
    Test-StationRootdControl
    $before = Get-StationSnapshot
    Assert-StationSnapshot $before $CurrentVersion
    if ((Get-LabelValue $before "START_SHA256") -ne $startDigest) { throw "SHA-256 start.sh не совпадает с проверенным состоянием" }

    & git.exe -C $root rev-parse --verify ("v" + $CurrentVersion) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "В локальном репозитории отсутствует tag v$CurrentVersion" }
    & $python (Join-Path $root "tools/make_updater_baseline.py") ("v" + $CurrentVersion) $baseline (Join-Path $root "tools/station-max-v0.3.5-overrides.json") (Join-Path $root "tools/station-max-generated-lineage.json")
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать baseline" }
    $baselineJson = Get-Content -Raw -LiteralPath $baseline | ConvertFrom-Json
    $approvedLocalHashes = @{
        "server.py" = "dd9411f9d60c30371fee282ad3d74f0d67a2e37266ded2cd48ad9293bd0d9e74"
        "max_profile.py" = "ac5e1ca84e3c9f10c59819164d7a8a6ee8c38a2f0e9df860f3cccd55fc9823e4"
        "static/app.js" = "b7a797f1a53a9439689381d2c46c314abc139267e8760517b8a78410327c91de"
    }
    foreach ($name in $approvedLocalHashes.Keys) {
        if (-not ($baselineJson.PSObject.Properties.Name -contains $name)) { throw "В baseline отсутствует $name" }
        $entry = $baselineJson.PSObject.Properties[$name].Value
        $approved = @($entry.approved)
        if ($approved.Count -ne 1 -or [string]$approved[0].ToLowerInvariant() -ne $approvedLocalHashes[$name]) {
            throw "В baseline указан неподтверждённый approved hash для $name"
        }
    }
    foreach ($name in @(
        "device/agent/build/VERSION",
        "device/agent/build/yapanel-agent",
        "device/assistant/build/VERSION",
        "device/assistant/build/assistantd",
        "device/viz/build/VERSION",
        "device/viz/build/vizd"
    )) {
        $entry = $baselineJson.PSObject.Properties[$name].Value
        if (-not $entry -or -not $entry.lineage -or -not $entry.approved -or @($entry.approved).Count -ne 1) {
            throw "В baseline отсутствует строгая lineage-запись $name"
        }
    }

    if (-not $AssetPath) {
        $release = Invoke-RestMethod -UseBasicParsing -Headers @{ "User-Agent" = "yapanel-station-installer"; Accept = "application/vnd.github+json" } -Uri ("https://api.github.com/repos/" + $repo + "/releases/tags/v" + $ExpectedVersion)
        if ($release.tag_name -ne ("v" + $ExpectedVersion)) { throw "GitHub вернул неожиданный release tag" }
        $releaseAsset = @($release.assets | Where-Object { $_.name -like ("*" + $assetName) }) | Select-Object -First 1
        if (-not $releaseAsset -or $releaseAsset.digest -ne ("sha256:" + $assetDigest)) { throw "Digest release asset не совпадает с утверждённым" }
        Invoke-WebRequest -UseBasicParsing -Headers @{ "User-Agent" = "yapanel-station-installer" } -Uri $releaseAsset.browser_download_url -OutFile $asset
    }
    if ((Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant() -ne $assetDigest) { throw "SHA-256 официального asset не совпадает" }

    & $python (Join-Path $root "tools/prepare_station_release.py") --asset $asset --output $initial --source-root $root --sha256 $assetDigest --version $ExpectedVersion
    if ($LASTEXITCODE -ne 0) { throw "Не удалось собрать initial-release" }
    $initialDigest = (Get-FileHash -LiteralPath $initial -Algorithm SHA256).Hash.ToLowerInvariant()
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "tools/backup_station_max.ps1") -OutputDirectory $backupDir -SshAlias $SshAlias -SshExecutable $SshExecutable -ExpectedVersion $CurrentVersion
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать backup станции" }

    Receive-StationFile "/data/yapanel/start.sh" $remoteStart
    if ((Get-FileHash -LiteralPath $remoteStart -Algorithm SHA256).Hash.ToLowerInvariant() -ne $startDigest) { throw "Локальная копия start.sh не совпала с read-only snapshot" }
    & $python (Join-Path $root "tools/patch_station_start.py") --input $remoteStart --output $patchedStart --expected-sha256 $startDigest
    if ($LASTEXITCODE -ne 0) { throw "Не удалось подготовить patch start.sh" }

    Write-Output ("Preflight завершён. Work directory: " + $work)
    Write-Output ("Initial-release SHA-256: " + $initialDigest)
    if (-not $Apply) {
        Write-Output "Dry-run: станция не изменялась. Для применения требуется отдельный запуск с -Apply."
        return
    }

    $stationChangesStarted = $true
    Invoke-StationSsh "mkdir -p /data/yapanel-state/updater"
    foreach ($file in @("update_agent.py", "station_overlay.py", "updater_bootstrap.py", "panel_update_integration.py")) {
        Send-StationFile (Join-Path $root $file) ("/data/yapanel-state/updater/" + $file)
    }
    Send-StationFile $baseline "/data/yapanel-state/updater/baseline.json"
    Send-StationFile $initial "/data/yapanel-state/updater/yapanel-station-0.3.11.zip"
    $bootstrapInstalled = $true

    $wrapperMetadata = (@{
        original_sha256 = $startDigest
        patched_sha256 = $patchedStartDigest
    } | ConvertTo-Json -Compress)
    Invoke-StationSsh ("cp -p /data/yapanel/start.sh /data/yapanel-state/updater/start.sh.before-migration && printf '%s\n' '" + $wrapperMetadata + "' > /data/yapanel-state/updater/start.sh.before-migration.json")
    Send-StationFile $patchedStart "/data/yapanel-state/updater/start.sh.new"
    $ownerMode = Get-LabelValue $before "START_OWNER"
    $owner = ($ownerMode -split ":")[0..1] -join ":"
    $mode = ($ownerMode -split ":")[2]
    Invoke-StationSsh ("chown " + $owner + " /data/yapanel-state/updater/start.sh.new && chmod " + $mode + " /data/yapanel-state/updater/start.sh.new && /system/bin/sh -n /data/yapanel-state/updater/start.sh.new")

    # Wrapper устанавливается до apply. Startup intent позволяет bootstrap
    # восстановить исходный запуск, если питание пропадёт до активации.
    Invoke-StationSsh ("/data/python/bin/python3 /data/yapanel-state/updater/updater_bootstrap.py --prepare-wrapper --root /data/yapanel --state /data/yapanel-state --python /data/python/bin/python3 --original-sha256 " + $startDigest + " --patched-sha256 " + $patchedStartDigest + " --target-version " + $ExpectedVersion)
    $wrapperPrepared = $true
    $installWrapper = @'
set -eu
/system/bin/sh -n /data/yapanel-state/updater/start.sh.new
mv /data/yapanel-state/updater/start.sh.new /data/yapanel/start.sh
/system/bin/sh -n /data/yapanel/start.sh
test "$(sha256sum /data/yapanel/start.sh | sed 's/ .*//')" = "__PATCHED_SHA256__"
'@.Replace('__PATCHED_SHA256__', $patchedStartDigest)
    $launchWrapperMayHaveChanged = $true
    Invoke-StationSsh $installWrapper
    $startPatched = $true

    $applyOutput = Invoke-StationSsh ("/data/python/bin/python3 /data/yapanel-state/updater/update_agent.py apply --root /data/yapanel --state /data/yapanel-state --archive /data/yapanel-state/updater/yapanel-station-0.3.11.zip --version " + $ExpectedVersion + " --sha256 " + $initialDigest)
    try {
        $applyResult = (($applyOutput -join [Environment]::NewLine) | ConvertFrom-Json)
    } catch {
        throw "update_agent apply вернул некорректный JSON"
    }
    $transaction = [string]$applyResult.transaction
    $journal = Assert-StationApplyContract $applyResult $ExpectedVersion
    $activationEvidence = $true

    $handoffAttempted = $true
    Invoke-StationPanelHandoff (Get-LabelValue $before "PANEL_PID")
    $handoffStarted = $true

    $health = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-RestMethod -UseBasicParsing ($HealthUri.TrimEnd('/') + "/api/panel/health")
            if ($health.ready -eq $true -and [string]$health.version -eq $ExpectedVersion -and [int]$health.critical_errors -eq 0) { break }
        } catch { }
        if ($HealthDelaySeconds -gt 0) { Start-Sleep -Seconds $HealthDelaySeconds }
    }
    if (-not $health -or $health.ready -ne $true -or [string]$health.version -ne $ExpectedVersion -or [int]$health.critical_errors -ne 0) { throw "Панель не прошла strict health check" }

    $after = Get-StationSnapshot
    Assert-StationSnapshot $after $ExpectedVersion
    if ((Get-LabelValue $after "ROOTD_SHA256") -ne (Get-LabelValue $before "ROOTD_SHA256")) { throw "Изменился rootd" }
    if ((Get-LabelValue $after "MOUNTS_SHA256") -ne (Get-LabelValue $before "MOUNTS_SHA256")) { throw "Изменились hosts или /odm mounts" }
    Write-Output "Установка завершена, health и системные invariants подтверждены."
}
catch {
    $originalError = $_
    if ($Apply -and $stationChangesStarted) {
        $recoveryErrors = @()
        try {
            if ($handoffAttempted) {
                try {
                    Wait-StationSupervisorRollback
                } catch {
                    $recoveryErrors += "Bootstrap не подтвердил rollback: $($_.Exception.Message)"
                    $status = (Invoke-StationSsh "cat /data/yapanel-state/updater/status.json 2>/dev/null || true") -join [Environment]::NewLine
                    if ($status -match '"state"\s*:\s*"rollback_failed"') {
                        $recoveryErrors += "Bootstrap зафиксировал rollback_failed; повторная автоматическая попытка запрещена"
                    } else {
                        $recoveryErrors += "Direct rollback после handoff запрещён; требуется восстановление через supervisor или ручная SSH-процедура"
                    }
                }
            } else {
                if (-not $transaction) {
                    try {
                        $recovery = Find-StationRecoveryTransaction
                        if ($recovery) {
                            $transaction = [string]$recovery.id
                            $activationEvidence = $true
                        }
                    } catch {
                        $recoveryErrors += "Не удалось определить durable pending-транзакцию: $($_.Exception.Message)"
                    }
                }
                if ($activationEvidence -and $transaction) {
                    try {
                        $handoffAttempted = $true
                        Invoke-StationPanelHandoff (Get-LabelValue $before "PANEL_PID")
                        $handoffStarted = $true
                        Wait-StationSupervisorRollback
                    } catch {
                        $recoveryErrors += "После активации recovery передан supervisor неуспешно: $($_.Exception.Message)"
                    }
                } elseif (-not $activationEvidence) {
                    # До durable recovery-required managed-файлы не менялись.
                    # Wrapper можно безопасно вернуть при работающей старой панели:
                    # уже запущенный процесс не читает start.sh повторно.
                    try {
                        if ($launchWrapperMayHaveChanged) {
                            Restore-LaunchWrapper
                        }
                        if ($wrapperPrepared) {
                            Clear-StationWrapperIntent
                        }
                    } catch {
                        $recoveryErrors += "Не удалось восстановить pre-activation wrapper: $($_.Exception.Message)"
                    }
                }
            }
        } catch {
            $recoveryErrors += "Rollback завершился ошибкой: $($_.Exception.Message)"
        }
        try {
            if ($launchWrapperMayHaveChanged -and -not $handoffAttempted -and $activationEvidence) {
                $wrapperSnapshot = Get-StationSnapshot
                if ((Get-LabelValue $wrapperSnapshot "YAPANEL") -eq "running" -or
                    (Get-LabelValue $wrapperSnapshot "PANEL_CANDIDATE_COUNT") -ne "0") {
                    throw "Нельзя восстановить start.sh при активной службе или процессе панели"
                }
                Restore-LaunchWrapper
            }
        } catch {
            $recoveryErrors += "Не удалось восстановить start.sh: $($_.Exception.Message)"
        }
        foreach ($recoveryError in $recoveryErrors) {
            Write-Error $recoveryError -ErrorAction Continue
        }
    }
    Write-Error $originalError.Exception.Message -ErrorAction Continue
    throw $originalError
}
