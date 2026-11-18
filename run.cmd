@echo off
rem ScreenToTranslate - silent start (tray + hotkey)
setlocal
set "HERE=%~dp0"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
start "" "%PYW%" "%HERE%st_app.py"
