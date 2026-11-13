@echo off
setlocal
chcp 65001 >nul
title Удаление YapAnel со Станции

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_to_max.ps1" %*
set ERR=%ERRORLEVEL%
if %ERR% neq 0 (
    echo.
    echo ----------------------------------------------------------------------
    echo  [ОШИБКА] Удаление не завершено. Смотрите сообщение выше.
    echo ----------------------------------------------------------------------
    pause
)
exit /b %ERR%
