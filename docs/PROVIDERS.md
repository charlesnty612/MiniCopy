# 服务商接入说明（v0.2.8+）

MiniCopy 通过 `provider` 配置项选择实际调用的服务商。两家都实现 **MiniMax H3 V2 视频生成协议**，但端点、模型命名、能力集与错误格式存在差异，本文记录这些差异及代码中的对应处理。

- 来源：[拓元 Vectron H3 API 文档](https://vectron.meta-stone.com/#/docs/h3-api)（采集于 2026-11-25）
- 对照实现：`src/minipic/config.py` 的 `PROVIDERS` / `PROVIDER_CONSTRAINT_OVERRIDES`，`src/minipic/client.py`，`src/minipic/errors.py`，`src/minipic/poller.py`

---

## 一、差异速查表

| 维度 | `minimax`（官方） | `vectron`（拓元内测） |
|---|---|---|
| Base URL | `https://api.minimaxi.com` | `https://api.vectron.meta-stone.com` |
| 鉴权 | `Authorization: Bearer <key>` | 同左 |
| 创建任务 | `POST /v2/video_generation` | 同左 |
| 查询任务 | `GET /v2/query/video_generation/{task_id}` | 同左 |
| 上传素材 | `POST /v1/files/upload` | `POST /v1/h3/files/upload` |
| 模型 ID | `MiniMax-H3` / `MiniMax-H3-Max` | `MiniMax/MiniMax-H3` |
| 可用模型 | H3、H3-Max | 仅 H3 |
| 分辨率 | H3: 768P/2K；Max: 480P/768P | 480P / 768P（**无 2K**） |
| 时长 | H3: 4–15s；Max: 5–15s | 4–15s |
| Context-IR | 支持 `/v2/h3_context_ir` | **不支持** |
| image-01 生图 | 支持 | **不支持** |
| `extra.prompt_expansion_mode` | H3-Max 支持 | 不适用（无 H3-Max） |
| 上传响应 | `{"file": {"file_id": ...}}` 或 `{"file_id": ...}` | `{"id": "..."}` |
| 状态词 | `Pending`/`Queuing`/`Processing`/`Success`/`Failed`/`Cancelled`/`Expired` | `queued`/`running`/`succeeded`/`failed`/`cancelled` |
| 错误码 | 数字码在 message 尾部 `(NNNN)` | 仅裸 HTTP 状态（400/401/402/422/429/500） |
| 任务可查窗口 | 7 天 | 7 天 |

---

## 二、体型与内容参数（两家一致）

`content[]` 元素结构完全相同：

```jsonc
{"type": "text", "text": "提示词"}
{"type": "image_url", "image_url": {"url": "..."}, "role": "first_frame"}
{"type": "video_url", "video_url": {"url": "..."}, "role": "reference_video"}
{"type": "audio_url", "audio_url": {"url": "..."}, "role": "reference_audio"}
```

`role` 取值：`first_frame` / `last_frame` / `reference_image` / `reference_video` / `reference_audio`。**图生视频与多模态参考互斥**（两家都如此）。上传后以 `mm_file://{file_id}` 引用。

素材限制（两家文档一致）：图片 ≤30MB、边长 256–5760px、JPG/PNG/WEBP/HEIC；视频 MP4/MOV ≤50MB、单段 2–15s、最多 3 个；音频 WAV/MP3 ≤15MB、单段 2–15s、最多 3 个；请求体总大小 ≤64MB。

> 拓元文件管理另有一条更严的限制：单个文件 **≤31 MiB**，文件 7 天后自动过期。上传大图时官方 30MB 的上限与拓元的 31MiB 基本相同，无实际冲突。

---

## 三、代码中的落地方式

### 3.1 配置层

`config.PROVIDERS` 是唯一事实来源：

```python
PROVIDERS = {
    "minimax": {
        "base_url": DEFAULT_BASE_URL,
        "upload_path": "/v1/files/upload",
        "models": ["MiniMax-H3", "MiniMax-H3-Max"],
        "model_ids": {"MiniMax-H3": "MiniMax-H3", "MiniMax-H3-Max": "MiniMax-H3-Max"},
        "supports_context_ir": True,
        "supports_image_generation": True,
        "supports_prompt_expansion": True,
    },
    "vectron": {
        "base_url": "https://api.vectron.meta-stone.com",
        "upload_path": "/v1/h3/files/upload",
        "models": ["MiniMax-H3"],
        "model_ids": {"MiniMax-H3": "MiniMax/MiniMax-H3"},
        "supports_context_ir": False,
        "supports_image_generation": False,
        "supports_prompt_expansion": False,
    },
}
```

- `provider_constraints(provider, model)`：返回模型的约束，叠加 `PROVIDER_CONSTRAINT_OVERRIDES`（拓元 H3 的分辨率砍掉 2K）。返回值是拷贝，不会污染全局 `MODEL_CONSTRAINTS`。
- `resolve_model_id(provider, model)`：把内部规范名翻成线路 ID。**只有发请求时翻译**，账本/UI/CLI 全用规范名。

### 3.2 端点解析

`Config.effective_base_url()`：

1. `base_url` 显式设置（≠ 官方默认值）→ 用它（支持国际区 / 自建网关）。
2. 否则 → 用所选服务商的 `base_url`。

这样旧配置里持久化的官方 `base_url` 不会把用户钉死在官方端点；切换服务商时 `POST /api/config` 与 `minipic config set provider` 也会主动重置这个字段。

### 3.3 能力守卫（请求发出前拦截）

| 场景 | 守卫位置 |
|---|---|
| 服务商无该模型 | `client.create_video_task` 首行 `provider_constraints(...)` |
| 服务商无 Context-IR | `client.create_context_ir_task`；`web._submit_task` 与 `cli._maybe_apply_context_ir` 也先查 `cfg.supports_context_ir`，直接跳过不报错 |
| 服务商无生图 | `client.generate_image` |
| 分辨率 / 时长越界 | `validate_model_params(..., provider=...)` |
| mode / role 不支持 | `validate_model_modes(..., provider=...)` |

调用方（Web / CLI）拿到的是带服务商上下文的 `ConfigError`，不会打到服务端换 4xx。

### 3.4 状态词

`poller` 不再用大小写敏感的等值比较，而是：

```python
SUCCESS_STATUSES  = {"Success", "Succeeded", "succeeded", "SUCCEEDED"}
FAILED_STATUSES   = {"Failed", "failed", "FAILED"}
CANCELLED_STATUSES = {"Cancelled", "Canceled", "cancelled", "canceled"}
EXPIRED_STATUSES  = {"Expired", "expired"}
TERMINAL_STATES   = SUCCESS | FAILED | CANCELLED | EXPIRED
```

Web 列表 / 详情、下载路径、CLI 的 `status` / `wait` 全部改用 `is_success` / `is_failure`。

### 3.5 错误映射

`client._request_with_retry` 的解析顺序：

1. `body.code` / `body.error.code`（官方数字码）。
2. 从 `body.error.message` / `body.message` 尾部抓 `(NNNN)`（官方实际形态）。
3. 都没有 → 用 `HTTP_STATUS_TO_EXC` 按裸状态映射（拓元形态）。

重试判定：数字码 ∈ `{1000, 1001, 1002}`，或**无数字码且** HTTP ∈ `{429, 500, 502, 503, 504}`。

| HTTP | 拓元文档说明 | 异常类 |
|---|---|---|
| 400 | 参数错误（如缺少非空 text） | `InvalidParamsError` |
| 401 | API Key 无效 | `AuthError` |
| 402 | 余额或额度不足 | `BalanceError` |
| 422 | 输入涉及敏感内容 | `SafetyError` |
| 429 | 触发限流 | `RateLimitError`（重试） |
| 500 | 服务端错误 | `ServerError`（重试） |

---

## 四、计费归属

任务创建时把 `provider` 写进 `extra`：

```python
extra = {..., "provider": cfg.provider_key()}
```

前端按 `t.extra.provider` 选价格表（老记录没有该字段时回退到当前服务商）。因此先跑官方、后切拓元，历史任务仍按官方价显示，不会被 1 折价覆盖。

- 官方价目：`web/index.html` 的 `PRICING`（H3 768P ¥0.50/s、2K ¥0.80/s 等刊例价）。
- 拓元内测价：`PROVIDER_PRICING.vectron` 统一 ¥0.05/s（约官方 1 折），参考图不额外计费。

---

## 五、未实现 / 已知限制

1. **拓元无 Context-IR**：UI 会禁用该勾选项并置灰，CLI 静默跳过；不会报错中断。
2. **拓元无 image-01**：右侧生图宣传栏走的是官方端点；在拓元模式下调用 `generate_image` 会抛 `ConfigError`。
3. **拓元无 H3-Max**：模型下拉只剩 H3，`prompt_expansion_mode` 开关一并禁用。
4. **拓元无 2K**：分辨率下拉只剩 480P / 768P；历史记录若曾是 2K，切到拓元后再提交会被本地拦下。
5. **未接入的能力**（两家都未做，非本次范围）：`callback_url` 握手、`/v2/video_regeneration` 再生成、官方任务列表直连（本地 SQLite 账本替代）。
6. **错误文案**：拓元的中文提示直接透传，UI 的 `humanizeError` 只清理 `request_id=` 噪声。

---

## 六、参考链接

- 拓元 H3 快速指南：`https://vectron.meta-stone.com/#/docs/h3-quickstart`
- 拓元 H3 API 文档：`https://vectron.meta-stone.com/#/docs/h3-api`
- 拓元素材上传指南：`https://vectron.meta-stone.com/#/docs/file-upload-guide`
- 官方 V2 参数全解：本仓库 `docs/MINIMAX_API_NOTES.md`
- 官方计费：`https://platform.minimaxi.com/docs/guides/pricing-paygo#视频`
