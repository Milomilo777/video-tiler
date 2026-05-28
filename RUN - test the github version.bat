@echo off
title VIDEO-TILER (GitHub version test)
:: Run from this script's own folder
cd /d "%~dp0"

:: Make yt-dlp.exe / ffmpeg.exe / ffplay.exe discoverable by reusing the ones
:: already present in your Jan 2026 folder (no need to copy 190 MB).
set "PATH=%~dp0..\SMTV Tiling - win7 & above -Jan 2026;%PATH%"

:: Use the local virtual environment created for this test
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found.
    echo It should have been created at: %~dp0.venv
    pause
    exit /b 1
)

echo Starting the GitHub video-tiler GUI...
".venv\Scripts\python.exe" "src\video-tiler.py"

echo.
echo [Info] The program window closed.
pause
