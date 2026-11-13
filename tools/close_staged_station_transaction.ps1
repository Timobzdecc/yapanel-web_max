[CmdletBinding()]
param(
    [string]$SshAlias = "station-max",
    [string]$Transaction = "20261111-162240-a47c353e",
    [string]$ExpectedVersion = "0.3.5"
)

$ErrorActionPreference = "Stop"
$startSha256 = "61d13d10a194dc31d1225fc2c094f9ecadf8b4c2655e7139ced1eaa9c6d44f36"
$conflicts = @(
    "CONTRIBUTION.md",
    "LICENSE",
    "README.md",
    "device/agent/build/VERSION",
    "device/agent/build/yapanel-agent",
    "device/assistant/build/VERSION",
    "device/assistant/build/assistantd",
    "device/viz/build/VERSION",
    "device/viz/build/vizd"
)

function Invoke-StationSsh {
    param([string]$RemoteCommand)
    $base64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($RemoteCommand))
    $transport = "printf %s $base64 | /system/bin/base64 -d | /system/bin/sh"
    $output = & ssh.exe -o BatchMode=yes $SshAlias $transport 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("SSH-команда завершилась с кодом " + $LASTEXITCODE + ": " + ($output -join [Environment]::NewLine))
    }
    return @($output)
}

$quotedConflicts = ($conflicts | ForEach-Object { "--conflict '" + $_ + "'" }) -join " "
$conflictsBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($conflicts | ConvertTo-Json -Compress)))
$closurePython = @'
import base64, hashlib, json, os, time
from pathlib import Path

transaction = "__TX__"
reason = "baseline_conflict"
conflicts = json.loads(base64.b64decode("__CONFLICTS_B64__").decode("utf-8"))
transaction_dir = Path("/data/yapanel-state/updater/transactions") / transaction
journal_path = transaction_dir / "transaction.json"
archive_path = transaction_dir.with_suffix(".zip")

def durable(path, value):
    target = Path(path)
    temporary = target.with_name(target.name + ".close-tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with temporary.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    descriptor = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

journal = json.loads(journal_path.read_text(encoding="utf-8"))
if journal.get("state") == "abandoned":
    print(json.dumps({"state": "abandoned", "idempotent": True}))
    raise SystemExit(0)
if journal.get("state") != "staged":
    raise SystemExit("Нельзя закрыть транзакцию в состоянии " + str(journal.get("state")))
if journal.get("backup") or journal.get("completed"):
    raise SystemExit("Staged-транзакция содержит признаки активации")
closure_path = transaction_dir / "closure.json"
if closure_path.is_file():
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    if closure.get("transaction") != transaction or closure.get("reason") != reason or closure.get("conflicts") != conflicts:
        raise SystemExit("Существующий closure.json не совпадает")
else:
    digest = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    closure = {"transaction": transaction, "previous_state": "staged", "reason": reason,
               "conflicts": conflicts, "journal_sha256_before": digest, "closed_at": time.time()}
    durable(closure_path, closure)
journal.setdefault("history", []).append({"state": "staged", "at": closure["closed_at"]})
journal.update({"state": "abandoned", "closed_at": closure["closed_at"], "close_reason": reason,
                "close_conflicts": conflicts, "closure": str(closure_path)})
durable(journal_path, journal)
print(json.dumps({"state": "abandoned", "idempotent": False}))
'@.Replace('__TX__', $Transaction).Replace('__CONFLICTS_B64__', $conflictsBase64)
$closurePythonBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($closurePython))
$command = @'
set -eu
tx=/data/yapanel-state/updater/transactions/__TX__
journal=$tx/transaction.json
archive=/data/yapanel-state/updater/transactions/__TX__.zip
test -f "$journal"
test -f "$archive"
test "$(cat /data/yapanel/VERSION)" = "__VERSION__"
test "$(sha256sum /data/yapanel/start.sh | sed 's/ .*//')" = "__START_SHA__"
state=$(/data/python/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "$journal")
test "$state" = staged
if [ -f /data/yapanel-state/updater/status.json ] && grep -Eq '"state"[[:space:]]*:[[:space:]]*"(activating|activated_pending_health|rollback_started)"' /data/yapanel-state/updater/status.json; then
    echo active_updater_status >&2
    exit 31
fi
if ps -eo args | grep -E '/data/yapanel-state/updater/(update_agent|updater_bootstrap)\.py' | grep -v grep >/dev/null 2>&1; then
    echo active_updater_process >&2
    exit 32
fi
archive_before=$(sha256sum "$archive" | sed 's/ .*//')
printf %s __CLOSURE_PYTHON_B64__ | /system/bin/base64 -d | /data/python/bin/python3 -
new_state=$(/data/python/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "$journal")
test "$new_state" = abandoned
test -f "$tx/closure.json"
test "$(sha256sum "$archive" | sed 's/ .*//')" = "$archive_before"
test "$(cat /data/yapanel/VERSION)" = "__VERSION__"
test "$(sha256sum /data/yapanel/start.sh | sed 's/ .*//')" = "__START_SHA__"
echo transaction=__TX__
echo state=abandoned
echo archive_sha256=$archive_before
'@.Replace('__TX__', $Transaction).Replace('__VERSION__', $ExpectedVersion).Replace('__START_SHA__', $startSha256).Replace('__CLOSURE_PYTHON_B64__', $closurePythonBase64)
Invoke-StationSsh $command
