@ECHO OFF
REM Run the full deterministic test suite with the project's virtualenv Python
REM (falls back to whatever 'python' is on PATH if there is no .venv).
SETLOCAL
IF EXIST "%~dp0.venv\Scripts\python.exe" (
    SET "PY=%~dp0.venv\Scripts\python.exe"
) ELSE (
    SET "PY=python"
)
"%PY%" "%~dp0tests\run_tests.py"
SET RC=%ERRORLEVEL%
ECHO.
IF %RC% NEQ 0 (
    ECHO Tests FAILED ^(exit %RC%^).
) ELSE (
    ECHO Tests passed.
)
PAUSE
EXIT /B %RC%
