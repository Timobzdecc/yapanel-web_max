[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Restart,
    [switch]$Rollback,
    [switch]$DryRun,
    [string]$TransactionId,
    [string]$Source,
    [string]$SshAlias = "station-max",
    [string]$AdbSerial = "192.168.31.196:5555",
    [string]$SshExecutable = "ssh.exe",
    [string]$AdbExecutable = "adb.exe",
    [string]$HealthUri = "http://192.168.31.196:8765",
    [string]$ExpectedVersion = "0.3.11",
    [string]$ExpectedCurrentSha256 = "49e7ea5e6d132abb9b91134f85d7ebd5d49159dde38abf02737cbe486018b098",
    [string]$ExpectedPatchedSha256 = "1a411df1621e172e3379e1760f6ec86010fd10dc15959e6a1f7789996e8f9d64",
    [string]$WorkDirectory = (Join-Path (Get-Location) "station-patch-0.3.11")
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Get-Command python.exe -ErrorAction Stop).Source
$rootdControl = Join-Path $PSScriptRoot "rootd_control.py"
$componentNames = @("update_agent.py", "station_overlay.py", "updater_bootstrap.py", "panel_update_integration.py")
if (-not $Source) { $Source = Join-Path $root "max_profile.py" }

if ($DryRun -and ($Apply -or $Restart -or $Rollback)) { throw "-DryRun нельзя совмещать с изменяющей операцией" }
if ($Rollback -and ($Apply -or $Restart)) { throw "-Rollback нельзя совмещать с -Apply или -Restart" }
if ($Restart -and -not $TransactionId) { throw "Для -Restart нужен -TransactionId" }
if ($Rollback -and -not $TransactionId) { throw "Для -Rollback нужен -TransactionId" }
if ($TransactionId -and $TransactionId -notmatch '^[A-Za-z0-9-]+$') { throw "Недопустимый transaction id" }
if ($Apply -and -not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Patch-файл не найден: $Source" }
if ($SshAlias -notmatch '^[A-Za-z0-9_.-]+$') { throw "Недопустимый SSH alias" }

function Invoke-StationSsh {
    param([Parameter(Mandatory = $true)][string]$Command)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Command))
    $transport = "printf %s $encoded | /system/bin/base64 -d | /system/bin/sh"
    $output = & $SshExecutable -o BatchMode=yes $SshAlias $transport 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("SSH-команда завершилась с кодом " + $LASTEXITCODE + ": " + ($output -join [Environment]::NewLine)) }
    return @($output)
}

function Invoke-StationAdb {
    param([Parameter(Mandatory = $true)][string]$Command)
    $output = & $AdbExecutable -s $AdbSerial shell $Command 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("ADB-команда завершилась с кодом " + $LASTEXITCODE + ": " + ($output -join [Environment]::NewLine)) }
    return @($output)
}

function Send-StationFileBase64 {
    param([string]$LocalPath, [string]$RemotePath)
    $sshPath = (Get-Command $SshExecutable -ErrorAction Stop).Source
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $sshPath
    $startInfo.Arguments = '-o BatchMode=yes ' + $SshAlias + ' "base64 -d > ' + $RemotePath + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardOutput = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Не удалось запустить SSH для передачи файла" }
    try {
        $bytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($LocalPath))
        $payload = [Text.Encoding]::ASCII.GetBytes($bytes)
        $process.StandardInput.BaseStream.Write($payload, 0, $payload.Length)
        $process.StandardInput.BaseStream.Close()
        $errorText = $process.StandardError.ReadToEnd()
        $outputText = $process.StandardOutput.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw ("SSH-передача завершилась с кодом " + $process.ExitCode + ": " + $errorText + $outputText) }
    }
    finally { $process.Dispose() }
}

function Get-LabelValue {
    param([object[]]$Lines, [string]$Label)
    $line = $Lines | Where-Object { [string]$_ -like ($Label + "=*") } | Select-Object -First 1
    if (-not $line) { return $null }
    return ([string]$line).Substring($Label.Length + 1).Trim()
}

function Get-StationFacts {
    $script = @'
set -eu
printf 'VERSION='; cat /data/yapanel/VERSION
printf 'START_SHA256='; sha256sum /data/yapanel/start.sh | cut -d' ' -f1
printf 'PATCH_SHA256='; sha256sum /data/yapanel/max_profile.py | cut -d' ' -f1
printf 'START_BOOTSTRAP='; grep -q 'updater_bootstrap.py' /data/yapanel/start.sh && echo yes || echo no
printf 'YAPANEL='; getprop init.svc.yapanel
printf 'ROOTD='; getprop init.svc.rootd
printf 'ADBGUARD='; getprop init.svc.adbguard
printf 'ABSSHD='; getprop init.svc.absshd
printf 'ROOTD_SHA256='; sha256sum /odm/bin/rootd | cut -d' ' -f1
printf 'MOUNTS_SHA256='; grep -e 'hosts-ota' -e 'hosts-full' -e '/system/etc/hosts' -e '/odm' /proc/self/mountinfo | sha256sum | cut -d' ' -f1
printf 'START_PARAMS='; grep -q -- '--device local' /data/yapanel/start.sh && grep -q -- '--host 0.0.0.0' /data/yapanel/start.sh && grep -q -- '--port 8765' /data/yapanel/start.sh && grep -q 'PANEL_ALLOW_NETWORKS=192.168.31.0/24' /data/yapanel/start.sh && echo yes || echo no
printf 'STATE='; sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /data/yapanel-state/updater/status.json | head -1
printf 'TRANSACTION='; sed -n 's/.*"transaction"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /data/yapanel-state/updater/status.json | head -1
printf 'PENDING='
for journal in /data/yapanel-state/updater/transactions/*/transaction.json; do
    [ -f "$journal" ] || continue
    state=$(sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$journal" | head -1)
    case "$state" in backup|activating|activated_pending_health|rollback_started|rollback_pending_health) printf '%s ' "$state";; esac
done
printf '\n'
panel_pids=""
for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in /data/python/bin/python3|/data/python/bin/python3.*) ;; *) continue;; esac
    [ "$(readlink "$proc/cwd" 2>/dev/null || true)" = "/data/yapanel" ] || continue
    script_ok=no; port_ok=no; expect_port=no
    for arg in $(tr '\000' '\n' < "$proc/cmdline" 2>/dev/null); do
        case "$arg" in server.py|/data/yapanel/server.py) script_ok=yes;; --port=8765) port_ok=yes;; --port) expect_port=yes;; 8765) [ "$expect_port" = yes ] && port_ok=yes; expect_port=no;; *) expect_port=no;; esac
    done
    [ "$script_ok" = yes ] && [ "$port_ok" = yes ] || continue
    ppid=$(cat "$proc/stat" 2>/dev/null | sed 's/.*) //' | cut -d' ' -f2)
    if [ "$ppid" != 1 ]; then
        parent_line=$(tr '\000' ' ' < "/proc/$ppid/cmdline" 2>/dev/null || true)
        case "$parent_line" in *updater_bootstrap.py*) ;; *) continue;; esac
    fi
    panel_pids="$panel_pids ${proc##*/}"
done
set -- $panel_pids
printf 'PANEL_COUNT=%s\n' "$#"
printf 'PANEL_PID=%s\n' "${1-}"
'@
    return Invoke-StationSsh $script
}

function Assert-HealthyPreflight {
    param([object[]]$Facts)
    foreach ($label in @("VERSION", "PATCH_SHA256", "YAPANEL", "ROOTD", "ADBGUARD", "ABSSHD", "START_BOOTSTRAP", "START_PARAMS", "STATE", "PENDING", "PANEL_COUNT")) {
        if ($null -eq (Get-LabelValue $Facts $label)) { throw "Read-only preflight не вернул $label" }
    }
    if ((Get-LabelValue $Facts "VERSION") -ne $ExpectedVersion) { throw "Ожидалась версия $ExpectedVersion" }
    if ((Get-LabelValue $Facts "PATCH_SHA256").ToLowerInvariant() -ne $ExpectedCurrentSha256.ToLowerInvariant()) { throw "Текущий max_profile.py не совпадает с проверенным baseline" }
    if ((Get-LabelValue $Facts "YAPANEL") -ne "running") { throw "Служба yapanel не running" }
    foreach ($service in @("ROOTD", "ADBGUARD", "ABSSHD")) { if ((Get-LabelValue $Facts $service) -ne "running") { throw "Сервис $service не running" } }
    if ((Get-LabelValue $Facts "START_BOOTSTRAP") -ne "yes" -or (Get-LabelValue $Facts "START_PARAMS") -ne "yes") { throw "start.sh не соответствует проверенному bootstrap-профилю" }
    if ((Get-LabelValue $Facts "STATE") -ne "healthy") { throw "Updater journal не healthy" }
    if (-not [string]::IsNullOrWhiteSpace((Get-LabelValue $Facts "PENDING"))) { throw "Найдена незавершённая transaction: $(Get-LabelValue $Facts 'PENDING')" }
    if ((Get-LabelValue $Facts "PANEL_COUNT") -ne "1") { throw "Ожидался единственный контролируемый процесс панели" }
}

function Assert-HttpHealth {
    $health = Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 ($HealthUri.TrimEnd('/') + "/api/panel/health")
    if ($health.ready -ne $true -or [string]$health.version -ne $ExpectedVersion -or [int]$health.critical_errors -ne 0) { throw "Strict HTTP health не подтверждён" }
}

function Invoke-Rootd {
    param([string]$Command, [int]$Timeout = 45)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Command))
    $output = & $python $rootdControl --serial $AdbSerial --adb $AdbExecutable --timeout $Timeout --command-base64 $encoded 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("rootd-команда завершилась с ошибкой: " + ($output -join [Environment]::NewLine)) }
    $result = (($output -join [Environment]::NewLine) | ConvertFrom-Json)
    if (-not $result.ok) { throw ("rootd не подтвердил команду: " + [string]$result.stdout) }
    return @(([string]$result.stdout) -split "`r?`n" | Where-Object { $_ -ne "" })
}

function Invoke-Backup {
    New-Item -ItemType Directory -Force -Path (Join-Path $WorkDirectory "backups") | Out-Null
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "backup_station_max.ps1") -OutputDirectory (Join-Path $WorkDirectory "backups") -SshAlias $SshAlias -SshExecutable $SshExecutable -ExpectedVersion $ExpectedVersion
    if ($LASTEXITCODE -ne 0) { throw "Создание backup станции завершилось с ошибкой" }
    $archive = Get-ChildItem -LiteralPath (Join-Path $WorkDirectory "backups") -Filter "*.tar.gz" -File | Sort-Object LastWriteTimeUtc | Select-Object -Last 1
    if (-not $archive) { throw "Backup-архив не найден" }
    & tar.exe -tzf $archive.FullName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Созданный backup невозможно прочитать" }
    Write-Output ("Backup: " + $archive.FullName)
    Write-Output ("Backup SHA-256: " + (Get-FileHash -LiteralPath $archive.FullName -Algorithm SHA256).Hash.ToLowerInvariant())
    return $archive.FullName
}

function Install-UpdaterComponents {
    Invoke-StationSsh "mkdir -p /data/yapanel-state/updater"
    foreach ($name in $componentNames) {
        $local = Join-Path $root $name
        $remote = "/data/yapanel-state/updater/$name"
        $temporary = "$remote.new"
        Send-StationFileBase64 $local $temporary
        Invoke-StationSsh ("set -eu; if [ -f '$remote' ]; then chown `$(stat -c '%u:%g' '$remote') '$temporary'; chmod `$(stat -c '%a' '$remote') '$temporary'; fi; mv -f '$temporary' '$remote'; sync") | Out-Null
    }
}

function Invoke-ApplyPatch {
    param([string]$PatchPath, [string]$CurrentSha)
    $remotePatch = "/data/yapanel-state/updater/max_profile.py.patch"
    $remotePatchTemporary = "$remotePatch.new"
    Send-StationFileBase64 $PatchPath $remotePatchTemporary
    $installPatch = @'
set -eu
temporary=/data/yapanel-state/updater/max_profile.py.patch.new
target=/data/yapanel-state/updater/max_profile.py.patch
expected=__EXPECTED_PATCH_SHA256__
trap 'rm -f "$temporary"' EXIT
test "$(sha256sum "$temporary" | cut -d' ' -f1)" = "$expected"
chown shell:shell "$temporary"
chmod 600 "$temporary"
mv -f "$temporary" "$target"
sync
test "$(stat -c '%U:%G:%a' "$target")" = "shell:shell:600"
test "$(sha256sum "$target" | cut -d' ' -f1)" = "$expected"
'@.Replace('__EXPECTED_PATCH_SHA256__', $ExpectedPatchedSha256)
    Invoke-StationSsh $installPatch | Out-Null
    $command = "/data/python/bin/python3 /data/yapanel-state/updater/update_agent.py apply-patch --root /data/yapanel --state /data/yapanel-state --file $remotePatch --version $ExpectedVersion --sha256 $ExpectedPatchedSha256 --expected-current-sha256 $CurrentSha"
    $lines = Invoke-StationAdb $command
    $jsonLine = $lines | Where-Object { [string]$_ -match '^\s*\{' } | Select-Object -Last 1
    if (-not $jsonLine) { throw "apply-patch не вернул JSON-контракт" }
    $result = ($jsonLine | ConvertFrom-Json)
    if ([string]$result.state -ne "activated_pending_health") { throw "apply-patch вернул неожиданное состояние: $($result.state)" }
    if ([string]::IsNullOrWhiteSpace([string]$result.transaction)) { throw "apply-patch не вернул transaction" }
    $journal = Invoke-StationSsh ("cat /data/yapanel-state/updater/transactions/$($result.transaction)/transaction.json")
    $journalJson = (($journal -join [Environment]::NewLine) | ConvertFrom-Json)
    if ([string]$journalJson.id -ne [string]$result.transaction -or [string]$journalJson.state -ne "activated_pending_health") { throw "Durable journal не соответствует CLI-контракту" }
    return [string]$result.transaction
}

function Invoke-PanelHandoff {
    param([string]$ExpectedPid)
    if ($ExpectedPid -notmatch '^\d+$') { throw "Не найден подтверждённый PID панели" }
    $command = @"
set -eu
pid=$ExpectedPid
test -r /proc/`$pid/cmdline
exe=`$(readlink /proc/`$pid/exe 2>/dev/null || true)
test "`$exe" = /data/python/bin/python3
args=`$(tr '\000' ' ' < /proc/`$pid/cmdline)
case "`$args" in *server.py*--port*8765*|*server.py*8765*) ;; *) echo unexpected_panel_argv >&2; exit 21;; esac
case "`$args" in *updater_bootstrap.py*) ;; *) ppid=`$(cat /proc/`$pid/stat | sed 's/.*) //' | cut -d' ' -f2); parent=`$(tr '\000' ' ' < /proc/`$ppid/cmdline); case "`$parent" in *updater_bootstrap.py*) ;; *) echo unowned_panel_process >&2; exit 22;; esac;; esac
test "`$(getprop init.svc.yapanel)" = running
kill -TERM "`$pid"
echo handoff_pid=`$pid
"@
    Invoke-Rootd $command | Out-Null
}

function Wait-Healthy {
    param([string]$OldPid)
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $health = Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 ($HealthUri.TrimEnd('/') + "/api/panel/health")
            $facts = Get-StationFacts
            $pid = Get-LabelValue $facts "PANEL_PID"
            if ($health.ready -eq $true -and [string]$health.version -eq $ExpectedVersion -and [int]$health.critical_errors -eq 0 -and (Get-LabelValue $facts "STATE") -eq "healthy" -and $pid -match '^\d+$' -and $pid -ne $OldPid) { return $facts }
        } catch { }
        Start-Sleep -Seconds 1
    }
    throw "Новая версия не прошла strict health или journal healthy за отведённое время"
}

function Invoke-SafeRollback {
    param([string]$Id)
    $facts = Get-StationFacts
    if ((Get-LabelValue $facts "YAPANEL") -eq "running" -or (Get-LabelValue $facts "PANEL_COUNT") -ne "0") { throw "Прямой rollback запрещён: служба или процесс панели активны; rollback принадлежит supervisor" }
    Invoke-StationAdb ("/data/python/bin/python3 /data/yapanel-state/updater/update_agent.py rollback $Id --root /data/yapanel --state /data/yapanel-state") | Out-Null
    Write-Output "Rollback файлов завершён; запуск панели не выполнялся. Требуется отдельная проверка legacy health."
}

& $AdbExecutable connect ($AdbSerial -replace ":.*$", "") | Out-Null
& $AdbExecutable -s $AdbSerial shell id | Out-Null
if ($LASTEXITCODE -ne 0) { throw "ADB недоступен" }

if ($DryRun -or (-not $Apply -and -not $Restart -and -not $Rollback)) {
    $facts = Get-StationFacts
    Assert-HealthyPreflight $facts
    Assert-HttpHealth
    $sourceHash = if (Test-Path -LiteralPath $Source -PathType Leaf) { (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant() } else { "not-found" }
    Write-Output "Dry-run: станция не изменялась."
    Write-Output ("Исходный SHA-256 max_profile.py: " + $sourceHash)
    Write-Output ("Текущий SHA-256 max_profile.py: " + (Get-LabelValue $facts "PATCH_SHA256"))
    Write-Output ("Текущий updater transaction: " + (Get-LabelValue $facts "TRANSACTION"))
    return
}

if ($Apply) {
    $sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sourceHash -ne $ExpectedPatchedSha256.ToLowerInvariant()) { throw "Локальный patch имеет неподтверждённый SHA-256: $sourceHash" }
    & $python -c "from pathlib import Path; compile(Path(r'$Source').read_text(encoding='utf-8'), r'$Source', 'exec')"
    if ($LASTEXITCODE -ne 0) { throw "Локальный patch не прошёл syntax check" }
    $before = Get-StationFacts
    Assert-HealthyPreflight $before
    Assert-HttpHealth
    Invoke-Backup | Out-Null
    Install-UpdaterComponents
    $TransactionId = [string](Invoke-ApplyPatch $Source (Get-LabelValue $before "PATCH_SHA256"))
    Write-Output ("Transaction: " + $TransactionId)
    Write-Output "Managed-файл активирован через durable transaction; рабочий процесс ещё не переключён."
    if (-not $Restart) {
        Write-Output ("Для контролируемого handoff выполните: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\apply_station_file_patch.ps1 -Restart -TransactionId " + $TransactionId)
        return
    }
}

if ($Restart) {
    $before = Get-StationFacts
    if ((Get-LabelValue $before "PENDING") -notmatch '(^|\s)activated_pending_health(\s|$)') { throw "Durable journal не находится в activated_pending_health" }
    $journal = Invoke-StationSsh ("cat /data/yapanel-state/updater/transactions/$TransactionId/transaction.json")
    $journalJson = (($journal -join [Environment]::NewLine) | ConvertFrom-Json)
    if ([string]$journalJson.id -ne $TransactionId -or [string]$journalJson.state -ne "activated_pending_health") { throw "Transaction ID не совпадает с durable journal" }
    $oldPid = Get-LabelValue $before "PANEL_PID"
    Invoke-PanelHandoff $oldPid
    $after = Wait-Healthy $oldPid
    if ((Get-LabelValue $after "ROOTD_SHA256") -ne (Get-LabelValue $before "ROOTD_SHA256") -or (Get-LabelValue $after "MOUNTS_SHA256") -ne (Get-LabelValue $before "MOUNTS_SHA256")) { throw "Изменились rootd или hosts/odm mount invariants" }
    Write-Output "Maintenance patch завершён: новая версия healthy, PID изменился, системные invariants сохранены."
}

if ($Rollback) { Invoke-SafeRollback $TransactionId }
