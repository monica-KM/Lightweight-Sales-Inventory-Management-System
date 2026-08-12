@echo off
cd /d "%~dp0"
start "" pythonw "%~dp0stop_server.py"
exit
