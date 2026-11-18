param(
    [string]$TargetIP = "",
    [switch]$Full,
    [switch]$NoPause
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8

function Write-Header {
    if (-not [Console]::IsOutputRedirected) {
        try { Clear-Host } catch {}
    }
    $title = "YapAnel · Удаление панели со Станции Макс"
    $sub   = "Остановка служб · Восстановление системных звуков и монтирований"
    $w = 68
    Write-Host ("╔" + ("═" * $w) + "╗") -ForegroundColor Cyan
    Write-Host ("║  " + $title.PadRight($w - 2) + "║") -ForegroundColor Yellow
    Write-Host ("║  " + $sub.PadRight($w - 2) + "║") -ForegroundColor Gray
    Write-Host ("╚" + ("═" * $w) + "╝") -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step([int]$num, [int]$total, [string]$label) {
    Write-Host ""
    Write-Host ("[{0}/{1}] " -f $num, $total) -NoNewline -ForegroundColor Cyan
    Write-Host $label -ForegroundColor White
    Write-Host ("  " + ("─" * 66)) -ForegroundColor DarkGray
}

function Write-Info([string]$msg) { Write-Host "  • " -NoNewline -ForegroundColor DarkCyan; Write-Host $msg -ForegroundColor Gray }
function Write-Ok([string]$msg)   { Write-Host "  ✓ " -NoNewline -ForegroundColor Green; Write-Host $msg -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  ! " -NoNewline -ForegroundColor Yellow; Write-Host $msg -ForegroundColor Yellow }

function Show-ErrorAndExit([string]$msg) {
    Write-Host ""
    Write-Host ("┌─ ОШИБКА " + ("─" * 59)) -ForegroundColor Red
    foreach ($line in ($msg -split "`r?`n")) { Write-Host ("│ " + $line) -ForegroundColor Red }
    Write-Host ("└" + ("─" * 68)) -ForegroundColor Red
    Write-Host ""
    if (-not $NoPause -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        Read-Host "Нажмите Enter для выхода"
    }
    exit 1
}

function Find-Adb([string]$ScriptDir) {
    $candidates = @(
        (Join-Path $ScriptDir "adb.exe"),
        (Join-Path $ScriptDir "platform-tools\adb.exe"),
        (Join-Path $ScriptDir "tools\platform-tools\adb.exe")
    )
    $cmd = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe") }
    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "Android\Sdk\platform-tools\adb.exe") }
    $pf86 = ${env:ProgramFiles(x86)}
    if ($pf86) { $candidates += (Join-Path $pf86 "Android\Sdk\platform-tools\adb.exe") }
    $candidates += @("C:\platform-tools\adb.exe", "adb")

    foreach ($cand in $candidates) {
        if (-not $cand) { continue }
        try {
            if ($cand -ne "adb" -and -not (Test-Path -LiteralPath $cand)) { continue }
            $null = & $cand version 2>$null
            if ($LASTEXITCODE -eq 0) { return $cand }
        } catch {}
    }
    return $null
}

function Parse-StationEndpoint([string]$Raw) {
    $s = ([string]$Raw).Trim() -replace "^https?://", ""
    $s = ($s -split "/")[0].Trim()
    $hostPart = $s
    $portPart = "5555"
    if ($s -match "^\[([^\]]+)\]:(\d+)$") {
        $hostPart = $Matches[1]
        $portPart = $Matches[2]
    } elseif ($s -match "^([^:]+):(\d+)$") {
        $hostPart = $Matches[1]
        if ($Matches[2] -ne "8765" -and $Matches[2] -ne "8766") {
            $portPart = $Matches[2]
        }
    }
    return @{ Host = $hostPart; Port = $portPart; Device = "${hostPart}:${portPart}" }
}

Write-Header

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir) { $ScriptDir = (Get-Location).Path }

$AdbExe = Find-Adb $ScriptDir
if (-not $AdbExe) {
    Show-ErrorAndExit "adb.exe не найден. Установите Android platform-tools или поместите adb.exe рядом со скриптом."
}

$ipFile = Join-Path $ScriptDir ".station_ip"
if (-not $TargetIP -and (Test-Path -LiteralPath $ipFile)) {
    $savedIp = (Get-Content -LiteralPath $ipFile -Raw -Encoding UTF8).Trim()
    if ($savedIp) {
        Write-Info "Сохранённый адрес: $savedIp"
        $promptIp = Read-Host "  › IP колонки [Enter = $savedIp]"
        if ([string]::IsNullOrWhiteSpace($promptIp)) { $TargetIP = $savedIp } else { $TargetIP = $promptIp.Trim() }
    }
}
while ([string]::IsNullOrWhiteSpace($TargetIP)) {
    $TargetIP = ([string](Read-Host "  › Введите IP-адрес Станции Макс (например 192.168.1.50)")).Trim()
}

$ep = Parse-StationEndpoint $TargetIP
$Device = $ep.Device

Write-Step 1 4 "Подключение по ADB ($Device)"
& $AdbExe connect $Device 2>&1 | Out-Null
$state = ((& $AdbExe -s $Device get-state 2>&1) | Out-String).Trim()
if ($state -ne "device") {
    Show-ErrorAndExit "Не удалось подключиться к $Device (state: $state)."
}
Write-Ok "ADB подключён ($Device)"

function Invoke-RootdShell {
    param([string]$Command, [int]$TimeoutSec = 30)
    $epoch = [int][Math]::Floor(([DateTime]::UtcNow - [DateTime]'1970-01-01').TotalSeconds)
    $rid = "yp-" + $epoch + "-" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))
    $cmdB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Command -replace "`r`n", "`n") + "`n"))
    $runnerScript = @"
rid="$rid"
q="/data/local/tmp"
echo "$cmdB64" | base64 -d > "`$q/.rootcmd-`$rid.tmp"
chmod 0660 "`$q/.rootcmd-`$rid.tmp" 2>/dev/null
mv -f "`$q/.rootcmd-`$rid.tmp" "`$q/rootcmd-`$rid"
i=0
while [ `$i -lt $TimeoutSec ]; do
  [ -s "`$q/rootres.`$rid" ] && break
  sleep 1
  i=`$((i + 1))
done
if [ ! -f "`$q/rootres.`$rid" ]; then
  rm -f "`$q/.rootcmd-`$rid.tmp" "`$q/rootcmd-`$rid"
  echo "ROOTD_TIMEOUT"
  exit 124
fi
_status=`$(grep -m 1 '^status=' "`$q/rootres.`$rid" 2>/dev/null | cut -d= -f2)
_reason=`$(grep -m 1 '^reason=' "`$q/rootres.`$rid" 2>/dev/null | cut -d= -f2-)
_b=`$(grep -nF -e "--- rootd request=`$rid begin ---" "`$q/rootout" 2>/dev/null | head -n 1 | cut -d: -f1)
_e=`$(grep -nF -e "--- rootd request=`$rid " "`$q/rootout" 2>/dev/null | grep " exit=" | head -n 1 | cut -d: -f1)
if [ -n "`$_b" ] && [ -n "`$_e" ] && [ "`$_e" -gt "`$_b" ]; then
  sed -n "`$((_b+1)),`$((_e-1))p" "`$q/rootout"
elif [ -n "`$_status" ] && [ "`$_status" != "executed" ]; then
  echo "ROOTD_ERROR: status=`$_status reason=`$_reason"
fi
rm -f "`$q/rootres.`$rid"
"@ -replace "`r`n", "`n"
    $wrapB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($runnerScript))
    $tmpWrap = "/data/local/tmp/.yp_rootd_$rid.sh"
    return (& $AdbExe -s $Device shell "echo $wrapB64 | base64 -d > $tmpWrap; sh $tmpWrap; rm -f $tmpWrap" 2>&1) | Out-String
}

Write-Step 2 4 "Проверка Root-доступа"
$script:RootMode = $null
$script:SuPath = "su"
$suId = ""
$rootdId = ""
$adbId = (& $AdbExe -s $Device shell "id" 2>&1) | Out-String
if ($adbId -match "uid=0\(root\)") {
    $script:RootMode = "direct"
} else {
    $suCandidates = @("su", "/odm/bin/su", "/vendor/xbin/su", "/vendor/bin/su", "/sbin/su", "/su/bin/su", "/system/xbin/su", "/system/bin/su", "/system/sbin/su")
    foreach ($cand in $suCandidates) {
        if ($cand -ne "su") {
            $exists = (& $AdbExe -s $Device shell "[ -x $cand ] && echo 1" 2>$null) | Out-String
            if ($exists -notmatch "1") { continue }
        }
        $tryC = (& $AdbExe -s $Device shell "$cand -c 'id'" 2>&1) | Out-String
        if ($tryC -match "uid=0\(root\)") {
            $suId = $tryC
            $script:SuPath = $cand
            $script:RootMode = "su"
            break
        }
        foreach ($su0Cmd in @("$cand 0 id", "$cand 0 sh -c id")) {
            $try0 = (& $AdbExe -s $Device shell $su0Cmd 2>&1) | Out-String
            if ($try0 -match "uid=0\(root\)") {
                $suId = $try0
                $script:SuPath = $cand
                $script:RootMode = "su0"
                break
            }
        }
        if ($script:RootMode) { break }
    }
    if (-not $script:RootMode) {
        $sudoId = (& $AdbExe -s $Device shell "sudo id" 2>&1) | Out-String
        if ($sudoId -match "uid=0\(root\)") {
            $script:RootMode = "sudo"
        }
    }
    if (-not $script:RootMode) {
        $hasRootdBin = (& $AdbExe -s $Device shell "(pidof rootd >/dev/null 2>&1 || [ -x /odm/bin/rootd ]) && echo 1" 2>$null) | Out-String
        if ($hasRootdBin -match "1") {
            $rootdId = Invoke-RootdShell "id" 8
            if ($rootdId -match "uid=0\(root\)") { $script:RootMode = "rootd" }
        }
    }
    if (-not $script:RootMode) {
        $null = & $AdbExe -s $Device root 2>&1
        Start-Sleep -Seconds 2
        & $AdbExe connect $Device 2>$null | Out-Null
        $adbRootCheck = (& $AdbExe -s $Device shell "id" 2>&1) | Out-String
        if ($adbRootCheck -match "uid=0\(root\)") { $script:RootMode = "direct" }
    }
}
if (-not $script:RootMode) {
    Show-ErrorAndExit ("Нет Root-доступа на $Device.`n  adb id: $($adbId.Trim())`n  su: $($suId.Trim())`n  rootd: $($rootdId.Trim())")
}
$rootLabel = if ($script:RootMode -in @("su", "su0")) { "$($script:RootMode) ($($script:SuPath))" } else { $script:RootMode }
Write-Ok "Root-доступ активен: $rootLabel"

function Invoke-RootShell {
    param([string]$Command, [int]$TimeoutSec = 60)
    if ($script:RootMode -eq "rootd") {
        return Invoke-RootdShell $Command $TimeoutSec
    }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Command -replace "`r`n", "`n")))
    $scriptFile = "/data/local/tmp/.yp_rootcmd.sh"
    $prep = "echo $b64 | base64 -d > $scriptFile; chmod 755 $scriptFile"
    if ($script:RootMode -eq "direct") {
        $execCmd = "sh $scriptFile"
    } elseif ($script:RootMode -eq "su") {
        $execCmd = "$script:SuPath -c 'sh $scriptFile'"
    } elseif ($script:RootMode -eq "su0") {
        $execCmd = "$script:SuPath 0 sh $scriptFile"
    } else {
        $execCmd = "sudo sh $scriptFile"
    }
    return (& $AdbExe -s $Device shell "$prep; $execCmd; rm -f $scriptFile" 2>&1) | Out-String
}

Write-Step 3 4 "Остановка служб, снятие mount и удаление панели"
$remove = @'
for pat in \
  'server.py --host 0.0.0.0 --port 8765' \
  'assistant_max.py' \
  'alice-frida-supervisor' \
  'frida-inject' \
  'gst_supervisor.sh' \
  'assistantd' \
  'ExtMusicPlayer' \
  'NativeMediaCtl' \
  'bt-memes' \
  'wakeword' \
  'yapanel-agent' \
  '/data/quasar/webpanel/viz'; do
  (ps -A -o PID,ARGS 2>/dev/null || ps -ef 2>/dev/null || ps 2>/dev/null) | grep -F "$pat" | grep -v grep | while read -r p _rest; do
    [ -n "$p" ] && kill -9 "$p" 2>/dev/null || true
  done
done

QUASAR="/data/data/com.yandex.io.sdk/files/system"
WEB="/data/quasar/webpanel"

# Восстановить оригинальные системные звуки и заставку, снять bind-mounts панели
grep -q " $WEB/patterns/clock/boot.binscled.zst " /proc/mounts 2>/dev/null && umount "$WEB/patterns/clock/boot.binscled.zst" 2>/dev/null || true
grep -q " $QUASAR/sounds/common/boot.wav " /proc/mounts 2>/dev/null && umount "$QUASAR/sounds/common/boot.wav" 2>/dev/null || true

if [ -d "$WEB/override" ]; then
  (cd "$WEB/override" && find . -type f 2>/dev/null) | while read -r f; do
    rel="${f#./}"
    targets=""
    case "$rel" in
      ledpatterns/ring/*)
        targets="$QUASAR/ledpatterns/${rel#ledpatterns/ring/}"
        ;;
      ledpatterns/clock/*)
        targets="$WEB/patterns/clock/${rel#ledpatterns/clock/}"
        ;;
      sounds/*)
        targets="$QUASAR/$rel"
        ;;
      *)
        targets="$WEB/patterns/clock/$rel $QUASAR/$rel"
        ;;
    esac
    for t in $targets; do
      grep -q " $t " /proc/mounts 2>/dev/null && umount "$t" 2>/dev/null || true
    done
  done
fi

# Снимаем mount /system/etc/hosts только если он принадлежит панели и нет adbguard v2
if [ -f "$WEB/hosts" ] && [ ! -f /odm/bin/adbguard.sh ] && [ ! -f /odm/etc/hosts-ota ]; then
  if [ "$(stat -c '%d:%i' "$WEB/hosts" 2>/dev/null)" = "$(stat -c '%d:%i' /system/etc/hosts 2>/dev/null)" ] && \
     [ "$(grep -c ' /system/etc/hosts ' /proc/mounts 2>/dev/null)" = "1" ]; then
    umount /system/etc/hosts 2>/dev/null || true
  fi
fi

# Дополнительная зачистка возможных оставшихся bind-mounts панели в /data/quasar/webpanel
awk '($5 ~ /^\/data\/quasar\/webpanel(\/|$)/ || $4 ~ /(^|\/)quasar\/webpanel(\/|$)/) && $5 != "/system/etc/hosts" {print $5}' /proc/self/mountinfo 2>/dev/null | sort -r | while read -r mp; do
  umount "$mp" 2>/dev/null || umount -l "$mp" 2>/dev/null || true
done

if [ -f "$WEB/boot_sound.orig.wav" ]; then
  mkdir -p "$QUASAR/sounds/common" && mv -f "$WEB/boot_sound.orig.wav" "$QUASAR/sounds/common/boot.wav" 2>/dev/null || true
elif [ -f "$WEB/sound.orig.wav" ]; then
  mkdir -p "$QUASAR/sounds/common" && mv -f "$WEB/sound.orig.wav" "$QUASAR/sounds/common/boot.wav" 2>/dev/null || true
fi
[ -f "$WEB/bt-memes/backup/bluetooth_turning_on_jingle.opus" ] && mkdir -p "$QUASAR/sounds/common" && mv -f "$WEB/bt-memes/backup/bluetooth_turning_on_jingle.opus" "$QUASAR/sounds/common/bluetooth_turning_on_jingle.opus" 2>/dev/null || true
[ -f "$WEB/bt-memes/backup/bluetooth_turning_off_jingle.opus" ] && mkdir -p "$QUASAR/sounds/common" && mv -f "$WEB/bt-memes/backup/bluetooth_turning_off_jingle.opus" "$QUASAR/sounds/common/bluetooth_turning_off_jingle.opus" 2>/dev/null || true
[ -f "$WEB/bt-memes/backup/bluetooth_turning_on.mp3" ] && mkdir -p "$QUASAR/sounds/ru-RU" && mv -f "$WEB/bt-memes/backup/bluetooth_turning_on.mp3" "$QUASAR/sounds/ru-RU/bluetooth_turning_on.mp3" 2>/dev/null || true
[ -f "$WEB/bt-memes/backup/bluetooth_turning_off.mp3" ] && mkdir -p "$QUASAR/sounds/ru-RU" && mv -f "$WEB/bt-memes/backup/bluetooth_turning_off.mp3" "$QUASAR/sounds/ru-RU/bluetooth_turning_off.mp3" 2>/dev/null || true

rm -rf /data/yapanel /data/yapanel-state /data/quasar/webpanel /data/wakeword
if [ -f /data/local/userinit.sh ]; then
  grep -v '/data/yapanel/run.sh' /data/local/userinit.sh > /data/local/userinit.sh.tmp 2>/dev/null || true
  mv -f /data/local/userinit.sh.tmp /data/local/userinit.sh 2>/dev/null || true
  if ! grep -q '[^[:space:]#]' /data/local/userinit.sh 2>/dev/null; then
    rm -f /data/local/userinit.sh
  else
    chmod 755 /data/local/userinit.sh 2>/dev/null || true
  fi
fi
rm -f /data/local/tmp/panel_embedded.tar.gz /data/local/tmp/yapanel_max.tar.gz /data/local/tmp/gst_* /data/local/tmp/sup.out /data/local/tmp/hook_location.js /data/local/tmp/.yp_rootcmd.sh /data/local/tmp/.yp_rootd_*.sh
echo PANEL_REMOVED
'@
$out = Invoke-RootShell $remove
if ($out -notmatch "PANEL_REMOVED") {
    Show-ErrorAndExit ("Удаление не подтверждено:`n" + $out.Trim())
}
Write-Ok "Файлы панели и привязки удалены"

Write-Step 4 4 "Среда runtime (/data/python, /data/lib, /data/etc)"
if ($Full) {
    Write-Info "Снимаю bind-mounts (/lib, /system/etc) и удаляю runtime..."
    $fullCmd = @'
rm -rf /data/python
if [ -d /data/lib ]; then
  if awk '$5 == "/lib" && $4 ~ /^(\/data)?\/lib$/ {found=1} END {exit !found}' /proc/self/mountinfo 2>/dev/null; then
    umount /lib 2>/dev/null || umount -l /lib 2>/dev/null || true
  fi
  if awk '$5 == "/lib" && $4 ~ /^(\/data)?\/lib$/ {found=1} END {exit !found}' /proc/self/mountinfo 2>/dev/null; then
    echo "BUSY_LIB"
  else
    rm -rf /data/lib
  fi
fi
if [ -d /data/etc ]; then
  if awk '$5 == "/system/etc" && $4 ~ /^(\/data)?\/etc$/ {found=1} END {exit !found}' /proc/self/mountinfo 2>/dev/null; then
    umount /system/etc 2>/dev/null || umount -l /system/etc 2>/dev/null || true
  fi
  if awk '$5 == "/system/etc" && $4 ~ /^(\/data)?\/etc$/ {found=1} END {exit !found}' /proc/self/mountinfo 2>/dev/null; then
    echo "BUSY_ETC"
  else
    rm -rf /data/etc
  fi
fi
echo RUNTIME_REMOVED
'@ -replace "`r`n", "`n"
    $rtRes = Invoke-RootShell $fullCmd
    if ($rtRes -match "BUSY_LIB") {
        Write-Warn "/lib ещё занят системой, /data/lib оставлен (перезагрузите колонку и повторите с -Full)."
    }
    if ($rtRes -match "BUSY_ETC") {
        Write-Warn "/system/etc ещё занят системой, /data/etc оставлен (перезагрузите колонку и повторите с -Full)."
    }
    if ($rtRes -notmatch "RUNTIME_REMOVED") {
        Write-Warn ("Удаление runtime завершилось с предупреждением:`n" + $rtRes.Trim())
    } else {
        Write-Ok "Runtime удалён"
    }
} else {
    Write-Info "Runtime сохранён (/data/python, /data/lib, /data/etc). Для полного удаления запустите с -Full."
}

Write-Host ""
Write-Ok "Готово: панель полностью удалена со станции."
