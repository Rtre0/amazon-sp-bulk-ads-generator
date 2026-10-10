@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo    Amazon SP Bulk Ads Generator  -  Local Launcher
echo    亚马逊 SP 广告批量表生成工具  -  本地一键启动
echo ============================================================
echo.

rem ---------- 首次运行：自动准备虚拟环境 ----------
if not exist "venv\Scripts\python.exe" (
    echo [1/2] 首次运行，正在创建虚拟环境...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo [错误] 创建虚拟环境失败。
        echo        请先安装 Python 3.10+ 并勾选 "Add Python to PATH"。
        echo.
        pause
        exit /b 1
    )
    echo [2/2] 正在安装依赖（首次约 1-2 分钟，请耐心等待）...
    "venv\Scripts\python.exe" -m pip install --upgrade pip
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请检查网络后重新双击本文件。
        echo.
        pause
        exit /b 1
    )
    echo.
    echo 环境准备完成！
    echo.
)

rem ---------- 启动服务 ----------
echo 正在启动，浏览器会自动打开： http://127.0.0.1:8000
echo.
echo   * 关闭本窗口 = 停止服务
echo   * 若浏览器没自动弹出，手动访问上面的地址即可
echo.
"venv\Scripts\python.exe" main.py

echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
