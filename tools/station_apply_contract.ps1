function Get-StationTransactionJournal {
    param([Parameter(Mandatory = $true)][string]$TransactionId)
    if ($TransactionId -notmatch '^[A-Za-z0-9-]+$') {
        throw "Некорректный идентификатор транзакции"
    }
    $raw = (Invoke-StationSsh ("cat /data/yapanel-state/updater/transactions/" + $TransactionId + "/transaction.json")) -join [Environment]::NewLine
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        throw "Не удалось разобрать durable journal транзакции $TransactionId"
    }
}

function Assert-StationApplyContract {
    param(
        [Parameter(Mandatory = $true)][object]$Result,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )
    $transactionId = [string]$Result.transaction
    if ([string]::IsNullOrWhiteSpace($transactionId)) {
        throw "update_agent apply не вернул transaction"
    }
    $journal = Get-StationTransactionJournal $transactionId
    if ([string]$journal.id -ne $transactionId) { throw "transaction id CLI не совпадает с durable journal" }
    if ([string]$journal.version -ne $ExpectedVersion) { throw "Версия durable journal не совпадает с ожидаемой" }
    if ([string]$journal.state -ne "activated_pending_health") { throw "Durable journal не подтверждает activated_pending_health" }
    if ([string]$Result.state -ne [string]$journal.state) { throw "Состояние CLI не совпадает с durable journal" }
    if ([string]$Result.version -ne [string]$journal.version) { throw "Версия CLI не совпадает с durable journal" }
    return $journal
}

function Test-StationRecoveryRequired {
    param([Parameter(Mandatory = $true)][object]$Journal)
    $safeStates = @("staged", "healthy", "rolled_back", "abandoned")
    return $safeStates -notcontains [string]$Journal.state
}
