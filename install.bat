@echo off
REM ============================================================
REM  Project Javis - Installer
REM  Installs Python dependencies and registers Javis to
REM  auto-start with Windows (per-user, no admin needed).
REM ============================================================

setlocal
cd /d "%~dp0"

echo.
echo  ============================================
echo   Installing Project Javis ...
echo  ============================================
echo.

REM ---- 1. Upgrade pip ----
echo [1/4] Upgrading pip...
python -m pip install --upgrade pip >nul

REM ---- 2. Install Python deps ----
echo [2/4] Installing Python packages...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install Python packages.
    echo Check your internet connection and try again.
    pause
    exit /b 1
)

REM ---- 3. Add a Startup shortcut ----
echo [3/4] Adding to Windows Startup...
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_FILE=%STARTUP_DIR%\Javis.vbs"
set "RUN_BAT=%~dp0run_silent.bat"

REM Create a VBS wrapper that launches the bat file silently (no cmd window)
> "%VBS_FILE%" echo CreateObject("WScript.Shell").Run Chr(34) ^& "%RUN_BAT%" ^& Chr(34), 0, False

if exist "%VBS_FILE%" (
    echo     Startup entry created: %VBS_FILE%
) else (
    echo [WARN] Could not create startup entry. You can still run run.bat manually.
)

REM ---- 4. Create a silent-run helper (used by the startup entry) ----
echo [4/4] Creating silent launcher...
> "%~dp0run_silent.bat" echo @echo off
>> "%~dp0run_silent.bat" echo cd /d "%~dp0"
>> "%~dp0run_silent.bat" echo start "" pythonw.exe "%~dp0app_ui.py"

echo.
echo  ============================================
echo   Installation complete!
echo  ============================================
echo.
echo   - To run NOW  : double-click  run.bat
echo   - To uninstall: run  uninstall.bat
echo   - Startup entry: %VBS_FILE%
echo.
echo   Javis will greet you the next time you log in.
echo.
pause
endlocal
