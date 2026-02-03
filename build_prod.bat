@echo off
chcp 65001 >nul
echo ==========================================
echo      ZuBangBao - Production Build Script
echo ==========================================

echo [INFO] Switching to PRODUCTION environment...
if exist .env.prod (
    copy /Y .env.prod .env >nul
    echo [OK] Loaded .env.prod
) else (
    echo [ERROR] .env.prod not found!
    pause
    exit /b 1
)

echo.
echo [IMPORTANT] Please ensure you have updated LICENSE_SERVER_URL in .env.prod with your actual production domain!
echo Current configuration:
findstr "LICENSE_SERVER_URL" .env
echo.
timeout /t 5

echo [1/4] Cleaning old files...
set "BUILD_OUTPUT=dist_prod"
if exist build rmdir /s /q build
if exist "%BUILD_OUTPUT%" rmdir /s /q "%BUILD_OUTPUT%"

echo [2/4] Building Backend (OrderMonitor)...
pyinstaller --noconfirm --log-level WARN --distpath "%BUILD_OUTPUT%" --onedir --console --name OrderMonitor --add-data ".env;." main.py
if %errorlevel% neq 0 (
    echo [ERROR] Backend build failed!
    exit /b %errorlevel%
)

echo [3/4] Building Frontend (Launcher)...
pyinstaller --noconfirm --log-level WARN --distpath "%BUILD_OUTPUT%" --onedir --windowed --name 租帮宝_v3 --add-data ".env;." --add-data "README.md;." --hidden-import=pystray launcher.py
if %errorlevel% neq 0 (
    echo [ERROR] Frontend build failed!
    exit /b %errorlevel%
)

echo [4/4] Assembling Environment...
set "DIST_DIR=%BUILD_OUTPUT%\租帮宝_v3"

if exist "%BUILD_OUTPUT%\OrderMonitor" (
    echo Deploying backend...
    mkdir "%DIST_DIR%\backend" 2>nul
    xcopy "%BUILD_OUTPUT%\OrderMonitor" "%DIST_DIR%\backend" /E /I /Y /Q >nul
    echo Backend deployed.
) else (
    echo [ERROR] Backend not found.
)

if exist "README.md" (
    copy "README.md" "%DIST_DIR%\README.md" >nul
    echo README.md copied.
)

if exist "playwright-browsers" (
    echo Copying browser environment...
    xcopy "playwright-browsers" "%DIST_DIR%\playwright-browsers" /E /I /Y /Q >nul
    echo Browser environment copied.
) else (
    echo [ERROR] playwright-browsers not found!
)

echo.
echo ==========================================
echo      Production Build Complete!
echo      Output: %DIST_DIR%\租帮宝_v3.exe
echo ==========================================
pause
