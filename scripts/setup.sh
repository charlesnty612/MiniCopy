#!/usr/bin/env bash
# ============================================================
# MiniCopy 一键环境安装（Linux / macOS）
# 作用：检查 Python -> 创建 .venv -> 安装依赖 -> 验证 CLI 可用
# 用法：把项目复制到新电脑后运行  bash scripts/setup.sh
# ============================================================
set -e
cd "$(dirname "$0")/.."

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

echo "[1/4] 检查 Python ..."
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[ERROR] 未找到 python3。请先安装 Python 3.10+："
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv"
    echo "  macOS:         brew install python@3.12"
    exit 1
fi
"$PY" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" || {
    echo "[ERROR] Python 版本过低，需要 >= 3.10。当前：$("$PY" --version)"
    exit 1
}
"$PY" --version

echo "[2/4] 创建虚拟环境 .venv ..."
if [ ! -d .venv ]; then
    "$PY" -m venv .venv || {
        echo "[ERROR] 创建 venv 失败。Debian/Ubuntu 请先 sudo apt install python3-venv"
        exit 1
    }
else
    echo "    .venv 已存在，跳过创建"
fi

echo "[3/4] 安装依赖（需要联网）..."
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e . || {
    echo "[ERROR] 依赖安装失败。若是网络问题，见 docs/DEPLOY.md 的「离线部署」一节"
    exit 1
}

echo "[4/4] 验证安装 ..."
./.venv/bin/python -m minipic.cli --help >/dev/null

echo ""
echo "============================================================"
echo " 安装完成！日常使用："
echo "   启动网页 UI:  ./.venv/bin/minipic web"
echo "   然后浏览器打开 http://127.0.0.1:7860"
echo " 首次使用请在网页右上角配置 MiniMax API Key。"
echo "============================================================"
