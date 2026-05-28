@ECHO OFF
REM Build a standalone Windows .exe of the Video Tiler with PyInstaller.
REM Run this from the repository root. Requires Python 3 on PATH.

REM 1. Install dependencies + PyInstaller
python -m pip install -r requirements.txt
python -m pip install pyinstaller

REM 2. Build (icon lives in src\img\app.ico). --windowed hides the console.
pyinstaller --clean --noconfirm --onefile --windowed ^
    --icon "src\img\app.ico" ^
    --add-data "src\img;img" ^
    --name "video-tiler" ^
    "src\video-tiler.py"

ECHO.
ECHO Done. The executable is in the "dist" folder.
ECHO Remember to place yt-dlp.exe, ffmpeg.exe and ffplay.exe on PATH or next to it.
PAUSE
