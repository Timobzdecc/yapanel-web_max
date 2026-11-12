[CmdletBinding(DefaultParameterSetName = "Apply")]
param(
    [Parameter(ParameterSetName = "Apply")]
    [switch]$Apply,
    [Parameter(ParameterSetName = "DryRun")]
    [switch]$DryRun,
    [Parameter(ParameterSetName = "Rollback", Mandatory = $true)]
    [switch]$Rollback,
    [string]$Source,
    [string]$BackupArchive,
    [string]$SshAlias = "station-max",
    [string]$AdbSerial = "192.168.31.196:5555",
    [string]$SshExecutable = "ssh.exe",
    [string]$AdbExecutable = "adb.exe",
    [string]$HealthUri = "http://192.168.31.196:8765",
    [string]$ExpectedVersion = "0.3.11",
    [string]$ExpectedCurrentSha256 = "49e7ea5e6d132abb9b91134f85d7ebd5d49159dde38abf02737cbe486018b098",
    [string]$ExpectedActiveSha256,
    [string]$RemotePath = "/data/yapanel/max_profile.py",
    [string]$WorkDirectory = (Join-Path (Get-Location) "station-patch-0.3.11")
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Get-Command python.exe -ErrorAction Stop).Source
$managedRemotePath = "/data/yapanel/max_profile.py"

if (($Apply -and $Rollback) -or ($DryRun -and ($Apply -or $Rollback))) {
    throw "Нельзя одновременно указать -Apply и -Rollback"
}
if (-not $Apply -and -not $Rollback) {
    $DryRun = $true
}
if ($SshAlias -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "Недопустимый SSH alias"
}
if ($RemotePath -ne $managedRemotePath) {
    throw "Разрешён только точечный патч $managedRemotePath"
}
if ($Rollback -and -not $BackupArchive) {
    throw "Для -Rollback нужен -BackupArchive"
}
if ($Rollback -and -not $ExpectedActiveSha256) {
    throw "Для -Rollback нужен SHA-256 текущего активного файла через -ExpectedActiveSha256"
}
if (($Apply -or $DryRun) -and -not $Source) {
    $Source = Join-Path $root "max_profile.py"
}
if ($Source -and -not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "Файл источника не найден: $Source"
}

function Invoke-StationSsh {
    param([string]$RemoteCommand)

    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($RemoteCommand))
    $transport = "printf %s $encoded | /system/bin/base64 -d | /system/bin/sh"
    $output = & $SshExecutable -o BatchMode=yes $SshAlias $transport 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("SSH-команда завершилась с кодом " + $LASTEXITCODE + ": " + $RemoteCommand +
            [Environment]::NewLine + ($output -join [Environment]::NewLine))
    }
    return @($output)
}

function Send-StationFileBase64 {
    param([string]$LocalPath, [string]$RemoteDestination)

    $sshPath = (Get-Command $SshExecutable -ErrorAction Stop).Source
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $sshPath
    $startInfo.Arguments = '-o BatchMode=yes ' + $SshAlias +
        ' "base64 -d > ' + $RemoteDestination + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardOutput = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Не удалось запустить SSH для передачи файла"
    }
    try {
        $bytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($LocalPath))
        $payload = [Text.Encoding]::ASCII.GetBytes($bytes)
        $process.StandardInput.BaseStream.Write($payload, 0, $payload.Length)
        $process.StandardInput.BaseStream.Close()
        $errorText = $process.StandardError.ReadToEnd()
        $outputText = $process.StandardOutput.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw ("SSH-передача завершилась с кодом " + $process.ExitCode + ": " + $errorText + $outputText)
        }
    }
    finally {
        $process.Dispose()
    }
}

function Get-StationFacts {
    $remote = @'
set -eu
printf 'VERSION='; cat /data/yapanel/VERSION
printf 'STATE='; sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /data/yapanel-state/updater/status.json | head -1
printf 'TRANSACTION='; sed -n 's/.*"transaction"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /data/yapanel-state/updater/status.json | head -1
printf 'TARGET_SHA='; sha256sum /data/yapanel/max_profile.py | cut -d' ' -f1
printf 'TARGET_META='; stat -c '%u:%g:%a' /data/yapanel/max_profile.py
printf 'YAPANEL='; getprop init.svc.yapanel
printf 'ROOTD='; getprop init.svc.rootd
printf 'ADBGUARD='; getprop init.svc.adbguard
printf 'ABSSHD='; getprop init.svc.absshd
printf 'PANEL_PIDS='; ps -A -o ARGS | grep '[s]erver.py' | wc -l
printf 'PENDING='
for journal in /data/yapanel-state/updater/transactions/*/transaction.json; do
    [ -f "$journal" ] || continue
    state=$(sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$journal" | head -1)
    case "$state" in
        backup|activating|activated_pending_health|rollback_started|rollback_pending_health) printf '%s ' "$state";;
    esac
done
printf '\n'
'@
    $lines = Invoke-StationSsh $remote
    $facts = @{}
    foreach ($line in $lines) {
        if ($line -match '^([A-Z_]+)=(.*)$') {
            $facts[$matches[1]] = $matches[2].Trim()
        }
    }
    return $facts
}

function Assert-StationPreflight {
    param([string]$TargetHash)
    $facts = Get-StationFacts
    foreach ($name in @("VERSION", "STATE", "TARGET_SHA", "TARGET_META", "YAPANEL", "ROOTD", "ADBGUARD", "ABSSHD")) {
        if (-not $facts.ContainsKey($name)) {
            throw "Не удалось получить read-only preflight: $name"
        }
    }
    if ($facts.VERSION -ne $ExpectedVersion) { throw "Ожидалась версия $ExpectedVersion, получена $($facts.VERSION)" }
    if ($facts.STATE -ne "healthy") { throw "Updater journal не healthy: $($facts.STATE)" }
    if ($facts.PENDING) { throw "Найдена незавершённая транзакция: $($facts.PENDING)" }
    if ($facts.YAPANEL -ne "running") { throw "Служба yapanel не running" }
    foreach ($service in @("ROOTD", "ADBGUARD", "ABSSHD")) {
        if ($facts[$service] -ne "running") { throw "Служба $service не running" }
    }
    if ($facts.PANEL_PIDS -ne "1") { throw "Ожидался единственный процесс server.py, найдено: $($facts.PANEL_PIDS)" }
    $expectedTargetHash = if ($TargetHash) {
        $TargetHash
    } elseif ($Rollback) {
        $ExpectedActiveSha256
    } else {
        $ExpectedCurrentSha256
    }
    if ($expectedTargetHash -and $facts.TARGET_SHA.ToLowerInvariant() -ne $expectedTargetHash.ToLowerInvariant()) {
        throw "Текущий SHA-256 max_profile.py не совпадает с ожидаемым"
    }
    return $facts
}

function Assert-HttpHealth {
    try {
        $health = Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 ($HealthUri.TrimEnd('/') + "/api/panel/health")
    }
    catch {
        throw "Не удалось получить HTTP health: $($_.Exception.Message)"
    }
    if ($health.ready -ne $true -or [string]$health.version -ne $ExpectedVersion -or [int]$health.critical_errors -ne 0) {
        throw "Strict HTTP health не подтверждён"
    }
}

function Assert-LocalPython {
    param([string]$Path)
    & $python -c "from pathlib import Path; compile(Path(r'$Path').read_text(encoding='utf-8'), r'$Path', 'exec')"
    if ($LASTEXITCODE -ne 0) { throw "Локальный Python syntax check не пройден" }
}

function Invoke-Backup {
    New-Item -ItemType Directory -Force -Path $WorkDirectory | Out-Null
    $backupDir = Join-Path $WorkDirectory "backups"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "backup_station_max.ps1") `
        -OutputDirectory $backupDir -SshAlias $SshAlias -SshExecutable $SshExecutable -ExpectedVersion $ExpectedVersion
    if ($LASTEXITCODE -ne 0) { throw "Создание backup завершилось с ошибкой" }
    $archive = Get-ChildItem -LiteralPath $backupDir -Filter "*.tar.gz" -File |
        Sort-Object LastWriteTimeUtc | Select-Object -Last 1
    if (-not $archive) { throw "Backup-архив не найден" }
    return $archive.FullName
}

function Get-BackupFile {
    param([string]$Archive)
    $extract = Join-Path ([IO.Path]::GetTempPath()) ("station-patch-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $extract | Out-Null
    try {
        & tar.exe -tzf $Archive | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Backup-архив не читается" }
        & tar.exe -xzf $Archive -C $extract "yapanel/max_profile.py"
        if ($LASTEXITCODE -ne 0) { throw "max_profile.py отсутствует в backup" }
        $file = Join-Path $extract "yapanel/max_profile.py"
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Не удалось извлечь max_profile.py" }
        Assert-LocalPython $file
        return @{ Root = $extract; File = $file }
    }
    catch {
        Remove-Item -LiteralPath $extract -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Invoke-RemoteFileActivation {
    param([string]$LocalFile, [string]$ExpectedHash, [string]$FactsTransaction, [string]$Operation)
    $id = $Operation + "-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + ([guid]::NewGuid().ToString("N").Substring(0, 8))
    $remoteDir = "/data/yapanel-state/updater/patches/$id"
    $remoteNew = "$remoteDir/max_profile.py.new"
    $remoteBefore = "$remoteDir/max_profile.py.before"
    $remote = @'
set -eu
mkdir -p '__REMOTE_DIR__'
cp -p '__REMOTE_PATH__' '__REMOTE_BEFORE__'
old_meta=$(stat -c '%u:%g:%a' '__REMOTE_PATH__')
test "$(sha256sum '__REMOTE_NEW__' | cut -d' ' -f1)" = '__EXPECTED_HASH__'
/data/python/bin/python3 -c "from pathlib import Path; compile(Path('__REMOTE_NEW__').read_text(encoding='utf-8'), '__REMOTE_NEW__', 'exec')"
chown "$(stat -c '%u:%g' '__REMOTE_PATH__')" '__REMOTE_NEW__'
chmod "$(stat -c '%a' '__REMOTE_PATH__')" '__REMOTE_NEW__'
mv '__REMOTE_NEW__' '__REMOTE_PATH__'
/data/python/bin/python3 -c "from pathlib import Path; compile(Path('__REMOTE_PATH__').read_text(encoding='utf-8'), '__REMOTE_PATH__', 'exec')"
test "$(sha256sum '__REMOTE_PATH__' | cut -d' ' -f1)" = '__EXPECTED_HASH__'
test "$(stat -c '%u:%g:%a' '__REMOTE_PATH__')" = "$old_meta"
'@
    $remote = $remote.Replace("__REMOTE_DIR__", $remoteDir).
        Replace("__REMOTE_PATH__", $remotePath).
        Replace("__REMOTE_BEFORE__", $remoteBefore).
        Replace("__REMOTE_NEW__", $remoteNew).
        Replace("__EXPECTED_HASH__", $ExpectedHash)
    Invoke-StationSsh ("mkdir -p '" + $remoteDir + "'") | Out-Null
    Send-StationFileBase64 $LocalFile $remoteNew
    Invoke-StationSsh $remote | Out-Null
    Assert-HttpHealth
    $after = Assert-StationPreflight -TargetHash $ExpectedHash
    if ($after.TRANSACTION -ne $FactsTransaction) { throw "Изменился updater transaction" }
    Write-Output "Файл активирован атомарно: $remotePath"
    Write-Output "Backup-файл на станции: $remoteBefore"
}

& $AdbExecutable connect ($AdbSerial -replace ":.*$", "") | Out-Null
& $AdbExecutable -s $AdbSerial shell id | Out-Null
if ($LASTEXITCODE -ne 0) { throw "ADB недоступен" }

if ($DryRun) {
    Assert-LocalPython $Source
    $sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
    $facts = Assert-StationPreflight
    Assert-HttpHealth
    Write-Output "Dry-run: станция не изменялась."
    Write-Output "Исходный SHA-256 max_profile.py: $sourceHash"
    Write-Output "Текущий SHA-256 max_profile.py: $($facts.TARGET_SHA)"
    Write-Output "Updater transaction: $($facts.TRANSACTION)"
    return
}

if ($Apply) {
    Assert-LocalPython $Source
    $newHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
    $facts = Assert-StationPreflight
    Assert-HttpHealth
    $archive = Invoke-Backup
    $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output "Backup: $archive"
    Write-Output "Backup SHA-256: $archiveHash"
    Invoke-RemoteFileActivation $Source $newHash $facts.TRANSACTION "max-profile"
    Write-Output "Новый SHA-256 max_profile.py: $newHash"
    Write-Output "Перезапуск панели не выполнялся; загруженный процесс использует прежний импорт до отдельного согласования restart."
}

if ($Rollback) {
    $backup = Get-BackupFile $BackupArchive
    try {
        $oldHash = (Get-FileHash -LiteralPath $backup.File -Algorithm SHA256).Hash.ToLowerInvariant()
        $facts = Assert-StationPreflight
        Invoke-RemoteFileActivation $backup.File $oldHash $facts.TRANSACTION "max-profile-rollback"
        Write-Output "max_profile.py восстановлен из проверенного backup."
        Write-Output "Временный каталог извлечения: $($backup.Root)"
    }
    catch {
        Remove-Item -LiteralPath $backup.Root -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}
