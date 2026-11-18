@echo off
rem Install ScreenToTranslate dependencies into the current user's profile.
setlocal
cd /d "%~dp0"

rem Prefer the Python 3.10 launcher, fall back to python from PATH.
py -3.10 -c "import sys" >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3.10")

echo Installing PySide6, Pillow and winsdk into the user profile...
echo This needs internet access and about 250 MB of disk space.
echo.
%PY% -m pip install --user --no-input --disable-pip-version-check --no-warn-script-location --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed

echo.
echo Done. You can start the program with run.cmd.
pause
exit /b 0

:failed
echo.
echo Installation failed. Check the internet connection and Python 3.10-3.12 (64-bit).
echo Manual command:
echo   python -m pip install --user -r requirements.txt
pause
exit /b 1
