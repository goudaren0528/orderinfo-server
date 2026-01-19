@echo off
chcp 65001 >nul
echo ==========================================
echo      租帮宝 - 自动化构建脚本
echo ==========================================

echo [1/4] 清理旧文件...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/4] 打包后端 (核心监控程序)...
:: 使用 onedir 模式打包，避免反复解压导致的各种环境问题
pyinstaller --noconfirm --onedir --console --name OrderMonitor main.py

echo [3/4] 打包前端 (GUI启动器)...
pyinstaller --noconfirm --onedir --windowed --name 租帮宝_v3 launcher.py

echo [4/4] 组装运行环境...
set "DIST_DIR=dist\租帮宝_v3"

:: 复制后端程序到前端目录 (onedir 结构)
if exist "dist\OrderMonitor" (
    echo 正在部署后端程序...
    mkdir "%DIST_DIR%\backend" 2>nul
    xcopy "dist\OrderMonitor" "%DIST_DIR%\backend" /E /I /Y /Q >nul
    echo 后端程序部署成功。
) else (
    echo [错误] 未找到 dist\OrderMonitor 目录，后端打包可能失败。
)

:: 复制配置文件
if exist "config.json" (
    copy "config.json" "%DIST_DIR%\config.json" >nul
    echo 配置文件复制成功。
) else (
    if exist "config.sample.json" (
        copy "config.sample.json" "%DIST_DIR%\config.json" >nul
        echo [提示] 未找到 config.json，已使用 config.sample.json 作为默认配置。
    ) else (
        echo [警告] 根目录下未找到 config.json 或 config.sample.json。
    )
)

:: 复制浏览器环境 (关键资源)
if exist "playwright-browsers" (
    echo 正在复制浏览器环境 - 文件较多，请稍候...
    xcopy "playwright-browsers" "%DIST_DIR%\playwright-browsers" /E /I /Y /Q >nul
    echo 浏览器环境复制成功。
) else (
    echo [错误] 未找到 playwright-browsers 文件夹，程序可能无法正常运行！
)

echo.
echo ==========================================
echo      构建完成！
echo      可执行文件位于: %DIST_DIR%\租帮宝_v3.exe
echo ==========================================
