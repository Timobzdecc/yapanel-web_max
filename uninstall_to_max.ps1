param(
    [string]$TargetIP = "",
    [switch]$Full
)

$ErrorActionPreference = "Stop"

function Find-Adb {
    $cmd = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
        "$env:ProgramFiles\Android\Sdk\platform-tools\adb.exe",
        "C:\platform-tools\adb.exe"
    )
    foreach ($path in $candidates) { if (Test-Path $path) { return $path } }
    return $null
}

$AdbExe = Find-Adb
if (-not $AdbExe) {
    Write-Host "[ОШИБКА] adb не найден. Установите platform-tools." -ForegroundColor Red
    exit 1
}

if (-not $TargetIP) {
    $TargetIP = Read-Host "Введите IP-адрес Станции (например 192.168.1.100)"
}
$TargetIP = $TargetIP.Trim()
if ($TargetIP -notmatch ":") { $TargetIP = "${TargetIP}:5555" }
$Device = $TargetIP

Write-Host "[1/4] Подключение к $Device..." -ForegroundColor Green
& $AdbExe connect $Device | Out-Null
$state = (& $AdbExe -s $Device get-state 2>&1) | Out-String
if ($state -notmatch "device") {
    Write-Host "[ОШИБКА] Не удалось подключиться к $Device" -ForegroundColor Red
    exit 1
}

function Invoke-RootShell {
    param([string]$Command)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Command))
    $scriptFile = "/data/local/tmp/.yp_rootcmd.sh"
    $runner = "echo $b64 | base64 -d > $scriptFile; chmod 755 $scriptFile; su -c sh $scriptFile; rm -f $scriptFile"
    return (& $AdbExe -s $Device shell $runner 2>&1) | Out-String
}

Write-Host "[2/4] Проверка root..." -ForegroundColor Green
$uid = Invoke-RootShell "id -u"
if ($uid -notmatch "0") {
    Write-Host "[ОШИБКА] Нет root-доступа: $uid" -ForegroundColor Red
    exit 1
}

Write-Host "[3/4] Остановка и удаление панели..." -ForegroundColor Green
$remove = @'
for pat in 'server.py --host 0.0.0.0 --port 8765' 'assistant_max.py' 'alice-frida-supervisor' 'frida-inject' 'gst_supervisor.sh' 'assistantd'; do
  for p in $(ps -A -o PID,ARGS | grep -F "$pat" | grep -v grep | awk '{print $1}'); do kill -9 $p 2>/dev/null; done
done
umount /data/quasar/webpanel/patterns/clock/boot.binscled.zst 2>/dev/null
rm -rf /data/yapanel /data/yapanel-state /data/quasar/webpanel /data/wakeword
rm -f /data/local/userinit.sh
rm -f /data/local/tmp/panel_embedded.tar.gz /data/local/tmp/yapanel_max.tar.gz /data/local/tmp/gst_* /data/local/tmp/rootcmd-* /data/local/tmp/rootres.* /data/local/tmp/rootout /data/local/tmp/sup.out
echo PANEL_REMOVED
'@
$out = Invoke-RootShell $remove
if ($out -notmatch "PANEL_REMOVED") {
    Write-Host "[ОШИБКА] Удаление не подтверждено:`n$out" -ForegroundColor Red
    exit 1
}

if ($Full) {
    Write-Host "[4/4] Удаление runtime (/data/python, /data/lib, /data/etc)..." -ForegroundColor Yellow
    Invoke-RootShell "rm -rf /data/python /data/lib /data/etc; echo RUNTIME_REMOVED" | Out-Null
} else {
    Write-Host "[4/4] Runtime оставлен (/data/python, /data/lib, /data/etc)" -ForegroundColor Green
}

Write-Host "Готово: панель удалена со станции." -ForegroundColor Green
