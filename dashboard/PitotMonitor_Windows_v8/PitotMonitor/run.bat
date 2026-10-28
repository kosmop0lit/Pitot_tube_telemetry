@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install -r requirements.txt
    py pitot_monitor.py
    exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
    python -m pip install -r requirements.txt
    python pitot_monitor.py
    exit /b %errorlevel%
)
echo Python не знайдено.
echo Встанови Python 3 з python.org та повтори запуск.
pause
