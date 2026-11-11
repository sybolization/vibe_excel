@echo off
setlocal
rem Double-click entry for start.ps1: bypasses the script execution policy.
rem All options are forwarded, e.g.  start.cmd -Full
rem Set VIBE_NO_PAUSE=1 to skip the final "press any key" (used by automation).

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo Failed with exit code %CODE%. The reason is printed above.
    if defined VIBE_NO_PAUSE exit /b %CODE%
    pause >nul
    exit /b %CODE%
)

if defined VIBE_NO_PAUSE exit /b 0
echo.
echo Done. Press any key to close this window.
pause >nul
exit /b 0
