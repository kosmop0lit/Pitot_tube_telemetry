@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python pitot_monitor.py
pause
