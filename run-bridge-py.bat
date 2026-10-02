@echo off
REM =====================================================
REM  pc-bridge.py launcher - double-click to run.
REM  Put this script in the SAME folder as pc-bridge.py,
REM  then double-click it. Needs Python 3 installed.
REM  Closing this window stops the bridge.
REM  (Pure ASCII on purpose: avoids cmd code-page issues.)
REM =====================================================

REM ---- settings (edit if needed) ----
REM ALLOW_READ:  dirs the bridge can READ (comma-separated)
REM ALLOW_WRITE: dirs the bridge can WRITE (comma-separated, empty = no write)
REM ALLOW_NODE:  Tailscale node name of the control side (edit this)
set ALLOW_NODE=your-control-node
set ALLOW_READ=%USERPROFILE%
set ALLOW_WRITE=
set ALLOW_CMD=hdc
REM -----------------------------------

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found on PATH.
    echo Install Python 3 from https://www.python.org/downloads/
    echo Remember to tick Add python.exe to PATH during install.
    pause
    exit /b 1
)

if not exist "pc-bridge.py" (
    echo [ERROR] pc-bridge.py not found.
    echo Put this script and pc-bridge.py in the same folder.
    pause
    exit /b 1
)

echo Starting pc-bridge.py ...
echo   allow-node : %ALLOW_NODE%
echo   allow-read : %ALLOW_READ%
echo   allow-write: %ALLOW_WRITE%
echo   allow-cmd  : %ALLOW_CMD%
echo.
python pc-bridge.py --allow-node "%ALLOW_NODE%" --allow-dir "%ALLOW_READ%" --allow-write-dir "%ALLOW_WRITE%" --allow-cmd "%ALLOW_CMD%"
echo.
echo ------------------------------------------------------
echo Bridge exited. If there was an error above, send a screenshot.
echo ------------------------------------------------------
pause
