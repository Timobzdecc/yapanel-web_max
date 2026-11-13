[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path (Get-Location) "station-backups"),
    [string]$SshAlias = "station-max",
    [string]$SshExecutable = "ssh.exe",
    [string]$ExpectedVersion = "0.3.5"
)

$ErrorActionPreference = "Stop"

function Invoke-CheckedSsh {
    param([string]$RemoteCommand)

    $output = & $SshExecutable -o BatchMode=yes $SshAlias $RemoteCommand 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "SSH-команда завершилась с кодом ${LASTEXITCODE}: $RemoteCommand`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

function Invoke-BinaryTarBackup {
    param([string]$ArchivePath)

    $sshPath = (Get-Command $SshExecutable -ErrorAction Stop).Source
    $quotedSsh = '"' + $sshPath.Replace('"', '\"') + '"'
    $quotedArchive = '"' + $ArchivePath.Replace('"', '\"') + '"'
    $remote = 'test "$(cat /data/yapanel/VERSION)" = "' + $ExpectedVersion + '" && ' +
        'test -f /data/yapanel/start.sh && test -d /data/yapanel-state && ' +
        'tar -C /data -czf - yapanel yapanel-state'
    $command = "$quotedSsh -o BatchMode=yes $SshAlias `"$remote`" > $quotedArchive"
    & $env:ComSpec /d /s /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось получить бинарный tar-архив по SSH, код $LASTEXITCODE"
    }
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$outputRoot = (Resolve-Path $OutputDirectory).Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = Join-Path $outputRoot "yapanel-station-$ExpectedVersion-$stamp.tar.gz"
$listing = "$archive.list.txt"
$manifestPath = "$archive.manifest.json"
$hashPath = "$archive.sha256"
$extractRoot = Join-Path ([IO.Path]::GetTempPath()) ("yapanel-backup-" + [guid]::NewGuid().ToString("N"))

$versionCheck = 'test -d /data/yapanel && test -d /data/yapanel-state && test -f /data/yapanel/start.sh && test "$(cat /data/yapanel/VERSION)" = "' + $ExpectedVersion + '"'
Invoke-CheckedSsh $versionCheck
Invoke-BinaryTarBackup $archive

if (-not (Test-Path -LiteralPath $archive) -or (Get-Item -LiteralPath $archive).Length -eq 0) {
    throw "Получен пустой backup-архив"
}

& tar.exe -tzf $archive | Set-Content -LiteralPath $listing -Encoding ASCII
if ($LASTEXITCODE -ne 0) {
    throw "Локальная проверка tar-архива завершилась с ошибкой"
}
$entries = Get-Content -LiteralPath $listing
foreach ($required in @("yapanel/VERSION", "yapanel/start.sh", "yapanel/server.py", "yapanel-state/")) {
    if (-not ($entries | Where-Object { $_ -eq $required -or $_ -like "$required*" })) {
        throw "В backup отсутствует обязательный путь: $required"
    }
}

try {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    & $python (Join-Path $PSScriptRoot "verify_station_backup.py") --archive $archive --output $extractRoot --version $ExpectedVersion
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось проверить извлечение backup-архива"
    }
    $extractedStart = Join-Path $extractRoot "yapanel/start.sh"
    $extractedStartHash = (Get-FileHash -LiteralPath $extractedStart -Algorithm SHA256).Hash.ToLowerInvariant()
}
finally {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $hashPath -Value "$hash  $(Split-Path -Leaf $archive)" -Encoding ASCII

$remoteFacts = Invoke-CheckedSsh "printf 'version='; cat /data/yapanel/VERSION; printf '\nstart_sha256='; sha256sum /data/yapanel/start.sh; printf '\n'; du -sh /data/yapanel-state"
$remoteStartHash = (($remoteFacts -join "`n") | Select-String -Pattern 'start_sha256=([0-9a-fA-F]{64})').Matches.Groups[1].Value.ToLowerInvariant()
if (-not $remoteStartHash -or $remoteStartHash -ne $extractedStartHash) {
    throw "SHA-256 start.sh в архиве не совпадает с удалённым файлом"
}
$manifest = [ordered]@{
    created = (Get-Date).ToUniversalTime().ToString("o")
    ssh_alias = $SshAlias
    version = $ExpectedVersion
    archive = (Split-Path -Leaf $archive)
    sha256 = $hash
    start_sha256 = $extractedStartHash
    required_paths = @("yapanel/VERSION", "yapanel/start.sh", "yapanel/server.py", "yapanel-state/")
    remote_facts = @($remoteFacts)
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

& icacls.exe $archive /inheritance:r /grant:r "${env:USERNAME}:(R)" "*S-1-5-32-544:(R)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось ограничить ACL backup-архива"
}

Write-Output "Backup создан: $archive"
Write-Output "SHA-256: $hash"
Write-Output "Manifest: $manifestPath"
