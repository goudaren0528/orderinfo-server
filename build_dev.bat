@echo off
chcp 65001 >nul
echo ==========================================
echo      ZuBangBao - Build Script (DEV)
echo ==========================================

echo [0/4] Preparing Environment (DEV)...
copy /Y .env.dev .env >nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to copy .env.dev to .env
    exit /b %errorlevel%
)
echo Environment set to DEVELOPMENT.

echo [1/4] Cleaning old files...
set "BUILD_OUTPUT=dist_dev"
if exist build rmdir /s /q build
if exist "%BUILD_OUTPUT%" rmdir /s /q "%BUILD_OUTPUT%"

echo [2/4] Building Backend (OrderMonitor)...
pyinstaller --noconfirm --log-level WARN --distpath "%BUILD_OUTPUT%" --onedir --console --name OrderMonitor --add-data ".env;." main.py
if %errorlevel% neq 0 (
    echo [ERROR] Backend build failed!
    exit /b %errorlevel%
)

echo [3/4] Building Frontend (Launcher)...
pyinstaller --noconfirm --log-level WARN --distpath "%BUILD_OUTPUT%" --onedir --windowed --name 租帮宝_v3 --icon "logo.ico" --add-data ".env;." --add-data "README.md;." --add-data "logo.ico;." --hidden-import=pystray launcher.py
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

echo Generating client-side .env...
findstr /B "LICENSE_SERVER_URL WEB_SERVER_HOST_IP" .env > "%DIST_DIR%\.env"
echo .env config generated for client (stripped sensitive secrets).

if exist "%DIST_DIR%\_internal\.env" (
    del "%DIST_DIR%\_internal\.env"
    echo Removed sensitive .env from _internal directory.
)

if exist "%DIST_DIR%\backend" (
    copy "%DIST_DIR%\.env" "%DIST_DIR%\backend\.env" >nul
    echo Copied sanitized .env to backend.
    
    if exist "%DIST_DIR%\backend\_internal\.env" (
        del "%DIST_DIR%\backend\_internal\.env"
        echo Removed sensitive .env from backend\_internal directory.
    )
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
echo      DEV Build Complete!
echo      Output: %DIST_DIR%\租帮宝_v3.exe
echo ==========================================
REM pause
