# MiniCopy 架构与业务逻辑

> 维护日期：2026-09-01（v0.2.0）。本文描述当前实现；改动代码后请同步更新。

## 定位

把 MiniMax H3 视频生成（文生视频 / 图生视频 / 多模态参考）包成本地工具：
**一条 API（`/v2/video_generation`）+ 两个入口（CLI + Web UI）+ 一套共享配置/任务库**。

## 总体结构

```
用户 ──► CLI（minipic create ...）────┐
用户 ──► Web UI（minipic web → 浏览器）─┤
                                      ▼
                    共享层：config / client / media / poller / storage / errors
                                      ▼
                          MiniMax API（V2 video_generation）
```

- **CLI 与 Web 是两条独立平行链路**，谁也不依赖谁（`web.py` 不 import `cli.py`）。
- **共享的东西**：
  - API key：user config 唯一权威（`user > local > env`），UI 保存后 CLI 自动共用。
  - 任务库：同一个 SQLite（`minipic list` 与网页「最近任务」看到同一批）。
  - content 构建：`media.build_content()`。
  - 下载：`media.download_task_result()`。

## 模块职责

| 模块 | 职责 |
|------|------|
| `config.py` | 配置加载（key 优先级 user > local > env）+ 模型能力约束（modes / i2v_roles） |
| `client.py` | httpx 异步客户端：上传（V1 file API）、提交/查询（V2）、下载 |
| `media.py` | 媒体校验（视频/音频 ≤15s）、`mm_file://` 引用、共享的 content 构建与下载 |
| `poller.py` | 异步轮询 + 进度回调 |
| `storage.py` | SQLite 任务库（tasks 表，`extra` 存自由扩展字段） |
| `cli.py` | click 命令壳（薄）：create / status / wait / list / cancel / videos / config / web |
| `web.py` | FastAPI 路由 + 请求校验（_validate_mode） |
| `web/index.html` | 前端单页（原生 JS） |

## 核心业务流程：提交一个视频任务

1. **校验**（web `_validate_mode` / cli 命令内联）：
   - 模式合法性、模型能力（H3-Max 禁 r2v 与中间帧）。
   - i2v 首/尾帧组合（1 图或 first+last 双图）、图生视频与多模态参考互斥。
   - text ≤7000 字符、参考图 ≤9、首/尾帧各 ≤1。
   - 分辨率 / 时长按模型（H3: 768P/2K、4-15s；H3-Max: 480P/768P、5-15s）。
2. **解析参考素材**（media）：
   - 本地图片/视频/音频 → V1 上传拿 `file_id` → 引用为 `mm_file://{file_id}`。
   - 视频/音频校验 ≤15s（多段已移除，超长报错提示先截取）。
   - 远程 URL 直接透传。
3. **构建 content[]**（`media.build_content`，cli/web 共用，官方 schema）：
   - text：`{"type":"text","text":"..."}`（扁平）。
   - image_url / video_url / audio_url：URL 嵌套 + `role`。
4. **提交**（`client.create_video_task`）→ `task_id` → 存 SQLite（submitted）。
5. **轮询**（poller）→ 终态 → **下载**（`media.download_task_result` → `videos/{task_id}.mp4`）→ 更新任务状态。
6. **用量/费用**：查询响应里的 `usage`（total_seconds 等）持久化到任务 `extra.usage`，Web 详情页展示估算费用。

## API key 逻辑

- 优先级：**user config > local config > env（兜底）**。
- UI 保存 key → 写 user config → CLI 重启后自动读到同一个 key。
- 首次启动无 user key → UI 显示未配置（env 不算 UI 已配置）；CLI 在无文件 key 时用 env 兜底。
- Web 端口固定 **7860**（CLI `web` 命令与 `python -m minipic.web` 一致，无 `--port` 参数）。

## 模型能力约束（对照官方文档）

| | MiniMax-H3 | MiniMax-H3-Max |
|---|---|---|
| 模式 | t2v / i2v / r2v | t2v / i2v |
| i2v 帧 | 首帧 / 尾帧（含首+尾组合） | 首帧 / 尾帧 |
| 分辨率 | 768P / 2K | 480P / 768P |
| 时长 | 4–15s | 5–15s |

- role 枚举只有 `first_frame` / `last_frame` / `reference_image` / `reference_video` / `reference_audio`，**没有 `middle_frame`**（官方能力文本提过中间帧，但 schema 无此枚举，按 schema 收紧）。
- 多段生成已移除，参考素材仅支持 ≤15s 单段。

## 测试

- `pytest`（tests/ 覆盖 config / client / media / poller / storage / cli / web）。
- 关键行为有回归保护：content 结构、模型能力拦截、key 优先级、上传限制。
