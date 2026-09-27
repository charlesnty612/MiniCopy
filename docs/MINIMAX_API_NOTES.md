# MiniMax 视频生成 API 参数全解（v0.2.7 对齐版）

> 来源：[MiniMax 开放平台文档](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)（主源：[创建任务](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)、[视频生成指南](https://platform.minimax.cn/docs/guides/video-generation)）。
> 采集日期：2026-09-27。
> 适用模型：`MiniMax-H3`、`MiniMax-H3-Max`（V2 接口）。
> 与本仓库（MiniCopy）v0.2.7 实现对照见文末 §九。

---

## 一、结论先行

MiniMax 当前提供两款视频生成模型：`MiniMax-H3`（标准）与 `MiniMax-H3-Max`（极速）。**创建任务接口（`POST /v2/video_generation`）是一切的入口**，异步返回 `task_id`，再用 `GET /v2/query/video_generation/{task_id}` 轮询结果。传参核心要点：

- **请求体 4 个一级必填字段**：`model`、`content[]`、`duration`、`resolution`——全部必填，无一可省。
- **`content[]` 是多模态数组**，每个元素由 `type`（`text`/`image_url`/`video_url`/`audio_url`）+ 必填键对 + 可选 `role` 组成；**任何模式都必须包含一个非空 `text`**（prompt 不可缺）。
- **`ratio` 是模式敏感参数**：t2v 必填且不能是 `adaptive`；i2v 恒为 `adaptive`（其他值会被忽略）；r2v 可选、默认 `adaptive`。这一约束是开发传参最容易踩坑的地方。
- **可选但易遗漏**：`extra`（仅 H3-Max，控制 prompt expansion）、`callback_url`（带 challenge 握手）、`aigc_watermark`（默认 `false`）。
- **duration / resolution严格按模型对齐**：H3 = 4–15s / `768P`+`2K`；H3-Max = 5–15s（无 4s）/ `480P`+`768P`（无 2K）。错配会在服务端被 400 拒掉。
- **注意：当前 `seed` 字段未在公开文档出现**——若有 seed 需求，文档未确认。

---

## 二、端点清单（V2 视频）

| 端点 | 方法 + 路径 | 主要参数 | 响应核心 |
|---|---|---|---|
| 创建视频生成任务 | `POST /v2/video_generation` | 见 §三 | `{"task_id": "..."}` |
| 查询任务 | `GET /v2/query/video_generation/{task_id}` | 路径参数 `task_id` | `{"task": {...}}`；含 `status` / `content.url` / `usage` / `ratio` / `task_type` / `modality` |
| 查询任务列表 | `GET /v2/query/video_generation` | query：`page_num`、`page_size`、`filter.status`、`filter.task_ids[]`、`filter.model`、`filter.task_type` ∈ {generation, h3_context_ir, regeneration} | `{"items": [...], "total": 476}`；仅最近 7 天 |
| 取消或删除任务 | `DELETE /v2/video_generation/{task_id}` | 路径 `task_id` | `{"task_id": "...", "action": "cancelled"/"deleted", "status": "cancelled"/"deleted"}`；规则：queued→cancel，succeeded/failed→delete，running/cancelled→报错 |
| 创建 H3-Context-IR（提示词增强） | `POST /v2/h3_context_ir` | 仅 H3 可用；参数 `{model, content[], duration, ratio, callback_url?}`；无 `resolution` / `extra` / `aigc_watermark` | `{"task_id": "..."}`；成功后从 `task.content.prompt` 拿增强 prompt；`task_type="h3_context_ir"`，`modality="text"` |
| 创建视频再生成（768P → 2K） | `POST /v2/video_regeneration` | **二选一**：`{model, source_task_id, resolution}` 或 `content` 中含 `{type:"video_url", role:"base_video"}`；`resolution` 强制 `2K` | `{"task_id": "..."}`；`task_type="regeneration"`；只接受符合 H3 768P 规格的源视频，需开通白名单 |

> `DELETE` 行为细则：`queued` → `cancelled`（取消任务，无扣费）；`succeeded`/`failed` → `deleted`（删除任务记录）；`running`/`cancelled` → 报错（不可操作）。
> `task_type` 三类（`generation` / `h3_context_ir` / `regeneration`）共用 query / list / delete 接口。

---

## 三、创建任务接口：`POST /v2/video_generation`

### 3.1 请求方法 / 路径 / 鉴权头

| 项 | 值 |
|---|---|
| 方法 | `POST` |
| 路径 | `https://api.minimax.cn/v2/video_generation` |
| 鉴权头 | `Authorization: Bearer <API_KEY>`（必填；HTTP Bearer Auth） |
| Content-Type | `application/json`（必填；默认值；可选枚举只有 `application/json`） |
| 请求体大小上限 | 64 MB（大文件请用公网 URL，不要走 Base64） |

### 3.2 完整请求体参数表

| 字段 | 类型 | 必填 | 取值 / 范围 | 默认 | 说明 |
|---|---|---|---|---|---|
| `model` | enum\<string\> | 是 | `MiniMax-H3`、`MiniMax-H3-Max` | — | `MiniMax-H3`：支持文生视频、图生视频（首/尾帧）、多模态参考生视频；分辨率 `768P`/`2K`；时长 4–15s。`MiniMax-H3-Max`：极速版，同样三种模式；分辨率 `480P`/`768P`（不支持 2K）；时长 5–15s（不支持 4s）。 |
| `content` | object[] | 是 | 至少含一个非空 `text` 元素 | — | 多模态输入数组；元素按 `type` 区分 `text` / `image_url` / `video_url` / `audio_url`，可用 `role` 标注用途。详见 §四。 |
| `resolution` | enum\<string\> | 是 | `480P`、`768P`、`2K` | `768P`（仅 H3-Max 文档明示默认） | 模型档位强约束：H3 → `768P`/`2K`；H3-Max → `480P`/`768P`，选 `2K` 会报错。 |
| `duration` | enum\<integer\> | 是 | `4`–`15`（整数） | — | H3：可用 `4–15`；H3-Max：可用 `5–15`（不支持 4 秒）。 |
| `ratio` | enum\<string\> | 模式敏感（见 §五） | `adaptive`、`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` | `adaptive`（i2v/r2v 默认；t2v 不允许） | 详见 §五 ratio 专节。 |
| `extra` | object | 否（H3-Max 才有效） | 仅支持子字段 `prompt_expansion_mode` | `balanced`（不传 `extra` 或不传该子字段时） | **H3-Max 专用**。不要传 `balance`、空串、布尔或其他未声明字段。 |
| `callback_url` | string | 否 | 任意 HTTPS/HTTP URL | — | 任务状态变更回调；启用前 MiniMax 会先 POST 一个含 `challenge` 的请求，需 **3 秒内原样回显** 完成验证；通过后每次状态变更会推送与 query 接口一致结构。回调 `status` 取值：`queued`、`running`、`succeeded`、`failed`、`cancelled`。 |
| `aigc_watermark` | boolean | 否 | `true` / `false` | `false` | 是否在生成视频打 AIGC 标识水印。 |

> 文档**未提供** `seed` 字段；若实现里传了 `seed`，那是未文档化的私有约定，文档未能确认其行为。

### 3.3 duration × resolution × model 对应约束速查表

| model | duration 可用 | resolution 可用 | 备注 |
|---|---|---|---|
| `MiniMax-H3` | 4–15 | `768P`、`2K` | 4s 仅 H3 可用；2K 仅 H3 可用 |
| `MiniMax-H3-Max` | 5–15 | `480P`、`768P` | 不支持 4s、不支持 2K；默认 resolution `768P` |

---

## 四、`content[]` 元素结构（按 `type` 拆开）

每个元素的通用字段：

```jsonc
{
  "type": "<text|image_url|video_url|audio_url>",
  "role": "<first_frame|last_frame|reference_image|reference_video|reference_audio|base_video>",  // 可选
  "text": "...",            // 仅 type=text
  "image_url": { "url": "..." },   // 仅 type=image_url
  "video_url": { "url": "..." },   // 仅 type=video_url
  "audio_url": { "url": "..." }    // 仅 type=audio_url
}
```

### 4.1 `role` 枚举

| role | 适用 type | 用途 |
|---|---|---|
| `first_frame` | `image_url` | 图生视频起始帧（图生-首帧场景；不填 role 也按首帧处理） |
| `last_frame` | `image_url` | 图生视频结束帧 |
| `reference_image` | `image_url` | 多模态参考生视频的参考图 |
| `reference_video` | `video_url` | 多模态参考生视频的参考视频 |
| `reference_audio` | `audio_url` | 多模态参考生视频的参考音频 |
| `base_video` | `video_url` | 仅再生成（regeneration）接口使用；create 接口不出现 |

**关键约束**（create 接口）：`reference_*` 系列与 `first_frame` / `last_frame` **互斥**——content 数组里若出现任一 `reference_*`，就不能再出现 `first_frame`/`last_frame`，反之亦然。

### 4.2 输入组合与生成场景

| 场景 | content 组合 | 文档代号 |
|---|---|---|
| 文生视频 | 仅一个 `text` | t2va |
| 图生视频-首帧 | `text` + 1 张 `image_url`（`role=first_frame`，可省） | i2va |
| 图生视频-尾帧 | `text` + 1 张 `image_url`（`role=last_frame`） | i2va |
| 图生视频-首尾帧 | `text` + 2 张 `image_url`（role 分别为 `first_frame`、`last_frame`） | i2va |
| 多模态参考生视频 | `text` + 参考图（`reference_image`）/参考视频（`reference_video`）/参考音频（`reference_audio`）的任意组合 | r2va |

### 4.3 输入媒体硬性限制（请求体 ≤ 64 MB；推荐 URL，勿用 Base64）

**`image_url`**（首帧 / 尾帧 / 参考图 共用基础限制）：

| 项 | 限制 |
|---|---|
| 格式 | JPG、JPEG、PNG、WEBP、HEIC、HEIF |
| 单文件 | ≤ 30 MB |
| 宽高 | [256, 5760] px |
| 长宽比（宽/高） | [0.4, 2.5] |
| 数量 | 首帧 ≤ 1、尾帧 ≤ 1、参考图 ≤ 9 |

**`video_url`**（仅多模态参考场景）：

| 项 | 限制 |
|---|---|
| 容器 / 格式 | MP4（`.mp4`）、MOV（`.mov`） |
| 编码 | 视频 H.264/AVC、H.265/HEVC；音频 AAC、MP3 |
| 单文件 | ≤ 50 MB |
| 个数 | ≤ 3 |
| 单段时长 | [2, 15] s；总时长 ≤ 15 s |
| 宽高 | [256, 5760] px |
| 长宽比 | [0.4, 2.5] |
| 帧率 | [23.976, 60] |

**`audio_url`**（仅多模态参考场景）：

| 项 | 限制 |
|---|---|
| 格式 | WAV、MP3 |
| 单文件 | ≤ 15 MB |
| 个数 | ≤ 3 |
| 单段时长 | [2, 15] s；总时长 ≤ 15 s |

**提示词字数上限**：≤ 7000 字符。

---

## 五、ratio 专节（重点）

### 5.1 全部取值枚举

`adaptive`、`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` —— 共 7 个值；`adaptive` 是默认值但**不是普适默认**（按模式而定）。

### 5.2 三种模式对 ratio 的要求

| 模式（content 形态） | ratio 是否必填 | ratio 允许值 | 不传 / 传 `adaptive` 时行为 |
|---|---|---|---|
| **t2v**（content 仅含 `text`） | **必填** | 6 个具体比例：`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` | **不允许传 `adaptive`**，否则参数错误；不传 ratio 也会报错 |
| **i2v**（content 含 `first_frame` / `last_frame` 图片） | ratio **恒为 `adaptive`** | ratio 字段在 i2v 模式下**强制被忽略**——无论传什么具体比例，都按 `adaptive` 处理；传了"合理值"不会报错但没意义 | 文档明示「宽高比由输入图片决定」 |
| **r2v**（content 含 `reference_image` / `reference_video` / `reference_audio`） | 可选 | 默认 `adaptive`；也可显式指定上述任一具体比例 | 不传 = `adaptive` |

### 5.3 `adaptive` 的语义

- 「由输入自适应选择最合适的宽高比」。具体产出比例在查询接口响应 `task.ratio` 字段获取。
- 在查询 / 列表接口的响应里，t2v 模式下 `ratio` 是请求里指定的实际比例（如 `"16:9"`）；i2v 模式下 `ratio` 可能保留 `"adaptive"` 或呈现真实输出比例（视服务端实现）。

### 5.4 模式识别表（开发分支依据）

| content 包含 | 识别出的模式 | ratio 行为 |
|---|---|---|
| 只有 `text` | t2v | 必填具体比例 |
| 含 `image_url` 且 role ∈ {first_frame, last_frame} | i2v | 传啥都被忽略，按 adaptive 处理 |
| 含 `image_url` / `video_url` / `audio_url` 且 role ∈ {reference_*} | r2v | 可选，默认 adaptive |
| `reference_*` 与 `first_frame`/`last_frame` 同存 | **非法** | 服务端 400 |

---

## 六、`extra` / `callback_url` / `aigc_watermark` 详解

### 6.1 `extra`（H3-Max 专用）

- 用途：控制 H3-Max 的提示词扩展行为。
- **仅 `MiniMax-H3-Max` 支持**；H3 传该字段会失败。
- 当前文档仅声明一个子字段：`prompt_expansion_mode`，取值 `disabled` / `balanced` / `quality`，默认 `balanced`（不传 `extra` 或不传该子字段时）。
- **禁止**：传 `balance`（拼写错误）、空字符串、布尔值或其他未声明字段——会参数错误。
- None / 不传 → 整个 `extra` 字段从 payload 中省略（不要发空对象）。

### 6.2 `callback_url`

- 任务状态变更回调；启用前 MiniMax 会先 POST 一个**含 `challenge` 字段**的握手请求，需 **3 秒内原样回显 `challenge`** 完成验证。
- 验证通过后，每次状态变更会 POST 一份与 query 接口响应同结构的对象。
- 回调 `status` 取值：`queued`、`running`、`succeeded`、`failed`、`cancelled`。
- **MiniCopy 决策不接入**（本地轮询架构，无需公网回调与 3 秒 challenge 握手——见 §九）。

### 6.3 `aigc_watermark`

- boolean，默认 `false`；true → 在生成视频上打 AIGC 标识水印。
- 两模型通用。

---

## 七、响应结构与错误码

### 7.1 创建成功响应

```json
{ "task_id": "424010985738629" }
```
- 提取路径：`response.json()["task_id"]`，或顶层 `task_id`。

### 7.2 典型查询成功响应（用于异步拿成品）

```json
{
  "task": {
    "id": "424010985738629",
    "model": "MiniMax-H3",
    "status": "succeeded",
    "created_at": 1785125529,
    "updated_at": 1785125946,
    "content": { "url": "https://.../output.mp4" },
    "resolution": "2K",
    "duration": 5,
    "usage": {
      "total_seconds": 5,
      "input_seconds": 0,
      "output_seconds": 5,
      "input_image_count": 0,
      "input_audio_seconds": 6,
      "total_tokens": 273890,
      "prompt_tokens": 13500,
      "completion_tokens": 260390
    },
    "ratio": "16:9",
    "task_type": "generation",
    "modality": "video"
  }
}
```
- 终端 `status`：`succeeded`、`failed`、`cancelled`（`queued` / `running` 为中间态）。
- 成功取成品 URL：`task.content.url`。

### 7.3 错误响应格式

```json
{
  "type": "error",
  "error": {
    "type": "<error_type>",
    "message": "... (<code>)",
    "http_code": "<400|401|402|422|429|500>"
  },
  "request_id": "021785229015510a2c883cf675b9804d"
}
```

| HTTP | error.type | message 示例 | 内部 code |
|---|---|---|---|
| 400 | `bad_request_error` | `invalid params, content must include a non-empty text item (prompt is required)` | 2013 |
| 401 | `authorized_error` | `login fail: Please carry the API secret key in the 'Authorization' field of the request header` | 1004 |
| 402 | `insufficient_balance_error` | `insufficient balance` | 1008 |
| 422 | `unprocessable_entity_error` | `video description contains sensitive content` | 1026 |
| 429 | `rate_limit_error` | `rate limit, please retry later` | 1002 |
| 500 | `server_error` | `internal error` | 1000 |

### 7.4 错误码解析坑

- 官方错误示例中**没有**在 `error.code` 子字段中显式给数字 code，**所有数字 code 都在 `message` 末尾的 `(NNNN)` 括号里**。
- 因此从响应解析错误码时，应：
  1. 先尝试 `err_body["error"]["code"]`（兼容部分旧/变体）；
  2. 兜底用 `re.search(r"\((\d{3,4})\)\s*$", message)` 抓 `(NNNN)`。
- 未做兜底时，限流（429/1002）将不命中 RETRYABLE_CODES，客户端不会重试——这是 v0.2.6 之前 `client.py` 的实际缺陷。

---

## 八、Context-IR 与再生成

### 8.1 H3-Context-IR（提示词增强）

- 端点：`POST /v2/h3_context_ir`
- 仅 `MiniMax-H3` 可用。
- 请求参数：`{model, content[], duration, ratio, callback_url?}`——**无 `resolution` / `extra` / `aigc_watermark`**。
- `duration` 仍为必填且范围 4–15（与 create 接口一致）。
- 响应：`{"task_id": "..."}`；成功后从 `task.content.prompt` 取增强 prompt（结构化多段）；`task_type="h3_context_ir"`，`modality="text"`。
- `ratio` 行为同 §五（t2v 必填非 adaptive；i2v 恒 adaptive；r2v 可选默认 adaptive）。
- **接口只返回增强提示词，不会创建视频生成任务**。
- `task_type=h3_context_ir` 是与 `generation` / `regeneration` 共用 query/list/delete 接口的关键区分。

### 8.2 视频再生成（768P → 2K）

- 端点：`POST /v2/video_regeneration`
- 仅 `MiniMax-H3`；`resolution` 强制 `2K`。
- **二选一**传源：
  - `{model, source_task_id, resolution}`：源是已有 `/v2/video_generation` 成功任务；
  - `{model, resolution, content: [...]}`：content 中含 `{type:"video_url", role:"base_video"}`。
- 源任务必须：当前账号所有、状态 `succeeded`、仍可查（创建 7 天内）。
- 需开通白名单；**非白名单用户调用会失败**。
- `task_type="regeneration"`。
- 计费单独——见 `MINIMAX_PRICING.md` §四。

---

## 九、与 MiniCopy v0.2.7 实现的对照

| 维度 | 官方口径 | MiniCopy v0.2.7 状态 |
|---|---|---|
| `model` 枚举校验 | 仅 `MiniMax-H3` / `MiniMax-H3-Max` | ✅ 已校验（`config.py` MODEL_CONSTRAINTS） |
| H3-Max `modes` 含 `r2v` | H3-Max 官方支持三种模式（含 r2v） | ✅ v0.2.6 已放开 |
| `duration` / `resolution` × `model` 强约束 | H3:4–15 / 768P+2K；H3-Max:5–15 / 480P+768P | ✅ 已校验 |
| `ratio` 枚举校验 | `adaptive|21:9|16:9|4:3|1:1|3:4|9:16` | ✅ v0.2.6 已落地（CreateBody Field pattern） |
| `ratio` 模式分支（t2v 必填非 adaptive；i2v 恒 adaptive；r2v 可选默认 adaptive） | 同左 | ✅ v0.2.6 已校验（`_validate_mode`） |
| `extra.prompt_expansion_mode` | enum `disabled|balanced|quality`，默认 balanced，仅 H3-Max | ✅ v0.2.6 已落地（`create_video_task` 关键字参数 + web.py 校验） |
| `extra` None / 不传时 | 整个 `extra` 字段从 payload 省略 | ✅ 已实现（仅在值非 None 时写 `payload["extra"]`） |
| `aigc_watermark` | boolean 默认 false | ✅ v0.2.6 已透传（create_video_task + web.py CreateBody） |
| `callback_url` | 含 3 秒 challenge 握手 | ✅ **决策不接入**（本地轮询架构，无需公网回调） |
| 错误码兜底解析（message 尾部 `(NNNN)`） | 官方错误示例无 `error.code` 子字段 | ✅ v0.2.6 已落地（`client.py` 正则兜底） |
| 错误码 1000/1001/1002 → ServerError / RateLimitError 映射 | 文档示例 500→1000、429→1002 | ✅ v0.2.6 已扩展 `_CODE_TO_EXC` |
| `task_id` 提取路径 | 顶层 `task_id` 字段 | ✅ 已实现 |
| CLI `--aigc-watermark` / `--prompt-expansion-mode` | t2v/i2v/r2v 三命令均应有 | ✅ v0.2.6 已加（Context-IR 调用不透传这两项） |
| `GET /v2/query/video_generation` 轮询 | 异步轮询，10s 间隔推荐 | ✅ 已实现（`poller.py`、`client.py`） |
| `GET /v2/query/video_generation` 列表（filter.status / filter.task_type / filter.model / page） | 列表端点 + 过滤 | ⚠️ **官方端点直连未实现**（MiniCopy 用本地 SQLite 做任务账本，不直连官方 list） |
| `DELETE /v2/video_generation/{task_id}` | queued→cancel、succeeded/failed→delete | ⚠️ **官方端点直连未实现** |
| 本地账本 `GET /api/tasks` / `DELETE /api/tasks/{id}` | MiniCopy 自有 | ✅ **本地 ledger 列表/删除已实现**（用本地 SQLite 做任务账本，不直连官方 list/delete） |
| `POST /v2/video_regeneration` | 768P → 2K，需白名单 | ⚠️ **未实现**（缺再生成能力） |
| Context-IR 异步任务支持 | H3 专用，启用后产增强 prompt | ✅ v0.2.6 已支持（`create_context_ir_task` + `fetch_context_ir_prompt`），失败/超时静默回退 |
| 任务记录补齐 model/resolution/duration/ratio 顶层字段（前端费用估算所需） | 查询响应里本就有这些键 | ✅ v0.2.7 已补（`_record_to_dict` 从 extra 透出） |
| `seed` 字段 | 文档未列 | 未实现（无文档化依据，不传） |

---

## 十、风险与未完成项

1. **`seed` / `negative_prompt` 等隐含字段**：文档未列；若要传这类字段，先在 [create 页](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create) 确认或在控制台/Playground 抓实际请求核对。
2. **错误码嵌套位置**：文档所有错误示例里 `code` 都在 `message` 末尾括号里（如 `(2013)`、`(1008)`），而**没有**在 `error.code` 子字段中显式给值——必须 message 正则 fallback。
3. **回调握手**：若启用 `callback_url`，要准备一个能在 3 秒内回显 `challenge` 的端点；3 秒内不返回 → 握手失败 → 后续不会有推送。
4. **i2v 的 ratio "被忽略"**：传给 i2v 的具体 ratio 不会报错，但会被忽略——调用方不要据此判断请求"成功指定了比例"。
5. **mode文档代号 t2v/i2v/r2v**：文档里实际写作 `t2va` / `i2va` / `r2va`（带后缀 `a`），原文照录；术语统一性见仁见智，传参不影响。
6. **2026 时间戳**：官方文档样例里 `created_at`/`updated_at` 是 1785125529 这类值，对应 2026 年附近的时间戳——文档时间范围合理，无需怀疑。
7. **`task_type` 区分**：`generation` / `h3_context_ir` / `regeneration` 三类任务共用同一 query/list/delete 接口，客户端**仅看 status 判定终态是不够的**，要看 `task_type` 才能正确路由（context-ir 没有 `content.url`，regeneration 的成品也可能不同）。

---

## 十一、来源 URL 列表

- [`https://platform.minimax.cn/docs/api-reference/video-generation-v2-create`](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create) — 创建任务接口（主源）
- [`https://platform.minimax.cn/docs/guides/video-generation`](https://platform.minimax.cn/docs/guides/video-generation) — 视频生成指南（H3/H3-Max 生成方式表、输入条件表）
- [`https://platform.minimax.cn/docs/api-reference/video-generation-v2-query`](https://platform.minimax.cn/docs/api-reference/video-generation-v2-query) — 查询任务
- [`https://platform.minimax.cn/docs/api-reference/video-generation-v2-list`](https://platform.minimax.cn/docs/api-reference/video-generation-v2-list) — 查询任务列表
- [`https://platform.minimax.cn/docs/api-reference/video-generation-v2-delete`](https://platform.minimax.cn/docs/api-reference/video-generation-v2-delete) — 取消或删除任务
- [`https://platform.minimax.cn/docs/api-reference/video-generation-v2-h3-context-ir`](https://platform.minimax.cn/docs/api-reference/video-generation-v2-h3-context-ir) — H3-Context-IR
- [`https://platform.minimax.cn/docs/api-reference/video-generation-v2-regeneration`](https://platform.minimax.cn/docs/api-reference/video-generation-v2-regeneration) — 视频再生成
- [`https://platform.minimax.cn/docs/guides/pricing-paygo#视频`](https://platform.minimax.cn/docs/guides/pricing-paygo#%E8%A7%86%E9%A2%91) — 视频计费（详见 `MINIMAX_PRICING.md`）