# MiniMax 视频 API 计费定价报告

> 来源：[MiniMax 开放平台-按量计费#视频](https://platform.minimax.cn/docs/guides/pricing-paygo#%E8%A7%86%E9%A2%91)。
> 采集日期：2026-09-27。
> **刊例价可能调整，调整时以官方页面为准——本文档数字仅作 2026-09-27 当日快照。**
> 所有数字直接抄自定价页视频节，单位均为**人民币元**。

---

## 一、结论先行

视频计费是**"输出按秒 + 输入素材分项 + 生成方式加成"**的三段式结构，UI 展示费用必须按这三段拆算，否则不准：

- **输出费**：按生成视频的"输出秒数 × 档位单价"线性计费。不同模型 × 分辨率 × 不同任务类型（如再生成）的"刊例价"都不同。
- **输入素材费**：参考图 / 参考视频 / 参考音频三类素材，**按张数或时长单独计费**，且都有"免费额度阈值"，超额才收费。**免费额度阈值与模型相关**：H3 图片免费 5 张；H3-Max 图片免费 2 张。
- **H3-Context-IR**：单独按 token 价（输入/输出不同价），与视频任务**累加**——MiniCopy 默认开启 Context-IR 时，**单次任务实际成本 = Context-IR token 费 + 视频任务费用**，漏算 Context-IR 是费用预估不准的最常见原因。

---

## 二、视频生成-输出价格（按秒计费）

| 模型 | 分辨率 | 单价 |
|---|---|---|
| MiniMax-H3 | 768P | **0.50 元/秒** |
| MiniMax-H3 | 2K | **0.80 元/秒** |
| MiniMax-H3-Max | 480P | **0.33 元/秒** |
| MiniMax-H3-Max | 768P | **0.50 元/秒** |

- 计费规则：**按生成的输出秒数计费**（与请求 `duration` 一致，与最终 `usage.output_seconds` 一致）。
- 一个 task_id 一次任务只对应一种分辨率档位，不会跨档叠加。
- `usage.total_seconds` ≈ `usage.output_seconds`（生成视频时长）。

预估公式：`输出费 = duration × 分辨率单价`

---

## 三、视频生成-输入素材价格（按张 / 按秒）

> 输入素材费用**与输出费独立累加**，与生成方式（t2v / i2v / r2v）无关，按"你往 content[] 里塞了什么素材"算。

### 3.1 MiniMax-H3

| 素材类型 | 计费规则 |
|---|---|
| 音频 | 免费 |
| 图片 | **5 张以内免费；超出部分 0.20 元/张** |
| 视频 | **按输入视频秒数 × 生成视频分辨率单价**：2K = 0.80 元/秒，768P = 0.50 元/秒 |

注意：
- 图片免费额度 **5 张**，与 role无关（first_frame / last_frame / reference_image 都算）。
- 视频输入按"**生成视频的目标分辨率**"定价，不按输入视频自己的分辨率。
- 首尾帧各 1 张图片 → 都在免费额度内（5 张内），所以 **i2v 模式不会有图片附加费**（只要不叠 reference_image）。
- r2v 模式：参考图 ≤ 9 张，前 5 张免费，超出 4 张按 0.20/张 → 即 r2v 图片附加费 = max(0, ref_image_count - 5) × 0.20。

### 3.2 MiniMax-H3-Max

| 素材类型 | 计费规则 |
|---|---|
| 音频 | 免费 |
| 图片 | **2 张以内免费；超出部分 0.50 元/张** |
| 视频 | **按输入视频秒数 × 生成视频分辨率单价**：480P = 0.37 元/秒，768P = 0.97 元/秒 |

注意：
- **图片免费额度只有 2 张**（比 H3 严），所以 H3-Max 的 r2v 模式只要 ref_image ≥ 3，就开始按 0.50/张收。
- 视频输入的768P 单价 0.97 比 H3 的 768P 单价 0.50 贵近1 倍——预估时务必按当前 model 取数，别用错表。
- 注意：H3-Max 不支持 2K，所以"H3-Max + 2K"组合不存在，文档也没列输入视频的 2K 输入单价（无需关心）。

---

## 四、视频再生成（H3 768P → 2K）单独计费

按 `/v2/video_regeneration` 接口生成 2K 时：

### 4.1 输出价

| 模型/接口 | 分辨率 | 单价 |
|---|---|---|
| MiniMax-H3-Regeneration | 768P → 2K | **0.30 元/秒** |

### 4.2 输入素材价（原768P 任务中用过的素材**重新计费**一次）

| 素材 |规则 |
|---|---|
| 音频 | 免费 |
| 图片 | **5 张以内免费；超出 0.15 元/张** |
| 视频 | **按原 768P 任务中输入视频秒数 × 0.30 元/秒** |

注意：
- 再生成是**单独任务**（`task_type="regeneration"`），成本是**原视频任务之外的额外费用**，预估时如果用户用 768P 出片后又升 2K，需要把再生成的输出费 + 重新计费的输入费都加上去。
- 再生成的输入图片免费额度也是 5 张（与 H3 视频生成一致）。
- MiniCopy 当前**未实现 regeneration 接口**（缺能力）。

---

## 五、H3-Context-IR 任务价格（按 token 单独计）

| 模型/接口 | 输入价格 | 输出价格 |
|---|---|---|
| MiniMax-H3-Context-IR | **5.80 元/百万 tokens** | **23.00 元/百万 tokens** |

- 计费规则：按 token 数（输入 + 输出 分开价）。
- 仅 H3 可用（不能用于 H3-Max）。
- 异步任务，**与视频任务在同一工作流中累加计费**。

预估公式：

```
context_ir 费 = (input_tokens × 5.80 + output_tokens × 23.00) / 1_000_000
```

但实际 MiniCopy 默认开启 Context-IR，**任务失败时静默回退到原始 prompt**——**成功的 Context-IR 任务既不改写提示词，也照样收费**。预估口径里**应该永远把 Context-IR 计进去**（哪怕回退，task_id 已下、token 已烧）。

---

## 六、"费用预估"完整公式（建议落到代码里）

设一次视频生成任务给：
- `model ∈ {MiniMax-H3, MiniMax-H3-Max}`
- `resolution ∈ {768P, 2K, 480P}`
- `duration`（整数秒，请求值）
- 输入素材：
  - `n_img`：content[] 里 `type=image_url` 的数量
  - `dur_video`：content[] 里所有 `type=video_url` 的时长合计（秒）
  - `n_audio`：音频素材数量（不计费）
- 是否开启 Context-IR（H3）

则：

```text
输出费 = duration × output_unit_price(model, resolution)            # 见 §二表
图片费 = max(0, n_img - free_img_threshold(model)) × img_unit_price(model)   # 见 §三表
视频费 = dur_video × input_video_unit_price(model, resolution)      # 见 §三表
Context-IR 费（H3）= (input_tokens × 5.80 + output_tokens × 23.00) / 1e6
```

> Context-IR 的 token 数文档未给静态预估口径，需要查 `/v2/h3_context_ir` 任务成功响应的 `usage.total_tokens / prompt_tokens / completion_tokens` 字段做**事后精确计费**；预估阶段可按经验值（参考一次实际响应量级）做粗估。

按 §二/§三表具体数值：

| 维度 | H3-768P | H3-2K | H3-Max-480P | H3-Max-768P |
|---|---|---|---|---|
| 输出 单价（元/秒） | 0.50 | 0.80 | 0.33 | 0.50 |
| 图片免费阈值 | 5 | 5 | 2 | 2 |
| 图片超额单价（元/张） | 0.20 | 0.20 | 0.50 | 0.50 |
| 输入视频 单价（元/秒） | 0.50 | 0.80 | 0.37 | 0.97 |

---

## 七、MiniCopy 费用估算落地现状（统计时点：v0.2.7）

> v0.2.7 起，前端 `web/index.html` 已落地按官方刊例价的三段式费用估算（`PRICING` 常量 + 输出费/输入图片费/输入视频费）。本节标记剩余缺口。

| 维度 | v0.2.7 现状 | 文档要求 | 缺口 |
|---|---|---|---|
| 输出费 | ✅ v0.2.7 已实现（前端刊例价估算） | duration × 分辨率档单价 | OK |
| 输入图片费 | ✅ v0.2.7 已实现（前端刊例价估算） | 按"超出免费阈值"张数计费 | OK |
| 输入视频费 | ✅ v0.2.7 已实现（前端刊例价估算） | 按"输入视频秒数 × 目标分辨率档单价" | OK |
| Context-IR 费 | ⚠️ 未纳入估算 | 输入5.80 元/百万 tokens，输出 23.00 元/百万 tokens | 缺 |
| 输入音频费 | ✅ v0.2.7 已实现（按"免费"处理，写 0） | 免费 | OK |
| 再生成（768P→2K）费 | ⚠️ 未纳入估算 | 输出 0.30 元/秒 + 输入素材重计费 | 缺（且接口都没实现） |
| 持久化 usage 字段 | ✅ v0.2.7 已实现 | 文档响应里 `usage.total_seconds / input_seconds / output_seconds / input_image_count / input_audio_seconds / total_tokens / prompt_tokens / completion_tokens` 都有 | OK |

剩余的"费用预估不准"原因有两条：
1. **缺 Context-IR 费**：MiniCopy 默认开启 Context-IR；成功/失败/超时都被静默吞掉——但 token 费照扣。这块是当前最大黑洞。
2. **缺再生成费**：regeneration 接口未实现，预估代码也无从写；如果将来开放768→2K 升档能力，必须同步加 §四的两段计费。

---

## 八、风险与未完成项

1. **刊例价可能随时间调整**：pricing-paygo 文档已多次出现"自 2026 年 X 月 X 日起"调整表述（如 MCP/web_search 都有预告），预估代码**必须做成可配置表**，不要硬编码 0.50/0.80 这种常量。
2. **H3-Max 输入视频 768P 单价 0.97 元/秒比 H3 768P 输出价 0.50 元/秒还贵**——一笔 r2v 任务如果参考视频合计 10s、生成 5s，**输入视频费9.7 元 > 输出费 2.5 元**，UI 必须明确告知输入视频费的占比，否则用户看不懂账单。
3. **付费接口下线（音乐）**：定价页明确"2026-08-20 起付费音乐接口不再面向新用户"，同页"历史模型"段保留——预估系统遇到 MiniMax-Hailuo-02/2.3 等历史模型时，应用历史单价（已在文档表里）。如果用户切到这些历史模型，预估逻辑要走另一张表。
4. **Context-IR 计费精度**：token 计费、字符/token 比值浮动（文档说1600 中文字符 ≈ 1000 tokens），预估阶段无法精确；建议 UI 显示"≈ X 元（精确值见 usage）"。
5. **服务下线 vs 上线模型混排**：pricing-paygo 同页有"按量计费"+"Token Plan 积分"两套体系（前者按 OpenAPI Key 扣余额，后者按订阅 Key 用积分）。MiniCopy 的预估系统应**只针对按量计费 Key**——如果是订阅 Key 的积分场景，计费维度完全不同，需要另行建模。
6. **错误重试不计费**：当前 retry 策略只重试 1000/1001/1002（`errors.py` 的 `RETRYABLE_CODES`），重试本身不计费；但如果重试仍失败并对账户扣了部分费，文档没说，需要看账单实测。

---

## 九、历史模型单价（参考）

> 来源：定价页 #视频段末"历史模型"小节。仅供查表，**MiniCopy 当前不调用这些模型**。

| 模型 | 功能 | 单价（元/视频） |
|---|---|---|
| MiniMax-Hailuo-2.3-Fast | 图生视频，768P 6s | 1.35 |
| MiniMax-Hailuo-2.3-Fast | 图生视频，768P 10s | 2.25 |
| MiniMax-Hailuo-2.3-Fast | 图生视频，1080P 6s | 2.31 |
| MiniMax-Hailuo-2.3 | 文生视频，图生视频，768P 6s | 2.00 |
| MiniMax-Hailuo-2.3 | 文生视频，图生视频，768P 10s | 4.00 |
| MiniMax-Hailuo-2.3 | 文生视频，图生视频，1080P 6s | 3.50 |
| MiniMax-Hailuo-02 | 文生视频，图生视频，768P 6s | 2.00 |
| MiniMax-Hailuo-02 | 文生视频，图生视频，768P 10s | 4.00 |
| MiniMax-Hailuo-02 | 文生视频，图生视频，1080P 6s | 3.50 |
| MiniMax-Hailuo-02 | 图生视频，512P 6s | 0.60 |
| MiniMax-Hailuo-02 | 图生视频，512P 10s | 1.00 |

---

## 十、来源 URL 列表

- [`https://platform.minimax.cn/docs/guides/pricing-paygo`](https://platform.minimax.cn/docs/guides/pricing-paygo) — 按量计费总览
- [`https://platform.minimax.cn/docs/guides/pricing-paygo#视频`](https://platform.minimax.cn/docs/guides/pricing-paygo#%E8%A7%86%E9%A2%91) — 视频计费段（主源）
- [`https://platform.minimax.cn/user-center/payment/balance`](https://platform.minimax.cn/user-center/payment/balance) — 充值入口
- [`https://platform.minimax.cn/docs/guides/pricing-token-plan`](https://platform.minimax.cn/docs/guides/pricing-token-plan) — Token Plan 积分定价（与按量计费是两套体系，本文档未覆盖）