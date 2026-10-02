@echo off
REM =====================================================
REM  pc-bridge launcher - double-click to run.
REM  Put this script in the SAME folder as
REM  pc-bridge-windows-amd64.exe, then double-click it.
REM  Closing this window stops the bridge.
REM  (Pure ASCII on purpose: avoids cmd code-page issues.)
REM =====================================================

REM ---- settings (edit if needed) ----
REM ALLOW_NODE:  Tailscale node name of the control side (edit this)
set ALLOW_NODE=your-control-node
set ALLOW_DIR=%USERPROFILE%
set ALLOW_CMD=hdc
set EXE=pc-bridge-windows-amd64.exe
REM -----------------------------------

cd /d "%~dp0"

if not exist "%EXE%" (
    echo [ERROR] %EXE% not found.
    echo Put this script and %EXE% in the same folder.
    pause
    exit /b 1
)

echo Starting pc-bridge ...
echo   allow-node: %ALLOW_NODE%
echo   allow-dir : %ALLOW_DIR%
echo   allow-cmd : %ALLOW_CMD%
echo.
"%EXE%" --allow-node "%ALLOW_NODE%" --allow-dir "%ALLOW_DIR%" --allow-cmd "%ALLOW_CMD%"
echo.
echo ------------------------------------------------------
echo Bridge exited. If there was an error above, send a screenshot.
echo ------------------------------------------------------
pause
