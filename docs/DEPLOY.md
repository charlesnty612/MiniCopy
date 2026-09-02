# MiniCopy 部署 / 迁移指南

把项目复制到任何一台新电脑后，按本文档操作即可使用。

## 新机器要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 唯一硬性要求；Windows 安装时勾选 "Add Python to PATH" |
| 网络 | 首次安装时需要 | 用于 pip 下载依赖；无外网见「离线部署」 |
| ffmpeg | **不需要安装** | `imageio-ffmpeg` 依赖自带静态二进制，自动兜底 |
| Node.js / mmx | 不需要 | 那是开发期生成 UI 素材的工具，运行时无关 |
| 浏览器 | 任意现代浏览器 | 网页 UI 用 |

## 快速开始（三步）

## 方案 A：免安装绿色包（小白推荐）

适合只想用、不想折腾 Python / venv / ffmpeg 的最终用户。

- 下载 `MiniCopy-win64.zip`（或 `dist/MiniCopy/` 目录），解压到任意位置
- 双击 `MiniCopy.exe` 启动；首次运行 Windows 防火墙弹窗请点"允许访问"
- 浏览器会自动打开 http://127.0.0.1:7860 ；右上角配置 API Key
- 用完直接关掉黑窗口即停止（不要强行结束进程，可能丢失未写完的日志）
- **仅支持 Windows 64 位**；Linux / macOS 用户需在对应平台上重新运行 `scripts/build_exe.sh`

## 方案 B：源码部署（开发者）

```bash
# 1. 复制项目目录到新电脑（排除清单见下节）

# 2. 运行一键安装脚本
#    Windows:        scripts\setup.bat
#    Linux / macOS:  bash scripts/setup.sh

# 3. 启动网页 UI
#    Windows:        .venv\Scripts\minipic.exe web
#    Linux / macOS:  ./.venv/bin/minipic web
#    浏览器打开 http://127.0.0.1:7860 ，右上角配置 API Key
```

脚本做的事：检查 Python 版本 → 创建 `.venv` → `pip install -e .` → 验证 CLI 可用。

## 复制项目时排除这些（不要拷）

- `.venv/` — 虚拟环境**平台相关**，到新机器必须重建（setup 脚本自动建）
- `videos/` — 已下载的成片，体积大；需要历史成片可单独拷
- `__pycache__/`、`*.egg-info/`、`.pytest_cache/` — 缓存垃圾
- `minimax-output/`、`docs/ui-before`、`docs/ui-after` — 开发期产物（可选）

用 git 分发最干净：`git clone` 或 `git archive` 出来的树天然不含上述内容。

## 配置与数据在哪（不在项目目录里）

MiniCopy 的配置和任务历史存放在**每台机器各自的用户目录**，复制项目目录不会带过去：

| 内容 | Windows | Linux / macOS |
|---|---|---|
| API Key 等配置 | `%LOCALAPPDATA%\minipic\config.json` | `~/.config/minipic/config.json` |
| 任务历史 / 上传缓存 | `%LOCALAPPDATA%\minipic\`（tasks.db、uploads/） | `~/.local/share/minipic/` |

- **只想在新机器用起来**：不用迁移任何东西，网页里重新配一次 API Key 即可。
- **想连任务历史一起搬**：把旧机器的上面两个目录拷到新机器对应位置。

## 离线部署（目标机器无外网）

在**有网的同平台机器**上先下载依赖包，随项目一起拷贝：

```bash
# 有网机器上执行（在项目根目录）
pip download -e . -d vendor/

# 离线机器上安装（setup 脚本改用这步替代联网 pip install）
pip install --no-index --find-links vendor -e .
```

注意：`imageio-ffmpeg` 的二进制按平台分发，Windows 和 Linux 的 wheel 不通用——离线包要在**与目标机相同操作系统**的机器上准备。

## 常见问题

- **`python` 不是命令**：Windows 装 Python 时没勾 PATH；重装勾选，或用 `py` 命令替代。
- **端口 7860 被占**：说明已有一个 MiniCopy 在跑，直接打开 http://127.0.0.1:7860 即可；或先结束旧进程。
- **公司代理导致 pip 失败**：`pip config set global.proxy http://代理地址:端口` 后重跑 setup。
- **杀毒软件拦 venv/脚本**：换非系统盘目录，或将项目目录加入杀清白名单。
- **想全局直接用 `minipic` 命令而不用 venv**：`pip install --user -e .`（不推荐，依赖易冲突）。

## 相关文档

- 安装与日常用法：[../README.md](../README.md)
- 架构与业务逻辑：[ARCHITECTURE.md](ARCHITECTURE.md)
