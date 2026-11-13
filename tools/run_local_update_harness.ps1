[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$UpdateAgent,
    [Parameter(Mandatory = $true)][string]$Contract,
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$State,
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$ResultPath
)

$ErrorActionPreference = "Stop"
$digest = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
$arguments = @(
    $UpdateAgent, "apply",
    "--root", $Root,
    "--state", $State,
    "--archive", $Archive,
    "--version", $Version,
    "--sha256", $digest
)
$output = & $Python @arguments 2>&1
if ($LASTEXITCODE -ne 0) {
    throw ("update_agent apply завершился с ошибкой: " + ($output -join [Environment]::NewLine))
}

$jsonLine = @($output | Where-Object { $_ -match '^\s*\{.*\}\s*$' } | Select-Object -Last 1)
if ($jsonLine.Count -ne 1) {
    throw ("update_agent apply не вернул JSON: " + ($output -join [Environment]::NewLine))
}
$result = $jsonLine | ConvertFrom-Json
$result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ResultPath -Encoding UTF8

$journalPath = Join-Path $State ("updater/transactions/" + [string]$result.transaction + "/transaction.json")
function Invoke-StationSsh {
    param([string]$RemoteCommand)
    if ($RemoteCommand -notmatch "transaction.json") {
        throw "Локальный harness получил неожиданную SSH-команду: $RemoteCommand"
    }
    return Get-Content -Raw -LiteralPath $journalPath
}

. $Contract
$journal = Assert-StationApplyContract $result $Version
if ([string]$journal.state -ne "activated_pending_health") {
    throw "Локальный harness получил неожиданный durable state"
}
Write-Output ("PowerShell contract accepted transaction " + [string]$result.transaction)
