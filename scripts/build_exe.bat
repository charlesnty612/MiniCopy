@echo off
REM ============================================================
REM MiniCopy 绿色 exe 打包脚本（PyInstaller one-folder）
REM 用法：在仓库根目录双击或在 cmd 里执行 scripts\build_exe.bat
REM 产物：dist\MiniCopy\MiniCopy.exe  +  dist\MiniCopy\_internal\
REM ============================================================
setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION
chcp 65001 >nul

REM 切到仓库根（脚本所在目录的上一级）
cd /d "%~dp0\.."

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYI=.venv\Scripts\pyinstaller.exe"

echo === [1/4] 检查 Python 与 venv ===
if not exist "%VENV_PY%" (
    echo [错误] 找不到 %VENV_PY% ，请先跑 scripts\setup.bat 建立虚拟环境
    exit /b 1
)

echo === [2/4] 安装 / 更新打包依赖（pyinstaller + pillow） ===
"%VENV_PY%" -m pip install --upgrade pyinstaller pillow || goto :fail

echo === [3/4] 生成 build\logo.ico（用 Pillow 把 web\assets\logo.png 转成多尺寸 ico） ===
if not exist "build" mkdir "build"
if not exist "web\assets\logo.png" (
    echo [错误] 找不到 web\assets\logo.png
    goto :fail
)
"%VENV_PY%" -c "from PIL import Image; img=Image.open(r'web\assets\logo.png'); img.save(r'build\logo.ico', sizes=[(256,256),(128,128),(48,48),(32,32),(16,16)])" || goto :fail
if not exist "build\logo.ico" (
    echo [错误] logo.ico 生成失败
    goto :fail
)

echo === [4/4] PyInstaller 打包（one-folder，控制台程序） ===
"%VENV_PYI%" --noconfirm --clean --name MiniCopy --console ^
  --icon "build\logo.ico" ^
  --add-data "web;web" ^
  --collect-all imageio_ffmpeg ^
  --collect-all uvicorn ^
  --hidden-import multipart ^
  --distpath "dist" --workpath "build" ^
  "src\minipic\frozen_main.py"
if errorlevel 1 goto :fail

echo.
echo === 完成 ===
echo 产物路径：dist\MiniCopy\MiniCopy.exe
echo 资源目录：dist\MiniCopy\_internal\
echo 双击 MiniCopy.exe 即可启动 Web UI（端口 7860，自动打开浏览器）。
exit /b 0

:fail
echo.
echo [失败] 打包中止，请查看上方日志。
exit /b 1