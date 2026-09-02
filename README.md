# MiniCopy（阿米巴破局者 · 短视频复刻工具）

本地 CLI + 极简网页 UI，把 MiniMax H3 视频生成（文生视频 / 图生视频 / 多模态参考）包成日常能用的工具。

- **后端**：MiniMax 公开 API（`https://api.minimaxi.com`，需自备 API Key）
- **模型**：`MiniMax-H3` / `MiniMax-H3-Max`（V2 端点）
- **支持模式**：
  - `t2v`：纯文本 → 视频
  - `i2v`：首帧图（+ 可选尾帧图） + 文本 → 视频
  - `r2v`：文本 + 任意组合的参考图/参考视频/参考音频（多模态参考）

## 模型能力

| 模型 | 模式 | i2v 帧 | 分辨率 | 时长 |
|------|------|--------|--------|------|
| `MiniMax-H3` | t2v / i2v / r2v | 首帧 / 尾帧 | 768P / 2K | 4–15s |
| `MiniMax-H3-Max` | t2v / i2v | 首帧 / 尾帧 | 480P / 768P | 5–15s |

- H3-Max **不支持多模态参考（r2v）**，也不支持中间帧（官方 API schema 的 role 枚举只有 `first_frame` / `last_frame` / `reference_*`，没有 `middle_frame`）。
- 参考视频 / 音频仅支持 **≤15s 单段**（多段生成已移除，>15s 会报错提示先截取）。

## 安装

> 复制到新电脑？直接跑一键脚本 `scripts\setup.bat`（Windows）/ `bash scripts/setup.sh`（Linux/macOS），详见 [docs/DEPLOY.md](docs/DEPLOY.md)。

```bash
# 1. 进入项目目录（换成你解压/克隆后的实际路径）
cd minipic

# 2. 创建 venv 并装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

# 3. 配置 API Key（任选一种）
#    a) 网页 UI 里点右上角 "API Key" 保存（推荐，CLI 会自动共用）
#    b) 写到用户配置目录（CLI/UI 共用）
minipic config set api_key eyJhbGciOi...
#    c) 环境变量（仅兜底，UI 保存后以用户配置为准）
$env:MINIMAX_API_KEY="eyJhbGciOi..."
```

> API key 优先级：**用户配置（`~/.config/minipic/config.json`）> 项目目录 `config.json` > 环境变量（兜底）**。在网页 UI 里保存的 key 对所有入口生效，重启后仍有效。

> 国际区请把 base_url 改成 `https://api.minimax.io`：`minipic config set base_url https://api.minimax.io`

## CLI 用法

```bash
# 1. 文生视频（ratio 必填，不能为 adaptive）
minipic create t2v \
  --prompt "镜头拍摄一个女性坐在咖啡馆里，抬头看向窗外" \
  --duration 6 --ratio 16:9 --resolution 768P

# 2. 图生视频（ratio 恒为 adaptive；可用 --ref-image-role 指定 first_frame / last_frame）
minipic create i2v \
  --prompt "The girl in the picture slowly turns her head" \
  --ref-image input.png

# 3. 多模态参考
minipic create r2v \
  --prompt "A girl standing on a high-speed train... (full H3 prompt)" \
  --ref-image bag_front.jpg:reference_image \
  --ref-video girl_train.mp4 \
  --duration 10 --ratio 9:16
```

其他子命令：

```bash
minipic status <task_id>           # 查一次
minipic wait <task_id>             # 阻塞轮询直到终态并下载
minipic list                       # 列出本地所有任务
minipic cancel <task_id>           # 取消排队中的任务
minipic videos                     # 列出已下载的视频
minipic config show                # 看当前配置
minipic web                        # 启动本地网页 UI（端口固定 7860）
```

## Web UI

```bash
minipic web          # 浏览器打开 http://127.0.0.1:7860
```

- 端口固定 **7860**（`minipic web` 与 `python -m minipic.web` 一致，无 `--port` 参数）。
- 在 UI 里保存的 API key 会写入用户配置，CLI 自动共用。
- 支持上传图片 / 视频 / 音频作为参考（图片 ≤30MB，视频 ≤50MB，音频 ≤15MB）。

## 费用预估

按 MiniMax 官方按量计费刊例价（人民币，[platform.minimaxi.com/docs/guides/pricing-paygo](https://platform.minimaxi.com/docs/guides/pricing-paygo)）：

| 分辨率 | 单价      | 10s 视频 |
|--------|-----------|----------|
| 768P   | ¥0.50/秒  | ≈ ¥5.00  |
| 2K     | ¥0.80/秒  | ≈ ¥8.00  |

输入素材：音频免费；参考图片 5 张内免费、超出 ¥0.20/张；参考视频按生成分辨率同价计费（2K ¥0.80/s、768P ¥0.50/s）。

## 项目结构

```
minipic/
├── src/minipic/
│   ├── config.py      # 配置加载（key 优先级 user > local > env）+ 模型能力约束
│   ├── client.py      # httpx 异步客户端（submit / query / upload / download）
│   ├── media.py       # 本地媒体校验 + 上传解析 + 共享的 content 构建 / 下载
│   ├── poller.py      # 异步轮询 + 进度回调
│   ├── storage.py     # SQLite 任务库 + 视频落盘
│   ├── errors.py      # 错误码 → 异常映射
│   ├── cli.py         # click 命令（create / status / wait / list / web ...）
│   └── web.py         # FastAPI 路由（/api/create / /api/config / /api/upload ...）
├── web/
│   └── index.html     # 极简网页 UI
├── examples/
├── tests/
└── pyproject.toml
```

## 跑测试

```bash
pytest
```

## 留痕

- `CHANGELOG.md`：每个版本一条记录
- 任务状态持久化到 `~/.local/share/minipic/tasks.db`（SQLite），崩了能恢复
- 配置文件 `config.json` 加进 `.gitignore`，API key 不进仓库
