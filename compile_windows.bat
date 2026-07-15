@ECHO OFF
REM Build a standalone Windows .exe of the Video Tiler with PyInstaller.
REM Run this from the repository root. Requires Python 3 on PATH.

REM 1. Install dependencies + PyInstaller
python -m pip install -r requirements.txt
python -m pip install pyinstaller

REM 2. Generate the Windows version resource from VERSION (single source of
REM    truth). A version-stamped exe is far less likely to be blocked by
REM    SmartScreen/AV than a blank one.
python make_version_info.py

REM 3. Build (icon lives in src\img\app.ico). --windowed hides the console.
pyinstaller --clean --noconfirm --onefile --windowed ^
    --icon "src\img\app.ico" ^
    --add-data "src\img;img" ^
    --add-data "VERSION;." ^
    --version-file "version_info.txt" ^
    --name "video-tiler" ^
    "src\video-tiler.py"

REM 4. Copy the offline fallback video next to the exe (same sibling-file
REM    convention as the yt-dlp/ffmpeg/ffplay binaries below).
if exist "assets\offline.mp4" copy /Y "assets\offline.mp4" "dist\offline.mp4" >nul

ECHO.
ECHO Done. The executable is in the "dist" folder.
ECHO Remember to place yt-dlp.exe, ffmpeg.exe and ffplay.exe on PATH or next to it.
PAUSE
