@echo off
title 赛狐 Chrome 调试模式启动器

echo.
echo  ============================================================
echo    正在启动「带调试端口」的 Chrome
echo  ============================================================
echo.

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE=%~dp0data\chrome-debug-profile"

if not exist "%CHROME%" (
  echo  [错误] 没找到 Chrome，请确认安装路径：
  echo  %CHROME%
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

echo  [OK] 已打开一个新的 Chrome 窗口
echo.
echo  接下来只需两步：
echo    1. 在这个新窗口里登录赛狐（第一次需要，以后会记住）
echo    2. 回到本系统网页，点【自动抓取 Cookie】
echo.
echo  注意：这个窗口请保持开着，不要关掉。
echo        你平时用的 Chrome 不受影响，可以照常用。
echo.
pause
