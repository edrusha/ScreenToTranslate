@echo off
rem ScreenToTranslate - console start (diagnostics)
setlocal
set "HERE=%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PY%" set "PY=python.exe"
"%PY%" "%HERE%st_app.py"
pause
