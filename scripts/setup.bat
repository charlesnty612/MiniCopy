@echo off
REM ============================================================
REM MiniCopy 一键环境安装（Windows）
REM 作用：检查 Python -> 创建 .venv -> 安装依赖 -> 验证 CLI 可用
REM 用法：把项目复制到新电脑后，双击或在终端运行本脚本
REM ============================================================
setlocal
cd /d "%~dp0\.."

echo [1/4] 检查 Python ...
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python。请先安装 Python 3.10+（安装时勾选 "Add Python to PATH"）：
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 版本过低，需要 ^>= 3.10。当前版本：
    python --version
    pause
    exit /b 1
)
python --version

echo [2/4] 创建虚拟环境 .venv ...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] 创建 venv 失败。若被公司策略/杀毒拦截，请换目录或改用：
        echo         pip install --user -e .
        pause
        exit /b 1
    )
) else (
    echo     .venv 已存在，跳过创建
)

echo [3/4] 安装依赖（需要联网）...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto pip_fail
.venv\Scripts\python.exe -m pip install -e .
if errorlevel 1 goto pip_fail

echo [4/4] 验证安装 ...
.venv\Scripts\python.exe -m minipic.cli --help >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 安装后验证失败，请把上方报错截图反馈
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  安装完成！日常使用：
echo    启动网页 UI:  .venv\Scripts\minipic.exe web
echo    然后浏览器打开 http://127.0.0.1:7860
echo  首次使用请在网页右上角配置 MiniMax API Key。
echo ============================================================
pause
exit /b 0

:pip_fail
echo [ERROR] 依赖安装失败。若是网络问题：
echo   - 换网络后重跑本脚本
echo   - 或使用离线安装，见 docs\DEPLOY.md 的「离线部署」一节
pause
exit /b 1
