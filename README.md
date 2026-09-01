# minipic

本地 CLI + 极简网页 UI，把 MiniMax H3 视频生成（文生视频 / 图生视频 / 多模态参考）包成日常能用的工具。

- **后端**：MiniMax 公开 API（`https://api.minimaxi.com`，需自备 API Key）
- **模型**：`MiniMax-H3`（V2 端点）
- **支持模式**：
  - `t2v`：纯文本 → 视频
  - `i2v`：首帧图 + 文本 → 视频
  - `r2v`：文本 + 任意组合的参考图/参考视频/参考音频（多模态参考）
- **不支持**：首尾帧（FL2VA）。需要的话单开。

## 安装

```bash
# 1. 进入项目目录
cd D:\zcodeproject\minipic

# 2. 创建 venv 并装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

# 3. 配置 API Key（任选一种）
#    a) 环境变量
$env:MINIMAX_API_KEY="eyJhbGciOi..."
#    b) 写到 config.json
minipic config set api_key eyJhbGciOi...
#    c) 写到 ~/.config/minipic/config.json
```

> 国际区请把 base_url 改成 `https://api.minimax.io`：`minipic config set base_url https://api.minimax.io`

## CLI 用法

```bash
# 1. 文生视频（ratio 必填）
minipic create t2v \
  --prompt "镜头拍摄一个女性坐在咖啡馆里，抬头看向窗外" \
  --duration 6 --ratio 16:9 --resolution 768P

# 2. 图生视频（ratio 恒为 adaptive）
minipic create i2v \
  --prompt "The girl in the picture slowly turns her head" \
  --ref-image input.png

# 3. 多模态参考（你今天的"换包"流程）
minipic create r2v \
  --prompt "A girl standing on a high-speed train... (full H3 prompt)" \
  --ref-image bag_front.jpg:reference_image \
  --ref-image bag_side.jpg:reference_image \
  --ref-image bag_back.jpg:reference_image \
  --ref-video girl_train.mp4 \
  --duration 10 --ratio 9:16

# 4. 长参考视频（> 15s）自动均匀采 3×5s，或显式指定
minipic create r2v --prompt "..." --ref-video long.mp4                    # 自动采样
minipic create r2v --prompt "..." --ref-video long.mp4 \
  --ref-video-clips 0:5,30:5,90:5                                          # 显式指定
```

其他子命令：

```bash
minipic status <task_id>           # 查一次
minipic wait <task_id>             # 阻塞轮询直到终态
minipic list                       # 列出本地所有任务
minipic cancel <task_id>           # 取消排队中的任务
minipic videos                     # 列出已下载的视频
minipic config show                # 看当前配置
minipic web --port 7860            # 启动本地网页 UI
```

## 费用预估

按公开 API 当前价：

| 分辨率 | 单价      | 10s 视频 |
|--------|-----------|----------|
| 768P   | $0.08/秒  | ≈ $0.80  |
| 2K     | $0.13/秒  | ≈ $1.30  |

## 长参考视频策略

H3 限制：单段 2–15s、最多 3 段、总和 ≤ 15s。

- 视频 ≤ 15s：原样上传
- 视频 > 15s 且无 `--ref-video-clips`：自动均匀采 3 段 × 5s（恰填满 15s 预算）
- 视频 > 15s 且显式指定：以用户为准，但总和 > 15s 报错
- 切片存到 `.minipic-cache/clips/`，上传后清理，不动用户原文件

## 项目结构

```
minipic/
├── src/minipic/
│   ├── config.py      # 读 config.json / env
│   ├── client.py      # httpx 异步客户端（submit / query / upload / retrieve）
│   ├── media.py       # 本地媒体上传 + 长视频切分
│   ├── prompt.py      # H3 提示词构造助手
│   ├── poller.py      # 异步轮询 + 进度回调
│   ├── storage.py     # SQLite 任务库 + 视频落盘
│   ├── errors.py      # 错误码 → 异常映射
│   ├── cli.py         # click 命令
│   └── web.py         # FastAPI 路由
├── web/
│   └── index.html     # 极简网页 UI
├── examples/
│   └── bag_swap.py    # 把今天的"换包"流程固化成可复用脚本
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
