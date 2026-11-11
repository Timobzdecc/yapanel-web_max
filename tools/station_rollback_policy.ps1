function Get-StationRollbackOwner {
    param(
        [bool]$HandoffAttempted,
        [string]$ServiceState,
        [string]$PanelPid,
        [string]$CandidateCount,
        [string]$SupervisorOwner
    )

    if ($HandoffAttempted) { return "supervisor" }
    if ($ServiceState -eq "running" -or -not [string]::IsNullOrWhiteSpace($PanelPid) -or $CandidateCount -ne "0") {
        return "blocked"
    }
    if ($SupervisorOwner -eq "yes") { return "blocked" }
    return "direct"
}
