@echo off
chcp 65001 >nul
setlocal
title 启动 Chrome 调试模式（端口 9222）

echo.
echo  ============================================================
echo    用独立配置目录启动带远程调试端口的 Chrome
echo    用途：在已登录赛狐的浏览器里抓取 Cookie（备用方式）
echo  ============================================================
echo.

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "SELLFOX_HOME=%USERPROFILE%\SellfoxAdsData"

rem 配置目录优先放项目外（含赛狐登录态，绝不能进 Git）
if exist "%SELLFOX_HOME%\data" (
    set "PROFILE=%SELLFOX_HOME%\data\chrome-debug-profile"
) else (
    set "PROFILE=%~dp0data\chrome-debug-profile"
)

if not exist "%CHROME%" (
  echo  [错误] 没找到 Chrome，请确认安装路径：
  echo  %CHROME%
  echo.
  pause
  exit /b 1
)

start "" "%CHROME%" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir="%PROFILE%" ^
  --no-first-run ^
  --no-default-browser-check ^
  "https://www.sellfox.com"

echo  [OK] 已打开一个独立的 Chrome 窗口
echo.
echo  配置目录：%PROFILE%
echo  在这个窗口里登录赛狐后，回到工具页点「从调试端口抓取」即可。
echo.
echo  注意：这个窗口要保持开着，不要关掉。
echo        它和平时用的 Chrome 互不影响。
echo.
pause
