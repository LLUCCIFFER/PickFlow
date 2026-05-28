@echo off
REM 片刻 · Windows 启动器
REM
REM 双击运行：自动装 Python + 依赖 + 检查更新 + 启动应用 + 自动开浏览器
REM 没装过 Python 也没关系，会用 uv 自动下载一个独立的 Python。
REM
REM 第一次双击可能弹出「Windows 已保护你的电脑」蓝色窗口：
REM   点「更多信息」→ 「仍要运行」即可。之后不再弹。

setlocal enableextensions enabledelayedexpansion
chcp 65001 >nul 2>&1

cd /d "%~dp0"

echo.
echo ============================================================
echo   片刻 . 启动器
echo ============================================================

REM ---- 1. 找 / 装 uv ----
set "UV="
where uv >nul 2>&1 && set "UV=uv"

if not defined UV (
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
)
if not defined UV (
  if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"
)

if not defined UV (
  echo.
  echo [首次准备] 正在下载 uv（Python 工具链，约 30MB）...
  echo   这一步只在第一次运行做，之后秒过。
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  if errorlevel 1 (
    echo.
    echo [错误] uv 安装失败。
    echo        常见原因：网络不通畅（astral.sh 走海外 CDN），请稍后重试。
    echo        或手动执行：powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    pause
    exit /b 1
  )
  REM 重新探测
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
  if not defined UV (
    if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"
  )
  echo   [√] uv 安装完成
)

if not defined UV (
  echo.
  echo [错误] 安装完 uv 后仍找不到可执行文件。请关闭本窗口重新双击一次。
  echo.
  pause
  exit /b 1
)

set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"

REM ---- 2. 用 uv 跑 launcher.py ----
REM 不加 --quiet：让 uv 下载 Python 的进度直接给用户看
echo.
echo 正在准备 Python 环境并启动 launcher...
"%UV%" run --no-project --python ">=3.10" -- python scripts\launcher.py
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo.
  echo [启动器以非零状态退出: %RC%]
  pause
)
endlocal & exit /b %RC%
