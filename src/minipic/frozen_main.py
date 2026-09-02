"""MiniCopy 绿色 exe 入口：启动 Web UI 并自动打开浏览器。

仅用于 PyInstaller 打包（scripts/build_exe.bat）；CLI/源码运行不受影响。
"""
from __future__ import annotations

import os
import socket
import threading
import webbrowser

HOST = "127.0.0.1"
PORT = 7860  # 与 minipic.cli.WEB_PORT 一致
URL = f"http://{HOST}:{PORT}"


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((HOST, PORT)) == 0


def _open_browser() -> None:
    if os.environ.get("MINICOPY_NO_BROWSER"):
        return
    webbrowser.open(URL)


def main() -> None:
    if _port_in_use():
        # 多半已有一个 MiniCopy 在跑：不报错，直接帮用户打开页面
        print(f"检测到 {URL} 已有 MiniCopy 在运行，直接打开浏览器。")
        _open_browser()
        return
    print(f"MiniCopy 启动中：{URL}（关闭本窗口即停止）")
    threading.Timer(1.5, _open_browser).start()
    import uvicorn
    from minipic.web import app

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()