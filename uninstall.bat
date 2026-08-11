@echo off
REM Remove the Windows Startup entry for Javis.
set "VBS_FILE=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Javis.vbs"
if exist "%VBS_FILE%" (
    del "%VBS_FILE%"
    echo Removed: %VBS_FILE%
) else (
    echo No startup entry found - nothing to remove.
)
echo.
echo Your files in %~dp0 are untouched. Delete this folder to fully remove Javis.
pause
