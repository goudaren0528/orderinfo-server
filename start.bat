@echo off
cd /d %~dp0
echo =======================================================
echo     Starting Order Notification Service
echo =======================================================
echo.

:: Check if virtual environment exists
if exist venv\Scripts\activate.bat (
    echo Activating virtual environment...
    call venv\Scripts\activate.bat
)

echo [INFO] Auto-switching to Development Config...
copy /Y .env.dev .env >nul

echo Starting main script...
python main.py

if %errorlevel% neq 0 (
    echo.
    echo Script exited with error code %errorlevel%.
    pause
)
pause
