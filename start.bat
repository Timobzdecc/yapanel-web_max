@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title ЯПанель — Станция Макс
rem Запуск панели на Windows: двойной щелчок,  start.bat [IP колонки]  или  start.bat --find  (найти колонку в сети)
rem Чего нет в системе (Python, adb, ffmpeg) — по согласию скачивается в папку runtime рядом с панелью.
rem В систему ничего не ставится, права администратора не нужны. Удалить — просто удалить папку.

set "RT=%~dp0runtime"
set "PYVER=3.14.8"
set "ARCH=amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=arm64"
where curl >nul 2>nul || goto oldwin
where tar >nul 2>nul || goto oldwin

rem ---------- Python ----------
set "PYEXE="
if exist "%RT%\python\python.exe" set "PYEXE=%RT%\python\python.exe"
if not defined PYEXE for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if defined PYEXE "%PYEXE%" -c "import sys;sys.exit(0 if sys.version_info>=(3,14) else 1)" >nul 2>nul || set "PYEXE="
if defined PYEXE goto adb
echo.
echo Python 3.14+ не найден (в нём есть zstd — нужен для заставки и анимаций часов).
echo Можно скачать портативный Python %PYVER% с python.org (около 11 МБ) в папку runtime.
choice /c YN /m "Скачать"
if errorlevel 2 goto nopy
if not exist "%RT%" mkdir "%RT%"
curl -fL --progress-bar -o "%RT%\python.zip" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-%ARCH%.zip" || goto dlfail
if not exist "%RT%\python" mkdir "%RT%\python"
tar -xf "%RT%\python.zip" -C "%RT%\python" || goto dlfail
del "%RT%\python.zip"
rem портативный Python не видит папку скрипта — добавляем корень панели в ._pth
for %%f in ("%RT%\python\python3*._pth") do echo ..\..>>"%%f"
set "PYEXE=%RT%\python\python.exe"

:adb
rem ---------- adb ----------
if defined ADB if exist "%ADB%" goto ffmpeg
set "ADB="
if exist "%~dp0platform-tools\adb.exe" set "ADB=%~dp0platform-tools\adb.exe"
if not defined ADB if exist "%RT%\platform-tools\adb.exe" set "ADB=%RT%\platform-tools\adb.exe"
if not defined ADB for /f "delims=" %%i in ('where adb 2^>nul') do if not defined ADB set "ADB=%%i"
if not defined ADB goto adbask
rem старый adb (например, 1.0.31) не понимает pull -a и forward tcp:0 — предлагаем скачать свежий
"%PYEXE%" -c "import re,subprocess,sys;o=subprocess.run([sys.argv[1],'version'],capture_output=True,text=True).stdout;m=re.search(r'version (\d+)\.(\d+)\.(\d+)',o);sys.exit(m is not None and tuple(map(int,m.groups()))<(1,0,39))" "%ADB%"
if errorlevel 1 echo. & echo Найден слишком старый adb: %ADB% & echo Панели нужен Android platform-tools 28 или новее. & set "ADB="
if defined ADB goto ffmpeg
:adbask
echo.
echo adb не найден. Можно скачать Android platform-tools с сайта Google (около 7 МБ) в папку runtime.
echo Условия Google: https://developer.android.com/tools/releases/platform-tools
choice /c YN /m "Скачать"
if errorlevel 2 goto noadb
if not exist "%RT%" mkdir "%RT%"
curl -fL --progress-bar -o "%RT%\platform-tools.zip" "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" || goto dlfail
tar -xf "%RT%\platform-tools.zip" -C "%RT%" || goto dlfail
del "%RT%\platform-tools.zip"
set "ADB=%RT%\platform-tools\adb.exe"

:ffmpeg
rem ---------- ffmpeg (по желанию: mp3/ogg и свои звуки) ----------
if exist "%RT%\ffmpeg\bin\ffmpeg.exe" set "PATH=%RT%\ffmpeg\bin;%PATH%"
where ffmpeg >nul 2>nul && goto run
if exist "%RT%\no-ffmpeg" goto run
echo.
echo ffmpeg не найден. Без него колонка играет только WAV и нельзя ставить свои звуки.
echo Можно скачать сборку ffmpeg с gyan.dev (около 110 МБ) в папку runtime.
choice /c YN /m "Скачать (N — больше не спрашивать)"
if errorlevel 2 (
  if not exist "%RT%" mkdir "%RT%"
  echo.>"%RT%\no-ffmpeg"
  goto run
)
if not exist "%RT%" mkdir "%RT%"
curl -fL --progress-bar -o "%RT%\ffmpeg.zip" "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || goto dlfail
if exist "%RT%\ffmpeg-tmp" rmdir /s /q "%RT%\ffmpeg-tmp"
mkdir "%RT%\ffmpeg-tmp"
tar -xf "%RT%\ffmpeg.zip" -C "%RT%\ffmpeg-tmp" || goto dlfail
for /d %%d in ("%RT%\ffmpeg-tmp\ffmpeg-*") do move "%%d" "%RT%\ffmpeg" >nul
rmdir /s /q "%RT%\ffmpeg-tmp"
del "%RT%\ffmpeg.zip"
set "PATH=%RT%\ffmpeg\bin;%PATH%"

:run
echo.
set "ARG1=%~1"
if "%~1"=="" (
  "%PYEXE%" server.py --open
) else if "%ARG1:~0,1%"=="-" (
  "%PYEXE%" server.py --open %*
) else (
  "%PYEXE%" server.py --open --device %*
)
echo.
pause
exit /b

:nopy
echo Установите Python с https://www.python.org/downloads/ (отметьте «Add python.exe to PATH») и запустите снова.
pause
exit /b 1

:noadb
echo Скачайте platform-tools: https://developer.android.com/tools/releases/platform-tools
echo и положите папку platform-tools рядом с server.py.
pause
exit /b 1

:dlfail
echo.
echo Не удалось скачать или распаковать. Проверьте интернет и запустите start.bat снова.
pause
exit /b 1

:oldwin
echo Нужна Windows 10 (1803) или новее: в ней есть curl и tar.
echo Или установите Python и platform-tools вручную — см. README.
pause
exit /b 1
