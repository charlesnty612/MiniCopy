"""MiniCopy 绿色 exe 入口：启动 Web UI 并自动打开浏览器。

仅用于 PyInstaller 打包（scripts/build_exe.bat）；CLI/源码运行不受影响。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

HOST = "127.0.0.1"
PORT = 7860  # 与 minipic.cli.WEB_PORT 一致
URL = f"http://{HOST}:{PORT}"

# 接管旧实例的轮询上限与间隔（秒）
_PORT_RELEASE_TIMEOUT = 5.0
_PORT_RELEASE_POLL_INTERVAL = 0.3


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((HOST, PORT)) == 0


def _port_owner_pid(port: int) -> int | None:
    """Windows 专用：解析 ``netstat -ano``，返回 LISTENING 占用该端口的 PID。

    任何解析失败 / 进程已退出 / 非 Windows 均返回 ``None``。
    """
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, errors="replace"
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    needle = f":{port}"
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        # netstat 列：协议 本地地址 外部地址 状态 PID
        if len(parts) < 5:
            continue
        local_addr, state, pid_str = parts[1], parts[3], parts[4]
        if not local_addr.endswith(needle):
            continue
        if state != "LISTENING":
            continue
        try:
            return int(pid_str)
        except ValueError:
            return None
    return None


def _process_image_name(pid: int) -> str:
    """Windows 专用：用 ``tasklist`` 查 PID 对应的映像名（不含 .exe 也返回原样）。

    异常 / 进程不存在 / 非 Windows 均返回 ``""``。
    """
    if os.name != "nt":
        return ""
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (proc.stdout or "").strip()
    if not out or out.startswith("INFO:"):
        # INFO: No tasks are running which match the specified criteria.
        return ""
    first = out.splitlines()[0]
    name = first.split(",", 1)[0].strip().strip('"')
    return name


def _wait_for_port_release(timeout: float) -> bool:
    """轮询端口直到释放或超时，返回是否成功释放。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_in_use():
            return True
        time.sleep(_PORT_RELEASE_POLL_INTERVAL)
    return not _port_in_use()


def _open_browser() -> None:
    if os.environ.get("MINICOPY_NO_BROWSER"):
        return
    webbrowser.open(URL)


def _maybe_takeover_existing_instance() -> bool:
    """若 7860 被旧版 MiniCopy.exe 占用，taskkill + 等待端口释放后返回 True。

    返回：
    - True：端口已释放（占用者已结束 或 本来就是空的），调用方可正常启动。
    - False：占用者不是 MiniCopy.exe / 解析失败 / 用户应手动处理，调用方应退出。
    """
    if not _port_in_use():
        return True  # 本来就空，交给主流程
    # 仅在 Windows 上尝试接管；其它平台维持旧兜底行为（提示 + 打开浏览器）。
    if os.name != "nt":
        print(f"检测到 {URL} 已有 MiniCopy 在运行，直接打开浏览器。")
        _open_browser()
        return True  # 非 Windows：不退出，保留旧兜底

    pid = _port_owner_pid(PORT)
    image = _process_image_name(pid) if pid else ""
    # 注意：映像名比对前必须确认已拿到 PID 与名称，避免误杀。
    if pid and image and image.lower() == "minicopy.exe":
        print(f"检测到旧版 MiniCopy 正在运行（PID {pid}），正在关闭并启动新版本…")
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if _wait_for_port_release(_PORT_RELEASE_TIMEOUT):
            print("旧实例已结束，准备启动新版本。")
            return True
        # 超时仍未释放：退回旧兜底（提示 + 打开浏览器），不强制退出。
        print(
            f"关闭旧实例超时（>{_PORT_RELEASE_TIMEOUT:.0f}s），"
            f"请手动结束 PID {pid} 后重试。"
        )
        _open_browser()
        return True

    if pid and image:
        # 占用者不是 MiniCopy.exe：明确报错退出，避免打开别人的应用。
        print(
            f"端口 {PORT} 被其它程序占用（PID {pid}，名称 {image}），"
            f"请关闭该程序后重试。"
        )
        sys.exit(1)

    # PID / 映像名拿不到：维持旧兜底（提示 + 打开浏览器），不擅作主张。
    print(f"检测到 {URL} 已有 MiniCopy 在运行，直接打开浏览器。")
    _open_browser()
    return True


def main() -> None:
    if not _maybe_takeover_existing_instance():
        return
    if _port_in_use():
        # 接管流程中端口仍未释放（理论不该到这），保留兜底。
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