@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ===========================================================================
rem  数据外置模式启动
rem  项目目录只放代码（可安全上传 GitHub），
rem  数据库 / 赛狐登录态 / 词库 全部放在 %USERPROFILE%\SellfoxAdsData
rem ===========================================================================
set "SELLFOX_HOME=%USERPROFILE%\SellfoxAdsData"

echo ============================================================
echo   Amazon SP Bulk Ads Generator  -  数据外置模式
echo ------------------------------------------------------------
echo   代码目录: %CD%
echo   数据目录: %SELLFOX_HOME%
echo ============================================================
echo.

if not exist "%SELLFOX_HOME%\data" mkdir "%SELLFOX_HOME%\data" 2>nul
if not exist "%SELLFOX_HOME%\config" mkdir "%SELLFOX_HOME%\config" 2>nul

if not exist "%SELLFOX_HOME%\data\local_store.db" (
    echo [提示] 外部数据目录里还没有数据库。
    echo        若要迁移现有数据，先执行：
    echo          venv\Scripts\python.exe tools\migrate_to_external.py --move
    echo        直接启动则会新建一个空数据库。
    echo.
)

if not exist "venv\Scripts\python.exe" (
    echo [错误] 没找到虚拟环境 venv\。
    echo        请先双击 start.bat 完成首次安装。
    echo.
    pause
    exit /b 1
)

rem ---------- 结束上一次遗留的服务 ----------
set "PORT=8000"
set "OLDPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set "OLDPID=%%p"
if defined OLDPID (
    echo [提示] 检测到旧服务 PID=%OLDPID%，正在关闭...
    taskkill /PID %OLDPID% /F >nul 2>&1
    timeout /t 2 /nobreak >nul
    echo [OK] 旧服务已关闭
    echo.
)

echo 正在启动，浏览器会自动打开： http://127.0.0.1:8000
echo.
echo   * 关闭本窗口 = 停止服务
echo   * 数据写在 %SELLFOX_HOME%，不在项目目录里
echo.
"venv\Scripts\python.exe" main.py

echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
